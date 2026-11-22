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


if __name__ == "__main__":
    unittest.main()
