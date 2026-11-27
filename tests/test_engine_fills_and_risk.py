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

        # Case 3 (CR3-16): Resting reduce_only contracts sell (NOT close_all!) worth 1x at limit_price
        # MUST NOT consume entry headroom for a -1.5x short target (if reserved, target would shrink to -0.5x).
        resting_ro = OrderIntent(
            "rest_ro", sym, "sell", "limit", "contracts", 100.0, limit_price=100.0, reduce_only=True
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
        res_ro = ov.filter(ctx_ro, StrategyOutput(targets=(TargetPosition(sym, -1.5),)))
        self.assertAlmostEqual(-1.5, res_ro.targets[0].target_weight, places=9)

    def test_exposure_cap_reserves_pending_additions_after_target_weight_intent_cr3_22(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu}, now_ms=t0)
        inst = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        ov = ExposureCapOverlay(max_gross_leverage=1.0, max_single_weight=1.0)

        # 1. With 1x cap and resting 0.6x buy limit, new 1x buy market target_weight MUST clamp to 0.4x!
        resting_buy = OrderIntent("rb60", sym, "buy", "limit", "equity_fraction", 0.6, limit_price=95.0)
        pf_rb = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(resting_buy,),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_rb = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_rb, instrument=inst)
        tw_buy = OrderIntent("tw1", sym, "buy", "market", "target_weight", 1.0)
        res_rb = ov.filter(ctx_rb, StrategyOutput(intents=(tw_buy,)))
        self.assertEqual(1, len(res_rb.intents))
        self.assertAlmostEqual(0.4, res_rb.intents[0].qty_value, places=9)

        # 2. Resting 1.0x buy limit leaves zero room -> new 1x buy target_weight is rejected!
        resting_full = OrderIntent("rb100", sym, "buy", "limit", "equity_fraction", 1.0, limit_price=95.0)
        pf_full = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(resting_full,),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_full = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_full, instrument=inst)
        res_full = ov.filter(ctx_full, StrategyOutput(intents=(tw_buy,)))
        self.assertEqual((), res_full.intents)

        # 3. Same-output incremental buy + target_weight buy respects the combined 1x cap!
        pf_empty = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_empty = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_empty, instrument=inst)
        res_seq = ov.filter(ctx_empty, StrategyOutput(intents=(resting_buy, tw_buy)))
        self.assertEqual(2, len(res_seq.intents))
        self.assertAlmostEqual(0.6, res_seq.intents[0].qty_value, places=9)
        self.assertAlmostEqual(0.4, res_seq.intents[1].qty_value, places=9)

        # 4. Short mirror: resting 0.6x sell limit clamps new -1x sell target_weight to -0.4x!
        resting_sell = OrderIntent("rs60", sym, "sell", "limit", "equity_fraction", 0.6, limit_price=105.0)
        pf_rs = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(resting_sell,),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_rs = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_rs, instrument=inst)
        tw_sell = OrderIntent("tw_s1", sym, "sell", "market", "target_weight", -1.0)
        res_rs = ov.filter(ctx_rs, StrategyOutput(intents=(tw_sell,)))
        self.assertEqual(1, len(res_rs.intents))
        self.assertAlmostEqual(-0.4, res_rs.intents[0].qty_value, places=9)

        # 5. Without pending additions, 1x target_weight still passes unchanged!
        res_clean = ov.filter(ctx_empty, StrategyOutput(intents=(tw_buy,)))
        self.assertEqual(1, len(res_clean.intents))
        self.assertAlmostEqual(1.0, res_clean.intents[0].qty_value, places=9)

    def test_exposure_cap_reserves_all_surviving_targets_and_documents_decision_time_scope_cr4_01_cr4_02(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu}, now_ms=t0)
        inst = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        ov = ExposureCapOverlay(max_gross_leverage=1.0, max_single_weight=1.0)

        # CR4-02: Overlay contract explicitly scopes caps to decision time and preserves 1.0x at unchanged open.
        self.assertEqual("decision_time", ov.cap_scope)
        self.assertIn("decision-time", (ExposureCapOverlay.__doc__ or "").lower())
        pf_flat = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_flat = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_flat, instrument=inst)
        c100 = OrderIntent("c100", sym, "buy", "market", "contracts", 100.0)
        res_c100 = ov.filter(ctx_flat, StrategyOutput(intents=(c100,)))
        self.assertEqual(1, len(res_c100.intents))
        self.assertAlmostEqual(100.0, res_c100.intents[0].qty_value, places=9)

        # CR4-01: With 1x cap and existing 0.1x long, emitting targets (0.6x, 0.1x) + 0.9x buy MUST
        # reserve the min/max across EVERY surviving target (0.6x), clamping the buy to 0.4x (not 0.9x!).
        pos_01 = {sym: PositionView(sym, qty=10.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=1_000.0, leverage=0.1)}
        pf_01 = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_01,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_01 = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_01, instrument=inst)
        buy_09 = OrderIntent("b90", sym, "buy", "market", "equity_fraction", 0.9)
        res_multi = ov.filter(
            ctx_01,
            StrategyOutput(
                targets=(TargetPosition(sym, 0.6), TargetPosition(sym, 0.1)),
                intents=(buy_09,),
            ),
        )
        self.assertEqual(1, len(res_multi.intents))
        self.assertAlmostEqual(0.4, res_multi.intents[0].qty_value, places=9)

        # A lone 0.1x target with existing 0.1x long MUST still leave exact 0.9x headroom!
        res_single = ov.filter(
            ctx_01,
            StrategyOutput(
                targets=(TargetPosition(sym, 0.1),),
                intents=(buy_09,),
            ),
        )
        self.assertEqual(1, len(res_single.intents))
        self.assertAlmostEqual(0.9, res_single.intents[0].qty_value, places=9)

        # Short mirror: existing -0.1x short, targets (-0.6x, -0.1x) + 0.9x sell clamps sell to 0.4x.
        pos_m01 = {sym: PositionView(sym, qty=-10.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=1_000.0, leverage=0.1)}
        pf_m01 = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_m01,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_m01 = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_m01, instrument=inst)
        sell_09 = OrderIntent("s90", sym, "sell", "market", "equity_fraction", 0.9)
        res_short = ov.filter(
            ctx_m01,
            StrategyOutput(
                targets=(TargetPosition(sym, -0.6), TargetPosition(sym, -0.1)),
                intents=(sell_09,),
            ),
        )
        self.assertEqual(1, len(res_short.intents))
        self.assertAlmostEqual(0.4, res_short.intents[0].qty_value, places=9)

    def test_exposure_cap_docstring_baseline_scaling_and_protective_reductions_cr5_03_cr5_04_cr5_05(self) -> None:
        sym_a = "MU-USDT-SWAP"
        sym_b = "ETH-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_a = _make_bar_series([100.0] * 5, symbol=sym_a, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_b = _make_bar_series([100.0] * 5, symbol=sym_b, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym_a, "1h"): bars_a, (sym_b, "1h"): bars_b}, now_ms=t0)
        inst = okx_swap_spec(sym_a, tick_size=Decimal("0.01"), max_leverage=5.0)
        ov = ExposureCapOverlay(max_gross_leverage=1.0, max_single_weight=1.0)

        # CR5-03 / CR6-03: Docstring removes nonexistent SimulatorConfig(max_leverage=...) and
        # accurately describes InstrumentSpec.max_leverage as a clamp on the requested target weight.
        doc = ExposureCapOverlay.__doc__ or ""
        self.assertNotIn("SimulatorConfig(max_leverage", doc)
        self.assertNotIn("per-position fill-time cap", doc.lower())
        self.assertIn("enforce_instrument_max_leverage", doc)
        self.assertIn("clamp on the requested target weight", doc.lower())
        self.assertIn("portfolio-level", doc.lower())

        # CR5-04: With 1x gross cap, existing 0.6x long in A, B flat: targets A=(1.0, 0.6), B=(0.6).
        # Scale factor 0.4 applies ONLY to requested increases above baseline 0.6 -> A=(0.76, 0.6), B=(0.24)!
        pos_a06 = {
            sym_a: PositionView(sym_a, qty=60.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=6_000.0, leverage=0.6)
        }
        pf_a06 = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_a06,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_a06 = BarContext(now_ms=t0, symbol=sym_a, market=mv, portfolio=pf_a06, instrument=inst)
        res_scale = ov.filter(
            ctx_a06,
            StrategyOutput(
                targets=(TargetPosition(sym_a, 1.0), TargetPosition(sym_a, 0.6), TargetPosition(sym_b, 0.6)),
            ),
        )
        self.assertEqual(3, len(res_scale.targets))
        self.assertAlmostEqual(0.76, res_scale.targets[0].target_weight, places=9)
        self.assertAlmostEqual(0.60, res_scale.targets[1].target_weight, places=9)
        self.assertAlmostEqual(0.24, res_scale.targets[2].target_weight, places=9)

        # Flat book still scales proportionally from zero: A=1.0, B=0.6 under 1x cap -> (0.625, 0.375).
        pf_flat = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions={},
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_flat = BarContext(now_ms=t0, symbol=sym_a, market=mv, portfolio=pf_flat, instrument=inst)
        res_flat = ov.filter(
            ctx_flat,
            StrategyOutput(targets=(TargetPosition(sym_a, 1.0), TargetPosition(sym_b, 0.6))),
        )
        self.assertAlmostEqual(0.625, res_flat.targets[0].target_weight, places=9)
        self.assertAlmostEqual(0.375, res_flat.targets[1].target_weight, places=9)

        # Short baseline mirror: existing -0.6x in A, B flat -> A=(-0.76, -0.6), B=(-0.24).
        pos_am06 = {
            sym_a: PositionView(sym_a, qty=-60.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=6_000.0, leverage=0.6)
        }
        pf_am06 = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_am06,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_am06 = BarContext(now_ms=t0, symbol=sym_a, market=mv, portfolio=pf_am06, instrument=inst)
        res_short_scale = ov.filter(
            ctx_am06,
            StrategyOutput(
                targets=(TargetPosition(sym_a, -1.0), TargetPosition(sym_a, -0.6), TargetPosition(sym_b, -0.6)),
            ),
        )
        self.assertAlmostEqual(-0.76, res_short_scale.targets[0].target_weight, places=9)
        self.assertAlmostEqual(-0.60, res_short_scale.targets[1].target_weight, places=9)
        self.assertAlmostEqual(-0.24, res_short_scale.targets[2].target_weight, places=9)

        # CR5-05: 1x cap, existing 0.8x long, targets (0.6, 0.8), reduce_only stop 0.5x, non-reduce_only 1.6x sell.
        # Protective reduction lowers reachable long floor from 0.6 -> 0.1, clamping the 1.6x sell to 1.1x!
        pos_a08 = {
            sym_a: PositionView(sym_a, qty=80.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=8_000.0, leverage=0.8)
        }
        pf_a08 = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_a08,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_a08 = BarContext(now_ms=t0, symbol=sym_a, market=mv, portfolio=pf_a08, instrument=inst)
        stop_05 = OrderIntent("stp05", sym_a, "sell", "stop_market", "equity_fraction", 0.5, stop_price=95.0, reduce_only=True)
        stop_50c = OrderIntent("stp50c", sym_a, "sell", "stop_market", "contracts", 50.0, stop_price=95.0, reduce_only=True)
        sell_16 = OrderIntent("s16", sym_a, "sell", "market", "equity_fraction", 1.6)
        res_with_stop = ov.filter(
            ctx_a08,
            StrategyOutput(
                targets=(TargetPosition(sym_a, 0.6), TargetPosition(sym_a, 0.8)),
                intents=(stop_05, sell_16),
            ),
        )
        self.assertEqual(2, len(res_with_stop.intents))
        self.assertAlmostEqual(1.1, res_with_stop.intents[1].qty_value, places=9)

        # Same with contracts reduce-only stop (50 contracts @ stop 95 on 100 ref price -> 0.50x reduction, clamps sell to 1.1x).
        res_with_cstop = ov.filter(
            ctx_a08,
            StrategyOutput(
                targets=(TargetPosition(sym_a, 0.6), TargetPosition(sym_a, 0.8)),
                intents=(stop_50c, sell_16),
            ),
        )
        self.assertAlmostEqual(1.1, res_with_cstop.intents[1].qty_value, places=9)

        # Without protective stop, 1.6x sell against 0.6x lower bound ends at exact -1.0x and passes unchanged!
        res_no_stop = ov.filter(
            ctx_a08,
            StrategyOutput(
                targets=(TargetPosition(sym_a, 0.6), TargetPosition(sym_a, 0.8)),
                intents=(sell_16,),
            ),
        )
        self.assertEqual(1, len(res_no_stop.intents))
        self.assertAlmostEqual(1.6, res_no_stop.intents[0].qty_value, places=9)

        # Short mirror: existing -0.8x short, targets (-0.6, -0.8), reduce_only buy stop 0.5x + 1.6x buy -> clamped to 1.1x.
        pos_am08 = {
            sym_a: PositionView(sym_a, qty=-80.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=8_000.0, leverage=0.8)
        }
        pf_am08 = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_am08,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_am08 = BarContext(now_ms=t0, symbol=sym_a, market=mv, portfolio=pf_am08, instrument=inst)
        bstop_05 = OrderIntent("bstp05", sym_a, "buy", "stop_market", "equity_fraction", 0.5, stop_price=105.0, reduce_only=True)
        buy_16 = OrderIntent("b16", sym_a, "buy", "market", "equity_fraction", 1.6)
        res_short_stop = ov.filter(
            ctx_am08,
            StrategyOutput(
                targets=(TargetPosition(sym_a, -0.6), TargetPosition(sym_a, -0.8)),
                intents=(bstop_05, buy_16),
            ),
        )
        self.assertAlmostEqual(1.1, res_short_stop.intents[1].qty_value, places=9)

    def test_exposure_cap_settled_equity_basis_and_instrument_leverage_clamp_cr6_04_cr6_05(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu}, now_ms=t0)
        inst_5x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)

        # CR6-04: 80 units long @ 100, marked_equity=10_000, settled_equity(cash)=12_000, 1x cap.
        # Reduce-only 0.5 equity_fraction sell removes 60 units under settled_equity (floor=20 -> max sell 120)
        # vs 50 units under marked_equity (floor=30 -> max sell 130).
        pos_80 = {sym: PositionView(sym, qty=80.0, avg_entry_price=100.0, unrealized_pnl=-2_000.0, notional=8_000.0, leverage=0.8)}
        pf_settled = PortfolioView(
            equity=10_000.0,
            cash=12_000.0,
            positions=pos_80,
            open_orders=(),
            peak_equity=12_000.0,
            daily_start_equity=12_000.0,
        )
        ctx_settled = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_settled, instrument=inst_5x)
        ro_sell_05 = OrderIntent("ro05", sym, "sell", "market", "equity_fraction", 0.5, reduce_only=True)
        opp_130 = OrderIntent("opp130", sym, "sell", "market", "contracts", 130.0)
        opp_ef12 = OrderIntent("oppef12", sym, "sell", "market", "equity_fraction", 1.2)

        ov_settled = ExposureCapOverlay(1.0, 1.0, sizing_basis="settled_equity")
        res_s = ov_settled.filter(ctx_settled, StrategyOutput(intents=(ro_sell_05, opp_130)))
        self.assertAlmostEqual(120.0, res_s.intents[1].qty_value, places=9)

        # Opposing equity_fraction sell (1.2 * 12k = 14.4k) also clamps to 1.0 (1.0 * 12k = 12k = 120 units).
        res_s_ef = ov_settled.filter(ctx_settled, StrategyOutput(intents=(ro_sell_05, opp_ef12)))
        self.assertAlmostEqual(1.0, res_s_ef.intents[1].qty_value, places=9)

        # Under marked_equity, same inputs leave 30-unit floor and admit full 130-unit sell.
        ov_marked = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity")
        res_m = ov_marked.filter(ctx_settled, StrategyOutput(intents=(ro_sell_05, opp_130)))
        self.assertAlmostEqual(130.0, res_m.intents[1].qty_value, places=9)

        # Standalone unknown sizing_basis=None conservatively reserves max(marked, settled) -> 120 units.
        ov_unknown = ExposureCapOverlay(1.0, 1.0)
        res_u = ov_unknown.filter(ctx_settled, StrategyOutput(intents=(ro_sell_05, opp_130)))
        self.assertAlmostEqual(120.0, res_u.intents[1].qty_value, places=9)

        # Short mirror under settled_equity: -80 short, reduce_only 0.5 buy + 130-unit buy -> 120 units.
        pos_m80 = {sym: PositionView(sym, qty=-80.0, avg_entry_price=100.0, unrealized_pnl=-2_000.0, notional=8_000.0, leverage=0.8)}
        pf_m80 = PortfolioView(
            equity=10_000.0,
            cash=12_000.0,
            positions=pos_m80,
            open_orders=(),
            peak_equity=12_000.0,
            daily_start_equity=12_000.0,
        )
        ctx_m80 = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_m80, instrument=inst_5x)
        ro_buy_05 = OrderIntent("rob05", sym, "buy", "market", "equity_fraction", 0.5, reduce_only=True)
        opp_b130 = OrderIntent("oppb130", sym, "buy", "market", "contracts", 130.0)
        res_short_s = ov_settled.filter(ctx_m80, StrategyOutput(intents=(ro_buy_05, opp_b130)))
        self.assertAlmostEqual(120.0, res_short_s.intents[1].qty_value, places=9)

        # CR6-05: 1x overlay cap + 0.5x instrument cap, 80-contract long @ 100 on 10_000 equity.
        # Reduce-only sell target_weight=+0.6 + 160-contract sell:
        inst_05x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=0.5)
        pf_eq = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_80,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_05x = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_eq, instrument=inst_05x)
        ro_tw06 = OrderIntent("rotw06", sym, "sell", "market", "target_weight", 0.6, reduce_only=True)
        opp_160 = OrderIntent("opp160", sym, "sell", "market", "contracts", 160.0)

        # 1. enforce_instrument_max_leverage=True -> protective target clamped to +0.5x -> max sell 150 contracts.
        ov_enf_on = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity", enforce_instrument_max_leverage=True)
        res_on = ov_enf_on.filter(ctx_05x, StrategyOutput(intents=(ro_tw06, opp_160)))
        self.assertAlmostEqual(150.0, res_on.intents[1].qty_value, places=9)

        # 2. enforce_instrument_max_leverage=False -> preserves +0.6x floor -> admits full 160 contracts.
        ov_enf_off = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity", enforce_instrument_max_leverage=False)
        res_off = ov_enf_off.filter(ctx_05x, StrategyOutput(intents=(ro_tw06, opp_160)))
        self.assertAlmostEqual(160.0, res_off.intents[1].qty_value, places=9)

        # 3. Standalone unknown enforce_instrument_max_leverage=None -> reserves down to flat (0.0) -> 100 contracts.
        ov_enf_unk = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity")
        res_unk = ov_enf_unk.filter(ctx_05x, StrategyOutput(intents=(ro_tw06, opp_160)))
        self.assertAlmostEqual(100.0, res_unk.intents[1].qty_value, places=9)

        # Short mirror with enforce_instrument_max_leverage=True: -80 short, reduce_only buy target_weight=-0.6 + 160 buy -> 150.
        pf_m_eq = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_m80,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_m05x = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_m_eq, instrument=inst_05x)
        ro_btw_m06 = OrderIntent("robtwm06", sym, "buy", "market", "target_weight", -0.6, reduce_only=True)
        opp_b160 = OrderIntent("oppb160", sym, "buy", "market", "contracts", 160.0)
        res_short_on = ov_enf_on.filter(ctx_m05x, StrategyOutput(intents=(ro_btw_m06, opp_b160)))
        self.assertAlmostEqual(150.0, res_short_on.intents[1].qty_value, places=9)

    def test_exposure_cap_protective_ef_limit_and_settled_target_conversions_cr7_04_to_cr7_06_cr7_10(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu}, now_ms=t0)
        inst_05x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=0.5)
        inst_5x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        inst_1x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=1.0)

        # CR7-04: 80-contract long @ 100 on 10_000 equity, inst max_leverage=0.5, reduce_only 0.7 equity_fraction sell + 130 sell.
        pos_80_eq = {sym: PositionView(sym, qty=80.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=8_000.0, leverage=0.8)}
        pf_10k = PortfolioView(
            equity=10_000.0,
            cash=10_000.0,
            positions=pos_80_eq,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_05x = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_10k, instrument=inst_05x)
        ro_ef07 = OrderIntent("roef07", sym, "sell", "market", "equity_fraction", 0.7, reduce_only=True)
        opp_130 = OrderIntent("opp130", sym, "sell", "market", "contracts", 130.0)

        # enforce=True -> protective 0.7 clamped to 0.5 (leaves 30 residual) -> admits full 130 contracts!
        ov_on = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity", enforce_instrument_max_leverage=True)
        self.assertAlmostEqual(130.0, ov_on.filter(ctx_05x, StrategyOutput(intents=(ro_ef07, opp_130))).intents[1].qty_value, places=9)
        # enforce=False and enforce=None -> preserve full 0.7 reservation (10 residual) -> clamp to 110 contracts!
        ov_off = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity", enforce_instrument_max_leverage=False)
        ov_unk = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity")
        self.assertAlmostEqual(110.0, ov_off.filter(ctx_05x, StrategyOutput(intents=(ro_ef07, opp_130))).intents[1].qty_value, places=9)
        self.assertAlmostEqual(110.0, ov_unk.filter(ctx_05x, StrategyOutput(intents=(ro_ef07, opp_130))).intents[1].qty_value, places=9)

        # Short mirror of CR7-04: -80 short, reduce_only 0.7 equity_fraction buy + 130 buy -> admits 130 when enforce=True.
        pos_m80_eq = {sym: PositionView(sym, qty=-80.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=8_000.0, leverage=0.8)}
        ctx_m05x = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 10_000.0, pos_m80_eq, (), 10_000.0, 10_000.0),
            instrument=inst_05x,
        )
        ro_bef07 = OrderIntent("robef07", sym, "buy", "market", "equity_fraction", 0.7, reduce_only=True)
        opp_b130 = OrderIntent("oppb130", sym, "buy", "market", "contracts", 130.0)
        self.assertAlmostEqual(130.0, ov_on.filter(ctx_m05x, StrategyOutput(intents=(ro_bef07, opp_b130))).intents[1].qty_value, places=9)

        # CR7-05: 80 long @ 75 -> ref_px=100, settled=8_000, marked=10_000 (upnl=+2_000), inst_ml=0.5, enforce=True.
        # Non-reduce-only market sell target_weight=+0.6 followed by 150-contract market sell:
        pos_80_prof = {sym: PositionView(sym, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8)}
        pf_8k_prof = PortfolioView(
            equity=10_000.0,
            cash=8_000.0,
            positions=pos_80_prof,
            open_orders=(),
            peak_equity=10_000.0,
            daily_start_equity=10_000.0,
        )
        ctx_8k_05x = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_8k_prof, instrument=inst_05x)
        tw_sell_06 = OrderIntent("tw06", sym, "sell", "market", "target_weight", 0.6)
        opp_150 = OrderIntent("opp150", sym, "sell", "market", "contracts", 150.0)

        # Under settled_equity: effective 0.5x target leaves 40 contracts (+0.4x marked) -> clamps 150 to 140 contracts!
        ov_s_on = ExposureCapOverlay(1.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        res_cr705_s = ov_s_on.filter(ctx_8k_05x, StrategyOutput(intents=(tw_sell_06, opp_150)))
        self.assertAlmostEqual(140.0, res_cr705_s.intents[1].qty_value, places=9)
        # Under marked_equity: effective 0.5x target leaves 50 contracts (+0.5x marked) -> keeps full 150 contracts!
        res_cr705_m = ov_on.filter(ctx_8k_05x, StrategyOutput(intents=(tw_sell_06, opp_150)))
        self.assertAlmostEqual(150.0, res_cr705_m.intents[1].qty_value, places=9)

        # Short mirror of CR7-05: -80 short @ 125 -> ref_px=100, settled=8_000, marked=10_000, target_weight=-0.6 buy + 150 buy -> 140.
        pos_m80_prof = {sym: PositionView(sym, qty=-80.0, avg_entry_price=125.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8)}
        ctx_m8k_05x = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_m80_prof, (), 10_000.0, 10_000.0),
            instrument=inst_05x,
        )
        tw_buy_m06 = OrderIntent("twm06", sym, "buy", "market", "target_weight", -0.6)
        opp_b150 = OrderIntent("oppb150", sym, "buy", "market", "contracts", 150.0)
        self.assertAlmostEqual(140.0, ov_s_on.filter(ctx_m8k_05x, StrategyOutput(intents=(tw_buy_m06, opp_b150))).intents[1].qty_value, places=9)

        # CR7-06: 80 long @ 75 -> ref_px=100, settled=8_000, marked=10_000, reduce_only 40-contract sell (+1_000 realized -> 9_000 settled)
        # + 1.75 equity_fraction sell -> clamped to 1.4 / 0.9 = 14/9 (140 contracts on 9_000 settled -> exact -1.0x cap!).
        ctx_8k_5x = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_8k_prof, instrument=inst_5x)
        ro_40 = OrderIntent("ro40", sym, "sell", "market", "contracts", 40.0, reduce_only=True)
        ef_175 = OrderIntent("ef175", sym, "sell", "market", "equity_fraction", 1.75)
        res_cr706 = ov_s_on.filter(ctx_8k_5x, StrategyOutput(intents=(ro_40, ef_175)))
        self.assertAlmostEqual(1.4 / 0.9, res_cr706.intents[1].qty_value, places=9)
        # When instrument max_leverage=1.0 binds (enforce=True), 1.0 * 0.9 = 0.9 fits within 1.4 allowance -> unchanged!
        ctx_8k_1x = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_8k_prof, instrument=inst_1x)
        res_cr706_bind = ov_s_on.filter(ctx_8k_1x, StrategyOutput(intents=(ro_40, ef_175)))
        self.assertAlmostEqual(1.75, res_cr706_bind.intents[1].qty_value, places=9)
        # Short mirror of CR7-06: -80 short @ 125, reduce_only 40-contract buy + 1.75 equity_fraction buy -> 1.4 / 0.9!
        ctx_m8k_5x = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_m80_prof, (), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        ro_b40 = OrderIntent("rob40", sym, "buy", "market", "contracts", 40.0, reduce_only=True)
        ef_b175 = OrderIntent("efb175", sym, "buy", "market", "equity_fraction", 1.75)
        self.assertAlmostEqual(1.4 / 0.9, ov_s_on.filter(ctx_m8k_5x, StrategyOutput(intents=(ro_b40, ef_b175))).intents[1].qty_value, places=9)

        # CR7-10: TargetPosition capped in marked-equity units under settled_equity=12_000, marked=10_000 (1.2 ratio), 1:1 under marked_equity.
        pf_12k_flat = PortfolioView(
            equity=10_000.0,
            cash=12_000.0,
            positions={},
            open_orders=(),
            peak_equity=12_000.0,
            daily_start_equity=12_000.0,
        )
        ctx_12k = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_12k_flat, instrument=inst_5x)
        ov_s = ExposureCapOverlay(1.0, 1.0, sizing_basis="settled_equity")
        ov_m = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity")
        res_t_pos = ov_s.filter(ctx_12k, StrategyOutput(targets=(TargetPosition(sym, 1.0),)))
        self.assertAlmostEqual(1.0 / 1.2, res_t_pos.targets[0].target_weight, places=9)
        res_t_neg = ov_s.filter(ctx_12k, StrategyOutput(targets=(TargetPosition(sym, -1.0),)))
        self.assertAlmostEqual(-1.0 / 1.2, res_t_neg.targets[0].target_weight, places=9)
        # Gross cap across two symbols: +0.5 each => 0.6 each in marked units (1.2 gross > 1.0) => 0.5/1.2 each!
        res_t_gross = ov_s.filter(
            ctx_12k,
            StrategyOutput(targets=(TargetPosition(sym, 0.5), TargetPosition("ETH-USDT-SWAP", 0.5))),
        )
        self.assertAlmostEqual(0.5 / 1.2, res_t_gross.targets[0].target_weight, places=9)
        self.assertAlmostEqual(0.5 / 1.2, res_t_gross.targets[1].target_weight, places=9)
        # Under marked_equity: exact 1:1 conversion -> +1.0 unchanged!
        res_t_marked = ov_m.filter(ctx_12k, StrategyOutput(targets=(TargetPosition(sym, 1.0),)))
        self.assertAlmostEqual(1.0, res_t_marked.targets[0].target_weight, places=9)

    def test_exposure_cap_phase_aware_post_target_protective_and_resting_ef_cr8_04_cr8_05(self) -> None:
        sym = "MU-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu}, now_ms=t0)
        inst_5x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        inst_1x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=1.0)

        # CR8-04: 80 long @ 75 -> mark=100, settled=8_000, marked=10_000, 1x cap, settled_equity.
        # TargetPosition(+0.625) reduces 80 -> 50 (+750 realized -> settled=8_750), then reduce_only 0.5 limit sell @ 100
        # sells 43.75 contracts (leaving 6.25) -> 110-contract market sell MUST clamp to 106.25 (exact -1.0x!).
        pos_80_p = {sym: PositionView(sym, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8)}
        pf_8k = PortfolioView(10_000.0, 8_000.0, pos_80_p, (), 10_000.0, 10_000.0)
        ctx_8k = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_8k, instrument=inst_5x)
        ov_s = ExposureCapOverlay(1.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)

        ro_lim05 = OrderIntent("rol05", sym, "sell", "limit", "equity_fraction", 0.5, limit_price=100.0, reduce_only=True)
        ro_mkt05 = OrderIntent("rom05", sym, "sell", "market", "equity_fraction", 0.5, reduce_only=True)
        mkt_110 = OrderIntent("m110", sym, "sell", "market", "contracts", 110.0)
        mkt_150 = OrderIntent("m150", sym, "sell", "market", "contracts", 150.0)

        res_post_tgt = ov_s.filter(
            ctx_8k,
            StrategyOutput(targets=(TargetPosition(sym, 0.625),), intents=(ro_lim05, mkt_110)),
        )
        self.assertAlmostEqual(106.25, res_post_tgt.intents[1].qty_value, places=9)

        # Without a profit-realizing target, the 0.5 limit sell preserves exact 40-contract reservation (80-40=40 -> max sell 140)!
        res_no_tgt = ov_s.filter(ctx_8k, StrategyOutput(intents=(ro_lim05, mkt_150)))
        self.assertAlmostEqual(140.0, res_no_tgt.intents[1].qty_value, places=9)

        # A Step 1.3 MARKET reduce_only sell runs BEFORE targets at settled=8_000 (40 contracts), so without target still 140!
        res_mkt_only = ov_s.filter(ctx_8k, StrategyOutput(intents=(ro_mkt05, mkt_150)))
        self.assertAlmostEqual(140.0, res_mkt_only.intents[1].qty_value, places=9)

        # CR8-05: 80 long @ 75 -> mark=100, settled=8_000, marked=10_000, cap=1x, resting 1.75 sell limit @ 100.
        # TargetPosition(+0.5) would leave 40 long and raise settled to 9_000, making resting 1.75 sell 157.5 (-117.5 = -1.175x!).
        # Overlay MUST pull target up to (12/23) / 0.8 = 0.6521739... (so net = -100.0 = exact -1.0x!).
        rest_sell_175 = OrderIntent("rs175", sym, "sell", "limit", "equity_fraction", 1.75, limit_price=100.0)
        pf_8k_rest = PortfolioView(10_000.0, 8_000.0, pos_80_p, (rest_sell_175,), 10_000.0, 10_000.0)
        ctx_rest_5x = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_8k_rest, instrument=inst_5x)
        res_rest_5x = ov_s.filter(ctx_rest_5x, StrategyOutput(targets=(TargetPosition(sym, 0.5),)))
        self.assertAlmostEqual((12.0 / 23.0) / 0.8, res_rest_5x.targets[0].target_weight, places=9)

        # With instrument max_leverage=1.0 enforced, resting 1.75 clamps to 1.0 (90 contracts on 9k settled -> -50 = -0.5x),
        # so TargetPosition(+0.5) fits cleanly and is admitted unchanged!
        ctx_rest_1x = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_8k_rest, instrument=inst_1x)
        res_rest_1x = ov_s.filter(ctx_rest_1x, StrategyOutput(targets=(TargetPosition(sym, 0.5),)))
        self.assertAlmostEqual(0.5, res_rest_1x.targets[0].target_weight, places=9)

        # Step 4 target_weight intent with resting 1.75 sell limit is ALSO pulled up to (12/23) / 0.8!
        res_rest_tw = ov_s.filter(
            ctx_rest_5x,
            StrategyOutput(intents=(OrderIntent("tw05", sym, "sell", "market", "target_weight", 0.5),)),
        )
        self.assertAlmostEqual((12.0 / 23.0) / 0.8, res_rest_tw.intents[0].qty_value, places=9)

        # Short mirror of CR8-05: -80 short @ 125, resting 1.75 buy limit @ 100 + TargetPosition(-0.5) -> -(12/23) / 0.8!
        pos_m80_p = {sym: PositionView(sym, qty=-80.0, avg_entry_price=125.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8)}
        rest_buy_175 = OrderIntent("rb175", sym, "buy", "limit", "equity_fraction", 1.75, limit_price=100.0)
        ctx_mrest_5x = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_m80_p, (rest_buy_175,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_mrest_5x = ov_s.filter(ctx_mrest_5x, StrategyOutput(targets=(TargetPosition(sym, -0.5),)))
        self.assertAlmostEqual(-(12.0 / 23.0) / 0.8, res_mrest_5x.targets[0].target_weight, places=9)

    def test_exposure_cap_sequential_resting_ef_lower_basis_and_gross_recheck_cr9_04_to_cr9_06(self) -> None:
        sym = "MU-USDT-SWAP"
        sym_b = "ETH-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_eth = _make_bar_series([100.0] * 5, symbol=sym_b, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu, (sym_b, "1h"): bars_eth}, now_ms=t0)
        inst_5x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        ov_s = ExposureCapOverlay(1.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)

        # CR9-04: 80 long @ 75, settled=8_000, mark=100, two resting sell limits (0.5 and 1.5 of settled equity).
        # With a new 10-contract market sell, sequential profit realization between 0.5 and 1.5 would reach -110.47;
        # overlay MUST clamp the market sell to exact 320/99 (~3.2323) regardless of resting order list order!
        pos_80_p = {sym: PositionView(sym, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8)}
        lim_05 = OrderIntent("rl05", sym, "sell", "limit", "equity_fraction", 0.5, limit_price=100.0)
        lim_15 = OrderIntent("rl15", sym, "sell", "limit", "equity_fraction", 1.5, limit_price=100.0)
        mkt_s10 = OrderIntent("ms10", sym, "sell", "market", "contracts", 10.0)
        for rest_pair in ((lim_05, lim_15), (lim_15, lim_05)):
            pf_pair = PortfolioView(10_000.0, 8_000.0, pos_80_p, rest_pair, 10_000.0, 10_000.0)
            ctx_pair = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_pair, instrument=inst_5x)
            res_pair = ov_s.filter(ctx_pair, StrategyOutput(intents=(mkt_s10,)))
            self.assertEqual(1, len(res_pair.intents))
            self.assertAlmostEqual(320.0 / 99.0, res_pair.intents[0].qty_value, places=6)
            # Without the market sell, the two resting limits leave -95 contracts and a no-op hold target (+1.0 -> 80) fits!
            res_hold = ov_s.filter(ctx_pair, StrategyOutput(targets=(TargetPosition(sym, 1.0),)))
            self.assertAlmostEqual(1.0, res_hold.targets[0].target_weight, places=9)

        # Multi-symbol CR9-04: A long 80 @ 75 has resting A sell ef=0.8 (realizes +1,600 -> settled 9,600),
        # while flat B has resting B buys ef=0.4 and 0.5 -> A sell filling first boosts BOTH B buys to 0.9 * 0.96 = 0.864,
        # so a 20-contract B buy under 1.0x single cap MUST clamp to exact 13.6 contracts!
        ov_multi = ExposureCapOverlay(2.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        rest_a08 = OrderIntent("ra08", sym, "sell", "limit", "equity_fraction", 0.8, limit_price=100.0)
        rest_b04 = OrderIntent("rb04", sym_b, "buy", "limit", "equity_fraction", 0.4, limit_price=100.0)
        rest_b05 = OrderIntent("rb05", sym_b, "buy", "limit", "equity_fraction", 0.5, limit_price=100.0)
        pf_multi = PortfolioView(10_000.0, 8_000.0, pos_80_p, (rest_b04, rest_a08, rest_b05), 10_000.0, 10_000.0)
        ctx_multi = BarContext(now_ms=t0, symbol=sym_b, market=mv, portfolio=pf_multi, instrument=inst_5x)
        res_multi = ov_multi.filter(ctx_multi, StrategyOutput(intents=(OrderIntent("mb20", sym_b, "buy", "market", "contracts", 20.0),)))
        self.assertAlmostEqual(13.6, res_multi.intents[0].qty_value, places=6)

        # CR9-05: 80 long @ 125 (mark=100, settled=12_000, marked=10_000), resting 128-contract sell limit @ 100.
        # Submit 40-contract market reduction (-1_000 realized loss -> settled=11_000) + TargetPosition(+0.25):
        # lower sizing basis leaves 27.5 contracts -> 27.5 - 128 = -100.5 (> 1x cap!).
        # Overlay MUST pull target up to 0.28 / 1.1 so BOTH lower (28 - 128 = -100) AND upper (30.55) stay <= 1x!
        pos_80_l = {sym: PositionView(sym, qty=80.0, avg_entry_price=125.0, unrealized_pnl=-2_000.0, notional=8_000.0, leverage=0.8)}
        rest_s128 = OrderIntent("rs128", sym, "sell", "limit", "contracts", 128.0, limit_price=100.0)
        ro_m40 = OrderIntent("rom40", sym, "sell", "market", "contracts", 40.0, reduce_only=True)
        pf_905 = PortfolioView(10_000.0, 12_000.0, pos_80_l, (rest_s128,), 12_000.0, 12_000.0)
        ctx_905 = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_905, instrument=inst_5x)
        res_905 = ov_s.filter(ctx_905, StrategyOutput(targets=(TargetPosition(sym, 0.25),), intents=(ro_m40,)))
        self.assertAlmostEqual(0.28 / 1.1, res_905.targets[0].target_weight, places=9)

        # With a 120-contract resting sell (27.5 - 120 = -92.5 fits!), +0.25 passes unchanged AND retains both bounds!
        rest_s120 = OrderIntent("rs120", sym, "sell", "limit", "contracts", 120.0, limit_price=100.0)
        pf_905_ok = PortfolioView(10_000.0, 12_000.0, pos_80_l, (rest_s120,), 12_000.0, 12_000.0)
        ctx_905_ok = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_905_ok, instrument=inst_5x)
        res_905_ok = ov_s.filter(ctx_905_ok, StrategyOutput(targets=(TargetPosition(sym, 0.25),), intents=(ro_m40,)))
        self.assertAlmostEqual(0.25, res_905_ok.targets[0].target_weight, places=9)

        # CR9-06: Gross-cap scaling with a reduced target below baseline!
        # A is long 0.6x with a 1.2x resting sell; A target = +0.2x (worst case 1.0x), B target = +0.5x.
        pos_a06 = {sym: PositionView(sym, qty=60.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=6_000.0, leverage=0.6)}
        rest_a120 = OrderIntent("ra120", sym, "sell", "limit", "contracts", 120.0, limit_price=100.0)
        pf_906 = PortfolioView(10_000.0, 10_000.0, pos_a06, (rest_a120,), 10_000.0, 10_000.0)
        ctx_906 = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_906, instrument=inst_5x)
        out_906 = StrategyOutput(targets=(TargetPosition(sym, 0.2), TargetPosition(sym_b, 0.5)))

        # Under 1.0x gross cap: A stays 0.2 (worst 1.0x), B scales to 0.0 -> total worst-case gross == 1.0x (NOT 1.22x!)
        ov_g10 = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity")
        res_g10 = ov_g10.filter(ctx_906, out_906)
        self.assertAlmostEqual(0.2, res_g10.targets[0].target_weight, places=9)
        self.assertAlmostEqual(0.0, res_g10.targets[1].target_weight, places=9)

        # Under 1.2x gross cap: A stays 0.2 (worst 1.0x), B gets exact remaining 0.2x -> total worst-case gross == 1.2x!
        ov_g12 = ExposureCapOverlay(1.2, 1.0, sizing_basis="marked_equity")
        res_g12 = ov_g12.filter(ctx_906, out_906)
        self.assertAlmostEqual(0.2, res_g12.targets[0].target_weight, places=9)
        self.assertAlmostEqual(0.2, res_g12.targets[1].target_weight, places=9)

        # Under 1.5x gross cap: worst-case gross (1.0 + 0.5 = 1.5) already fits -> BOTH pass through UNCHANGED!
        ov_g15 = ExposureCapOverlay(1.5, 1.0, sizing_basis="marked_equity")
        res_g15 = ov_g15.filter(ctx_906, out_906)
        self.assertAlmostEqual(0.2, res_g15.targets[0].target_weight, places=9)
        self.assertAlmostEqual(0.5, res_g15.targets[1].target_weight, places=9)

        # With untouched C=+0.3x under 1.0x gross cap (allowed_targeted_gross=0.7), A's +0.2 target (worst 1.0 > 0.7)
        # is pulled up toward hold (+0.5 -> 0.5 - 1.2 = -0.7) so total gross == 0.7 + 0.3 == 1.0x!
        pos_ac = {
            sym: pos_a06[sym],
            "BTC-USDT-SWAP": PositionView("BTC-USDT-SWAP", qty=30.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=3_000.0, leverage=0.3),
        }
        pf_906_c = PortfolioView(10_000.0, 10_000.0, pos_ac, (rest_a120,), 10_000.0, 10_000.0)
        ctx_906_c = BarContext(now_ms=t0, symbol=sym, market=mv, portfolio=pf_906_c, instrument=inst_5x)
        res_g10_c = ov_g10.filter(ctx_906_c, StrategyOutput(targets=(TargetPosition(sym, 0.2),)))
        self.assertAlmostEqual(0.5, res_g10_c.targets[0].target_weight, places=6)

    def test_exposure_cap_reversal_omission_cross_symbol_and_ef_replay_cr10_02_to_cr10_08(self) -> None:
        sym = "MU-USDT-SWAP"
        sym_b = "ETH-USDT-SWAP"
        sym_c = "BTC-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_eth = _make_bar_series([100.0] * 5, symbol=sym_b, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_btc = _make_bar_series([100.0] * 5, symbol=sym_c, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu, (sym_b, "1h"): bars_eth, (sym_c, "1h"): bars_btc}, now_ms=t0)
        inst_5x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)

        # CR10-02: Infeasible reversal with short bracket on a long position MUST be omitted, never sign-flipped!
        ov_m10 = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity")
        pos_l06 = {sym: PositionView(sym, qty=60.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=6_000.0, leverage=0.6)}
        rest_s120 = OrderIntent("rs120", sym, "sell", "limit", "contracts", 120.0, limit_price=100.0)
        ctx_rev_l = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 10_000.0, pos_l06, (rest_s120,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        rev_short = TargetPosition(sym, -0.2, stop_price=110.0, take_profit=90.0, reason="short_rev")
        res_rev_omit = ov_m10.filter(ctx_rev_l, StrategyOutput(targets=(rev_short,)))
        self.assertEqual((), res_rev_omit.targets)

        # Short mirror of CR10-02: short -0.6x with resting 1.2x buy + long bracket reversal -> omitted!
        pos_s06 = {sym: PositionView(sym, qty=-60.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=6_000.0, leverage=0.6)}
        rest_b120 = OrderIntent("rb120", sym, "buy", "limit", "contracts", 120.0, limit_price=100.0)
        ctx_rev_s = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 10_000.0, pos_s06, (rest_b120,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        rev_long = TargetPosition(sym, 0.2, stop_price=90.0, take_profit=110.0, reason="long_rev")
        self.assertEqual((), ov_m10.filter(ctx_rev_s, StrategyOutput(targets=(rev_long,))).targets)

        # Feasible reversal: resting 0.7x sell + -0.5x short reversal -> clamped to -0.3x with bracket PRESERVED!
        rest_s70 = OrderIntent("rs70", sym, "sell", "limit", "contracts", 70.0, limit_price=100.0)
        ctx_rev_ok = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 10_000.0, pos_l06, (rest_s70,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        rev_feas = TargetPosition(sym, -0.5, stop_price=110.0, take_profit=90.0, reason="short_rev")
        res_rev_ok = ov_m10.filter(ctx_rev_ok, StrategyOutput(targets=(rev_feas,)))
        self.assertEqual(1, len(res_rev_ok.targets))
        self.assertAlmostEqual(-0.3, res_rev_ok.targets[0].target_weight, places=9)
        self.assertEqual(110.0, res_rev_ok.targets[0].stop_price)
        self.assertEqual(90.0, res_rev_ok.targets[0].take_profit)

        # CR10-03 & CR10-05: A long 80 @ 75 (settled=8k, marked=10k), resting B buy 1.05 (0.84x), cap 2x gross / 1x single.
        ov_2x1x = ExposureCapOverlay(2.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        pos_a80 = {sym: PositionView(sym, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8)}
        rest_b105 = OrderIntent("rb105", sym_b, "buy", "limit", "equity_fraction", 1.05, limit_price=100.0)
        ctx_ab = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_a80, (rest_b105,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        # TargetPosition(A, +0.1) would push B to 1.029x -> pulled to exact 5/21 so B == 1.000x!
        res_1003 = ov_2x1x.filter(ctx_ab, StrategyOutput(targets=(TargetPosition(sym, 0.1),)))
        self.assertEqual(1, len(res_1003.targets))
        self.assertAlmostEqual(5.0 / 21.0, res_1003.targets[0].target_weight, places=6)
        # TargetPosition(A, +0.5) leaves B at 0.945x <= 1.0x -> passes unchanged!
        res_1003_ok = ov_2x1x.filter(ctx_ab, StrategyOutput(targets=(TargetPosition(sym, 0.5),)))
        self.assertAlmostEqual(0.5, res_1003_ok.targets[0].target_weight, places=9)
        # Joint multi-target CR10-03: A=+0.5 and C=+0.5 each fit alone (B=0.945x), but together push B to 1.05x!
        pos_ac80 = {
            sym: PositionView(sym, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8),
            sym_c: PositionView(sym_c, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8),
        }
        ctx_abc = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 6_000.0, pos_ac80, (rest_b105,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        # At settled=6_000, A+C holding gives B = 0.63x; reducing both to 0.0 realizes +4_000 -> settled 10_000 -> B=1.05x!
        ov_3x1x = ExposureCapOverlay(3.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        res_joint = ov_3x1x.filter(ctx_abc, StrategyOutput(targets=(TargetPosition(sym, 0.0), TargetPosition(sym_c, 0.0))))
        self.assertEqual(2, len(res_joint.targets))
        # Combined remaining contracts on A+C must keep realized profit <= 9523.81 - 6000 = 3523.81 -> remaining >= 19.0476 contracts!
        rem_contracts = (res_joint.targets[0].target_weight + res_joint.targets[1].target_weight) * 60.0
        self.assertAlmostEqual(160.0 - (10_000.0 / 1.05 - 6_000.0) / 25.0, rem_contracts, places=4)

        # CR10-05: Step 4 target_weight intent and contracts sell intent check B's single-symbol cap too!
        res_1005_tw = ov_2x1x.filter(
            ctx_ab,
            StrategyOutput(intents=(OrderIntent("tw01", sym, "sell", "market", "target_weight", 0.1),)),
        )
        self.assertEqual(1, len(res_1005_tw.intents))
        self.assertAlmostEqual(5.0 / 21.0, res_1005_tw.intents[0].qty_value, places=6)
        res_1005_tw_ok = ov_2x1x.filter(
            ctx_ab,
            StrategyOutput(intents=(OrderIntent("tw05", sym, "sell", "market", "target_weight", 0.5),)),
        )
        self.assertAlmostEqual(0.5, res_1005_tw_ok.intents[0].qty_value, places=9)
        res_1005_c = ov_2x1x.filter(
            ctx_ab,
            StrategyOutput(intents=(OrderIntent("ms72", sym, "sell", "market", "contracts", 72.0),)),
        )
        self.assertAlmostEqual((10_000.0 / 1.05 - 8_000.0) / 25.0, res_1005_c.intents[0].qty_value, places=5)

        # CR10-04: Resting 0.5 sell limit + new 1.75 sell limit (settled=8k, 80 long @ 75, cap 1x) -> clamped to exact 14/9!
        ov_1x1x = ExposureCapOverlay(1.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        rest_s05 = OrderIntent("rs05", sym, "sell", "limit", "equity_fraction", 0.5, limit_price=100.0)
        new_s175 = OrderIntent("ns175", sym, "sell", "limit", "equity_fraction", 1.75, limit_price=100.0)
        ctx_1004 = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_a80, (rest_s05,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_1004 = ov_1x1x.filter(ctx_1004, StrategyOutput(intents=(new_s175,)))
        self.assertEqual(1, len(res_1004.intents))
        self.assertAlmostEqual(14.0 / 9.0, res_1004.intents[0].qty_value, places=6)
        # Same exact clamp when BOTH 0.5 and 1.75 are emitted together in the same output.intents!
        ctx_1004_pair = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_a80, (), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_1004_pair = ov_1x1x.filter(ctx_1004_pair, StrategyOutput(intents=(rest_s05, new_s175)))
        self.assertEqual(2, len(res_1004_pair.intents))
        self.assertAlmostEqual(0.5, res_1004_pair.intents[0].qty_value, places=9)
        self.assertAlmostEqual(14.0 / 9.0, res_1004_pair.intents[1].qty_value, places=6)

        # CR10-08: Under marked_equity sizing, resting ef orders aggregate directly in O(1) (0.3 + 0.4 = 0.7 -> room 0.3)!
        rest_b03 = OrderIntent("rb03", sym, "buy", "limit", "equity_fraction", 0.3, limit_price=100.0)
        rest_b04 = OrderIntent("rb04", sym, "buy", "limit", "equity_fraction", 0.4, limit_price=100.0)
        ctx_1008 = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, {}, (rest_b03, rest_b04), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_1008 = ov_m10.filter(
            ctx_1008,
            StrategyOutput(intents=(OrderIntent("nb05", sym, "buy", "limit", "equity_fraction", 0.5, limit_price=100.0),)),
        )
        self.assertEqual(1, len(res_1008.intents))
        self.assertAlmostEqual(0.3, res_1008.intents[0].qty_value, places=9)

    def test_exposure_cap_post_ef_target_weight_and_same_symbol_breach_cr11_01_to_cr11_03(self) -> None:
        sym = "MU-USDT-SWAP"
        sym_b = "ETH-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_eth = _make_bar_series([100.0] * 5, symbol=sym_b, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu, (sym_b, "1h"): bars_eth}, now_ms=t0)
        inst_5x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        ov_1x = ExposureCapOverlay(1.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        ov_2x1x = ExposureCapOverlay(2.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        pos_a80 = {sym: PositionView(sym, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8)}

        # CR11-01 (same-symbol): Resting A sell ef=0.5 (raises settled 8k -> 9k) + resting A buy target_weight=+1.0
        # -> resting target_weight expands to 0.90x after fractional replay, so a 15-contract fixed buy MUST clamp to 10.0!
        rest_s05 = OrderIntent("rs05", sym, "sell", "limit", "equity_fraction", 0.5, limit_price=100.0)
        rest_tw10_a = OrderIntent("rtw10a", sym, "buy", "limit", "target_weight", 1.0, limit_price=100.0)
        ctx_1101_a = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_a80, (rest_s05, rest_tw10_a), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_1101_a = ov_1x.filter(
            ctx_1101_a,
            StrategyOutput(intents=(OrderIntent("mb15", sym, "buy", "market", "contracts", 15.0),)),
        )
        self.assertEqual(1, len(res_1101_a.intents))
        self.assertAlmostEqual(10.0, res_1101_a.intents[0].qty_value, places=6)

        # CR11-01 (cross-symbol): Resting A sell ef=0.5 + resting B buy target_weight=+1.05 (0.84x pre-replay -> 0.945x post-replay)
        # -> a 10-contract B fixed buy under 1.0x single cap MUST clamp to exact 5.5 contracts (0.055x)!
        rest_tw105_b = OrderIntent("rtw105b", sym_b, "buy", "limit", "target_weight", 1.05, limit_price=100.0)
        ctx_1101_b = BarContext(
            now_ms=t0,
            symbol=sym_b,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_a80, (rest_s05, rest_tw105_b), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_1101_b = ov_2x1x.filter(
            ctx_1101_b,
            StrategyOutput(intents=(OrderIntent("mb10", sym_b, "buy", "market", "contracts", 10.0),)),
        )
        self.assertEqual(1, len(res_1101_b.intents))
        self.assertAlmostEqual(5.5, res_1101_b.intents[0].qty_value, places=6)

        # CR11-02: 80 long @ 75, settled=8_000, mark=100, 1x cap.
        # Submit 0.5 ef market sell then +1.2 target_weight market buy -> sell raises ratio 0.8 -> 0.9, so +1.2 clamps to 10/9!
        ctx_1102 = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_a80, (), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_1102 = ov_1x.filter(
            ctx_1102,
            StrategyOutput(
                intents=(
                    OrderIntent("ms05", sym, "sell", "market", "equity_fraction", 0.5),
                    OrderIntent("tw12", sym, "buy", "market", "target_weight", 1.2),
                )
            ),
        )
        self.assertEqual(2, len(res_1102.intents))
        self.assertAlmostEqual(0.5, res_1102.intents[0].qty_value, places=9)
        self.assertAlmostEqual(10.0 / 9.0, res_1102.intents[1].qty_value, places=6)

        # Without the 0.5 sell, +1.2 target_weight (96 contracts = 0.96x) STILL passes unchanged!
        res_1102_solo = ov_1x.filter(
            ctx_1102,
            StrategyOutput(intents=(OrderIntent("tw12", sym, "buy", "market", "target_weight", 1.2),)),
        )
        self.assertEqual(1, len(res_1102_solo.intents))
        self.assertAlmostEqual(1.2, res_1102_solo.intents[0].qty_value, places=9)

        # Reversed non-market order: limit +1.2 target_weight buy then 0.5 ef market sell -> sell clamps to keep 1.2 <= 1.0x!
        res_1102_rev = ov_1x.filter(
            ctx_1102,
            StrategyOutput(
                intents=(
                    OrderIntent("tw12l", sym, "buy", "limit", "target_weight", 1.2, limit_price=100.0),
                    OrderIntent("ms05", sym, "sell", "market", "equity_fraction", 0.5),
                )
            ),
        )
        self.assertEqual(2, len(res_1102_rev.intents))
        self.assertAlmostEqual(1.2, res_1102_rev.intents[0].qty_value, places=9)
        # Max settled equity allowed for 1.2 target_weight under 1.0x cap is 10_000 / 1.2 = 8_333.33 -> gain 333.33 -> 13.33 contracts -> ef = 1/6!
        self.assertAlmostEqual(1.0 / 6.0, res_1102_rev.intents[1].qty_value, places=6)

        # CR11-03: 0.6x long with resting 1.2x sell and 0.5x buy (pre_lo=-0.6x, pre_hi=1.1x existing breach), 1x cap.
        # A sell target_weight intent of 0.0 moves short bound -0.6x -> -1.2x (NEW breach!) -> MUST be rejected!
        ov_m10 = ExposureCapOverlay(1.0, 1.0, sizing_basis="marked_equity")
        pos_l06 = {sym: PositionView(sym, qty=60.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=6_000.0, leverage=0.6)}
        rest_s120 = OrderIntent("rs120", sym, "sell", "limit", "contracts", 120.0, limit_price=100.0)
        rest_b50 = OrderIntent("rb50", sym, "buy", "limit", "contracts", 50.0, limit_price=100.0)
        ctx_1103 = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 10_000.0, pos_l06, (rest_s120, rest_b50), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_1103_rej = ov_m10.filter(
            ctx_1103,
            StrategyOutput(intents=(OrderIntent("tw0", sym, "sell", "market", "target_weight", 0.0),)),
        )
        self.assertEqual((), res_1103_rej.intents)
        # A sell target_weight intent of +0.2 leaves short bound -1.0x (within cap) and does not worsen 1.1x long bound -> allowed!
        res_1103_ok = ov_m10.filter(
            ctx_1103,
            StrategyOutput(intents=(OrderIntent("tw02", sym, "sell", "market", "target_weight", 0.2),)),
        )
        self.assertEqual(1, len(res_1103_ok.intents))
        self.assertAlmostEqual(0.2, res_1103_ok.intents[0].qty_value, places=9)
        # Step 2 TargetPosition(0.0) vs TargetPosition(0.2) enforces the exact same invariant!
        self.assertEqual((), ov_m10.filter(ctx_1103, StrategyOutput(targets=(TargetPosition(sym, 0.0),))).targets)
        res_1103_tp_ok = ov_m10.filter(ctx_1103, StrategyOutput(targets=(TargetPosition(sym, 0.2),)))
        self.assertEqual(1, len(res_1103_tp_ok.targets))
        self.assertAlmostEqual(0.2, res_1103_tp_ok.targets[0].target_weight, places=9)
        # Same CR11-03 check holds when resting orders under marked_equity are equity_fraction orders!
        ctx_1103_ef = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(
                10_000.0,
                10_000.0,
                pos_l06,
                (
                    OrderIntent("rsef12", sym, "sell", "limit", "equity_fraction", 1.2, limit_price=100.0),
                    OrderIntent("rbef05", sym, "buy", "limit", "equity_fraction", 0.5, limit_price=100.0),
                ),
                10_000.0,
                10_000.0,
            ),
            instrument=inst_5x,
        )
        self.assertEqual(
            (),
            ov_m10.filter(
                ctx_1103_ef,
                StrategyOutput(intents=(OrderIntent("tw0", sym, "sell", "market", "target_weight", 0.0),)),
            ).intents,
        )

    def test_exposure_cap_other_symbol_per_side_signed_bounds_cr12_01_and_cr12_02(self) -> None:
        sym = "MU-USDT-SWAP"
        sym_b = "ETH-USDT-SWAP"
        sym_c = "BTC-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_mu = _make_bar_series([100.0] * 5, symbol=sym, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_eth = _make_bar_series([100.0] * 5, symbol=sym_b, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_btc = _make_bar_series([100.0] * 5, symbol=sym_c, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym, "1h"): bars_mu, (sym_b, "1h"): bars_eth, (sym_c, "1h"): bars_btc}, now_ms=t0)
        inst_5x = okx_swap_spec(sym, tick_size=Decimal("0.01"), max_leverage=5.0)
        ov = ExposureCapOverlay(3.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)

        # Symbol B has pre-existing long bound +1.20 (> 1.0x single cap) AND resting short target_weight -1.15
        # (-0.92x at settled=8_000). Reducing profitable long A (80 @ 75 -> settled 9_000) widens B's short
        # bound from -0.92 to -1.035 (NEW short-side breach!) while B's max(|lo|, |hi|) stays 1.20!
        pos_ab = {
            sym: PositionView(sym, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8),
            sym_b: PositionView(sym_b, qty=120.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=12_000.0, leverage=1.2),
        }
        rest_b_short = OrderIntent("rb_tw", sym_b, "sell", "limit", "target_weight", -1.15, limit_price=100.0)
        ctx_ab = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 8_000.0, pos_ab, (rest_b_short,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )

        # 1. CR12-02 Step 2: TargetPosition(A, +0.5) MUST be pulled to exact 15/23 (B short bound == -1.000x),
        #    while TargetPosition(A, +0.8) (B short bound -0.966x <= 1.0) passes unchanged!
        res_tp = ov.filter(ctx_ab, StrategyOutput(targets=(TargetPosition(sym, 0.5),)))
        self.assertEqual(1, len(res_tp.targets))
        self.assertAlmostEqual(15.0 / 23.0, res_tp.targets[0].target_weight, places=6)
        res_tp_ok = ov.filter(ctx_ab, StrategyOutput(targets=(TargetPosition(sym, 0.8),)))
        self.assertAlmostEqual(0.8, res_tp_ok.targets[0].target_weight, places=9)

        # 2. CR12-02 Step 3 (joint multi-target): A and C each long 80 @ 75 (settled=6_000 -> B pre-bounds (-0.69, +1.20));
        #    reducing BOTH A and C to 0.0 would raise settled to 10_000 (B short bound -1.15 < -1.0!) -> pulled back
        #    so realized profit <= 10_000/1.15 - 6_000 = 2695.652 -> remaining A+C contracts == 160 - 2695.652/25!
        pos_abc = {
            sym: PositionView(sym, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8),
            sym_c: PositionView(sym_c, qty=80.0, avg_entry_price=75.0, unrealized_pnl=2_000.0, notional=8_000.0, leverage=0.8),
            sym_b: PositionView(sym_b, qty=120.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=12_000.0, leverage=1.2),
        }
        ctx_abc = BarContext(
            now_ms=t0,
            symbol=sym,
            market=mv,
            portfolio=PortfolioView(10_000.0, 6_000.0, pos_abc, (rest_b_short,), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        ov_4x = ExposureCapOverlay(4.0, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        res_joint = ov_4x.filter(ctx_abc, StrategyOutput(targets=(TargetPosition(sym, 0.0), TargetPosition(sym_c, 0.0))))
        self.assertEqual(2, len(res_joint.targets))
        rem_contracts = (res_joint.targets[0].target_weight + res_joint.targets[1].target_weight) * 60.0
        self.assertAlmostEqual(160.0 - (10_000.0 / 1.15 - 6_000.0) / 25.0, rem_contracts, places=4)

        # 3. CR12-01 Step 4: target_weight, contracts, and equity_fraction sell intents on A all catch B's short breach!
        res_tw = ov.filter(
            ctx_ab,
            StrategyOutput(intents=(OrderIntent("tw05", sym, "sell", "market", "target_weight", 0.5),)),
        )
        self.assertEqual(1, len(res_tw.intents))
        self.assertAlmostEqual(15.0 / 23.0, res_tw.intents[0].qty_value, places=6)
        res_tw_ok = ov.filter(
            ctx_ab,
            StrategyOutput(intents=(OrderIntent("tw08", sym, "sell", "market", "target_weight", 0.8),)),
        )
        self.assertAlmostEqual(0.8, res_tw_ok.intents[0].qty_value, places=9)

        res_c = ov.filter(
            ctx_ab,
            StrategyOutput(intents=(OrderIntent("ms40", sym, "sell", "market", "contracts", 40.0),)),
        )
        self.assertEqual(1, len(res_c.intents))
        self.assertAlmostEqual(640.0 / 23.0, res_c.intents[0].qty_value, places=5)
        res_c_ok = ov.filter(
            ctx_ab,
            StrategyOutput(intents=(OrderIntent("ms20", sym, "sell", "market", "contracts", 20.0),)),
        )
        self.assertAlmostEqual(20.0, res_c_ok.intents[0].qty_value, places=9)

        res_ef = ov.filter(
            ctx_ab,
            StrategyOutput(intents=(OrderIntent("ef05", sym, "sell", "market", "equity_fraction", 0.5),)),
        )
        self.assertEqual(1, len(res_ef.intents))
        self.assertAlmostEqual(8.0 / 23.0, res_ef.intents[0].qty_value, places=6)

    def test_exposure_cap_step3_fallback_enforces_gross_cap_cr13_01(self) -> None:
        sym_a = "MU-USDT-SWAP"
        sym_b = "ETH-USDT-SWAP"
        sym_c = "BTC-USDT-SWAP"
        t0 = _utc_ms(2026, 6, 11, 14, 0)
        bars_a = _make_bar_series([100.0] * 5, symbol=sym_a, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_b = _make_bar_series([100.0] * 5, symbol=sym_b, interval="1h", start_ms=t0 - 5 * 3_600_000)
        bars_c = _make_bar_series([100.0] * 5, symbol=sym_c, interval="1h", start_ms=t0 - 5 * 3_600_000)
        mv = MarketView({(sym_a, "1h"): bars_a, (sym_b, "1h"): bars_b, (sym_c, "1h"): bars_c}, now_ms=t0)
        inst_5x = okx_swap_spec(sym_a, tick_size=Decimal("0.01"), max_leverage=5.0)

        # Construct exact CR13-01 scenario:
        # - Marked equity = 10_000, settled equity = 10_000 (C has 10 contracts long @ 400 -> -3_000 unrealized,
        #   so a reduce_only market sell on C lowers target_lower_ratio to 0.7 while target_upper_ratio stays 1.0!).
        # - A and B each hold 0.6x long with a 1.2x resting sell (no-target gross = 0.6 + 0.6 + 0.1 = 1.3 <= 1.4!).
        # - Targets A=+0.2, B=+0.2 get pulled in Step 2 to 2/7 (~0.2857, worst 1.0 each -> pre-fallback gross 2.1).
        # - cand_h1 pulls A and B to hold_w = 0.6/0.7 = 6/7 (~0.8571 each -> cand_h1 gross 1.8143 < 2.1, yet > 1.4!).
        pos_abc = {
            sym_a: PositionView(sym_a, qty=60.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=6_000.0, leverage=0.6),
            sym_b: PositionView(sym_b, qty=60.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=6_000.0, leverage=0.6),
            sym_c: PositionView(sym_c, qty=10.0, avg_entry_price=400.0, unrealized_pnl=-3_000.0, notional=1_000.0, leverage=0.1),
        }
        rest_a = OrderIntent("ra120", sym_a, "sell", "limit", "contracts", 120.0, limit_price=100.0)
        rest_b = OrderIntent("rb120", sym_b, "sell", "limit", "contracts", 120.0, limit_price=100.0)
        ro_c = OrderIntent("roc", sym_c, "sell", "market", "close_all", 1.0, reduce_only=True)
        ctx = BarContext(
            now_ms=t0,
            symbol=sym_a,
            market=mv,
            portfolio=PortfolioView(10_000.0, 10_000.0, pos_abc, (rest_a, rest_b), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        out = StrategyOutput(
            targets=(TargetPosition(sym_a, 0.2), TargetPosition(sym_b, 0.2)),
            intents=(ro_c,),
        )

        # 1. Under max_gross_leverage=1.4: neither cand_h1 (1.8143) nor pre-fallback (2.1) nor any single
        #    target (1.7) satisfies 1.4 -> MUST restore the compliant no-target envelope (targets == ())!
        ov_14 = ExposureCapOverlay(1.4, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        res_14 = ov_14.filter(ctx, out)
        self.assertEqual((), res_14.targets)

        # 2. Under max_gross_leverage=1.75: cand_h1 (1.8143) STILL exceeds 1.75 and MUST be rejected,
        #    while keeping just [A] has gross 1.0 + 0.6 + 0.1 = 1.70 <= 1.75 -> emits compliant [A]!
        ov_175 = ExposureCapOverlay(1.75, 1.0, sizing_basis="settled_equity", enforce_instrument_max_leverage=True)
        res_175 = ov_175.filter(ctx, out)
        self.assertEqual(1, len(res_175.targets))
        self.assertEqual(sym_a, res_175.targets[0].symbol)
        self.assertAlmostEqual(0.2 / 0.7, res_175.targets[0].target_weight, places=6)

        # 3. Pre-existing gross breach (no_target_gross = 1.8 > 1.4): risk-reducing flatten that does NOT
        #    worsen no_target_gross (1.8 <= 1.8) is still admitted cleanly!
        pos_over = {
            sym_a: PositionView(sym_a, qty=100.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=10_000.0, leverage=1.0),
            sym_b: PositionView(sym_b, qty=80.0, avg_entry_price=100.0, unrealized_pnl=0.0, notional=8_000.0, leverage=0.8),
        }
        ctx_over = BarContext(
            now_ms=t0,
            symbol=sym_b,
            market=mv,
            portfolio=PortfolioView(10_000.0, 10_000.0, pos_over, (), 10_000.0, 10_000.0),
            instrument=inst_5x,
        )
        res_over = ov_14.filter(ctx_over, StrategyOutput(targets=(TargetPosition(sym_b, 0.0),)))
        self.assertEqual(1, len(res_over.targets))
        self.assertAlmostEqual(0.0, res_over.targets[0].target_weight, places=9)


if __name__ == "__main__":
    unittest.main()
