"""Unit and lifecycle tests for all 4 Layer 2 concrete strategy families."""

from __future__ import annotations

import json
import math
import unittest

from mu_strategy.core.instrument import okx_swap_spec
from mu_strategy.market_data.bar_series import BarSeries, MarketView
from mu_strategy.models import Candle
from mu_strategy.strategies.families import (
    DonchianATRParams,
    DonchianATRStrategy,
    FibonacciPullbackParams,
    FibonacciPullbackStrategy,
    MeanReversionParams,
    MeanReversionStrategy,
    TSMomentumParams,
    TSMomentumStrategy,
)
from mu_strategy.strategies.features import FeatureCache
from mu_strategy.strategies.protocol import (
    BarContext,
    PortfolioView,
    PositionView,
    Strategy,
)
from mu_strategy.strategy import StrategyConfig


def _build_candles(
    closes: list[float],
    *,
    start_ms: int = 1_767_624_900_000,  # 2026-01-05 09:55 ET (Monday US cash window)
    interval_ms: int = 3_600_000,
    spread: float = 0.5,
) -> list[Candle]:
    bars: list[Candle] = []
    for i, c in enumerate(closes):
        open_ms = start_ms + i * interval_ms
        prev_c = closes[i - 1] if i > 0 else c
        bars.append(
            Candle(
                open_time_ms=open_ms,
                open=prev_c,
                high=max(prev_c, c) + spread,
                low=max(0.01, min(prev_c, c) - spread),
                close=c,
                volume=1000.0,
            )
        )
    return bars


def _portfolio(symbol: str = "MU-USDT-SWAP", qty: float = 0.0, avg_px: float = 0.0, equity: float = 10_000.0) -> PortfolioView:
    positions = {}
    if qty != 0.0:
        positions[symbol] = PositionView(
            symbol=symbol,
            qty=qty,
            avg_entry_price=avg_px,
            unrealized_pnl=0.0,
            notional=abs(qty) * avg_px,
            leverage=1.0,
        )
    return PortfolioView(
        equity=equity,
        cash=equity,
        positions=positions,
        open_orders=(),
        peak_equity=equity,
        daily_start_equity=equity,
    )


