"""Unit tests for Layer 2 Strategy protocol, multi-family registry, and FeatureCache."""

from __future__ import annotations

import math
import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import MappingProxyType
from unittest.mock import patch

from mu_strategy.core.instrument import okx_swap_spec
from mu_strategy.core.trading_calendar import LEGACY_CALENDAR
from mu_strategy.indicators import ema as legacy_ema
from mu_strategy.indicators import macd as legacy_macd
from mu_strategy.indicators import rsi as legacy_rsi
from mu_strategy.market_data.bar_series import BarSeries, MarketView, MarketViewCursor
from mu_strategy.models import Candle
from mu_strategy.strategies import features as features_mod
from mu_strategy.strategies.families import (
    DEFAULT_STRATEGY_FAMILY_REGISTRY,
    DonchianATRStrategy,
    FibonacciPullbackStrategy,
    MeanReversionStrategy,
    TSMomentumStrategy,
)
from mu_strategy.strategies.features import FeatureCache, FeatureSlice
from mu_strategy.strategies.protocol import (
    EMPTY_MAPPING,
    EMPTY_OUTPUT,
    MAX_PARAM_GRID_SIZE,
    BarContext,
    FastCursor,
    FastCursorMarket,
    OrderIntent,
    ParamSpace,
    ParamSpec,
    PortfolioView,
    PositionView,
    PriceExclusionBand,
    SessionGate,
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

    def test_round1_protocol_immutability_causality_param_grid_and_session_gate(self) -> None:
        # CR1-01: PortfolioView copies caller-supplied MappingProxyType (preserving EMPTY_MAPPING fast path).
        backing_pos = {"BTC": PositionView("BTC", 2.0, 100.0, 0.0, 200.0, 1.0)}
        pv = PortfolioView(
            equity=1000.0,
            cash=800.0,
            positions=MappingProxyType(backing_pos),
            open_orders=(),
            peak_equity=1000.0,
            daily_start_equity=1000.0,
        )
        backing_pos.clear()
        self.assertEqual(2.0, pv.position_qty("BTC"))

        pv_empty = PortfolioView(
            equity=1000.0,
            cash=1000.0,
            positions=EMPTY_MAPPING,
            open_orders=(),
            peak_equity=1000.0,
            daily_start_equity=1000.0,
        )
        self.assertIs(pv_empty.positions, EMPTY_MAPPING)

        # CR1-56: StrategyOutput copies caller-supplied MappingProxyType diagnostics.
        backing_diag = {"signal": 1.0}
        out = StrategyOutput(diagnostics=MappingProxyType(backing_diag))
        backing_diag["signal"] = -99.0
        self.assertEqual(1.0, out.diagnostics["signal"])
        self.assertIs(EMPTY_OUTPUT.diagnostics, EMPTY_MAPPING)

        # CR1-02: BarContext rejects a MarketView whose timestamp disagrees with ctx.now_ms.
        candles = _make_candles([100.0, 101.0, 102.0])
        series = BarSeries.from_candles("MU-USDT-SWAP", "1h", candles)
        spec = okx_swap_spec("MU-USDT-SWAP")
        mv_ahead = MarketView({("MU-USDT-SWAP", "1h"): series}, now_ms=series.close_times[1])
        with self.assertRaises(ValueError):
            BarContext(now_ms=series.close_times[0], symbol="MU-USDT-SWAP", market=mv_ahead, portfolio=pv, instrument=spec)
        ctx_ok = BarContext(now_ms=series.close_times[1], symbol="MU-USDT-SWAP", market=mv_ahead, portfolio=pv, instrument=spec)
        self.assertEqual(series.close_times[1], ctx_ok.now_ms)

        # CR1-55: ParamSpec validates every explicit choice against type and bounds.
        with self.assertRaises(ValueError):
            ParamSpec("k", "int", 1, choices=(1, 1.5))
        with self.assertRaises(ValueError):
            ParamSpec("k", "int", 2, min_value=2, max_value=10, choices=(1, 2))

        # CR1-58: ParamSpec rejects fractional bounds/steps for kind="int".
        with self.assertRaises(ValueError):
            ParamSpec("k", "int", 2, min_value=1, max_value=3, step=0.5)
        with self.assertRaises(ValueError):
            ParamSpec("k", "int", 2, min_value=1.5, max_value=3, step=1)
        self.assertEqual((10, 20, 30), ParamSpec("k", "int", 20, min_value=10, max_value=30, step=10).grid_values())

        # CR1-54: Float grid preserves sub-1e-12 bounds/steps without rounding outside declared range.
        tiny = ParamSpec("eps", "float", 1e-13, min_value=1e-13, max_value=3e-13, step=1e-13)
        tiny_grid = tiny.grid_values()
        self.assertEqual(3, len(tiny_grid))
        for val in tiny_grid:
            tiny.validate(val)
            self.assertGreaterEqual(val, 1e-13)
            self.assertLessEqual(val, 3e-13)

        # CR1-57: Float grid bounds generation before allocating (handles sys.float_info.max & oversized grids).
        huge = ParamSpec("huge", "float", 0.0, min_value=0.0, max_value=sys.float_info.max, step=1e308)
        huge_grid = huge.grid_values()
        self.assertEqual((0.0, 1e308), huge_grid)
        oversized = ParamSpec("too_many", "float", 0.0, min_value=0.0, max_value=float(MAX_PARAM_GRID_SIZE + 5), step=1.0)
        with self.assertRaises(ValueError):
            oversized.grid_values()

        # CR1-59 & SessionGate / open_only OrderIntent contract checks.
        self.assertTrue(TSMomentumStrategy.supports_ephemeral_context)
        self.assertTrue(MeanReversionStrategy.supports_ephemeral_context)
        self.assertTrue(DonchianATRStrategy.supports_ephemeral_context)
        self.assertFalse(getattr(FibonacciPullbackStrategy, "supports_ephemeral_context", False))

        gate = SessionGate(LEGACY_CALENDAR, (("09:45", "11:30"),))
        self.assertTrue(gate.is_allowed(1_767_625_200_000))  # 2026-01-05 10:00 ET
        self.assertFalse(gate.is_allowed(1_767_661_200_000))  # 2026-01-05 20:00 ET

        open_lim = OrderIntent(
            "ol1", "MU", "buy", "limit", "equity_fraction", 1.0, limit_price=105.0, time_in_force="open_only", session_gate=gate
        )
        self.assertEqual("open_only", open_lim.time_in_force)
        self.assertEqual(gate, open_lim.session_gate)
        with self.assertRaises(ValueError):
            OrderIntent("bad_tif", "MU", "buy", "stop_market", "contracts", 1.0, stop_price=100.0, time_in_force="open_only")
        with self.assertRaises(ValueError):
            OrderIntent(
                "bad_gate", "MU", "sell", "stop_market", "close_all", 0.0, stop_price=95.0, reduce_only=True, session_gate=gate
            )

        # CR2-38: SessionGate validates HH:MM format & bounds at construction (not lazily at fill time).
        with self.assertRaises(ValueError):
            SessionGate(LEGACY_CALENDAR, (("25:00", "26:00"),))
        with self.assertRaises(ValueError):
            SessionGate(LEGACY_CALENDAR, (("15:00", "14:00"),))
        with self.assertRaises(ValueError):
            SessionGate(LEGACY_CALENDAR, (("09:60", "10:00"),))
        with self.assertRaises(ValueError):
            SessionGate(LEGACY_CALENDAR, ("09:45",))  # type: ignore[arg-type]

        # CR2-36: StrategyOutput freezes nested mutable containers in diagnostics.
        nested_list = [1, 2]
        nested_dict = {"a": [10]}
        out_nested = StrategyOutput(diagnostics={"signals": nested_list, "meta": nested_dict})
        nested_list.append(999)
        nested_dict["a"].append(888)
        nested_dict["b"] = "mutated"
        self.assertEqual((1, 2), out_nested.diagnostics["signals"])
        self.assertEqual((10,), out_nested.diagnostics["meta"]["a"])
        self.assertNotIn("b", out_nested.diagnostics["meta"])

        # CR2-37: Float grid limit checks emitted point count floor(steps_exact) + 1.
        exact_limit = ParamSpec("g", "float", 0.0, min_value=0.0, max_value=99_999.5, step=1.0)
        grid_100k = exact_limit.grid_values()
        self.assertEqual(MAX_PARAM_GRID_SIZE, len(grid_100k))
        self.assertEqual(0.0, grid_100k[0])
        self.assertEqual(99_999.0, grid_100k[-1])
        over_by_one = ParamSpec("g_over", "float", 0.0, min_value=0.0, max_value=float(MAX_PARAM_GRID_SIZE), step=1.0)
        with self.assertRaises(ValueError):
            over_by_one.grid_values()

        # CR2-35: ParamSpace.iter_valid_grid applies constraints to the empty assignment {}.
        def _reject_empty(p: dict[str, object]) -> None:
            raise ValueError("empty rejected")

        empty_unconstrained = ParamSpace()
        self.assertTrue(empty_unconstrained.is_valid({}))
        self.assertEqual([{}], list(empty_unconstrained.iter_valid_grid()))

        empty_constrained = ParamSpace(constraints=(_reject_empty,))
        self.assertFalse(empty_constrained.is_valid({}))
        self.assertEqual([], list(empty_constrained.iter_valid_grid()))

        # CR2-15 & CR2-44 protocol contracts: PriceExclusionBand & BarContext.require_touch_through.
        band = PriceExclusionBand(center=101.0, tolerance_pct=0.003)
        self.assertTrue(band.excludes(101.0))
        self.assertTrue(band.excludes(101.2))
        self.assertFalse(band.excludes(100.0))
        with self.assertRaises(ValueError):
            PriceExclusionBand(center=0.0, tolerance_pct=0.003)
        with self.assertRaises(ValueError):
            PriceExclusionBand(center=100.0, tolerance_pct=1.0)
        entry_with_band = OrderIntent(
            "eb1", "MU", "buy", "market", "equity_fraction", 1.0, time_in_force="open_only", excluded_price_bands=(band,)
        )
        self.assertEqual((band,), entry_with_band.excluded_price_bands)
        with self.assertRaises(ValueError):
            OrderIntent(
                "bad_band", "MU", "sell", "stop_market", "close_all", 0.0, stop_price=95.0, reduce_only=True, excluded_price_bands=(band,)
            )
        ctx_tt = BarContext(
            now_ms=series.close_times[1],
            symbol="MU-USDT-SWAP",
            market=mv_ahead,
            portfolio=pv,
            instrument=spec,
            require_touch_through=True,
        )
        self.assertTrue(ctx_tt.require_touch_through)
        self.assertFalse(ctx_ok.require_touch_through)

        # CR3-13: Empty iterators for excluded_price_bands normalize to () BEFORE reduce_only rejection!
        ro_empty_iter = OrderIntent(
            "ro_iter_ok",
            "MU",
            "sell",
            "stop_market",
            "close_all",
            0.0,
            stop_price=95.0,
            reduce_only=True,
            excluded_price_bands=iter(()),  # type: ignore[arg-type]
        )
        self.assertEqual((), ro_empty_iter.excluded_price_bands)
        with self.assertRaises(ValueError):
            OrderIntent(
                "ro_iter_bad",
                "MU",
                "sell",
                "stop_market",
                "close_all",
                0.0,
                stop_price=95.0,
                reduce_only=True,
                excluded_price_bands=iter([band]),  # type: ignore[arg-type]
            )
        entry_iter_band = OrderIntent(
            "entry_iter_ok",
            "MU",
            "buy",
            "market",
            "equity_fraction",
            1.0,
            excluded_price_bands=iter([band]),  # type: ignore[arg-type]
        )
        self.assertEqual((band,), entry_iter_band.excluded_price_bands)

        # CR3-14: Cyclic diagnostics raise clear ValueError (never RecursionError); shared acyclic containers remain valid!
        cyclic_dict: dict[str, object] = {}
        cyclic_dict["self"] = cyclic_dict
        with self.assertRaises(ValueError):
            StrategyOutput(diagnostics=cyclic_dict)
        cyclic_list: list[object] = []
        cyclic_list.append(cyclic_list)
        with self.assertRaises(ValueError):
            StrategyOutput(diagnostics={"loop": cyclic_list})
        shared_list = [1, 2]
        shared_dict = {"x": 10}
        out_shared = StrategyOutput(
            diagnostics={"a": shared_list, "b": shared_list, "d1": shared_dict, "d2": shared_dict}
        )
        shared_list.append(99)
        shared_dict["x"] = -1
        self.assertEqual((1, 2), out_shared.diagnostics["a"])
        self.assertEqual((1, 2), out_shared.diagnostics["b"])
        self.assertEqual(10, out_shared.diagnostics["d1"]["x"])
        self.assertEqual(10, out_shared.diagnostics["d2"]["x"])


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

        # CR1-26: Compute perturbed-future prefix comparisons with TWO SEPARATE FeatureCaches
        # so a cache-key collision can never mask a look-ahead leak!
        fc_a = FeatureCache()
        fc_b = FeatureCache()
        cutoff_ts = s1.close_times[34]
        mv1 = MarketView({("MU", "1h"): s1}, now_ms=cutoff_ts, features=fc_a)
        mv2 = MarketView({("MU", "1h"): s2}, now_ms=cutoff_ts, features=fc_b)

        sl1 = mv1.closed_bars("MU", "1h")
        sl2 = mv2.closed_bars("MU", "1h")
        self.assertEqual(35, len(sl1))

        # Every single indicator on [0..34] must be bit-for-bit identical despite wild future bars [35..59]!
        indicator_specs = (
            ("ema", lambda fc, sl: fc.ema(sl, 12)),
            ("sma", lambda fc, sl: fc.sma(sl, 10)),
            ("rsi", lambda fc, sl: fc.rsi(sl, 14)),
            ("atr", lambda fc, sl: fc.atr(sl, 14)),
            ("dh", lambda fc, sl: fc.donchian_high(sl, 10)),
            ("dl", lambda fc, sl: fc.donchian_low(sl, 10)),
            ("rvol", lambda fc, sl: fc.realized_vol(sl, 15)),
            ("zscore", lambda fc, sl: fc.zscore(sl, 15)),
            ("vwap", lambda fc, sl: fc.vwap(sl, 10)),
        )
        for name, fn in indicator_specs:
            f1 = fn(fc_a, sl1)
            f2 = fn(fc_b, sl2)
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

        # CR1-26: With a SHARED cache, the two distinct series with identical (symbol, interval)
        # MUST NOT collide and MUST diverge after bar 34 where their inputs differ!
        fc_shared = FeatureCache()
        full_sl1 = s1.slice_view(0, len(s1))
        full_sl2 = s2.slice_view(0, len(s2))
        for name, fn in indicator_specs:
            g1 = fn(fc_shared, full_sl1)
            g2 = fn(fc_shared, full_sl2)
            self.assertTrue(
                any(g1[i] != g2[i] for i in range(35, len(s1))),
                f"shared cache collision: {name} identical after bar 34!",
            )

        # Advancing MarketViewCursor.view_at(now_ms) reuses cached underlying arrays (zero recompute!).
        fc2 = FeatureCache()
        cursor = MarketViewCursor({("MU", "1h"): s1}, features=fc2)
        for bar_idx in range(20, 60):
            view = cursor.view_at(s1.close_times[bar_idx])
            bars = view.closed_bars("MU", "1h")
            val = fc2.ema_value(bars, 21, offset=-1)
            self.assertTrue(math.isfinite(val))
        stats = fc2.stats()
        self.assertEqual(1, stats["misses"])
        self.assertEqual(39, stats["hits"])

    def test_round1_feature_numerical_stability_and_linear_constant_windows(self) -> None:
        fc = FeatureCache()

        # CR1-70: Dominant outgoing SMA value [1e16, 1, 1] with period=2 recovers exact 1.0.
        s_sma = BarSeries.from_candles("SMA_BIG", "1h", _make_candles([1e16, 1.0, 1.0], spread=0.0))
        sma2 = fc.sma(s_sma, 2)
        self.assertAlmostEqual(1.0, sma2[2], places=12)

        # CR1-68: VWAP stays defined when positive volume remains after a dominant outgoing bar.
        s_vwap = BarSeries.from_candles(
            "VWAP_BIG", "1h", _make_candles([100.0, 100.0, 100.0, 100.0], spread=0.0, volumes=[1e12, 1e-5, 0.0, 0.0])
        )
        vwap2 = fc.vwap(s_vwap, 2)
        self.assertAlmostEqual(100.0, vwap2[2], places=10)
        self.assertTrue(math.isnan(vwap2[3]))

        # CR1-69: Unseen extreme future closes [1e200, 1e-200] do not break closed-prefix realized_vol.
        ext_closes = [100.0, 101.0, 102.0, 1e200, 1e-200]
        ext_candles = [
            Candle(
                open_time_ms=1_700_000_000_000 + i * 3_600_000,
                open=c,
                high=c,
                low=c,
                close=c,
                volume=100.0,
            )
            for i, c in enumerate(ext_closes)
        ]
        s_ext = BarSeries.from_candles("EXT", "1h", ext_candles)
        prefix = s_ext.slice_view(0, 3)
        rvol_prefix = fc.realized_vol(prefix, 2)
        self.assertEqual(3, len(rvol_prefix))
        self.assertTrue(math.isfinite(rvol_prefix[2]))
        self.assertGreater(rvol_prefix[2], 0.0)
        rvol_full = fc.realized_vol(s_ext, 2)
        self.assertTrue(math.isfinite(rvol_full[4]))

        # CR1-21, CR1-22 & CR2-39: Constant series triggers ZERO exact-window rescans (deterministic O(N) proof,
        # no flaky wall-clock threshold in default test discovery).
        const_series = BarSeries.from_candles("CONST", "1h", _make_candles([100.0] * 4000, spread=0.0))
        with patch.object(
            features_mod,
            "_exact_window_stats_scaled",
            wraps=features_mod._exact_window_stats_scaled,
        ) as exact_spy:
            const_z = fc.zscore(const_series, 2000)
            const_v = fc.realized_vol(const_series, 2000)
        self.assertEqual(0.0, const_z[-1])
        self.assertEqual(0.0, const_v[-1])
        self.assertEqual(0, exact_spy.call_count)

        # CR2-12: Future extreme prices [1e308, 1.1e308] at scheduled resync do NOT break closed-prefix z-score.
        base_alt = [100.0 if i % 2 == 0 else 101.0 for i in range(256)]
        s_clean = BarSeries.from_candles(
            "Z_CLEAN",
            "1h",
            [Candle(1_700_000_000_000 + i * 3_600_000, c, c, c, c, 100.0) for i, c in enumerate(base_alt)],
        )
        s_future_extreme = BarSeries.from_candles(
            "Z_EXTREME",
            "1h",
            [
                Candle(1_700_000_000_000 + i * 3_600_000, c, c, c, c, 100.0)
                for i, c in enumerate(base_alt + [1e308, 1.1e308])
            ],
        )
        fc_clean = FeatureCache()
        fc_ext = FeatureCache()
        z_clean = fc_clean.zscore(s_clean.slice_view(0, 2), 2)
        z_ext_prefix = fc_ext.zscore(s_future_extreme.slice_view(0, 2), 2)
        self.assertTrue(math.isnan(z_clean[0]) and math.isnan(z_ext_prefix[0]))
        self.assertEqual(z_clean[1], z_ext_prefix[1])
        z_ext_full = fc_ext.zscore(s_future_extreme, 2)
        self.assertTrue(math.isfinite(z_ext_full[256]))
        self.assertAlmostEqual(1.0 / math.sqrt(2.0), z_ext_full[257], places=12)

        # CR3-12: Jump from tiny scale (1e-200, negative scale_exp) to huge close (1e200) resyncs BEFORE _ldexp overflow,
        # while representable small-scale rolling updates remain on the rolling path!
        s_scale_jump = BarSeries.from_candles(
            "Z_SCALE_JUMP",
            "1h",
            [
                Candle(1_700_000_000_000 + i * 3_600_000, c, c, c, c, 100.0)
                for i, c in enumerate([1e-200, 2e-200, 3e-200, 1e200, 2e200])
            ],
        )
        with patch.object(
            features_mod,
            "_exact_window_stats_scaled",
            wraps=features_mod._exact_window_stats_scaled,
        ) as scale_spy:
            z_jump = fc.zscore(s_scale_jump, 2)
        self.assertAlmostEqual(1.0 / math.sqrt(2.0), z_jump[1], places=12)
        self.assertAlmostEqual(1.0 / math.sqrt(2.0), z_jump[2], places=12)
        self.assertAlmostEqual(1.0 / math.sqrt(2.0), z_jump[3], places=12)
        self.assertAlmostEqual(1.0 / math.sqrt(2.0), z_jump[4], places=12)
        # Initial window (bar 1) + incompatible jump (bar 3) resynced; bars 2 & 4 stayed on representable rolling updates!
        self.assertEqual(2, scale_spy.call_count)

        # CR2-42: SMA remains finite when mean fits (1e308) even though raw sum overflows.
        s_sma_huge = BarSeries.from_candles(
            "SMA_HUGE",
            "1h",
            [Candle(1_700_000_000_000 + i * 3_600_000, 1e308, 1e308, 1e308, 1e308, 10.0) for i in range(3)],
        )
        sma_huge = fc.sma(s_sma_huge, 2)
        self.assertTrue(math.isfinite(sma_huge[1]))
        self.assertAlmostEqual(1e308, sma_huge[1], delta=1e296)

        # CR2-43: Finite zero-volume extreme-price bar gets zero weight before typical price is summed.
        s_vwap_zv = BarSeries.from_candles(
            "VWAP_ZV",
            "1h",
            [
                Candle(1_700_000_000_000, 1e308, 1e308, 1e308, 1e308, 0.0),
                Candle(1_700_003_600_000, 100.0, 100.0, 100.0, 100.0, 1.0),
                Candle(1_700_007_200_000, 1e308, 1e308, 1e308, 1e308, 0.0),
                Candle(1_700_010_800_000, 1e308, 1e308, 1e308, 1e308, 0.0),
            ],
        )
        vwap_zv = fc.vwap(s_vwap_zv, 2)
        self.assertAlmostEqual(100.0, vwap_zv[1], places=12)
        self.assertAlmostEqual(100.0, vwap_zv[2], places=12)
        self.assertTrue(math.isnan(vwap_zv[3]))

        # Non-constant large-offset series still preserves exact precision fallback.
        big_osc = [1e9 + (0.01 if i % 2 == 0 else -0.01) for i in range(40)]
        s_osc = BarSeries.from_candles("OSC", "1h", _make_candles(big_osc, spread=0.0))
        z_osc = fc.zscore(s_osc, 20)
        self.assertTrue(math.isfinite(z_osc[-1]))
        self.assertAlmostEqual(-0.97467943448, z_osc[-1], places=6)

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

        # Warm lookup speed sanity on 13,440 bars (strict budget only under opt-in MU_STRICT_PERF=1)
        big_closes = [100.0 + math.sin(i * 0.01) * 10.0 for i in range(13_440)]
        big = BarSeries.from_candles("MU-USDT-SWAP", "15m", _make_candles(big_closes, interval_ms=900_000))
        fc_perf = FeatureCache()
        _ = fc_perf.ema(big, 21)
        t0 = time.perf_counter()
        for end in range(1, 13_441):
            _ = fc_perf.ema_value(big.slice_view(0, end), 21, offset=-1)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if os.environ.get("MU_STRICT_PERF") == "1":
            self.assertLess(elapsed_ms, 200.0, f"FeatureCache hot lookup unexpectedly slow: {elapsed_ms:.2f}ms")


if __name__ == "__main__":
    unittest.main()
