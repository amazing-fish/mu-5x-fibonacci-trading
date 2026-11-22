"""Unit tests for Layer 2 Strategy protocol, multi-family registry, and FeatureCache."""

from __future__ import annotations

import math
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import MappingProxyType

from mu_strategy.core.instrument import okx_swap_spec
from mu_strategy.indicators import ema as legacy_ema
from mu_strategy.indicators import macd as legacy_macd
from mu_strategy.indicators import rsi as legacy_rsi
from mu_strategy.market_data.bar_series import BarSeries, MarketView
from mu_strategy.models import Candle
from mu_strategy.strategies.families import (
    DEFAULT_STRATEGY_FAMILY_REGISTRY,
    DonchianATRStrategy,
    FibonacciPullbackStrategy,
    MeanReversionStrategy,
    TSMomentumStrategy,
)
from mu_strategy.strategies.features import FeatureCache, FeatureSlice
from mu_strategy.strategies.protocol import (
    EMPTY_OUTPUT,
    BarContext,
    FastCursor,
    FastCursorMarket,
    OrderIntent,
    ParamSpace,
    ParamSpec,
    PortfolioView,
    PositionView,
    Strategy,
    StrategyFamilyDescriptor,
    StrategyFamilyRegistry,
    StrategyOutput,
    TargetPosition,
)


def _make_candles(
    closes: list[float],
    *,
    start_ms: int = 1_700_000_000_000,
    interval_ms: int = 3_600_000,
    spread: float = 1.0,
    volumes: list[float] | None = None,
) -> list[Candle]:
    candles: list[Candle] = []
    for i, c in enumerate(closes):
        open_ms = start_ms + i * interval_ms
        close_ms = open_ms + interval_ms
        prev_c = closes[i - 1] if i > 0 else c
        hi = max(prev_c, c) + spread
        lo = max(0.01, min(prev_c, c) - spread)
        vol = volumes[i] if volumes is not None else 100.0 + (i % 7) * 15.0
        candles.append(
            Candle(
                open_time_ms=open_ms,
                open=prev_c,
                high=hi,
                low=lo,
                close=c,
                volume=vol,
            )
        )
    return candles