class TSMomentumStrategyTests(unittest.TestCase):
    def test_long_short_flat_and_side_modes(self) -> None:
        # Rise for 15 bars, then fall for 20 bars
        closes = [100.0 + i * 1.5 for i in range(15)] + [121.0 - i * 2.0 for i in range(20)]
        series = BarSeries.from_candles("BTC-USDT-SWAP", "1h", _build_candles(closes))
        spec = okx_swap_spec("BTC-USDT-SWAP")
        fc = FeatureCache()

        strat_ls = TSMomentumStrategy(TSMomentumParams(lookback_bars=5, target_weight=1.2, side_mode="long_short"))
        strat_buy = TSMomentumStrategy(TSMomentumParams(lookback_bars=5, target_weight=1.2, side_mode="buy"))
        strat_sell = TSMomentumStrategy(TSMomentumParams(lookback_bars=5, target_weight=1.2, side_mode="sell"))

        self.assertIsInstance(strat_ls, Strategy)

        # Uptrend bar (index 10): long_short -> +1.2, buy -> +1.2, sell -> flat
        mv_up = MarketView({("BTC-USDT-SWAP", "1h"): series}, now_ms=series.close_times[10], features=fc)
        ctx_up = BarContext(series.close_times[10], "BTC-USDT-SWAP", mv_up, _portfolio("BTC-USDT-SWAP"), spec)
        out_ls_up = strat_ls.on_bar(ctx_up)
        out_buy_up = strat_buy.on_bar(ctx_up)
        out_sell_up = strat_sell.on_bar(ctx_up)

        self.assertEqual(1, len(out_ls_up.targets))
        self.assertAlmostEqual(1.2, out_ls_up.targets[0].target_weight)
        self.assertAlmostEqual(1.2, out_buy_up.targets[0].target_weight)
        self.assertEqual((), out_sell_up.targets)

        # Downtrend bar (index 25): long_short -> -1.2, buy -> 0.0 (exit), sell -> -1.2
        mv_dn = MarketView({("BTC-USDT-SWAP", "1h"): series}, now_ms=series.close_times[25], features=fc)
        ctx_dn_long = BarContext(series.close_times[25], "BTC-USDT-SWAP", mv_dn, _portfolio("BTC-USDT-SWAP", qty=1.0, avg_px=115.0), spec)
        ctx_dn_flat = BarContext(series.close_times[25], "BTC-USDT-SWAP", mv_dn, _portfolio("BTC-USDT-SWAP"), spec)

        out_ls_dn = strat_ls.on_bar(ctx_dn_long)
        out_buy_dn = strat_buy.on_bar(ctx_dn_long)
        out_sell_dn = strat_sell.on_bar(ctx_dn_flat)

        self.assertAlmostEqual(-1.2, out_ls_dn.targets[0].target_weight)
        self.assertEqual(0.0, out_buy_dn.targets[0].target_weight)
        self.assertAlmostEqual(-1.2, out_sell_dn.targets[0].target_weight)

    def test_deadband_hysteresis_and_state_restore_determinism(self) -> None:
        closes = [100.0] * 6 + [105.0, 105.2, 105.1, 104.9, 104.8, 105.0, 99.0, 98.0]
        series = BarSeries.from_candles("BTC-USDT-SWAP", "1h", _build_candles(closes))
        spec = okx_swap_spec("BTC-USDT-SWAP")
        fc = FeatureCache()

        params = TSMomentumParams(lookback_bars=3, deadband_return=0.02, use_deadband_hysteresis=True, stop_atr_mult=2.0, atr_period=3)
        full = TSMomentumStrategy(params)
        split = TSMomentumStrategy(params)
        mv_root = MarketView({("BTC-USDT-SWAP", "1h"): series}, now_ms=series.close_times[0], features=fc)

        full_outputs = []
        split_outputs = []
        for idx in range(len(closes)):
            ts = series.close_times[idx]
            ctx = BarContext(ts, "BTC-USDT-SWAP", mv_root.at(ts), _portfolio("BTC-USDT-SWAP"), spec)
            full_outputs.append(full.on_bar(ctx))
            if idx == 8:
                blob = json.dumps(split.snapshot_state())
                split = TSMomentumStrategy(params)
                split.restore_state(json.loads(blob))
            split_outputs.append(split.on_bar(ctx))

        self.assertEqual([o.targets for o in full_outputs], [o.targets for o in split_outputs])
        # Bar 6 jumped +5% -> long with valid stop_price < close
        self.assertEqual(1.0, full_outputs[6].targets[0].target_weight)
        self.assertIsNotNone(full_outputs[6].targets[0].stop_price)
        self.assertLess(full_outputs[6].targets[0].stop_price, closes[6])
        # Bar 9 is inside deadband (+0% return over 3 bars) -> hysteresis keeps long!
        self.assertEqual(1.0, full_outputs[9].targets[0].target_weight)


class DonchianATRStrategyTests(unittest.TestCase):
    def test_breakout_monotone_trailing_stop_flip_and_state_restore(self) -> None:
        # 12 flat range bars [99..101], then breakout rally up to 120, then reversal down below 95
        closes = (
            [100.0, 100.5, 99.5, 100.2, 99.8, 100.1, 100.0, 99.7, 100.3, 99.9, 100.0, 100.2]
            + [104.0, 107.0, 111.0, 115.0, 114.5, 114.0]
            + [105.0, 96.0, 90.0, 88.0, 89.0]
        )
        series = BarSeries.from_candles("ETH-USDT-SWAP", "1h", _build_candles(closes, spread=0.4))
        spec = okx_swap_spec("ETH-USDT-SWAP")
        fc = FeatureCache()
        mv_root = MarketView({("ETH-USDT-SWAP", "1h"): series}, now_ms=series.close_times[0], features=fc)

        params = DonchianATRParams(entry_period=8, exit_period=4, atr_period=5, atr_stop_mult=2.0, side_mode="long_short")
        strat = DonchianATRStrategy(params)
        strat_clone = DonchianATRStrategy(params)
        self.assertIsInstance(strat, Strategy)

        # Simulate simple bar-by-bar portfolio feedback (fill next bar at close)
        pos_qty = 0.0
        avg_px = 0.0
        long_stops: list[float] = []
        saw_long = False
        saw_short = False

        for idx in range(len(closes)):
            ts = series.close_times[idx]
            ctx = BarContext(ts, "ETH-USDT-SWAP", mv_root.at(ts), _portfolio("ETH-USDT-SWAP", pos_qty, avg_px), spec)
            out = strat.on_bar(ctx)

            if idx == 15:
                # Snapshot & restore mid-trend while trailing stop is active
                snap = json.dumps(strat_clone.snapshot_state())
                strat_clone = DonchianATRStrategy(params)
                strat_clone.restore_state(json.loads(snap))
            out_clone = strat_clone.on_bar(ctx)
            self.assertEqual(out.targets, out_clone.targets, f"state restore mismatch at bar {idx}")

            if out.targets:
                t = out.targets[0]
                if t.target_weight > 0:
                    saw_long = True
                    self.assertIsNotNone(t.stop_price)
                    if pos_qty > 0:
                        long_stops.append(t.stop_price)  # type: ignore[arg-type]
                    if pos_qty <= 0:
                        pos_qty = 1.0
                        avg_px = closes[idx]
                elif t.target_weight < 0:
                    saw_short = True
                    self.assertIsNotNone(t.stop_price)
                    self.assertGreater(t.stop_price, closes[idx])
                    pos_qty = -1.0
                    avg_px = closes[idx]
                else:
                    pos_qty = 0.0
                    avg_px = 0.0

        self.assertTrue(saw_long)
        self.assertTrue(saw_short)
        self.assertGreaterEqual(len(long_stops), 2)
        # Verify long trailing stop is strictly non-decreasing (monotone ratchet!)
        for prev_s, next_s in zip(long_stops, long_stops[1:]):
            self.assertGreaterEqual(next_s, prev_s)


