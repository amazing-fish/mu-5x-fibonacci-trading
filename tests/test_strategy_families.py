"""Unit and lifecycle tests for all 4 Layer 2 concrete strategy families."""

from __future__ import annotations

import json
import math
import unittest

from mu_strategy.core.instrument import CostModel, FixedTicks, okx_swap_spec
from mu_strategy.engine import Simulator, SimulatorConfig
from mu_strategy.market_data.bar_series import BarSeries, MarketView, MarketViewCursor
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
    donchian_atr_param_space,
    mean_reversion_param_space,
)
from mu_strategy.strategies.features import FeatureCache
from mu_strategy.strategies.protocol import (
    BarContext,
    PortfolioView,
    PositionView,
    SessionGate,
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
        mv_root = MarketViewCursor({("BTC-USDT-SWAP", "1h"): series}, features=fc)

        full_outputs = []
        split_outputs = []
        for idx in range(len(closes)):
            ts = series.close_times[idx]
            ctx = BarContext(ts, "BTC-USDT-SWAP", mv_root.view_at(ts), _portfolio("BTC-USDT-SWAP"), spec)
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

    def test_round1_cursor_hysteresis_base_switch_and_overflow_stop(self) -> None:
        # CR1-16: Fast cursor updates hysteresis state on every emitted target so equal closes preserve short.
        closes = [100.0, 101.0, 99.0, 99.0]
        series = BarSeries.from_candles("BTC-USDT-SWAP", "1h", _build_candles(closes, spread=0.2))
        spec = okx_swap_spec("BTC-USDT-SWAP")
        fc = FeatureCache()

        class _Acct:
            def __init__(self) -> None:
                self.bar_idx = 0
                self.pos_qty = 0.0

        class _FastMkt:
            def __init__(self, ser: BarSeries, acct: _Acct, sym: str) -> None:
                self._ser = ser
                self._acct = acct
                self._p_sym = sym
                self._p_iv = "1h"
                self._features = fc

            @property
            def now_ms(self) -> int:
                return int(self._ser.close_times[self._acct.bar_idx])

            def closed_bars(self, sym: str, iv: str):
                return self._ser.slice_view(0, self._acct.bar_idx + 1)

        class _FastCtx:
            def __init__(self, sym: str, mkt: _FastMkt, pf: PortfolioView) -> None:
                self.symbol = sym
                self.market = mkt
                self.portfolio = pf
                self.instrument = spec

            @property
            def now_ms(self) -> int:
                return self.market.now_ms

        params = TSMomentumParams(lookback_bars=1, deadband_return=0.0, use_deadband_hysteresis=True)
        strat = TSMomentumStrategy(params)
        acct = _Acct()
        fm = _FastMkt(series, acct, "BTC-USDT-SWAP")
        fctx = _FastCtx("BTC-USDT-SWAP", fm, _portfolio("BTC-USDT-SWAP"))

        outputs = []
        snap_after_short = None
        for idx in range(len(closes)):
            acct.bar_idx = idx
            outputs.append(strat.on_bar(fctx))  # type: ignore[arg-type]
            if idx == 2:
                snap_after_short = json.dumps(strat.snapshot_state())

        self.assertEqual(1.0, outputs[1].targets[0].target_weight)
        self.assertEqual(-1.0, outputs[2].targets[0].target_weight)
        self.assertEqual(-1.0, outputs[3].targets[0].target_weight)

        # Restored snapshot after bar 2 agrees on bar 3!
        assert snap_after_short is not None
        restored = TSMomentumStrategy(params)
        restored.restore_state(json.loads(snap_after_short))
        acct.bar_idx = 3
        self.assertEqual(outputs[3].targets, restored.on_bar(fctx).targets)  # type: ignore[arg-type]

        # CR1-17: Switching to an ordinary context for a different shorter series clears fast cursor binding.
        long_ser = BarSeries.from_candles("BTC-USDT-SWAP", "1h", _build_candles([100.0 + i for i in range(10)], spread=0.2))
        short_ser = BarSeries.from_candles("ETH-USDT-SWAP", "1h", _build_candles([200.0, 195.0, 190.0], spread=0.2))
        strat_sw = TSMomentumStrategy(TSMomentumParams(lookback_bars=1))
        acct_sw = _Acct()
        fm_sw = _FastMkt(long_ser, acct_sw, "BTC-USDT-SWAP")
        fctx_sw = _FastCtx("BTC-USDT-SWAP", fm_sw, _portfolio("BTC-USDT-SWAP"))
        acct_sw.bar_idx = 5
        self.assertEqual(1.0, strat_sw.on_bar(fctx_sw).targets[0].target_weight)  # type: ignore[arg-type]

        # Ordinary context on shorter ETH series replaces _cached_base.
        mv_eth = MarketView({("ETH-USDT-SWAP", "1h"): short_ser}, now_ms=short_ser.close_times[2], features=fc)
        ctx_eth = BarContext(short_ser.close_times[2], "ETH-USDT-SWAP", mv_eth, _portfolio("ETH-USDT-SWAP"), okx_swap_spec("ETH-USDT-SWAP"))
        self.assertEqual(-1.0, strat_sw.on_bar(ctx_eth).targets[0].target_weight)

        # Calling original BTC cursor at bar_idx=8 (> len(short_ser)!) must reload BTC series without IndexError!
        acct_sw.bar_idx = 8
        out_btc = strat_sw.on_bar(fctx_sw)  # type: ignore[arg-type]
        self.assertEqual("BTC-USDT-SWAP", out_btc.targets[0].symbol)
        self.assertEqual(1.0, out_btc.targets[0].target_weight)

        # CR1-61: Derived non-finite short stop rejects BEFORE updating _last_direction / _last_target_weight.
        dn_ser = BarSeries.from_candles("BTC-USDT-SWAP", "1h", _build_candles([120.0, 115.0, 110.0, 105.0, 100.0], spread=5.0))
        strat_ovf = TSMomentumStrategy(TSMomentumParams(lookback_bars=1, stop_atr_mult=1e308, atr_period=2))
        mv_ovf = MarketView({("BTC-USDT-SWAP", "1h"): dn_ser}, now_ms=dn_ser.close_times[-1], features=fc)
        ctx_ovf = BarContext(dn_ser.close_times[-1], "BTC-USDT-SWAP", mv_ovf, _portfolio("BTC-USDT-SWAP"), spec)
        before_snap = strat_ovf.snapshot_state()
        with self.assertRaises(ValueError):
            strat_ovf.on_bar(ctx_ovf)
        after_snap = strat_ovf.snapshot_state()
        self.assertEqual(before_snap["last_direction"], after_snap["last_direction"])
        self.assertEqual(before_snap["last_target_weight"], after_snap["last_target_weight"])


class DonchianATRStrategyTests(unittest.TestCase):
    def test_breakout_monotone_trailing_stop_flip_and_state_restore(self) -> None:
        # 12 flat range bars [99..101], then breakout rally up to 115, then sudden crash to 95
        # breaking BOTH exit_low and entry_low in one bar while the long is STILL OPEN (CR1-72)!
        closes = (
            [100.0, 100.5, 99.5, 100.2, 99.8, 100.1, 100.0, 99.7, 100.3, 99.9, 100.0, 100.2]
            + [104.0, 107.0, 111.0, 115.0, 114.5, 114.0]
            + [95.0, 90.0, 88.0, 89.0]
        )
        series = BarSeries.from_candles("ETH-USDT-SWAP", "1h", _build_candles(closes, spread=0.4))
        spec = okx_swap_spec("ETH-USDT-SWAP")
        fc = FeatureCache()
        mv_root = MarketViewCursor({("ETH-USDT-SWAP", "1h"): series}, features=fc)

        params = DonchianATRParams(entry_period=8, exit_period=4, atr_period=5, atr_stop_mult=2.0, side_mode="long_short")
        strat = DonchianATRStrategy(params)
        strat_clone = DonchianATRStrategy(params)
        strat_no_flip = DonchianATRStrategy(
            DonchianATRParams(entry_period=8, exit_period=4, atr_period=5, atr_stop_mult=2.0, side_mode="long_short", allow_flip=False)
        )
        self.assertIsInstance(strat, Strategy)

        # Simulate simple bar-by-bar portfolio feedback (fill next bar at close)
        pos_qty = 0.0
        avg_px = 0.0
        long_stops: list[float] = []
        saw_long = False
        saw_direct_flip_short = False

        for idx in range(len(closes)):
            ts = series.close_times[idx]
            ctx = BarContext(ts, "ETH-USDT-SWAP", mv_root.view_at(ts), _portfolio("ETH-USDT-SWAP", pos_qty, avg_px), spec)
            out = strat.on_bar(ctx)
            out_no_flip = strat_no_flip.on_bar(ctx)

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
                    self.assertIsNotNone(t.stop_price)
                    self.assertGreater(t.stop_price, closes[idx])
                    if pos_qty > 0:
                        # CR1-72: Direct reversal from open long to short; allow_flip=False yields flat on same bar!
                        self.assertEqual("donchian_flip_short", t.reason)
                        self.assertEqual(1, len(out_no_flip.targets))
                        self.assertEqual(0.0, out_no_flip.targets[0].target_weight)
                        saw_direct_flip_short = True
                    pos_qty = -1.0
                    avg_px = closes[idx]
                else:
                    pos_qty = 0.0
                    avg_px = 0.0

        self.assertTrue(saw_long)
        self.assertTrue(saw_direct_flip_short)
        self.assertGreaterEqual(len(long_stops), 2)
        # Verify long trailing stop is strictly non-decreasing (monotone ratchet!)
        for prev_s, next_s in zip(long_stops, long_stops[1:]):
            self.assertGreaterEqual(next_s, prev_s)

    def test_round1_donchian_cursor_switch_monotonic_flip_param_space_restore_and_overflow(self) -> None:
        fc = FeatureCache()
        spec = okx_swap_spec("ETH-USDT-SWAP")

        # CR1-65: ParamSpace enforces exit_period <= entry_period while keeping equal pairs like (10, 10).
        dspace = donchian_atr_param_space()
        dspace.validate_params({"entry_period": 10, "exit_period": 10})
        self.assertTrue(dspace.is_valid({"entry_period": 10, "exit_period": 10}))
        self.assertFalse(dspace.is_valid({"entry_period": 10, "exit_period": 15}))
        with self.assertRaises(ValueError):
            dspace.validate_params({"entry_period": 10, "exit_period": 15})

        # CR1-66: restore_state validates entire snapshot before mutating any field.
        strat_r = DonchianATRStrategy(DonchianATRParams(entry_period=5, exit_period=3, atr_period=3))
        strat_r.restore_state(
            {
                "schema_version": 1,
                "position_dir": 1,
                "trailing_stop": 105.0,
                "highest_close": 110.0,
                "lowest_close": None,
                "pending_initial_stop": None,
                "last_bar_ms": 1000,
            }
        )
        orig_snap = strat_r.snapshot_state()
        with self.assertRaises(ValueError):
            strat_r.restore_state({"schema_version": 1, "position_dir": -1, "trailing_stop": -1.0})
        self.assertEqual(orig_snap, strat_r.snapshot_state())

        # CR1-25: Deferred reduce after a flip preserves the open long's trailing stop monotonically!
        # High rally establishes a high trailing stop, then a crash emits donchian_flip_short,
        # then a rebound bar with STILL-OPEN long (`pos_qty > 0`) and wider ATR must NOT lower the trail!
        bars_mono = _build_candles(
            [100.0, 100.5, 99.5, 100.0, 100.2, 104.0, 108.0, 112.0, 115.0, 94.0, 106.0],
            spread=0.5,
        )
        # Make bar 10 have wide range so ATR jumps sharply while close is below highest_close=115.
        bars_mono[10] = Candle(
            open_time_ms=bars_mono[10].open_time_ms,
            open=105.0,
            high=120.0,
            low=95.0,
            close=108.0,
            volume=1000.0,
        )
        s_mono = BarSeries.from_candles("ETH-USDT-SWAP", "1h", bars_mono)
        cur_mono = MarketViewCursor({("ETH-USDT-SWAP", "1h"): s_mono}, features=fc)
        strat_m = DonchianATRStrategy(DonchianATRParams(entry_period=4, exit_period=2, atr_period=3, atr_stop_mult=1.5))

        # Feed bars 5..8 while holding long so trailing stop ratchets up.
        for i in range(5, 9):
            ts = s_mono.close_times[i]
            out_hold = strat_m.on_bar(BarContext(ts, "ETH-USDT-SWAP", cur_mono.view_at(ts), _portfolio("ETH-USDT-SWAP", 1.0, 104.0), spec))
        stop_before_flip = strat_m.snapshot_state()["trailing_stop"]
        assert stop_before_flip is not None

        # Bar 9 crashes to 94.0 -> emits donchian_flip_short, keeping prior trail intact!
        ts9 = s_mono.close_times[9]
        out_flip = strat_m.on_bar(BarContext(ts9, "ETH-USDT-SWAP", cur_mono.view_at(ts9), _portfolio("ETH-USDT-SWAP", 1.0, 104.0), spec))
        self.assertEqual("donchian_flip_short", out_flip.targets[0].reason)
        self.assertEqual(stop_before_flip, strat_m.snapshot_state()["trailing_stop"])

        # Bar 10: deferred reduce keeps long STILL OPEN (`pos_qty=1.0`); trailing stop MUST NOT drop below stop_before_flip!
        ts10 = s_mono.close_times[10]
        strat_m.on_bar(BarContext(ts10, "ETH-USDT-SWAP", cur_mono.view_at(ts10), _portfolio("ETH-USDT-SWAP", 1.0, 104.0), spec))
        self.assertGreaterEqual(strat_m.snapshot_state()["trailing_stop"], stop_before_flip)

        # CR1-24: Clearing fast binding when another series replaces _cached_base prevents stale array reads / IndexError.
        class _Acct:
            def __init__(self) -> None:
                self.bar_idx = 0
                self.pos_qty = 0.0

        class _FastMkt:
            def __init__(self, ser: BarSeries, acct: _Acct, sym: str) -> None:
                self._ser = ser
                self._acct = acct
                self._p_sym = sym
                self._p_iv = "1h"
                self._features = fc

            @property
            def now_ms(self) -> int:
                return int(self._ser.close_times[self._acct.bar_idx])

            def closed_bars(self, sym: str, iv: str):
                return self._ser.slice_view(0, self._acct.bar_idx + 1)

        class _FastPf:
            def __init__(self, acct: _Acct) -> None:
                self._acct = acct

            def position_qty(self, sym: str) -> float:
                return self._acct.pos_qty

            def position(self, sym: str):
                return None

        class _FastCtx:
            def __init__(self, sym: str, mkt: _FastMkt, pf: _FastPf) -> None:
                self.symbol = sym
                self.market = mkt
                self.portfolio = pf
                self.instrument = spec

            @property
            def now_ms(self) -> int:
                return self.market.now_ms

        ser_a = BarSeries.from_candles("ETH-USDT-SWAP", "1h", _build_candles([100.0] * 10 + [105.0, 108.0], spread=0.5))
        ser_b = BarSeries.from_candles("SOL-USDT-SWAP", "1h", _build_candles([50.0, 50.0, 50.0, 50.0, 50.0, 45.0], spread=0.5))
        strat_fb = DonchianATRStrategy(DonchianATRParams(entry_period=3, exit_period=2, atr_period=3))
        acct_d = _Acct()
        fm_d = _FastMkt(ser_a, acct_d, "ETH-USDT-SWAP")
        fp_d = _FastPf(acct_d)
        fctx_d = _FastCtx("ETH-USDT-SWAP", fm_d, fp_d)
        acct_d.bar_idx = 10
        self.assertEqual(1.0, strat_fb.on_bar(fctx_d).targets[0].target_weight)  # type: ignore[arg-type]

        # Ordinary context on shorter SOL series replaces _cached_base.
        mv_b = MarketView({("SOL-USDT-SWAP", "1h"): ser_b}, now_ms=ser_b.close_times[-1], features=fc)
        ctx_b = BarContext(ser_b.close_times[-1], "SOL-USDT-SWAP", mv_b, _portfolio("SOL-USDT-SWAP"), okx_swap_spec("SOL-USDT-SWAP"))
        self.assertEqual(-1.0, strat_fb.on_bar(ctx_b).targets[0].target_weight)

        # Returning to original ETH cursor at bar_idx=11 (> len(ser_b)) must reload ETH cleanly without IndexError!
        acct_d.bar_idx = 11
        out_a = strat_fb.on_bar(fctx_d)  # type: ignore[arg-type]
        self.assertEqual("ETH-USDT-SWAP", out_a.targets[0].symbol)
        self.assertEqual(1.0, out_a.targets[0].target_weight)

        # CR1-67: Non-finite short stops (atr_stop_mult=1e308) are rejected on short entry, flip, and hold.
        strat_ovf = DonchianATRStrategy(DonchianATRParams(entry_period=3, exit_period=2, atr_period=3, atr_stop_mult=1e308))
        dn_ser = BarSeries.from_candles("ETH-USDT-SWAP", "1h", _build_candles([100.0, 100.0, 100.0, 100.0, 90.0], spread=2.0))
        mv_dn = MarketView({("ETH-USDT-SWAP", "1h"): dn_ser}, now_ms=dn_ser.close_times[-1], features=fc)
        # Short entry -> EMPTY_OUTPUT (no inf stop)
        self.assertEqual((), strat_ovf.on_bar(BarContext(dn_ser.close_times[-1], "ETH-USDT-SWAP", mv_dn, _portfolio("ETH-USDT-SWAP"), spec)).targets)
        # Long -> short flip -> falls through to safe flat exit (no inf stop)
        out_ovf_flip = strat_ovf.on_bar(BarContext(dn_ser.close_times[-1], "ETH-USDT-SWAP", mv_dn, _portfolio("ETH-USDT-SWAP", 1.0, 100.0), spec))
        self.assertEqual(0.0, out_ovf_flip.targets[0].target_weight)
        # Short hold -> EMPTY_OUTPUT and snapshot/restore remains valid!
        out_ovf_hold = strat_ovf.on_bar(BarContext(dn_ser.close_times[-1], "ETH-USDT-SWAP", mv_dn, _portfolio("ETH-USDT-SWAP", -1.0, 100.0), spec))
        self.assertEqual((), out_ovf_hold.targets)
        snap_ovf = strat_ovf.snapshot_state()
        self.assertIsNone(snap_ovf["trailing_stop"])
        DonchianATRStrategy(DonchianATRParams(entry_period=3, exit_period=2, atr_period=3, atr_stop_mult=1e308)).restore_state(snap_ovf)


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
        mv_root = MarketViewCursor({("SOL-USDT-SWAP", "1h"): series}, features=fc)

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
        ctx_os = BarContext(series.close_times[27], "SOL-USDT-SWAP", mv_root.view_at(series.close_times[27]), _portfolio("SOL-USDT-SWAP"), spec)
        out_os = strat.on_bar(ctx_os)
        self.assertEqual(1, len(out_os.targets))
        self.assertEqual(1.0, out_os.targets[0].target_weight)
        self.assertEqual("mr_entry_long", out_os.targets[0].reason)
        # Tight vol gate blocks the same shock bar
        self.assertEqual((), strat_gated.on_bar(ctx_os).targets)

        # Reversion bar (index 29, close=99.5) exits long to flat
        ctx_rev_l = BarContext(
            series.close_times[29], "SOL-USDT-SWAP", mv_root.view_at(series.close_times[29]), _portfolio("SOL-USDT-SWAP", qty=1.0, avg_px=86.0), spec
        )
        out_rev_l = strat.on_bar(ctx_rev_l)
        self.assertEqual(0.0, out_rev_l.targets[0].target_weight)
        self.assertEqual("mr_reverted_long", out_rev_l.targets[0].reason)

        # Overbought bar (index 48, close=117.0) triggers short
        ctx_ob = BarContext(series.close_times[48], "SOL-USDT-SWAP", mv_root.view_at(series.close_times[48]), _portfolio("SOL-USDT-SWAP"), spec)
        out_ob = strat.on_bar(ctx_ob)
        self.assertEqual(1, len(out_ob.targets))
        self.assertEqual(-1.0, out_ob.targets[0].target_weight)
        self.assertEqual("mr_entry_short", out_ob.targets[0].reason)

        # Snapshot/restore round-trip
        snap = json.dumps(strat.snapshot_state())
        restored = MeanReversionStrategy(params)
        restored.restore_state(json.loads(snap))
        ctx_rev_s = BarContext(
            series.close_times[50], "SOL-USDT-SWAP", mv_root.view_at(series.close_times[50]), _portfolio("SOL-USDT-SWAP", qty=-1.0, avg_px=117.0), spec
        )
        self.assertEqual(strat.on_bar(ctx_rev_s).targets, restored.on_bar(ctx_rev_s).targets)

    def test_round1_mean_reversion_param_space_atr_wait_overflow_and_atomic_restore(self) -> None:
        fc = FeatureCache()
        spec = okx_swap_spec("SOL-USDT-SWAP")

        # CR1-62: ParamSpace rejects entry_z == exit_z while keeping valid pairs such as (1.25, 1.0).
        mspace = mean_reversion_param_space()
        mspace.validate_params({"entry_z": 1.25, "exit_z": 1.0})
        self.assertTrue(mspace.is_valid({"entry_z": 1.25, "exit_z": 1.0}))
        self.assertFalse(mspace.is_valid({"entry_z": 1.0, "exit_z": 1.0}))
        with self.assertRaises(ValueError):
            mspace.validate_params({"entry_z": 1.0, "exit_z": 1.0})

        # CR1-23: Wait for usable ATR before entering when stop_atr_mult is configured; keep no-stop mode unchanged.
        short_ser = BarSeries.from_candles("SOL-USDT-SWAP", "1h", _build_candles([100.0, 100.0, 90.0], spread=0.5))
        mv_short = MarketView({("SOL-USDT-SWAP", "1h"): short_ser}, now_ms=short_ser.close_times[-1], features=fc)
        ctx_short = BarContext(short_ser.close_times[-1], "SOL-USDT-SWAP", mv_short, _portfolio("SOL-USDT-SWAP"), spec)

        strat_with_stop = MeanReversionStrategy(
            MeanReversionParams(
                rsi_period=2, zscore_period=3, entry_z=1.0, exit_z=0.25, max_ann_vol=None, atr_period=14, stop_atr_mult=3.0
            )
        )
        strat_no_stop = MeanReversionStrategy(
            MeanReversionParams(
                rsi_period=2, zscore_period=3, entry_z=1.0, exit_z=0.25, max_ann_vol=None, atr_period=14, stop_atr_mult=None
            )
        )
        self.assertEqual((), strat_with_stop.on_bar(ctx_short).targets)
        out_ns = strat_no_stop.on_bar(ctx_short)
        self.assertEqual(1.0, out_ns.targets[0].target_weight)
        self.assertIsNone(out_ns.targets[0].stop_price)

        # CR1-64: Overflowed short stop (stop_atr_mult=1e308) rejects entry instead of emitting inf or opening stop-less.
        ob_ser = BarSeries.from_candles("SOL-USDT-SWAP", "1h", _build_candles([100.0, 100.0, 100.0, 120.0], spread=2.0))
        mv_ob = MarketView({("SOL-USDT-SWAP", "1h"): ob_ser}, now_ms=ob_ser.close_times[-1], features=fc)
        ctx_ob = BarContext(ob_ser.close_times[-1], "SOL-USDT-SWAP", mv_ob, _portfolio("SOL-USDT-SWAP"), spec)
        strat_ovf = MeanReversionStrategy(
            MeanReversionParams(
                rsi_period=2, zscore_period=3, entry_z=1.0, exit_z=0.25, max_ann_vol=None, atr_period=2, stop_atr_mult=1e308
            )
        )
        self.assertEqual((), strat_ovf.on_bar(ctx_ob).targets)
        self.assertIsNone(strat_ovf.snapshot_state()["pending_stop"])

        # CR1-63: Failed restore_state preserves existing position state atomically.
        strat_r = MeanReversionStrategy()
        strat_r.restore_state(
            {
                "schema_version": 1,
                "position_dir": 1,
                "bars_in_position": 4,
                "entry_stop": 95.0,
                "pending_stop": None,
                "last_bar_ms": 2000,
            }
        )
        orig_snap = strat_r.snapshot_state()
        with self.assertRaises(ValueError):
            strat_r.restore_state(
                {
                    "schema_version": 1,
                    "position_dir": -1,
                    "bars_in_position": 1,
                    "entry_stop": 110.0,
                    "pending_stop": -1.0,
                    "last_bar_ms": 3000,
                }
            )
        self.assertEqual(orig_snap, strat_r.snapshot_state())

        # CR2-13: If feature refresh raises (e.g., _zscore_array fails), the binding is NOT marked current
        # and a retry with the same (base, features, generation) re-runs refresh cleanly.
        class _FlakyFeatureCache(FeatureCache):
            def __init__(self) -> None:
                super().__init__()
                self.z_calls = 0

            def _zscore_array(self, base, period: int):  # type: ignore[override]
                self.z_calls += 1
                if self.z_calls == 1:
                    raise RuntimeError("transient zscore failure")
                return super()._zscore_array(base, period)

        flaky_fc = _FlakyFeatureCache()
        mv_flaky = MarketView({("SOL-USDT-SWAP", "1h"): ob_ser}, now_ms=ob_ser.close_times[-1], features=flaky_fc)
        ctx_flaky = BarContext(ob_ser.close_times[-1], "SOL-USDT-SWAP", mv_flaky, _portfolio("SOL-USDT-SWAP"), spec)
        strat_flaky = MeanReversionStrategy(
            MeanReversionParams(
                rsi_period=2, zscore_period=3, entry_z=1.0, exit_z=0.25, max_ann_vol=None, stop_atr_mult=None
            )
        )
        with self.assertRaises(RuntimeError):
            strat_flaky.on_bar(ctx_flaky)
        # Same base, same cache, same generation -> MUST retry refresh (not read None/stale arrays)!
        out_retry = strat_flaky.on_bar(ctx_flaky)
        self.assertEqual(2, flaky_fc.z_calls)
        self.assertEqual(-1.0, out_retry.targets[0].target_weight)


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

        # CR1-27: End-to-end Simulator test on a fillable bar whose low touches BOTH the entry limit
        # and the legacy micro-wick stop while staying above the Lab stop floor -> ONLY Lab survives!
        fib_limit = leg_entry.limit_price
        bar_ms = 900_000
        e2e_bars = [
            Candle(
                open_time_ms=int(s15.open_times[i]),
                open=float(s15.opens[i]),
                high=float(s15.highs[i]),
                low=float(s15.lows[i]),
                close=float(s15.closes[i]),
                volume=float(s15.volumes[i]),
            )
            for i in range(sig_idx + 1)
        ]
        # Bar sig_idx + 1 (10:15 ET, inside US cash window): fills limit AND stops out legacy (low < leg_stop)
        # while staying safely above lab_stop!
        fill_open_ms = int(s15.open_times[sig_idx]) + bar_ms
        e2e_bars.append(
            Candle(
                open_time_ms=fill_open_ms,
                open=fib_limit + 0.40,
                high=fib_limit + 1.00,
                low=leg_stop.stop_price - 0.05,  # below legacy stop, well above Lab stop!
                close=fib_limit + 0.80,
                volume=500.0,
            )
        )
        self.assertGreater(e2e_bars[-1].low, lab_stop.stop_price)
        s15_e2e = BarSeries.from_candles("MU-USDT-SWAP", "15m", e2e_bars)

        sim = Simulator(
            spec,
            cost_model=CostModel(
                maker_fee_bps=0.0,
                taker_fee_bps=0.0,
                half_spread_bps=0.0,
                slippage_model=FixedTicks(0.0),
                stop_slippage_multiplier=1.0,
            ),
            config=SimulatorConfig(
                primary_interval="15m",
                close_on_end=True,
                allow_missing_funding_for_perp=True,
            ),
        )
        res_legacy = sim.run(
            FibonacciPullbackStrategy.from_legacy_config(legacy_cfg),
            {("MU-USDT-SWAP", "15m"): s15_e2e, ("MU-USDT-SWAP", "1h"): s1h},
        )
        res_lab = sim.run(
            FibonacciPullbackStrategy(
                FibonacciPullbackParams(
                    symbol="MU-USDT-SWAP",
                    entry_execution="second_pullback",
                    fib_lookback=32,
                    min_stop_pct=0.01,
                    atr_stop_multiplier=1.5,
                    use_session_window=True,
                )
            ),
            {("MU-USDT-SWAP", "15m"): s15_e2e, ("MU-USDT-SWAP", "1h"): s1h},
        )
        # Legacy filled and immediately stopped out on bar sig_idx + 1!
        self.assertEqual(1, len(res_legacy.trades))
        self.assertEqual("initial_stop", res_legacy.trades[0].reason)
        # Lab filled and SURVIVED the micro-wick (stayed open through bar sig_idx + 1 until end_of_data)!
        self.assertEqual(1, len(res_lab.trades))
        self.assertEqual("end_of_data", res_lab.trades[0].reason)

    def test_fill_pyramid_stop_tightening_and_state_restore(self) -> None:
        s15, s1h, sig_idx = self._build_fib_retest_fixture()
        spec = okx_swap_spec("MU-USDT-SWAP")
        fc = FeatureCache()
        mv_root = MarketViewCursor({("MU-USDT-SWAP", "15m"): s15, ("MU-USDT-SWAP", "1h"): s1h}, features=fc)

        cfg = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
        strat = FibonacciPullbackStrategy.from_legacy_config(cfg)

        # Signal bar -> emits second_pullback limit + initial stop
        out_sig = strat.on_bar(
            BarContext(s15.close_times[sig_idx], "MU-USDT-SWAP", mv_root.view_at(s15.close_times[sig_idx]), _portfolio("MU-USDT-SWAP"), spec)
        )
        limit_px = out_sig.intents[0].limit_price
        assert limit_px is not None

        # Round-trip snapshot while pending entry is active
        snap1 = json.dumps(strat.snapshot_state())
        strat_copy = FibonacciPullbackStrategy.from_legacy_config(cfg)
        strat_copy.restore_state(json.loads(snap1))

        # Bar sig_idx + 1: entry fills!
        ts_fill = s15.close_times[sig_idx + 1]
        pos_fill = _portfolio("MU-USDT-SWAP", qty=100.0, avg_px=limit_px)
        ctx_fill = BarContext(ts_fill, "MU-USDT-SWAP", mv_root.view_at(ts_fill), pos_fill, spec)
        out_fill = strat.on_bar(ctx_fill)
        out_fill_copy = strat_copy.on_bar(ctx_fill)
        self.assertEqual(out_fill.intents, out_fill_copy.intents)
        self.assertEqual(1, len(out_fill.intents))
        self.assertEqual("stop", out_fill.intents[0].tag)

        # Bar sig_idx + 2: price rallied +3 -> emits stop + stage-2 pyramid add stop_market!
        ts_next = s15.close_times[sig_idx + 2]
        ctx_next = BarContext(ts_next, "MU-USDT-SWAP", mv_root.view_at(ts_next), pos_fill, spec)
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

    def test_round1_fibonacci_margin_validation_missing_regime_cap_and_session_gate(self) -> None:
        # CR1-71 & CR2-27: Validate direct params guard AND effective legacy_config guard independently.
        with self.assertRaises(ValueError):
            FibonacciPullbackParams(margin_steps=(0.0, 0.2, 0.2, 0.4))
        with self.assertRaises(ValueError):
            FibonacciPullbackParams(margin_steps=())
        # Supply valid FibonacciPullbackParams with a separate invalid legacy_config to exercise effective-config validation directly.
        valid_params_bad_legacy = FibonacciPullbackParams(
            legacy_parity=True,
            legacy_config=StrategyConfig(margin_steps=(0.0, 0.2, 0.2, 0.4)),
        )
        with self.assertRaises(ValueError):
            FibonacciPullbackStrategy(valid_params_bad_legacy)

        # CR2-14: Every pyramid stage beyond stage 1 must have an add_threshold (len(add_thresholds) >= len(margin_steps) - 1).
        with self.assertRaises(ValueError):
            FibonacciPullbackParams(margin_steps=(0.2, 0.2, 0.2, 0.2, 0.2), add_thresholds=(0.02, 0.04, 0.06))
        with self.assertRaises(ValueError):
            FibonacciPullbackStrategy(
                FibonacciPullbackParams(
                    legacy_parity=True,
                    legacy_config=StrategyConfig(
                        margin_steps=(0.2, 0.2, 0.2, 0.2, 0.2),
                        add_thresholds=(0.02, 0.04, 0.06),
                    ),
                )
            )
        # Default 4-stage schedule and shorter schedules with sufficient thresholds remain valid.
        self.assertIsInstance(FibonacciPullbackStrategy(), FibonacciPullbackStrategy)
        self.assertIsInstance(
            FibonacciPullbackStrategy(FibonacciPullbackParams(margin_steps=(0.5, 0.5), add_thresholds=(0.02,))),
            FibonacciPullbackStrategy,
        )
        self.assertIsInstance(
            FibonacciPullbackStrategy(FibonacciPullbackParams(margin_steps=(1.0,), add_thresholds=())),
            FibonacciPullbackStrategy,
        )

        s15, s1h, sig_idx = self._build_fib_retest_fixture()
        spec = okx_swap_spec("MU-USDT-SWAP")
        fc = FeatureCache()

        # CR1-18: Missing / unclosed 1h regime blocks Lab entries & pyramid adds while keeping protective stops active,
        # and preserves explicit legacy_parity fallback to "yellow".
        mv_no_h1 = MarketView({("MU-USDT-SWAP", "15m"): s15}, now_ms=s15.close_times[sig_idx], features=fc)
        ctx_no_h1 = BarContext(s15.close_times[sig_idx], "MU-USDT-SWAP", mv_no_h1, _portfolio("MU-USDT-SWAP"), spec)

        lab_strat = FibonacciPullbackStrategy(
            FibonacciPullbackParams(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
        )
        legacy_strat = FibonacciPullbackStrategy.from_legacy_config(
            StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
        )
        # Lab blocks entry when 1h regime is unavailable; legacy falls back to "yellow" and emits entry!
        self.assertEqual((), lab_strat.on_bar(ctx_no_h1).intents)
        self.assertEqual(2, len(legacy_strat.on_bar(ctx_no_h1).intents))

        # Lab open position with missing 1h STILL emits protective reduce_only stop, but skips pyramid add!
        mv_open_no_h1 = MarketView({("MU-USDT-SWAP", "15m"): s15}, now_ms=s15.close_times[sig_idx + 2], features=fc)
        ctx_open_no_h1 = BarContext(
            s15.close_times[sig_idx + 2],
            "MU-USDT-SWAP",
            mv_open_no_h1,
            _portfolio("MU-USDT-SWAP", qty=100.0, avg_px=float(s15.closes[sig_idx]) - 0.5),
            spec,
        )
        lab_open_strat = FibonacciPullbackStrategy(
            FibonacciPullbackParams(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
        )
        lab_open_strat.restore_state(
            {
                "schema_version": 1,
                "last_qty": 100.0,
                "fills": [[int(s15.open_times[sig_idx + 1]), float(s15.closes[sig_idx]) - 0.5, 100.0]],
                "stop_price": 100.0,
                "entry_anchor": float(s15.closes[sig_idx]) - 0.5,
                "initial_stop_price": 100.0,
                "max_stage": 1,
                "stop_transition_fill_count": 0,
                "stop_transition_start": 0.0,
            }
        )
        out_open = lab_open_strat.on_bar(ctx_open_no_h1)
        self.assertEqual(1, len(out_open.intents))
        self.assertEqual("stop", out_open.intents[0].tag)
        self.assertTrue(out_open.intents[0].reduce_only)
        self.assertIsNone(out_open.diagnostics["regime"])

        # CR1-19: direct_next_open enforces max_entry_price via open_only limit intent (uncapped stays market).
        mv_full = MarketView({("MU-USDT-SWAP", "15m"): s15, ("MU-USDT-SWAP", "1h"): s1h}, now_ms=s15.close_times[sig_idx], features=fc)
        ctx_full = BarContext(s15.close_times[sig_idx], "MU-USDT-SWAP", mv_full, _portfolio("MU-USDT-SWAP"), spec)
        dno_capped = FibonacciPullbackStrategy.from_legacy_config(
            StrategyConfig(
                symbol="MU-USDT-SWAP",
                entry_execution="direct_next_open",
                fib_lookback=32,
                max_entry_above_signal_close_pct=0.005,
            )
        )
        out_cap = dno_capped.on_bar(ctx_full)
        self.assertEqual(2, len(out_cap.intents))
        cap_entry = out_cap.intents[0]
        self.assertEqual("limit", cap_entry.order_type)
        self.assertEqual("open_only", cap_entry.time_in_force)
        self.assertAlmostEqual(float(s15.closes[sig_idx]) * 1.005, cap_entry.limit_price, places=10)
        self.assertIsInstance(cap_entry.session_gate, SessionGate)
        self.assertIsNone(out_cap.intents[1].session_gate)

        # CR1-20: second_pullback attaches SessionGate for fill-time enforcement and keeps pending_entry alive
        # if a submitted bar falls outside the session window even when low <= fib_level!
        sp_strat = FibonacciPullbackStrategy.from_legacy_config(
            StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
        )
        out_sp = sp_strat.on_bar(ctx_full)
        self.assertEqual(2, len(out_sp.intents))
        self.assertIsInstance(out_sp.intents[0].session_gate, SessionGate)
        self.assertIsNone(out_sp.intents[1].session_gate)
        fib_px = out_sp.intents[0].limit_price
        assert fib_px is not None

        # Simulate a data gap pushing bar sig_idx + 1 to 20:00 ET (outside US cash window) with low <= fib_px,
        # followed by bar sig_idx + 2 back inside US cash window (next trading day 10:00 ET) touching fib_px:
        gap_bars = [
            Candle(
                open_time_ms=int(s15.open_times[i]),
                open=float(s15.opens[i]),
                high=float(s15.highs[i]),
                low=float(s15.lows[i]),
                close=float(s15.closes[i]),
                volume=float(s15.volumes[i]),
            )
            for i in range(sig_idx + 1)
        ]
        gap_open_ms = int(s15.open_times[sig_idx]) + 10 * 3_600_000  # 20:00 ET (outside window!)
        gap_bars.append(
            Candle(
                open_time_ms=gap_open_ms,
                open=fib_px + 0.5,
                high=fib_px + 0.6,
                low=fib_px - 0.5,  # dips below fib_px, but outside session window!
                close=fib_px + 0.2,
                volume=500.0,
            )
        )
        s15_gap = BarSeries.from_candles("MU-USDT-SWAP", "15m", gap_bars)
        mv_gap = MarketView({("MU-USDT-SWAP", "15m"): s15_gap, ("MU-USDT-SWAP", "1h"): s1h}, now_ms=s15_gap.close_times[-1], features=fc)
        # Since session_gate blocked the fill on the out-of-window bar (curr_qty == 0.0), pending_entry MUST stay alive!
        out_after_gap = sp_strat.on_bar(BarContext(s15_gap.close_times[-1], "MU-USDT-SWAP", mv_gap, _portfolio("MU-USDT-SWAP"), spec))
        self.assertIsNotNone(sp_strat.snapshot_state()["pending_entry"])
        self.assertEqual(2, len(out_after_gap.intents))
        self.assertEqual("fib_second_pullback_entry", out_after_gap.intents[0].tag)

        # End-to-end Simulator checks for CR1-19 (direct_next_open cap) & CR1-20 (fill-time SessionGate across gap).
        sim = Simulator(
            spec,
            cost_model=CostModel(
                maker_fee_bps=0.0,
                taker_fee_bps=0.0,
                half_spread_bps=0.0,
                slippage_model=FixedTicks(0.0),
                stop_slippage_multiplier=1.0,
            ),
            config=SimulatorConfig(
                primary_interval="15m",
                close_on_end=True,
                allow_missing_funding_for_perp=True,
            ),
        )

        # CR1-19 E2E: next bar opens ABOVE cap (even though low dips below cap!) -> MUST NOT fill!
        base_prefix = gap_bars[: sig_idx + 1]
        cap_px = float(s15.closes[sig_idx]) * 1.005
        bar_ms = 900_000
        dno_over_cap_bars = base_prefix + [
            Candle(
                open_time_ms=int(s15.open_times[sig_idx]) + bar_ms,  # 10:15 ET (in-window)
                open=cap_px + 0.50,  # above cap!
                high=cap_px + 1.00,
                low=cap_px - 0.50,  # dips below cap intrabar — open_only MUST still reject!
                close=cap_px + 0.20,
                volume=500.0,
            )
        ]
        dno_under_cap_bars = base_prefix + [
            Candle(
                open_time_ms=int(s15.open_times[sig_idx]) + bar_ms,
                open=cap_px - 0.10,  # at/below cap -> fills at open!
                high=cap_px + 1.00,
                low=cap_px - 0.20,
                close=cap_px + 0.80,
                volume=500.0,
            )
        ]
        dno_cfg = StrategyConfig(
            symbol="MU-USDT-SWAP",
            entry_execution="direct_next_open",
            fib_lookback=32,
            max_entry_above_signal_close_pct=0.005,
        )
        res_over = sim.run(
            FibonacciPullbackStrategy.from_legacy_config(dno_cfg),
            {("MU-USDT-SWAP", "15m"): BarSeries.from_candles("MU-USDT-SWAP", "15m", dno_over_cap_bars), ("MU-USDT-SWAP", "1h"): s1h},
        )
        res_under = sim.run(
            FibonacciPullbackStrategy.from_legacy_config(dno_cfg),
            {("MU-USDT-SWAP", "15m"): BarSeries.from_candles("MU-USDT-SWAP", "15m", dno_under_cap_bars), ("MU-USDT-SWAP", "1h"): s1h},
        )
        self.assertEqual(0, len(res_over.trades))
        self.assertEqual(1, len(res_under.trades))
        self.assertAlmostEqual(cap_px - 0.10, res_under.trades[0].entry_price, places=8)

        # CR1-20 E2E: Data gap moves bar sig_idx+1 to 12:00 ET (outside 11:30-14:30 window) and bar sig_idx+2 to
        # 14:30 ET (inside afternoon cash window) touching fib_px -> MUST skip 12:00 ET and fill at 14:30 ET!
        lunch_gap_open_ms = int(s15.open_times[sig_idx]) + 2 * 3_600_000  # 12:00 ET (outside window!)
        afternoon_open_ms = int(s15.open_times[sig_idx]) + int(4.5 * 3_600_000)  # 14:30 ET (inside window!)
        sp_gap_bars = base_prefix + [
            Candle(
                open_time_ms=lunch_gap_open_ms,
                open=fib_px + 0.5,
                high=fib_px + 0.6,
                low=fib_px - 0.5,  # touches fib_px, but 12:00 ET is outside window!
                close=fib_px + 0.2,
                volume=500.0,
            ),
            Candle(
                open_time_ms=afternoon_open_ms,
                open=fib_px + 0.3,
                high=fib_px + 1.0,
                low=fib_px - 0.01,  # touches fib_px inside 14:30 ET window and stays above stop!
                close=fib_px + 0.8,
                volume=500.0,
            ),
        ]
        res_sp_gap = sim.run(
            FibonacciPullbackStrategy.from_legacy_config(
                StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
            ),
            {("MU-USDT-SWAP", "15m"): BarSeries.from_candles("MU-USDT-SWAP", "15m", sp_gap_bars), ("MU-USDT-SWAP", "1h"): s1h},
        )
        self.assertEqual(1, len(res_sp_gap.trades))
        self.assertEqual(afternoon_open_ms, res_sp_gap.trades[0].entry_time_ms)

        # CR1-20 E2E for direct_next_open: gap pushes next bar to 12:00 ET (outside window) -> MUST NOT fill!
        dno_gap_bars = base_prefix + [
            Candle(
                open_time_ms=lunch_gap_open_ms,
                open=float(s15.closes[sig_idx]),
                high=float(s15.closes[sig_idx]) + 1.0,
                low=float(s15.closes[sig_idx]) - 0.1,
                close=float(s15.closes[sig_idx]) + 0.5,
                volume=500.0,
            )
        ]
        res_dno_gap = sim.run(
            FibonacciPullbackStrategy.from_legacy_config(
                StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="direct_next_open", fib_lookback=32)
            ),
            {("MU-USDT-SWAP", "15m"): BarSeries.from_candles("MU-USDT-SWAP", "15m", dno_gap_bars), ("MU-USDT-SWAP", "1h"): s1h},
        )
        self.assertEqual(0, len(res_dno_gap.trades))

    def test_round2_fibonacci_touch_through_and_reverse_fib_fill_open_resistance(self) -> None:
        s15, s1h, sig_idx = self._build_fib_retest_fixture()
        spec = okx_swap_spec("MU-USDT-SWAP")
        fc = FeatureCache()
        mv_sig = MarketView({("MU-USDT-SWAP", "15m"): s15, ("MU-USDT-SWAP", "1h"): s1h}, now_ms=s15.close_times[sig_idx], features=fc)

        # CR2-44: With require_touch_through=True, a bar whose low only equals fib_level does NOT clear pending_entry!
        cfg_sp = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=32)
        sp_strat = FibonacciPullbackStrategy.from_legacy_config(cfg_sp)
        out_sig = sp_strat.on_bar(
            BarContext(s15.close_times[sig_idx], "MU-USDT-SWAP", mv_sig, _portfolio("MU-USDT-SWAP"), spec, require_touch_through=True)
        )
        fib_px = out_sig.intents[0].limit_price
        assert fib_px is not None
        bar_ms = 900_000
        prefix = [
            Candle(
                open_time_ms=int(s15.open_times[i]),
                open=float(s15.opens[i]),
                high=float(s15.highs[i]),
                low=float(s15.lows[i]),
                close=float(s15.closes[i]),
                volume=float(s15.volumes[i]),
            )
            for i in range(sig_idx + 1)
        ]
        touch_exact_bar = Candle(
            open_time_ms=int(s15.open_times[sig_idx]) + bar_ms,
            open=fib_px + 0.50,
            high=fib_px + 0.80,
            low=fib_px,  # exact touch (not strict <) -> Simulator with require_touch_through=True does NOT fill!
            close=fib_px + 0.40,
            volume=500.0,
        )
        break_through_bar = Candle(
            open_time_ms=int(s15.open_times[sig_idx]) + 2 * bar_ms,
            open=fib_px + 0.30,
            high=fib_px + 1.00,
            low=fib_px - 0.01,  # strict < fib_px -> fills cleanly and stays above stop!
            close=fib_px + 0.80,
            volume=500.0,
        )
        s15_tt = BarSeries.from_candles("MU-USDT-SWAP", "15m", prefix + [touch_exact_bar, break_through_bar])
        mv_tt = MarketView({("MU-USDT-SWAP", "15m"): s15_tt, ("MU-USDT-SWAP", "1h"): s1h}, now_ms=s15_tt.close_times[sig_idx + 1], features=fc)
        out_tt = sp_strat.on_bar(
            BarContext(s15_tt.close_times[sig_idx + 1], "MU-USDT-SWAP", mv_tt, _portfolio("MU-USDT-SWAP"), spec, require_touch_through=True)
        )
        # Pending entry MUST survive the exact-touch bar and re-emit!
        self.assertIsNotNone(sp_strat.snapshot_state()["pending_entry"])
        self.assertEqual(2, len(out_tt.intents))
        self.assertEqual("fib_second_pullback_entry", out_tt.intents[0].tag)

        # End-to-end Simulator check with require_touch_through=True: skips exact-touch bar, fills on break-through bar!
        sim_tt = Simulator(
            spec,
            cost_model=CostModel(
                maker_fee_bps=0.0,
                taker_fee_bps=0.0,
                half_spread_bps=0.0,
                slippage_model=FixedTicks(0.0),
                stop_slippage_multiplier=1.0,
            ),
            config=SimulatorConfig(
                primary_interval="15m",
                close_on_end=True,
                require_touch_through=True,
                allow_missing_funding_for_perp=True,
            ),
        )
        res_tt = sim_tt.run(
            FibonacciPullbackStrategy.from_legacy_config(cfg_sp),
            {("MU-USDT-SWAP", "15m"): s15_tt, ("MU-USDT-SWAP", "1h"): s1h},
        )
        self.assertEqual(1, len(res_tt.trades))
        self.assertEqual(break_through_bar.open_time_ms, res_tt.trades[0].entry_time_ms)

        # CR2-15: direct_next_open evaluates reverse-Fib resistance against the ACTUAL next-bar open
        # (NOT curr_close), protecting BOTH capped open_only limit AND uncapped open_only market entries!
        # Build a 46-bar 15m series:
        # - Bars 0..19: prior downswing from high=122.0 (bar 1) down to low=100.0 (bar 19)
        #   => reverse_fib_lookback=64 has resistance at 100 + 22*0.382 = 108.404 (band [108.08, 108.73]).
        # - Bars 20..44: recent upswing from low=100.0 to high=111.0 (bar 42), fib_lookback=24.
        # - Bar 45 (Monday 10:00 ET): retests fib_382 (~106.798), closes at ~107.25 (OUTSIDE 108.404 resistance!).
        sig_open_ms = 1_767_625_200_000
        start_rev = sig_open_ms - 45 * bar_ms
        rev_bars: list[Candle] = []
        for i in range(46):
            open_ms = start_rev + i * bar_ms
            if i == 0:
                p, lo, hi = 120.0, 119.5, 120.5
            elif i == 1:
                p, lo, hi = 121.0, 120.5, 122.0  # prior swing HIGH = 122.0
            elif i < 19:
                p = 120.0 - (i - 1) * 1.1
                lo, hi = p - 0.3, p + 0.3
            elif i == 19:
                p, lo, hi = 100.5, 100.0, 101.0  # prior swing LOW = 100.0
            elif i < 42:
                p = 100.8 + (i - 19) * 0.25
                lo, hi = p - 0.15, p + 0.25
            elif i == 42:
                p, lo, hi = 106.6, 106.3, 111.0  # local swing high inside fib_lookback=24
            elif i < 45:
                p, lo, hi = 106.8, 106.6, 107.2
            else:
                # Signal bar 45: retest fib_382 of [100.0 .. 111.0] = 106.798, close at 107.25 (outside 108.404 band!)
                swing_low = min(b.low for b in rev_bars[21:45])
                swing_high = max(b.high for b in rev_bars[21:45])
                fib_382 = round(swing_high - (swing_high - swing_low) * 0.382, 8)
                p = fib_382 + 0.45
                lo = fib_382 - 0.02
                hi = fib_382 + 0.65
            rev_bars.append(Candle(open_time_ms=open_ms, open=p, high=hi, low=lo, close=p, volume=500.0))

        # Verify resistance center at 0.382 of [100..122] = 108.404
        res_level = 100.0 + (122.0 - 100.0) * 0.382
        rev_sig_close = rev_bars[45].close
        self.assertGreater(abs(rev_sig_close - res_level), rev_sig_close * 0.003)  # curr_close is OUTSIDE band!

        start_1h = start_rev - 30 * 3_600_000
        s1h_rev = BarSeries.from_candles(
            "MU-USDT-SWAP",
            "1h",
            _build_candles([95.0 + i * 0.5 for i in range(60)], start_ms=start_1h, interval_ms=3_600_000, spread=0.3),
        )
        sim_rev = Simulator(
            spec,
            cost_model=CostModel(
                maker_fee_bps=0.0,
                taker_fee_bps=0.0,
                half_spread_bps=0.0,
                slippage_model=FixedTicks(0.0),
                stop_slippage_multiplier=1.0,
            ),
            config=SimulatorConfig(
                primary_interval="15m",
                close_on_end=True,
                allow_missing_funding_for_perp=True,
            ),
        )

        # Next bar opening INSIDE resistance (108.404) vs OUTSIDE resistance (107.30) — both under cap 109.5!
        bar_open_in_res = Candle(
            open_time_ms=sig_open_ms + bar_ms,
            open=res_level,
            high=res_level + 1.0,
            low=res_level - 0.1,
            close=res_level + 0.5,
            volume=500.0,
        )
        bar_open_clean = Candle(
            open_time_ms=sig_open_ms + bar_ms,
            open=rev_sig_close + 0.05,
            high=rev_sig_close + 1.0,
            low=rev_sig_close - 0.1,
            close=rev_sig_close + 0.5,
            volume=500.0,
        )
        for cap_pct in (0.03, None):  # test BOTH capped limit AND uncapped market direct_next_open!
            cfg_rev = StrategyConfig(
                symbol="MU-USDT-SWAP",
                entry_execution="direct_next_open",
                fib_lookback=24,
                block_reverse_fib_resistance=True,
                reverse_fib_lookback=64,
                reverse_fib_tolerance_pct=0.003,
                max_entry_above_signal_close_pct=cap_pct,
            )
            s_in_res = BarSeries.from_candles("MU-USDT-SWAP", "15m", rev_bars + [bar_open_in_res])
            s_clean = BarSeries.from_candles("MU-USDT-SWAP", "15m", rev_bars + [bar_open_clean])
            # Unit check: signal bar emits intent carrying excluded_price_bands even though curr_close is outside resistance!
            mv_r = MarketView({("MU-USDT-SWAP", "15m"): s_in_res, ("MU-USDT-SWAP", "1h"): s1h_rev}, now_ms=s_in_res.close_times[45], features=fc)
            out_r = FibonacciPullbackStrategy.from_legacy_config(cfg_rev).on_bar(
                BarContext(s_in_res.close_times[45], "MU-USDT-SWAP", mv_r, _portfolio("MU-USDT-SWAP"), spec)
            )
            self.assertEqual(2, len(out_r.intents))
            self.assertEqual(3, len(out_r.intents[0].excluded_price_bands))
            # CR4-08: direct_next_open reverse-Fib bands evaluate fill-bar open (price_source="open").
            self.assertTrue(all(b.price_source == "open" for b in out_r.intents[0].excluded_price_bands))
            self.assertTrue(any(b.excludes(res_level) for b in out_r.intents[0].excluded_price_bands))

            # E2E Simulator check: next bar open inside resistance -> REJECTED (0 trades); clean open -> FILLS!
            res_blocked = sim_rev.run(
                FibonacciPullbackStrategy.from_legacy_config(cfg_rev),
                {("MU-USDT-SWAP", "15m"): s_in_res, ("MU-USDT-SWAP", "1h"): s1h_rev},
            )
            res_allowed = sim_rev.run(
                FibonacciPullbackStrategy.from_legacy_config(cfg_rev),
                {("MU-USDT-SWAP", "15m"): s_clean, ("MU-USDT-SWAP", "1h"): s1h_rev},
            )
            self.assertEqual(0, len(res_blocked.trades), f"resistance open should be rejected (cap={cap_pct})")
            self.assertEqual(1, len(res_allowed.trades), f"clean open should fill (cap={cap_pct})")
            self.assertAlmostEqual(bar_open_clean.open, res_allowed.trades[0].entry_price, places=8)

        # CR4-08: break_high reverse-Fib bands keep price_source="fill" so stop-trigger fill prices remain checked.
        cfg_bh_rev = StrategyConfig(
            symbol="MU-USDT-SWAP",
            entry_execution="break_high",
            fib_lookback=24,
            block_reverse_fib_resistance=True,
            reverse_fib_lookback=64,
            reverse_fib_tolerance_pct=0.003,
        )
        s_bh_rev = BarSeries.from_candles("MU-USDT-SWAP", "15m", rev_bars)
        mv_bh = MarketView({("MU-USDT-SWAP", "15m"): s_bh_rev, ("MU-USDT-SWAP", "1h"): s1h_rev}, now_ms=s_bh_rev.close_times[45], features=fc)
        out_bh_rev = FibonacciPullbackStrategy.from_legacy_config(cfg_bh_rev).on_bar(
            BarContext(s_bh_rev.close_times[45], "MU-USDT-SWAP", mv_bh, _portfolio("MU-USDT-SWAP"), spec)
        )
        self.assertEqual(2, len(out_bh_rev.intents))
        self.assertEqual(3, len(out_bh_rev.intents[0].excluded_price_bands))
        self.assertTrue(all(b.price_source == "fill" for b in out_bh_rev.intents[0].excluded_price_bands))

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