class ProtocolContractsTests(unittest.TestCase):
    def test_position_view_sides_and_validation(self) -> None:
        long_pos = PositionView("MU-USDT-SWAP", 2.5, 100.0, 12.5, 262.5, 5.0, liquidation_price=81.0, stage=2)
        self.assertTrue(long_pos.is_long)
        self.assertFalse(long_pos.is_short)
        self.assertFalse(long_pos.is_flat)
        self.assertEqual("long", long_pos.side)

        short_pos = PositionView("BTC-USDT-SWAP", -0.5, 90_000.0, -50.0, 45_050.0, 2.0, liquidation_price=134_000.0)
        self.assertTrue(short_pos.is_short)
        self.assertEqual("short", short_pos.side)

        flat_pos = PositionView("MU-USDT-SWAP", -0.0, 0.0, -0.0, 0.0, 0.0)
        self.assertTrue(flat_pos.is_flat)
        self.assertEqual("flat", flat_pos.side)
        self.assertFalse(math.copysign(1.0, flat_pos.qty) < 0.0)
        self.assertFalse(math.copysign(1.0, flat_pos.unrealized_pnl) < 0.0)

        with self.assertRaises(FrozenInstanceError):
            long_pos.qty = 3.0  # type: ignore[misc]
        with self.assertRaises(ValueError):
            PositionView("", 1.0, 100.0, 0.0, 100.0, 1.0)
        with self.assertRaises(ValueError):
            PositionView("MU", math.nan, 100.0, 0.0, 100.0, 1.0)
        with self.assertRaises(ValueError):
            PositionView("MU", 1.0, 100.0, 0.0, -10.0, 1.0)
        with self.assertRaises(ValueError):
            PositionView("MU", 1.0, 100.0, 0.0, 100.0, 1.0, liquidation_price=0.0)
        with self.assertRaises(ValueError):
            PositionView("MU", 1.0, 100.0, 0.0, 100.0, 1.0, stage=-1)
        with self.assertRaises(ValueError):
            PositionView("MU", True, 100.0, 0.0, 100.0, 1.0)  # type: ignore[arg-type]

    def test_order_intent_strict_validation(self) -> None:
        mkt = OrderIntent("id1", "MU-USDT-SWAP", "buy", "market", "equity_fraction", 1.0, tag="entry")
        self.assertIsNone(mkt.limit_price)
        self.assertIsNone(mkt.stop_price)

        lim = OrderIntent("id2", "MU-USDT-SWAP", "buy", "limit", "contracts", 5.0, limit_price=99.5, expires_after_bars=8)
        self.assertEqual(99.5, lim.limit_price)

        stp = OrderIntent(
            "id3", "MU-USDT-SWAP", "sell", "stop_market", "close_all", 0.0, stop_price=97.0, reduce_only=True
        )
        self.assertTrue(stp.reduce_only)

        sl = OrderIntent(
            "id4", "MU-USDT-SWAP", "buy", "stop_limit", "target_weight", -1.5, limit_price=101.0, stop_price=100.5
        )
        self.assertEqual(-1.5, sl.qty_value)

        # Missing required prices
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "buy", "limit", "contracts", 1.0)
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "sell", "stop_market", "close_all", 0.0)
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "buy", "stop_limit", "contracts", 1.0, stop_price=100.0)
        # Disallowed prices on wrong order type
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "buy", "market", "contracts", 1.0, limit_price=100.0)
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "buy", "limit", "contracts", 1.0, limit_price=100.0, stop_price=99.0)
        # Invalid enums / quantities / expiration / non-finite
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "long", "market", "contracts", 1.0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "buy", "market", "contracts", 0.0)
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "buy", "market", "equity_fraction", -0.5)
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "buy", "market", "contracts", math.inf)
        with self.assertRaises(ValueError):
            OrderIntent("bad", "MU", "buy", "market", "contracts", 1.0, expires_after_bars=0)

    def test_target_position_portfolio_view_and_bar_context(self) -> None:
        long_tp = TargetPosition("BTC-USDT-SWAP", 1.5, stop_price=88_000.0, take_profit=96_000.0, reason="trend")
        short_tp = TargetPosition("BTC-USDT-SWAP", -1.5, stop_price=95_000.0, take_profit=85_000.0, reason="short")
        flat_tp = TargetPosition("BTC-USDT-SWAP", -0.0, reason="flat")
        self.assertEqual(1.5, long_tp.target_weight)
        self.assertEqual(-1.5, short_tp.target_weight)
        self.assertFalse(math.copysign(1.0, flat_tp.target_weight) < 0.0)

        # Invalid bracket ordering
        with self.assertRaises(ValueError):
            TargetPosition("BTC", 1.0, stop_price=105.0, take_profit=100.0)
        with self.assertRaises(ValueError):
            TargetPosition("BTC", -1.0, stop_price=95.0, take_profit=100.0)

        pos_map = {
            "BTC": PositionView("BTC", 1.0, 100.0, 5.0, 105.0, 1.0),
            "ETH": PositionView("ETH", -2.0, 50.0, -4.0, 104.0, 1.0),
        }
        pv = PortfolioView(
            equity=1000.0,
            cash=900.0,
            positions=pos_map,
            open_orders=(),
            peak_equity=1050.0,
            daily_start_equity=1010.0,
        )
        self.assertIsInstance(pv.positions, MappingProxyType)
        pos_map["SOL"] = PositionView("SOL", 1.0, 10.0, 0.0, 10.0, 1.0)
        self.assertIsNone(pv.position("SOL"))
        self.assertEqual(1.0, pv.position_qty("BTC"))
        self.assertEqual(-2.0, pv.position_qty("ETH"))
        self.assertEqual(0.0, pv.position_qty("UNKNOWN"))
        self.assertAlmostEqual(209.0, pv.gross_notional())
        self.assertAlmostEqual(1.0, pv.net_notional())

        out = StrategyOutput(targets=(long_tp,), diagnostics={"score": 1.2})
        self.assertIsInstance(out.diagnostics, MappingProxyType)
        self.assertIs(EMPTY_OUTPUT, StrategyOutput.empty())

        candles = _make_candles([100.0, 101.0, 102.0])
        series = BarSeries.from_candles("MU-USDT-SWAP", "1h", candles)
        market = MarketView({("MU-USDT-SWAP", "1h"): series}, now_ms=series.close_times[-1])
        spec = okx_swap_spec("MU-USDT-SWAP")
        ctx = BarContext(now_ms=series.close_times[-1], symbol="MU-USDT-SWAP", market=market, portfolio=pv, instrument=spec)
        self.assertEqual(series.close_times[-1], ctx.now_ms)
        with self.assertRaises(ValueError):
            BarContext(now_ms=-1, symbol="MU-USDT-SWAP", market=market, portfolio=pv, instrument=spec)

    def test_param_space_and_multi_family_registry_supports_buy_sell_long_short(self) -> None:
        space = ParamSpace(
            (
                ParamSpec("lookback", "int", 20, min_value=10, max_value=30, step=10),
                ParamSpec("threshold", "float", 0.5, min_value=0.0, max_value=1.0, step=0.5),
                ParamSpec("enabled", "bool", True),
                ParamSpec("side_mode", "categorical", "long_short", choices=("buy", "sell", "long_short")),
            )
        )
        self.assertEqual((10, 20, 30), space.by_name()["lookback"].grid_values())
        self.assertEqual((0.0, 0.5, 1.0), space.by_name()["threshold"].grid_values())
        self.assertEqual((False, True), space.by_name()["enabled"].grid_values())
        space.validate_params({"lookback": 20, "threshold": 0.5, "enabled": False, "side_mode": "sell"})
        with self.assertRaises(ValueError):
            space.validate_params({"lookback": 5})
        with self.assertRaises(ValueError):
            space.validate_params({"side_mode": "invalid"})

        # Default registry natively supports buy, sell, long_short with all 4 families.
        reg = DEFAULT_STRATEGY_FAMILY_REGISTRY
        self.assertEqual(4, len(reg))
        self.assertIn("ts_momentum", reg)
        self.assertIn("donchian_atr", reg)
        self.assertIn("mean_reversion", reg)
        self.assertIn("fibonacci_pullback", reg)
        self.assertIn("tsmom", reg)

        short_capable = {d.family_id for d in reg.list_families(side="sell")}
        self.assertEqual({"ts_momentum", "donchian_atr", "mean_reversion"}, short_capable)
        ls_capable = {d.family_id for d in reg.list_families(side="long_short")}
        self.assertEqual({"ts_momentum", "donchian_atr", "mean_reversion"}, ls_capable)
        buy_capable = {d.family_id for d in reg.list_families(side="buy")}
        self.assertEqual({"ts_momentum", "donchian_atr", "mean_reversion", "fibonacci_pullback"}, buy_capable)

        for fid in reg.family_ids():
            strat = reg.create(fid)
            self.assertIsInstance(strat, Strategy)

        # Custom short-only family registration succeeds (no side != "buy" restriction!).
        custom = StrategyFamilyRegistry()
        custom.register(
            StrategyFamilyDescriptor(
                family_id="short_only_demo",
                version="1.0.0",
                display_name="Short Only Demo",
                supported_sides=("sell",),
                default_intervals=("1h",),
                factory=lambda **kw: TSMomentumStrategy(side_mode="sell", **kw),
                param_space=TSMomentumStrategy().param_space(),
            )
        )
        self.assertEqual(1, len(custom.list_families(side="sell")))
        self.assertEqual(0, len(custom.list_families(side="buy")))

        # Verify documented FastCursor / FastCursorMarket structural protocols
        class _DummyCursor:
            bar_idx = 5
            pos_qty = 0.0

        class _DummyFastMarket:
            _acct = _DummyCursor()
            _p_sym = "MU-USDT-SWAP"
            _p_iv = "1h"

        self.assertIsInstance(_DummyCursor(), FastCursor)
        self.assertIsInstance(_DummyFastMarket(), FastCursorMarket)


