"""Unit and integration tests for Phase 2 6-Gate evaluator and walk-forward discovery engine."""

from __future__ import annotations

import json
import math
import unittest

from mu_strategy.core.instrument import CostModel, okx_swap_spec, spot_spec
from mu_strategy.lab import (
    DiscoveryConfig,
    DiscoveryReport,
    GateConfig,
    StrategyCatalogEntry,
    build_walk_forward_folds,
    compute_deflated_sharpe_ratio,
    default_strategy_catalog,
    discover_strategies,
    evaluate_six_gates,
    export_winner_recipe,
    overlay_presets,
)
from mu_strategy.market_data.bar_series import BarSeries
from mu_strategy.market_data.funding import FundingSeries
from mu_strategy.models import Candle
from mu_strategy.strategies.families import TSMomentumParams, TSMomentumStrategy
from mu_strategy.strategies.protocol import (
    EMPTY_OUTPUT,
    BarContext,
    ParamSpace,
    ParamSpec,
    Strategy,
    StrategyOutput,
    TargetPosition,
)


def _synthetic_series(
    n_bars: int = 2400,
    *,
    symbol: str = "MU-USDT-SWAP",
    interval: str = "1h",
    start_ms: int = 1_772_323_200_000,  # 2026-03-01T00:00:00Z
    interval_ms: int = 3_600_000,
) -> BarSeries:
    """Build deterministic multi-regime synthetic hourly candles (trend + pullback waves)."""
    candles: list[Candle] = []
    px = 100.0
    for i in range(n_bars):
        # Slow regime waves so trend/breakout and mean-reversion strategies generate real trades.
        wave = 0.0045 * math.sin(i / 45.0) + 0.0025 * math.sin(i / 13.0) + 0.00025
        next_px = max(5.0, px * (1.0 + wave))
        hi = max(px, next_px) * 1.003
        lo = min(px, next_px) * 0.997
        candles.append(
            Candle(
                open_time_ms=start_ms + (i * interval_ms),
                open=px,
                high=hi,
                low=lo,
                close=next_px,
                volume=5000.0 + (i % 17) * 100.0,
            )
        )
        px = next_px
    return BarSeries.from_candles(symbol, interval, candles, interval_ms=interval_ms)


def _synthetic_funding(series: BarSeries, instrument_id: str, apr: float = 0.10) -> FundingSeries:
    return FundingSeries.synthetic_constant_apr(
        instrument_id,
        start_ms=int(series.open_times[0]) - 8 * 3_600_000,
        end_ms=int(series.close_times[-1]) + 8 * 3_600_000,
        interval_hours=8,
        apr=apr,
        phase_ms=0,
    )


