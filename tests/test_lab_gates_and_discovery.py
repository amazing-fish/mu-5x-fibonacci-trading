"""Unit and integration tests for Phase 2 6-Gate evaluator and walk-forward discovery engine."""

from __future__ import annotations

from dataclasses import replace as dc_replace
import json
import math
import unittest

from mu_strategy.core.instrument import CostModel, okx_swap_spec, spot_spec
from mu_strategy.engine.metrics import CostAttribution, TradeRecord, compute_simulation_metrics
from mu_strategy.lab import (
    DiscoveryConfig,
    DiscoveryReport,
    GateConfig,
    StrategyCatalogEntry,
    StressScenarioResult,
    build_walk_forward_folds,
    compute_deflated_sharpe_ratio,
    compute_null_distribution_pvalue,
    default_strategy_catalog,
    discover_strategies,
    evaluate_six_gates,
    export_winner_recipe,
    overlay_presets,
)
from mu_strategy.lab.discovery import _stitch_fold_results
from mu_strategy.market_data.bar_series import BarSeries
from mu_strategy.market_data.funding import FundingSeries
from mu_strategy.models import Candle
from mu_strategy.strategies.families import (
    FibonacciPullbackParams,
    TSMomentumParams,
    TSMomentumStrategy,
)
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


class DeflatedSharpeAndNullDistTests(unittest.TestCase):
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
        self.assertEqual(0.0, compute_deflated_sharpe_ratio(50.0, 1000, 1000.0, 0.0, 1))

    def test_null_distribution_pvalue_determinism_significance_and_fail_closed(self) -> None:
        # Strong steady upward drift with jitter -> small significant p-value and deterministic across calls.
        eq = 10_000.0
        strong_curve: list[tuple[int, float]] = [(0, eq)]
        for i in range(1, 720):
            r = 0.002 + (0.0005 if i % 3 else -0.0003)
            eq *= 1.0 + r
            strong_curve.append((i * 3_600_000, eq))
        p1 = compute_null_distribution_pvalue(strong_curve, block_bars=24, n_permutations=200, seed=42)
        p2 = compute_null_distribution_pvalue(strong_curve, block_bars=24, n_permutations=200, seed=42)
        self.assertEqual(p1, p2)
        self.assertLessEqual(p1, 0.05)

        # Fail closed to 1.0 on flat, losing, too-short, or non-finite curves.
        self.assertEqual(1.0, compute_null_distribution_pvalue([(0, 100.0), (1, 101.0), (2, 102.0)]))
        self.assertEqual(1.0, compute_null_distribution_pvalue([(i, 100.0) for i in range(100)]))
        self.assertEqual(1.0, compute_null_distribution_pvalue([(i, 100.0 - i * 0.1) for i in range(100)]))
        self.assertEqual(1.0, compute_null_distribution_pvalue([(0, 100.0), (1, float("nan")), (2, 102.0), (3, 103.0)]))


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
            "null_p_value": 0.01,
        }

    def test_go_verdict_all_six_pass_and_json_clean(self) -> None:
        ev = evaluate_six_gates(**self._base_go_kwargs())  # type: ignore[arg-type]
        self.assertEqual("GO", ev.verdict)
        self.assertEqual(6, ev.passed_count)
        self.assertEqual((), ev.failed_gates)
        self.assertTrue(ev.null_passed)
        self.assertAlmostEqual(0.01, ev.null_p_value or 0.0)
        self.assertGreaterEqual(ev.dsr, 0.95)
        payload = ev.to_dict()
        encoded = json.dumps(payload, allow_nan=False)
        self.assertIn('"verdict": "GO"', encoded)
        self.assertTrue(payload["null_passed"])
        self.assertEqual(0.01, payload["null_p_value"])
        kw_pos = dict(self._base_go_kwargs(), oos_max_drawdown=0.10, spot_oos_max_drawdown=0.30)
        self.assertEqual(ev.verdict, evaluate_six_gates(**kw_pos).verdict)  # type: ignore[arg-type]

    def test_null_p_value_omitted_or_failing_blocks_go(self) -> None:
        # P2R1-09: omitted null_p_value fails closed and NEVER returns GO.
        ev_missing = evaluate_six_gates(**dict(self._base_go_kwargs(), null_p_value=None))  # type: ignore[arg-type]
        self.assertFalse(ev_missing.null_passed)
        self.assertIsNone(ev_missing.null_p_value)
        self.assertNotEqual("GO", ev_missing.verdict)
        self.assertEqual("WARN", ev_missing.checks[2].status)

        # P2R1-09: failing null_p_value > max_null_p_value blocks G3 PASS and GO.
        ev_high_p = evaluate_six_gates(**dict(self._base_go_kwargs(), null_p_value=0.25))  # type: ignore[arg-type]
        self.assertFalse(ev_high_p.null_passed)
        self.assertNotEqual("GO", ev_high_p.verdict)
        self.assertEqual("WARN", ev_high_p.checks[2].status)

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

    def test_no_go_on_hard_gate_failures_and_unverified_funding(self) -> None:
        # G1 sample insufficiency
        self.assertEqual("NO_GO", evaluate_six_gates(**dict(self._base_go_kwargs(), oos_days=30.0)).verdict)  # type: ignore[arg-type]
        self.assertEqual("NO_GO", evaluate_six_gates(**dict(self._base_go_kwargs(), oos_trades=5)).verdict)  # type: ignore[arg-type]
        # P2R1-08: missing/unverified perp funding ALWAYS fails G2 (NO_GO), even when stress_passed=True!
        ev_unv = evaluate_six_gates(**dict(self._base_go_kwargs(), funding_verified=False, stress_passed=True))  # type: ignore[arg-type]
        self.assertEqual("FAIL", ev_unv.checks[1].status)
        self.assertIn("G2", ev_unv.failed_gates)
        self.assertIn("unverified", ev_unv.checks[1].detail)
        self.assertEqual("NO_GO", ev_unv.verdict)
        # Spot does not require perp funding verification.
        ev_spot = evaluate_six_gates(**dict(self._base_go_kwargs(), is_perp=False, funding_verified=False))  # type: ignore[arg-type]
        self.assertEqual("PASS", ev_spot.checks[1].status)
        self.assertEqual("GO", ev_spot.verdict)
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
            GateConfig(max_null_p_value=0.0)
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
            self.assertTrue(len(entry.fixed_params) > 0)
            strat = entry.factory(entry.param_space.defaults())
            self.assertIsInstance(strat, Strategy)

        p1 = overlay_presets()
        p2 = overlay_presets()
        self.assertEqual({"none", "dd20_vol25", "dd15_vol25"}, set(p1.keys()))
        self.assertEqual((), p1["none"])
        self.assertIsNot(p1["dd20_vol25"][0], p2["dd20_vol25"][0])

    def test_stitch_fold_results_scales_trade_amounts_with_compounded_capital(self) -> None:
        # P2R1-05 regression: +1000 then -1000 on 10,000 yields equity 9,900 and PF ~0.909 (not 1.0).
        tr_win = TradeRecord(
            entry_time_ms=0, exit_time_ms=3_600_000, side="long", entry_price=100.0, exit_price=110.0,
            qty=100.0, gross_pnl=1000.0, fees=0.0, slippage=0.0, funding_pnl=0.0, net_pnl=1000.0,
            return_pct=0.10, reason="exit", symbol="MU-USDT-SWAP",
        )
        tr_loss = TradeRecord(
            entry_time_ms=7_200_000, exit_time_ms=10_800_000, side="long", entry_price=100.0, exit_price=90.0,
            qty=100.0, gross_pnl=-1000.0, fees=0.0, slippage=0.0, funding_pnl=0.0, net_pnl=-1000.0,
            return_pct=-0.10, reason="exit", symbol="MU-USDT-SWAP",
        )
        f1 = compute_simulation_metrics(
            starting_equity=10_000.0, ending_equity=11_000.0,
            equity_curve=((0, 10_000.0), (3_600_000, 11_000.0)),
            trades=(tr_win,),
            attribution=CostAttribution.from_components(gross_pnl=1000.0, fees=0.0, slippage=0.0, funding_pnl=0.0),
            interval_ms=3_600_000, symbol="MU-USDT-SWAP", interval="1h", strategy_id="test",
        )
        f2 = compute_simulation_metrics(
            starting_equity=10_000.0, ending_equity=9_000.0,
            equity_curve=((7_200_000, 10_000.0), (10_800_000, 9_000.0)),
            trades=(tr_loss,),
            attribution=CostAttribution.from_components(gross_pnl=-1000.0, fees=0.0, slippage=0.0, funding_pnl=0.0),
            interval_ms=3_600_000, symbol="MU-USDT-SWAP", interval="1h", strategy_id="test",
        )
        stitched = _stitch_fold_results(
            (f1, f2), starting_equity=10_000.0, interval_ms=3_600_000,
            symbol="MU-USDT-SWAP", interval="1h", strategy_id="test",
        )
        self.assertAlmostEqual(9_900.0, stitched.ending_equity, places=6)
        self.assertAlmostEqual(-100.0, sum(t.net_pnl for t in stitched.trades), places=6)
        self.assertAlmostEqual(110.0, stitched.trades[1].qty, places=6)
        self.assertAlmostEqual(1000.0 / 1100.0, stitched.profit_factor, places=6)


