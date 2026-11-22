"""Decoupled composable RiskOverlay protocol and production risk overlays for Layer 3."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from mu_strategy.core.trading_calendar import (
    US_EQUITIES_ALGORITHMIC_CALENDAR,
    calendar_sha256,
    is_trading_window_allowed,
)
from mu_strategy.engine.metrics import MS_PER_YEAR
from mu_strategy.strategies.protocol import (
    EMPTY_OUTPUT,
    BarContext,
    OrderIntent,
    StrategyOutput,
    TargetPosition,
)

__all__ = [
    "DailyLossLimitOverlay",
    "DrawdownCircuitBreaker",
    "ExposureCapOverlay",
    "RiskOverlay",
    "SessionWindowOverlay",
    "VolTargetOverlay",
    "apply_risk_overlays",
]

_MS_PER_DAY = 86_400_000


def _require_positive_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be a finite positive number, got {value!r}")
    return float(value)


def _require_fraction_01(name: str, value: float) -> float:
    val = _require_positive_finite(name, value)
    if val > 1.0:
        raise ValueError(f"{name} must be in (0, 1], got {value!r}")
    return val


def _is_reducing_intent(intent: OrderIntent, ctx: BarContext) -> bool:
    if intent.reduce_only or intent.qty_mode == "close_all":
        return True
    pos_qty = ctx.portfolio.position_qty(intent.symbol)
    if intent.qty_mode == "target_weight":
        if intent.qty_value == 0.0:
            return True
        if pos_qty == 0.0:
            return False
        equity = ctx.portfolio.equity
        if equity <= 0.0:
            return True
        pos = ctx.portfolio.position(intent.symbol)
        curr_weight = (pos.notional / equity) * (1.0 if pos_qty > 0.0 else -1.0) if pos is not None else 0.0
        return (curr_weight > 0.0 and 0.0 <= intent.qty_value <= curr_weight) or (
            curr_weight < 0.0 and curr_weight <= intent.qty_value <= 0.0
        )
    if pos_qty > 0.0 and intent.side == "sell":
        return True
    if pos_qty < 0.0 and intent.side == "buy":
        return True
    return False


def _flatten_output(ctx: BarContext, output: StrategyOutput, *, reason: str) -> StrategyOutput:
    """Drop all risk-increasing entries and emit explicit flat targets for open positions."""
    kept_intents = tuple(intent for intent in output.intents if intent.reduce_only or intent.qty_mode == "close_all")
    flatten_targets: list[TargetPosition] = []
    seen_symbols: set[str] = set()

    for sym, pos in ctx.portfolio.positions.items():
        if not pos.is_flat:
            flatten_targets.append(TargetPosition(symbol=sym, target_weight=0.0, reason=reason))
            seen_symbols.add(sym)

    # Also keep explicit flat targets emitted by strategy if not already added.
    for target in output.targets:
        if target.target_weight == 0.0 and target.symbol not in seen_symbols:
            flatten_targets.append(target)
            seen_symbols.add(target.symbol)

    if not kept_intents and not flatten_targets:
        if output.diagnostics:
            return StrategyOutput(diagnostics=output.diagnostics)
        return EMPTY_OUTPUT
    return StrategyOutput(
        intents=kept_intents,
        targets=tuple(flatten_targets),
        diagnostics=output.diagnostics,
    )


def _scale_intent_size(intent: OrderIntent, factor: float) -> OrderIntent:
    if intent.reduce_only or intent.qty_mode == "close_all":
        return intent
    new_val = intent.qty_value * factor
    if intent.qty_mode in ("contracts", "equity_fraction") and new_val <= 0.0:
        return intent
    return replace(intent, qty_value=new_val)


@runtime_checkable
class RiskOverlay(Protocol):
    """Composable risk filter applied to StrategyOutput before order queuing."""

    def reset(self) -> None: ...

    def filter(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput: ...

    def snapshot_state(self) -> dict[str, Any]: ...

    def restore_state(self, state: Mapping[str, Any]) -> None: ...


def apply_risk_overlays(
    overlays: Sequence[RiskOverlay],
    ctx: BarContext,
    output: StrategyOutput,
) -> StrategyOutput:
    """Apply a sequence of RiskOverlays in order."""
    current = output
    for overlay in overlays:
        current = overlay.filter(ctx, current)
    return current


class VolTargetOverlay:
    """Scale TargetPosition weights to a target annualized volatility.

    Strictly bounded by ``vol_floor`` and ``max_leverage`` so low-volatility
    regimes cannot produce unbounded leverage. Uses strictly causal closed bars
    from ``ctx.market`` (zero look-ahead).
    """

    def __init__(
        self,
        target_ann_vol: float,
        vol_lookback: int = 20,
        vol_floor: float = 0.05,
        max_leverage: float = 3.0,
        *,
        interval: str | None = None,
        periods_per_year: float | None = None,
        rebalance_tolerance: float = 0.10,
    ) -> None:
        self.target_ann_vol = _require_positive_finite("target_ann_vol", target_ann_vol)
        if isinstance(vol_lookback, bool) or not isinstance(vol_lookback, int) or vol_lookback < 2:
            raise ValueError(f"vol_lookback must be an int >= 2, got {vol_lookback!r}")
        self.vol_lookback = vol_lookback
        self.vol_floor = _require_positive_finite("vol_floor", vol_floor)
        self.max_leverage = _require_positive_finite("max_leverage", max_leverage)
        self.interval = interval
        if periods_per_year is not None:
            _require_positive_finite("periods_per_year", periods_per_year)
        self.periods_per_year = periods_per_year
        if (
            isinstance(rebalance_tolerance, bool)
            or not isinstance(rebalance_tolerance, (int, float))
            or not math.isfinite(rebalance_tolerance)
            or not (0.0 <= rebalance_tolerance < 1.0)
        ):
            raise ValueError(f"rebalance_tolerance must be in [0, 1), got {rebalance_tolerance!r}")
        self.rebalance_tolerance = float(rebalance_tolerance)
        self._last_scale: float | None = None
        self._last_realized_vol: float | None = None
        self._held_scales: dict[str, float] = {}

    def reset(self) -> None:
        self._last_scale = None
        self._last_realized_vol = None
        self._held_scales = {}

    def _resolve_interval(self, market: Any, symbol: str) -> str | None:
        if self.interval is not None:
            return self.interval
        if hasattr(market, "intervals"):
            intervals = market.intervals(symbol)
            if intervals:
                return intervals[0]
        if hasattr(market, "available_intervals"):
            intervals = market.available_intervals(symbol)
            if intervals:
                return intervals[0]
        for candidate in ("1h", "15m", "4h", "1d", "5m"):
            try:
                market.closed_bars(symbol, candidate)
                return candidate
            except (KeyError, ValueError):
                continue
        return None

    def _causal_realized_vol(self, ctx: BarContext, symbol: str) -> float | None:
        market = ctx.market
        if market is None:
            return None
        interval = self._resolve_interval(market, symbol)
        if interval is None:
            return None
        try:
            bars = market.closed_bars(symbol, interval)
        except (KeyError, ValueError):
            return None
        n = len(bars)
        if n < self.vol_lookback + 1:
            return None

        interval_ms = getattr(bars, "interval_ms", None)
        if interval_ms is None and hasattr(bars, "series"):
            interval_ms = getattr(bars.series, "interval_ms", None)
        ppy = (
            self.periods_per_year
            if self.periods_per_year is not None
            else (MS_PER_YEAR / interval_ms if interval_ms and interval_ms > 0 else 8760.0)
        )

        # Fast path: query shared read-only FeatureCache if attached to MarketView.
        features = getattr(market, "features", None) or getattr(market, "feature_cache", None)
        if features is not None:
            if hasattr(features, "realized_vol_value"):
                try:
                    val = features.realized_vol_value(bars, self.vol_lookback, periods_per_year=ppy, offset=-1)
                except IndexError:
                    val = None  # warmup: not enough closed bars yet
                if val is not None and math.isfinite(val):
                    return max(0.0, float(val))
            elif hasattr(features, "realized_vol"):
                rv_series = features.realized_vol(bars, self.vol_lookback, periods_per_year=ppy)
                val = rv_series[-1] if len(rv_series) > 0 else None
                if val is not None and math.isfinite(val):
                    return max(0.0, float(val))

        # Pure stdlib O(vol_lookback) fallback over strictly closed bars.
        base = getattr(bars, "_series", None) or getattr(bars, "series", None)
        if base is not None and hasattr(bars, "end"):
            closes = base.closes
            end_idx = bars.end
        else:
            closes = bars.closes
            end_idx = n

        start_idx = end_idx - self.vol_lookback
        log_returns: list[float] = []
        for idx in range(start_idx, end_idx):
            prev_c = closes[idx - 1]
            curr_c = closes[idx]
            if prev_c <= 0.0 or curr_c <= 0.0:
                return None
            log_returns.append(math.log(curr_c / prev_c))

        m = len(log_returns)
        if m < 2:
            return None
        mean_lr = math.fsum(log_returns) / m
        var = math.fsum((r - mean_lr) * (r - mean_lr) for r in log_returns) / (m - 1)
        std = math.sqrt(var) if var > 0.0 else 0.0
        return std * math.sqrt(ppy)

    def compute_scale(self, realized_vol: float | None) -> float:
        if realized_vol is None or not math.isfinite(realized_vol):
            return min(1.0, self.max_leverage)
        denom = max(max(0.0, realized_vol), self.vol_floor)
        return min(self.max_leverage, self.target_ann_vol / denom)

    def _apply_hysteresis(self, symbol: str, raw_scale: float) -> float:
        """Hold the previously applied scale while ``raw_scale`` stays within ``±rebalance_tolerance``."""
        tol = self.rebalance_tolerance
        held = self._held_scales.get(symbol)
        if tol > 0.0 and held is not None and held > 0.0 and abs(raw_scale / held - 1.0) <= tol:
            return held
        self._held_scales[symbol] = raw_scale
        return raw_scale

    def filter(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput:
        if not output.targets:
            return output

        scaled_targets: list[TargetPosition] = []
        cache_rv: dict[str, float | None] = {}
        for target in output.targets:
            if target.target_weight == 0.0:
                scaled_targets.append(target)
                continue
            sym = target.symbol
            if sym not in cache_rv:
                cache_rv[sym] = self._causal_realized_vol(ctx, sym)
            rv = cache_rv[sym]
            scale = self._apply_hysteresis(sym, self.compute_scale(rv))
            self._last_realized_vol = rv
            self._last_scale = scale
            raw_weight = target.target_weight * scale
            clamped = max(-self.max_leverage, min(self.max_leverage, raw_weight))
            if clamped == 0.0:
                clamped = 0.0
            scaled_targets.append(replace(target, target_weight=clamped))

        return StrategyOutput(
            intents=output.intents,
            targets=tuple(scaled_targets),
            diagnostics=output.diagnostics,
        )

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "last_scale": self._last_scale,
            "last_realized_vol": self._last_realized_vol,
            "held_scales": dict(self._held_scales),
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self._last_scale = state.get("last_scale")
        self._last_realized_vol = state.get("last_realized_vol")
        held = state.get("held_scales") or {}
        self._held_scales = {str(k): float(v) for k, v in held.items()}


class DrawdownCircuitBreaker:
    """Monitors running peak equity and halts/flattens or halves size on drawdown breach.

    When ``cooldown_bars`` is set to ``N``, the breaker stays active for ``N`` full
    bars after the trigger bar before resetting its high-water mark to current equity
    and resuming normal sizing.
    """

    def __init__(
        self,
        max_drawdown_pct: float,
        action: Literal["flatten_and_halt", "halve_size"] = "flatten_and_halt",
        cooldown_bars: int | None = None,
    ) -> None:
        self.max_drawdown_pct = _require_fraction_01("max_drawdown_pct", max_drawdown_pct)
        if action not in ("flatten_and_halt", "halve_size"):
            raise ValueError(f"unsupported action: {action!r}")
        self.action = action
        if cooldown_bars is not None:
            if isinstance(cooldown_bars, bool) or not isinstance(cooldown_bars, int) or cooldown_bars <= 0:
                raise ValueError(f"cooldown_bars must be a positive int or None, got {cooldown_bars!r}")
        self.cooldown_bars = cooldown_bars
        self._peak_equity: float | None = None
        self._active: bool = False
        self._cooldown_remaining: int = 0

    @property
    def is_triggered(self) -> bool:
        return self._active

    def reset(self) -> None:
        self._peak_equity = None
        self._active = False
        self._cooldown_remaining = 0

    def filter(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput:
        equity = ctx.portfolio.equity
        if self._peak_equity is None:
            self._peak_equity = max(equity, ctx.portfolio.peak_equity) if ctx.portfolio.peak_equity > 0.0 else equity

        if self._active:
            if self.cooldown_bars is not None:
                if self._cooldown_remaining > 0:
                    self._cooldown_remaining -= 1
                    return self._apply_active_action(ctx, output)
                # Cooldown complete: reset watermark to current equity and resume.
                self._active = False
                self._peak_equity = equity
            elif self.action == "halve_size" and self._peak_equity > 0.0 and equity >= self._peak_equity:
                self._active = False
                self._peak_equity = equity
            else:
                return self._apply_active_action(ctx, output)

        if equity > self._peak_equity:
            self._peak_equity = equity

        if self._peak_equity > 0.0:
            dd = (equity / self._peak_equity) - 1.0
            if dd <= -self.max_drawdown_pct + 1e-12:
                self._active = True
                if self.cooldown_bars is not None:
                    self._cooldown_remaining = self.cooldown_bars
                return self._apply_active_action(ctx, output)

        return output

    def _apply_active_action(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput:
        if self.action == "flatten_and_halt":
            return _flatten_output(ctx, output, reason="drawdown_circuit_breaker")
        # halve_size
        scaled_targets = tuple(
            replace(t, target_weight=(0.0 if t.target_weight == 0.0 else t.target_weight * 0.5))
            for t in output.targets
        )
        scaled_intents = tuple(_scale_intent_size(intent, 0.5) for intent in output.intents)
        return StrategyOutput(
            intents=scaled_intents,
            targets=scaled_targets,
            diagnostics=output.diagnostics,
        )

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "peak_equity": self._peak_equity,
            "active": self._active,
            "cooldown_remaining": self._cooldown_remaining,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self._peak_equity = state.get("peak_equity")
        self._active = bool(state.get("active", False))
        self._cooldown_remaining = int(state.get("cooldown_remaining", 0))


class DailyLossLimitOverlay:
    """Flattens and blocks new entries for the rest of the calendar day once daily loss breaches cap."""

    def __init__(self, max_daily_loss_pct: float, reset_tz: str = "UTC") -> None:
        self.max_daily_loss_pct = _require_fraction_01("max_daily_loss_pct", max_daily_loss_pct)
        if not isinstance(reset_tz, str) or not reset_tz.strip():
            raise ValueError("reset_tz must be a non-empty timezone name")
        self.reset_tz = reset_tz
        self._is_utc = reset_tz.upper() == "UTC"
        self._tz = timezone.utc if self._is_utc else ZoneInfo(reset_tz)
        self._current_day_key: int | None = None
        self._day_start_equity: float | None = None
        self._halted: bool = False

    @property
    def is_halted(self) -> bool:
        return self._halted

    def reset(self) -> None:
        self._current_day_key = None
        self._day_start_equity = None
        self._halted = False

    def _day_key(self, now_ms: int) -> int:
        if self._is_utc:
            return now_ms // _MS_PER_DAY
        return datetime.fromtimestamp(now_ms / 1000.0, tz=self._tz).date().toordinal()

    def filter(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput:
        day_key = self._day_key(ctx.now_ms)
        equity = ctx.portfolio.equity

        if day_key != self._current_day_key:
            self._current_day_key = day_key
            self._day_start_equity = equity
            self._halted = False

        if not self._halted and self._day_start_equity is not None and self._day_start_equity > 0.0:
            daily_ret = (equity / self._day_start_equity) - 1.0
            if daily_ret <= -self.max_daily_loss_pct + 1e-12:
                self._halted = True

        if self._halted:
            return _flatten_output(ctx, output, reason="daily_loss_limit")
        return output

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "current_day_key": self._current_day_key,
            "day_start_equity": self._day_start_equity,
            "halted": self._halted,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self._current_day_key = state.get("current_day_key")
        self._day_start_equity = state.get("day_start_equity")
        self._halted = bool(state.get("halted", False))


class ExposureCapOverlay:
    """Clamps signed target weights and risk-increasing order sizes to hard exposure limits."""

    def __init__(self, max_gross_leverage: float, max_single_weight: float) -> None:
        self.max_gross_leverage = _require_positive_finite("max_gross_leverage", max_gross_leverage)
        self.max_single_weight = _require_positive_finite("max_single_weight", max_single_weight)

    def reset(self) -> None:
        return None

    def _reference_price(self, ctx: BarContext, symbol: str) -> float | None:
        pos = ctx.portfolio.position(symbol)
        if pos is not None and pos.avg_entry_price > 0.0:
            return pos.avg_entry_price
        market = ctx.market
        if market is not None and hasattr(market, "latest_closed_bar"):
            for iv in ("15m", "1h", "5m", "1d"):
                try:
                    bar = market.latest_closed_bar(symbol, iv)
                    if bar is not None and bar.close > 0.0:
                        return float(bar.close)
                except (KeyError, ValueError):
                    continue
        return None

    def filter(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput:
        if not output.targets and not output.intents:
            return output

        single_cap = min(self.max_single_weight, self.max_gross_leverage)
        equity = ctx.portfolio.equity

        # Current weights by symbol (for multi-symbol gross cap accounting).
        existing_weights: dict[str, float] = {}
        if equity > 0.0:
            for sym, pos in ctx.portfolio.positions.items():
                if not pos.is_flat:
                    sign = 1.0 if pos.qty > 0.0 else -1.0
                    existing_weights[sym] = sign * (pos.notional / equity)

        # 1. Clamp individual TargetPositions to [-single_cap, +single_cap].
        clamped_targets: list[TargetPosition] = []
        targeted_symbols: set[str] = set()
        for t in output.targets:
            w = max(-single_cap, min(single_cap, t.target_weight))
            if w == 0.0:
                w = 0.0
            clamped_targets.append(replace(t, target_weight=w))
            targeted_symbols.add(t.symbol)

        # 2. Enforce max_gross_leverage across all targeted + untouched open positions.
        if clamped_targets:
            untouched_gross = math.fsum(
                abs(w) for sym, w in existing_weights.items() if sym not in targeted_symbols
            )
            target_gross = math.fsum(abs(t.target_weight) for t in clamped_targets)
            allowed_target_gross = max(0.0, self.max_gross_leverage - untouched_gross)
            if target_gross > allowed_target_gross + 1e-12 and target_gross > 0.0:
                ratio = allowed_target_gross / target_gross
                clamped_targets = [
                    replace(t, target_weight=(0.0 if t.target_weight == 0.0 else t.target_weight * ratio))
                    for t in clamped_targets
                ]

        # 3. Clamp non-reduce-only OrderIntents.
        clamped_intents: list[OrderIntent] = []
        running_gross = math.fsum(abs(w) for w in existing_weights.values())
        running_by_sym = dict(existing_weights)

        for intent in output.intents:
            if intent.reduce_only or intent.qty_mode == "close_all":
                clamped_intents.append(intent)
                continue

            sym = intent.symbol
            curr_w = running_by_sym.get(sym, 0.0)
            other_gross = max(0.0, running_gross - abs(curr_w))

            if intent.qty_mode == "target_weight":
                desired = max(-single_cap, min(single_cap, intent.qty_value))
                max_for_gross = max(0.0, self.max_gross_leverage - other_gross)
                if abs(desired) > max_for_gross:
                    desired = math.copysign(max_for_gross, desired) if max_for_gross > 0.0 else 0.0
                if desired == 0.0:
                    desired = 0.0
                clamped_intents.append(replace(intent, qty_value=desired))
                running_by_sym[sym] = desired
                running_gross = other_gross + abs(desired)
                continue

            side_sign = 1.0 if intent.side == "buy" else -1.0
            if intent.qty_mode == "equity_fraction":
                proposed_w = curr_w + side_sign * intent.qty_value
                max_abs_sym = min(single_cap, max(0.0, self.max_gross_leverage - other_gross))
                if abs(proposed_w) <= max_abs_sym + 1e-12:
                    clamped_intents.append(intent)
                    running_by_sym[sym] = proposed_w
                    running_gross = other_gross + abs(proposed_w)
                else:
                    # Clamp to remaining headroom in the order's direction.
                    target_w = math.copysign(max_abs_sym, proposed_w) if max_abs_sym > 0.0 else 0.0
                    allowed_delta = (target_w - curr_w) * side_sign
                    if allowed_delta > 1e-12:
                        clamped_intents.append(replace(intent, qty_value=allowed_delta))
                        running_by_sym[sym] = curr_w + side_sign * allowed_delta
                        running_gross = other_gross + abs(running_by_sym[sym])
                    # else: zero headroom -> drop order
                continue

            if intent.qty_mode == "contracts":
                ref_px = intent.limit_price or intent.stop_price or self._reference_price(ctx, sym)
                if equity > 0.0 and ref_px is not None and ref_px > 0.0:
                    order_weight = (intent.qty_value * ref_px) / equity
                    proposed_w = curr_w + side_sign * order_weight
                    max_abs_sym = min(single_cap, max(0.0, self.max_gross_leverage - other_gross))
                    if abs(proposed_w) <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        running_by_sym[sym] = proposed_w
                        running_gross = other_gross + abs(proposed_w)
                    else:
                        target_w = math.copysign(max_abs_sym, proposed_w) if max_abs_sym > 0.0 else 0.0
                        allowed_weight = (target_w - curr_w) * side_sign
                        allowed_qty = (allowed_weight * equity) / ref_px
                        if allowed_qty > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_qty))
                            running_by_sym[sym] = curr_w + side_sign * allowed_weight
                            running_gross = other_gross + abs(running_by_sym[sym])
                else:
                    clamped_intents.append(intent)

        return StrategyOutput(
            intents=tuple(clamped_intents),
            targets=tuple(clamped_targets),
            diagnostics=output.diagnostics,
        )

    def snapshot_state(self) -> dict[str, Any]:
        return {}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        return None


@dataclass(frozen=True)
class _CalendarConfigAdapter:
    trading_calendar_id: str
    trading_calendar_sha256: str
    trading_windows_et: tuple[tuple[str, str], ...]


class SessionWindowOverlay:
    """Decoupled calendar/session-window gate reusing ``mu_strategy.core.trading_calendar``.

    Blocks new entries outside the configured session windows while ALWAYS allowing
    risk-reducing exits and protective stops. Optionally flattens open positions outside
    the session when ``flatten_outside_session=True``.
    """

    def __init__(
        self,
        trading_windows_et: Sequence[tuple[str, str]],
        calendar_id: str = US_EQUITIES_ALGORITHMIC_CALENDAR,
        *,
        flatten_outside_session: bool = False,
    ) -> None:
        windows = tuple((str(start), str(end)) for start, end in trading_windows_et)
        if not windows:
            raise ValueError("trading_windows_et must be non-empty")
        digest = calendar_sha256(calendar_id)
        self.trading_windows_et = windows
        self.calendar_id = calendar_id
        self.flatten_outside_session = bool(flatten_outside_session)
        self._cal_config = _CalendarConfigAdapter(
            trading_calendar_id=calendar_id,
            trading_calendar_sha256=digest,
            trading_windows_et=windows,
        )
        # Validate windows eagerly via calendar parser.
        is_trading_window_allowed(1_735_689_600_000, self._cal_config)

    def reset(self) -> None:
        return None

    def is_allowed(self, at_ms: int) -> bool:
        return is_trading_window_allowed(at_ms, self._cal_config)

    def filter(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput:
        if self.is_allowed(ctx.now_ms):
            return output

        if self.flatten_outside_session:
            return _flatten_output(ctx, output, reason="outside_session_window")

        # Outside session: allow exits/reductions, block new entries/scale-ups.
        kept_intents = tuple(intent for intent in output.intents if _is_reducing_intent(intent, ctx))
        kept_targets: list[TargetPosition] = []
        equity = ctx.portfolio.equity

        for target in output.targets:
            if target.target_weight == 0.0:
                kept_targets.append(target)
                continue
            pos = ctx.portfolio.position(target.symbol)
            if pos is None or pos.is_flat or equity <= 0.0:
                # Currently flat -> block new entry outside session.
                continue
            curr_w = (pos.notional / equity) * (1.0 if pos.qty > 0.0 else -1.0)
            # Allow reducing same-side exposure toward flat; block reversals or scale-ups.
            if (curr_w > 0.0 and 0.0 < target.target_weight <= curr_w + 1e-12) or (
                curr_w < 0.0 and curr_w - 1e-12 <= target.target_weight < 0.0
            ):
                kept_targets.append(target)

        if not kept_intents and not kept_targets:
            if output.diagnostics:
                return StrategyOutput(diagnostics=output.diagnostics)
            return EMPTY_OUTPUT
        return StrategyOutput(
            intents=kept_intents,
            targets=tuple(kept_targets),
            diagnostics=output.diagnostics,
        )

    def snapshot_state(self) -> dict[str, Any]:
        return {}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        return None