class _DummyWeightStrategy:
    """Minimal deterministic Strategy for controlled neighbor/gate unit tests."""

    strategy_id = "dummy_weight"
    version = "1.0.0"
    supported_sides = ("buy", "sell", "long_short")
    supports_ephemeral_context = True

    def __init__(self, weight: float = 1.0, flip_every: int = 24) -> None:
        self.weight = float(weight)
        self.flip_every = int(flip_every)
        self.required_intervals = ("1h",)
        self.warmup_bars = {"1h": 2}
        self._step = 0

    def reset(self) -> None:
        self._step = 0

    def on_bar(self, ctx: BarContext) -> StrategyOutput:
        self._step += 1
        if self.weight == 0.0:
            return EMPTY_OUTPUT
        if self._step % self.flip_every == 1:
            return StrategyOutput(targets=(TargetPosition(symbol=ctx.symbol, target_weight=self.weight),))
        if self._step % self.flip_every == (self.flip_every // 2):
            return StrategyOutput(targets=(TargetPosition(symbol=ctx.symbol, target_weight=0.0),))
        return EMPTY_OUTPUT

    def snapshot_state(self) -> dict[str, int]:
        return {"step": self._step}

    def restore_state(self, state: dict[str, int]) -> None:
        self._step = int(state.get("step", 0))


class DeflatedSharpeRatioTests(unittest.TestCase):
    def test_gaussian_single_trial_symmetry_and_monotonicity(self) -> None:
        self.assertAlmostEqual(0.5, compute_deflated_sharpe_ratio(0.0, 2000, 0.0, 0.0, 1), places=12)
        dsr_low = compute_deflated_sharpe_ratio(1.0, 4000, 0.0, 0.0, 1)
        dsr_high = compute_deflated_sharpe_ratio(3.0, 4000, 0.0, 0.0, 1)
        self.assertGreater(dsr_low, 0.5)
        self.assertGreater(dsr_high, dsr_low)
        self.assertLess(compute_deflated_sharpe_ratio(-1.5, 4000, 0.0, 0.0, 1), 0.5)

    def test_multiple_testing_and_non_normality_penalties(self) -> None:
        dsr_1 = compute_deflated_sharpe_ratio(4.0, 4000, 0.0, 0.0, 1)
        dsr_20 = compute_deflated_sharpe_ratio(4.0, 4000, 0.0, 0.0, 20)
        dsr_100 = compute_deflated_sharpe_ratio(4.0, 4000, 0.0, 0.0, 100)
        self.assertGreater(dsr_1, dsr_20)
        self.assertGreater(dsr_20, dsr_100)

        # Negative skew and fat tails (high excess kurtosis) reduce DSR for positive Sharpe.
        dsr_gauss = compute_deflated_sharpe_ratio(4.5, 4000, 0.0, 0.0, 10)
        dsr_neg_skew = compute_deflated_sharpe_ratio(4.5, 4000, -2.5, 0.0, 10)
        dsr_fat_tail = compute_deflated_sharpe_ratio(4.5, 4000, 0.0, 20.0, 10)
        self.assertLess(dsr_neg_skew, dsr_gauss)
        self.assertLess(dsr_fat_tail, dsr_gauss)

    def test_fail_closed_invalid_and_degenerate_inputs(self) -> None:
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(float("nan"), 1000, 0.0, 0.0, 1))
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(float("inf"), 1000, 0.0, 0.0, 1))
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(2.0, 1000, float("nan"), 0.0, 1))
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(2.0, 1000, 0.0, float("-inf"), 1))
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(2.0, 2, 0.0, 0.0, 1))
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(2.0, 1000, 0.0, 0.0, 0))
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(2.0, 1000, 0.0, 0.0, 1, annualization_factor=0.0))
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(True, 1000, 0.0, 0.0, 1))  # type: ignore[arg-type]
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(2.0, True, 0.0, 0.0, 1))  # type: ignore[arg-type]
        # Extreme skew causing non-positive variance numerator fails closed to 0.0.
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(50.0, 1000, 1000.0, 0.0, 1))