class MeanReversionStrategyTests(unittest.TestCase):
    def test_oversold_long_overbought_short_and_vol_gate(self) -> None:
        # Stable oscillation around 100 -> sharp oversold dip to 86 -> revert to 100 -> spike to 116 -> revert to 100
        closes = (
            [100.0 + (0.2 if i % 2 == 0 else -0.2) for i in range(25)]
            + [96.0, 91.0, 86.0, 93.0, 99.5, 100.0]
            + [100.0 + (0.2 if i % 2 == 0 else -0.2) for i in range(15)]
            + [105.0, 111.0, 117.0, 109.0, 100.5, 100.0]
        )
        series = BarSeries.from_candles("SOL-USDT-SWAP", "1h", _build_candles(closes, spread=0.3))
        spec = okx_swap_spec("SOL-USDT-SWAP")
        fc = FeatureCache()
        mv_root = MarketView({("SOL-USDT-SWAP", "1h"): series}, now_ms=series.close_times[0], features=fc)

        params = MeanReversionParams(
            rsi_period=5,
            zscore_period=10,
            entry_z=1.5,
            exit_z=0.3,
            rsi_oversold=35.0,
            rsi_overbought=65.0,
            max_ann_vol=None,
            side_mode="long_short",
        )
        strat = MeanReversionStrategy(params)
        strat_gated = MeanReversionStrategy(
            MeanReversionParams(
                rsi_period=5,
                zscore_period=10,
                entry_z=1.5,
                exit_z=0.3,
                rsi_oversold=35.0,
                rsi_overbought=65.0,
                max_ann_vol=0.05,  # tight vol ceiling should block high-vol shock entries
                vol_period=10,
                side_mode="long_short",
            )
        )

        # Oversold bar (index 27, close=86.0) triggers long
        ctx_os = BarContext(series.close_times[27], "SOL-USDT-SWAP", mv_root.at(series.close_times[27]), _portfolio("SOL-USDT-SWAP"), spec)
        out_os = strat.on_bar(ctx_os)
        self.assertEqual(1, len(out_os.targets))
        self.assertEqual(1.0, out_os.targets[0].target_weight)
        self.assertEqual("mr_entry_long", out_os.targets[0].reason)
        # Tight vol gate blocks the same shock bar
        self.assertEqual((), strat_gated.on_bar(ctx_os).targets)

        # Reversion bar (index 29, close=99.5) exits long to flat
        ctx_rev_l = BarContext(
            series.close_times[29], "SOL-USDT-SWAP", mv_root.at(series.close_times[29]), _portfolio("SOL-USDT-SWAP", qty=1.0, avg_px=86.0), spec
        )
        out_rev_l = strat.on_bar(ctx_rev_l)
        self.assertEqual(0.0, out_rev_l.targets[0].target_weight)
        self.assertEqual("mr_reverted_long", out_rev_l.targets[0].reason)

        # Overbought bar (index 48, close=117.0) triggers short
        ctx_ob = BarContext(series.close_times[48], "SOL-USDT-SWAP", mv_root.at(series.close_times[48]), _portfolio("SOL-USDT-SWAP"), spec)
        out_ob = strat.on_bar(ctx_ob)
        self.assertEqual(1, len(out_ob.targets))
        self.assertEqual(-1.0, out_ob.targets[0].target_weight)
        self.assertEqual("mr_entry_short", out_ob.targets[0].reason)

        # Snapshot/restore round-trip
        snap = json.dumps(strat.snapshot_state())
        restored = MeanReversionStrategy(params)
        restored.restore_state(json.loads(snap))
        ctx_rev_s = BarContext(
            series.close_times[50], "SOL-USDT-SWAP", mv_root.at(series.close_times[50]), _portfolio("SOL-USDT-SWAP", qty=-1.0, avg_px=117.0), spec
        )
        self.assertEqual(strat.on_bar(ctx_rev_s).targets, restored.on_bar(ctx_rev_s).targets)