class FeatureCacheTests(unittest.TestCase):
    def test_ema_rsi_macd_exact_match_with_legacy_indicators(self) -> None:
        closes = [100.0 + math.sin(i * 0.35) * 4.0 + i * 0.15 for i in range(120)]
        candles = _make_candles(closes)
        series = BarSeries.from_candles("MU-USDT-SWAP", "15m", candles)
        fc = FeatureCache()

        # EMA exact parity across all bars
        got_ema = fc.ema(series, 21)
        exp_ema = legacy_ema(closes, 21)
        self.assertEqual(len(closes), len(got_ema))
        for i, (g, e) in enumerate(zip(got_ema, exp_ema)):
            self.assertAlmostEqual(e, g, places=12, msg=f"EMA mismatch at {i}")

        # RSI: math.nan for warmup 0..13, exact match for >= 14
        got_rsi = fc.rsi(series, 14)
        exp_rsi = legacy_rsi(closes, 14)
        for i in range(14):
            self.assertTrue(math.isnan(got_rsi[i]), f"expected NaN warmup at {i}")
        for i in range(14, len(closes)):
            self.assertAlmostEqual(exp_rsi[i], got_rsi[i], places=12, msg=f"RSI mismatch at {i}")

        # Flat series RSI preserves _rsi_from_averages(0.0, 0.0) == 50.0!
        flat_candles = _make_candles([50.0] * 30, spread=0.0)
        flat_series = BarSeries.from_candles("FLAT", "15m", flat_candles)
        flat_rsi = fc.rsi(flat_series, 14)
        for i in range(14):
            self.assertTrue(math.isnan(flat_rsi[i]))
        for i in range(14, 30):
            self.assertEqual(50.0, flat_rsi[i])

        # MACD exact parity across all bars
        g_line, g_sig, g_hist = fc.macd(series, 12, 26, 9)
        e_line, e_sig, e_hist = legacy_macd(closes, 12, 26, 9)
        for i in range(len(closes)):
            self.assertAlmostEqual(e_line[i], g_line[i], places=12)
            self.assertAlmostEqual(e_sig[i], g_sig[i], places=12)
            self.assertAlmostEqual(e_hist[i], g_hist[i], places=12)

    def test_sma_atr_donchian_realized_vol_zscore_vwap_accuracy_and_stability(self) -> None:
        # Include large BTC-scale prices (~95,000 + small noise) to verify numerical stability!
        closes = [95_000.0 + math.sin(i * 0.4) * 25.0 + (i % 5) * 3.5 for i in range(80)]
        volumes = [0.0 if 30 <= i < 35 else 50.0 + i * 2.0 for i in range(80)]
        candles = _make_candles(closes, spread=8.0, volumes=volumes)
        series = BarSeries.from_candles("BTC-USDT-SWAP", "1h", candles)
        fc = FeatureCache()

        # SMA
        sma5 = fc.sma(series, 5)
        for i in range(4):
            self.assertTrue(math.isnan(sma5[i]))
        for i in range(4, len(closes)):
            expected = sum(closes[i - 4 : i + 1]) / 5.0
            self.assertAlmostEqual(expected, sma5[i], places=9)

        # ATR (Wilder)
        atr14 = fc.atr(series, 14)
        trs = [candles[0].high - candles[0].low]
        for i in range(1, len(candles)):
            pc = candles[i - 1].close
            trs.append(max(candles[i].high - candles[i].low, abs(candles[i].high - pc), abs(candles[i].low - pc)))
        for i in range(13):
            self.assertTrue(math.isnan(atr14[i]))
        ref_atr = sum(trs[:14]) / 14.0
        self.assertAlmostEqual(ref_atr, atr14[13], places=10)
        for i in range(14, len(candles)):
            ref_atr = (ref_atr * 13.0 + trs[i]) / 14.0
            self.assertAlmostEqual(ref_atr, atr14[i], places=10)

        # Donchian high & low
        dh10 = fc.donchian_high(series, 10)
        dl10 = fc.donchian_low(series, 10)
        for i in range(9):
            self.assertTrue(math.isnan(dh10[i]))
            self.assertTrue(math.isnan(dl10[i]))
        for i in range(9, len(candles)):
            self.assertAlmostEqual(max(c.high for c in candles[i - 9 : i + 1]), dh10[i], places=12)
            self.assertAlmostEqual(min(c.low for c in candles[i - 9 : i + 1]), dl10[i], places=12)

        # Realized vol (annualized sample std of log returns, inferred 8760 for 1h)
        rvol = fc.realized_vol(series, 20)
        for i in range(20):
            self.assertTrue(math.isnan(rvol[i]))
        log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        for i in range(20, len(closes)):
            win = log_rets[i - 20 : i]
            mu = sum(win) / 20.0
            std = math.sqrt(sum((x - mu) ** 2 for x in win) / 19.0)
            self.assertAlmostEqual(std * math.sqrt(8760.0), rvol[i], places=9)

        # Z-score (anchor-shifted high-precision test on ~95,000 prices)
        zs = fc.zscore(series, 20)
        for i in range(19):
            self.assertTrue(math.isnan(zs[i]))
        for i in range(19, len(closes)):
            win = closes[i - 19 : i + 1]
            mu = sum(win) / 20.0
            std = math.sqrt(sum((x - mu) ** 2 for x in win) / 19.0)
            expected_z = (closes[i] - mu) / std
            self.assertAlmostEqual(expected_z, zs[i], places=9)

        # Flat series zscore -> 0.0 (not NaN)
        flat = BarSeries.from_candles("FLAT", "1h", _make_candles([95_000.0] * 25, spread=0.0))
        flat_z = fc.zscore(flat, 20)
        self.assertEqual(0.0, flat_z[-1])

        # VWAP (including zero-volume window returning NaN cleanly)
        vwap5 = fc.vwap(series, 5)
        for i in range(4):
            self.assertTrue(math.isnan(vwap5[i]))
        self.assertTrue(math.isnan(vwap5[34]))  # bars 30..34 all have volume 0
        for i in (10, 29, 45, 79):
            win = candles[i - 4 : i + 1]
            num = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in win)
            den = sum(c.volume for c in win)
            self.assertAlmostEqual(num / den, vwap5[i], places=9)

    def test_strict_causality_read_only_and_cache_reuse(self) -> None:
        base_closes = [100.0 + i * 0.5 + math.sin(i) for i in range(60)]
        perturbed_closes = base_closes[:35] + [999.0 if i % 2 == 0 else 10.0 for i in range(35, 60)]

        s1 = BarSeries.from_candles("MU", "1h", _make_candles(base_closes))
        s2 = BarSeries.from_candles("MU", "1h", _make_candles(perturbed_closes))
        fc = FeatureCache()

        cutoff_ts = s1.close_times[34]
        mv1 = MarketView({("MU", "1h"): s1}, now_ms=cutoff_ts, features=fc)
        mv2 = MarketView({("MU", "1h"): s2}, now_ms=cutoff_ts, features=fc)

        sl1 = mv1.closed_bars("MU", "1h")
        sl2 = mv2.closed_bars("MU", "1h")
        self.assertEqual(35, len(sl1))

        # Every single indicator on [0..34] must be bit-for-bit identical despite wild future bars [35..59]!
        for name, fn in (
            ("ema", lambda sl: fc.ema(sl, 12)),
            ("sma", lambda sl: fc.sma(sl, 10)),
            ("rsi", lambda sl: fc.rsi(sl, 14)),
            ("atr", lambda sl: fc.atr(sl, 14)),
            ("dh", lambda sl: fc.donchian_high(sl, 10)),
            ("dl", lambda sl: fc.donchian_low(sl, 10)),
            ("rvol", lambda sl: fc.realized_vol(sl, 15)),
            ("zscore", lambda sl: fc.zscore(sl, 15)),
            ("vwap", lambda sl: fc.vwap(sl, 10)),
        ):
            f1 = fn(sl1)
            f2 = fn(sl2)
            self.assertIsInstance(f1, FeatureSlice)
            self.assertEqual(35, len(f1))
            # Cannot read past slice.end!
            with self.assertRaises(IndexError):
                _ = f1[35]
            with self.assertRaises(IndexError):
                _ = f1[-36]
            # Read-only: cannot mutate!
            with self.assertRaises(TypeError):
                f1[0] = 0.0  # type: ignore[index]
            for idx in range(35):
                v1, v2 = f1[idx], f2[idx]
                if math.isnan(v1):
                    self.assertTrue(math.isnan(v2), f"{name}[{idx}] NaN mismatch")
                else:
                    self.assertEqual(v1, v2, f"{name}[{idx}] look-ahead leak detected!")

        # Advancing MarketView.at(now_ms) reuses cached underlying arrays (zero recompute!).
        fc2 = FeatureCache()
        mv = MarketView({("MU", "1h"): s1}, now_ms=s1.close_times[20], features=fc2)
        for bar_idx in range(20, 60):
            view = mv.at(s1.close_times[bar_idx])
            bars = view.closed_bars("MU", "1h")
            val = fc2.ema_value(bars, 21, offset=-1)
            self.assertTrue(math.isfinite(val))
        stats = fc2.stats()
        self.assertEqual(1, stats["misses"])
        self.assertEqual(39, stats["hits"])

    def test_empty_single_bar_and_thread_safety(self) -> None:
        empty = BarSeries.from_candles("EMPTY", "1h", [])
        single = BarSeries.from_candles("ONE", "1h", _make_candles([100.0]))
        fc = FeatureCache()

        self.assertEqual(0, len(fc.ema(empty, 5)))
        self.assertTrue(math.isnan(fc.ema(empty, 5).latest))
        self.assertEqual(100.0, fc.ema(single, 5).latest)
        self.assertTrue(math.isnan(fc.sma(single, 5).latest))
        self.assertTrue(math.isnan(fc.rsi(single, 14).latest))
        self.assertTrue(math.isnan(fc.zscore(single, 5).latest))
        self.assertTrue(math.isnan(fc.realized_vol(single, 5).latest))

        # Thread-safe concurrent access on a shared FeatureCache
        series = BarSeries.from_candles("BTC", "1h", _make_candles([100.0 + i * 0.2 for i in range(300)]))

        def worker(offset: int) -> float:
            sl = series.slice_view(0, 150 + offset)
            _ = fc.ema(sl, 21).latest
            _ = fc.rsi(sl, 14).latest
            _ = fc.macd(sl)[2].latest
            _ = fc.atr(sl, 14).latest
            _ = fc.zscore(sl, 20).latest
            _ = fc.donchian_high(sl, 20).latest
            _ = fc.realized_vol(sl, 20).latest
            return fc.vwap(sl, 20).latest

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(16)))
        self.assertEqual(16, len(results))
        self.assertTrue(all(math.isfinite(val) for val in results))

        # Warm lookup speed sanity on 13,440 bars
        big_closes = [100.0 + math.sin(i * 0.01) * 10.0 for i in range(13_440)]
        big = BarSeries.from_candles("MU-USDT-SWAP", "15m", _make_candles(big_closes, interval_ms=900_000))
        fc_perf = FeatureCache()
        _ = fc_perf.ema(big, 21)
        t0 = time.perf_counter()
        for end in range(1, 13_441):
            _ = fc_perf.ema_value(big.slice_view(0, end), 21, offset=-1)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(elapsed_ms, 200.0, f"FeatureCache hot lookup unexpectedly slow: {elapsed_ms:.2f}ms")


if __name__ == "__main__":
    unittest.main()