class SixGateEvaluationTests(unittest.TestCase):
    def _base_go_kwargs(self) -> dict[str, object]:
        return {
            "oos_days": 180.0,
            "oos_trades": 40,
            "oos_total_return": 0.45,
            "oos_sharpe": 4.0,
            "oos_max_drawdown": -0.10,
            "oos_calmar": 3.0,
            "liquidations": 0,
            "n_obs": 8760,
            "return_skew": 0.0,
            "return_excess_kurtosis": 0.0,
            "n_trials": 20,
            "positive_fold_ratio": 1.0,
            "is_sharpe": 4.0,
            "is_perp": True,
            "funding_verified": True,
            "stress_passed": True,
            "stress_liquidations": 0,
            "spot_oos_sharpe": 1.0,
            "spot_oos_max_drawdown": -0.30,
            "neighbor_pass_ratio": 0.75,
        }

    def test_go_verdict_all_six_pass_and_json_clean(self) -> None:
        ev = evaluate_six_gates(**self._base_go_kwargs())  # type: ignore[arg-type]
        self.assertEqual("GO", ev.verdict)
        self.assertEqual(6, ev.passed_count)
        self.assertEqual((), ev.failed_gates)
        self.assertGreaterEqual(ev.dsr, 0.95)
        payload = ev.to_dict()
        encoded = json.dumps(payload, allow_nan=False)
        self.assertIn('"verdict": "GO"', encoded)
        # Positive drawdown magnitude produces identical result.
        kw_pos = dict(self._base_go_kwargs(), oos_max_drawdown=0.10, spot_oos_max_drawdown=0.30)
        self.assertEqual(ev.verdict, evaluate_six_gates(**kw_pos).verdict)  # type: ignore[arg-type]

    def test_conditional_verdict_on_soft_gates(self) -> None:
        # 1. G3 WARN (0.80 <= DSR < 0.95) -> CONDITIONAL.
        kw = dict(self._base_go_kwargs(), oos_sharpe=3.0, n_obs=8760, n_trials=20)
        ev_g3 = evaluate_six_gates(**kw)  # type: ignore[arg-type]
        self.assertEqual("WARN", ev_g3.checks[2].status)
        self.assertEqual("CONDITIONAL", ev_g3.verdict)

        # 2. G5 benchmark WARN (stress OK + 0 liq, but lower Sharpe and DD > 0.65 * Spot DD) -> CONDITIONAL.
        kw_g5 = dict(self._base_go_kwargs(), spot_oos_sharpe=6.0, oos_max_drawdown=-0.22, spot_oos_max_drawdown=-0.30)
        ev_g5 = evaluate_six_gates(**kw_g5)  # type: ignore[arg-type]
        self.assertEqual("WARN", ev_g5.checks[4].status)
        self.assertEqual("CONDITIONAL", ev_g5.verdict)

        # 3. G6 soft fail (0.35 <= neighbor_pass_ratio < 0.50) -> CONDITIONAL.
        kw_g6 = dict(self._base_go_kwargs(), neighbor_pass_ratio=0.40)
        ev_g6 = evaluate_six_gates(**kw_g6)  # type: ignore[arg-type]
        self.assertEqual("FAIL", ev_g6.checks[5].status)
        self.assertEqual(("G6",), ev_g6.failed_gates)
        self.assertEqual("CONDITIONAL", ev_g6.verdict)

    def test_no_go_on_hard_gate_failures_and_spot_dd_value_add(self) -> None:
        # G1 sample insufficiency
        self.assertEqual("NO_GO", evaluate_six_gates(**dict(self._base_go_kwargs(), oos_days=30.0)).verdict)  # type: ignore[arg-type]
        self.assertEqual("NO_GO", evaluate_six_gates(**dict(self._base_go_kwargs(), oos_trades=5)).verdict)  # type: ignore[arg-type]
        # G2 unverified perp funding + stress fail -> NO_GO; unverified + stress pass -> PASS G2
        ev_unv = evaluate_six_gates(**dict(self._base_go_kwargs(), funding_verified=False, stress_passed=False))  # type: ignore[arg-type]
        self.assertIn("G2", ev_unv.failed_gates)
        self.assertEqual("NO_GO", ev_unv.verdict)
        ev_unv_ok = evaluate_six_gates(**dict(self._base_go_kwargs(), funding_verified=False, stress_passed=True))  # type: ignore[arg-type]
        self.assertEqual("PASS", ev_unv_ok.checks[1].status)
        # G4 liquidation -> NO_GO
        ev_liq = evaluate_six_gates(**dict(self._base_go_kwargs(), liquidations=1))  # type: ignore[arg-type]
        self.assertIn("G4", ev_liq.failed_gates)
        self.assertEqual("NO_GO", ev_liq.verdict)
        # G5 stress liquidation -> NO_GO
        ev_sliq = evaluate_six_gates(**dict(self._base_go_kwargs(), stress_liquidations=1))  # type: ignore[arg-type]
        self.assertIn("G5", ev_sliq.failed_gates)
        self.assertEqual("NO_GO", ev_sliq.verdict)
        # G5 passes via DD reduction (10% <= 0.65 * 30% = 19.5%) even when Sharpe < Spot Sharpe
        ev_dd_add = evaluate_six_gates(**dict(self._base_go_kwargs(), spot_oos_sharpe=6.0, oos_max_drawdown=-0.10, spot_oos_max_drawdown=-0.30))  # type: ignore[arg-type]
        self.assertEqual("PASS", ev_dd_add.checks[4].status)
        self.assertEqual("GO", ev_dd_add.verdict)
        # G6 hard fail (< 0.35) -> NO_GO
        self.assertEqual("NO_GO", evaluate_six_gates(**dict(self._base_go_kwargs(), neighbor_pass_ratio=0.20)).verdict)  # type: ignore[arg-type]

    def test_gate_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            GateConfig(min_dsr_go=0.80, min_dsr_warn=0.90)
        with self.assertRaises(ValueError):
            GateConfig(min_neighbor_pass_ratio=0.40, min_neighbor_soft_ratio=0.50)
        with self.assertRaises(ValueError):
            GateConfig(min_oos_trades=-1)
        with self.assertRaises(ValueError):
            GateConfig(stress_apr=0.0)


