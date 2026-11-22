"""Realistic fill matching, slippage evaluation, and Long/Short MMR liquidation model."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from mu_strategy.core.instrument import AtrBps, CostModel, FixedTicks, InstrumentSpec, SqrtVolumeImpact

_isfinite = math.isfinite
_sqrt = math.sqrt
_INF = math.inf

__all__ = [
    "ExecutionModel",
    "FillQuote",
    "IntrabarPolicy",
    "compute_liquidation_price",
    "compute_position_liquidation_price",
]

IntrabarPolicy = Literal["CONSERVATIVE"]
FillOrderType = Literal["market", "limit", "stop_market", "stop_limit", "liquidation"]
LiquidityRole = Literal["maker", "taker"]


def _clean_zero(value: float) -> float:
    return 0.0 if value == 0.0 else float(value)


def compute_position_liquidation_price(
    *,
    side: Literal["long", "short"],
    entry_price: float,
    qty: float,
    margin_equity: float,
    maintenance_margin_rate: float,
    liquidation_fee_bps: float = 0.0,
    incurred_fees: float = 0.0,
    accrued_funding: float = 0.0,
) -> float | None:
    """Compute exact MMR liquidation price for a long or short position.

    Solves ``equity(P) == (mmr + liq_fee_rate) * qty * P`` where
    ``equity(P) = (margin_equity - incurred_fees + accrued_funding) + signed_qty * (P - entry_price)``.

    Returns ``None`` for unlevered / fully-collateralized longs whose equity
    remains non-negative even as price approaches 0.
    """
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    if entry_price <= 0.0 or not math.isfinite(entry_price):
        raise ValueError(f"entry_price must be positive finite, got {entry_price!r}")
    if qty <= 0.0 or not math.isfinite(qty):
        raise ValueError(f"qty must be positive finite, got {qty!r}")
    if maintenance_margin_rate < 0.0 or maintenance_margin_rate >= 1.0:
        raise ValueError(f"maintenance_margin_rate must be in [0, 1), got {maintenance_margin_rate!r}")
    if liquidation_fee_bps < 0.0 or not math.isfinite(liquidation_fee_bps):
        raise ValueError(f"liquidation_fee_bps must be non-negative finite, got {liquidation_fee_bps!r}")

    effective_cash = margin_equity - incurred_fees + accrued_funding
    eff_mmr = maintenance_margin_rate + (liquidation_fee_bps / 10_000.0)
    if eff_mmr >= 1.0:
        eff_mmr = 0.999999

    if side == "long":
        numerator = (qty * entry_price) - effective_cash
        if numerator <= 0.0:
            return None
        denom = qty * (1.0 - eff_mmr)
        liq_px = numerator / denom
        return _clean_zero(liq_px) if liq_px > 0.0 else None

    # Short position: unbounded upside risk -> always finite positive liquidation price.
    numerator = (qty * entry_price) + effective_cash
    if numerator <= 0.0:
        return 1e-12
    denom = qty * (1.0 + eff_mmr)
    return _clean_zero(numerator / denom)


def compute_liquidation_price(
    *,
    qty: float,
    avg_entry_price: float,
    cash: float,
    cost_model: CostModel,
    asset_class: str = "perp",
) -> float | None:
    """Compute liquidation price from account cash (already net of entry fees & accrued funding)."""
    if qty == 0.0 or avg_entry_price <= 0.0:
        return None
    if asset_class in ("spot", "equity") and qty > 0.0:
        return None
    side: Literal["long", "short"] = "long" if qty > 0.0 else "short"
    return compute_position_liquidation_price(
        side=side,
        entry_price=avg_entry_price,
        qty=abs(qty),
        margin_equity=cash,
        maintenance_margin_rate=cost_model.maintenance_margin_rate,
        liquidation_fee_bps=cost_model.liquidation_fee_bps,
        incurred_fees=0.0,
        accrued_funding=0.0,
    )


@dataclass(frozen=True, slots=True)
class FillQuote:
    """Single executed fill with separated reference price, execution price, fee, and slippage."""

    side: Literal["buy", "sell"]
    order_type: FillOrderType
    ref_price: float
    exec_price: float
    qty: float
    notional: float
    fee: float
    slippage_cost: float
    liquidity: LiquidityRole
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionModel:
    """Deterministic OHLC bar execution and conservative protective-exit matching model."""

    instrument: InstrumentSpec
    cost_model: CostModel
    intrabar_policy: IntrabarPolicy = "CONSERVATIVE"
    require_touch_through: bool = False
    min_price: float = 1e-12
    _tick_size: float = field(init=False, repr=False, compare=False)
    _half_spread_rate: float = field(init=False, repr=False, compare=False)
    _taker_fee_rate: float = field(init=False, repr=False, compare=False)
    _maker_fee_rate: float = field(init=False, repr=False, compare=False)
    _liq_total_fee_rate: float = field(init=False, repr=False, compare=False)
    _stop_slip_mult: float = field(init=False, repr=False, compare=False)
    _slip_kind: int = field(init=False, repr=False, compare=False)
    _slip_p1: float = field(init=False, repr=False, compare=False)
    _slip_p2: float = field(init=False, repr=False, compare=False)
    _slip_p1_stop: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.intrabar_policy != "CONSERVATIVE":
            raise ValueError(f"unsupported intrabar_policy: {self.intrabar_policy!r}")
        if not isinstance(self.require_touch_through, bool):
            raise ValueError("require_touch_through must be a bool")
        if self.min_price <= 0.0 or not _isfinite(self.min_price):
            raise ValueError("min_price must be a positive finite number")

        tick_sz = self.instrument.tick_size_float
        cm = self.cost_model
        stop_mult = cm.stop_slippage_multiplier
        taker_r = cm.taker_fee_rate
        object.__setattr__(self, "_tick_size", tick_sz)
        object.__setattr__(self, "_half_spread_rate", cm.half_spread_rate)
        object.__setattr__(self, "_taker_fee_rate", taker_r)
        object.__setattr__(self, "_maker_fee_rate", cm.maker_fee_rate)
        object.__setattr__(self, "_liq_total_fee_rate", taker_r + cm.liquidation_fee_rate)
        object.__setattr__(self, "_stop_slip_mult", stop_mult)

        sm = cm.slippage_model
        if isinstance(sm, FixedTicks):
            if sm.ticks == 0.0:
                kind, p1, p2, p1s = 0, 0.0, 0.0, 0.0
            else:
                kind = 1
                p1 = sm.ticks * tick_sz
                p2 = 0.0
                p1s = p1 * stop_mult
        elif isinstance(sm, AtrBps):
            if sm.bps_per_atr == 0.0:
                kind, p1, p2, p1s = 0, 0.0, 0.0, 0.0
            else:
                kind = 2
                p1 = sm.bps_per_atr / 10_000.0
                p2 = 0.0
                p1s = p1 * stop_mult
        elif isinstance(sm, SqrtVolumeImpact):
            if sm.impact_coeff == 0.0 or sm.max_bps == 0.0:
                kind, p1, p2, p1s = 0, 0.0, 0.0, 0.0
            else:
                kind = 3
                p1 = sm.impact_coeff
                p2 = sm.max_bps
                p1s = stop_mult
        else:
            kind, p1, p2, p1s = 4, 0.0, 0.0, 0.0
        object.__setattr__(self, "_slip_kind", kind)
        object.__setattr__(self, "_slip_p1", p1)
        object.__setattr__(self, "_slip_p2", p2)
        object.__setattr__(self, "_slip_p1_stop", p1s)

    def compute_slippage_price(
        self,
        ref_price: float,
        *,
        order_notional: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None = None,
        bar_high: float | None = None,
        bar_low: float | None = None,
        is_stop: bool = False,
    ) -> float:
        """Compute adverse price slippage (excluding half-spread) for one fill."""
        kind = self._slip_kind
        if kind == 0:
            return 0.0
        if kind == 1:
            return self._slip_p1_stop if is_stop else self._slip_p1
        if kind == 2:
            eff_atr = bar_atr
            if eff_atr is None or not (0.0 < eff_atr < _INF):
                if bar_high is not None and bar_low is not None and bar_high > bar_low:
                    eff_atr = bar_high - bar_low
                else:
                    return 0.0
            return eff_atr * (self._slip_p1_stop if is_stop else self._slip_p1)
        if kind == 3:
            if not (0.0 < order_notional < _INF):
                return 0.0
            bar_usd_vol = bar_close * bar_volume
            if not (0.0 < bar_usd_vol < _INF):
                impact_bps = self._slip_p2
            else:
                raw_bps = self._slip_p1 * _sqrt(order_notional / bar_usd_vol)
                impact_bps = raw_bps if raw_bps < self._slip_p2 else self._slip_p2
            base = ref_price * (impact_bps / 10_000.0)
            return (base * self._slip_p1_stop) if is_stop else base

        effective_atr = bar_atr
        if effective_atr is None or not _isfinite(effective_atr) or effective_atr <= 0.0:
            if bar_high is not None and bar_low is not None and bar_high >= bar_low:
                effective_atr = bar_high - bar_low
            else:
                effective_atr = None
        bar_usd_volume = max(0.0, bar_close * bar_volume)
        return self.cost_model.estimate_slippage_price(
            ref_price,
            tick_size=self._tick_size,
            atr=effective_atr,
            order_notional=max(0.0, order_notional),
            bar_usd_volume=bar_usd_volume,
            is_stop=is_stop,
        )

    def _adverse_exec_price(
        self,
        side: Literal["buy", "sell"],
        ref_price: float,
        *,
        order_notional: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None,
        is_stop: bool,
    ) -> float:
        kind = self._slip_kind
        if kind == 0:
            slip = 0.0
        elif kind == 1:
            slip = self._slip_p1_stop if is_stop else self._slip_p1
        else:
            slip = self.compute_slippage_price(
                ref_price,
                order_notional=order_notional,
                bar_close=bar_close,
                bar_volume=bar_volume,
                bar_atr=bar_atr,
                bar_high=bar_high,
                bar_low=bar_low,
                is_stop=is_stop,
            )
        adverse = (ref_price * self._half_spread_rate) + slip
        if side == "buy":
            return ref_price + adverse
        exec_px = ref_price - adverse
        return exec_px if exec_px > self.min_price else self.min_price

    def _make_quote(
        self,
        *,
        side: Literal["buy", "sell"],
        order_type: FillOrderType,
        ref_price: float,
        exec_price: float,
        qty: float,
        fee_rate: float,
        liquidity: LiquidityRole,
        reason: str = "",
    ) -> FillQuote:
        notional = exec_price * qty
        fee = notional * fee_rate
        slippage_cost = (exec_price - ref_price) * (qty if side == "buy" else -qty)
        return FillQuote(
            side=side,
            order_type=order_type,
            ref_price=0.0 if ref_price == 0.0 else ref_price,
            exec_price=0.0 if exec_price == 0.0 else exec_price,
            qty=0.0 if qty == 0.0 else qty,
            notional=0.0 if notional == 0.0 else notional,
            fee=0.0 if fee == 0.0 else fee,
            slippage_cost=0.0 if slippage_cost == 0.0 else slippage_cost,
            liquidity=liquidity,
            reason=reason,
        )

    def market_exec_price(
        self,
        side: Literal["buy", "sell"],
        *,
        order_notional: float,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None = None,
    ) -> tuple[float, float]:
        """Return ``(ref_price, exec_price)`` for a market order at bar open."""
        ref_price = bar_open
        exec_price = self._adverse_exec_price(
            side,
            ref_price,
            order_notional=order_notional,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_atr=bar_atr,
            is_stop=False,
        )
        return ref_price, exec_price

    def fill_market(
        self,
        side: Literal["buy", "sell"],
        qty: float,
        *,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None = None,
        reason: str = "market",
    ) -> FillQuote:
        """Fill a market order at ``bar.open +/- half_spread +/- slippage`` (taker fee)."""
        if qty <= 0.0:
            raise ValueError(f"qty must be positive, got {qty}")
        ref_price, exec_price = self.market_exec_price(
            side,
            order_notional=bar_open * qty,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_atr=bar_atr,
        )
        return self._make_quote(
            side=side,
            order_type="market",
            ref_price=ref_price,
            exec_price=exec_price,
            qty=qty,
            fee_rate=self._taker_fee_rate,
            liquidity="taker",
            reason=reason,
        )

    def limit_fill_price(
        self,
        side: Literal["buy", "sell"],
        limit_price: float,
        *,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        open_only: bool = False,
    ) -> float | None:
        """Return fill price for a resting limit order (with open-gap improvement), or None.

        When ``open_only=True``, the order is evaluated strictly at ``bar_open`` and
        never fills intrabar (fills at ``bar_open`` iff ``bar_open <= limit_price``
        for buys or ``bar_open >= limit_price`` for sells).
        """
        if open_only:
            if side == "buy":
                return bar_open if bar_open <= limit_price else None
            return bar_open if bar_open >= limit_price else None

        if side == "buy":
            if bar_open < limit_price:
                return bar_open
            if self.require_touch_through:
                if bar_low < limit_price:
                    return limit_price
                return None
            if bar_low <= limit_price:
                return limit_price
            return None

        # side == "sell"
        if bar_open > limit_price:
            return bar_open
        if self.require_touch_through:
            if bar_high > limit_price:
                return limit_price
            return None
        if bar_high >= limit_price:
            return limit_price
        return None

    def try_fill_limit(
        self,
        side: Literal["buy", "sell"],
        qty: float,
        limit_price: float,
        *,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        reason: str = "limit",
        open_only: bool = False,
    ) -> FillQuote | None:
        """Fill a passive limit order if bar crosses ``limit_price`` (maker fee, 0 adverse slip)."""
        if qty <= 0.0:
            raise ValueError(f"qty must be positive, got {qty}")
        fill_px = self.limit_fill_price(
            side,
            limit_price,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
            open_only=open_only,
        )
        if fill_px is None:
            return None
        return self._make_quote(
            side=side,
            order_type="limit",
            ref_price=fill_px,
            exec_price=fill_px,
            qty=qty,
            fee_rate=self._maker_fee_rate,
            liquidity="maker",
            reason=reason,
        )

    def stop_trigger_ref_price(
        self,
        side: Literal["buy", "sell"],
        stop_price: float,
        *,
        bar_open: float,
        bar_high: float,
        bar_low: float,
    ) -> float | None:
        """Return pre-slippage trigger price for a stop order (worse of open/stop), or None."""
        if side == "sell":
            if bar_low > stop_price:
                return None
            return bar_open if bar_open < stop_price else stop_price
        # side == "buy"
        if bar_high < stop_price:
            return None
        return bar_open if bar_open > stop_price else stop_price

    def try_fill_stop_market(
        self,
        side: Literal["buy", "sell"],
        qty: float,
        stop_price: float,
        *,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None = None,
        reason: str = "stop_market",
    ) -> FillQuote | None:
        """Fill a stop-market order with ``stop_slippage_multiplier`` and taker fee."""
        if qty <= 0.0:
            raise ValueError(f"qty must be positive, got {qty}")
        ref_price = self.stop_trigger_ref_price(
            side,
            stop_price,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
        )
        if ref_price is None:
            return None
        exec_price = self._adverse_exec_price(
            side,
            ref_price,
            order_notional=ref_price * qty,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_atr=bar_atr,
            is_stop=True,
        )
        return self._make_quote(
            side=side,
            order_type="stop_market",
            ref_price=ref_price,
            exec_price=exec_price,
            qty=qty,
            fee_rate=self._taker_fee_rate,
            liquidity="taker",
            reason=reason,
        )

    def evaluate_stop_limit(
        self,
        side: Literal["buy", "sell"],
        qty: float,
        stop_price: float,
        limit_price: float,
        *,
        activated: bool = False,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None = None,
        reason: str = "stop_limit",
    ) -> tuple[FillQuote | None, bool]:
        """Evaluate a stop-limit order, returning ``(quote_or_none, now_activated)``.

        Once the stop trigger is reached, ``now_activated`` becomes ``True`` and
        subsequent bars match the order purely as a resting limit at ``limit_price``
        without re-testing ``stop_price``.
        """
        if qty <= 0.0:
            raise ValueError(f"qty must be positive, got {qty}")
        if activated:
            quote = self.try_fill_limit(
                side,
                qty,
                limit_price,
                bar_open=bar_open,
                bar_high=bar_high,
                bar_low=bar_low,
                reason=reason,
            )
            return quote, True

        ref_price = self.stop_trigger_ref_price(
            side,
            stop_price,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
        )
        if ref_price is None:
            return None, False

        if side == "buy":
            if ref_price > limit_price:
                # Activated at the open above limit_price: rests as a buy limit for the bar.
                if bar_open >= stop_price:
                    reached = (bar_low < limit_price) if self.require_touch_through else (bar_low <= limit_price)
                    if reached:
                        return (
                            self._make_quote(
                                side="buy",
                                order_type="limit",
                                ref_price=limit_price,
                                exec_price=limit_price,
                                qty=qty,
                                fee_rate=self._maker_fee_rate,
                                liquidity="maker",
                                reason=reason,
                            ),
                            True,
                        )
                return None, True
            raw_exec = self._adverse_exec_price(
                "buy",
                ref_price,
                order_notional=ref_price * qty,
                bar_high=bar_high,
                bar_low=bar_low,
                bar_close=bar_close,
                bar_volume=bar_volume,
                bar_atr=bar_atr,
                is_stop=True,
            )
            exec_price = raw_exec if raw_exec <= limit_price else limit_price
        else:
            if ref_price < limit_price:
                # Activated at the open below limit_price: rests as a sell limit for the bar.
                if bar_open <= stop_price:
                    reached = (bar_high > limit_price) if self.require_touch_through else (bar_high >= limit_price)
                    if reached:
                        return (
                            self._make_quote(
                                side="sell",
                                order_type="limit",
                                ref_price=limit_price,
                                exec_price=limit_price,
                                qty=qty,
                                fee_rate=self._maker_fee_rate,
                                liquidity="maker",
                                reason=reason,
                            ),
                            True,
                        )
                return None, True
            raw_exec = self._adverse_exec_price(
                "sell",
                ref_price,
                order_notional=ref_price * qty,
                bar_high=bar_high,
                bar_low=bar_low,
                bar_close=bar_close,
                bar_volume=bar_volume,
                bar_atr=bar_atr,
                is_stop=True,
            )
            exec_price = raw_exec if raw_exec >= limit_price else limit_price

        return (
            self._make_quote(
                side=side,
                order_type="stop_limit",
                ref_price=ref_price,
                exec_price=exec_price,
                qty=qty,
                fee_rate=self._taker_fee_rate,
                liquidity="taker",
                reason=reason,
            ),
            True,
        )

    def try_fill_stop_limit(
        self,
        side: Literal["buy", "sell"],
        qty: float,
        stop_price: float,
        limit_price: float,
        *,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None = None,
        reason: str = "stop_limit",
    ) -> FillQuote | None:
        """Fill a stop-limit order if stop triggers and fill respects ``limit_price`` (taker fee)."""
        quote, _ = self.evaluate_stop_limit(
            side,
            qty,
            stop_price,
            limit_price,
            activated=False,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_atr=bar_atr,
            reason=reason,
        )
        return quote

    def try_fill_liquidation(
        self,
        position_qty: float,
        liq_price: float | None,
        *,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None = None,
    ) -> FillQuote | None:
        """Fill a forced MMR liquidation for a long (qty > 0) or short (qty < 0) position."""
        if position_qty == 0.0 or liq_price is None or liq_price <= 0.0:
            return None
        close_side: Literal["buy", "sell"] = "sell" if position_qty > 0.0 else "buy"
        qty_abs = abs(position_qty)
        ref_price = self.stop_trigger_ref_price(
            close_side,
            liq_price,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
        )
        if ref_price is None:
            return None
        exec_price = self._adverse_exec_price(
            close_side,
            ref_price,
            order_notional=ref_price * qty_abs,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_atr=bar_atr,
            is_stop=True,
        )
        return self._make_quote(
            side=close_side,
            order_type="liquidation",
            ref_price=ref_price,
            exec_price=exec_price,
            qty=qty_abs,
            fee_rate=self._liq_total_fee_rate,
            liquidity="taker",
            reason="liquidation",
        )

    def evaluate_protective_exits(
        self,
        position_qty: float,
        *,
        liq_price: float | None,
        stop_price: float | None,
        take_profit: float | None,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None = None,
        stop_reason: str = "stop_loss",
        tp_reason: str = "take_profit",
    ) -> FillQuote | None:
        """Resolve liquidation, stop-loss, and take-profit under CONSERVATIVE policy.

        Priority rules:
          1. Stop vs Liquidation (physical path ordering):
             - If bar opens already at/beyond ``liq_price``, liquidation wins.
             - Otherwise, an active stop strictly better than ``liq_price`` fills
               first before price reaches liquidation.
             - If stop is absent or at/worse than ``liq_price``, liquidation wins.
          2. CONSERVATIVE SL-before-TP:
             - Whenever both SL/liquidation and TP lie inside ``[bar_low, bar_high]``,
               SL/liquidation ALWAYS fills first (never optimistic TP-first).
        """
        if position_qty == 0.0:
            return None

        is_long = position_qty > 0.0
        close_side: Literal["buy", "sell"] = "sell" if is_long else "buy"
        qty_abs = abs(position_qty)

        # 0. Evaluate orders already marketable AT THE BAR OPEN before intrabar paths.
        #    Open-priority order: liquidation > stop-loss > marketable take-profit.
        liq_at_open = (
            liq_price is not None
            and liq_price > 0.0
            and ((bar_open <= liq_price) if is_long else (bar_open >= liq_price))
        )
        stop_at_open = (
            stop_price is not None
            and stop_price > 0.0
            and ((bar_open <= stop_price) if is_long else (bar_open >= stop_price))
        )
        if not liq_at_open and not stop_at_open and take_profit is not None and take_profit > 0.0:
            if is_long:
                tp_at_open = (bar_open > take_profit) if self.require_touch_through else (bar_open >= take_profit)
            else:
                tp_at_open = (bar_open < take_profit) if self.require_touch_through else (bar_open <= take_profit)
            if tp_at_open:
                return self._make_quote(
                    side=close_side,
                    order_type="limit",
                    ref_price=bar_open,
                    exec_price=bar_open,
                    qty=qty_abs,
                    fee_rate=self._maker_fee_rate,
                    liquidity="maker",
                    reason=tp_reason,
                )

        stop_ref = (
            self.stop_trigger_ref_price(
                close_side,
                stop_price,
                bar_open=bar_open,
                bar_high=bar_high,
                bar_low=bar_low,
            )
            if (stop_price is not None and stop_price > 0.0)
            else None
        )
        liq_ref = (
            self.stop_trigger_ref_price(
                close_side,
                liq_price,
                bar_open=bar_open,
                bar_high=bar_high,
                bar_low=bar_low,
            )
            if (liq_price is not None and liq_price > 0.0)
            else None
        )

        # 1. Resolve liquidation vs stop-loss priority.
        if liq_ref is not None:
            gapped_past_liq = (bar_open <= liq_price) if is_long else (bar_open >= liq_price)  # type: ignore[operator]
            stop_beats_liq = (
                not gapped_past_liq
                and stop_ref is not None
                and ((stop_ref > liq_price) if is_long else (stop_ref < liq_price))  # type: ignore[operator]
            )
            if stop_beats_liq:
                exec_px = self._adverse_exec_price(
                    close_side,
                    stop_ref,  # type: ignore[arg-type]
                    order_notional=stop_ref * qty_abs,  # type: ignore[operator]
                    bar_high=bar_high,
                    bar_low=bar_low,
                    bar_close=bar_close,
                    bar_volume=bar_volume,
                    bar_atr=bar_atr,
                    is_stop=True,
                )
                return self._make_quote(
                    side=close_side,
                    order_type="stop_market",
                    ref_price=stop_ref,  # type: ignore[arg-type]
                    exec_price=exec_px,
                    qty=qty_abs,
                    fee_rate=self._taker_fee_rate,
                    liquidity="taker",
                    reason=stop_reason,
                )
            exec_px = self._adverse_exec_price(
                close_side,
                liq_ref,
                order_notional=liq_ref * qty_abs,
                bar_high=bar_high,
                bar_low=bar_low,
                bar_close=bar_close,
                bar_volume=bar_volume,
                bar_atr=bar_atr,
                is_stop=True,
            )
            return self._make_quote(
                side=close_side,
                order_type="liquidation",
                ref_price=liq_ref,
                exec_price=exec_px,
                qty=qty_abs,
                fee_rate=self._liq_total_fee_rate,
                liquidity="taker",
                reason="liquidation",
            )

        if stop_ref is not None:
            exec_px = self._adverse_exec_price(
                close_side,
                stop_ref,
                order_notional=stop_ref * qty_abs,
                bar_high=bar_high,
                bar_low=bar_low,
                bar_close=bar_close,
                bar_volume=bar_volume,
                bar_atr=bar_atr,
                is_stop=True,
            )
            return self._make_quote(
                side=close_side,
                order_type="stop_market",
                ref_price=stop_ref,
                exec_price=exec_px,
                qty=qty_abs,
                fee_rate=self._taker_fee_rate,
                liquidity="taker",
                reason=stop_reason,
            )

        # 2. Only evaluate take-profit if neither liquidation nor stop-loss triggered (CONSERVATIVE).
        if take_profit is not None and take_profit > 0.0:
            return self.try_fill_limit(
                close_side,
                qty_abs,
                take_profit,
                bar_open=bar_open,
                bar_high=bar_high,
                bar_low=bar_low,
                reason=tp_reason,
            )

        return None
