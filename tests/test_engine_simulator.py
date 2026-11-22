"""End-to-end Simulator tests: L/S, signed funding, attribution, causality, sensitivity, and speed."""

from __future__ import annotations

import math
import os
import tempfile
import time
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mu_strategy.core.instrument import (
    AtrBps,
    CostModel,
    FixedTicks,
    SqrtVolumeImpact,
    binance_perp_spec,
    okx_swap_spec,
    spot_spec,
)
from mu_strategy.core.trading_calendar import US_EQUITIES_ALGORITHMIC_CALENDAR
from mu_strategy.engine import (
    CostAttribution,
    DailyLossLimitOverlay,
    DrawdownCircuitBreaker,
    ExposureCapOverlay,
    SessionWindowOverlay,
    Simulator,
    SimulatorConfig,
    VolTargetOverlay,
    apply_risk_overlays,
    compute_buy_and_hold_benchmark,
    compute_simulation_metrics,
)
from mu_strategy.engine.metrics import _finalize_curve_stats
from mu_strategy.market_data.bar_series import BarSeries, load_or_build_bar_series
from mu_strategy.market_data.funding import (
    FundingRateEvent,
    FundingSeries,
    MissingFundingHistoryError,
)
from mu_strategy.models import Candle
from mu_strategy.strategies.families import (
    DonchianATRStrategy,
    MeanReversionStrategy,
    TSMomentumStrategy,
)
from mu_strategy.strategies.features import FeatureCache
from mu_strategy.strategies.protocol import (
    EMPTY_OUTPUT,
    BarContext,
    OrderIntent,
    ParamSpace,
    PriceExclusionBand,
    SessionGate,
    StrategyOutput,
    TargetPosition,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DATA_DIR = REPO_ROOT / "data" / "live"


def _make_series(
    ohlcv: list[tuple[float, float, float, float, float]],
    *,
    symbol: str = "MU-USDT-SWAP",
    interval: str = "1h",
    start_ms: int = 1_735_689_600_000,
) -> BarSeries:
    step = 3_600_000 if interval == "1h" else 900_000
    candles = [
        Candle(
            open_time_ms=start_ms + i * step,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
        )
        for i, (o, h, l, c, v) in enumerate(ohlcv)
    ]
    return BarSeries.from_candles(symbol, interval, candles)


def _assert_no_neg_zero_or_nan(test: unittest.TestCase, value: float, label: str) -> None:
    test.assertFalse(math.isnan(value), f"{label} must not be NaN")
    test.assertFalse(value == 0.0 and math.copysign(1.0, value) < 0.0, f"{label} must not be -0.0")


def _assert_accounting_invariants(test: unittest.TestCase, result) -> None:
    attr = result.attribution
    for name, val in (
        ("gross_pnl", attr.gross_pnl),
        ("fees", attr.fees),
        ("slippage", attr.slippage),
        ("funding_pnl", attr.funding_pnl),
        ("net_pnl", attr.net_pnl),
        ("starting_equity", result.starting_equity),
        ("ending_equity", result.ending_equity),
        ("total_return_pct", result.total_return_pct),
        ("max_drawdown_pct", result.max_drawdown_pct),
        ("sharpe_ratio", result.sharpe_ratio),
        ("sortino_ratio", result.sortino_ratio),
        ("calmar_ratio", result.calmar_ratio),
        ("win_rate", result.win_rate),
        ("expectancy", result.expectancy),
        ("turnover", result.turnover),
        ("time_in_market_pct", result.time_in_market_pct),
        ("ex_top3_net_pnl", result.ex_top3_net_pnl),
        ("ex_top5_net_pnl", result.ex_top5_net_pnl),
    ):
        test.assertTrue(math.isfinite(val), f"{name} must be finite, got {val!r}")
        _assert_no_neg_zero_or_nan(test, val, name)
    if result.top3_pnl_share is not None:
        test.assertTrue(math.isfinite(result.top3_pnl_share))
        _assert_no_neg_zero_or_nan(test, result.top3_pnl_share, "top3_pnl_share")
    _assert_no_neg_zero_or_nan(test, result.profit_factor, "profit_factor")
    expected_net = attr.gross_pnl - attr.fees - attr.slippage + attr.funding_pnl
    test.assertLessEqual(abs(attr.net_pnl - expected_net), 1e-6)
    test.assertLessEqual(abs((result.ending_equity - result.starting_equity) - attr.net_pnl), 1e-6)
    test.assertLessEqual(result.max_drawdown_pct, 0.0)
    for t in result.trades:
        t_exp = t.gross_pnl - t.fees - t.slippage + t.funding_pnl
        test.assertLessEqual(abs(t.net_pnl - t_exp), 1e-6)
        test.assertGreater(t.qty, 0.0)
        for fn in ("gross_pnl", "fees", "slippage", "funding_pnl", "net_pnl", "return_pct"):
            _assert_no_neg_zero_or_nan(test, getattr(t, fn), f"trade.{fn}")
    # Canonical bar-close timestamps must be strictly increasing and values clean.
    for idx, (ts, eq) in enumerate(result.equity_curve):
        _assert_no_neg_zero_or_nan(test, eq, f"equity_curve[{idx}]")
        if idx > 0:
            test.assertGreater(ts, result.equity_curve[idx - 1][0])


class _ScriptedStrategy:
    """Minimal scripted test strategy emitting exact StrategyOutput objects by bar index."""

    strategy_id = "scripted_output"
    version = "1.0.0"

    def __init__(
        self,
        script: Mapping[int, StrategyOutput],
        *,
        interval: str = "1h",
        warmup: int = 0,
        supports_ephemeral_context: bool = False,
    ) -> None:
        self.script = dict(script)
        self.required_intervals = (interval,)
        self.warmup_bars = {interval: warmup}
        self.supports_ephemeral_context = supports_ephemeral_context
        self.params = None
        self._step = 0

    def reset(self) -> None:
        self._step = 0

    def on_bar(self, ctx: BarContext) -> StrategyOutput:
        step = self._step
        self._step += 1
        return self.script.get(step, EMPTY_OUTPUT)

    def snapshot_state(self) -> dict[str, Any]:
        return {"step": self._step}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self._step = int(state["step"])


class _ScriptedTargetStrategy:
    """Minimal deterministic test strategy emitting scripted TargetPositions by bar index."""

    strategy_id = "scripted_target"
    version = "1.0.0"
    supported_sides = ("buy", "sell", "long_short")

    def __init__(
        self,
        schedule: Mapping[int, tuple[float, float | None, float | None]],
        *,
        interval: str = "1h",
        warmup: int = 0,
        supports_ephemeral_context: bool = False,
    ) -> None:
        self.schedule = dict(schedule)
        self.required_intervals = (interval,)
        self.warmup_bars = {interval: warmup}
        self.supports_ephemeral_context = supports_ephemeral_context
        self.params = None
        self.seen_now_ms: list[int] = []
        self.seen_closed_counts: list[int] = []
        self._bar_idx = 0

    def param_space(self) -> ParamSpace:
        return ParamSpace()

    def reset(self) -> None:
        self.seen_now_ms = []
        self.seen_closed_counts = []
        self._bar_idx = 0

    def on_bar(self, ctx: BarContext) -> StrategyOutput:
        bars = ctx.market.closed_bars(ctx.symbol, self.required_intervals[0])
        # Structural causality check inside on_bar: every visible bar must have close_time_ms <= now_ms.
        if len(bars) > 0:
            assert bars.close_times[-1] <= ctx.now_ms
        self.seen_now_ms.append(ctx.now_ms)
        self.seen_closed_counts.append(len(bars))

        idx = self._bar_idx
        self._bar_idx += 1
        if idx not in self.schedule:
            return EMPTY_OUTPUT
        weight, stop_px, tp_px = self.schedule[idx]
        return StrategyOutput(
            targets=(
                TargetPosition(
                    symbol=ctx.symbol,
                    target_weight=weight,
                    stop_price=stop_px,
                    take_profit=tp_px,
                    reason=f"step_{idx}",
                ),
            )
        )

    def snapshot_state(self) -> dict[str, Any]:
        return {"bar_idx": self._bar_idx}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self._bar_idx = int(state["bar_idx"])


class SimulatorCoreTests(unittest.TestCase):
    def test_long_short_flip_and_exact_cost_attribution_identity(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.05"), max_leverage=5.0)
        cost = CostModel(
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            half_spread_bps=5.0,
            slippage_model=FixedTicks(1.0),
            stop_slippage_multiplier=1.5,
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
        )
        # Bar 0 close -> signal long +2x; Bar 1 open fills long at 100 -> rises to 110;
        # Bar 2 close -> signal flip to short -2x; Bar 3 open fills flip at 110 -> drops to 95;
        # Bar 4 close -> end_of_data closes short at 95!
        bars = _make_series(
            [
                (99.0, 101.0, 98.5, 100.0, 10_000.0),
                (100.0, 106.0, 99.5, 105.0, 10_000.0),
                (105.0, 111.0, 104.5, 110.0, 10_000.0),
                (110.0, 110.5, 101.0, 102.0, 10_000.0),
                (102.0, 102.5, 94.0, 95.0, 10_000.0),
            ]
        )
        # Positive funding on Bar 2 close (long pays) and negative funding on Bar 4 close (short pays)
        funding = FundingSeries.from_events(
            inst.instrument_id,
            [
                FundingRateEvent(ts_ms=int(bars.close_times[2]), rate=0.0010),
                FundingRateEvent(ts_ms=int(bars.close_times[4]), rate=-0.0005),
            ],
            settlement_interval_hours=8,
        )
        strat = _ScriptedTargetStrategy({0: (2.0, None, None), 2: (-2.0, None, None)})
        sim = Simulator(inst, cost, SimulatorConfig(starting_equity=10_000.0, close_on_end=True))
        res = sim.run(strat, bars, funding=funding)

        _assert_accounting_invariants(self, res)
        self.assertEqual(2, res.trade_count)
        self.assertEqual("long", res.trades[0].side)
        self.assertEqual("short", res.trades[1].side)
        self.assertGreater(res.trades[0].gross_pnl, 0.0)
        self.assertGreater(res.trades[1].gross_pnl, 0.0)
        self.assertGreater(res.attribution.fees, 0.0)
        self.assertGreater(res.attribution.slippage, 0.0)
        self.assertLess(res.trades[0].funding_pnl, 0.0)  # long paid positive funding
        self.assertLess(res.trades[1].funding_pnl, 0.0)  # short paid negative funding

        # Also verify close_on_end=False preserves exact attribution identity with unrealized open position!
        sim_open = Simulator(inst, cost, SimulatorConfig(starting_equity=10_000.0, close_on_end=False))
        res_open = sim_open.run(strat, bars, funding=funding)
        _assert_accounting_invariants(self, res_open)
        self.assertEqual(1, res_open.trade_count)

    def test_exact_bidirectional_signed_funding_settlement(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        bars = _make_series([(100.0, 100.0, 100.0, 100.0, 1000.0)] * 5)
        # Positive rate (+0.001) on bar 2, negative rate (-0.002) on bar 3
        pos_funding = FundingSeries.from_events(
            inst.instrument_id,
            [FundingRateEvent(ts_ms=int(bars.close_times[2]), rate=0.001)],
        )
        neg_funding = FundingSeries.from_events(
            inst.instrument_id,
            [FundingRateEvent(ts_ms=int(bars.close_times[2]), rate=-0.002)],
        )
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))

        # 1. Positive funding (+0.001) on 1x ($10,000 notional):
        #    LONG pays -$10, SHORT collects +$10!
        res_long_pos = sim.run(_ScriptedTargetStrategy({0: (1.0, None, None)}), bars, funding=pos_funding)
        res_short_pos = sim.run(_ScriptedTargetStrategy({0: (-1.0, None, None)}), bars, funding=pos_funding)
        self.assertAlmostEqual(-10.0, res_long_pos.attribution.funding_pnl, places=6)
        self.assertAlmostEqual(9_990.0, res_long_pos.ending_equity, places=6)
        self.assertAlmostEqual(+10.0, res_short_pos.attribution.funding_pnl, places=6)
        self.assertAlmostEqual(10_010.0, res_short_pos.ending_equity, places=6)

        # 2. Negative funding (-0.002) on 1x ($10,000 notional):
        #    LONG collects +$20, SHORT pays -$20!
        res_long_neg = sim.run(_ScriptedTargetStrategy({0: (1.0, None, None)}), bars, funding=neg_funding)
        res_short_neg = sim.run(_ScriptedTargetStrategy({0: (-1.0, None, None)}), bars, funding=neg_funding)
        self.assertAlmostEqual(+20.0, res_long_neg.attribution.funding_pnl, places=6)
        self.assertAlmostEqual(10_020.0, res_long_neg.ending_equity, places=6)
        self.assertAlmostEqual(-20.0, res_short_neg.attribution.funding_pnl, places=6)
        self.assertAlmostEqual(9_980.0, res_short_neg.ending_equity, places=6)
        for r in (res_long_pos, res_short_pos, res_long_neg, res_short_neg):
            _assert_accounting_invariants(self, r)

    def test_zero_lookahead_truncation_invariance_proof(self) -> None:
        inst = spot_spec("BTCUSDT", venue="binance", tick_size=Decimal("0.01"))
        cost = CostModel(maker_fee_bps=2.0, taker_fee_bps=5.0, half_spread_bps=1.0)

        # Build 160 hourly bars; mutate bars 120..159 wildly in the second copy.
        base_ohlcv = []
        px = 100.0
        for i in range(160):
            drift = 0.35 if (i // 20) % 2 == 0 else -0.30
            o = px
            c = max(10.0, o + drift)
            h = max(o, c) + 0.5
            l = min(o, c) - 0.5
            base_ohlcv.append((o, h, l, c, 5000.0))
            px = c

        mutated_ohlcv = list(base_ohlcv[:120])
        for i in range(120, 160):
            o = base_ohlcv[119][3] * (2.5 if i % 2 == 0 else 0.4)
            mutated_ohlcv.append((o, o * 1.1, o * 0.9, o * 1.05, 5000.0))

        bars_orig = _make_series(base_ohlcv, symbol="BTCUSDT")
        bars_mut = _make_series(mutated_ohlcv, symbol="BTCUSDT")

        sim = Simulator(inst, cost, SimulatorConfig(starting_equity=10_000.0, close_on_end=False))
        strat1 = TSMomentumStrategy(interval="1h", lookback_bars=24, side_mode="buy")
        strat2 = TSMomentumStrategy(interval="1h", lookback_bars=24, side_mode="buy")
        res1 = sim.run(strat1, bars_orig, feature_cache=FeatureCache())
        res2 = sim.run(strat2, bars_mut, feature_cache=FeatureCache())

        # Every single bar-close equity sample BEFORE cutoff bar 120 must be BIT-FOR-BIT identical!
        cutoff_ms = int(bars_orig.close_times[119])
        curve1 = [(ts, eq) for ts, eq in res1.equity_curve if ts <= cutoff_ms]
        curve2 = [(ts, eq) for ts, eq in res2.equity_curve if ts <= cutoff_ms]
        self.assertGreater(len(curve1), 50)
        self.assertEqual(curve1, curve2)

    def test_missing_funding_on_perp_fails_closed_and_sensitivity_band_runs_explicitly(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", funding_interval_hours=8)
        bars = _make_series([(100.0, 100.5, 99.5, 100.0, 1000.0)] * 40)  # 40h -> 5 funding settlements
        sim = Simulator(inst, CostModel.okx_default(), SimulatorConfig(starting_equity=10_000.0))
        long_strat = _ScriptedTargetStrategy({0: (2.0, None, None)})

        # Default run() MUST reject missing funding on perps (never silently assume 0!).
        with self.assertRaises(MissingFundingHistoryError):
            sim.run(long_strat, bars)

        # run_with_funding_sensitivity runs explicit 0% / +15% / +30% APR band!
        sens = sim.run_with_funding_sensitivity(long_strat, bars, funding=None)
        self.assertEqual("sensitivity_band", sens.mode)
        self.assertEqual((0.0, 0.15, 0.30), sens.apr_band)
        self.assertEqual({"apr_0pct", "apr_15pct", "apr_30pct"}, set(sens.results.keys()))
        r0 = sens.results["apr_0pct"]
        r15 = sens.results["apr_15pct"]
        r30 = sens.results["apr_30pct"]
        self.assertAlmostEqual(0.0, r0.attribution.funding_pnl, places=6)
        self.assertLess(r15.attribution.funding_pnl, 0.0)
        self.assertLess(r30.attribution.funding_pnl, r15.attribution.funding_pnl)
        self.assertEqual(r30, sens.worst_net_pnl_result)
        for r in sens.results.values():
            _assert_accounting_invariants(self, r)

    def test_bankrupt_account_flattens_halts_cleanly_and_never_goes_negative_units(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", max_leverage=5.0)
        cost = CostModel(
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            half_spread_bps=0.0,
            slippage_model=FixedTicks(0.0),
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
        )
        # Enter 5x long at 100 on bar 1; bar 2 gaps straight to 65 (below liq ~80.85 -> equity < 0!);
        # Strategy tries to re-enter on bars 3..4 -> MUST stay halted!
        bars = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (65.0, 66.0, 64.0, 65.0, 1000.0),
                (65.0, 68.0, 64.0, 67.0, 1000.0),
                (67.0, 70.0, 66.0, 69.0, 1000.0),
            ]
        )
        strat = _ScriptedTargetStrategy({0: (5.0, None, None), 2: (1.0, None, None), 3: (1.0, None, None)})
        sim = Simulator(
            inst,
            cost,
            SimulatorConfig(starting_equity=10_000.0, allow_missing_funding_for_perp=True),
        )
        res = sim.run(strat, bars)

        self.assertEqual(1, res.trade_count)
        self.assertEqual(1, res.liquidations)
        self.assertEqual("liquidation", res.trades[0].reason)
        self.assertLessEqual(res.ending_equity, 0.0)
        _assert_accounting_invariants(self, res)

    def test_empty_and_single_bar_edge_cases_and_buy_and_hold_benchmark(self) -> None:
        inst = spot_spec("MUUSDT", venue="binance")
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        strat = _ScriptedTargetStrategy({0: (1.0, None, None)})

        empty = _make_series([], symbol="MUUSDT")
        res_empty = sim.run(strat, empty)
        self.assertEqual(0, res_empty.trade_count)
        self.assertEqual(10_000.0, res_empty.ending_equity)
        self.assertEqual(0.0, res_empty.max_drawdown_pct)
        _assert_accounting_invariants(self, res_empty)

        single = _make_series([(100.0, 102.0, 99.0, 101.0, 500.0)], symbol="MUUSDT")
        res_single = sim.run(strat, single)
        self.assertEqual(0, res_single.trade_count)
        self.assertEqual(1, len(res_single.equity_curve))
        self.assertEqual(10_000.0, res_single.ending_equity)
        _assert_accounting_invariants(self, res_single)

        # Buy-and-hold benchmark with funding
        trend = _make_series([(100.0 + i * 2.0, 102.0 + i * 2.0, 99.0 + i * 2.0, 101.0 + i * 2.0, 1000.0) for i in range(10)], symbol="MUUSDT")
        f = FundingSeries.from_events("binance:MUUSDT", [FundingRateEvent(int(trend.close_times[4]), 0.001)])
        bh = compute_buy_and_hold_benchmark(trend, starting_equity=10_000.0, leverage=1.0, funding=f, taker_fee_bps=5.0)
        self.assertEqual(1, bh.trade_count)
        self.assertGreater(bh.total_return_pct, 0.15)
        self.assertLess(bh.attribution.funding_pnl, 0.0)
        _assert_accounting_invariants(self, bh)

    def test_e2e_all_three_ls_families_on_binance_fixture_and_overlays(self) -> None:
        # Synthetic trending + mean-reverting Binance BTCUSDT perp fixture (320 hourly bars)
        # with realistic bi-directional (positive AND negative) 8h funding rates.
        ohlcv: list[tuple[float, float, float, float, float]] = []
        px = 50_000.0
        for i in range(320):
            # Regime 1 (0..110): strong uptrend; Regime 2 (110..210): sharp downtrend; Regime 3 (210..320): chop
            if i < 110:
                ret = 0.004 + 0.003 * math.sin(i * 0.7)
            elif i < 210:
                ret = -0.005 + 0.003 * math.cos(i * 0.6)
            else:
                ret = 0.015 * math.sin(i * 0.9)
            o = px
            c = max(1000.0, o * (1.0 + ret))
            h = max(o, c) * 1.006
            l = min(o, c) * 0.994
            ohlcv.append((o, h, l, c, 200.0))
            px = c

        bars = _make_series(ohlcv, symbol="BTCUSDT", interval="1h")
        inst = binance_perp_spec("BTCUSDT", tick_size=Decimal("0.1"), max_leverage=10.0)
        cost = CostModel(
            maker_fee_bps=2.0,
            taker_fee_bps=4.0,
            half_spread_bps=0.5,
            slippage_model=SqrtVolumeImpact(impact_coeff=10.0, max_bps=50.0),
            maintenance_margin_rate=0.005,
            liquidation_fee_bps=5.0,
        )
        # Bi-directional funding every 8h (positive in bull regime, negative in bear regime!)
        events = [
            FundingRateEvent(
                ts_ms=int(bars.close_times[i]),
                rate=(0.00015 if i < 120 else -0.00012 if i < 220 else 0.00005),
            )
            for i in range(7, len(bars), 8)
        ]
        funding = FundingSeries.from_events(inst.instrument_id, events, settlement_interval_hours=8)
        cache = FeatureCache()
        sim = Simulator(inst, cost, SimulatorConfig(starting_equity=10_000.0))

        strategies = (
            TSMomentumStrategy(interval="1h", lookback_bars=48, side_mode="long_short"),
            DonchianATRStrategy(interval="1h", entry_period=20, exit_period=10, side_mode="long_short"),
            MeanReversionStrategy(
                interval="1h",
                rsi_period=14,
                zscore_period=20,
                entry_z=1.5,
                exit_z=0.25,
                rsi_oversold=40.0,
                rsi_overbought=60.0,
                max_ann_vol=3.0,
                side_mode="long_short",
            ),
        )
        overlays = (
            VolTargetOverlay(target_ann_vol=0.40, vol_lookback=20, vol_floor=0.05, max_leverage=2.0, interval="1h"),
            ExposureCapOverlay(max_gross_leverage=2.0, max_single_weight=2.0),
            DrawdownCircuitBreaker(max_drawdown_pct=0.30, action="halve_size"),
            DailyLossLimitOverlay(max_daily_loss_pct=0.10, reset_tz="UTC"),
        )
        for strat in strategies:
            with self.subTest(strategy=strat.strategy_id):
                res = sim.run(strat, bars, funding=funding, overlays=overlays, feature_cache=cache)
                _assert_accounting_invariants(self, res)
                self.assertGreater(res.trade_count, 0)
                self.assertNotEqual(0.0, res.attribution.funding_pnl)
                sides = {t.side for t in res.trades}
                self.assertEqual({"long", "short"}, sides, f"{strat.strategy_id} must trade both long and short")

    def test_funding_charged_on_position_held_after_bar_fills_no_one_bar_lag(self) -> None:
        """F1 regression: funding in (open_t, close_t] applies to the position held AFTER Bar t's fills."""
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        bars = _make_series([(100.0, 100.0, 100.0, 100.0, 1000.0)] * 6)
        for reuse in (True, False):
            sim = Simulator(
                inst,
                CostModel.zero_cost(),
                SimulatorConfig(starting_equity=10_000.0, reuse_bar_context=reuse, funding_gap_policy="allow_partial"),
            )
            with self.subTest(case="enter_bar1_event_close1", reuse=reuse):
                # Signal at bar0 close -> enter 1x long at bar1 open; event at close_times[1] MUST charge -$10.
                f = FundingSeries.from_events(inst.instrument_id, [FundingRateEvent(int(bars.close_times[1]), 0.001)])
                res = sim.run(
                    _ScriptedTargetStrategy({0: (1.0, None, None)}, supports_ephemeral_context=reuse),
                    bars,
                    funding=f,
                )
                self.assertAlmostEqual(-10.0, res.attribution.funding_pnl, places=9)
                _assert_accounting_invariants(self, res)
            with self.subTest(case="exit_bar4_event_close4", reuse=reuse):
                # Signal flat at bar3 close -> exit at bar4 open; event at close_times[4] MUST charge 0.
                f = FundingSeries.from_events(inst.instrument_id, [FundingRateEvent(int(bars.close_times[4]), 0.001)])
                res = sim.run(
                    _ScriptedTargetStrategy(
                        {0: (1.0, None, None), 3: (0.0, None, None)},
                        supports_ephemeral_context=reuse,
                    ),
                    bars,
                    funding=f,
                )
                self.assertEqual(0.0, res.attribution.funding_pnl)
                self.assertEqual(1, res.trade_count)
                _assert_accounting_invariants(self, res)

    def test_partial_funding_history_fails_closed_allow_partial_labels_and_sensitivity_fills_gaps(self) -> None:
        """F2 regression: gaps in historical funding over the scored span are never silently counted as 0."""
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0, funding_interval_hours=8)
        bars = _make_series([(100.0, 100.5, 99.5, 100.0, 1000.0)] * 96)  # 96h -> 12 settlements
        # Only the LAST 4 settlements exist (leading ~64h gap), like OKX's ~3-month archive limit.
        events = [FundingRateEvent(int(bars.close_times[i]), 0.0002) for i in range(7, 96, 8)][-4:]
        partial = FundingSeries.from_events(inst.instrument_id, events, settlement_interval_hours=8)
        strat = _ScriptedTargetStrategy({0: (1.0, None, None)})

        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        with self.assertRaises(MissingFundingHistoryError):
            sim.run(strat, bars, funding=partial)

        sim_partial = Simulator(
            inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0, funding_gap_policy="allow_partial")
        )
        res = sim_partial.run(strat, bars, funding=partial)
        self.assertEqual("historical_partial", res.funding_mode)
        self.assertIn("funding_coverage_ratio", res.diagnostics)
        self.assertLess(res.diagnostics["funding_coverage_ratio"], 0.5)
        self.assertGreaterEqual(res.diagnostics["funding_gap_count"], 1)
        _assert_accounting_invariants(self, res)

        sens = sim.run_with_funding_sensitivity(strat, bars, funding=partial)
        self.assertEqual("historical_gap_filled", sens.mode)
        self.assertEqual(3, len(sens.results))
        funding_by_label = {k: v.attribution.funding_pnl for k, v in sens.results.items()}
        r0 = funding_by_label["historical+apr_0pct"]
        r15 = funding_by_label["historical+apr_15pct"]
        r30 = funding_by_label["historical+apr_30pct"]
        # 0% fill keeps exactly the real events; higher APR fills strictly worsen long funding.
        self.assertLess(r0, 0.0)
        self.assertLess(r15, r0)
        self.assertLess(r30, r15)
        for r in sens.results.values():
            _assert_accounting_invariants(self, r)

        # Full-coverage history stays labelled "historical" with no diagnostics.
        full = FundingSeries.from_events(
            inst.instrument_id,
            [FundingRateEvent(int(bars.close_times[i]), 0.0002) for i in range(7, 96, 8)],
            settlement_interval_hours=8,
        )
        res_full = sim.run(strat, bars, funding=full)
        self.assertEqual("historical", res_full.funding_mode)
        self.assertNotIn("funding_coverage_ratio", res_full.diagnostics)

    def test_vol_target_rebalance_tolerance_suppresses_churn(self) -> None:
        """F4 regression: VolTargetOverlay hysteresis prevents per-bar micro-rebalances."""
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        ohlcv = []
        px = 100.0
        for i in range(200):
            ret = 0.004 * math.sin(i * 1.3) + 0.0015
            o = px
            c = o * (1.0 + ret)
            ohlcv.append((o, max(o, c) * 1.002, min(o, c) * 0.998, c, 5_000.0))
            px = c
        bars = _make_series(ohlcv)
        f = FundingSeries.synthetic_constant_apr(
            inst.instrument_id, start_ms=int(bars.open_times[0]), end_ms=int(bars.close_times[-1]), interval_hours=8, apr=0.1
        )
        sim = Simulator(inst, CostModel.okx_default(), SimulatorConfig(starting_equity=10_000.0))
        strat = TSMomentumStrategy(interval="1h", lookback_bars=24, side_mode="long_short")
        churny = sim.run(
            strat, bars, funding=f, feature_cache=FeatureCache(),
            overlays=(VolTargetOverlay(target_ann_vol=0.3, vol_lookback=20, interval="1h", rebalance_tolerance=0.0),),
        )
        calm = sim.run(
            strat, bars, funding=f, feature_cache=FeatureCache(),
            overlays=(VolTargetOverlay(target_ann_vol=0.3, vol_lookback=20, interval="1h", rebalance_tolerance=0.10),),
        )
        _assert_accounting_invariants(self, churny)
        _assert_accounting_invariants(self, calm)
        self.assertLess(calm.trade_count, churny.trade_count)
        self.assertLess(calm.attribution.fees, churny.attribution.fees)

    def test_reuse_bar_context_matches_immutable_bit_for_bit_including_lazy_fields(self) -> None:
        """Verify fast reuse_bar_context=True matches reuse_bar_context=False bit-for-bit."""
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        cost = CostModel.okx_default()
        ohlcv = []
        px = 100.0
        for i in range(120):
            ret = 0.012 * math.sin(i * 0.23) + (0.004 if i < 60 else -0.005)
            o = px
            c = max(10.0, o * (1.0 + ret))
            ohlcv.append((o, max(o, c) * 1.008, min(o, c) * 0.992, c, 3_000.0))
            px = c
        bars = _make_series(ohlcv, symbol="MU-USDT-SWAP", interval="1h")
        funding = FundingSeries.from_events(
            inst.instrument_id,
            [FundingRateEvent(int(bars.close_times[i]), 0.0002 if i % 16 == 7 else -0.0001) for i in range(7, 120, 8)],
            settlement_interval_hours=8,
        )
        sim_fast = Simulator(inst, cost, SimulatorConfig(starting_equity=10_000.0, reuse_bar_context=True))
        sim_immut = Simulator(inst, cost, SimulatorConfig(starting_equity=10_000.0, reuse_bar_context=False))
        cache = FeatureCache()

        # 1. Inspect every lazy property on BarContext / PortfolioView / PositionView / MarketView
        class _InspectorStrategy:
            strategy_id = "inspector"
            version = "1.0.0"
            required_intervals = ("1h",)
            warmup_bars = {"1h": 2}
            supports_ephemeral_context = True
            params = None

            def __init__(self) -> None:
                self.trace: list[tuple[Any, ...]] = []
                self._step = 0

            def reset(self) -> None:
                self.trace = []
                self._step = 0

            def on_bar(self, ctx: BarContext) -> StrategyOutput:
                pf = ctx.portfolio
                pos = pf.position(ctx.symbol)
                latest = ctx.market.latest_closed_bar(ctx.symbol, "1h")
                fseries = ctx.market.funding_series(ctx.symbol)
                fbetween = ctx.market.funding_between(ctx.symbol, 0, ctx.now_ms + 86_400_000)
                self.trace.append(
                    (
                        ctx.now_ms,
                        ctx.market.now_ms,
                        repr(ctx.market),
                        len(fseries) if fseries is not None else -1,
                        len(fbetween),
                        latest.close if latest is not None else None,
                        round(pf.equity, 9),
                        round(pf.cash, 9),
                        round(pf.peak_equity, 9),
                        round(pf.daily_start_equity, 9),
                        len(pf.positions),
                        len(pf.open_orders),
                        round(pos.qty, 9) if pos is not None else 0.0,
                        round(pos.unrealized_pnl, 9) if pos is not None else 0.0,
                        round(pos.notional, 9) if pos is not None else 0.0,
                        round(pos.leverage, 9) if pos is not None else 0.0,
                        round(pos.liquidation_price, 9) if (pos is not None and pos.liquidation_price is not None) else None,
                    )
                )
                step = self._step
                self._step += 1
                if step == 5:
                    return StrategyOutput(targets=(TargetPosition(ctx.symbol, 2.0, reason="enter_long"),))
                if step == 35:
                    return StrategyOutput(targets=(TargetPosition(ctx.symbol, -2.0, reason="flip_short"),))
                if step == 80:
                    return StrategyOutput(targets=(TargetPosition(ctx.symbol, 0.0, reason="flatten"),))
                return EMPTY_OUTPUT

            def snapshot_state(self) -> dict[str, Any]:
                return {"step": self._step}

            def restore_state(self, state: Mapping[str, Any]) -> None:
                self._step = int(state["step"])

        s_fast = _InspectorStrategy()
        s_immut = _InspectorStrategy()
        r_fast = sim_fast.run(s_fast, bars, funding=funding, feature_cache=cache)
        r_immut = sim_immut.run(s_immut, bars, funding=funding, feature_cache=cache)
        self.assertEqual(r_immut, r_fast)
        self.assertEqual(s_immut.trace, s_fast.trace)
        self.assertEqual(0, s_fast.trace[0][3])
        self.assertEqual(len(funding), s_fast.trace[-1][3])
        self.assertEqual(len(funding), s_fast.trace[-1][4])

        # 2. All three built-in families must match bit-for-bit between reuse=True and reuse=False.
        for strat in (
            TSMomentumStrategy(interval="1h", lookback_bars=12, side_mode="long_short"),
            DonchianATRStrategy(interval="1h", entry_period=10, exit_period=5, side_mode="long_short"),
            MeanReversionStrategy(interval="1h", entry_z=1.2, rsi_oversold=42.0, rsi_overbought=58.0, max_ann_vol=3.0),
        ):
            with self.subTest(strategy=strat.strategy_id):
                res_a = sim_fast.run(strat, bars, funding=funding, feature_cache=cache)
                res_b = sim_immut.run(strat, bars, funding=funding, feature_cache=cache)
                self.assertEqual(res_b, res_a)

    def test_risk_overlay_snapshot_and_restore_state_mid_run_equivalence(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        bars = _make_series(
            [
                (100.0 - i * 0.4, 100.5 - i * 0.4, 99.0 - i * 0.4, 99.8 - i * 0.4, 2_000.0)
                for i in range(60)
            ],
            symbol="MU-USDT-SWAP",
            interval="1h",
        )
        cache = FeatureCache()

        def _make_overlays() -> tuple[Any, ...]:
            return (
                VolTargetOverlay(target_ann_vol=0.35, vol_lookback=10, vol_floor=0.05, max_leverage=2.0, interval="1h"),
                DrawdownCircuitBreaker(max_drawdown_pct=0.08, action="halve_size", cooldown_bars=5),
                DailyLossLimitOverlay(max_daily_loss_pct=0.04, reset_tz="UTC"),
                ExposureCapOverlay(max_gross_leverage=1.5, max_single_weight=1.5),
                SessionWindowOverlay(trading_windows_et=(("09:30", "16:00"),)),
            )

        ov_continuous = _make_overlays()
        ov_resumed = _make_overlays()
        raw_out = StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 2.0, reason="signal"),))

        from mu_strategy.market_data.bar_series import MarketView
        from mu_strategy.strategies.protocol import PortfolioView

        outputs_cont: list[StrategyOutput] = []
        outputs_resumed: list[StrategyOutput] = []
        snaps: list[dict[str, Any]] = []

        for i in range(15, 60):
            now_ms = int(bars.close_times[i])
            mv = MarketView(bars={("MU-USDT-SWAP", "1h"): bars}, now_ms=now_ms, features=cache)
            eq = 10_000.0 - (i - 15) * 35.0
            pf = PortfolioView(
                equity=eq,
                cash=eq,
                positions={},
                open_orders=(),
                peak_equity=10_000.0,
                daily_start_equity=10_000.0,
            )
            ctx = BarContext(now_ms=now_ms, symbol="MU-USDT-SWAP", market=mv, portfolio=pf, instrument=inst)

            out_c = apply_risk_overlays(ov_continuous, ctx, raw_out)
            if i == 34:
                snaps = [ov.snapshot_state() for ov in ov_continuous]
                for ov_r, snap in zip(ov_resumed, snaps):
                    ov_r.restore_state(snap)
            elif i >= 35:
                outputs_cont.append(out_c)
                outputs_resumed.append(apply_risk_overlays(ov_resumed, ctx, raw_out))

        self.assertGreater(len(outputs_cont), 10)
        self.assertEqual(outputs_cont, outputs_resumed)

    @unittest.skipUnless(LIVE_DATA_DIR.exists(), "data/live fixture not available")
    def test_e2e_okx_live_data_and_warm_cache_speed_under_5ms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s1h = load_or_build_bar_series(LIVE_DATA_DIR, symbol="MU-USDT-SWAP", interval="1h", cache_dir=Path(tmp))
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        cost = CostModel.okx_default()
        funding_1h = FundingSeries.synthetic_constant_apr(
            inst.instrument_id,
            start_ms=int(s1h.open_times[0]),
            end_ms=int(s1h.close_times[-1]),
            interval_hours=8,
            apr=0.10,
        )
        sim = Simulator(inst, cost, SimulatorConfig(starting_equity=10_000.0, primary_interval="1h"))
        cache = FeatureCache()

        # Verify E2E L/S execution + exact funding attribution across all 3 baseline families on data/live.
        tsm = TSMomentumStrategy(interval="1h", lookback_bars=96, side_mode="long_short")
        for strat in (
            tsm,
            DonchianATRStrategy(interval="1h", entry_period=20, exit_period=10, side_mode="long_short"),
            MeanReversionStrategy(
                interval="1h",
                entry_z=1.5,
                rsi_oversold=40.0,
                rsi_overbought=60.0,
                max_ann_vol=3.0,
                side_mode="long_short",
            ),
        ):
            with self.subTest(strategy=strat.strategy_id):
                warm_res = sim.run(strat, s1h, funding=funding_1h, feature_cache=cache)
                _assert_accounting_invariants(self, warm_res)
                self.assertGreater(warm_res.trade_count, 0)
                sides = {t.side for t in warm_res.trades}
                self.assertEqual({"long", "short"}, sides)

        # AC4 benchmark: warm-cache single-strategy backtest < 5 ms/run on data/live.
        # Assert on min() (robust to scheduler noise); strict 5 ms budget when MU_STRICT_PERF=1, else 3x for shared CI.
        runs = 15
        timings_ms: list[float] = []
        for _ in range(runs):
            t0 = time.perf_counter()
            sim.run(tsm, s1h, funding=funding_1h, feature_cache=cache)
            timings_ms.append((time.perf_counter() - t0) * 1000.0)
        best_ms = min(timings_ms)
        budget_ms = 5.0 if os.environ.get("MU_STRICT_PERF") == "1" else 15.0
        self.assertLess(
            best_ms,
            budget_ms,
            f"warm-cache ts_momentum on {len(s1h)} 1h bars best run took {best_ms:.2f} ms (budget {budget_ms} ms)",
        )

    def test_rejects_mismatched_funding_instrument_and_contradicting_bar_keys_cr1_36_cr1_48(self) -> None:
        okx_inst = okx_swap_spec("BTCUSDT", tick_size=Decimal("0.1"), max_leverage=5.0)
        # CR2-46: Complete 24h bar & 8h funding history (zero coverage gaps) so only the identity guard can fire.
        btc_bars = _make_series([(100.0, 101.0, 99.0, 100.0, 500.0)] * 24, symbol="BTCUSDT", interval="1h")
        eth_bars = _make_series([(10.0, 11.0, 9.0, 10.0, 500.0)] * 24, symbol="ETHUSDT", interval="1h")
        binance_funding = FundingSeries.from_events(
            "binance:BTCUSDT",
            [FundingRateEvent(int(btc_bars.close_times[i]), 0.0001) for i in (7, 15, 23)],
            settlement_interval_hours=8,
        )
        self.assertEqual(
            (),
            binance_funding.coverage_gaps(int(btc_bars.open_times[0]), int(btc_bars.close_times[-1]), 8),
        )
        sim = Simulator(okx_inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        strat = _ScriptedStrategy({})

        # CR1-36 + CR2-46: Assert the specific instrument-mismatch message in both run() and sensitivity().
        with self.assertRaisesRegex(ValueError, "does not match simulator instrument"):
            sim.run(strat, btc_bars, funding=binance_funding)
        with self.assertRaisesRegex(ValueError, "does not match simulator instrument"):
            sim.run_with_funding_sensitivity(strat, btc_bars, funding=binance_funding)

        # CR1-48: Bar map keys contradicting BarSeries identity rejected for both string and tuple keys.
        with self.assertRaises(ValueError):
            sim.run(strat, {"1h": eth_bars}, funding=None)
        with self.assertRaises(ValueError):
            sim.run(strat, {("BTCUSDT", "1h"): eth_bars}, funding=None)
        with self.assertRaises(ValueError):
            sim.run(strat, {("BTCUSDT", "15m"): btc_bars}, funding=None)

    def test_rejects_missing_interval_with_positive_warmup_cr1_37(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        s15 = _make_series([(100.0, 101.0, 99.0, 100.0, 500.0)] * 20, symbol="MUUSDT", interval="15m")

        class _NeedsHourly(_ScriptedStrategy):
            def __init__(self, req_1h: int) -> None:
                super().__init__({}, interval="15m", warmup=2)
                self.required_intervals = ("15m", "1h")
                self.warmup_bars = {"15m": 2, "1h": req_1h}

        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        with self.assertRaises(KeyError):
            sim.run(_NeedsHourly(26), s15)

        # Explicit warmup override still rejects missing positive-warmup secondary interval.
        sim_ov = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0, warmup_bars=5))
        with self.assertRaises(KeyError):
            sim_ov.run(_NeedsHourly(26), s15)

        # Zero-bar requirement preserved without failing.
        res_zero = sim.run(_NeedsHourly(0), s15)
        self.assertEqual(0, res_zero.trade_count)

    def test_loss_overlay_halt_cancels_flat_queued_entries_and_preserves_exits_cr1_38(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        # Bar 0: flat, queue resting buy limit at 80 (created_while_open=False!) AND buy market 1.0x
        # Bar 1: market buys at 100, price closes at 90 (-10% > 5% DD limit -> DrawdownCircuitBreaker halts & flattens!)
        # Bar 2: opens at 90 (flatten executes!), then dips to 75 -> surviving buy limit at 80 MUST NOT FILL!
        bars = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 100.0, 89.0, 90.0, 1000.0),
                (90.0, 91.0, 75.0, 85.0, 1000.0),
                (85.0, 86.0, 84.0, 85.0, 1000.0),
            ],
            symbol="MUUSDT",
            interval="1h",
        )
        strat = _ScriptedStrategy(
            {
                0: StrategyOutput(
                    targets=(TargetPosition("MUUSDT", 1.0, reason="enter_mkt"),),
                    intents=(
                        OrderIntent(
                            "resting_dip",
                            "MUUSDT",
                            "buy",
                            "limit",
                            "equity_fraction",
                            1.0,
                            limit_price=80.0,
                            expires_after_bars=5,
                        ),
                    ),
                ),
            },
            warmup=0,
        )
        cb = DrawdownCircuitBreaker(max_drawdown_pct=0.05, action="flatten_and_halt")
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        res = sim.run(strat, bars, overlays=(cb,))
        self.assertTrue(cb.is_triggered)
        # Only the initial trade (entered bar 1, flattened bar 2 by circuit breaker) — resting_dip NEVER filled!
        self.assertEqual(1, res.trade_count)
        self.assertEqual("drawdown_circuit_breaker", res.trades[0].reason)
        _assert_accounting_invariants(self, res)

    def test_session_window_and_session_gate_block_resting_entry_outside_session_cr1_39(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        # Bar 0: 14:30 UTC (10:30 ET - IN SESSION), bar 1: 15:30 UTC (11:30 ET - OUT), bar 2: 16:30 UTC (12:30 ET - OUT)
        t0 = int(datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        bars = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),  # opens 10:00 ET (in window)
                (100.0, 101.0, 99.0, 100.0, 1000.0),  # opens 11:00 ET (in window, doesn't hit limit 95)
                (100.0, 101.0, 90.0, 95.0, 1000.0),   # opens 12:00 ET (OUT of 09:45-11:30 window, dips to 90!)
                (95.0, 96.0, 94.0, 95.0, 1000.0),     # opens 13:00 ET (OUT)
            ],
            symbol="MUUSDT",
            interval="1h",
            start_ms=t0,
        )
        ov = SessionWindowOverlay(
            trading_windows_et=(("09:45", "11:30"),),
            calendar_id=US_EQUITIES_ALGORITHMIC_CALENDAR,
        )
        strat_entry = _ScriptedStrategy(
            {
                0: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "lim_in",
                            "MUUSDT",
                            "buy",
                            "limit",
                            "equity_fraction",
                            1.0,
                            limit_price=95.0,
                            expires_after_bars=5,
                        ),
                    )
                )
            },
            warmup=0,
        )
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        res_blocked = sim.run(strat_entry, bars, overlays=(ov,))
        # Resting limit MUST NOT fill on out-of-session bar 2!
        self.assertEqual(0, res_blocked.trade_count)

        # Also verify OrderIntent.session_gate enforces fill-bar open even without SessionWindowOverlay!
        gate = SessionGate(
            trading_calendar_id=US_EQUITIES_ALGORITHMIC_CALENDAR,
            trading_windows_et=(("09:45", "11:30"),),
        )
        strat_gated = _ScriptedStrategy(
            {
                0: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "lim_gate",
                            "MUUSDT",
                            "buy",
                            "limit",
                            "equity_fraction",
                            1.0,
                            limit_price=95.0,
                            expires_after_bars=5,
                            session_gate=gate,
                        ),
                    )
                )
            },
            warmup=0,
        )
        self.assertEqual(0, sim.run(strat_gated, bars).trade_count)

        # Risk-reducing protective stop exit STILL fills outside session!
        strat_exit = _ScriptedStrategy(
            {
                0: StrategyOutput(
                    intents=(
                        OrderIntent("e1", "MUUSDT", "buy", "market", "equity_fraction", 1.0),
                        OrderIntent("sl", "MUUSDT", "sell", "stop_market", "close_all", 0.0, stop_price=95.0, reduce_only=True),
                    )
                )
            },
            warmup=0,
        )
        res_exit = sim.run(strat_exit, bars, overlays=(ov,))
        self.assertEqual(1, res_exit.trade_count)
        self.assertAlmostEqual(95.0, res_exit.trades[0].exit_price, places=6)

    def test_open_fill_uses_only_pre_open_data_for_volume_and_atr_slippage_cr1_40(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        prefix = [
            (100.0, 102.0, 98.0, 100.0, 1000.0),
            (100.0, 103.0, 97.0, 101.0, 1200.0),
            (101.0, 104.0, 99.0, 102.0, 1100.0),
        ]
        # Bar 3 has IDENTICAL open=102.0, but completely different final high/low/close/volume!
        bars_a = _make_series(prefix + [(102.0, 103.0, 101.0, 102.5, 50.0), (102.5, 103.0, 102.0, 102.5, 1000.0)], symbol="MUUSDT")
        bars_b = _make_series(prefix + [(102.0, 150.0, 60.0, 140.0, 500_000.0), (140.0, 141.0, 139.0, 140.0, 1000.0)], symbol="MUUSDT")

        for slip_model in (SqrtVolumeImpact(impact_coeff=0.25), AtrBps(bps_per_atr=25.0)):
            cost = CostModel(maker_fee_bps=0.0, taker_fee_bps=0.0, half_spread_bps=0.0, slippage_model=slip_model)
            sim = Simulator(inst, cost, SimulatorConfig(starting_equity=10_000.0, atr_period=2, close_on_end=False))
            for mode_name, strat in (
                ("target", _ScriptedStrategy({2: StrategyOutput(targets=(TargetPosition("MUUSDT", 1.0),))}, warmup=0)),
                ("market_intent", _ScriptedStrategy({2: StrategyOutput(intents=(OrderIntent("m1", "MUUSDT", "buy", "market", "equity_fraction", 1.0),))}, warmup=0)),
            ):
                with self.subTest(slip=type(slip_model).__name__, mode=mode_name):
                    # Use a tiny exit at bar 4 so TradeRecord exposes exact entry_price.
                    ra = sim.run(
                        _ScriptedStrategy(
                            {2: strat.script[2], 3: StrategyOutput(targets=(TargetPosition("MUUSDT", 0.0),))},
                            warmup=0,
                        ),
                        bars_a,
                    )
                    rb = sim.run(
                        _ScriptedStrategy(
                            {2: strat.script[2], 3: StrategyOutput(targets=(TargetPosition("MUUSDT", 0.0),))},
                            warmup=0,
                        ),
                        bars_b,
                    )
                    self.assertEqual(1, ra.trade_count)
                    self.assertEqual(1, rb.trade_count)
                    self.assertEqual(ra.trades[0].entry_price, rb.trades[0].entry_price)

    def test_gap_funding_settles_before_next_open_fill_cr1_41(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        t0 = 1_735_689_600_000  # 00:00
        h = 3_600_000
        # Bar 0: 06:00 -> 07:00 (close=100); GAP 07:00..09:00 with funding at 08:00; Bar 1: 09:00 -> 10:00 (open=105)
        candles = [
            Candle(open_time_ms=t0 + 5 * h, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
            Candle(open_time_ms=t0 + 6 * h, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
            Candle(open_time_ms=t0 + 9 * h, open=105.0, high=106.0, low=104.0, close=105.0, volume=1000.0),
            Candle(open_time_ms=t0 + 10 * h, open=105.0, high=106.0, low=104.0, close=105.0, volume=1000.0),
        ]
        bars = BarSeries.from_candles("MU-USDT-SWAP", "1h", candles)
        funding = FundingSeries.from_events(
            inst.instrument_id,
            [FundingRateEvent(t0 + 8 * h, 0.001)],
            settlement_interval_hours=8,
        )

        for reuse in (True, False):
            sim = Simulator(
                inst,
                CostModel.zero_cost(),
                SimulatorConfig(starting_equity=10_000.0, reuse_bar_context=reuse, funding_gap_policy="allow_partial"),
            )
            # Case A: flat at 07:00, open long at 09:00 -> MUST NOT pay 08:00 gap funding!
            strat_open_after = _ScriptedStrategy(
                {1: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 1.0),))},
                warmup=0,
                supports_ephemeral_context=reuse,
            )
            res_a = sim.run(strat_open_after, bars, funding=funding)
            self.assertAlmostEqual(0.0, res_a.attribution.funding_pnl, places=9)
            _assert_accounting_invariants(self, res_a)

            # Case B: enter long at 06:00 (100 units at 100.0), close at 09:00 -> MUST pay 08:00 gap funding (-10.0)!
            strat_close_after = _ScriptedStrategy(
                {
                    0: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 1.0),)),
                    1: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 0.0),)),
                },
                warmup=0,
                supports_ephemeral_context=reuse,
            )
            res_b = sim.run(strat_close_after, bars, funding=funding)
            self.assertAlmostEqual(-10.0, res_b.attribution.funding_pnl, places=6)
            _assert_accounting_invariants(self, res_b)

    def test_activated_entry_stop_limit_persists_without_resetting_expiry_cr1_42(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        # Buy stop_limit(stop=101, limit=102, expires_after_bars=3):
        # Bar 1: open=103, high=104, low=102.5 -> triggers stop, cannot fill (>102), activates (2 bars left).
        # Bar 2: open=100, high=100.5, low=99.5 (never hits 101!) -> fills activated limit at 100.0!
        bars_fill = _make_series(
            [
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (103.0, 104.0, 102.5, 103.0, 1000.0),
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (100.0, 100.5, 99.5, 100.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        strat = _ScriptedStrategy(
            {
                0: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "sl_ent",
                            "MUUSDT",
                            "buy",
                            "stop_limit",
                            "contracts",
                            10.0,
                            stop_price=101.0,
                            limit_price=102.0,
                            expires_after_bars=2,
                        ),
                    )
                )
            },
            warmup=0,
        )
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        res_fill = sim.run(strat, bars_fill)
        self.assertEqual(1, res_fill.trade_count)
        self.assertAlmostEqual(100.0, res_fill.trades[0].entry_price, places=6)

        # Expiry NOT reset on activation: expires_after_bars=2 -> bar 1 (miss, 1 left), bar 2 (miss, 0 left -> expired), bar 3 dip MUST NOT fill!
        bars_exp = _make_series(
            [
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (103.0, 104.0, 102.5, 103.0, 1000.0),
                (103.0, 104.0, 102.5, 103.0, 1000.0),
                (100.0, 100.5, 99.5, 100.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        res_exp = sim.run(strat, bars_exp)
        self.assertEqual(0, res_exp.trade_count)

    def test_end_of_data_close_applies_modeled_spread_and_slippage_cr1_43(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        bars = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 101.0, 99.0, 100.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        strat = _ScriptedStrategy({0: StrategyOutput(targets=(TargetPosition("MUUSDT", 1.0),))}, warmup=0)

        # Non-zero spread & ticks -> EOD close pays spread + slippage!
        cost_real = CostModel(maker_fee_bps=0.0, taker_fee_bps=5.0, half_spread_bps=10.0, slippage_model=FixedTicks(2))
        sim_real = Simulator(inst, cost_real, SimulatorConfig(starting_equity=10_000.0, close_on_end=True))
        res_real = sim_real.run(strat, bars)
        self.assertEqual(1, res_real.trade_count)
        self.assertEqual("end_of_data", res_real.trades[0].reason)
        self.assertLess(res_real.trades[0].exit_price, 100.0)
        self.assertGreater(res_real.attribution.slippage, 0.0)
        _assert_accounting_invariants(self, res_real)

        # Zero spread & zero ticks -> parity preserved (exit_price == final_close, slippage == 0).
        cost_zero = CostModel(maker_fee_bps=0.0, taker_fee_bps=5.0, half_spread_bps=0.0, slippage_model=FixedTicks(0))
        sim_zero = Simulator(inst, cost_zero, SimulatorConfig(starting_equity=10_000.0, close_on_end=True))
        res_zero = sim_zero.run(strat, bars)
        self.assertAlmostEqual(100.0, res_zero.trades[0].exit_price, places=9)
        self.assertAlmostEqual(0.0, res_zero.attribution.slippage, places=9)
        _assert_accounting_invariants(self, res_zero)

    def test_resting_reduce_only_stop_limit_qty_and_stop_limit_preservation_cr1_44_cr1_45(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        # CR1-44: Long 10 contracts, reduce-only stop_market sell 2 contracts -> closes ONLY 2, leaving 8 until EOD!
        bars_part = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (99.0, 100.0, 94.0, 98.0, 1000.0),
                (98.0, 102.0, 97.0, 102.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        strat_part = _ScriptedStrategy(
            {
                0: StrategyOutput(intents=(OrderIntent("e1", "MUUSDT", "buy", "market", "contracts", 10.0),)),
                1: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "p_stop",
                            "MUUSDT",
                            "sell",
                            "stop_market",
                            "contracts",
                            2.0,
                            stop_price=95.0,
                            reduce_only=True,
                        ),
                    )
                ),
            },
            warmup=0,
        )
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        res_part = sim.run(strat_part, bars_part)
        self.assertEqual(2, res_part.trade_count)
        self.assertAlmostEqual(2.0, res_part.trades[0].qty, places=6)
        self.assertAlmostEqual(95.0, res_part.trades[0].exit_price, places=6)
        self.assertAlmostEqual(8.0, res_part.trades[1].qty, places=6)
        self.assertEqual("end_of_data", res_part.trades[1].reason)
        _assert_accounting_invariants(self, res_part)

        # CR1-45: Protective sell stop_limit(stop=95, limit=94) when bar 2 opens at 90 (high=93 < 94) ->
        # MUST NOT downgrade to stop_market at 90; activates and fills at 94 on bar 3 (high=94.5)!
        bars_sl = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (90.0, 93.0, 89.0, 91.0, 1000.0),
                (92.0, 94.5, 91.0, 93.0, 1000.0),
                (93.0, 93.5, 92.5, 93.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        strat_sl = _ScriptedStrategy(
            {
                0: StrategyOutput(intents=(OrderIntent("e1", "MUUSDT", "buy", "market", "contracts", 10.0),)),
                1: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "prot_sl",
                            "MUUSDT",
                            "sell",
                            "stop_limit",
                            "close_all",
                            0.0,
                            stop_price=95.0,
                            limit_price=94.0,
                            reduce_only=True,
                            expires_after_bars=3,
                        ),
                    )
                ),
            },
            warmup=0,
        )
        res_sl = sim.run(strat_sl, bars_sl)
        self.assertEqual(1, res_sl.trade_count)
        self.assertAlmostEqual(94.0, res_sl.trades[0].exit_price, places=6)
        self.assertEqual(int(bars_sl.open_times[3]), res_sl.trades[0].exit_time_ms)
        _assert_accounting_invariants(self, res_sl)

    def test_same_bar_scale_in_rechecks_liquidation_and_ignores_pre_fill_low_cr1_46(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=10.0)
        sim = Simulator(
            inst,
            CostModel.zero_cost(),
            SimulatorConfig(starting_equity=10_000.0, allow_missing_funding_for_perp=True),
        )

        # Case 1: 1x long at 100 (bar 1), adds 4x market buy at bar 2 open=100 -> liq=80; bar 2 low=75 -> LIQUIDATED!
        bars_open_add = _make_series(
            [
                (100.0, 100.0, 100.0, 100.0, 1000.0),
                (100.0, 100.0, 100.0, 100.0, 1000.0),
                (100.0, 101.0, 75.0, 100.0, 1000.0),
                (100.0, 100.0, 100.0, 100.0, 1000.0),
            ],
            symbol="MU-USDT-SWAP",
        )
        strat_open_add = _ScriptedStrategy(
            {
                0: StrategyOutput(intents=(OrderIntent("e1", "MU-USDT-SWAP", "buy", "market", "equity_fraction", 1.0),)),
                1: StrategyOutput(intents=(OrderIntent("add4", "MU-USDT-SWAP", "buy", "market", "equity_fraction", 4.0),)),
            },
            warmup=0,
        )
        res_liq = sim.run(strat_open_add, bars_open_add)
        self.assertEqual(1, res_liq.liquidations)
        self.assertEqual("liquidation", res_liq.trades[0].reason)
        _assert_accounting_invariants(self, res_liq)

        # Case 2: Intrabar buy stop add at 105 on bar (open=100, low=75, high=106, close=105):
        # low=75 could be PRE-FILL, so post-fill path [105..106] MUST NOT falsely liquidate!
        bars_prefill = _make_series(
            [
                (100.0, 100.0, 100.0, 100.0, 1000.0),
                (100.0, 100.0, 100.0, 100.0, 1000.0),
                (100.0, 106.0, 75.0, 105.0, 1000.0),
                (105.0, 105.0, 105.0, 105.0, 1000.0),
            ],
            symbol="MU-USDT-SWAP",
        )
        strat_stop_add = _ScriptedStrategy(
            {
                0: StrategyOutput(intents=(OrderIntent("e1", "MU-USDT-SWAP", "buy", "market", "equity_fraction", 1.0),)),
                1: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "add_stp",
                            "MU-USDT-SWAP",
                            "buy",
                            "stop_market",
                            "equity_fraction",
                            4.0,
                            stop_price=105.0,
                        ),
                    )
                ),
            },
            warmup=0,
        )
        res_no_liq = sim.run(strat_stop_add, bars_prefill)
        self.assertEqual(0, res_no_liq.liquidations)
        _assert_accounting_invariants(self, res_no_liq)

    def test_reduce_only_market_order_caps_to_position_and_rejects_same_side_cr1_47(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        bars = _make_series([(100.0, 101.0, 99.0, 100.0, 1000.0)] * 6, symbol="MU-USDT-SWAP")
        sim = Simulator(
            inst,
            CostModel.zero_cost(),
            SimulatorConfig(starting_equity=10_000.0, allow_missing_funding_for_perp=True),
        )
        # Bar 0: buy 10 contracts
        # Bar 1: reduce_only market buy 5 (same side -> REJECTED) + reduce_only market sell 4 (partial -> long 6)
        # Bar 2: reduce_only market sell 20 (>6 -> capped to 6, closes to FLAT, NEVER flips short!)
        strat = _ScriptedStrategy(
            {
                0: StrategyOutput(intents=(OrderIntent("b10", "MU-USDT-SWAP", "buy", "market", "contracts", 10.0),)),
                1: StrategyOutput(
                    intents=(
                        OrderIntent("bad_b", "MU-USDT-SWAP", "buy", "market", "contracts", 5.0, reduce_only=True),
                        OrderIntent("part_s", "MU-USDT-SWAP", "sell", "market", "contracts", 4.0, reduce_only=True),
                    )
                ),
                2: StrategyOutput(
                    intents=(
                        OrderIntent("over_s", "MU-USDT-SWAP", "sell", "market", "contracts", 20.0, reduce_only=True),
                    )
                ),
            },
            warmup=0,
        )
        res = sim.run(strat, bars)
        self.assertEqual(2, res.trade_count)
        self.assertEqual("long", res.trades[0].side)
        self.assertAlmostEqual(4.0, res.trades[0].qty, places=6)
        self.assertEqual("long", res.trades[1].side)
        self.assertAlmostEqual(6.0, res.trades[1].qty, places=6)
        _assert_accounting_invariants(self, res)

    def test_closed_bar_slices_immutable_and_ephemeral_context_opt_in_enforced_cr1_49(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        bars = _make_series([(100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0) for i in range(6)], symbol="MUUSDT")

        class _RetainingStrategy:
            strategy_id = "retainer"
            version = "1.0.0"
            required_intervals = ("1h",)
            warmup_bars = {"1h": 0}
            params = None

            def __init__(self) -> None:
                self.saved_slices: list[Any] = []
                self.saved_ctxs: list[BarContext] = []
                self.saved_pfs: list[Any] = []

            def reset(self) -> None:
                self.saved_slices = []
                self.saved_ctxs = []
                self.saved_pfs = []

            def on_bar(self, ctx: BarContext) -> StrategyOutput:
                self.saved_slices.append(ctx.market.closed_bars(ctx.symbol, "1h"))
                self.saved_ctxs.append(ctx)
                self.saved_pfs.append(ctx.portfolio)
                if len(self.saved_slices) == 1:
                    return StrategyOutput(targets=(TargetPosition(ctx.symbol, 1.0),))
                return EMPTY_OUTPUT

            def snapshot_state(self) -> dict[str, Any]:
                return {}

            def restore_state(self, state: Mapping[str, Any]) -> None:
                return None

        # Default SimulatorConfig(): standard Strategy receives stable immutable snapshots!
        s_default = _RetainingStrategy()
        sim_default = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        sim_default.run(s_default, bars)
        self.assertEqual([1, 2, 3, 4, 5, 6], [len(sl) for sl in s_default.saved_slices])
        self.assertEqual(
            [int(bars.close_times[i]) for i in range(6)],
            [c.now_ms for c in s_default.saved_ctxs],
        )
        # Portfolio snapshot on bar 0 was flat (10_000), bar 5 had marked gains!
        self.assertAlmostEqual(10_000.0, s_default.saved_pfs[0].equity, places=6)
        self.assertGreater(s_default.saved_pfs[-1].equity, 10_000.0)

        # CR2-11: Explicit reuse_bar_context=True MUST reject strategies without supports_ephemeral_context=True.
        sim_ephem = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0, reuse_bar_context=True))
        with self.assertRaisesRegex(ValueError, "supports_ephemeral_context"):
            sim_ephem.run(_RetainingStrategy(), bars)

        # Opt-in strategy with supports_ephemeral_context=True: closed_bars() STILL returns immutable fixed slices,
        # while retained ephemeral BarContext/PortfolioView/MarketView raise RuntimeError outside on_bar!
        class _OptInRetainingStrategy(_RetainingStrategy):
            supports_ephemeral_context = True

        s_ephem = _OptInRetainingStrategy()
        sim_ephem.run(s_ephem, bars)
        self.assertEqual([1, 2, 3, 4, 5, 6], [len(sl) for sl in s_ephem.saved_slices])
        with self.assertRaises(RuntimeError):
            _ = s_ephem.saved_ctxs[0].now_ms
        with self.assertRaises(RuntimeError):
            _ = s_ephem.saved_pfs[0].equity

    def test_benchmark_funding_validation_ohlc_liquidation_and_calmar_losses_cr1_50_51_52(self) -> None:
        # CR1-51: Calmar preserves compounded annual losses (-10% in 1h -> ann_return ~ -1.0 -> calmar ~ -10.0, NOT -8760).
        t0 = 1_735_689_600_000
        curve = ((t0, 100.0), (t0 + 3_600_000, 90.0))
        res_c = compute_simulation_metrics(
            starting_equity=100.0,
            ending_equity=90.0,
            equity_curve=curve,
            trades=(),
            attribution=CostAttribution.from_components(gross_pnl=-10.0, fees=0.0, slippage=0.0, funding_pnl=0.0),
            interval_ms=3_600_000,
        )
        self.assertAlmostEqual(-10.0, res_c.calmar_ratio, places=6)
        _, _, _, _, calmar_fast = _finalize_curve_stats(
            starting_equity=100.0,
            ending_equity=90.0,
            first_ts=t0,
            last_ts=t0 + 3_600_000,
            n_points=2,
            max_dd=-0.10,
            avg_equity=95.0,
            sum_r=-0.10,
            sum_r2=0.01,
            downside_sq=0.01,
            interval_ms=3_600_000,
        )
        self.assertAlmostEqual(-10.0, calmar_fast, places=6)

        # CR1-50: 10-day perpetual benchmark rejects missing or 1-event incomplete funding; spot allows no funding.
        bars_10d = _make_series([(100.0, 101.0, 99.0, 100.0, 1000.0)] * 240, symbol="MU-USDT-SWAP", interval="1h")
        perp_inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        sparse_f = FundingSeries.from_events(
            perp_inst.instrument_id,
            [FundingRateEvent(int(bars_10d.close_times[7]), 0.0001)],
            settlement_interval_hours=8,
        )
        with self.assertRaises(MissingFundingHistoryError):
            compute_buy_and_hold_benchmark(bars_10d, instrument=perp_inst, funding=None)
        with self.assertRaises(MissingFundingHistoryError):
            compute_buy_and_hold_benchmark(bars_10d, instrument=perp_inst, funding=sparse_f)
        with self.assertRaises(MissingFundingHistoryError):
            compute_buy_and_hold_benchmark(bars_10d, funding=sparse_f)

        bh_partial = compute_buy_and_hold_benchmark(
            bars_10d, instrument=perp_inst, funding=sparse_f, funding_gap_policy="allow_partial"
        )
        self.assertEqual("historical_partial", bh_partial.funding_mode)
        self.assertIn("funding_gap_count", bh_partial.diagnostics)

        sp_inst = spot_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"))
        bh_spot = compute_buy_and_hold_benchmark(bars_10d, instrument=sp_inst, funding=None)
        self.assertEqual("none", bh_spot.funding_mode)

        # CR1-52: 5x leveraged long benchmark entered at 100 with 1% MMR liquidates on intrabar low=75 even if close=100!
        bars_wick = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 102.0, 75.0, 100.0, 1000.0),
                (100.0, 101.0, 99.0, 100.0, 1000.0),
            ],
            symbol="MU-USDT-SWAP",
            interval="1h",
        )
        cost_mmr = CostModel(
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            half_spread_bps=0.0,
            slippage_model=FixedTicks(0),
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=0.0,
        )
        bh_liq = compute_buy_and_hold_benchmark(
            bars_wick,
            leverage=5.0,
            side="long",
            instrument=perp_inst,
            cost_model=cost_mmr,
            allow_missing_funding_for_perp=True,
        )
        self.assertEqual(1, bh_liq.liquidations)
        self.assertEqual(1, bh_liq.trade_count)
        self.assertEqual("liquidation", bh_liq.trades[0].reason)
        _assert_accounting_invariants(self, bh_liq)

    def test_gap_filled_sensitivity_keeps_close_apr_keys_distinct_cr1_73(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        bars = _make_series([(100.0 + 0.1 * i, 101.0 + 0.1 * i, 99.0 + 0.1 * i, 100.5 + 0.1 * i, 1000.0) for i in range(72)], symbol="MU-USDT-SWAP")
        # Partial funding covering only the last 24h (leading 48h gap).
        partial_f = FundingSeries.from_events(
            inst.instrument_id,
            [FundingRateEvent(int(bars.close_times[i]), 0.0001) for i in (55, 63, 71)],
            settlement_interval_hours=8,
        )
        strat = _ScriptedStrategy({0: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 1.0),))}, warmup=0)
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        sens = sim.run_with_funding_sensitivity(strat, bars, funding=partial_f, apr_band=(0.0, 0.15, 0.15000001))
        self.assertEqual("historical_gap_filled", sens.mode)
        self.assertEqual(3, len(sens.results))
        self.assertEqual(
            {"historical+apr_0pct", "historical+apr_15pct", "historical+apr_15.000001pct"},
            set(sens.results.keys()),
        )
        self.assertGreater(
            sens.results["historical+apr_15pct"].attribution.funding_pnl,
            sens.results["historical+apr_15.000001pct"].attribution.funding_pnl,
        )

    def test_require_touch_through_propagated_and_excluded_price_bands_enforced_cr2_15_cr2_44(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        bars = _make_series(
            [
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (100.5, 104.0, 99.5, 103.0, 1000.0),
                (103.0, 104.0, 102.0, 103.0, 1000.0),
            ],
            symbol="MUUSDT",
        )

        # CR2-44: BarContext.require_touch_through matches SimulatorConfig.require_touch_through in both modes.
        class _RttObserver:
            strategy_id = "rtt_obs"
            version = "1.0.0"
            required_intervals = ("1h",)
            warmup_bars = {"1h": 0}
            supports_ephemeral_context = True
            params = None

            def __init__(self) -> None:
                self.seen: list[bool] = []

            def reset(self) -> None:
                self.seen = []

            def on_bar(self, ctx: BarContext) -> StrategyOutput:
                self.seen.append(ctx.require_touch_through)
                return EMPTY_OUTPUT

            def snapshot_state(self) -> dict[str, Any]:
                return {}

            def restore_state(self, state: Mapping[str, Any]) -> None:
                return None

        for rtt_flag in (False, True):
            for reuse in (False, True):
                obs = _RttObserver()
                sim_rtt = Simulator(
                    inst,
                    CostModel.zero_cost(),
                    SimulatorConfig(starting_equity=10_000.0, require_touch_through=rtt_flag, reuse_bar_context=reuse),
                )
                sim_rtt.run(obs, bars)
                self.assertEqual([rtt_flag] * 3, obs.seen)

        # CR2-15: excluded_price_bands blocks market, limit, stop_market, and stop_limit fills inside the band
        # without blocking fills outside the band.
        band = (PriceExclusionBand(center=100.0, tolerance_pct=0.01),)  # excludes ~[99.01, 101.01]
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))
        blocked_intents = (
            OrderIntent("b_mkt", "MUUSDT", "buy", "market", "contracts", 1.0, excluded_price_bands=band, expires_after_bars=1),
            OrderIntent("b_lim", "MUUSDT", "buy", "limit", "contracts", 1.0, limit_price=100.5, excluded_price_bands=band, expires_after_bars=1),
            OrderIntent("b_stp", "MUUSDT", "buy", "stop_market", "contracts", 1.0, stop_price=100.5, excluded_price_bands=band, expires_after_bars=1),
            OrderIntent("b_sl", "MUUSDT", "buy", "stop_limit", "contracts", 1.0, stop_price=100.5, limit_price=101.5, excluded_price_bands=band, expires_after_bars=1),
        )
        res_blocked = sim.run(_ScriptedStrategy({0: StrategyOutput(intents=blocked_intents)}, warmup=0), bars)
        self.assertEqual(0, res_blocked.trade_count)

        # Fill outside band (stop_market at 103.0) fills cleanly!
        ok_intent = OrderIntent(
            "ok_stp", "MUUSDT", "buy", "stop_market", "contracts", 2.0, stop_price=103.0, excluded_price_bands=band, expires_after_bars=1
        )
        res_ok = sim.run(_ScriptedStrategy({0: StrategyOutput(intents=(ok_intent,))}, warmup=0), bars)
        self.assertEqual(1, res_ok.trade_count)
        self.assertAlmostEqual(103.0, res_ok.trades[0].entry_price, places=6)

    def test_blocked_target_never_overwrites_existing_bracket_cr2_16(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        # Bar 0: 14:00 UTC (10:00 ET, inside 09:45-11:30)
        # Bar 1: 15:00 UTC (11:00 ET, inside) -> enters 1x long at 100 with stop=90
        # Bar 2: 20:00 UTC (16:00 ET, OUTSIDE) -> queued short target (-1.0, stop=110) is BLOCKED;
        #   existing long stop=90 MUST remain intact (NOT overwritten with 110 which would close the long at open=100!).
        # Bar 3: 21:00 UTC (17:00 ET, OUTSIDE) -> queued scale-up target (+2.0, stop=99) is BLOCKED;
        #   existing long stop=90 MUST remain intact!
        # Bar 4: 14:00 UTC next day (inside) -> same-side hold (+1.0, stop=95) refreshes stop to 95;
        #   low=94 triggers refreshed stop at 95!
        t_in0 = int(datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_in1 = int(datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_out2 = int(datetime(2026, 6, 11, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_out3 = int(datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_in4 = int(datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_in5 = int(datetime(2026, 6, 12, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        candles = [
            Candle(t_in0, 100.0, 101.0, 99.0, 100.0, 1000.0),
            Candle(t_in1, 100.0, 101.0, 99.0, 100.0, 1000.0),
            Candle(t_out2, 100.0, 101.0, 98.0, 100.0, 1000.0),
            Candle(t_out3, 100.0, 101.0, 98.0, 100.0, 1000.0),
            Candle(t_in4, 100.0, 101.0, 99.0, 100.0, 1000.0),
            Candle(t_in5, 100.0, 101.0, 94.0, 96.0, 1000.0),
        ]
        bars = BarSeries.from_candles("MU-USDT-SWAP", "1h", candles)
        sw = SessionWindowOverlay(
            trading_windows_et=(("09:45", "11:30"),),
            calendar_id=US_EQUITIES_ALGORITHMIC_CALENDAR,
        )
        strat = _ScriptedStrategy(
            {
                0: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 1.0, stop_price=90.0),)),
                1: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", -1.0, stop_price=110.0),)),
                2: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 2.0, stop_price=99.0),)),
                4: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 1.0, stop_price=95.0),)),
            },
            warmup=0,
        )
        sim = Simulator(
            inst,
            CostModel.zero_cost(),
            SimulatorConfig(starting_equity=10_000.0, allow_missing_funding_for_perp=True),
        )
        res = sim.run(strat, bars, overlays=(sw,))
        self.assertEqual(1, res.trade_count)
        self.assertEqual("long", res.trades[0].side)
        self.assertEqual("stop_loss", res.trades[0].reason)
        self.assertAlmostEqual(95.0, res.trades[0].exit_price, places=6)
        self.assertEqual(t_in5, res.trades[0].exit_time_ms)
        _assert_accounting_invariants(self, res)

    def test_spot_and_equity_never_settle_funding_even_with_matching_series_cr2_17(self) -> None:
        t0 = 1_735_689_600_000
        h = 3_600_000
        # Include a 3h gap between bar 1 close (t0+2h) and bar 2 open (t0+5h) so BOTH gap and bar-close funding paths are tested!
        candles = [
            Candle(t0, 100.0, 101.0, 99.0, 100.0, 1000.0),
            Candle(t0 + h, 100.0, 101.0, 99.0, 100.0, 1000.0),
            Candle(t0 + 5 * h, 100.0, 101.0, 99.0, 100.0, 1000.0),
            Candle(t0 + 6 * h, 100.0, 101.0, 99.0, 100.0, 1000.0),
        ]
        for inst in (
            spot_spec("MUUSDT", tick_size=Decimal("0.01")),
            spot_spec("MU", venue="nasdaq", tick_size=Decimal("0.01"), asset_class="equity"),
        ):
            bars = BarSeries.from_candles(inst.symbol, "1h", candles)
            matching_funding = FundingSeries.from_events(
                inst.instrument_id,
                [
                    FundingRateEvent(t0 + 3 * h, 0.01),   # gap event
                    FundingRateEvent(t0 + 6 * h, 0.01),   # bar-close event
                ],
                settlement_interval_hours=8,
            )
            for reuse in (False, True):
                with self.subTest(instrument=inst.instrument_id, reuse=reuse):
                    sim = Simulator(
                        inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0, reuse_bar_context=reuse)
                    )
                    strat = _ScriptedStrategy(
                        {0: StrategyOutput(targets=(TargetPosition(inst.symbol, 1.0),))},
                        warmup=0,
                        supports_ephemeral_context=reuse,
                    )
                    res = sim.run(strat, bars, funding=matching_funding)
                    self.assertEqual("spot_no_funding", res.funding_mode)
                    self.assertEqual(0.0, res.attribution.funding_pnl)
                    _assert_accounting_invariants(self, res)

                    sens = sim.run_with_funding_sensitivity(strat, bars, funding=matching_funding)
                    self.assertEqual(0.0, sens.base_result.attribution.funding_pnl)

    def test_intrabar_activated_protective_stop_limit_never_matches_earlier_open_cr2_18(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))

        # Case 1: Protective sell stop_limit(stop=95, limit=97) on bar 2 (open=100, high=100, low=94, close=94):
        # Activates intrabar at 95 (cannot fill at 95 < 97); MUST NOT match earlier bar 2 open=100!
        # Bar 3 opens at 98 -> now pre-open activated, fills cleanly at bar 3 open=98.0!
        bars_intrabar = _make_series(
            [
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (100.0, 100.0, 94.0, 94.0, 1000.0),
                (98.0, 99.0, 96.0, 98.0, 1000.0),
                (98.0, 98.5, 97.5, 98.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        strat_intrabar = _ScriptedStrategy(
            {
                0: StrategyOutput(intents=(OrderIntent("e1", "MUUSDT", "buy", "market", "contracts", 10.0),)),
                1: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "prot_sl",
                            "MUUSDT",
                            "sell",
                            "stop_limit",
                            "close_all",
                            0.0,
                            stop_price=95.0,
                            limit_price=97.0,
                            reduce_only=True,
                            expires_after_bars=3,
                        ),
                    )
                ),
            },
            warmup=0,
        )
        res_intra = sim.run(strat_intrabar, bars_intrabar)
        self.assertEqual(1, res_intra.trade_count)
        self.assertAlmostEqual(98.0, res_intra.trades[0].exit_price, places=6)
        self.assertEqual(int(bars_intrabar.open_times[3]), res_intra.trades[0].exit_time_ms)
        _assert_accounting_invariants(self, res_intra)

        # Case 2: Activated AT OPEN past limit (sell stop=95, limit=97, bar 2 open=94, high=98, low=93):
        # Activates at open 94, then fills intrabar on return to limit=97.0 (NOT at 94!).
        bars_at_open = _make_series(
            [
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (94.0, 98.0, 93.0, 96.0, 1000.0),
                (96.0, 96.5, 95.5, 96.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        res_at_open = sim.run(strat_intrabar, bars_at_open)
        self.assertEqual(1, res_at_open.trade_count)
        self.assertAlmostEqual(97.0, res_at_open.trades[0].exit_price, places=6)
        self.assertEqual(int(bars_at_open.open_times[2]), res_at_open.trades[0].exit_time_ms)
        _assert_accounting_invariants(self, res_at_open)

    def test_open_only_limits_expire_after_missed_eligible_open_in_both_queues_cr2_19(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        sim = Simulator(inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0))

        # Entry queue: buy limit 95 with time_in_force="open_only" and expires_after_bars=None.
        # Bar 1 opens at 100 (misses!) -> MUST expire; bar 2 open=90 MUST NOT fill!
        bars_ent = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 101.0, 90.0, 95.0, 1000.0),
                (90.0, 91.0, 89.0, 90.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        strat_miss = _ScriptedStrategy(
            {
                0: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "oo_buy",
                            "MUUSDT",
                            "buy",
                            "limit",
                            "contracts",
                            10.0,
                            limit_price=95.0,
                            time_in_force="open_only",
                        ),
                    )
                )
            },
            warmup=0,
        )
        self.assertEqual(0, sim.run(strat_miss, bars_ent).trade_count)

        # Marketable open_only limit (buy limit 105 on open 100) STILL fills at 100!
        strat_hit = _ScriptedStrategy(
            {
                0: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "oo_hit",
                            "MUUSDT",
                            "buy",
                            "limit",
                            "contracts",
                            10.0,
                            limit_price=105.0,
                            time_in_force="open_only",
                        ),
                    )
                )
            },
            warmup=0,
        )
        res_hit = sim.run(strat_hit, bars_ent)
        self.assertEqual(1, res_hit.trade_count)
        self.assertAlmostEqual(100.0, res_hit.trades[0].entry_price, places=6)

        # Protective queue: open_only reduce_only sell limit 110 misses bar 2 open=100 -> MUST expire before bar 3 open=115!
        bars_prot = _make_series(
            [
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 101.0, 99.0, 100.0, 1000.0),
                (100.0, 112.0, 99.0, 105.0, 1000.0),
                (115.0, 116.0, 114.0, 115.0, 1000.0),
                (102.0, 103.0, 101.0, 102.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        strat_prot = _ScriptedStrategy(
            {
                0: StrategyOutput(intents=(OrderIntent("e1", "MUUSDT", "buy", "market", "contracts", 10.0),)),
                1: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "oo_tp",
                            "MUUSDT",
                            "sell",
                            "limit",
                            "close_all",
                            0.0,
                            limit_price=110.0,
                            reduce_only=True,
                            time_in_force="open_only",
                        ),
                    )
                ),
            },
            warmup=0,
        )
        res_prot = sim.run(strat_prot, bars_prot)
        self.assertEqual(1, res_prot.trade_count)
        self.assertEqual("end_of_data", res_prot.trades[0].reason)
        self.assertAlmostEqual(102.0, res_prot.trades[0].exit_price, places=6)

    def test_mid_bar_liquidation_resolves_before_later_intrabar_entry_cr2_20(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=10.0)
        # Bar 1 opens at 100, crashes to low=65, closes at 70.
        # 5x long opened at 100 has liq near 80 -> path 100 -> 70 MUST liquidate BEFORE queued buy limit at 70!
        bars = _make_series(
            [
                (100.0, 100.0, 100.0, 100.0, 1000.0),
                (100.0, 100.0, 65.0, 70.0, 1000.0),
                (70.0, 72.0, 68.0, 70.0, 1000.0),
            ],
            symbol="MU-USDT-SWAP",
        )
        cost_hi_mmr = CostModel(
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            half_spread_bps=0.0,
            slippage_model=FixedTicks(0),
            maintenance_margin_rate=0.05,
            liquidation_fee_bps=0.0,
        )
        mkt_5x = OrderIntent("open5x", "MU-USDT-SWAP", "buy", "market", "equity_fraction", 5.0)
        lim_70 = OrderIntent("add70", "MU-USDT-SWAP", "buy", "limit", "contracts", 50.0, limit_price=70.0)

        for label, order_pair in (("market_then_limit", (mkt_5x, lim_70)), ("limit_then_market", (lim_70, mkt_5x))):
            for cost in (CostModel.zero_cost(), cost_hi_mmr):
                with self.subTest(order=label, mmr=cost.maintenance_margin_rate):
                    sim = Simulator(
                        inst,
                        cost,
                        SimulatorConfig(starting_equity=10_000.0, allow_missing_funding_for_perp=True),
                    )
                    strat = _ScriptedStrategy({0: StrategyOutput(intents=order_pair)}, warmup=0)
                    res = sim.run(strat, bars)
                    self.assertEqual(1, res.liquidations)
                    self.assertEqual(1, res.trade_count)
                    self.assertEqual("liquidation", res.trades[0].reason)
                    self.assertEqual(1, res.trades[0].max_stage)
                    _assert_accounting_invariants(self, res)

    def test_protective_orders_replaced_by_intent_id_not_order_type_cr2_21(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        # Bar 1: enters 10 contracts at 100.
        # Bar 2: open=100, low=94, close=94 -> activates stop_limit "sl_a" (stop=95, limit=98) without filling!
        #        At bar 2 close, strategy emits an independent stop_market "sl_b" (stop=90).
        # Bar 3: open=94, high=99, low=93, close=98 -> reaches 98 (never hits 90); MUST fill "sl_a" at 98.0!
        bars = _make_series(
            [
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (100.0, 100.5, 99.5, 100.0, 1000.0),
                (100.0, 100.0, 94.0, 94.0, 1000.0),
                (94.0, 99.0, 93.0, 98.0, 1000.0),
                (98.0, 98.5, 97.5, 98.0, 1000.0),
            ],
            symbol="MUUSDT",
        )
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                strat = _ScriptedStrategy(
                    {
                        0: StrategyOutput(intents=(OrderIntent("e1", "MUUSDT", "buy", "market", "contracts", 10.0),)),
                        1: StrategyOutput(
                            intents=(
                                OrderIntent(
                                    "sl_a",
                                    "MUUSDT",
                                    "sell",
                                    "stop_limit",
                                    "close_all",
                                    0.0,
                                    stop_price=95.0,
                                    limit_price=98.0,
                                    reduce_only=True,
                                    expires_after_bars=3,
                                    tag="activated_sl_a",
                                ),
                            )
                        ),
                        2: StrategyOutput(
                            intents=(
                                OrderIntent(
                                    "sl_b",
                                    "MUUSDT",
                                    "sell",
                                    "stop_market",
                                    "close_all",
                                    0.0,
                                    stop_price=90.0,
                                    reduce_only=True,
                                    expires_after_bars=3,
                                    tag="backup_sl_b",
                                ),
                            )
                        ),
                    },
                    warmup=0,
                    supports_ephemeral_context=reuse,
                )
                sim = Simulator(
                    inst, CostModel.zero_cost(), SimulatorConfig(starting_equity=10_000.0, reuse_bar_context=reuse)
                )
                res = sim.run(strat, bars)
                self.assertEqual(1, res.trade_count)
                self.assertEqual("activated_sl_a", res.trades[0].reason)
                self.assertAlmostEqual(98.0, res.trades[0].exit_price, places=6)
                self.assertEqual(int(bars.open_times[3]), res.trades[0].exit_time_ms)
                _assert_accounting_invariants(self, res)

    def test_opening_stop_limit_and_target_fill_bar_bracket_use_causal_pre_open_atr_fallback_cr2_22_cr2_23(self) -> None:
        inst = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        cost_atr = CostModel(
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            half_spread_bps=0.0,
            slippage_model=AtrBps(bps_per_atr=100.0),
            stop_slippage_multiplier=1.0,
        )
        # Bar 0 is flat (high==low==100 -> pre_atr is None because atr_period=14, fallback uses pre_high-pre_low=0.0).
        # Bar 1 has IDENTICAL open=101 in both series, differing ONLY in Bar 1's (high, low) range!
        bars_narrow = _make_series(
            [(100.0, 100.0, 100.0, 100.0, 1000.0), (101.0, 101.5, 100.5, 101.0, 1000.0), (101.0, 101.0, 101.0, 101.0, 1000.0)],
            symbol="MUUSDT",
        )
        bars_wide = _make_series(
            [(100.0, 100.0, 100.0, 100.0, 1000.0), (101.0, 150.0, 50.0, 101.0, 1000.0), (101.0, 101.0, 101.0, 101.0, 1000.0)],
            symbol="MUUSDT",
        )
        sim = Simulator(inst, cost_atr, SimulatorConfig(starting_equity=10_000.0, atr_period=14))

        # CR2-22: Buy stop_limit(stop=100.5, limit=120.0) triggered & filled AT OPEN of bar 1 ->
        # opening execution price MUST NOT depend on bar 1's later (high, low) range!
        strat_sl = _ScriptedStrategy(
            {
                0: StrategyOutput(
                    intents=(
                        OrderIntent(
                            "sl_open",
                            "MUUSDT",
                            "buy",
                            "stop_limit",
                            "contracts",
                            10.0,
                            stop_price=100.5,
                            limit_price=120.0,
                        ),
                    )
                )
            },
            warmup=0,
        )
        res_sl_n = sim.run(strat_sl, bars_narrow)
        res_sl_w = sim.run(strat_sl, bars_wide)
        self.assertAlmostEqual(res_sl_n.trades[0].entry_price, res_sl_w.trades[0].entry_price, places=12)
        self.assertAlmostEqual(101.0, res_sl_w.trades[0].entry_price, places=12)

        # CR2-23: Target installs a bracket stop (stop_price=102.0) already triggered at bar 1 open=101.0 ->
        # BOTH target-only fast path AND overlay/pending path MUST price stop strictly from pre-open data!
        strat_tgt = _ScriptedStrategy(
            {0: StrategyOutput(targets=(TargetPosition("MUUSDT", 1.0, stop_price=102.0),))},
            warmup=0,
        )
        for overlays in ((), (ExposureCapOverlay(max_gross_leverage=5.0, max_single_weight=5.0),)):
            res_t_n = sim.run(strat_tgt, bars_narrow, overlays=overlays)
            res_t_w = sim.run(strat_tgt, bars_wide, overlays=overlays)
            self.assertEqual("stop_loss", res_t_n.trades[0].reason)
            self.assertEqual("stop_loss", res_t_w.trades[0].reason)
            self.assertAlmostEqual(res_t_n.trades[0].exit_price, res_t_w.trades[0].exit_price, places=12)
            self.assertAlmostEqual(101.0, res_t_w.trades[0].exit_price, places=12)

    def test_same_side_reducing_target_never_flips_or_full_closes_cr2_24(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        # 100 bps half-spread => sell at open=100 executes at 99.0!
        cost = CostModel(
            maker_fee_bps=0.0, taker_fee_bps=0.0, half_spread_bps=100.0, slippage_model=FixedTicks(0)
        )
        t_in0 = int(datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_in1 = int(datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_out2 = int(datetime(2026, 6, 11, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)  # outside session!
        t_out3 = int(datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc).timestamp() * 1000)
        candles = [
            Candle(t_in0, 100.0, 100.0, 100.0, 100.0, 1000.0),
            Candle(t_in1, 100.0, 100.0, 100.0, 100.0, 1000.0),
            Candle(t_out2, 100.0, 100.0, 100.0, 100.0, 1000.0),
            Candle(t_out3, 100.0, 100.0, 100.0, 100.0, 1000.0),
        ]
        bars = BarSeries.from_candles("MU-USDT-SWAP", "1h", candles)
        sw = SessionWindowOverlay(
            trading_windows_et=(("09:45", "11:30"),),
            calendar_id=US_EQUITIES_ALGORITHMIC_CALENDAR,
        )
        sim = Simulator(
            inst, cost, SimulatorConfig(starting_equity=1_000.0, allow_missing_funding_for_perp=True)
        )

        # Enter long at bar 1 open (~9.90099 units), then emit same-side reducing target_weight=0.005
        # (desired_notional ~ 5, where naive delta/99 > live qty would flip short or full-close!).
        for overlays in ((), (sw,)):
            with self.subTest(has_session_overlay=bool(overlays)):
                strat_tiny = _ScriptedStrategy(
                    {
                        0: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 1.0),)),
                        1: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 0.005),)),
                    },
                    warmup=0,
                )
                res = sim.run(strat_tiny, bars, overlays=overlays)
                # Two trades: partial reduction on bar 2 (long) + remaining positive long closed at end_of_data!
                self.assertEqual(2, res.trade_count)
                self.assertEqual("long", res.trades[0].side)
                self.assertEqual("long", res.trades[1].side)
                self.assertEqual("end_of_data", res.trades[1].reason)
                self.assertGreater(res.trades[1].qty, 0.04)
                _assert_accounting_invariants(self, res)

        # Ordinary partial reduction (target_weight=0.5) stays unchanged!
        strat_half = _ScriptedStrategy(
            {
                0: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 1.0),)),
                1: StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 0.5),)),
            },
            warmup=0,
        )
        res_half = sim.run(strat_half, bars)
        self.assertEqual(2, res_half.trade_count)
        self.assertEqual("long", res_half.trades[0].side)
        self.assertEqual("long", res_half.trades[1].side)
        _assert_accounting_invariants(self, res_half)

    def test_buy_and_hold_benchmark_round2_fixes_cr2_30_to_34_and_47(self) -> None:
        # CR2-32: Reject instrument whose symbol does not match the bar series.
        eth_bars = _make_series([(100.0, 101.0, 99.0, 100.0, 1000.0)] * 5, symbol="ETHUSDT", interval="1h")
        btc_inst = okx_swap_spec("BTCUSDT", tick_size=Decimal("0.1"), max_leverage=5.0)
        with self.assertRaisesRegex(ValueError, "does not match bars symbol"):
            compute_buy_and_hold_benchmark(
                eth_bars,
                instrument=btc_inst,
                allow_missing_funding_for_perp=True,
            )

        # CR2-47: Empty FundingSeries treated identically to None (raises without opt-in, labels missing_explicit_zero with opt-in).
        perp = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        short_bars = _make_series([(100.0, 101.0, 99.0, 100.0, 1000.0)] * 3, symbol="MU-USDT-SWAP", interval="1h")
        empty_funding = FundingSeries.from_events(perp.instrument_id, [], settlement_interval_hours=8)
        with self.assertRaises(MissingFundingHistoryError):
            compute_buy_and_hold_benchmark(short_bars, instrument=perp, funding=empty_funding)
        bh_empty_ok = compute_buy_and_hold_benchmark(
            short_bars,
            instrument=perp,
            funding=empty_funding,
            allow_missing_funding_for_perp=True,
        )
        self.assertEqual("missing_explicit_zero", bh_empty_ok.funding_mode)

        # CR2-33: Combining instrument + cost_model + liquidation override uses dataclasses.replace (no AttributeError)
        # and preserves cost_model.slippage_model!
        custom_cost = CostModel(
            maker_fee_bps=1.0,
            taker_fee_bps=5.0,
            half_spread_bps=2.0,
            slippage_model=FixedTicks(3),
            maintenance_margin_rate=0.005,
            liquidation_fee_bps=10.0,
        )
        bh_ov = compute_buy_and_hold_benchmark(
            short_bars,
            instrument=perp,
            cost_model=custom_cost,
            maintenance_margin_rate=0.02,
            liquidation_fee_bps=25.0,
            allow_missing_funding_for_perp=True,
        )
        self.assertGreater(bh_ov.attribution.slippage, 0.0)
        _assert_accounting_invariants(self, bh_ov)

        # CR2-30: With warmup_bars=0 and SqrtVolumeImpact, changing ONLY bar 0 volume MUST NOT change entry price!
        cost_vol = CostModel(
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            half_spread_bps=0.0,
            slippage_model=SqrtVolumeImpact(impact_coeff=50.0, max_bps=500.0),
        )
        spot = spot_spec("MUUSDT", tick_size=Decimal("0.01"))
        b_low_v = _make_series([(100.0, 101.0, 99.0, 100.0, 10.0), (100.0, 101.0, 99.0, 100.0, 1000.0)], symbol="MUUSDT")
        b_hi_v = _make_series([(100.0, 101.0, 99.0, 100.0, 1_000_000.0), (100.0, 101.0, 99.0, 100.0, 1000.0)], symbol="MUUSDT")
        bh_v1 = compute_buy_and_hold_benchmark(b_low_v, instrument=spot, cost_model=cost_vol, warmup_bars=0)
        bh_v2 = compute_buy_and_hold_benchmark(b_hi_v, instrument=spot, cost_model=cost_vol, warmup_bars=0)
        self.assertAlmostEqual(bh_v1.trades[0].entry_price, bh_v2.trades[0].entry_price, places=12)

        # CR2-31: Gap-at-open liquidation in leveraged benchmark uses pre-open inputs (independent of current bar H/L/V).
        b_gap_a = _make_series(
            [(100.0, 101.0, 99.0, 100.0, 5_000.0), (70.0, 71.0, 69.0, 70.0, 50.0), (70.0, 70.0, 70.0, 70.0, 5_000.0)],
            symbol="MU-USDT-SWAP",
        )
        b_gap_b = _make_series(
            [(100.0, 101.0, 99.0, 100.0, 5_000.0), (70.0, 95.0, 20.0, 70.0, 5_000_000.0), (70.0, 70.0, 70.0, 70.0, 5_000.0)],
            symbol="MU-USDT-SWAP",
        )
        bh_ga = compute_buy_and_hold_benchmark(
            b_gap_a, leverage=5.0, instrument=perp, cost_model=cost_vol, allow_missing_funding_for_perp=True
        )
        bh_gb = compute_buy_and_hold_benchmark(
            b_gap_b, leverage=5.0, instrument=perp, cost_model=cost_vol, allow_missing_funding_for_perp=True
        )
        self.assertEqual(1, bh_ga.liquidations)
        self.assertEqual(1, bh_gb.liquidations)
        self.assertAlmostEqual(bh_ga.trades[0].exit_price, bh_gb.trades[0].exit_price, places=12)
        self.assertAlmostEqual(bh_ga.attribution.net_pnl, bh_gb.attribution.net_pnl, places=9)

        # CR2-34: End-of-data exit under AtrBps uses Wilder ATR matching Simulator (even when final bar is flat!).
        cost_atr = CostModel(
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            half_spread_bps=0.0,
            slippage_model=AtrBps(bps_per_atr=100.0),
        )
        wide_then_flat = [(100.0, 110.0, 90.0, 100.0, 1000.0)] * 20 + [(100.0, 100.0, 100.0, 100.0, 1000.0)]
        b_atr = _make_series(wide_then_flat, symbol="MUUSDT")
        bh_atr = compute_buy_and_hold_benchmark(b_atr, warmup_bars=1, instrument=spot, cost_model=cost_atr, atr_period=14)
        sim_atr = Simulator(spot, cost_atr, SimulatorConfig(starting_equity=10_000.0, atr_period=14))
        sim_res = sim_atr.run(_ScriptedStrategy({0: StrategyOutput(targets=(TargetPosition("MUUSDT", 1.0),))}, warmup=0), b_atr)
        # Final bar has high==low==100.0, yet Wilder ATR > 0 -> BOTH benchmark and simulator charge identical EOD exit slippage!
        self.assertLess(bh_atr.trades[0].exit_price, 100.0)
        self.assertAlmostEqual(sim_res.trades[0].exit_price, bh_atr.trades[0].exit_price, places=12)
        _assert_accounting_invariants(self, bh_atr)


if __name__ == "__main__":
    unittest.main()