class WalkForwardAndCatalogTests(unittest.TestCase):
    def test_anchored_and_rolling_folds_strictly_causal_and_non_overlapping(self) -> None:
        anchored = build_walk_forward_folds(1000, n_folds=3, is_ratio=0.5, purge_bars=24, mode="anchored")
        self.assertEqual(3, len(anchored))
        for k, fold in enumerate(anchored):
            self.assertEqual(k, fold.fold_index)
            self.assertEqual(0, fold.is_start)
            self.assertLessEqual(fold.is_end + 24, fold.oos_start)
            self.assertLess(fold.oos_start, fold.oos_end)
            if k > 0:
                self.assertEqual(anchored[k - 1].oos_end, fold.oos_start)
        self.assertEqual(1000, anchored[-1].oos_end)

        rolling = build_walk_forward_folds(1000, n_folds=3, is_ratio=0.5, purge_bars=24, mode="rolling")
        self.assertGreater(rolling[1].is_start, rolling[0].is_start)
        for fold in rolling:
            self.assertLessEqual(fold.is_end + 24, fold.oos_start)

        with self.assertRaises(ValueError):
            build_walk_forward_folds(40, n_folds=3, is_ratio=0.5, purge_bars=24)
        with self.assertRaises(ValueError):
            build_walk_forward_folds(1000, n_folds=0)

    def test_default_catalog_and_overlay_presets(self) -> None:
        catalog = default_strategy_catalog(symbol="MU-USDT-SWAP", interval="1h")
        family_ids = [e.family_id for e in catalog]
        self.assertEqual(["ts_momentum", "donchian_atr", "mean_reversion", "fibonacci_pullback"], family_ids)
        total_grid_points = sum(len(list(e.param_space.iter_valid_grid())) for e in catalog)
        self.assertGreaterEqual(total_grid_points, 20)
        self.assertLessEqual(total_grid_points, 40)
        for entry in catalog:
            strat = entry.factory(entry.param_space.defaults())
            self.assertIsInstance(strat, Strategy)

        p1 = overlay_presets()
        p2 = overlay_presets()
        self.assertEqual({"none", "dd20_vol25", "dd15_vol25"}, set(p1.keys()))
        self.assertEqual((), p1["none"])
        self.assertIsNot(p1["dd20_vol25"][0], p2["dd20_vol25"][0])