class FibonacciPullbackStrategyTests(unittest.TestCase):
    def _build_fib_retest_fixture(self) -> tuple[BarSeries, BarSeries, int]:
        """Create a deterministic 15m swing [100 -> 110] followed by a tight 0.382 Fib retest inside US cash hours."""
        # Signal bar 35 sits at 2026-01-05 10:00 ET (Monday inside 09:45-11:30 ET cash window).
        sig_open_ms = 1_767_625_200_000
        bar_ms = 900_000
        start_15m = sig_open_ms - 35 * bar_ms
        bars_15m: list[Candle] = []
        for i in range(36):
            open_ms = start_15m + i * bar_ms
            if i < 20:
                p = 100.0 + i * 0.10
                lo = p - 0.15
                hi = p + 0.15
            elif i < 33:
                p = 102.0 + (i - 19) * 0.35
                lo = p - 0.15
                hi = p + 0.25
            elif i == 33:
                # Swing high = 112.0 via upper spike while close stays near trend
                p = 106.8
                lo = 106.5
                hi = 112.0
            elif i == 34:
                # Consolidation bar
                p = 107.0
                lo = 106.8
                hi = 107.4
            else:
                # Retest candle (index 35): micro-wick touching 0.382 fib cleanly while closing strong (MACD hist > 0)!
                swing_low = min(b.low for b in bars_15m[3:35])
                swing_high = max(b.high for b in bars_15m[3:35])
                fib_382 = round(swing_high - (swing_high - swing_low) * 0.382, 8)
                p = fib_382 + 0.55
                lo = fib_382 - 0.03  # tiny ~0.03% wick below fib!
                hi = fib_382 + 0.75
            bars_15m.append(
                Candle(
                    open_time_ms=open_ms,
                    open=p,
                    high=hi,
                    low=lo,
                    close=p,
                    volume=500.0,
                )
            )
        # Add 2 follow-up bars inside session window (10:15 ET, 10:30 ET) for limit fill + pyramid check
        last_close = bars_15m[-1].close
        for k in range(2):
            open_ms = start_15m + (36 + k) * bar_ms
            bars_15m.append(
                Candle(
                    open_time_ms=open_ms,
                    open=last_close,
                    high=last_close + 4.0,
                    low=last_close - 0.2,
                    close=last_close + 3.0,
                    volume=500.0,
                )
            )

        # Bullish 1h bars (green regime) covering well past the 15m window
        start_1h = start_15m - 30 * 3_600_000
        bars_1h = _build_candles([95.0 + i * 0.5 for i in range(55)], start_ms=start_1h, interval_ms=3_600_000, spread=0.3)
        return (
            BarSeries.from_candles("MU-USDT-SWAP", "15m", bars_15m),
            BarSeries.from_candles("MU-USDT-SWAP", "1h", bars_1h),
            35,
        )

    def test_lab_defaults_and_stop_floor_prevents_micro_wick_stopout(self) -> None:
        default_strat = FibonacciPullbackStrategy()
        self.assertIsInstance(default_strat, Strategy)
        self.assertEqual(32, default_strat.params.fib_lookback)
        self.assertGreater(default_strat.params.min_stop_pct, 0.0)
        self.assertGreater(default_strat.params.atr_stop_multiplier, 0.0)
        self.assertFalse(default_strat.params.legacy_parity)

        s15, s1h, sig_idx = self._build_fib_retest_fixture()
        spec = okx_swap_spec("MU-USDT-SWAP")
        fc = FeatureCache()
        mv = MarketView({("MU-USDT-SWAP", "15m"): s15, ("MU-USDT-SWAP", "1h"): s1h}, now_ms=s15.close_times[sig_idx], features=fc)
        ctx = BarContext(s15.close_times[sig_idx], "MU-USDT-SWAP", mv, _portfolio("MU-USDT-SWAP"), spec)

        legacy_cfg = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
        legacy_strat = FibonacciPullbackStrategy.from_legacy_config(legacy_cfg)
        self.assertTrue(legacy_strat.params.legacy_parity)
        self.assertEqual(1, legacy_strat.warmup_bars["15m"])

        lab_strat = FibonacciPullbackStrategy(
            FibonacciPullbackParams(
                symbol="MU-USDT-SWAP",
                entry_execution="second_pullback",
                fib_lookback=32,
                min_stop_pct=0.01,
                atr_stop_multiplier=1.5,
                use_session_window=True,
            )
        )

        out_legacy = legacy_strat.on_bar(ctx)
        out_lab = lab_strat.on_bar(ctx)

        self.assertEqual(2, len(out_legacy.intents))
        self.assertEqual(2, len(out_lab.intents))
        leg_entry, leg_stop = out_legacy.intents
        lab_entry, lab_stop = out_lab.intents

        self.assertEqual("limit", leg_entry.order_type)
        self.assertEqual(leg_entry.limit_price, lab_entry.limit_price)
        self.assertEqual("stop_market", leg_stop.order_type)
        self.assertTrue(leg_stop.reduce_only)
        assert leg_entry.limit_price is not None and leg_stop.stop_price is not None and lab_stop.stop_price is not None

        # Legacy stop is pinned to the tiny micro-wick (~0.03% below limit),
        # while Lab stop floor gives >= 1.0% breathing room!
        legacy_stop_dist = (leg_entry.limit_price - leg_stop.stop_price) / leg_entry.limit_price
        lab_stop_dist = (lab_entry.limit_price - lab_stop.stop_price) / lab_entry.limit_price
        self.assertLess(legacy_stop_dist, 0.002)
        self.assertGreaterEqual(lab_stop_dist, 0.009999)
        self.assertLess(lab_stop.stop_price, leg_stop.stop_price)

    def test_fill_pyramid_stop_tightening_and_state_restore(self) -> None:
        s15, s1h, sig_idx = self._build_fib_retest_fixture()
        spec = okx_swap_spec("MU-USDT-SWAP")
        fc = FeatureCache()
        mv_root = MarketView({("MU-USDT-SWAP", "15m"): s15, ("MU-USDT-SWAP", "1h"): s1h}, now_ms=s15.close_times[sig_idx], features=fc)

        cfg = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
        strat = FibonacciPullbackStrategy.from_legacy_config(cfg)

        # Signal bar -> emits second_pullback limit + initial stop
        out_sig = strat.on_bar(BarContext(s15.close_times[sig_idx], "MU-USDT-SWAP", mv_root, _portfolio("MU-USDT-SWAP"), spec))
        limit_px = out_sig.intents[0].limit_price
        assert limit_px is not None

        # Round-trip snapshot while pending entry is active
        snap1 = json.dumps(strat.snapshot_state())
        strat_copy = FibonacciPullbackStrategy.from_legacy_config(cfg)
        strat_copy.restore_state(json.loads(snap1))

        # Bar sig_idx + 1: entry fills!
        ts_fill = s15.close_times[sig_idx + 1]
        pos_fill = _portfolio("MU-USDT-SWAP", qty=100.0, avg_px=limit_px)
        ctx_fill = BarContext(ts_fill, "MU-USDT-SWAP", mv_root.at(ts_fill), pos_fill, spec)
        out_fill = strat.on_bar(ctx_fill)
        out_fill_copy = strat_copy.on_bar(ctx_fill)
        self.assertEqual(out_fill.intents, out_fill_copy.intents)
        self.assertEqual(1, len(out_fill.intents))
        self.assertEqual("stop", out_fill.intents[0].tag)

        # Bar sig_idx + 2: price rallied +3 -> emits stop + stage-2 pyramid add stop_market!
        ts_next = s15.close_times[sig_idx + 2]
        ctx_next = BarContext(ts_next, "MU-USDT-SWAP", mv_root.at(ts_next), pos_fill, spec)
        out_next = strat.on_bar(ctx_next)
        out_next_copy = strat_copy.on_bar(ctx_next)
        self.assertEqual(out_next.intents, out_next_copy.intents)
        tags = [i.tag for i in out_next.intents]
        self.assertIn("stop", tags)
        self.assertIn("fib_pyramid_stage_2", tags)

        # Snapshot/restore while holding open position preserves fills, stage, and stop state
        snap2 = json.dumps(strat.snapshot_state())
        strat_restored = FibonacciPullbackStrategy.from_legacy_config(cfg)
        strat_restored.restore_state(json.loads(snap2))
        self.assertEqual(strat.snapshot_state(), strat_restored.snapshot_state())

    def test_break_high_and_direct_next_open_modes(self) -> None:
        s15, s1h, sig_idx = self._build_fib_retest_fixture()
        spec = okx_swap_spec("MU-USDT-SWAP")
        fc = FeatureCache()
        mv = MarketView({("MU-USDT-SWAP", "15m"): s15, ("MU-USDT-SWAP", "1h"): s1h}, now_ms=s15.close_times[sig_idx], features=fc)
        ctx = BarContext(s15.close_times[sig_idx], "MU-USDT-SWAP", mv, _portfolio("MU-USDT-SWAP"), spec)

        bh_strat = FibonacciPullbackStrategy.from_legacy_config(
            StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="break_high", fib_lookback=32)
        )
        bh_out = bh_strat.on_bar(ctx)
        self.assertEqual(2, len(bh_out.intents))
        self.assertEqual("stop_market", bh_out.intents[0].order_type)
        self.assertGreater(bh_out.intents[0].stop_price, s15.highs[sig_idx])  # strict > signal.high

        dno_strat = FibonacciPullbackStrategy.from_legacy_config(
            StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="direct_next_open", fib_lookback=32)
        )
        dno_out = dno_strat.on_bar(ctx)
        self.assertEqual(2, len(dno_out.intents))
        self.assertEqual("market", dno_out.intents[0].order_type)
        self.assertEqual("initial_stop", dno_out.intents[1].tag)

    def test_session_window_toggle_for_24_7_crypto_perps(self) -> None:
        # Shift fixture to 20:00 ET (outside US cash window)
        s15, s1h, sig_idx = self._build_fib_retest_fixture()
        shift_ms = 10 * 3_600_000
        shifted_15m = [
            Candle(
                open_time_ms=int(s15.open_times[i]) + shift_ms,
                open=float(s15.opens[i]),
                high=float(s15.highs[i]),
                low=float(s15.lows[i]),
                close=float(s15.closes[i]),
                volume=float(s15.volumes[i]),
            )
            for i in range(len(s15))
        ]
        shifted_1h = [
            Candle(
                open_time_ms=int(s1h.open_times[i]) + shift_ms,
                open=float(s1h.opens[i]),
                high=float(s1h.highs[i]),
                low=float(s1h.lows[i]),
                close=float(s1h.closes[i]),
                volume=float(s1h.volumes[i]),
            )
            for i in range(len(s1h))
        ]
        s15_off = BarSeries.from_candles("BTC-USDT-SWAP", "15m", shifted_15m)
        s1h_off = BarSeries.from_candles("BTC-USDT-SWAP", "1h", shifted_1h)
        spec = okx_swap_spec("BTC-USDT-SWAP")
        mv = MarketView({("BTC-USDT-SWAP", "15m"): s15_off, ("BTC-USDT-SWAP", "1h"): s1h_off}, now_ms=s15_off.close_times[sig_idx])
        ctx = BarContext(s15_off.close_times[sig_idx], "BTC-USDT-SWAP", mv, _portfolio("BTC-USDT-SWAP"), spec)

        gated = FibonacciPullbackStrategy(FibonacciPullbackParams(symbol="BTC-USDT-SWAP", use_session_window=True))
        always_on = FibonacciPullbackStrategy(FibonacciPullbackParams(symbol="BTC-USDT-SWAP", use_session_window=False))
        self.assertEqual((), gated.on_bar(ctx).intents)
        self.assertEqual(2, len(always_on.on_bar(ctx).intents))


if __name__ == "__main__":
    unittest.main()
