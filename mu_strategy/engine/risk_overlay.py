"""Decoupled composable RiskOverlay protocol and production risk overlays for Layer 3."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
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


def _contract_multiplier(ctx: BarContext, symbol: str) -> float:
    inst = ctx.instrument
    if inst is not None and symbol == inst.symbol:
        cv = getattr(inst, "contract_value_float", None)
        if cv is not None:
            return float(cv)
        return float(inst.contract_value)
    return 1.0


def _causal_reference_price(ctx: BarContext, symbol: str) -> float | None:
    """Resolve the current causal market price for ``symbol`` across available market intervals."""
    market = ctx.market
    if market is not None and hasattr(market, "latest_closed_bar"):
        intervals: Sequence[str] = ()
        if hasattr(market, "available_intervals"):
            intervals = market.available_intervals(symbol)
        elif hasattr(market, "intervals"):
            intervals = market.intervals(symbol)
        if not intervals:
            intervals = ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")
        for iv in intervals:
            try:
                bar = market.latest_closed_bar(symbol, iv)
                if bar is not None and math.isfinite(bar.close) and bar.close > 0.0:
                    return float(bar.close)
            except (KeyError, ValueError):
                continue
    pos = ctx.portfolio.position(symbol)
    if pos is not None and not pos.is_flat and pos.notional > 0.0:
        px = pos.notional / abs(pos.qty)
        if math.isfinite(px) and px > 0.0:
            return px
    return None


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


def _has_entry_gate(intent: OrderIntent) -> bool:
    return (getattr(intent, "session_gate", None) is not None) or bool(
        getattr(intent, "excluded_price_bands", ())
    )


def _filter_one_reducing_intent(
    intent: OrderIntent,
    ctx: BarContext,
    rem_base_by_sym: dict[str, float],
    rem_weight_by_sym: dict[str, float],
) -> OrderIntent | None:
    """Filter a single intent for exit-only / fail-closed policy without stripping caller gates."""
    if intent.reduce_only:
        return intent

    has_gate = _has_entry_gate(intent)
    if intent.qty_mode == "close_all":
        if has_gate:
            gate = getattr(intent, "session_gate", None)
            if gate is not None and not gate.is_allowed(ctx.now_ms):
                return None
            # Keep gated close_all intact without stripping caller's gate (Simulator caps close_all to flat).
            return intent
        return replace(intent, reduce_only=True)

    sym = intent.symbol
    pos = ctx.portfolio.position(sym)
    pos_qty = pos.qty if pos is not None else 0.0
    equity = ctx.portfolio.equity

    if pos is None or pos.is_flat:
        return None
    is_opposing = (pos_qty > 0.0 and intent.side == "sell") or (pos_qty < 0.0 and intent.side == "buy")
    if not is_opposing:
        return None

    # Non-reduce_only gated intents cannot be converted to reduce_only=True without silently
    # discarding the caller-supplied session_gate / excluded_price_bands (CR2-25): reject cleanly.
    if has_gate:
        return None

    if intent.qty_mode == "target_weight":
        # Require compatible opposing side and carry reduce_only=True into fill-time execution (CR2-28).
        if intent.qty_value == 0.0:
            return replace(intent, reduce_only=True)
        if equity <= 0.0:
            return None
        curr_w = (pos.notional / equity) * (1.0 if pos_qty > 0.0 else -1.0)
        if (curr_w > 0.0 and 0.0 <= intent.qty_value <= curr_w + 1e-12) or (
            curr_w < 0.0 and curr_w - 1e-12 <= intent.qty_value <= 0.0
        ):
            return replace(intent, reduce_only=True)
        return None

    if intent.qty_mode == "contracts":
        mult = _contract_multiplier(ctx, sym)
        rem_base = rem_base_by_sym.get(sym, abs(pos_qty))
        if rem_base <= 1e-12:
            return None
        req_base = intent.qty_value * mult
        allow_base = req_base if req_base <= rem_base else rem_base
        rem_base_by_sym[sym] = max(0.0, rem_base - allow_base)
        if equity > 0.0 and pos.notional > 0.0:
            curr_w_abs = pos.notional / equity
            rem_w = rem_weight_by_sym.get(sym, curr_w_abs)
            frac_used = (allow_base / abs(pos_qty)) * curr_w_abs
            rem_weight_by_sym[sym] = max(0.0, rem_w - frac_used)
        allow_contracts = allow_base / mult
        if allow_contracts > 1e-12:
            return replace(intent, qty_value=allow_contracts, reduce_only=True)
        return None

    if intent.qty_mode == "equity_fraction":
        if equity <= 0.0:
            return None
        curr_w_abs = pos.notional / equity
        rem_w = rem_weight_by_sym.get(sym, curr_w_abs)
        if rem_w <= 1e-12:
            return None
        allow_w = intent.qty_value if intent.qty_value <= rem_w else rem_w
        rem_weight_by_sym[sym] = max(0.0, rem_w - allow_w)
        if curr_w_abs > 0.0:
            rem_base = rem_base_by_sym.get(sym, abs(pos_qty))
            base_used = (allow_w / curr_w_abs) * abs(pos_qty)
            rem_base_by_sym[sym] = max(0.0, rem_base - base_used)
        if allow_w > 1e-12:
            return replace(intent, qty_value=allow_w, reduce_only=True)
        return None

    return None


def _filter_reducing_intents(intents: Sequence[OrderIntent], ctx: BarContext) -> tuple[OrderIntent, ...]:
    """Keep only risk-reducing intents and cap non-reduce-only exits so combined exits cannot reverse past flat."""
    kept: list[OrderIntent] = []
    rem_base_by_sym: dict[str, float] = {}
    rem_weight_by_sym: dict[str, float] = {}

    for intent in intents:
        filtered = _filter_one_reducing_intent(intent, ctx, rem_base_by_sym, rem_weight_by_sym)
        if filtered is not None:
            kept.append(filtered)

    return tuple(kept)


def _filter_reducing_targets(targets: Sequence[TargetPosition], ctx: BarContext) -> tuple[TargetPosition, ...]:
    """Keep only flat or same-side risk-reducing targets; block new entries, scale-ups, and reversals."""
    kept: list[TargetPosition] = []
    equity = ctx.portfolio.equity
    for target in targets:
        if target.target_weight == 0.0:
            kept.append(target)
            continue
        pos = ctx.portfolio.position(target.symbol)
        if pos is None or pos.is_flat or equity <= 0.0:
            continue
        curr_w = (pos.notional / equity) * (1.0 if pos.qty > 0.0 else -1.0)
        if (curr_w > 0.0 and 0.0 < target.target_weight <= curr_w + 1e-12) or (
            curr_w < 0.0 and curr_w - 1e-12 <= target.target_weight < 0.0
        ):
            kept.append(target)
    return tuple(kept)


def _flatten_output(ctx: BarContext, output: StrategyOutput, *, reason: str) -> StrategyOutput:
    """Drop all risk-increasing entries and emit explicit flat targets for open positions."""
    kept_list: list[OrderIntent] = []
    for intent in output.intents:
        if intent.reduce_only:
            kept_list.append(intent)
        elif intent.qty_mode == "close_all":
            if _has_entry_gate(intent):
                if intent.session_gate is not None and not intent.session_gate.is_allowed(ctx.now_ms):
                    continue
                kept_list.append(intent)
            else:
                kept_list.append(replace(intent, reduce_only=True))
    kept_intents = tuple(kept_list)
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
    """Composable risk filter applied to StrategyOutput before order queuing.

    Callback lifetime contract (CR3-08):
      - Overlays without ``supports_ephemeral_context = True`` (explicit Boolean
        ``True``) always receive point-in-time immutable ``BarContext`` snapshots
        even when the paired ``Strategy`` opts into ephemeral context reuse.
      - Overlays that explicitly set ``supports_ephemeral_context = True`` MUST NOT
        retain ``ctx``, ``ctx.market``, or ``ctx.portfolio`` across ``filter()`` calls.
    """

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

    supports_ephemeral_context: bool = True

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

    def _causal_realized_vol_status(
        self,
        ctx: BarContext,
        symbol: str,
    ) -> tuple[Literal["ok", "warmup", "missing"], float | None]:
        market = ctx.market
        if market is None:
            return "missing", None
        interval = self._resolve_interval(market, symbol)
        if interval is None:
            return "missing", None
        if hasattr(market, "has_series") and not market.has_series(symbol, interval):
            return "missing", None
        try:
            bars = market.closed_bars(symbol, interval)
        except (KeyError, ValueError):
            return "missing", None

        # Explicit `is None` check so an empty BarSeries (bool == False) is never discarded (CR2-26).
        base = getattr(bars, "_series", None)
        if base is None:
            base = getattr(bars, "series", None)
        if base is not None and len(base) == 0:
            return "missing", None

        interval_ms = getattr(bars, "interval_ms", None)
        if interval_ms is None and base is not None:
            interval_ms = getattr(base, "interval_ms", None)

        n = len(bars)
        if n > 0 and interval_ms is not None and interval_ms > 0:
            last_close_ms = int(bars.close_times[-1])
            is_equity = ctx.instrument is not None and ctx.instrument.asset_class == "equity"
            max_age_ms = max(2 * interval_ms, 5 * _MS_PER_DAY) if is_equity else (2 * interval_ms)
            if ctx.now_ms - last_close_ms > max_age_ms:
                return "missing", None

        if n < self.vol_lookback + 1:
            return "warmup", None
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
                    return "ok", max(0.0, float(val))
            elif hasattr(features, "realized_vol"):
                rv_series = features.realized_vol(bars, self.vol_lookback, periods_per_year=ppy)
                val = rv_series[-1] if len(rv_series) > 0 else None
                if val is not None and math.isfinite(val):
                    return "ok", max(0.0, float(val))

        # Pure stdlib O(vol_lookback) fallback over strictly closed bars.
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
                return "missing", None
            log_returns.append(math.log(curr_c / prev_c))

        m = len(log_returns)
        if m < 2:
            return "warmup", None
        mean_lr = math.fsum(log_returns) / m
        var = math.fsum((r - mean_lr) * (r - mean_lr) for r in log_returns) / (m - 1)
        std = math.sqrt(var) if var > 0.0 else 0.0
        return "ok", std * math.sqrt(ppy)

    def _causal_realized_vol(self, ctx: BarContext, symbol: str) -> float | None:
        _, rv = self._causal_realized_vol_status(ctx, symbol)
        return rv

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
        if not output.targets and not output.intents:
            return output

        scaled_targets: list[TargetPosition] = []
        cache_status: dict[str, tuple[Literal["ok", "warmup", "missing"], float | None]] = {}

        def _get_status(symbol: str) -> tuple[Literal["ok", "warmup", "missing"], float | None]:
            if symbol not in cache_status:
                cache_status[symbol] = self._causal_realized_vol_status(ctx, symbol)
            return cache_status[symbol]

        equity = ctx.portfolio.equity
        for target in output.targets:
            if target.target_weight == 0.0:
                scaled_targets.append(target)
                continue
            sym = target.symbol
            status, rv = _get_status(sym)
            if status == "missing":
                # Fail closed on missing volatility series: preserve same-side reductions only (CR1-35).
                pos = ctx.portfolio.position(sym)
                if pos is not None and not pos.is_flat and equity > 0.0:
                    curr_w = (pos.notional / equity) * (1.0 if pos.qty > 0.0 else -1.0)
                    if (curr_w > 0.0 and 0.0 < target.target_weight <= curr_w + 1e-12) or (
                        curr_w < 0.0 and curr_w - 1e-12 <= target.target_weight < 0.0
                    ):
                        scaled_targets.append(target)
                continue
            scale = self._apply_hysteresis(sym, self.compute_scale(rv))
            self._last_realized_vol = rv
            self._last_scale = scale
            raw_weight = target.target_weight * scale
            clamped = max(-self.max_leverage, min(self.max_leverage, raw_weight))
            if clamped == 0.0:
                clamped = 0.0
            scaled_targets.append(replace(target, target_weight=clamped))

        kept_intents: tuple[OrderIntent, ...] = output.intents
        if output.intents:
            # Filter missing-volatility intents IN PLACE without reordering survivors (CR2-45).
            ordered_kept: list[OrderIntent] = []
            rem_base_by_sym: dict[str, float] = {}
            rem_weight_by_sym: dict[str, float] = {}
            changed = False
            for intent in output.intents:
                if intent.reduce_only:
                    ordered_kept.append(intent)
                    continue
                status, _ = _get_status(intent.symbol)
                if status == "missing":
                    changed = True
                    filtered = _filter_one_reducing_intent(intent, ctx, rem_base_by_sym, rem_weight_by_sym)
                    if filtered is not None:
                        ordered_kept.append(filtered)
                else:
                    ordered_kept.append(intent)
            if changed:
                kept_intents = tuple(ordered_kept)

        if not kept_intents and not scaled_targets:
            if output.diagnostics:
                return StrategyOutput(diagnostics=output.diagnostics)
            return EMPTY_OUTPUT
        return StrategyOutput(
            intents=kept_intents,
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

    supports_ephemeral_context: bool = True

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

    @property
    def is_halting_entries(self) -> bool:
        return self._active and self.action == "flatten_and_halt"

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

    supports_ephemeral_context: bool = True

    def __init__(self, max_daily_loss_pct: float, reset_tz: str = "UTC") -> None:
        self.max_daily_loss_pct = _require_fraction_01("max_daily_loss_pct", max_daily_loss_pct)
        if not isinstance(reset_tz, str) or not reset_tz.strip():
            raise ValueError("reset_tz must be a non-empty timezone name")
        self.reset_tz = reset_tz
        self._is_utc = reset_tz.upper() == "UTC"
        self._tz = timezone.utc if self._is_utc else ZoneInfo(reset_tz)
        self._current_day_key: int | None = None
        self._day_start_equity: float | None = None
        self._last_equity: float | None = None
        self._halted: bool = False

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def is_halting_entries(self) -> bool:
        return self._halted

    def reset(self) -> None:
        self._current_day_key = None
        self._day_start_equity = None
        self._last_equity = None
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
            # Use equity at the day boundary BEFORE this opening bar's PnL (CR1-30).
            if self._last_equity is not None:
                self._day_start_equity = self._last_equity
            elif ctx.portfolio.daily_start_equity > 0.0:
                self._day_start_equity = ctx.portfolio.daily_start_equity
            else:
                self._day_start_equity = equity
            self._halted = False

        if not self._halted and self._day_start_equity is not None and self._day_start_equity > 0.0:
            daily_ret = (equity / self._day_start_equity) - 1.0
            if daily_ret <= -self.max_daily_loss_pct + 1e-12:
                self._halted = True

        self._last_equity = equity

        if self._halted:
            return _flatten_output(ctx, output, reason="daily_loss_limit")
        return output

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "current_day_key": self._current_day_key,
            "day_start_equity": self._day_start_equity,
            "last_equity": self._last_equity,
            "halted": self._halted,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self._current_day_key = state.get("current_day_key")
        self._day_start_equity = state.get("day_start_equity")
        self._last_equity = state.get("last_equity")
        self._halted = bool(state.get("halted", False))


class ExposureCapOverlay:
    """Clamps signed target weights and risk-increasing order sizes to decision-time exposure limits.

    Scope & contract:
      - Caps (`max_gross_leverage`, `max_single_weight`) apply pre-trade at
        bar-close decision time inside ``filter(ctx, output)`` against the causal
        decision-time portfolio equity and reference prices.
      - Target-weight outputs (``TargetPosition`` and ``OrderIntent(qty_mode="target_weight")``)
        express exposure relative to fill-time equity/price. When
        ``SimulatorConfig(enforce_instrument_max_leverage=True)`` (the default) is enabled,
        the simulator clamps each requested target weight to ``±InstrumentSpec.max_leverage``
        before sizing at fill time; this is a clamp on the requested target weight (not a
        portfolio-level gross cap, and marked post-fill exposure can still exceed that weight
        under adverse spread/slippage).
      - Incremental ``OrderIntent(qty_mode="equity_fraction")`` orders only clamp the single
        order's notional to ``eq_basis * InstrumentSpec.max_leverage`` at fill time and do
        not bound held-plus-order exposure across opening gaps.
      - Fixed-quantity ``OrderIntent(qty_mode="contracts")`` orders are clamped at
        decision time using the order's limit/stop price or causal decision-time
        reference price (``qty * contract_value * ref_px / equity``); they are not
        re-clamped at fill time if the next bar opens at a gapped price.
    """

    cap_scope: Literal["decision_time"] = "decision_time"
    supports_ephemeral_context: bool = True

    def __init__(
        self,
        max_gross_leverage: float,
        max_single_weight: float,
        *,
        sizing_basis: Literal["marked_equity", "settled_equity"] | None = None,
        enforce_instrument_max_leverage: bool | None = None,
    ) -> None:
        self.max_gross_leverage = _require_positive_finite("max_gross_leverage", max_gross_leverage)
        self.max_single_weight = _require_positive_finite("max_single_weight", max_single_weight)
        if sizing_basis is not None and sizing_basis not in ("marked_equity", "settled_equity"):
            raise ValueError(f"unsupported sizing_basis: {sizing_basis!r}")
        if enforce_instrument_max_leverage is not None and not isinstance(enforce_instrument_max_leverage, bool):
            raise ValueError(
                f"enforce_instrument_max_leverage must be bool or None, got {enforce_instrument_max_leverage!r}"
            )
        self.sizing_basis = sizing_basis
        self.enforce_instrument_max_leverage = enforce_instrument_max_leverage
        # Run-scoped simulator policy bindings (set/restored by Simulator.run in try/finally).
        self._runtime_sizing_basis: Literal["marked_equity", "settled_equity"] | None = None
        self._runtime_enforce_instrument_max_leverage: bool | None = None
        self._runtime_settled_equity_source: Any = None

    def reset(self) -> None:
        return None

    def _reference_price(self, ctx: BarContext, symbol: str) -> float | None:
        return _causal_reference_price(ctx, symbol)

    def filter(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput:
        if not output.targets and not output.intents:
            return output

        single_cap = min(self.max_single_weight, self.max_gross_leverage)
        equity = ctx.portfolio.equity
        eff_sizing_basis = (
            self._runtime_sizing_basis
            if self._runtime_sizing_basis is not None
            else self.sizing_basis
        )
        eff_enforce_inst_lev = (
            self._runtime_enforce_instrument_max_leverage
            if self._runtime_enforce_instrument_max_leverage is not None
            else self.enforce_instrument_max_leverage
        )
        settled_src = self._runtime_settled_equity_source
        settled_equity = (
            float(settled_src.settled_equity)
            if settled_src is not None
            else float(ctx.portfolio.cash)
        )
        # Current signed marked-equity weights and realizable net PnL by symbol.
        existing_weights: dict[str, float] = {}
        pos_realizable_net: dict[str, float] = {}
        if equity > 0.0:
            src_sym = getattr(settled_src, "symbol", None) if settled_src is not None else None
            for sym, pos in ctx.portfolio.positions.items():
                if not pos.is_flat:
                    sign = 1.0 if pos.qty > 0.0 else -1.0
                    existing_weights[sym] = sign * (pos.notional / equity)
                    r_net = float(pos.unrealized_pnl)
                    if settled_src is not None and sym == src_sym:
                        r_net += float(settled_src.open_funding_pnl) - float(settled_src.open_entry_fees)
                    pos_realizable_net[sym] = r_net

        def _inst_max_leverage(osym: str) -> float | None:
            inst_obj = ctx.instrument
            if inst_obj is not None and osym == inst_obj.symbol:
                raw_ml = getattr(inst_obj, "max_leverage", None)
                if raw_ml is not None and math.isfinite(raw_ml) and raw_ml > 0.0:
                    return float(raw_ml)
            return None

        def _settled_ratios_for_bounds(
            lo_fn: Callable[[str], float],
            hi_fn: Callable[[str], float],
        ) -> tuple[float, float]:
            """Map simulator eq_basis units to marked-equity weights (upper, lower) (CR6-04 / CR7-05 / CR7-06 / CR7-10)."""
            if equity <= 0.0:
                return 1.0, 0.0
            if eff_sizing_basis == "marked_equity":
                return 1.0, 1.0
            gain = 0.0
            loss = 0.0
            for osym, ew in existing_weights.items():
                r_net = pos_realizable_net.get(osym, 0.0)
                if r_net == 0.0 or ew == 0.0:
                    continue
                if ew > 0.0:
                    lo_w = lo_fn(osym)
                    rem_pos = lo_w if lo_w > 0.0 else 0.0
                    closed_frac = (ew - rem_pos) / ew if rem_pos < ew else 0.0
                else:
                    hi_w = hi_fn(osym)
                    rem_neg = -hi_w if hi_w < 0.0 else 0.0
                    ew_abs = -ew
                    closed_frac = (ew_abs - rem_neg) / ew_abs if rem_neg < ew_abs else 0.0
                if closed_frac > 1.0:
                    closed_frac = 1.0
                if closed_frac > 0.0:
                    if r_net > 0.0:
                        gain += closed_frac * r_net
                    else:
                        loss += closed_frac * r_net
            max_eq_r = max(0.0, (settled_equity + gain) / equity)
            min_eq_r = max(0.0, (settled_equity + loss) / equity)
            if eff_sizing_basis == "settled_equity":
                return max_eq_r, min_eq_r
            return max(1.0, max_eq_r), min(1.0, min_eq_r)

        if equity > 0.0:
            if eff_sizing_basis == "marked_equity":
                prot_tw_ratio = 1.0
            else:
                loss_all = math.fsum(v for v in pos_realizable_net.values() if v < 0.0)
                min_settled_r = max(0.0, (settled_equity + loss_all) / equity)
                prot_tw_ratio = min(1.0, min_settled_r) if settled_equity > 0.0 else 0.0
        else:
            prot_tw_ratio = 0.0

        # 1. Summarize already-resting open orders AND protective reductions in their exact Simulator
        #    execution phases BEFORE admitting targets (CR2-29 / CR3-16 / CR5-05 / CR6-04 / CR6-05 /
        #    CR7-04 / CR7-06 / CR8-04 / CR8-05):
        #    - Phase 1.3 (market reduce_only / close_all): runs BEFORE Step 1.4 TargetPosition.
        #    - Phase 1.5 (non-market reduce_only / close_all + target brackets): runs AFTER Step 1.4 TargetPosition.
        #    - Phase 1.6 (non-reduce-only resting open orders): runs AFTER Step 1.4 TargetPosition & Phase 1.5.
        rest_buy_fixed: dict[str, float] = {}
        rest_sell_fixed: dict[str, float] = {}
        rest_ef_buys: dict[str, list[float]] = {}
        rest_ef_sells: dict[str, list[float]] = {}
        rest_tw_orders: list[tuple[str, float]] = []
        mkt_red_sell: dict[str, float] = {}
        mkt_red_buy: dict[str, float] = {}
        mkt_red_sell_floor: dict[str, float] = {}
        mkt_red_buy_ceil: dict[str, float] = {}
        post_red_sell_fixed: dict[str, float] = {}
        post_red_buy_fixed: dict[str, float] = {}
        post_red_sell_floor: dict[str, float] = {}
        post_red_buy_ceil: dict[str, float] = {}
        post_prot_ef_orders: list[tuple[str, str, float]] = []
        all_known_syms: set[str] = set(existing_weights.keys())

        def _apply_red_maps(
            sym: str,
            lo_val: float,
            hi_val: float,
            r_sell: Mapping[str, float],
            r_buy: Mapping[str, float],
            r_s_floor: Mapping[str, float],
            r_b_ceil: Mapping[str, float],
        ) -> tuple[float, float]:
            # Shrink only the inner side toward 0.0 (never crossing 0.0, preserving CR3-16 & CR1-31).
            if lo_val > 0.0:
                if sym in r_s_floor and r_s_floor[sym] < lo_val:
                    lo_val = r_s_floor[sym]
                rs_red = r_sell.get(sym, 0.0)
                if rs_red > 0.0:
                    lo_val = max(0.0, lo_val - rs_red)
            if hi_val < 0.0:
                if sym in r_b_ceil and r_b_ceil[sym] > hi_val:
                    hi_val = r_b_ceil[sym]
                rb_red = r_buy.get(sym, 0.0)
                if rb_red > 0.0:
                    hi_val = min(0.0, hi_val + rb_red)
            return lo_val, hi_val

        def _mkt_prot_lo(sym: str) -> float:
            ew = existing_weights.get(sym, 0.0)
            lo_v, _ = _apply_red_maps(
                sym, ew, ew, mkt_red_sell, mkt_red_buy, mkt_red_sell_floor, mkt_red_buy_ceil
            )
            return lo_v

        def _mkt_prot_hi(sym: str) -> float:
            ew = existing_weights.get(sym, 0.0)
            _, hi_v = _apply_red_maps(
                sym, ew, ew, mkt_red_sell, mkt_red_buy, mkt_red_sell_floor, mkt_red_buy_ceil
            )
            return hi_v

        def _record_protective_reduction(ord_obj: OrderIntent, *, is_market_phase: bool) -> None:
            osym = ord_obj.symbol
            all_known_syms.add(osym)
            qm = ord_obj.qty_mode
            r_sell = mkt_red_sell if is_market_phase else post_red_sell_fixed
            r_buy = mkt_red_buy if is_market_phase else post_red_buy_fixed
            r_s_floor = mkt_red_sell_floor if is_market_phase else post_red_sell_floor
            r_b_ceil = mkt_red_buy_ceil if is_market_phase else post_red_buy_ceil
            if qm == "close_all":
                if ord_obj.side == "sell":
                    r_sell[osym] = math.inf
                else:
                    r_buy[osym] = math.inf
            elif qm == "equity_fraction":
                # CR7-04 / CR8-04: Clamp protective equity_fraction by known instrument max_leverage when
                # enforced; Step 1.3 market orders size against pre-target settled equity, while Step 1.5/1.7
                # non-market protective orders are deferred to post-target settled equity evaluation.
                raw_ef = ord_obj.qty_value
                if eff_enforce_inst_lev is True:
                    inst_ml_ef = _inst_max_leverage(osym)
                    if inst_ml_ef is not None and raw_ef > inst_ml_ef:
                        raw_ef = inst_ml_ef
                if is_market_phase:
                    cur_prot_ef_ratio, _ = _settled_ratios_for_bounds(_mkt_prot_lo, _mkt_prot_hi)
                    w_red_ef = raw_ef * cur_prot_ef_ratio
                    if ord_obj.side == "sell":
                        r_sell[osym] = r_sell.get(osym, 0.0) + w_red_ef
                    else:
                        r_buy[osym] = r_buy.get(osym, 0.0) + w_red_ef
                else:
                    post_prot_ef_orders.append((osym, ord_obj.side, raw_ef))
            elif qm == "contracts":
                ord_px = ord_obj.limit_price or ord_obj.stop_price
                dec_px = self._reference_price(ctx, osym)
                px_candidates = [p for p in (ord_px, dec_px) if p is not None and p > 0.0]
                if equity > 0.0 and px_candidates:
                    mult = _contract_multiplier(ctx, osym)
                    w_red = (ord_obj.qty_value * mult * max(px_candidates)) / equity
                else:
                    w_red = math.inf
                if ord_obj.side == "sell":
                    r_sell[osym] = r_sell.get(osym, 0.0) + w_red
                else:
                    r_buy[osym] = r_buy.get(osym, 0.0) + w_red
            elif qm == "target_weight":
                # CR6-05: Reserve against the effective protective target AFTER instrument leverage
                # clamping when known; reserve conservatively down to flat (0.0) when policy is unknown;
                # preserve the unclamped target when leverage enforcement is explicitly off.
                inst_ml = _inst_max_leverage(osym)
                raw_tw = ord_obj.qty_value
                if ord_obj.side == "sell":
                    if eff_enforce_inst_lev is True:
                        eff_tw = min(raw_tw, inst_ml) if inst_ml is not None else 0.0
                        fl = max(0.0, eff_tw) * prot_tw_ratio
                    elif eff_enforce_inst_lev is False:
                        fl = max(0.0, raw_tw) * prot_tw_ratio
                    else:
                        fl = 0.0
                    r_s_floor[osym] = fl if osym not in r_s_floor else min(r_s_floor[osym], fl)
                else:
                    if eff_enforce_inst_lev is True:
                        eff_tw = max(raw_tw, -inst_ml) if inst_ml is not None else 0.0
                        cl = min(0.0, eff_tw) * prot_tw_ratio
                    elif eff_enforce_inst_lev is False:
                        cl = min(0.0, raw_tw) * prot_tw_ratio
                    else:
                        cl = 0.0
                    r_b_ceil[osym] = cl if osym not in r_b_ceil else max(r_b_ceil[osym], cl)

        same_output_reduce_ids = {
            it.intent_id
            for it in output.intents
            if it.reduce_only or it.qty_mode == "close_all"
        }

        all_prot_orders: list[OrderIntent] = []
        for open_ord in ctx.portfolio.open_orders:
            if open_ord.reduce_only or open_ord.qty_mode == "close_all":
                if open_ord.intent_id not in same_output_reduce_ids:
                    all_prot_orders.append(open_ord)
        for it_pre in output.intents:
            if it_pre.reduce_only or it_pre.qty_mode == "close_all":
                all_prot_orders.append(it_pre)

        # Step 1a: Record Phase 1.3 market protective reductions BEFORE computing target conversion ratios.
        for po in all_prot_orders:
            if po.order_type == "market":
                _record_protective_reduction(po, is_market_phase=True)

        target_upper_ratio, target_lower_ratio = _settled_ratios_for_bounds(_mkt_prot_lo, _mkt_prot_hi)

        # Step 1b: Record Phase 1.5 non-market protective reductions and target bracket exits (after targets).
        for po in all_prot_orders:
            if po.order_type != "market":
                _record_protective_reduction(po, is_market_phase=False)

        for t_pre in output.targets:
            all_known_syms.add(t_pre.symbol)
            if t_pre.stop_price is not None or t_pre.take_profit is not None:
                if t_pre.target_weight > 0.0:
                    post_red_sell_fixed[t_pre.symbol] = math.inf
                elif t_pre.target_weight < 0.0:
                    post_red_buy_fixed[t_pre.symbol] = math.inf

        # Step 1c: Summarize non-reduce-only resting open orders (Phase 1.6, after targets & Phase 1.5).
        for open_ord in ctx.portfolio.open_orders:
            if open_ord.reduce_only or open_ord.qty_mode == "close_all":
                continue
            osym = open_ord.symbol
            all_known_syms.add(osym)
            if open_ord.qty_mode == "target_weight":
                rest_tw_orders.append((osym, open_ord.qty_value))
            elif open_ord.qty_mode == "equity_fraction":
                # CR8-05 / CR9-04: Preserve each resting equity_fraction order individually (clamped by
                # instrument max_leverage when enforced) so sequential profit realization between fills
                # is captured accurately!
                raw_ef_r = open_ord.qty_value
                if eff_enforce_inst_lev is True:
                    inst_ml_r = _inst_max_leverage(osym)
                    if inst_ml_r is not None and raw_ef_r > inst_ml_r:
                        raw_ef_r = inst_ml_r
                if open_ord.side == "buy":
                    rest_ef_buys.setdefault(osym, []).append(raw_ef_r)
                else:
                    rest_ef_sells.setdefault(osym, []).append(raw_ef_r)
            elif open_ord.qty_mode == "contracts":
                ref_px = open_ord.limit_price or open_ord.stop_price or self._reference_price(ctx, osym)
                if equity > 0.0 and ref_px is not None and ref_px > 0.0:
                    mult = _contract_multiplier(ctx, osym)
                    w = (open_ord.qty_value * mult * ref_px) / equity
                    if open_ord.side == "buy":
                        rest_buy_fixed[osym] = rest_buy_fixed.get(osym, 0.0) + w
                    else:
                        rest_sell_fixed[osym] = rest_sell_fixed.get(osym, 0.0) + w

        is_marked_basis = eff_sizing_basis == "marked_equity" or equity <= 0.0
        rest_ef_buy_sum: dict[str, float] = (
            {s: math.fsum(efs) for s, efs in rest_ef_buys.items()} if is_marked_basis else {}
        )
        rest_ef_sell_sum: dict[str, float] = (
            {s: math.fsum(efs) for s, efs in rest_ef_sells.items()} if is_marked_basis else {}
        )

        def _target_marked_interval(osym: str, w_raw: float) -> tuple[float, float]:
            """Map a simulator sizing-basis target weight to its [lo, hi] marked-equity interval (CR7-10 / CR9-05)."""
            inst_ml_t = _inst_max_leverage(osym)
            if eff_enforce_inst_lev is True and inst_ml_t is not None:
                t_vals: tuple[float, ...] = (max(-inst_ml_t, min(inst_ml_t, w_raw)),)
            elif eff_enforce_inst_lev is False or inst_ml_t is None:
                t_vals = (w_raw,)
            else:
                t_cl = max(-inst_ml_t, min(inst_ml_t, w_raw))
                t_vals = (w_raw, t_cl) if t_cl != w_raw else (w_raw,)
            cands = [v * r for v in t_vals for r in (target_lower_ratio, target_upper_ratio)]
            return min(cands), max(cands)

        def _safe_replace_target(orig_t: TargetPosition, new_w: float) -> TargetPosition | None:
            """Return updated TargetPosition, or None to omit if clamping flipped reversal sign (CR10-02)."""
            orig_w = orig_t.target_weight
            if new_w == 0.0:
                new_w = 0.0
            if (orig_w < 0.0 and new_w > 0.0) or (orig_w > 0.0 and new_w < 0.0):
                return None
            stop = orig_t.stop_price
            tp = orig_t.take_profit
            if stop is not None and tp is not None:
                if (new_w > 0.0 and stop >= tp) or (new_w < 0.0 and stop <= tp):
                    return replace(orig_t, target_weight=new_w, stop_price=None, take_profit=None)
            return replace(orig_t, target_weight=new_w) if new_w != orig_w else orig_t

        def _simulate_ef_additions(
            lo_map: Mapping[str, float],
            hi_map: Mapping[str, float],
            ef_buys: Mapping[str, Sequence[float]] = rest_ef_buys,
            ef_sells: Mapping[str, Sequence[float]] = rest_ef_sells,
            *,
            extra_ef: tuple[str, str, float] | None = None,
        ) -> tuple[dict[str, float], dict[str, float], float, float]:
            """Simulate fractional fills with inter-fill settled-equity updates (CR9-04 / CR10-04 / CR10-08 / CR11-01 / CR11-02).

            Returns ``(best_buy, best_sell, max_r_up_post_ef, min_r_lo_post_ef)``.
            """
            has_extra = extra_ef is not None and extra_ef[2] > 0.0
            # CR10-08: Under marked_equity sizing (ratio == 1.0), order cannot affect size — sum in O(1).
            if is_marked_basis:
                if not ef_buys and not ef_sells and not has_extra:
                    return {}, {}, 1.0, 1.0
                if ef_buys is rest_ef_buys and ef_sells is rest_ef_sells and not has_extra:
                    return rest_ef_buy_sum, rest_ef_sell_sum, 1.0, 1.0
                b_out = {s: math.fsum(efs) for s, efs in ef_buys.items()}
                s_out = {s: math.fsum(efs) for s, efs in ef_sells.items()}
                if has_extra and extra_ef is not None:
                    esym, eside, eval_ = extra_ef
                    if eside == "buy":
                        b_out[esym] = b_out.get(esym, 0.0) + eval_
                    else:
                        s_out[esym] = s_out.get(esym, 0.0) + eval_
                return b_out, s_out, 1.0, 1.0

            r_up_init, r_lo_init = _settled_ratios_for_bounds(
                lambda s, _lo=lo_map: _lo.get(s, 0.0),
                lambda s, _hi=hi_map: _hi.get(s, 0.0),
            )
            if not ef_buys and not ef_sells and not has_extra:
                return {}, {}, r_up_init, r_lo_init

            orders: list[tuple[float, str, str]] = []
            for s, efs in ef_sells.items():
                for ef in efs:
                    orders.append((ef, s, "sell"))
            for s, efs in ef_buys.items():
                for ef in efs:
                    orders.append((ef, s, "buy"))
            if has_extra and extra_ef is not None:
                orders.append((extra_ef[2], extra_ef[0], extra_ef[1]))

            def _is_reducing(item: tuple[float, str, str]) -> bool:
                _, osym, oside = item
                ew_i = existing_weights.get(osym, 0.0)
                return (oside == "sell" and ew_i > 0.0) or (oside == "buy" and ew_i < 0.0)

            red_asc = sorted((o for o in orders if _is_reducing(o)), key=lambda item: item[0])
            non_red = sorted((o for o in orders if not _is_reducing(o)), key=lambda item: item[0])
            orderings: list[list[tuple[float, str, str]]] = [red_asc + non_red]
            if len(red_asc) > 1:
                orderings.append(list(reversed(red_asc)) + non_red)
            ef_syms = {o[1] for o in orders}
            if len(ef_syms) > 1 and red_asc:
                for target_s in ef_syms:
                    other_red = [o for o in red_asc if o[1] != target_s]
                    if other_red:
                        own_red = [o for o in red_asc if o[1] == target_s]
                        orderings.append(other_red + own_red + non_red)

            best_buy: dict[str, float] = {}
            best_sell: dict[str, float] = {}
            max_r_up = r_up_init
            min_r_lo = r_lo_init
            for seq in orderings:
                sim_lo = dict(lo_map)
                sim_hi = dict(hi_map)
                tot_buy: dict[str, float] = {}
                tot_sell: dict[str, float] = {}
                for ef_val, osym, oside in seq:
                    r_up, _ = _settled_ratios_for_bounds(
                        lambda s, _lo=sim_lo: _lo.get(s, 0.0),
                        lambda s, _hi=sim_hi: _hi.get(s, 0.0),
                    )
                    w_fill = ef_val * r_up
                    if oside == "sell":
                        tot_sell[osym] = tot_sell.get(osym, 0.0) + w_fill
                        sim_lo[osym] = sim_lo.get(osym, 0.0) - w_fill
                    else:
                        tot_buy[osym] = tot_buy.get(osym, 0.0) + w_fill
                        sim_hi[osym] = sim_hi.get(osym, 0.0) + w_fill
                r_u_end, r_l_end = _settled_ratios_for_bounds(
                    lambda s, _lo=sim_lo: _lo.get(s, 0.0),
                    lambda s, _hi=sim_hi: _hi.get(s, 0.0),
                )
                if r_u_end > max_r_up:
                    max_r_up = r_u_end
                if r_l_end < min_r_lo:
                    min_r_lo = r_l_end
                for s, v in tot_buy.items():
                    if v > best_buy.get(s, 0.0):
                        best_buy[s] = v
                for s, v in tot_sell.items():
                    if v > best_sell.get(s, 0.0):
                        best_sell[s] = v
            return best_buy, best_sell, max_r_up, min_r_lo

        def _expand_tw_interval(osym: str, w_tw: float, r_lo_v: float, r_up_v: float) -> tuple[float, float]:
            """Convert a target_weight order to its [lo, hi] marked-equity interval under [r_lo_v, r_up_v]."""
            inst_ml_r = _inst_max_leverage(osym)
            if eff_enforce_inst_lev is True and inst_ml_r is not None:
                r_vals: tuple[float, ...] = (max(-inst_ml_r, min(inst_ml_r, w_tw)),)
            elif eff_enforce_inst_lev is False or inst_ml_r is None:
                r_vals = (w_tw,)
            else:
                r_cl = max(-inst_ml_r, min(inst_ml_r, w_tw))
                r_vals = (w_tw, r_cl) if r_cl != w_tw else (w_tw,)
            r_cands = [v * r for v in r_vals for r in (r_lo_v, r_up_v)]
            return min(r_cands), max(r_cands)

        def _eval_phase_15_16(
            sym_t_lo: Mapping[str, float],
            sym_t_hi: Mapping[str, float],
        ) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float], float, float]:
            """Evaluate post-target Phase 1.5 protective reductions and Phase 1.6 resting orders (CR8-04 / CR8-05 / CR9-04 / CR9-05 / CR11-01)."""
            lo_15: dict[str, float] = {}
            hi_15: dict[str, float] = {}
            for s in all_known_syms:
                ew = existing_weights.get(s, 0.0)
                b_lo, b_hi = _apply_red_maps(
                    s, ew, ew, mkt_red_sell, mkt_red_buy, mkt_red_sell_floor, mkt_red_buy_ceil
                )
                if s in sym_t_lo and sym_t_lo[s] < b_lo:
                    b_lo = sym_t_lo[s]
                if s in sym_t_hi and sym_t_hi[s] > b_hi:
                    b_hi = sym_t_hi[s]
                b_lo, b_hi = _apply_red_maps(
                    s, b_lo, b_hi, post_red_sell_fixed, post_red_buy_fixed, post_red_sell_floor, post_red_buy_ceil
                )
                lo_15[s] = b_lo
                hi_15[s] = b_hi

            # Apply Phase 1.5 non-market equity_fraction protective orders sequentially on post-target settled equity.
            for psym, pside, raw_ef in post_prot_ef_orders:
                r_up_15, _ = _settled_ratios_for_bounds(
                    lambda s: lo_15.get(s, 0.0),
                    lambda s: hi_15.get(s, 0.0),
                )
                w_red = raw_ef * r_up_15
                if pside == "sell":
                    cur_lo = lo_15.get(psym, 0.0)
                    if cur_lo > 0.0:
                        lo_15[psym] = max(0.0, cur_lo - w_red)
                else:
                    cur_hi = hi_15.get(psym, 0.0)
                    if cur_hi < 0.0:
                        hi_15[psym] = min(0.0, cur_hi + w_red)

            r_up_16, r_lo_16 = _settled_ratios_for_bounds(
                lambda s: lo_15.get(s, 0.0) - rest_sell_fixed.get(s, 0.0),
                lambda s: hi_15.get(s, 0.0) + rest_buy_fixed.get(s, 0.0),
            )
            ef_buy_add: dict[str, float] = {}
            ef_sell_add: dict[str, float] = {}
            # Fixed-point closure between resting target_weight orders and resting equity_fraction fills (CR11-01).
            for _ in range(4):
                if rest_tw_orders:
                    for osym, w_tw in rest_tw_orders:
                        tw_lo_m, tw_hi_m = _expand_tw_interval(osym, w_tw, r_lo_16, r_up_16)
                        tw_lo_m, tw_hi_m = _apply_red_maps(
                            osym, tw_lo_m, tw_hi_m, post_red_sell_fixed, post_red_buy_fixed, post_red_sell_floor, post_red_buy_ceil
                        )
                        for psym, pside, raw_ef in post_prot_ef_orders:
                            if psym == osym:
                                w_red = raw_ef * r_up_16
                                if pside == "sell" and tw_lo_m > 0.0:
                                    tw_lo_m = max(0.0, tw_lo_m - w_red)
                                elif pside == "buy" and tw_hi_m < 0.0:
                                    tw_hi_m = min(0.0, tw_hi_m + w_red)
                        if tw_lo_m < lo_15.get(osym, 0.0):
                            lo_15[osym] = tw_lo_m
                        if tw_hi_m > hi_15.get(osym, 0.0):
                            hi_15[osym] = tw_hi_m
                pre_ef_lo = {s: lo_15.get(s, 0.0) - rest_sell_fixed.get(s, 0.0) for s in all_known_syms}
                pre_ef_hi = {s: hi_15.get(s, 0.0) + rest_buy_fixed.get(s, 0.0) for s in all_known_syms}
                ef_buy_add, ef_sell_add, r_up_ef, r_lo_ef = _simulate_ef_additions(pre_ef_lo, pre_ef_hi)
                next_r_up = r_up_ef if r_up_ef > r_up_16 else r_up_16
                next_r_lo = r_lo_ef if r_lo_ef < r_lo_16 else r_lo_16
                if is_marked_basis or not rest_tw_orders or (
                    next_r_up <= r_up_16 + 1e-12 and next_r_lo >= r_lo_16 - 1e-12
                ):
                    r_up_16, r_lo_16 = next_r_up, next_r_lo
                    break
                r_up_16, r_lo_16 = next_r_up, next_r_lo

            rb_eff = {
                s: rest_buy_fixed.get(s, 0.0) + ef_buy_add.get(s, 0.0)
                for s in all_known_syms
            }
            rs_eff = {
                s: rest_sell_fixed.get(s, 0.0) + ef_sell_add.get(s, 0.0)
                for s in all_known_syms
            }
            return lo_15, hi_15, rb_eff, rs_eff, r_up_16, r_lo_16

        # Baseline per-symbol signed exposure bounds (lo, hi) BEFORE admitting any targets (CR10-03 / CR12-02).
        lo_base_0, hi_base_0, rb_base_0, rs_base_0, _, _ = _eval_phase_15_16({}, {})
        base_sym_bounds_0: dict[str, tuple[float, float]] = {
            s: (
                lo_base_0.get(s, 0.0) - rs_base_0.get(s, 0.0),
                hi_base_0.get(s, 0.0) + rb_base_0.get(s, 0.0),
            )
            for s in all_known_syms
        }

        def _sym_sides_single_ok(
            sym_s: str,
            lo_s: float,
            hi_s: float,
            pre_bounds: Mapping[str, tuple[float, float]],
            tol: float = 0.0,
        ) -> bool:
            """Check long and short exposure sides independently against single_cap and pre-existing bounds (CR12-01 / CR12-02)."""
            pre_lo, pre_hi = pre_bounds.get(sym_s, (0.0, 0.0))
            if hi_s > single_cap + tol and hi_s > pre_hi + 1e-12:
                return False
            if (-lo_s) > single_cap + tol and (-lo_s) > (-pre_lo) + 1e-12:
                return False
            return True

        def _other_syms_single_ok(
            l15: Mapping[str, float],
            h15: Mapping[str, float],
            rbe: Mapping[str, float],
            rse: Mapping[str, float],
            *,
            exclude_sym: str | None = None,
            tol: float = 0.0,
        ) -> bool:
            if is_marked_basis:
                return True
            for s in all_known_syms:
                if s == exclude_sym:
                    continue
                o_lo = l15.get(s, 0.0) - rse.get(s, 0.0)
                o_hi = h15.get(s, 0.0) + rbe.get(s, 0.0)
                if not _sym_sides_single_ok(s, o_lo, o_hi, base_sym_bounds_0, tol=tol):
                    return False
            return True

        def _single_env_w(cand_w: float, sym_t: str, tol: float = 0.0) -> tuple[float, float, bool]:
            t_lo_w, t_hi_w = _target_marked_interval(sym_t, cand_w)
            l15, h15, rbe, rse, _, _ = _eval_phase_15_16({sym_t: t_lo_w}, {sym_t: t_hi_w})
            others_ok = _other_syms_single_ok(l15, h15, rbe, rse, exclude_sym=sym_t, tol=tol)
            return l15.get(sym_t, 0.0) - rse.get(sym_t, 0.0), h15.get(sym_t, 0.0) + rbe.get(sym_t, 0.0), others_ok

        # 2. Clamp individual TargetPositions against single_cap AND resting orders using BOTH target
        #    sizing bounds [target_lower_ratio, target_upper_ratio] AND cross-symbol single-cap impact
        #    (CR2-29 / CR7-10 / CR8-05 / CR9-04 / CR9-05 / CR10-02 / CR10-03).
        clamped_targets: list[TargetPosition] = []
        targeted_symbols: set[str] = set()
        for t in output.targets:
            tsym = t.symbol
            w_cur = t.target_weight
            ew = existing_weights.get(tsym, 0.0)

            pre_lo_2 = lo_base_0.get(tsym, 0.0) - rs_base_0.get(tsym, 0.0)
            pre_hi_2 = hi_base_0.get(tsym, 0.0) + rb_base_0.get(tsym, 0.0)
            e_lo, e_hi, _ = _single_env_w(w_cur, tsym, tol=1e-12)
            t_lo_init, t_hi_init = _target_marked_interval(tsym, w_cur)
            # Same-sign clamping toward 0.0 (standard cap against single_cap + resting orders);
            # preserve risk-reducing targets that do not worsen a pre-existing same-side breach (CR11-03).
            if w_cur > 0.0 and e_hi > single_cap + 1e-12 and not (t_hi_init <= ew + 1e-12 and e_hi <= pre_hi_2 + 1e-12):
                if _single_env_w(0.0, tsym, tol=1e-12)[1] <= single_cap + 1e-12:
                    lo_b, hi_b = 0.0, w_cur
                    for _ in range(52):
                        mid = 0.5 * (lo_b + hi_b)
                        if _single_env_w(mid, tsym)[1] <= single_cap:
                            lo_b = mid
                        else:
                            hi_b = mid
                    w_cur = lo_b
                else:
                    w_cur = 0.0
            elif w_cur < 0.0 and -e_lo > single_cap + 1e-12 and not (t_lo_init >= ew - 1e-12 and (-e_lo) <= (-pre_lo_2) + 1e-12):
                if -_single_env_w(0.0, tsym, tol=1e-12)[0] <= single_cap + 1e-12:
                    lo_b, hi_b = w_cur, 0.0
                    for _ in range(52):
                        mid = 0.5 * (lo_b + hi_b)
                        if -_single_env_w(mid, tsym)[0] <= single_cap:
                            hi_b = mid
                        else:
                            lo_b = mid
                    w_cur = hi_b
                else:
                    w_cur = 0.0

            # Opposing-side & cross-symbol settled-equity check (CR8-05 / CR9-05 / CR10-03):
            # reducing a position can let a resting opposing order or another symbol's resting ef order
            # breach single_cap; pull w_cur back toward hold (omitting reversals that would flip sign, CR10-02).
            e_lo, e_hi, others_ok = _single_env_w(w_cur, tsym, tol=1e-12)
            t_lo_cur, t_hi_cur = _target_marked_interval(tsym, w_cur)
            if ew > 0.0 and t_lo_cur < ew and (-e_lo > single_cap + 1e-12 or not others_ok):
                ref_r = target_lower_ratio if target_lower_ratio > 0.0 else target_upper_ratio
                cap_hi_w = (ew / ref_r) if ref_r > 0.0 else w_cur
                if cap_hi_w > w_cur and _single_env_w(cap_hi_w, tsym, tol=1e-12)[1] > single_cap + 1e-12:
                    lo_c, hi_c = w_cur, cap_hi_w
                    for _ in range(52):
                        mid = 0.5 * (lo_c + hi_c)
                        if _single_env_w(mid, tsym)[1] <= single_cap:
                            lo_c = mid
                        else:
                            hi_c = mid
                    cap_hi_w = lo_c
                if cap_hi_w > w_cur:
                    hi_lo, _, hi_oth = _single_env_w(cap_hi_w, tsym, tol=1e-12)
                    if -hi_lo <= single_cap + 1e-12 and hi_oth:
                        lo_b, hi_b = w_cur, cap_hi_w
                        for _ in range(52):
                            mid = 0.5 * (lo_b + hi_b)
                            m_lo, _, m_oth = _single_env_w(mid, tsym)
                            if -m_lo <= single_cap and m_oth:
                                hi_b = mid
                            else:
                                lo_b = mid
                        w_cur = hi_b
                    else:
                        w_cur = cap_hi_w
            elif ew < 0.0 and t_hi_cur > ew and (e_hi > single_cap + 1e-12 or not others_ok):
                ref_r = target_lower_ratio if target_lower_ratio > 0.0 else target_upper_ratio
                cap_lo_w = (ew / ref_r) if ref_r > 0.0 else w_cur
                if cap_lo_w < w_cur and -_single_env_w(cap_lo_w, tsym, tol=1e-12)[0] > single_cap + 1e-12:
                    lo_c, hi_c = cap_lo_w, w_cur
                    for _ in range(52):
                        mid = 0.5 * (lo_c + hi_c)
                        if -_single_env_w(mid, tsym)[0] <= single_cap:
                            hi_c = mid
                        else:
                            lo_c = mid
                    cap_lo_w = hi_c
                if cap_lo_w < w_cur:
                    _, lo_hi, lo_oth = _single_env_w(cap_lo_w, tsym, tol=1e-12)
                    if lo_hi <= single_cap + 1e-12 and lo_oth:
                        lo_b, hi_b = cap_lo_w, w_cur
                        for _ in range(52):
                            mid = 0.5 * (lo_b + hi_b)
                            _, m_hi, m_oth = _single_env_w(mid, tsym)
                            if m_hi <= single_cap and m_oth:
                                lo_b = mid
                            else:
                                hi_b = mid
                        w_cur = lo_b
                    else:
                        w_cur = cap_lo_w

            # CR10-02 / CR10-03 / CR11-03: Never emit a flipped-sign reversal target, a target that
            # breaches another symbol's cap, or a target that creates a new breach on the same symbol.
            safe_t = _safe_replace_target(t, w_cur)
            if safe_t is None:
                continue
            fin_lo_2, fin_hi_2, fin_oth_2 = _single_env_w(safe_t.target_weight, tsym, tol=1e-12)
            pre_lo_2 = lo_base_0.get(tsym, 0.0) - rs_base_0.get(tsym, 0.0)
            pre_hi_2 = hi_base_0.get(tsym, 0.0) + rb_base_0.get(tsym, 0.0)
            if (
                (not is_marked_basis and not fin_oth_2)
                or fin_hi_2 > max(single_cap, pre_hi_2) + 1e-12
                or (-fin_lo_2) > max(single_cap, -pre_lo_2) + 1e-12
            ):
                continue
            clamped_targets.append(safe_t)
            targeted_symbols.add(tsym)
            all_known_syms.add(tsym)

        # 3. Enforce max_gross_leverage AND full-portfolio single-symbol caps across all untouched + targeted
        #    symbols in marked-equity units, reserving unscalable/reduced targets' full worst-case exposure
        #    and verifying exact post-scaling gross + per-symbol caps (CR4-01 / CR5-04 / CR7-10 / CR8-04 /
        #    CR8-05 / CR9-05 / CR9-06 / CR10-02 / CR10-03).
        if clamped_targets:
            def _eval_targets_gross(
                t_seq: Sequence[TargetPosition],
                tol: float = 1e-12,
            ) -> tuple[float, float, dict[str, float], bool, dict[str, float], dict[str, float]]:
                s_lo: dict[str, float] = {}
                s_hi: dict[str, float] = {}
                active_tsyms: set[str] = set()
                for t_item in t_seq:
                    ts = t_item.symbol
                    active_tsyms.add(ts)
                    tl, th = _target_marked_interval(ts, t_item.target_weight)
                    s_lo[ts] = tl if ts not in s_lo else min(s_lo[ts], tl)
                    s_hi[ts] = th if ts not in s_hi else max(s_hi[ts], th)
                la, ha, rba, rsa, _, _ = _eval_phase_15_16(s_lo, s_hi)
                u_gross = 0.0
                all_single_ok = True
                for us in all_known_syms:
                    if us not in active_tsyms:
                        u_lo = la.get(us, 0.0) - rsa.get(us, 0.0)
                        u_hi = ha.get(us, 0.0) + rba.get(us, 0.0)
                        u_gross += max(abs(u_lo), abs(u_hi))
                        if not _sym_sides_single_ok(us, u_lo, u_hi, base_sym_bounds_0, tol=tol):
                            all_single_ok = False
                sw: dict[str, float] = {}
                for ts in active_tsyms:
                    t_lo = la.get(ts, 0.0) - rsa.get(ts, 0.0)
                    t_hi = ha.get(ts, 0.0) + rba.get(ts, 0.0)
                    sw[ts] = max(abs(t_lo), abs(t_hi))
                    if not _sym_sides_single_ok(ts, t_lo, t_hi, base_sym_bounds_0, tol=tol):
                        all_single_ok = False
                return u_gross, math.fsum(sw.values()), sw, all_single_ok, rba, rsa

            untouched_gross, total_target_worst, sym_worst, single_ok_init, rb_all, rs_all = _eval_targets_gross(clamped_targets)
            allowed_targeted_gross = max(0.0, self.max_gross_leverage - untouched_gross)
            if total_target_worst > allowed_targeted_gross + 1e-12:
                zero_map = {tsym: 0.0 for tsym in targeted_symbols}
                lo_z, hi_z, rb_z, rs_z, _, _ = _eval_phase_15_16(zero_map, zero_map)
                lo_ew, hi_ew, rb_ew, rs_ew, _, _ = _eval_phase_15_16({}, {})
                base_pos_w: dict[str, float] = {}
                base_neg_w: dict[str, float] = {}
                for tsym in targeted_symbols:
                    fl_z = max(abs(lo_z.get(tsym, 0.0) - rs_z.get(tsym, 0.0)), abs(hi_z.get(tsym, 0.0) + rb_z.get(tsym, 0.0)))
                    fl_e = max(abs(lo_ew.get(tsym, 0.0) - rs_ew.get(tsym, 0.0)), abs(hi_ew.get(tsym, 0.0) + rb_ew.get(tsym, 0.0)))
                    fl = fl_z if fl_z <= fl_e else fl_e
                    bp_m = max(0.0, fl - rb_all.get(tsym, 0.0))
                    bn_m = max(0.0, fl - rs_all.get(tsym, 0.0))
                    base_pos_w[tsym] = (bp_m / target_upper_ratio) if target_upper_ratio > 0.0 else 0.0
                    base_neg_w[tsym] = (bn_m / target_upper_ratio) if target_upper_ratio > 0.0 else 0.0

                def _scale_targets_by(r_val: float) -> list[TargetPosition]:
                    out_t: list[TargetPosition] = []
                    for t_item in clamped_targets:
                        ts = t_item.symbol
                        wc = t_item.target_weight
                        if wc > 0.0 and wc > base_pos_w[ts] + 1e-12:
                            ws = base_pos_w[ts] + (wc - base_pos_w[ts]) * r_val
                        elif wc < 0.0 and (-wc) > base_neg_w[ts] + 1e-12:
                            ws = -(base_neg_w[ts] + (-wc - base_neg_w[ts]) * r_val)
                        else:
                            ws = wc
                        st = _safe_replace_target(t_item, ws)
                        if st is not None:
                            out_t.append(st)
                    return out_t

                # CR9-06: Compute exact unscalable floor (including full worst-case exposure of targets
                # at/below baseline that _scale_targets_by(0.0) leaves unchanged) before allocating ratio.
                scaled_zero = _scale_targets_by(0.0)
                u_0, tot_0, worst_0, single_ok_0, _, _ = _eval_targets_gross(scaled_zero)
                inc_contribs = {tsym: max(0.0, sym_worst.get(tsym, 0.0) - worst_0.get(tsym, 0.0)) for tsym in targeted_symbols}
                inc_sum = math.fsum(inc_contribs.values())
                rem_for_inc = max(0.0, self.max_gross_leverage - u_0 - tot_0)
                if inc_sum > 1e-12:
                    ratio = min(1.0, rem_for_inc / inc_sum)
                    cand_scaled = _scale_targets_by(ratio)
                    u_p, tot_p, _, single_ok_p, _, _ = _eval_targets_gross(cand_scaled)
                    if u_p + tot_p > self.max_gross_leverage + 1e-12 or not single_ok_p:
                        if u_0 + tot_0 <= self.max_gross_leverage + 1e-12 and single_ok_0:
                            lo_r, hi_r = 0.0, ratio
                            for _ in range(48):
                                mid_r = 0.5 * (lo_r + hi_r)
                                um, tm, _, sm_ok, _, _ = _eval_targets_gross(_scale_targets_by(mid_r), tol=0.0)
                                if um + tm <= self.max_gross_leverage and sm_ok:
                                    lo_r = mid_r
                                else:
                                    hi_r = mid_r
                            cand_scaled = _scale_targets_by(lo_r)
                        else:
                            cand_scaled = scaled_zero
                    clamped_targets = cand_scaled
                elif u_0 + tot_0 <= self.max_gross_leverage + 1e-12 and single_ok_0:
                    clamped_targets = scaled_zero

            # Secondary post-scaling & portfolio-wide single-cap guard (CR9-06 / CR10-02 / CR10-03):
            # if unscalable reducing targets or joint profit realization still leave gross > max_gross_leverage
            # or push any symbol over single_cap, pull reducing targets smoothly toward hold (never flipping
            # reversal signs; omitting infeasible reversals or offending targets if required).
            u_fin, tot_fin, _, single_ok_fin, _, _ = _eval_targets_gross(clamped_targets)
            if u_fin + tot_fin > self.max_gross_leverage + 1e-12 or not single_ok_fin:
                ref_r_hold = target_lower_ratio if target_lower_ratio > 0.0 else target_upper_ratio
                max_hold_w = (single_cap / target_upper_ratio) if target_upper_ratio > 0.0 else single_cap

                def _pull_toward_hold(alpha: float, *, omit_reversals: bool = False) -> list[TargetPosition]:
                    pulled: list[TargetPosition] = []
                    for t_item in clamped_targets:
                        ts = t_item.symbol
                        ew_s = existing_weights.get(ts, 0.0)
                        hold_w = (ew_s / ref_r_hold) if ref_r_hold > 0.0 else 0.0
                        if hold_w > max_hold_w:
                            hold_w = max_hold_w
                        elif hold_w < -max_hold_w:
                            hold_w = -max_hold_w
                        wc = t_item.target_weight
                        if (ew_s > 0.0 and wc < 0.0) or (ew_s < 0.0 and wc > 0.0):
                            if omit_reversals:
                                continue
                            wp = wc * (1.0 - alpha)
                            st = _safe_replace_target(t_item, wp)
                            if st is not None:
                                pulled.append(st)
                        elif (ew_s > 0.0 and wc < hold_w) or (ew_s < 0.0 and wc > hold_w):
                            wp = wc + (hold_w - wc) * alpha
                            st = _safe_replace_target(t_item, wp)
                            if st is not None:
                                pulled.append(st)
                        else:
                            pulled.append(t_item)
                    return pulled

                resolved_step3 = False
                for omit_rev in (False, True):
                    cand_h = _pull_toward_hold(1.0, omit_reversals=omit_rev)
                    u_h, tot_h, _, single_ok_h, _, _ = _eval_targets_gross(cand_h)
                    if u_h + tot_h <= self.max_gross_leverage + 1e-12 and single_ok_h:
                        lo_a, hi_a = 0.0, 1.0
                        for _ in range(48):
                            mid_a = 0.5 * (lo_a + hi_a)
                            ua, ta, _, s_ok_a, _, _ = _eval_targets_gross(
                                _pull_toward_hold(mid_a, omit_reversals=omit_rev), tol=0.0
                            )
                            if ua + ta <= self.max_gross_leverage and s_ok_a:
                                hi_a = mid_a
                            else:
                                lo_a = mid_a
                        clamped_targets = _pull_toward_hold(hi_a, omit_reversals=omit_rev)
                        resolved_step3 = True
                        break
                if not resolved_step3:
                    # CR13-01: Every emitted fallback MUST satisfy both per-symbol caps and the gross cap
                    # (never exceeding max_gross_leverage when the no-target envelope fits, and never worse
                    # than the no-target envelope under a pre-existing breach).
                    u_base, tot_base, _, _, _, _ = _eval_targets_gross(())
                    no_target_gross = u_base + tot_base
                    fallback_gross_cap = (
                        self.max_gross_leverage
                        if no_target_gross <= self.max_gross_leverage + 1e-12
                        else no_target_gross
                    )
                    accepted_fallback = False
                    cand_h1 = _pull_toward_hold(1.0, omit_reversals=True)
                    u_h1, tot_h1, _, single_ok_h1, _, _ = _eval_targets_gross(cand_h1)
                    if (
                        single_ok_h1
                        and u_h1 + tot_h1 <= fallback_gross_cap + 1e-12
                        and (u_h1 + tot_h1 < u_fin + tot_fin - 1e-12 or not single_ok_fin)
                    ):
                        clamped_targets = cand_h1
                        accepted_fallback = True
                    if not accepted_fallback and allowed_targeted_gross <= 1e-12 and single_ok_fin:
                        z_t = [st for t_item in clamped_targets if (st := _safe_replace_target(t_item, 0.0)) is not None]
                        uz, tz, _, single_ok_z, _, _ = _eval_targets_gross(z_t)
                        if (
                            single_ok_z
                            and uz + tz <= fallback_gross_cap + 1e-12
                            and uz + tz < u_fin + tot_fin - 1e-12
                        ):
                            clamped_targets = z_t
                            accepted_fallback = True
                    if not accepted_fallback:
                        # Final fail-closed fallback (CR10-03 / CR13-01): retain only a compliant subset
                        # satisfying both single caps and fallback_gross_cap (restoring [] if none fits).
                        kept_step3: list[TargetPosition] = []
                        for t_item in clamped_targets:
                            trial = kept_step3 + [t_item]
                            u_tr, tot_tr, _, s_ok_tr, _, _ = _eval_targets_gross(trial)
                            if s_ok_tr and u_tr + tot_tr <= fallback_gross_cap + 1e-12:
                                kept_step3 = trial
                        clamped_targets = kept_step3
                targeted_symbols = {t_item.symbol for t_item in clamped_targets}

        # 4. Seed conservative reachable weight state per symbol (in marked-equity units) from the exact
        #    post-target Phase 1.5/1.6 evaluator, separating absolute target bounds [base_lo, base_hi],
        #    fixed incremental additions, and first-class per-symbol fractional lists (CR9-04 / CR10-04 /
        #    CR10-05 / CR10-08).
        tracked_syms: set[str] = set(all_known_syms)
        target_lo_by_sym: dict[str, float] = {}
        target_hi_by_sym: dict[str, float] = {}
        for t in clamped_targets:
            tsym = t.symbol
            t_lo_m, t_hi_m = _target_marked_interval(tsym, t.target_weight)
            target_lo_by_sym[tsym] = t_lo_m if tsym not in target_lo_by_sym else min(target_lo_by_sym[tsym], t_lo_m)
            target_hi_by_sym[tsym] = t_hi_m if tsym not in target_hi_by_sym else max(target_hi_by_sym[tsym], t_hi_m)

        base_lo, base_hi, _, _, init_r_up, _ = _eval_phase_15_16(target_lo_by_sym, target_hi_by_sym)
        add_buy_fixed: dict[str, float] = {s: rest_buy_fixed.get(s, 0.0) for s in all_known_syms}
        add_sell_fixed: dict[str, float] = {s: rest_sell_fixed.get(s, 0.0) for s in all_known_syms}
        dyn_ef_buys: dict[str, list[float]] = {s: list(efs) for s, efs in rest_ef_buys.items()}
        dyn_ef_sells: dict[str, list[float]] = {s: list(efs) for s, efs in rest_ef_sells.items()}
        dyn_tw_orders: list[tuple[str, float]] = list(rest_tw_orders)
        dyn_ef_buy_sum: dict[str, float] = dict(rest_ef_buy_sum) if is_marked_basis else {}
        dyn_ef_sell_sum: dict[str, float] = dict(rest_ef_sell_sum) if is_marked_basis else {}
        has_dyn_ef = (not is_marked_basis) and bool(dyn_ef_buys or dyn_ef_sells or dyn_tw_orders)

        def _apply_all_protective_to_target(sym_p: str, lo_val: float, hi_val: float, r_up: float) -> tuple[float, float]:
            lo_val, hi_val = _apply_red_maps(
                sym_p, lo_val, hi_val, mkt_red_sell, mkt_red_buy, mkt_red_sell_floor, mkt_red_buy_ceil
            )
            lo_val, hi_val = _apply_red_maps(
                sym_p, lo_val, hi_val, post_red_sell_fixed, post_red_buy_fixed, post_red_sell_floor, post_red_buy_ceil
            )
            for psym, pside, raw_ef in post_prot_ef_orders:
                if psym == sym_p:
                    w_red = raw_ef * r_up
                    if pside == "sell" and lo_val > 0.0:
                        lo_val = max(0.0, lo_val - w_red)
                    elif pside == "buy" and hi_val < 0.0:
                        hi_val = min(0.0, hi_val + w_red)
            return lo_val, hi_val

        def _step4_state(
            extra_lo: tuple[str, float] | None = None,
            extra_hi: tuple[str, float] | None = None,
            extra_sell: tuple[str, float] | None = None,
            extra_buy: tuple[str, float] | None = None,
            extra_ef: tuple[str, str, float] | None = None,
        ) -> tuple[float, float, dict[str, float], dict[str, float], Mapping[str, float], Mapping[str, float]]:
            """Return (r_up, r_lo, ef_buy_adds, ef_sell_adds, eff_base_lo, eff_base_hi) at fixed point (CR9-04 / CR10-04 / CR10-05 / CR10-08 / CR11-01 / CR11-02)."""
            has_ex_ef = extra_ef is not None and extra_ef[2] > 0.0
            if is_marked_basis:
                if extra_lo is None and extra_hi is None and not has_ex_ef:
                    return 1.0, 1.0, dyn_ef_buy_sum, dyn_ef_sell_sum, base_lo, base_hi
                blo_m: Mapping[str, float] = base_lo
                bhi_m: Mapping[str, float] = base_hi
                if extra_lo is not None and extra_lo[1] < base_lo.get(extra_lo[0], 0.0):
                    blo_m = dict(base_lo)
                    blo_m[extra_lo[0]] = extra_lo[1]
                if extra_hi is not None and extra_hi[1] > base_hi.get(extra_hi[0], 0.0):
                    bhi_m = dict(base_hi)
                    bhi_m[extra_hi[0]] = extra_hi[1]
                if not has_ex_ef:
                    return 1.0, 1.0, dyn_ef_buy_sum, dyn_ef_sell_sum, blo_m, bhi_m
                ef_bm = dict(dyn_ef_buy_sum)
                ef_sm = dict(dyn_ef_sell_sum)
                if extra_ef is not None:
                    esym, eside, eval_ = extra_ef
                    if eside == "buy":
                        ef_bm[esym] = ef_bm.get(esym, 0.0) + eval_
                    else:
                        ef_sm[esym] = ef_sm.get(esym, 0.0) + eval_
                return 1.0, 1.0, ef_bm, ef_sm, blo_m, bhi_m

            eff_blo: dict[str, float] = dict(base_lo)
            eff_bhi: dict[str, float] = dict(base_hi)
            if extra_lo is not None:
                s_l, v_l = extra_lo
                if v_l < eff_blo.get(s_l, 0.0):
                    eff_blo[s_l] = v_l
            if extra_hi is not None:
                s_h, v_h = extra_hi
                if v_h > eff_bhi.get(s_h, 0.0):
                    eff_bhi[s_h] = v_h

            syms_eval = set(tracked_syms)
            for ext in (extra_lo, extra_hi, extra_sell, extra_buy):
                if ext is not None:
                    syms_eval.add(ext[0])
            if has_ex_ef and extra_ef is not None:
                syms_eval.add(extra_ef[0])
            for tw_s, _ in dyn_tw_orders:
                syms_eval.add(tw_s)

            pre_lo: dict[str, float] = {}
            pre_hi: dict[str, float] = {}
            for s in syms_eval:
                asf = add_sell_fixed.get(s, 0.0) + (extra_sell[1] if (extra_sell is not None and s == extra_sell[0]) else 0.0)
                abf = add_buy_fixed.get(s, 0.0) + (extra_buy[1] if (extra_buy is not None and s == extra_buy[0]) else 0.0)
                pre_lo[s] = eff_blo.get(s, 0.0) - asf
                pre_hi[s] = eff_bhi.get(s, 0.0) + abf

            r_up, r_lo = _settled_ratios_for_bounds(
                lambda s, _lo=pre_lo: _lo.get(s, 0.0),
                lambda s, _hi=pre_hi: _hi.get(s, 0.0),
            )
            ef_b: dict[str, float] = {}
            ef_s: dict[str, float] = {}
            # Fixed-point closure between resting/admitted target_weight orders and fractional replay (CR11-01 / CR11-02).
            for _ in range(4):
                if dyn_tw_orders:
                    for osym, w_tw in dyn_tw_orders:
                        tw_lo_raw, tw_hi_raw = _expand_tw_interval(osym, w_tw, r_lo, r_up)
                        tw_lo_m, tw_hi_m = _apply_all_protective_to_target(osym, tw_lo_raw, tw_hi_raw, r_up)
                        if tw_lo_m < eff_blo.get(osym, 0.0):
                            eff_blo[osym] = tw_lo_m
                        if tw_hi_m > eff_bhi.get(osym, 0.0):
                            eff_bhi[osym] = tw_hi_m
                        asf = add_sell_fixed.get(osym, 0.0) + (extra_sell[1] if (extra_sell is not None and osym == extra_sell[0]) else 0.0)
                        abf = add_buy_fixed.get(osym, 0.0) + (extra_buy[1] if (extra_buy is not None and osym == extra_buy[0]) else 0.0)
                        pre_lo[osym] = eff_blo.get(osym, 0.0) - asf
                        pre_hi[osym] = eff_bhi.get(osym, 0.0) + abf

                if dyn_ef_buys or dyn_ef_sells or has_ex_ef:
                    ef_b, ef_s, r_up_post, r_lo_post = _simulate_ef_additions(
                        pre_lo, pre_hi, dyn_ef_buys, dyn_ef_sells, extra_ef=extra_ef
                    )
                else:
                    ef_b, ef_s = {}, {}
                    r_up_post, r_lo_post = _settled_ratios_for_bounds(
                        lambda s, _lo=pre_lo: _lo.get(s, 0.0),
                        lambda s, _hi=pre_hi: _hi.get(s, 0.0),
                    )
                next_r_up = r_up_post if r_up_post > r_up else r_up
                next_r_lo = r_lo_post if r_lo_post < r_lo else r_lo
                if not dyn_tw_orders or (next_r_up <= r_up + 1e-12 and next_r_lo >= r_lo - 1e-12):
                    r_up, r_lo = next_r_up, next_r_lo
                    break
                r_up, r_lo = next_r_up, next_r_lo

            return r_up, r_lo, ef_b, ef_s, eff_blo, eff_bhi

        def _sym_signed_bounds(
            sym_w: str,
            ef_b: Mapping[str, float],
            ef_s: Mapping[str, float],
            blo_map: Mapping[str, float] = base_lo,
            bhi_map: Mapping[str, float] = base_hi,
        ) -> tuple[float, float]:
            lo_s = blo_map.get(sym_w, 0.0) - (add_sell_fixed.get(sym_w, 0.0) + ef_s.get(sym_w, 0.0))
            hi_s = bhi_map.get(sym_w, 0.0) + (add_buy_fixed.get(sym_w, 0.0) + ef_b.get(sym_w, 0.0))
            return lo_s, hi_s

        def _other_portfolio_check(
            sym_chk: str,
            sym_lo_after: float,
            sym_hi_after: float,
            ef_b_after: Mapping[str, float],
            ef_s_after: Mapping[str, float],
            blo_after: Mapping[str, float],
            bhi_after: Mapping[str, float],
            cur_sym_bounds: Mapping[str, tuple[float, float]],
            other_before: float,
            tol: float = 0.0,
        ) -> tuple[bool, float]:
            """Verify cross-symbol per-side single_cap & total gross (CR10-05 / CR12-01); returns (ok, other_gross_after)."""
            other_after = 0.0
            for s in tracked_syms:
                if s == sym_chk:
                    continue
                o_lo, o_hi = _sym_signed_bounds(s, ef_b_after, ef_s_after, blo_after, bhi_after)
                a_lo = -o_lo if o_lo < 0.0 else o_lo
                a_hi = -o_hi if o_hi < 0.0 else o_hi
                other_after += a_hi if a_hi >= a_lo else a_lo
                if not _sym_sides_single_ok(s, o_lo, o_hi, cur_sym_bounds, tol=tol):
                    return False, other_after
            if other_after > other_before + 1e-12:
                sym_w_after = max(-sym_lo_after if sym_lo_after < 0.0 else sym_lo_after, -sym_hi_after if sym_hi_after < 0.0 else sym_hi_after)
                if sym_w_after + other_after > self.max_gross_leverage + tol:
                    return False, other_after
            return True, other_after

        def _step4_tw_eval(
            sym_t: str,
            cand_w: float,
            *,
            b_lo_t: float,
            b_hi_t: float,
            cur_lo_r: float,
            cur_up_r: float,
            cur_sym_bounds: Mapping[str, tuple[float, float]],
            other_before: float,
            tol: float = 0.0,
        ) -> tuple[float, float, bool]:
            """Evaluate a candidate target_weight intent across the full portfolio envelope (CR10-05 / CR11-01 / CR11-02 / CR12-01)."""
            lp, hp = _expand_tw_interval(sym_t, cand_w, cur_lo_r, cur_up_r)
            ru_t, _, _, _, _, _ = _step4_state(extra_lo=(sym_t, lp), extra_hi=(sym_t, hp))
            la, ha = _apply_all_protective_to_target(sym_t, lp, hp, ru_t)
            bl_t = la if la < b_lo_t else b_lo_t
            bh_t = ha if ha > b_hi_t else b_hi_t
            _, _, ef_bt, ef_st, blo_t, bhi_t = _step4_state(extra_lo=(sym_t, bl_t), extra_hi=(sym_t, bh_t))
            lo_t = blo_t.get(sym_t, bl_t) - (add_sell_fixed.get(sym_t, 0.0) + ef_st.get(sym_t, 0.0))
            hi_t = bhi_t.get(sym_t, bh_t) + (add_buy_fixed.get(sym_t, 0.0) + ef_bt.get(sym_t, 0.0))
            pf_ok, other_after = _other_portfolio_check(
                sym_t, lo_t, hi_t, ef_bt, ef_st, blo_t, bhi_t, cur_sym_bounds, other_before, tol=tol
            )
            if not pf_ok:
                return lo_t, hi_t, False
            lim_t = min(single_cap, max(0.0, self.max_gross_leverage - other_after))
            # Check if other_after shrank headroom for sym_t itself.
            if other_after > other_before + 1e-12 and max(abs(lo_t), abs(hi_t)) > lim_t + tol:
                return lo_t, hi_t, False
            return lo_t, hi_t, True

        def _step4_order_ok(
            sym_o: str,
            side_str: str,
            *,
            b_lo_o: float,
            b_hi_o: float,
            lo_before: float,
            hi_before: float,
            cur_sym_bounds: Mapping[str, tuple[float, float]],
            other_before: float,
            extra_fixed_w: float = 0.0,
            extra_ef_val: float = 0.0,
            tol: float = 0.0,
        ) -> bool:
            """Check full-portfolio cap for a candidate contracts or equity_fraction order (CR10-04 / CR10-05 / CR12-01)."""
            _, _, ef_b_d, ef_s_d, blo_d, bhi_d = _step4_state(
                extra_buy=(sym_o, extra_fixed_w) if (side_str == "buy" and extra_fixed_w > 0.0) else None,
                extra_sell=(sym_o, extra_fixed_w) if (side_str == "sell" and extra_fixed_w > 0.0) else None,
                extra_ef=(sym_o, side_str, extra_ef_val) if extra_ef_val > 0.0 else None,
            )
            lo_d = blo_d.get(sym_o, b_lo_o) - (
                add_sell_fixed.get(sym_o, 0.0)
                + ef_s_d.get(sym_o, 0.0)
                + (extra_fixed_w if side_str == "sell" else 0.0)
            )
            hi_d = bhi_d.get(sym_o, b_hi_o) + (
                add_buy_fixed.get(sym_o, 0.0)
                + ef_b_d.get(sym_o, 0.0)
                + (extra_fixed_w if side_str == "buy" else 0.0)
            )
            pf_ok, other_d = _other_portfolio_check(
                sym_o, lo_d, hi_d, ef_b_d, ef_s_d, blo_d, bhi_d, cur_sym_bounds, other_before, tol=tol
            )
            if not pf_ok:
                return False
            lim_d = min(single_cap, max(0.0, self.max_gross_leverage - other_d))
            if side_str == "buy":
                if hi_d > lim_d + tol:
                    return False
                if (-lo_d) > (-lo_before) + 1e-12 and (-lo_d) > lim_d + tol:
                    return False
            else:
                if (-lo_d) > lim_d + tol:
                    return False
                if hi_d > hi_before + 1e-12 and hi_d > lim_d + tol:
                    return False
            return True

        clamped_intents: list[OrderIntent] = []

        for intent in output.intents:
            if intent.reduce_only or intent.qty_mode == "close_all":
                clamped_intents.append(intent)
                continue

            sym = intent.symbol
            cur_upper_ratio, cur_lower_ratio, cur_ef_b, cur_ef_s, cur_blo, cur_bhi = _step4_state()
            b_lo = cur_blo.get(sym, 0.0)
            b_hi = cur_bhi.get(sym, 0.0)
            ab = add_buy_fixed.get(sym, 0.0) + cur_ef_b.get(sym, 0.0)
            as_ = add_sell_fixed.get(sym, 0.0) + cur_ef_s.get(sym, 0.0)
            lo = b_lo - as_
            hi = b_hi + ab
            cur_sym_bounds = {s: _sym_signed_bounds(s, cur_ef_b, cur_ef_s, cur_blo, cur_bhi) for s in tracked_syms}
            other_gross = math.fsum(max(abs(b[0]), abs(b[1])) for s, b in cur_sym_bounds.items() if s != sym)
            max_abs_sym = min(single_cap, max(0.0, self.max_gross_leverage - other_gross))

            if intent.qty_mode == "target_weight":
                raw_w = intent.qty_value
                raw_w_m = raw_w * cur_upper_ratio
                if raw_w == 0.0:
                    desired = 0.0
                elif raw_w > 0.0:
                    max_pos_w = max_abs_sym - ab
                    if raw_w_m <= b_hi + 1e-12 and intent.side == "sell":
                        desired = raw_w
                    elif max_pos_w <= 1e-12:
                        continue
                    elif raw_w_m <= max_pos_w + 1e-12:
                        desired = raw_w
                    elif cur_upper_ratio > 0.0:
                        desired = max_pos_w / cur_upper_ratio
                    else:
                        desired = raw_w
                else:
                    min_neg_w = -(max_abs_sym - as_)
                    if raw_w_m >= b_lo - 1e-12 and intent.side == "buy":
                        desired = raw_w
                    elif min_neg_w >= -1e-12:
                        continue
                    elif raw_w_m >= min_neg_w - 1e-12:
                        desired = raw_w
                    elif cur_upper_ratio > 0.0:
                        desired = min_neg_w / cur_upper_ratio
                    else:
                        desired = raw_w
                if desired == 0.0:
                    desired = 0.0
                # CR8-05 / CR9-04 / CR9-05 / CR10-05: Check full portfolio envelope (same-symbol opposing
                # orders AND cross-symbol settled-equity single/gross caps) before admitting target_weight intent.
                has_sym_rest = has_dyn_ef or ab > 0.0 or as_ > 0.0
                if has_sym_rest and cur_upper_ratio > 0.0:
                    ew_s = existing_weights.get(sym, 0.0)
                    ref_r_tw = cur_lower_ratio if cur_lower_ratio > 0.0 else cur_upper_ratio
                    tw_lo_chk, tw_hi_chk, tw_pf_ok = _step4_tw_eval(
                        sym,
                        desired,
                        b_lo_t=b_lo,
                        b_hi_t=b_hi,
                        cur_lo_r=cur_lower_ratio,
                        cur_up_r=cur_upper_ratio,
                        cur_sym_bounds=cur_sym_bounds,
                        other_before=other_gross,
                        tol=1e-12,
                    )
                    if intent.side == "sell" and ew_s > 0.0 and (-tw_lo_chk > max_abs_sym + 1e-12 or not tw_pf_ok):
                        hi_w = ew_s / ref_r_tw
                        if hi_w > desired and _step4_tw_eval(
                            sym,
                            hi_w,
                            b_lo_t=b_lo,
                            b_hi_t=b_hi,
                            cur_lo_r=cur_lower_ratio,
                            cur_up_r=cur_upper_ratio,
                            cur_sym_bounds=cur_sym_bounds,
                            other_before=other_gross,
                            tol=1e-12,
                        )[1] > max_abs_sym + 1e-12:
                            lo_c, hi_c = desired, hi_w
                            for _ in range(48):
                                mid = 0.5 * (lo_c + hi_c)
                                if _step4_tw_eval(
                                    sym,
                                    mid,
                                    b_lo_t=b_lo,
                                    b_hi_t=b_hi,
                                    cur_lo_r=cur_lower_ratio,
                                    cur_up_r=cur_upper_ratio,
                                    cur_sym_bounds=cur_sym_bounds,
                                    other_before=other_gross,
                                )[1] <= max_abs_sym:
                                    lo_c = mid
                                else:
                                    hi_c = mid
                            hi_w = lo_c
                        if hi_w > desired:
                            hi_lo_v, _, hi_pf_v = _step4_tw_eval(
                                sym,
                                hi_w,
                                b_lo_t=b_lo,
                                b_hi_t=b_hi,
                                cur_lo_r=cur_lower_ratio,
                                cur_up_r=cur_upper_ratio,
                                cur_sym_bounds=cur_sym_bounds,
                                other_before=other_gross,
                                tol=1e-12,
                            )
                            if -hi_lo_v <= max_abs_sym + 1e-12 and hi_pf_v:
                                lo_b, hi_b = desired, hi_w
                                for _ in range(48):
                                    mid = 0.5 * (lo_b + hi_b)
                                    m_lo_v, _, m_pf_v = _step4_tw_eval(
                                        sym,
                                        mid,
                                        b_lo_t=b_lo,
                                        b_hi_t=b_hi,
                                        cur_lo_r=cur_lower_ratio,
                                        cur_up_r=cur_upper_ratio,
                                        cur_sym_bounds=cur_sym_bounds,
                                        other_before=other_gross,
                                    )
                                    if -m_lo_v <= max_abs_sym and m_pf_v:
                                        hi_b = mid
                                    else:
                                        lo_b = mid
                                desired = hi_b
                            else:
                                desired = hi_w
                    elif intent.side == "buy" and ew_s < 0.0 and (tw_hi_chk > max_abs_sym + 1e-12 or not tw_pf_ok):
                        lo_w = ew_s / ref_r_tw
                        if lo_w < desired and -_step4_tw_eval(
                            sym,
                            lo_w,
                            b_lo_t=b_lo,
                            b_hi_t=b_hi,
                            cur_lo_r=cur_lower_ratio,
                            cur_up_r=cur_upper_ratio,
                            cur_sym_bounds=cur_sym_bounds,
                            other_before=other_gross,
                            tol=1e-12,
                        )[0] > max_abs_sym + 1e-12:
                            lo_c, hi_c = lo_w, desired
                            for _ in range(48):
                                mid = 0.5 * (lo_c + hi_c)
                                if -_step4_tw_eval(
                                    sym,
                                    mid,
                                    b_lo_t=b_lo,
                                    b_hi_t=b_hi,
                                    cur_lo_r=cur_lower_ratio,
                                    cur_up_r=cur_upper_ratio,
                                    cur_sym_bounds=cur_sym_bounds,
                                    other_before=other_gross,
                                )[0] <= max_abs_sym:
                                    hi_c = mid
                                else:
                                    lo_c = mid
                            lo_w = hi_c
                        if lo_w < desired:
                            _, lo_hi_v, lo_pf_v = _step4_tw_eval(
                                sym,
                                lo_w,
                                b_lo_t=b_lo,
                                b_hi_t=b_hi,
                                cur_lo_r=cur_lower_ratio,
                                cur_up_r=cur_upper_ratio,
                                cur_sym_bounds=cur_sym_bounds,
                                other_before=other_gross,
                                tol=1e-12,
                            )
                            if lo_hi_v <= max_abs_sym + 1e-12 and lo_pf_v:
                                lo_b, hi_b = lo_w, desired
                                for _ in range(48):
                                    mid = 0.5 * (lo_b + hi_b)
                                    _, m_hi_v, m_pf_v = _step4_tw_eval(
                                        sym,
                                        mid,
                                        b_lo_t=b_lo,
                                        b_hi_t=b_hi,
                                        cur_lo_r=cur_lower_ratio,
                                        cur_up_r=cur_upper_ratio,
                                        cur_sym_bounds=cur_sym_bounds,
                                        other_before=other_gross,
                                    )
                                    if m_hi_v <= max_abs_sym and m_pf_v:
                                        lo_b = mid
                                    else:
                                        hi_b = mid
                                desired = lo_b
                            else:
                                desired = lo_w
                    # Final fail-closed portfolio & same-symbol bound check (CR10-05 / CR11-03):
                    # allow an existing breach that this intent does not worsen, but reject any new breach!
                    fin_lo, fin_hi, fin_pf_ok = _step4_tw_eval(
                        sym,
                        desired,
                        b_lo_t=b_lo,
                        b_hi_t=b_hi,
                        cur_lo_r=cur_lower_ratio,
                        cur_up_r=cur_upper_ratio,
                        cur_sym_bounds=cur_sym_bounds,
                        other_before=other_gross,
                        tol=1e-12,
                    )
                    if (
                        not fin_pf_ok
                        or fin_hi > max(max_abs_sym, hi) + 1e-12
                        or (-fin_lo) > max(max_abs_sym, -lo) + 1e-12
                    ):
                        continue
                clamped_intents.append(replace(intent, qty_value=desired))
                tracked_syms.add(sym)
                # CR7-05 / CR11-01 / CR11-02: Convert the effective target into marked-equity units
                # under the post-replay ratio interval and record non-zero targets in dyn_tw_orders.
                d_lo_pre, d_hi_pre = _expand_tw_interval(sym, desired, cur_lower_ratio, cur_upper_ratio)
                post_r_up, _, _, _, _, _ = _step4_state(extra_lo=(sym, d_lo_pre), extra_hi=(sym, d_hi_pre))
                d_lo, d_hi = _apply_all_protective_to_target(sym, d_lo_pre, d_hi_pre, post_r_up)
                base_lo[sym] = d_lo if d_lo < b_lo else b_lo
                base_hi[sym] = d_hi if d_hi > b_hi else b_hi
                if desired != 0.0 and intent.order_type != "market" and not is_marked_basis:
                    dyn_tw_orders.append((sym, desired))
                    has_dyn_ef = True
                add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                continue

            if intent.qty_mode == "equity_fraction":
                # CR7-06 / CR9-04 / CR10-04 / CR10-08: Under marked_equity, size in O(1); under settled_equity,
                # replay candidate alongside resting/admitted fractional orders across all feasible fill orderings!
                eff_ef = intent.qty_value
                if eff_enforce_inst_lev is True:
                    inst_ml_e = _inst_max_leverage(sym)
                    if inst_ml_e is not None and eff_ef > inst_ml_e:
                        eff_ef = inst_ml_e
                if is_marked_basis:
                    if intent.side == "buy":
                        if hi + eff_ef <= max_abs_sym + 1e-12:
                            clamped_intents.append(intent)
                            tracked_syms.add(sym)
                            dyn_ef_buy_sum[sym] = dyn_ef_buy_sum.get(sym, 0.0) + eff_ef
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                        else:
                            allowed_ef = max_abs_sym - hi
                            if allowed_ef > 1e-12:
                                clamped_intents.append(replace(intent, qty_value=allowed_ef))
                                tracked_syms.add(sym)
                                dyn_ef_buy_sum[sym] = dyn_ef_buy_sum.get(sym, 0.0) + allowed_ef
                                base_lo.setdefault(sym, b_lo)
                                base_hi.setdefault(sym, b_hi)
                                add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                    else:
                        if -(lo - eff_ef) <= max_abs_sym + 1e-12:
                            clamped_intents.append(intent)
                            tracked_syms.add(sym)
                            dyn_ef_sell_sum[sym] = dyn_ef_sell_sum.get(sym, 0.0) + eff_ef
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                        else:
                            allowed_ef = max_abs_sym + lo
                            if allowed_ef > 1e-12:
                                clamped_intents.append(replace(intent, qty_value=allowed_ef))
                                tracked_syms.add(sym)
                                dyn_ef_sell_sum[sym] = dyn_ef_sell_sum.get(sym, 0.0) + allowed_ef
                                base_lo.setdefault(sym, b_lo)
                                base_hi.setdefault(sym, b_hi)
                                add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                    continue

                admit_ef = _step4_order_ok(
                    sym,
                    intent.side,
                    b_lo_o=b_lo,
                    b_hi_o=b_hi,
                    lo_before=lo,
                    hi_before=hi,
                    cur_sym_bounds=cur_sym_bounds,
                    other_before=other_gross,
                    extra_ef_val=eff_ef,
                    tol=1e-12,
                )
                if admit_ef:
                    clamped_intents.append(intent)
                    tracked_syms.add(sym)
                    if intent.side == "buy":
                        dyn_ef_buys.setdefault(sym, []).append(eff_ef)
                    else:
                        dyn_ef_sells.setdefault(sym, []).append(eff_ef)
                    has_dyn_ef = True
                    base_lo.setdefault(sym, b_lo)
                    base_hi.setdefault(sym, b_hi)
                    add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                    add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                else:
                    room_ef = (max_abs_sym - hi) if intent.side == "buy" else (max_abs_sym + lo)
                    if room_ef > 1e-12 and eff_ef > 1e-12:
                        lo_ef, hi_ef = 0.0, eff_ef
                        for _ in range(48):
                            mid_ef = 0.5 * (lo_ef + hi_ef)
                            if _step4_order_ok(
                                sym,
                                intent.side,
                                b_lo_o=b_lo,
                                b_hi_o=b_hi,
                                lo_before=lo,
                                hi_before=hi,
                                cur_sym_bounds=cur_sym_bounds,
                                other_before=other_gross,
                                extra_ef_val=mid_ef,
                            ):
                                lo_ef = mid_ef
                            else:
                                hi_ef = mid_ef
                        if lo_ef > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=lo_ef))
                            tracked_syms.add(sym)
                            if intent.side == "buy":
                                dyn_ef_buys.setdefault(sym, []).append(lo_ef)
                            else:
                                dyn_ef_sells.setdefault(sym, []).append(lo_ef)
                            has_dyn_ef = True
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                            add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                continue

            if intent.qty_mode == "contracts":
                ref_px = intent.limit_price or intent.stop_price or self._reference_price(ctx, sym)
                if equity <= 0.0 or ref_px is None or ref_px <= 0.0:
                    # Fail closed (CR1-33): do not pass an unpriceable contracts order through the decision-time cap.
                    continue
                mult = _contract_multiplier(ctx, sym)
                order_weight = (intent.qty_value * mult * ref_px) / equity
                if intent.side == "buy":
                    admit_cb = (
                        _step4_order_ok(
                            sym,
                            "buy",
                            b_lo_o=b_lo,
                            b_hi_o=b_hi,
                            lo_before=lo,
                            hi_before=hi,
                            cur_sym_bounds=cur_sym_bounds,
                            other_before=other_gross,
                            extra_fixed_w=order_weight,
                            tol=1e-12,
                        )
                        if has_dyn_ef
                        else (hi + order_weight <= max_abs_sym + 1e-12)
                    )
                    if admit_cb:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_buy_fixed[sym] = add_buy_fixed.get(sym, 0.0) + order_weight
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                    else:
                        allowed_weight = max_abs_sym - hi
                        if has_dyn_ef and allowed_weight > 1e-12:
                            lo_b, hi_b = 0.0, min(order_weight, allowed_weight)
                            for _ in range(48):
                                mid = 0.5 * (lo_b + hi_b)
                                if _step4_order_ok(
                                    sym,
                                    "buy",
                                    b_lo_o=b_lo,
                                    b_hi_o=b_hi,
                                    lo_before=lo,
                                    hi_before=hi,
                                    cur_sym_bounds=cur_sym_bounds,
                                    other_before=other_gross,
                                    extra_fixed_w=mid,
                                ):
                                    lo_b = mid
                                else:
                                    hi_b = mid
                            allowed_weight = lo_b
                        allowed_qty = (allowed_weight * equity) / (mult * ref_px)
                        if allowed_qty > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_qty))
                            tracked_syms.add(sym)
                            add_buy_fixed[sym] = add_buy_fixed.get(sym, 0.0) + allowed_weight
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                else:
                    admit_cs = (
                        _step4_order_ok(
                            sym,
                            "sell",
                            b_lo_o=b_lo,
                            b_hi_o=b_hi,
                            lo_before=lo,
                            hi_before=hi,
                            cur_sym_bounds=cur_sym_bounds,
                            other_before=other_gross,
                            extra_fixed_w=order_weight,
                            tol=1e-12,
                        )
                        if has_dyn_ef
                        else (-(lo - order_weight) <= max_abs_sym + 1e-12)
                    )
                    if admit_cs:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_sell_fixed[sym] = add_sell_fixed.get(sym, 0.0) + order_weight
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                    else:
                        allowed_weight = max_abs_sym + lo
                        if has_dyn_ef and allowed_weight > 1e-12:
                            lo_b, hi_b = 0.0, min(order_weight, allowed_weight)
                            for _ in range(48):
                                mid = 0.5 * (lo_b + hi_b)
                                if _step4_order_ok(
                                    sym,
                                    "sell",
                                    b_lo_o=b_lo,
                                    b_hi_o=b_hi,
                                    lo_before=lo,
                                    hi_before=hi,
                                    cur_sym_bounds=cur_sym_bounds,
                                    other_before=other_gross,
                                    extra_fixed_w=mid,
                                ):
                                    lo_b = mid
                                else:
                                    hi_b = mid
                            allowed_weight = lo_b
                        allowed_qty = (allowed_weight * equity) / (mult * ref_px)
                        if allowed_qty > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_qty))
                            tracked_syms.add(sym)
                            add_sell_fixed[sym] = add_sell_fixed.get(sym, 0.0) + allowed_weight
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))

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

    supports_ephemeral_context: bool = True

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

        # Outside session: allow bounded exits/reductions (cannot reverse past flat), block new entries/scale-ups.
        kept_intents = _filter_reducing_intents(output.intents, ctx)
        kept_targets = _filter_reducing_targets(output.targets, ctx)

        if not kept_intents and not kept_targets:
            if output.diagnostics:
                return StrategyOutput(diagnostics=output.diagnostics)
            return EMPTY_OUTPUT
        return StrategyOutput(
            intents=kept_intents,
            targets=kept_targets,
            diagnostics=output.diagnostics,
        )

    def snapshot_state(self) -> dict[str, Any]:
        return {}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        return None