class DiscoveryEngineIntegrationTests(unittest.TestCase):
    def test_discover_strategies_perp_determinism_attribution_and_json_contract(self) -> None:
        series = _synthetic_series(n_bars=1800)
        inst = okx_swap_spec("MU-USDT-SWAP")
        funding = _synthetic_funding(series, inst.instrument_id, apr=0.10)
        cfg = DiscoveryConfig(
            n_folds=3,
            is_ratio=0.5,
            purge_bars=24,
            overlay_names=("none", "dd20_vol25"),
            family_ids=("ts_momentum", "donchian_atr"),
            top_curves=3,
        )

        report_a = discover_strategies(series, inst, CostModel.okx_default(), funding=funding, config=cfg)
        report_b = discover_strategies(series, inst, CostModel.okx_default(), funding=funding, config=cfg)
        self.assertIsInstance(report_a, DiscoveryReport)
        self.assertTrue(report_a.funding_verified)
        self.assertEqual(24, report_a.n_trials)  # (6 + 6) * 2 overlays

        dict_a = report_a.to_dict()
        dict_b = report_b.to_dict()
        self.assertEqual("phase2.discovery.v1", dict_a["schema_version"])
        dict_a["runtime_ms"] = 0.0
        dict_b["runtime_ms"] = 0.0
        self.assertEqual(dict_a, dict_b)
        json.dumps(dict_a, allow_nan=False)

        # Verify top_curves downsampling policy and exact attribution identity on every candidate.
        for idx, cand in enumerate(report_a.candidates):
            if idx < 3:
                self.assertGreater(len(cand.curve), 1)
            else:
                self.assertEqual((), cand.curve)
            attr = cand.attribution
            expected_net = attr.gross_pnl - attr.fees - attr.slippage + attr.funding_pnl
            self.assertAlmostEqual(attr.net_pnl, expected_net, places=5)
            self.assertAlmostEqual(cand.ending_equity - cand.starting_equity, attr.net_pnl, places=4)
            self.assertEqual(3, len(cand.folds))

    def test_missing_funding_spot_mode_and_neighbor_stability(self) -> None:
        series = _synthetic_series(n_bars=1200, symbol="BTC-USDT-SWAP")
        inst_perp = okx_swap_spec("BTC-USDT-SWAP")

        # Singleton catalog grid -> neighbor_pass_ratio == 1.0, missing funding -> funding_verified=False.
        singleton_entry = StrategyCatalogEntry(
            "singleton_ts",
            "Singleton TSMom",
            lambda p: TSMomentumStrategy(TSMomentumParams(interval="1h", **p)),
            ParamSpace((ParamSpec("lookback_bars", "int", 48, choices=(48,)),)),
        )
        rep_missing = discover_strategies(
            series,
            inst_perp,
            CostModel.okx_default(),
            funding=None,
            catalog=[singleton_entry],
            config=DiscoveryConfig(overlay_names=("none",)),
        )
        self.assertFalse(rep_missing.funding_verified)
        self.assertEqual(1, rep_missing.n_trials)
        self.assertEqual(1.0, rep_missing.candidates[0].neighbor_pass_ratio)

        # Spot mode works with funding=None and spot_1x == perp_1x.
        spot_series = _synthetic_series(n_bars=1200, symbol="BTC-USDT")
        inst_spot = spot_spec("BTC-USDT", venue="okx")
        rep_spot = discover_strategies(
            spot_series,
            inst_spot,
            CostModel.okx_default(),
            funding=None,
            catalog=[singleton_entry],
            config=DiscoveryConfig(overlay_names=("none",)),
        )
        self.assertTrue(rep_spot.funding_verified)
        self.assertEqual(rep_spot.spot_1x, rep_spot.perp_1x)

        # 3-point 1D dummy grid where weight=0.0 fails and weight=1.0/-1.0 behave distinctly:
        # verify exact 1-step neighbor ratios are strictly computed over adjacent indices only.
        dummy_entry = StrategyCatalogEntry(
            "dummy_grid",
            "Dummy 1D Grid",
            lambda p: _DummyWeightStrategy(weight=p["weight"]),
            ParamSpace((ParamSpec("weight", "float", 0.0, choices=(0.0, 0.5, 1.0)),)),
        )
        rep_grid = discover_strategies(
            spot_series,
            inst_spot,
            CostModel.zero_cost(),
            funding=None,
            catalog=[dummy_entry],
            config=DiscoveryConfig(
                overlay_names=("none",),
                gate_config=GateConfig(min_oos_sharpe=0.0, max_drawdown=1.0),
            ),
        )
        by_w = {c.params["weight"]: c.neighbor_pass_ratio for c in rep_grid.candidates}
        # Weight 0.0 has 0% return (fails core), while 0.5 and 1.0 have positive return on upward wave:
        # 0.0's only 1-step neighbor is 0.5 (passes -> 1.0); 0.5's neighbors are 0.0 (fail) & 1.0 (pass -> 0.5);
        # 1.0's only 1-step neighbor is 0.5 (passes -> 1.0).
        self.assertEqual(1.0, by_w[0.0])
        self.assertAlmostEqual(0.5, by_w[0.5])
        self.assertEqual(1.0, by_w[1.0])

    def test_export_winner_recipe_safety_invariants(self) -> None:
        series = _synthetic_series(n_bars=1000)
        inst = okx_swap_spec("MU-USDT-SWAP")
        funding = _synthetic_funding(series, inst.instrument_id)
        entry = StrategyCatalogEntry(
            "flat_only",
            "Always Flat",
            lambda _p: _DummyWeightStrategy(weight=0.0),
            ParamSpace((ParamSpec("weight", "float", 0.0, choices=(0.0,)),)),
        )
        report = discover_strategies(
            series,
            inst,
            CostModel.okx_default(),
            funding=funding,
            catalog=[entry],
            config=DiscoveryConfig(overlay_names=("none",)),
        )
        recipe = export_winner_recipe(report)
        self.assertTrue(recipe["dry_run_only"])
        self.assertFalse(recipe["live_orders_allowed"])
        self.assertEqual("NO_GO_WARNING", recipe["status"])
        self.assertEqual("NO_GO", recipe["verdict"])
        self.assertIsNone(recipe["strategy"])
        self.assertIsNotNone(recipe["best_rejected_candidate"])
        json.dumps(recipe, allow_nan=False)

        # Verify GO & CONDITIONAL export paths always enforce dry_run_only & live_orders_allowed=False.
        go_ev = evaluate_six_gates(
            oos_days=180.0, oos_trades=40, oos_total_return=0.45, oos_sharpe=4.0,
            oos_max_drawdown=-0.10, oos_calmar=3.0, liquidations=0, n_obs=8760,
            return_skew=0.0, return_excess_kurtosis=0.0, n_trials=10, positive_fold_ratio=1.0,
            is_sharpe=4.0, is_perp=True, funding_verified=True, stress_passed=True,
            stress_liquidations=0, spot_oos_sharpe=1.0, spot_oos_max_drawdown=-0.30, neighbor_pass_ratio=1.0,
        )
        cond_ev = evaluate_six_gates(
            oos_days=180.0, oos_trades=40, oos_total_return=0.45, oos_sharpe=4.0,
            oos_max_drawdown=-0.10, oos_calmar=3.0, liquidations=0, n_obs=8760,
            return_skew=0.0, return_excess_kurtosis=0.0, n_trials=10, positive_fold_ratio=1.0,
            is_sharpe=4.0, is_perp=True, funding_verified=True, stress_passed=True,
            stress_liquidations=0, spot_oos_sharpe=1.0, spot_oos_max_drawdown=-0.30, neighbor_pass_ratio=0.40,
        )
        from dataclasses import replace as dc_replace
        go_report = dc_replace(report, candidates=(dc_replace(report.candidates[0], gates=go_ev),))
        go_recipe = export_winner_recipe(go_report)
        self.assertTrue(go_recipe["dry_run_only"])
        self.assertFalse(go_recipe["live_orders_allowed"])
        self.assertEqual("READY_DRY_RUN", go_recipe["status"])
        self.assertEqual("GO", go_recipe["verdict"])
        self.assertIsNone(go_recipe["warning"])
        self.assertEqual("flat_only", go_recipe["strategy"]["family_id"])

        cond_report = dc_replace(report, candidates=(dc_replace(report.candidates[0], gates=cond_ev),))
        cond_recipe = export_winner_recipe(cond_report)
        self.assertTrue(cond_recipe["dry_run_only"])
        self.assertFalse(cond_recipe["live_orders_allowed"])
        self.assertEqual("CONDITIONAL_DRY_RUN", cond_recipe["status"])
        self.assertEqual("CONDITIONAL", cond_recipe["verdict"])
        self.assertIsNotNone(cond_recipe["warning"])


if __name__ == "__main__":
    unittest.main()