class DiscoveryEngineIntegrationTests(unittest.TestCase):
    def test_discover_strategies_perp_determinism_stress_suite_and_provenance(self) -> None:
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
        prov = {
            "dataset_sha256": "abc123",
            "funding_sha256": "def456",
            "code_revision": "deadbeef",
            "fee_profile": "okx_default",
            "data_source": "unit_test",
            "generation_id": "gen-1",
            "days": 75,
        }

        report_a = discover_strategies(series, inst, CostModel.okx_default(), funding=funding, config=cfg, provenance=prov)
        report_b = discover_strategies(series, inst, CostModel.okx_default(), funding=funding, config=cfg, provenance=prov)
        self.assertIsInstance(report_a, DiscoveryReport)
        self.assertTrue(report_a.funding_verified)
        self.assertEqual(24, report_a.n_trials)

        dict_a = report_a.to_dict()
        dict_b = report_b.to_dict()
        self.assertEqual("phase2.discovery.v1", dict_a["schema_version"])
        self.assertEqual(prov, dict_a["provenance"])
        self.assertEqual(24, dict_a["effective_config"]["purge_bars"])
        self.assertEqual(
            ["slippage_10bp", "funding_0pct_apr", "funding_15pct_apr", "funding_30pct_apr"],
            dict_a["effective_config"]["stress_scenarios"],
        )
        self.assertEqual(CostModel.okx_default().to_dict(), dict_a["effective_config"]["cost_model"])
        dict_a["runtime_ms"] = 0.0
        dict_b["runtime_ms"] = 0.0
        self.assertEqual(dict_a, dict_b)
        json.dumps(dict_a, allow_nan=False)

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
            # P2R1-06 & P2R1-11: all 4 scenarios always emitted and available for perp.
            self.assertEqual(4, len(cand.stress_scenarios))
            self.assertEqual(
                ["slippage_10bp", "funding_0pct_apr", "funding_15pct_apr", "funding_30pct_apr"],
                [sc.scenario_id for sc in cand.stress_scenarios],
            )
            self.assertTrue(all(sc.available and isinstance(sc, StressScenarioResult) for sc in cand.stress_scenarios))
            # P2R1-09: null_p_value computed & wired into gates.
            self.assertIsNotNone(cand.gates.null_p_value)

    def test_missing_funding_spot_stress_na_and_neighbor_stability(self) -> None:
        series = _synthetic_series(n_bars=1200, symbol="BTC-USDT-SWAP")
        inst_perp = okx_swap_spec("BTC-USDT-SWAP")

        singleton_entry = StrategyCatalogEntry(
            "singleton_ts",
            "Singleton TSMom",
            lambda p: TSMomentumStrategy(TSMomentumParams(interval="1h", **p)),
            ParamSpace((ParamSpec("lookback_bars", "int", 48, choices=(48,)),)),
            fixed_params={"interval": "1h"},
        )
        rep_missing = discover_strategies(
            series, inst_perp, CostModel.okx_default(), funding=None,
            catalog=[singleton_entry], config=DiscoveryConfig(overlay_names=("none",)),
        )
        self.assertFalse(rep_missing.funding_verified)
        self.assertEqual(1, rep_missing.n_trials)
        self.assertEqual(1.0, rep_missing.candidates[0].neighbor_pass_ratio)
        # Missing perp funding must fail G2 cleanly.
        self.assertIn("G2", rep_missing.candidates[0].gates.failed_gates)
        self.assertEqual("NO_GO", rep_missing.candidates[0].gates.verdict)

        # Spot mode: slippage_10bp available, 3 funding scenarios explicitly unavailable (N/A).
        spot_series = _synthetic_series(n_bars=1200, symbol="BTC-USDT")
        inst_spot = spot_spec("BTC-USDT", venue="okx")
        rep_spot = discover_strategies(
            spot_series, inst_spot, CostModel.okx_default(), funding=None,
            catalog=[singleton_entry], config=DiscoveryConfig(overlay_names=("none",)),
        )
        self.assertTrue(rep_spot.funding_verified)
        self.assertEqual(rep_spot.spot_1x, rep_spot.perp_1x)
        spot_scenarios = rep_spot.candidates[0].stress_scenarios
        self.assertEqual(4, len(spot_scenarios))
        self.assertTrue(spot_scenarios[0].available)
        self.assertEqual("slippage_10bp", spot_scenarios[0].scenario_id)
        for sc in spot_scenarios[1:]:
            self.assertFalse(sc.available)
            self.assertIsNone(sc.passed)
            self.assertIsNone(sc.total_return)
            self.assertIsNone(sc.liquidations)
            self.assertIn("spot", sc.detail.lower())

        # Exact 1-step neighbor ratios on 3-point 1D grid.
        dummy_entry = StrategyCatalogEntry(
            "dummy_grid",
            "Dummy 1D Grid",
            lambda p: _DummyWeightStrategy(weight=p["weight"]),
            ParamSpace((ParamSpec("weight", "float", 0.0, choices=(0.0, 0.5, 1.0)),)),
        )
        rep_grid = discover_strategies(
            spot_series, inst_spot, CostModel.zero_cost(), funding=None,
            catalog=[dummy_entry],
            config=DiscoveryConfig(overlay_names=("none",), gate_config=GateConfig(min_oos_sharpe=0.0, max_drawdown=1.0)),
        )
        by_w = {c.params["weight"]: c.neighbor_pass_ratio for c in rep_grid.candidates}
        self.assertEqual(1.0, by_w[0.0])
        self.assertAlmostEqual(0.5, by_w[0.5])
        self.assertEqual(1.0, by_w[1.0])

    def test_export_winner_recipe_preserves_fibonacci_fixed_params_and_safety(self) -> None:
        # P2R1-07 regression: Fibonacci recipe must preserve leverage=2.0 and use_session_window=False.
        series = _synthetic_series(n_bars=1000)
        inst = okx_swap_spec("MU-USDT-SWAP")
        funding = _synthetic_funding(series, inst.instrument_id)
        fib_report = discover_strategies(
            series, inst, CostModel.okx_default(), funding=funding,
            config=DiscoveryConfig(overlay_names=("none",), family_ids=("fibonacci_pullback",)),
        )
        go_ev = evaluate_six_gates(
            oos_days=180.0, oos_trades=40, oos_total_return=0.45, oos_sharpe=4.0,
            oos_max_drawdown=-0.10, oos_calmar=3.0, liquidations=0, n_obs=8760,
            return_skew=0.0, return_excess_kurtosis=0.0, n_trials=10, positive_fold_ratio=1.0,
            is_sharpe=4.0, is_perp=True, funding_verified=True, stress_passed=True,
            stress_liquidations=0, spot_oos_sharpe=1.0, spot_oos_max_drawdown=-0.30,
            neighbor_pass_ratio=1.0, null_p_value=0.01,
        )
        cond_ev = evaluate_six_gates(
            oos_days=180.0, oos_trades=40, oos_total_return=0.45, oos_sharpe=4.0,
            oos_max_drawdown=-0.10, oos_calmar=3.0, liquidations=0, n_obs=8760,
            return_skew=0.0, return_excess_kurtosis=0.0, n_trials=10, positive_fold_ratio=1.0,
            is_sharpe=4.0, is_perp=True, funding_verified=True, stress_passed=True,
            stress_liquidations=0, spot_oos_sharpe=1.0, spot_oos_max_drawdown=-0.30,
            neighbor_pass_ratio=0.40, null_p_value=0.01,
        )
        go_report = dc_replace(fib_report, candidates=(dc_replace(fib_report.candidates[0], gates=go_ev),))
        go_recipe = export_winner_recipe(go_report)
        self.assertTrue(go_recipe["dry_run_only"])
        self.assertFalse(go_recipe["live_orders_allowed"])
        self.assertEqual("READY_DRY_RUN", go_recipe["status"])
        self.assertEqual("GO", go_recipe["verdict"])
        self.assertIsNone(go_recipe["warning"])
        self.assertEqual("fibonacci_pullback", go_recipe["strategy"]["catalog_entry_id"])
        self.assertIn("fib_lookback", go_recipe["strategy"]["swept_params"])
        rebuilt = FibonacciPullbackParams(**go_recipe["strategy"]["params"])
        self.assertEqual(2.0, rebuilt.leverage)
        self.assertFalse(rebuilt.use_session_window)
        self.assertEqual("MU-USDT-SWAP", rebuilt.symbol)
        self.assertEqual("1h", rebuilt.primary_interval)
        self.assertEqual("1h", rebuilt.regime_interval)

        cond_report = dc_replace(fib_report, candidates=(dc_replace(fib_report.candidates[0], gates=cond_ev),))
        cond_recipe = export_winner_recipe(cond_report)
        self.assertTrue(cond_recipe["dry_run_only"])
        self.assertFalse(cond_recipe["live_orders_allowed"])
        self.assertEqual("CONDITIONAL_DRY_RUN", cond_recipe["status"])
        self.assertEqual("CONDITIONAL", cond_recipe["verdict"])
        self.assertIsNotNone(cond_recipe["warning"])

        # NO_GO warning path also carries full params, swept_params, and catalog_entry_id.
        flat_entry = StrategyCatalogEntry(
            "flat_only", "Always Flat", lambda _p: _DummyWeightStrategy(weight=0.0),
            ParamSpace((ParamSpec("weight", "float", 0.0, choices=(0.0,)),)),
            fixed_params={"note": "flat"},
        )
        nogo_report = discover_strategies(
            series, inst, CostModel.okx_default(), funding=funding,
            catalog=[flat_entry], config=DiscoveryConfig(overlay_names=("none",)),
        )
        nogo_recipe = export_winner_recipe(nogo_report)
        self.assertTrue(nogo_recipe["dry_run_only"])
        self.assertFalse(nogo_recipe["live_orders_allowed"])
        self.assertEqual("NO_GO_WARNING", nogo_recipe["status"])
        self.assertEqual("NO_GO", nogo_recipe["verdict"])
        self.assertIsNone(nogo_recipe["strategy"])
        self.assertEqual("flat_only", nogo_recipe["best_rejected_candidate"]["catalog_entry_id"])
        self.assertEqual("flat", nogo_recipe["best_rejected_candidate"]["params"]["note"])
        json.dumps(nogo_recipe, allow_nan=False)

    def test_effective_config_serializes_gate_config_and_instrument_spec(self) -> None:
        # P2R2-02 regression: custom GateConfig and InstrumentSpec must appear in effective_config.
        series = _synthetic_series(n_bars=1000, symbol="ETH-USDT-SWAP")
        custom_inst = okx_swap_spec(
            "ETH-USDT-SWAP",
            tick_size="0.05",
            lot_size="0.1",
            min_size="0.2",
            contract_value="0.1",
            max_leverage=10.0,
            funding_interval_hours=8,
        )
        custom_gate = GateConfig(min_dsr_warn=0.90, max_drawdown=0.18, min_oos_trades=20)
        entry = StrategyCatalogEntry(
            "singleton_eth",
            "Singleton ETH",
            lambda _p: _DummyWeightStrategy(weight=0.0),
            ParamSpace((ParamSpec("weight", "float", 0.0, choices=(0.0,)),)),
        )
        report = discover_strategies(
            series,
            custom_inst,
            CostModel.okx_default(),
            funding=_synthetic_funding(series, custom_inst.instrument_id),
            catalog=[entry],
            config=DiscoveryConfig(
                overlay_names=("none",),
                stress_apr=0.25,
                gate_config=custom_gate,
            ),
        )
        payload = report.to_dict()
        json.dumps(payload, allow_nan=False)
        eff = payload["effective_config"]
        self.assertIn("gate_config", eff)
        self.assertIn("instrument", eff)
        self.assertEqual(0.90, eff["gate_config"]["min_dsr_warn"])
        self.assertEqual(0.18, eff["gate_config"]["max_drawdown"])
        self.assertEqual(20, eff["gate_config"]["min_oos_trades"])
        self.assertEqual(0.25, eff["gate_config"]["stress_apr"])
        self.assertEqual(custom_inst.to_dict(), eff["instrument"])
        self.assertEqual("0.05", eff["instrument"]["tick_size"])
        self.assertEqual(10.0, eff["instrument"]["max_leverage"])


if __name__ == "__main__":
    unittest.main()
