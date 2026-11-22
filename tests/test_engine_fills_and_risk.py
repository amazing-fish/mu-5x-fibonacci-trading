"""Comprehensive tests for Layer 3 ExecutionModel fills, MMR liquidation, and all 5 RiskOverlays."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from mu_strategy.core.instrument import (
    AtrBps,
    CostModel,
    FixedTicks,
    SqrtVolumeImpact,
    okx_swap_spec,
    spot_spec,
)
from mu_strategy.core.trading_calendar import US_EQUITIES_ALGORITHMIC_CALENDAR
from mu_strategy.engine.fills import (
    ExecutionModel,
    compute_liquidation_price,
    compute_position_liquidation_price,
)
from mu_strategy.engine.risk_overlay import (
    DailyLossLimitOverlay,
    DrawdownCircuitBreaker,
    ExposureCapOverlay,
    SessionWindowOverlay,
    VolTargetOverlay,
    apply_risk_overlays,
)
from mu_strategy.market_data.bar_series import BarSeries, MarketView
from mu_strategy.models import Candle
from mu_strategy.strategies.features import FeatureCache
from mu_strategy.strategies.protocol import (
    BarContext,
    OrderIntent,
    PortfolioView,
    PositionView,
    PriceExclusionBand,
    SessionGate,
    StrategyOutput,
    TargetPosition,
)


def _utc_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def _make_bar_series(closes: list[float], *, symbol: str = "MU-USDT-SWAP", interval: str = "1h", start_ms: int = 1_735_689_600_000) -> BarSeries:
    step = 3_600_000 if interval == "1h" else 900_000
    candles = [
        Candle(
            open_time_ms=start_ms + i * step,
            open=c,
            high=c * 1.005,
            low=c * 0.995,
            close=c,
            volume=1000.0,
        )
        for i, c in enumerate(closes)
    ]
    return BarSeries.from_candles(symbol, interval, candles)


def _make_ctx(
    *,
    now_ms: int,
    equity: float = 10_000.0,
    peak_equity: float = 10_000.0,
    daily_start_equity: float = 10_000.0,
    positions: dict[str, PositionView] | None = None,
    bars: BarSeries | None = None,
    feature_cache: FeatureCache | None = None,
    symbol: str = "MU-USDT-SWAP",
) -> BarContext:
    inst = okx_swap_spec(symbol, tick_size=Decimal("0.01"), max_leverage=5.0)
    if bars is None:
        bars = _make_bar_series([100.0] * 30, symbol=symbol, start_ms=now_ms - 30 * 3_600_000)
    market = MarketView({(symbol, bars.interval): bars}, now_ms=now_ms, features=feature_cache)
    portfolio = PortfolioView(
        equity=equity,
        cash=equity,
        positions=positions or {},
        open_orders=(),
        peak_equity=peak_equity,
        daily_start_equity=daily_start_equity,
    )
    return BarContext(now_ms=now_ms, symbol=symbol, market=market, portfolio=portfolio, instrument=inst)


class ExecutionModelFillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.05"), max_leverage=5.0)

    def test_market_order_applies_half_spread_fixed_ticks_and_taker_fee(self) -> None:
        cost = CostModel(
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            half_spread_bps=10.0,  # 0.10% = $0.10 on $100
            slippage_model=FixedTicks(2.0),  # 2 * 0.05 = $0.10
        )
        model = ExecutionModel(self.inst, cost)

        buy_q = model.fill_market(
            "buy", 10.0, bar_open=100.0, bar_high=102.0, bar_low=99.0, bar_close=101.0, bar_volume=1000.0
        )
        self.assertEqual("taker", buy_q.liquidity)
        self.assertAlmostEqual(100.0, buy_q.ref_price)
        self.assertAlmostEqual(100.20, buy_q.exec_price)
        self.assertAlmostEqual(2.0, buy_q.slippage_cost)  # (100.20 - 100.0) * 10
        self.assertAlmostEqual(100.20 * 10.0 * 0.0005, buy_q.fee)

        sell_q = model.fill_market(
            "sell", 10.0, bar_open=100.0, bar_high=102.0, bar_low=99.0, bar_close=101.0, bar_volume=1000.0
        )
        self.assertAlmostEqual(99.80, sell_q.exec_price)
        self.assertAlmostEqual(2.0, sell_q.slippage_cost)  # (99.80 - 100.0) * (-10) = +2.0
        self.assertAlmostEqual(99.80 * 10.0 * 0.0005, sell_q.fee)

    def test_limit_order_gap_improvement_touch_through_and_maker_fee(self) -> None:
        cost = CostModel(maker_fee_bps=2.0, taker_fee_bps=5.0, half_spread_bps=10.0, slippage_model=FixedTicks(3.0))
        normal = ExecutionModel(self.inst, cost, require_touch_through=False)
        strict = ExecutionModel(self.inst, cost, require_touch_through=True)

        # Exact touch: bar_low == limit_price
        q_norm = normal.try_fill_limit("buy", 5.0, 99.0, bar_open=100.0, bar_high=101.0, bar_low=99.0)
        q_strict = strict.try_fill_limit("buy", 5.0, 99.0, bar_open=100.0, bar_high=101.0, bar_low=99.0)
        self.assertIsNotNone(q_norm)
        assert q_norm is not None
        self.assertEqual("maker", q_norm.liquidity)
        self.assertAlmostEqual(99.0, q_norm.exec_price)
        self.assertAlmostEqual(0.0, q_norm.slippage_cost)
        self.assertAlmostEqual(99.0 * 5.0 * 0.0002, q_norm.fee)
        self.assertIsNone(q_strict)

        # Touch-through fills even in strict mode.
        q_strict_hit = strict.try_fill_limit("buy", 5.0, 99.0, bar_open=100.0, bar_high=101.0, bar_low=98.95)
        self.assertIsNotNone(q_strict_hit)
        assert q_strict_hit is not None
        self.assertAlmostEqual(99.0, q_strict_hit.exec_price)

        # Gap improvement: buy opens below limit -> fills at bar_open (even in strict mode).
        q_gap_buy = strict.try_fill_limit("buy", 5.0, 99.0, bar_open=97.5, bar_high=98.0, bar_low=97.0)
        self.assertIsNotNone(q_gap_buy)
        assert q_gap_buy is not None
        self.assertAlmostEqual(97.5, q_gap_buy.exec_price)

        # Gap improvement: sell opens above limit -> fills at bar_open.
        q_gap_sell = strict.try_fill_limit("sell", 5.0, 102.0, bar_open=104.0, bar_high=105.0, bar_low=103.0)
        self.assertIsNotNone(q_gap_sell)
        assert q_gap_sell is not None
        self.assertAlmostEqual(104.0, q_gap_sell.exec_price)

    def test_stop_market_and_stop_limit_use_stop_slippage_multiplier_and_gap_rules(self) -> None:
        cost = CostModel(
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            half_spread_bps=0.0,
            slippage_model=FixedTicks(2.0),  # base = $0.10
            stop_slippage_multiplier=2.5,  # stop = $0.25
        )
        model = ExecutionModel(self.inst, cost)

        # Sell stop triggered intrabar at 98 -> exec = 98 - 0.25 = 97.75
        q_sell_stop = model.try_fill_stop_market(
            "sell", 10.0, 98.0, bar_open=100.0, bar_high=100.5, bar_low=97.0, bar_close=98.5, bar_volume=1000.0
        )
        self.assertIsNotNone(q_sell_stop)
        assert q_sell_stop is not None
        self.assertAlmostEqual(98.0, q_sell_stop.ref_price)
        self.assertAlmostEqual(97.75, q_sell_stop.exec_price)
        self.assertAlmostEqual(2.5, q_sell_stop.slippage_cost)
        self.assertEqual("taker", q_sell_stop.liquidity)

        # Sell stop gapped below stop at open (open=95 < stop=98) -> worse of open/stop = 95, exec = 94.75
        q_gap = model.try_fill_stop_market(
            "sell", 10.0, 98.0, bar_open=95.0, bar_high=96.0, bar_low=94.0, bar_close=95.5, bar_volume=1000.0
        )
        assert q_gap is not None
        self.assertAlmostEqual(95.0, q_gap.ref_price)
        self.assertAlmostEqual(94.75, q_gap.exec_price)

        # Buy stop-limit: rejected when open gaps above limit_price
        q_sl_rej = model.try_fill_stop_limit(
            "buy", 10.0, stop_price=101.0, limit_price=102.0,
            bar_open=103.0, bar_high=104.0, bar_low=102.5, bar_close=103.5, bar_volume=1000.0,
        )
        self.assertIsNone(q_sl_rej)

        # Buy stop-limit: triggered and capped at limit_price
        q_sl_ok = model.try_fill_stop_limit(
            "buy", 10.0, stop_price=101.0, limit_price=101.15,
            bar_open=100.0, bar_high=102.0, bar_low=99.5, bar_close=101.5, bar_volume=1000.0,
        )
        assert q_sl_ok is not None
        self.assertAlmostEqual(101.0, q_sl_ok.ref_price)
        self.assertAlmostEqual(101.15, q_sl_ok.exec_price)  # capped by limit_price < 101.25

    def test_atr_bps_and_sqrt_volume_impact_amplify_in_thin_weekend_liquidity(self) -> None:
        atr_cost = CostModel(half_spread_bps=0.0, slippage_model=AtrBps(bps_per_atr=500.0))  # 5% of ATR
        atr_model = ExecutionModel(self.inst, atr_cost)
        q_atr = atr_model.fill_market(
            "buy", 10.0, bar_open=100.0, bar_high=104.0, bar_low=98.0, bar_close=100.0, bar_volume=100.0, bar_atr=4.0
        )
        self.assertAlmostEqual(100.20, q_atr.exec_price)  # 4.0 * 0.05 = 0.20

        vol_cost = CostModel(half_spread_bps=0.0, slippage_model=SqrtVolumeImpact(impact_coeff=50.0, max_bps=100.0))
        vol_model = ExecutionModel(self.inst, vol_cost)

        # Liquid weekday bar: close*volume = 100 * 100,000 = $10M USD, order = $10,000 -> participation = 0.001
        q_liquid = vol_model.fill_market(
            "buy", 100.0, bar_open=100.0, bar_high=101.0, bar_low=99.0, bar_close=100.0, bar_volume=100_000.0
        )
        # Thin weekend bar: close*volume = 100 * 1,000 = $100k USD (100x thinner!) -> participation = 0.1 -> 10x larger impact!
        q_thin = vol_model.fill_market(
            "buy", 100.0, bar_open=100.0, bar_high=101.0, bar_low=99.0, bar_close=100.0, bar_volume=1_000.0
        )
        self.assertGreater(q_thin.slippage_cost, q_liquid.slippage_cost * 9.0)
        self.assertAlmostEqual(q_thin.slippage_cost / q_liquid.slippage_cost, 10.0, places=6)

        # Zero-volume bar saturates conservatively at max_bps (100 bps = 1%)
        q_zero_vol = vol_model.fill_market(
            "buy", 100.0, bar_open=100.0, bar_high=101.0, bar_low=99.0, bar_close=100.0, bar_volume=0.0
        )
        self.assertAlmostEqual(101.0, q_zero_vol.exec_price)

    def test_conservative_intrabar_policy_always_fills_stop_loss_before_take_profit(self) -> None:
        model = ExecutionModel(self.inst, CostModel.zero_cost(), intrabar_policy="CONSERVATIVE")

        # Long: both SL=98 and TP=105 lie inside [97, 106] -> SL MUST fill first!
        q_long = model.evaluate_protective_exits(
            10.0,
            liq_price=80.0,
            stop_price=98.0,
            take_profit=105.0,
            bar_open=100.0,
            bar_high=106.0,
            bar_low=97.0,
            bar_close=105.5,
            bar_volume=1000.0,
        )
        assert q_long is not None
        self.assertEqual("stop_loss", q_long.reason)
        self.assertAlmostEqual(98.0, q_long.exec_price)

        # Short: both SL=102 and TP=95 lie inside [94, 103] -> SL MUST fill first!
        q_short = model.evaluate_protective_exits(
            -10.0,
            liq_price=120.0,
            stop_price=102.0,
            take_profit=95.0,
            bar_open=100.0,
            bar_high=103.0,
            bar_low=94.0,
            bar_close=94.5,
            bar_volume=1000.0,
        )
        assert q_short is not None
        self.assertEqual("stop_loss", q_short.reason)
        self.assertAlmostEqual(102.0, q_short.exec_price)

        # When SL is NOT touched, TP fills normally (maker).
        q_tp = model.evaluate_protective_exits(
            10.0,
            liq_price=80.0,
            stop_price=98.0,
            take_profit=105.0,
            bar_open=100.0,
            bar_high=106.0,
            bar_low=98.5,
            bar_close=105.5,
            bar_volume=1000.0,
        )
        assert q_tp is not None
        self.assertEqual("take_profit", q_tp.reason)
        self.assertEqual("maker", q_tp.liquidity)
        self.assertAlmostEqual(105.0, q_tp.exec_price)

    def test_long_and_short_mmr_liquidation_with_fees_funding_and_stop_priority(self) -> None:
        cost = CostModel(
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            half_spread_bps=0.0,
            slippage_model=FixedTicks(0.0),
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
        )
        # 5x long: margin=10_000, entry=100, qty=500 -> naive liq=80, with MMR+liq_fee=0.0105 -> 80/(1-0.0105)
        liq_long = compute_position_liquidation_price(
            side="long",
            entry_price=100.0,
            qty=500.0,
            margin_equity=10_000.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
        )
        assert liq_long is not None
        self.assertAlmostEqual(80.0 / (1.0 - 0.0105), liq_long, places=6)
        self.assertGreater(liq_long, 80.0)

        # Paying funding (-500) and entry fee (25) pushes long liquidation price higher (liquidates earlier!).
        liq_long_drained = compute_position_liquidation_price(
            side="long",
            entry_price=100.0,
            qty=500.0,
            margin_equity=10_000.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
            incurred_fees=25.0,
            accrued_funding=-500.0,
        )
        assert liq_long_drained is not None
        self.assertGreater(liq_long_drained, liq_long)

        # 5x short: margin=10_000, entry=100, qty=500 -> naive liq=120, with MMR+liq_fee=0.0105 -> 120/1.0105
        liq_short = compute_position_liquidation_price(
            side="short",
            entry_price=100.0,
            qty=500.0,
            margin_equity=10_000.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
        )
        assert liq_short is not None
        self.assertAlmostEqual(120.0 / 1.0105, liq_short, places=6)
        self.assertLess(liq_short, 120.0)

        # Unlevered spot long cannot be liquidated.
        self.assertIsNone(
            compute_liquidation_price(qty=50.0, avg_entry_price=100.0, cash=10_000.0, cost_model=cost, asset_class="spot")
        )

        # Stop-vs-liquidation priority for LONG:
        model = ExecutionModel(self.inst, cost)
        # Case A: continuous intrabar drop (open=95 -> low=75) with active stop at 90 > liq (~80.85) -> STOP wins!
        q_stop_first = model.evaluate_protective_exits(
            500.0,
            liq_price=liq_long,
            stop_price=90.0,
            take_profit=None,
            bar_open=95.0,
            bar_high=96.0,
            bar_low=75.0,
            bar_close=78.0,
            bar_volume=1000.0,
        )
        assert q_stop_first is not None
        self.assertEqual("stop_loss", q_stop_first.reason)
        self.assertAlmostEqual(90.0, q_stop_first.exec_price)

        # Case B: bar opens GAPPED BELOW liquidation (open=79 <= liq ~80.85) -> LIQUIDATION wins at 79!
        q_gap_liq = model.evaluate_protective_exits(
            500.0,
            liq_price=liq_long,
            stop_price=90.0,
            take_profit=None,
            bar_open=79.0,
            bar_high=82.0,
            bar_low=75.0,
            bar_close=80.0,
            bar_volume=1000.0,
        )
        assert q_gap_liq is not None
        self.assertEqual("liquidation", q_gap_liq.reason)
        self.assertEqual("liquidation", q_gap_liq.order_type)
        self.assertAlmostEqual(79.0, q_gap_liq.exec_price)
        self.assertAlmostEqual(79.0 * 500.0 * (cost.taker_fee_rate + cost.liquidation_fee_rate), q_gap_liq.fee)

        # Stop-vs-liquidation priority for SHORT:
        # Case C: continuous intrabar spike (open=105 -> high=125) with buy stop at 110 < liq (~118.75) -> STOP wins!
        q_short_stop = model.evaluate_protective_exits(
            -500.0,
            liq_price=liq_short,
            stop_price=110.0,
            take_profit=None,
            bar_open=105.0,
            bar_high=125.0,
            bar_low=104.0,
            bar_close=122.0,
            bar_volume=1000.0,
        )
        assert q_short_stop is not None
        self.assertEqual("stop_loss", q_short_stop.reason)
        self.assertAlmostEqual(110.0, q_short_stop.exec_price)

        # Case D: bar opens GAPPED ABOVE short liquidation (open=121 >= liq ~118.75) -> LIQUIDATION wins at 121!
        q_short_gap_liq = model.evaluate_protective_exits(
            -500.0,
            liq_price=liq_short,
            stop_price=110.0,
            take_profit=None,
            bar_open=121.0,
            bar_high=125.0,
            bar_low=120.0,
            bar_close=123.0,
            bar_volume=1000.0,
        )
        assert q_short_gap_liq is not None
        self.assertEqual("liquidation", q_short_gap_liq.reason)
        self.assertAlmostEqual(121.0, q_short_gap_liq.exec_price)


class RiskOverlayTests(unittest.TestCase):
    def test_vol_target_overlay_enforces_vol_floor_max_leverage_and_causality(self) -> None:
        # Flat zero-vol series -> realized_vol == 0.0 -> bounded by vol_floor & max_leverage!
        flat_bars = _make_bar_series([100.0] * 30, start_ms=_utc_ms(2026, 1, 1))
        now_ms = int(flat_bars.close_times[-1])
        ctx = _make_ctx(now_ms=now_ms, bars=flat_bars, feature_cache=FeatureCache())
        out = StrategyOutput(targets=(TargetPosition("MU-USDT-SWAP", 1.0),))

        # target_ann_vol=0.20, vol_floor=0.10 -> ratio=2.0 < max_leverage=3.0 -> weight=2.0
        ov_floor = VolTargetOverlay(target_ann_vol=0.20, vol_lookback=20, vol_floor=0.10, max_leverage=3.0)
        res_floor = ov_floor.filter(ctx, out)
        self.assertAlmostEqual(2.0, res_floor.targets[0].target_weight, places=6)

        # target_ann_vol=0.50, vol_floor=0.05 -> ratio=10.0 -> capped at max_leverage=3.0!
        ov_cap = VolTargetOverlay(target_ann_vol=0.50, vol_lookback=20, vol_floor=0.05, max_leverage=3.0)
        res_cap = ov_cap.filter(ctx, out)
        self.assertAlmostEqual(3.0, res_cap.targets[0].target_weight, places=6)

        # Causality check: huge future volatility spike AFTER now_ms must NOT change scale at now_ms!
        quiet_then_crash = [100.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(25)] + [100.0, 50.0, 140.0, 40.0, 160.0]
        bars_full = _make_bar_series(quiet_then_crash, start_ms=_utc_ms(2026, 1, 1))
        bars_prefix = _make_bar_series(quiet_then_crash[:25], start_ms=_utc_ms(2026, 1, 1))
        cutoff_ms = int(bars_prefix.close_times[-1])

        ov = VolTargetOverlay(target_ann_vol=0.20, vol_lookback=20, vol_floor=0.05, max_leverage=3.0)
        res_full = ov.filter(_make_ctx(now_ms=cutoff_ms, bars=bars_full, feature_cache=FeatureCache()), out)
        res_prefix = ov.filter(_make_ctx(now_ms=cutoff_ms, bars=bars_prefix, feature_cache=FeatureCache()), out)
        self.assertAlmostEqual(res_prefix.targets[0].target_weight, res_full.targets[0].target_weight, places=12)

    def test_drawdown_circuit_breaker_flatten_halt_cooldown_and_halve_size(self) -> None:
        sym = "MU-USDT-SWAP"
        open_pos = {sym: PositionView(sym, qty=10.0, avg_entry_price=100.0, unrealized_pnl=-1200.0, notional=880.0, leverage=1.0)}
        long_out = StrategyOutput(targets=(TargetPosition(sym, 1.0),))
        t0 = _utc_ms(2026, 6, 1, 12, 0)

        # 1. flatten_and_halt with cooldown_bars=2
        cb = DrawdownCircuitBreaker(max_drawdown_pct=0.10, action="flatten_and_halt", cooldown_bars=2)
        # Bar 0: peak=10_000 -> pass through
        r0 = cb.filter(_make_ctx(now_ms=t0, equity=10_000.0, peak_equity=10_000.0, positions=open_pos), long_out)
        self.assertAlmostEqual(1.0, r0.targets[0].target_weight)

        # Bar 1: equity=8_800 (-12% breach!) -> flattens open position!
        r1 = cb.filter(_make_ctx(now_ms=t0 + 3_600_000, equity=8_800.0, peak_equity=10_000.0, positions=open_pos), long_out)
        self.assertEqual(1, len(r1.targets))
        self.assertAlmostEqual(0.0, r1.targets[0].target_weight)
        self.assertTrue(cb.is_triggered)

        # Snapshot/restore mid-cooldown!
        snap = cb.snapshot_state()
        cb2 = DrawdownCircuitBreaker(max_drawdown_pct=0.10, action="flatten_and_halt", cooldown_bars=2)
        cb2.restore_state(snap)

        # Bars 2 & 3 (2 full cooldown bars): blocked when flat!
        r2 = cb2.filter(_make_ctx(now_ms=t0 + 2 * 3_600_000, equity=8_800.0, peak_equity=10_000.0), long_out)
        self.assertEqual((), r2.targets)
        r3 = cb2.filter(_make_ctx(now_ms=t0 + 3 * 3_600_000, equity=8_800.0, peak_equity=10_000.0), long_out)
        self.assertEqual((), r3.targets)

        # Bar 4: cooldown expired -> watermark resets to 8_800, trading resumes!
        r4 = cb2.filter(_make_ctx(now_ms=t0 + 4 * 3_600_000, equity=8_800.0, peak_equity=10_000.0), long_out)
        self.assertAlmostEqual(1.0, r4.targets[0].target_weight)
        self.assertFalse(cb2.is_triggered)

        # 2. halve_size mode
        cb_half = DrawdownCircuitBreaker(max_drawdown_pct=0.10, action="halve_size", cooldown_bars=None)
        cb_half.filter(_make_ctx(now_ms=t0, equity=10_000.0), long_out)
        rh = cb_half.filter(_make_ctx(now_ms=t0 + 3_600_000, equity=8_900.0), long_out)
        self.assertAlmostEqual(0.5, rh.targets[0].target_weight)
        # Recovers once equity makes a new high >= 10_000
        r_rec = cb_half.filter(_make_ctx(now_ms=t0 + 2 * 3_600_000, equity=10_100.0), long_out)
        self.assertAlmostEqual(1.0, r_rec.targets[0].target_weight)

    def test_daily_loss_limit_overlay_halts_same_day_and_resets_next_day(self) -> None:
        sym = "MU-USDT-SWAP"
        open_pos = {sym: PositionView(sym, qty=10.0, avg_entry_price=100.0, unrealized_pnl=-500.0, notional=950.0, leverage=1.0)}
        long_out = StrategyOutput(targets=(TargetPosition(sym, 1.0),))

        ov = DailyLossLimitOverlay(max_daily_loss_pct=0.04, reset_tz="UTC")
        day1_t0 = _utc_ms(2026, 6, 11, 10, 0)
        day1_t1 = _utc_ms(2026, 6, 11, 11, 0)
        day1_t2 = _utc_ms(2026, 6, 11, 14, 0)
        day2_t0 = _utc_ms(2026, 6, 12, 0, 0)

        # Start of day 1: equity 10_000 -> allowed
        r0 = ov.filter(_make_ctx(now_ms=day1_t0, equity=10_000.0), long_out)
        self.assertAlmostEqual(1.0, r0.targets[0].target_weight)

        # Breach (-5% > 4%) -> flattens open position!
        r1 = ov.filter(_make_ctx(now_ms=day1_t1, equity=9_500.0, positions=open_pos), long_out)
        self.assertAlmostEqual(0.0, r1.targets[0].target_weight)
        self.assertTrue(ov.is_halted)

        # Later same day -> still blocked!
        r2 = ov.filter(_make_ctx(now_ms=day1_t2, equity=9_500.0), long_out)
        self.assertEqual((), r2.targets)

        # Next UTC day -> resets cleanly and allows new entries!
        r3 = ov.filter(_make_ctx(now_ms=day2_t0, equity=9_500.0), long_out)
        self.assertAlmostEqual(1.0, r3.targets[0].target_weight)
        self.assertFalse(ov.is_halted)

    def test_exposure_cap_overlay_clamps_signed_weights_and_order_intents(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        ctx = _make_ctx(now_ms=t0, equity=10_000.0)
        ov = ExposureCapOverlay(max_gross_leverage=2.0, max_single_weight=1.5)

        # Single long > 1.5 -> clamped to 1.5; single short < -1.5 -> clamped to -1.5
        out_single = StrategyOutput(targets=(TargetPosition(sym, -2.5),))
        res_single = ov.filter(ctx, out_single)
        self.assertAlmostEqual(-1.5, res_single.targets[0].target_weight)

        # Multi-symbol gross sum (1.5 + 1.5 = 3.0 > max_gross=2.0) -> proportionally scaled to sum 2.0
        out_multi = StrategyOutput(
            targets=(TargetPosition("MU-USDT-SWAP", 1.5), TargetPosition("BTC-USDT-SWAP", -1.5))
        )
        res_multi = ov.filter(ctx, out_multi)
        self.assertAlmostEqual(1.0, res_multi.targets[0].target_weight)
        self.assertAlmostEqual(-1.0, res_multi.targets[1].target_weight)

        # OrderIntent equity_fraction clamped to single_cap=1.5; reduce_only untouched
        out_intents = StrategyOutput(
            intents=(
                OrderIntent("e1", sym, "buy", "market", "equity_fraction", 3.0),
                OrderIntent("s1", sym, "sell", "stop_market", "close_all", 0.0, stop_price=95.0, reduce_only=True),
            )
        )
        res_intents = ov.filter(ctx, out_intents)
        self.assertAlmostEqual(1.5, res_intents.intents[0].qty_value)
        self.assertEqual("s1", res_intents.intents[1].intent_id)

    def test_session_window_overlay_blocks_entries_outside_window_but_allows_exits(self) -> None:
        sym = "MU-USDT-SWAP"
        ov = SessionWindowOverlay(
            trading_windows_et=(("09:45", "11:30"), ("14:30", "15:45")),
            calendar_id=US_EQUITIES_ALGORITHMIC_CALENDAR,
        )
        # 2026-06-11 14:00 UTC = 10:00 ET (Thursday, inside window!)
        in_session_ms = _utc_ms(2026, 6, 11, 14, 0)
        # 2026-06-11 20:15 UTC = 16:15 ET (outside cash session!)
        out_session_ms = _utc_ms(2026, 6, 11, 20, 15)
        # 2026-06-13 14:00 UTC = Saturday (weekend closed!)
        weekend_ms = _utc_ms(2026, 6, 13, 14, 0)

        entry_intent = OrderIntent("e1", sym, "buy", "market", "equity_fraction", 1.0)
        stop_intent = OrderIntent("s1", sym, "sell", "stop_market", "close_all", 0.0, stop_price=95.0, reduce_only=True)
        out_entry = StrategyOutput(intents=(entry_intent, stop_intent), targets=(TargetPosition(sym, 1.0),))

        # Inside session: everything passes through.
        res_in = ov.filter(_make_ctx(now_ms=in_session_ms), out_entry)
        self.assertEqual(2, len(res_in.intents))
        self.assertEqual(1, len(res_in.targets))

        # Outside session (flat): entry intent & non-zero target blocked; reduce_only stop kept!
        res_out = ov.filter(_make_ctx(now_ms=out_session_ms), out_entry)
        self.assertEqual((stop_intent,), res_out.intents)
        self.assertEqual((), res_out.targets)

        # Weekend: entry blocked, explicit flat exit target ALLOWED!
        open_pos = {sym: PositionView(sym, qty=10.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=1000.0, leverage=0.1)}
        exit_out = StrategyOutput(targets=(TargetPosition(sym, 0.0, reason="exit"),))
        res_wknd_exit = ov.filter(_make_ctx(now_ms=weekend_ms, positions=open_pos), exit_out)
        self.assertEqual(1, len(res_wknd_exit.targets))
        self.assertAlmostEqual(0.0, res_wknd_exit.targets[0].target_weight)

        # Stacked overlays via apply_risk_overlays
        stacked = apply_risk_overlays(
            (ExposureCapOverlay(2.0, 1.5), ov),
            _make_ctx(now_ms=in_session_ms),
            StrategyOutput(targets=(TargetPosition(sym, 2.5),)),
        )
        self.assertAlmostEqual(1.5, stacked.targets[0].target_weight)

    def test_open_marketable_take_profit_fills_before_intrabar_stop_cr1_53(self) -> None:
        inst = okx_swap_spec("MU-USDT-SWAP", tick_size=Decimal("0.01"), max_leverage=5.0)
        cost = CostModel(maker_fee_bps=2.0, taker_fee_bps=5.0, half_spread_bps=0.0, slippage_model=FixedTicks(0))
        em = ExecutionModel(inst, cost)

        # Long TP=105, SL=98: bar opens at 107 (marketable TP!) then drops to 97 -> TP fills at 107!
        q_long = em.evaluate_protective_exits(
            10.0,
            liq_price=80.0,
            stop_price=98.0,
            take_profit=105.0,
            bar_open=107.0,
            bar_high=108.0,
            bar_low=97.0,
            bar_close=99.0,
            bar_volume=1000.0,
        )
        self.assertIsNotNone(q_long)
        assert q_long is not None
        self.assertEqual("take_profit", q_long.reason)
        self.assertEqual("limit", q_long.order_type)
        self.assertAlmostEqual(107.0, q_long.exec_price)

        # Short TP=95, SL=102: bar opens at 93 then spikes to 104 -> TP fills at 93!
        q_short = em.evaluate_protective_exits(
            -10.0,
            liq_price=120.0,
            stop_price=102.0,
            take_profit=95.0,
            bar_open=93.0,
            bar_high=104.0,
            bar_low=92.0,
            bar_close=101.0,
            bar_volume=1000.0,
        )
        self.assertIsNotNone(q_short)
        assert q_short is not None
        self.assertEqual("take_profit", q_short.reason)
        self.assertAlmostEqual(93.0, q_short.exec_price)

        # Open between SL=98 and TP=105 (ambiguous intrabar order) -> CONSERVATIVE SL-first preserved!
        q_ambig = em.evaluate_protective_exits(
            10.0,
            liq_price=80.0,
            stop_price=98.0,
            take_profit=105.0,
            bar_open=101.0,
            bar_high=107.0,
            bar_low=97.0,
            bar_close=103.0,
            bar_volume=1000.0,
        )
        self.assertIsNotNone(q_ambig)
        assert q_ambig is not None
        self.assertEqual("stop_loss", q_ambig.reason)
        self.assertAlmostEqual(98.0, q_ambig.exec_price)

    def test_exposure_cap_uses_contract_multiplier_in_both_conversions_cr1_29(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        inst = okx_swap_spec(sym, tick_size=Decimal("0.01"), contract_value=Decimal("10"), max_leverage=5.0)
        bars = _make_bar_series([100.0] * 10, symbol=sym, start_ms=t0 - 10 * 3_600_000)
        market = MarketView({(sym, "1h"): bars}, now_ms=t0)
        pf = PortfolioView(equity=10_000.0, cash=10_000.0, positions={}, open_orders=(), peak_equity=10_000.0, daily_start_equity=10_000.0)
        ctx = BarContext(now_ms=t0, symbol=sym, market=market, portfolio=pf, instrument=inst)

        ov = ExposureCapOverlay(max_gross_leverage=1.0, max_single_weight=1.0)
        # 100 contracts * contract_value=10 * price=100 = $100,000 (10x!) -> must clamp to 10 contracts ($10,000 = 1x).
        out = StrategyOutput(intents=(OrderIntent("c1", sym, "buy", "market", "contracts", 100.0),))
        res = ov.filter(ctx, out)
        self.assertEqual(1, len(res.intents))
        self.assertAlmostEqual(10.0, res.intents[0].qty_value, places=9)

    def test_daily_loss_limit_includes_opening_bar_loss_cr1_30(self) -> None:
        sym = "MU-USDT-SWAP"
        long_out = StrategyOutput(targets=(TargetPosition(sym, 1.0),))
        open_pos = {sym: PositionView(sym, qty=10.0, avg_entry_price=100.0, unrealized_pnl=-600.0, notional=9_400.0, leverage=1.0)}
        ov = DailyLossLimitOverlay(max_daily_loss_pct=0.05, reset_tz="UTC")

        # Day 0 closes at 10_000
        d0_end = _utc_ms(2026, 6, 11, 23, 0)
        r0 = ov.filter(_make_ctx(now_ms=d0_end, equity=10_000.0), long_out)
        self.assertAlmostEqual(1.0, r0.targets[0].target_weight)
        self.assertFalse(ov.is_halted)

        # Day 1 opening bar carries position from 10_000 -> 9_400 (-6% > 5% limit): MUST trigger immediately!
        d1_open = _utc_ms(2026, 6, 12, 1, 0)
        r1 = ov.filter(_make_ctx(now_ms=d1_open, equity=9_400.0, positions=open_pos), long_out)
        self.assertTrue(ov.is_halted)
        self.assertEqual(1, len(r1.targets))
        self.assertAlmostEqual(0.0, r1.targets[0].target_weight)

        # Day 2 opening bar flat at 9_400 -> cleanly resets with baseline 9_400 (0% loss)!
        d2_open = _utc_ms(2026, 6, 13, 1, 0)
        r2 = ov.filter(_make_ctx(now_ms=d2_open, equity=9_400.0), long_out)
        self.assertFalse(ov.is_halted)
        self.assertAlmostEqual(1.0, r2.targets[0].target_weight)

    def test_exposure_cap_does_not_release_headroom_before_reduce_fills_cr1_31(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        # Start already at 1.0x long cap
        pos = {sym: PositionView(sym, qty=100.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=10_000.0, leverage=1.0)}
        ctx = _make_ctx(now_ms=t0, equity=10_000.0, positions=pos)
        ov = ExposureCapOverlay(max_gross_leverage=1.0, max_single_weight=1.0)

        # Sell limit (might not fill!) followed by market buy 1.0x -> buy MUST be dropped!
        out = StrategyOutput(
            intents=(
                OrderIntent("s_lim", sym, "sell", "limit", "equity_fraction", 1.0, limit_price=110.0),
                OrderIntent("b_mkt", sym, "buy", "market", "equity_fraction", 1.0),
            )
        )
        res = ov.filter(ctx, out)
        self.assertEqual(1, len(res.intents))
        self.assertEqual("s_lim", res.intents[0].intent_id)

    def test_exposure_cap_shared_budget_across_targets_and_intents_cr1_32(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        ctx = _make_ctx(now_ms=t0, equity=10_000.0)
        ov = ExposureCapOverlay(max_gross_leverage=1.0, max_single_weight=1.0)

        # Flat account + 1x cap: 1x target + 1x buy intent -> intent dropped!
        out_full = StrategyOutput(
            targets=(TargetPosition(sym, 1.0),),
            intents=(OrderIntent("b1", sym, "buy", "market", "equity_fraction", 1.0),),
        )
        res_full = ov.filter(ctx, out_full)
        self.assertEqual(1, len(res_full.targets))
        self.assertAlmostEqual(1.0, res_full.targets[0].target_weight)
        self.assertEqual((), res_full.intents)

        # Partial budget: 0.6x target + 0.8x buy intent -> intent clamped to 0.4x!
        out_partial = StrategyOutput(
            targets=(TargetPosition(sym, 0.6),),
            intents=(OrderIntent("b2", sym, "buy", "market", "equity_fraction", 0.8),),
        )
        res_partial = ov.filter(ctx, out_partial)
        self.assertEqual(1, len(res_partial.intents))
        self.assertAlmostEqual(0.4, res_partial.intents[0].qty_value, places=9)

    def test_exposure_cap_resolves_4h_series_and_blocks_unpriceable_contracts_cr1_33(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 16, 0)
        inst = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        # 4h-only series (not in old fixed lookup list!)
        step_4h = 4 * 3_600_000
        c4h = [Candle(open_time_ms=t0 - (5 - i) * step_4h, open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0) for i in range(5)]
        s4h = BarSeries.from_candles(sym, "4h", c4h)
        mv_4h = MarketView({(sym, "4h"): s4h}, now_ms=t0)
        pf = PortfolioView(equity=10_000.0, cash=10_000.0, positions={}, open_orders=(), peak_equity=10_000.0, daily_start_equity=10_000.0)
        ctx_4h = BarContext(now_ms=t0, symbol=sym, market=mv_4h, portfolio=pf, instrument=inst)

        ov = ExposureCapOverlay(max_gross_leverage=1.0, max_single_weight=1.0)
        out = StrategyOutput(intents=(OrderIntent("c1", sym, "buy", "market", "contracts", 200.0),))
        res_4h = ov.filter(ctx_4h, out)
        self.assertEqual(1, len(res_4h.intents))
        self.assertAlmostEqual(100.0, res_4h.intents[0].qty_value, places=9)

        # Truly unpriceable symbol -> new exposure blocked, explicit reduce_only exit preserved!
        mv_other = MarketView({("OTHER", "4h"): BarSeries.from_candles("OTHER", "4h", c4h)}, now_ms=t0)
        ctx_unp = BarContext(now_ms=t0, symbol=sym, market=mv_other, portfolio=pf, instrument=inst)
        exit_ord = OrderIntent("ex", sym, "sell", "market", "close_all", 0.0, reduce_only=True)
        out_unp = StrategyOutput(
            intents=(OrderIntent("c2", sym, "buy", "market", "contracts", 200.0), exit_ord)
        )
        res_unp = ov.filter(ctx_unp, out_unp)
        self.assertEqual((exit_ord,), res_unp.intents)

    def test_session_window_prevents_outside_session_reduction_reversal_cr1_34(self) -> None:
        sym = "MU-USDT-SWAP"
        ov = SessionWindowOverlay(
            trading_windows_et=(("09:45", "11:30"),),
            calendar_id=US_EQUITIES_ALGORITHMIC_CALENDAR,
        )
        out_session_ms = _utc_ms(2026, 6, 11, 20, 15)  # 16:15 ET
        open_pos = {sym: PositionView(sym, qty=10.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=1000.0, leverage=0.1)}
        ctx = _make_ctx(now_ms=out_session_ms, positions=open_pos)

        # Oversized non-reduce_only sell of 20 on long 10 -> capped to 10 with reduce_only=True!
        out_rev = StrategyOutput(intents=(OrderIntent("s20", sym, "sell", "market", "contracts", 20.0),))
        res_rev = ov.filter(ctx, out_rev)
        self.assertEqual(1, len(res_rev.intents))
        self.assertTrue(res_rev.intents[0].reduce_only)
        self.assertAlmostEqual(10.0, res_rev.intents[0].qty_value, places=9)

        # Two non-reduce_only sells of 7 each -> capped to 7 + 3 = 10 total, both reduce_only=True!
        out_two = StrategyOutput(
            intents=(
                OrderIntent("s7a", sym, "sell", "market", "contracts", 7.0),
                OrderIntent("s7b", sym, "sell", "market", "contracts", 7.0),
            )
        )
        res_two = ov.filter(ctx, out_two)
        self.assertEqual(2, len(res_two.intents))
        self.assertAlmostEqual(7.0, res_two.intents[0].qty_value, places=9)
        self.assertAlmostEqual(3.0, res_two.intents[1].qty_value, places=9)
        self.assertTrue(all(it.reduce_only for it in res_two.intents))

        # Valid partial exit (sell 4) and close_all remain eligible!
        out_ok = StrategyOutput(
            intents=(
                OrderIntent("s4", sym, "sell", "market", "contracts", 4.0),
                OrderIntent("ca", sym, "sell", "market", "close_all", 0.0, reduce_only=True),
            )
        )
        res_ok = ov.filter(ctx, out_ok)
        self.assertEqual(2, len(res_ok.intents))
        self.assertAlmostEqual(4.0, res_ok.intents[0].qty_value, places=9)

    def test_vol_target_blocks_new_exposure_when_vol_series_missing_or_stale_cr1_35(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_1h = _make_bar_series([100.0 + 0.2 * i for i in range(30)], symbol=sym, interval="1h", start_ms=t0 - 30 * 3_600_000)

        # Overlay requests 4h bars, market has ONLY 1h bars -> missing series fails closed!
        ov_4h = VolTargetOverlay(target_ann_vol=0.40, vol_lookback=10, vol_floor=0.05, max_leverage=2.0, interval="4h")
        ctx_1h_only = _make_ctx(now_ms=t0, bars=bars_1h)
        res_entry = ov_4h.filter(ctx_1h_only, StrategyOutput(targets=(TargetPosition(sym, 1.0),)))
        self.assertEqual((), res_entry.targets)

        # Explicit flat exit and same-side reduction are preserved when vol series is missing!
        open_pos = {sym: PositionView(sym, qty=100.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=10_000.0, leverage=1.0)}
        ctx_open = _make_ctx(now_ms=t0, bars=bars_1h, positions=open_pos)
        res_exit = ov_4h.filter(ctx_open, StrategyOutput(targets=(TargetPosition(sym, 0.0), TargetPosition(sym, 0.5))))
        self.assertEqual(2, len(res_exit.targets))
        self.assertAlmostEqual(0.0, res_exit.targets[0].target_weight)
        self.assertAlmostEqual(0.5, res_exit.targets[1].target_weight)

        # Present series still in warmup (5 bars < lookback+1=11) -> bounded neutral warmup scale preserved!
        bars_short = _make_bar_series([100.0, 101.0, 100.5, 101.5, 102.0], symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        ov_1h = VolTargetOverlay(target_ann_vol=0.40, vol_lookback=10, vol_floor=0.05, max_leverage=2.0, interval="1h")
        res_warm = ov_1h.filter(_make_ctx(now_ms=t0, bars=bars_short), StrategyOutput(targets=(TargetPosition(sym, 1.0),)))
        self.assertEqual(1, len(res_warm.targets))
        self.assertAlmostEqual(1.0, res_warm.targets[0].target_weight)

        # Stale series (last closed 1h bar is 10 hours old on 24/7 perp) -> fails closed!
        bars_stale = _make_bar_series([100.0 + 0.1 * i for i in range(25)], symbol=sym, interval="1h", start_ms=t0 - 35 * 3_600_000)
        res_stale = ov_1h.filter(_make_ctx(now_ms=t0, bars=bars_stale), StrategyOutput(targets=(TargetPosition(sym, 1.0),)))
        self.assertEqual((), res_stale.targets)

    def test_loss_overlays_handle_entry_gates_and_stamp_reduce_only_on_target_weight_cr2_25_cr2_28(self) -> None:
        sym = "MU-USDT-SWAP"
        in_session_ms = _utc_ms(2026, 6, 11, 14, 0)   # 10:00 ET (inside 09:45-11:30)
        out_session_ms = _utc_ms(2026, 6, 11, 20, 15)  # 16:15 ET (outside 09:45-11:30)
        gate = SessionGate(
            trading_calendar_id=US_EQUITIES_ALGORITHMIC_CALENDAR,
            trading_windows_et=(("09:45", "11:30"),),
        )
        band = (PriceExclusionBand(center=100.0, tolerance_pct=0.01),)
        open_pos = {
            sym: PositionView(
                sym, qty=100.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=10_000.0, leverage=1.0
            )
        }
        # DrawdownCircuitBreaker tripped (15% DD > 10% cap) with flatten_and_halt.
        dd_in = DrawdownCircuitBreaker(max_drawdown_pct=0.10, action="flatten_and_halt")
        ctx_in = _make_ctx(now_ms=in_session_ms, equity=8_500.0, peak_equity=10_000.0, positions=open_pos)

        # CR2-25: Non-reduce_only gated intents (session_gate or excluded_price_bands) MUST NOT crash
        # OrderIntent.__post_init__ and MUST NOT have caller gates silently stripped.
        gated_contracts = OrderIntent("gc", sym, "sell", "market", "contracts", 20.0, session_gate=gate)
        banded_contracts = OrderIntent("bc", sym, "sell", "market", "contracts", 20.0, excluded_price_bands=band)
        gated_tw = OrderIntent("gtw", sym, "sell", "market", "target_weight", 0.5, session_gate=gate)
        gated_close_all = OrderIntent("gca", sym, "sell", "market", "close_all", 0.0, session_gate=gate)
        ungated_close_all = OrderIntent("uca", sym, "sell", "market", "close_all", 0.0)
        # CR2-28: Ungated reducing target_weight intent must require opposing side AND receive reduce_only=True.
        good_tw = OrderIntent("tw_red", sym, "sell", "market", "target_weight", 0.5)
        wrong_side_tw = OrderIntent("tw_bad", sym, "buy", "market", "target_weight", 0.5)

        all_intents = (
            gated_contracts,
            banded_contracts,
            gated_tw,
            gated_close_all,
            ungated_close_all,
            good_tw,
            wrong_side_tw,
        )

        # 1. DrawdownCircuitBreaker flatten_and_halt: flattens open pos, keeps gated close_all only when allowed,
        #    and stamps ungated close_all with reduce_only=True without crashing on entry-gated intents.
        res_dd_in = dd_in.filter(ctx_in, StrategyOutput(intents=all_intents))
        dd_by_id = {it.intent_id: it for it in res_dd_in.intents}
        self.assertIn("gca", dd_by_id)
        self.assertEqual(gate, dd_by_id["gca"].session_gate)
        self.assertFalse(dd_by_id["gca"].reduce_only)
        self.assertTrue(dd_by_id["uca"].reduce_only)
        self.assertEqual(1, len(res_dd_in.targets))
        self.assertEqual(0.0, res_dd_in.targets[0].target_weight)

        dd_out = DrawdownCircuitBreaker(max_drawdown_pct=0.10, action="flatten_and_halt")
        ctx_out = _make_ctx(now_ms=out_session_ms, equity=8_500.0, peak_equity=10_000.0, positions=open_pos)
        res_dd_out = dd_out.filter(ctx_out, StrategyOutput(intents=(gated_close_all, ungated_close_all)))
        self.assertEqual(("uca",), tuple(it.intent_id for it in res_dd_out.intents))
        self.assertTrue(res_dd_out.intents[0].reduce_only)

        # 2. VolTargetOverlay with missing vol (exercises _filter_reducing_intents at in_session_ms):
        #    - drops gated contracts / banded contracts / gated target_weight / wrong-side target_weight
        #    - preserves allowed gated close_all with gate intact
        #    - stamps reduce_only=True on ungated close_all AND compatible reducing target_weight (CR2-28)
        vt_missing = VolTargetOverlay(target_ann_vol=0.40, vol_lookback=10, interval="4h")
        res_vt = vt_missing.filter(ctx_in, StrategyOutput(intents=all_intents))
        vt_by_id = {it.intent_id: it for it in res_vt.intents}
        self.assertEqual({"gca", "uca", "tw_red"}, set(vt_by_id.keys()))
        self.assertEqual(gate, vt_by_id["gca"].session_gate)
        self.assertFalse(vt_by_id["gca"].reduce_only)
        self.assertTrue(vt_by_id["uca"].reduce_only)
        self.assertTrue(vt_by_id["tw_red"].reduce_only)
        self.assertAlmostEqual(0.5, vt_by_id["tw_red"].qty_value, places=9)

        # 3. SessionWindowOverlay outside session applies the same CR2-25 + CR2-28 guarantees.
        sw = SessionWindowOverlay(
            trading_windows_et=(("09:45", "11:30"),),
            calendar_id=US_EQUITIES_ALGORITHMIC_CALENDAR,
        )
        res_sw = sw.filter(ctx_out, StrategyOutput(intents=all_intents))
        self.assertEqual(("uca", "tw_red"), tuple(it.intent_id for it in res_sw.intents))
        self.assertTrue(all(it.reduce_only for it in res_sw.intents))

    def test_vol_target_empty_series_missing_and_preserves_intent_order_cr2_26_cr2_45(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        empty_series = BarSeries.from_candles(sym, "1h", [])
        mv_empty = MarketView({(sym, "1h"): empty_series}, now_ms=t0)
        inst = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        open_pos = {
            sym: PositionView(
                sym, qty=10.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=1_000.0, leverage=0.1
            )
        }
        pf_flat = PortfolioView(
            equity=10_000.0, cash=10_000.0, positions={}, open_orders=(), peak_equity=10_000.0, daily_start_equity=10_000.0
        )
        pf_open = PortfolioView(
            equity=10_000.0,
            cash=9_000.0,
            positions=open_pos,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ov = VolTargetOverlay(target_ann_vol=0.40, vol_lookback=10, vol_floor=0.05, max_leverage=2.0, interval="1h")

        # CR2-26: Empty BarSeries (bool == False) MUST be classified as missing (fail closed), NOT warmup!
        ctx_empty_flat = BarContext(now_ms=t0, symbol=sym, market=mv_empty, portfolio=pf_flat, instrument=inst)
        res_entry = ov.filter(ctx_empty_flat, StrategyOutput(targets=(TargetPosition(sym, 1.0),)))
        self.assertEqual((), res_entry.targets)

        # CR2-45: Missing volatility MUST preserve exact relative order of retained intents
        # (bounded partial sell remains ahead of close_all sell).
        ctx_empty_open = BarContext(now_ms=t0, symbol=sym, market=mv_empty, portfolio=pf_open, instrument=inst)
        partial_sell = OrderIntent("part_s", sym, "sell", "market", "contracts", 4.0)
        close_all_sell = OrderIntent("close_s", sym, "sell", "market", "close_all", 0.0, reduce_only=True)
        res_order = ov.filter(ctx_empty_open, StrategyOutput(intents=(partial_sell, close_all_sell)))
        self.assertEqual(("part_s", "close_s"), tuple(it.intent_id for it in res_order.intents))
        self.assertTrue(res_order.intents[0].reduce_only)
        self.assertAlmostEqual(4.0, res_order.intents[0].qty_value, places=9)

    def test_exposure_cap_accounts_for_resting_open_orders_before_clamping_targets_cr2_29(self) -> None:
        sym = "MU-USDT-SWAP"
        other = "ETH-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_eth = _make_bar_series([200.0] * 5, symbol=other, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu, (other, "1h"): bars_eth}, now_ms=t0)
        inst = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        ov = ExposureCapOverlay(max_gross_leverage=1.5, max_single_weight=1.5)

        # Case 1: Resting 1.0x open buy order on MU -> MU target 1.5x MUST clamp to <= 0.5x!
        resting_mu = OrderIntent("rest_mu", sym, "buy", "limit", "equity_fraction", 1.0, limit_price=95.0)
        pf_same = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(resting_mu,),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_same = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_same, instrument=inst)
        res_same = ov.filter(ctx_same, StrategyOutput(targets=(TargetPosition(sym, 1.5),)))
        self.assertEqual(1, len(res_same.targets))
        self.assertAlmostEqual(0.5, res_same.targets[0].target_weight, places=9)

        # Case 2: Resting 1.0x open buy order on ETH (untouched symbol) -> MU target 1.5x MUST clamp to <= 0.5x!
        resting_eth = OrderIntent("rest_eth", other, "buy", "limit", "equity_fraction", 1.0, limit_price=190.0)
        pf_cross = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(resting_eth,),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_cross = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_cross, instrument=inst)
        res_cross = ov.filter(ctx_cross, StrategyOutput(targets=(TargetPosition(sym, 1.5),)))
        self.assertEqual(1, len(res_cross.targets))
        self.assertAlmostEqual(0.5, res_cross.targets[0].target_weight, places=9)

        # Case 3: Resting reduce_only order does NOT consume entry headroom.
        resting_ro = OrderIntent(
            "rest_ro", sym, "sell", "stop_market", "close_all", 0.0, stop_price=90.0, reduce_only=True
        )
        pf_ro = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(resting_ro,),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_ro = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_ro, instrument=inst)
        res_ro = ov.filter(ctx_ro, StrategyOutput(targets=(TargetPosition(sym, 1.5),)))
        self.assertAlmostEqual(1.5, res_ro.targets[0].target_weight, places=9)


if __name__ == "__main__":
    unittest.main()
