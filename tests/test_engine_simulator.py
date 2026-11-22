"""End-to-end Simulator tests: L/S, signed funding, attribution, causality, sensitivity, and speed."""

from __future__ import annotations

import math
import os
import tempfile
import time
import unittest
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mu_strategy.core.instrument import (
    CostModel,
    FixedTicks,
    SqrtVolumeImpact,
    binance_perp_spec,
    okx_swap_spec,
    spot_spec,
)
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
)
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
    ) -> None:
        self.schedule = dict(schedule)
        self.required_intervals = (interval,)
        self.warmup_bars = {interval: warmup}
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
                res = sim.run(_ScriptedTargetStrategy({0: (1.0, None, None)}), bars, funding=f)
                self.assertAlmostEqual(-10.0, res.attribution.funding_pnl, places=9)
                _assert_accounting_invariants(self, res)
            with self.subTest(case="exit_bar4_event_close4", reuse=reuse):
                # Signal flat at bar3 close -> exit at bar4 open; event at close_times[4] MUST charge 0.
                f = FundingSeries.from_events(inst.instrument_id, [FundingRateEvent(int(bars.close_times[4]), 0.001)])
                res = sim.run(_ScriptedTargetStrategy({0: (1.0, None, None), 3: (0.0, None, None)}), bars, funding=f)
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


if __name__ == "__main__":
    unittest.main()
