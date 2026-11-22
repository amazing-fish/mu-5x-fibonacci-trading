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

        # 1. Summarize already-resting open orders AND protective reductions BEFORE admitting targets
        #    (CR2-29 / CR3-16 / CR5-05 / CR6-04 / CR6-05 / CR7-04 / CR7-06):
        #    - Non-reduce-only resting orders can still fill AFTER a queued TargetPosition fills.
        #    - Protective reduce_only / close_all orders (resting + same-output, deduplicated by intent_id)
        #      can shrink a positive long down toward 0.0 (never negative) or a negative short up toward 0.0
        #      (never positive) BEFORE a non-reduce-only opposing order executes!
        rest_buy: dict[str, float] = {}
        rest_sell: dict[str, float] = {}
        rest_tw_lo: dict[str, float] = {}
        rest_tw_hi: dict[str, float] = {}
        red_sell: dict[str, float] = {}
        red_buy: dict[str, float] = {}
        red_sell_floor: dict[str, float] = {}
        red_buy_ceil: dict[str, float] = {}
        all_known_syms: set[str] = set(existing_weights.keys())

        def _apply_protective_reductions(sym: str, lo_val: float, hi_val: float) -> tuple[float, float]:
            # Shrink only the inner side toward 0.0 (never crossing 0.0, preserving CR3-16 & CR1-31).
            if lo_val > 0.0:
                if sym in red_sell_floor and red_sell_floor[sym] < lo_val:
                    lo_val = red_sell_floor[sym]
                rs_red = red_sell.get(sym, 0.0)
                if rs_red > 0.0:
                    lo_val = max(0.0, lo_val - rs_red)
            if hi_val < 0.0:
                if sym in red_buy_ceil and red_buy_ceil[sym] > hi_val:
                    hi_val = red_buy_ceil[sym]
                rb_red = red_buy.get(sym, 0.0)
                if rb_red > 0.0:
                    hi_val = min(0.0, hi_val + rb_red)
            return lo_val, hi_val

        def _prot_only_lo(sym: str) -> float:
            ew = existing_weights.get(sym, 0.0)
            lo_v, _ = _apply_protective_reductions(sym, ew, ew)
            return lo_v

        def _prot_only_hi(sym: str) -> float:
            ew = existing_weights.get(sym, 0.0)
            _, hi_v = _apply_protective_reductions(sym, ew, ew)
            return hi_v

        def _record_protective_reduction(ord_obj: OrderIntent) -> None:
            osym = ord_obj.symbol
            all_known_syms.add(osym)
            qm = ord_obj.qty_mode
            if qm == "close_all":
                if ord_obj.side == "sell":
                    red_sell[osym] = math.inf
                else:
                    red_buy[osym] = math.inf
            elif qm == "equity_fraction":
                # CR7-04: Clamp protective equity_fraction by known instrument max_leverage when enforced;
                # size against current upper settled ratio (reflecting any prior profit-realizing reductions).
                raw_ef = ord_obj.qty_value
                if eff_enforce_inst_lev is True:
                    inst_ml_ef = _inst_max_leverage(osym)
                    if inst_ml_ef is not None and raw_ef > inst_ml_ef:
                        raw_ef = inst_ml_ef
                cur_prot_ef_ratio, _ = _settled_ratios_for_bounds(_prot_only_lo, _prot_only_hi)
                w_red_ef = raw_ef * cur_prot_ef_ratio
                if ord_obj.side == "sell":
                    red_sell[osym] = red_sell.get(osym, 0.0) + w_red_ef
                else:
                    red_buy[osym] = red_buy.get(osym, 0.0) + w_red_ef
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
                    red_sell[osym] = red_sell.get(osym, 0.0) + w_red
                else:
                    red_buy[osym] = red_buy.get(osym, 0.0) + w_red
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
                    red_sell_floor[osym] = fl if osym not in red_sell_floor else min(red_sell_floor[osym], fl)
                else:
                    if eff_enforce_inst_lev is True:
                        eff_tw = max(raw_tw, -inst_ml) if inst_ml is not None else 0.0
                        cl = min(0.0, eff_tw) * prot_tw_ratio
                    elif eff_enforce_inst_lev is False:
                        cl = min(0.0, raw_tw) * prot_tw_ratio
                    else:
                        cl = 0.0
                    red_buy_ceil[osym] = cl if osym not in red_buy_ceil else max(red_buy_ceil[osym], cl)

        same_output_reduce_ids = {
            it.intent_id
            for it in output.intents
            if it.reduce_only or it.qty_mode == "close_all"
        }

        # Step 1a: Record all Step 1.3 protective orders BEFORE computing target conversion ratios.
        for open_ord in ctx.portfolio.open_orders:
            if open_ord.reduce_only or open_ord.qty_mode == "close_all":
                if open_ord.intent_id not in same_output_reduce_ids:
                    _record_protective_reduction(open_ord)

        for it_pre in output.intents:
            if it_pre.reduce_only or it_pre.qty_mode == "close_all":
                _record_protective_reduction(it_pre)

        target_upper_ratio, target_lower_ratio = _settled_ratios_for_bounds(_prot_only_lo, _prot_only_hi)

        # Step 1b: Record target bracket protective exits (Step 1.5, after Step 1.4 targets).
        for t_pre in output.targets:
            if t_pre.stop_price is not None or t_pre.take_profit is not None:
                if t_pre.target_weight > 0.0:
                    red_sell[t_pre.symbol] = math.inf
                elif t_pre.target_weight < 0.0:
                    red_buy[t_pre.symbol] = math.inf

        rest_upper_ratio, rest_lower_ratio = _settled_ratios_for_bounds(_prot_only_lo, _prot_only_hi)

        # Step 1c: Summarize non-reduce-only resting open orders in marked-equity units.
        for open_ord in ctx.portfolio.open_orders:
            if open_ord.reduce_only or open_ord.qty_mode == "close_all":
                continue
            osym = open_ord.symbol
            all_known_syms.add(osym)
            if open_ord.qty_mode == "target_weight":
                w = open_ord.qty_value
                inst_ml_r = _inst_max_leverage(osym)
                if eff_enforce_inst_lev is True and inst_ml_r is not None:
                    r_vals: tuple[float, ...] = (max(-inst_ml_r, min(inst_ml_r, w)),)
                elif eff_enforce_inst_lev is False or inst_ml_r is None:
                    r_vals = (w,)
                else:
                    r_cl = max(-inst_ml_r, min(inst_ml_r, w))
                    r_vals = (w, r_cl) if r_cl != w else (w,)
                r_cands = [v * r for v in r_vals for r in (rest_lower_ratio, rest_upper_ratio)]
                r_lo = min(r_cands)
                r_hi = max(r_cands)
                rest_tw_lo[osym] = r_lo if osym not in rest_tw_lo else min(rest_tw_lo[osym], r_lo)
                rest_tw_hi[osym] = r_hi if osym not in rest_tw_hi else max(rest_tw_hi[osym], r_hi)
            elif open_ord.qty_mode == "equity_fraction":
                w_ef = open_ord.qty_value * rest_upper_ratio
                if open_ord.side == "buy":
                    rest_buy[osym] = rest_buy.get(osym, 0.0) + w_ef
                else:
                    rest_sell[osym] = rest_sell.get(osym, 0.0) + w_ef
            elif open_ord.qty_mode == "contracts":
                ref_px = open_ord.limit_price or open_ord.stop_price or self._reference_price(ctx, osym)
                if equity > 0.0 and ref_px is not None and ref_px > 0.0:
                    mult = _contract_multiplier(ctx, osym)
                    w = (open_ord.qty_value * mult * ref_px) / equity
                    if open_ord.side == "buy":
                        rest_buy[osym] = rest_buy.get(osym, 0.0) + w
                    else:
                        rest_sell[osym] = rest_sell.get(osym, 0.0) + w

        def _post_target_envelope(
            sym: str,
            w_lo: float | None,
            w_hi: float | None = None,
        ) -> tuple[float, float]:
            ew = existing_weights.get(sym, 0.0)
            b_lo = ew
            b_hi = ew
            if w_lo is not None:
                hi_val = w_lo if w_hi is None else w_hi
                if w_lo < b_lo:
                    b_lo = w_lo
                if hi_val > b_hi:
                    b_hi = hi_val
            if sym in rest_tw_lo and rest_tw_lo[sym] < b_lo:
                b_lo = rest_tw_lo[sym]
            if sym in rest_tw_hi and rest_tw_hi[sym] > b_hi:
                b_hi = rest_tw_hi[sym]
            b_lo, b_hi = _apply_protective_reductions(sym, b_lo, b_hi)
            return b_lo - rest_sell.get(sym, 0.0), b_hi + rest_buy.get(sym, 0.0)

        # 2. Clamp individual TargetPositions in marked-equity units against single_cap AND resting orders
        #    on that symbol, then convert capped results back to the simulator's sizing basis (CR2-29 / CR7-10).
        clamped_targets: list[TargetPosition] = []
        target_marked: list[float] = []
        targeted_symbols: set[str] = set()
        for t in output.targets:
            tsym = t.symbol
            rb = rest_buy.get(tsym, 0.0)
            rs = rest_sell.get(tsym, 0.0)
            w_raw = t.target_weight
            w_m = w_raw * target_upper_ratio
            clamped_s = False
            if w_m > 0.0:
                lim_pos = max(0.0, single_cap - rb)
                if w_m > lim_pos:
                    w_m = lim_pos
                    clamped_s = True
            elif w_m < 0.0:
                lim_neg = -max(0.0, single_cap - rs)
                if w_m < lim_neg:
                    w_m = lim_neg
                    clamped_s = True
            w_out = ((w_m / target_upper_ratio) if target_upper_ratio > 0.0 else 0.0) if clamped_s else w_raw
            if w_out == 0.0:
                w_out = 0.0
            if w_m == 0.0:
                w_m = 0.0
            clamped_targets.append(replace(t, target_weight=w_out))
            target_marked.append(w_m)
            targeted_symbols.add(tsym)
            all_known_syms.add(tsym)

        # 3. Enforce max_gross_leverage across all untouched symbols (incl. resting orders) + targeted symbols
        #    in marked-equity units, aggregating the min/max envelope across EVERY surviving target per symbol (CR4-01 / CR7-10).
        if clamped_targets:
            untouched_gross = 0.0
            for usym in all_known_syms:
                if usym not in targeted_symbols:
                    u_lo, u_hi = _post_target_envelope(usym, None)
                    untouched_gross += max(abs(u_lo), abs(u_hi))
            allowed_targeted_gross = max(0.0, self.max_gross_leverage - untouched_gross)
            sym_t_lo: dict[str, float] = {}
            sym_t_hi: dict[str, float] = {}
            for t, tw_m in zip(clamped_targets, target_marked):
                tsym = t.symbol
                sym_t_lo[tsym] = tw_m if tsym not in sym_t_lo else min(sym_t_lo[tsym], tw_m)
                sym_t_hi[tsym] = tw_m if tsym not in sym_t_hi else max(sym_t_hi[tsym], tw_m)
            sym_worst: dict[str, float] = {}
            for tsym in targeted_symbols:
                e_lo, e_hi = _post_target_envelope(tsym, sym_t_lo[tsym], sym_t_hi[tsym])
                sym_worst[tsym] = max(abs(e_lo), abs(e_hi))
            total_target_worst = math.fsum(sym_worst.values())
            if total_target_worst > allowed_targeted_gross + 1e-12:
                # Reserve the unavoidable floor gross from existing positions + resting orders on targeted symbols,
                # then proportionally scale the incremental target weights within remaining headroom.
                base_floors: dict[str, float] = {}
                inc_contribs: dict[str, float] = {}
                for tsym in targeted_symbols:
                    f_lo, f_hi = _post_target_envelope(tsym, 0.0, 0.0)
                    fl = max(abs(f_lo), abs(f_hi))
                    base_floors[tsym] = fl
                    inc_contribs[tsym] = max(0.0, sym_worst[tsym] - fl)
                inc_sum = math.fsum(inc_contribs.values())
                rem_for_inc = max(0.0, allowed_targeted_gross - math.fsum(base_floors.values()))
                if inc_sum > 1e-12:
                    ratio = min(1.0, rem_for_inc / inc_sum)
                    new_targets: list[TargetPosition] = []
                    new_marked: list[float] = []
                    for t, w_cur_m in zip(clamped_targets, target_marked):
                        tsym = t.symbol
                        if inc_contribs.get(tsym, 0.0) > 0.0:
                            # CR5-04: Preserve each symbol's baseline (existing position / floor) and
                            # scale ONLY the requested increase above that baseline; flat books scale from 0.
                            fl_sym = base_floors[tsym]
                            if w_cur_m > 0.0:
                                base_pos = max(0.0, fl_sym - rest_buy.get(tsym, 0.0))
                                w_scaled_m = (base_pos + (w_cur_m - base_pos) * ratio) if w_cur_m > base_pos else w_cur_m
                            elif w_cur_m < 0.0:
                                base_neg = max(0.0, fl_sym - rest_sell.get(tsym, 0.0))
                                w_scaled_m = -(base_neg + (-w_cur_m - base_neg) * ratio) if (-w_cur_m) > base_neg else w_cur_m
                            else:
                                w_scaled_m = 0.0
                            if w_scaled_m == 0.0:
                                w_scaled_m = 0.0
                            if w_scaled_m != w_cur_m:
                                w_scaled_out = (w_scaled_m / target_upper_ratio) if target_upper_ratio > 0.0 else 0.0
                                if w_scaled_out == 0.0:
                                    w_scaled_out = 0.0
                                new_targets.append(replace(t, target_weight=w_scaled_out))
                            else:
                                new_targets.append(t)
                            new_marked.append(w_scaled_m)
                        else:
                            new_targets.append(t)
                            new_marked.append(w_cur_m)
                    clamped_targets = new_targets
                    target_marked = new_marked
                elif allowed_targeted_gross <= 1e-12:
                    clamped_targets = [replace(t, target_weight=0.0) for t in clamped_targets]
                    target_marked = [0.0] * len(clamped_targets)

        # 4. Seed conservative reachable weight state per symbol (in marked-equity units), separating
        #    absolute target bounds [base_lo, base_hi] from pending incremental additions (add_buy, add_sell).
        #    Reachable exposure is [base_lo - add_sell, base_hi + add_buy].
        #    This guarantees:
        #      - CR7-05 / CR7-06 / CR7-10: unified marked-equity accounting with dynamic settled_equity profit updates.
        #      - CR5-05: protective reductions adjust inner bound toward 0.0 for opposing orders.
        #      - CR4-01: reserve min/max across every surviving target per symbol (so on_change cannot bypass cap).
        #      - CR3-22: pending buy/sell increments are reserved AFTER any new target_weight fill.
        #      - CR2-29: resting orders reserved before targets and carried into intent budget.
        #      - CR1-32: single shared exposure budget across targets + intents in the same output.
        #      - CR1-31: reducing orders never release exposure headroom before filling.
        #      - CR1-29: contract_value multiplier applied symmetrically to contracts orders.
        #      - CR1-33: unpriceable contracts orders fail closed (blocked) rather than bypassing cap.
        base_lo: dict[str, float] = {}
        base_hi: dict[str, float] = {}
        add_buy: dict[str, float] = {}
        add_sell: dict[str, float] = {}
        tracked_syms: set[str] = set(all_known_syms)
        target_lo_by_sym: dict[str, float] = {}
        target_hi_by_sym: dict[str, float] = {}
        for t in clamped_targets:
            tsym = t.symbol
            tw = t.target_weight
            inst_ml_t = _inst_max_leverage(tsym)
            if eff_enforce_inst_lev is True and inst_ml_t is not None:
                t_vals: tuple[float, ...] = (max(-inst_ml_t, min(inst_ml_t, tw)),)
            elif eff_enforce_inst_lev is False or inst_ml_t is None:
                t_vals = (tw,)
            else:
                t_cl = max(-inst_ml_t, min(inst_ml_t, tw))
                t_vals = (tw, t_cl) if t_cl != tw else (tw,)
            t_cands = [v * r for v in t_vals for r in (target_lower_ratio, target_upper_ratio)]
            t_lo_m = min(t_cands)
            t_hi_m = max(t_cands)
            target_lo_by_sym[tsym] = t_lo_m if tsym not in target_lo_by_sym else min(target_lo_by_sym[tsym], t_lo_m)
            target_hi_by_sym[tsym] = t_hi_m if tsym not in target_hi_by_sym else max(target_hi_by_sym[tsym], t_hi_m)
        for sym in all_known_syms:
            ew = existing_weights.get(sym, 0.0)
            b_lo = ew
            b_hi = ew
            if sym in target_lo_by_sym and target_lo_by_sym[sym] < b_lo:
                b_lo = target_lo_by_sym[sym]
            if sym in target_hi_by_sym and target_hi_by_sym[sym] > b_hi:
                b_hi = target_hi_by_sym[sym]
            if sym in rest_tw_lo and rest_tw_lo[sym] < b_lo:
                b_lo = rest_tw_lo[sym]
            if sym in rest_tw_hi and rest_tw_hi[sym] > b_hi:
                b_hi = rest_tw_hi[sym]
            b_lo, b_hi = _apply_protective_reductions(sym, b_lo, b_hi)
            base_lo[sym] = b_lo
            base_hi[sym] = b_hi
            add_buy[sym] = rest_buy.get(sym, 0.0)
            add_sell[sym] = rest_sell.get(sym, 0.0)

        def _sym_worst_abs(sym: str) -> float:
            lo_s = base_lo.get(sym, 0.0) - add_sell.get(sym, 0.0)
            hi_s = base_hi.get(sym, 0.0) + add_buy.get(sym, 0.0)
            a_lo = -lo_s if lo_s < 0.0 else lo_s
            a_hi = -hi_s if hi_s < 0.0 else hi_s
            return a_hi if a_hi >= a_lo else a_lo

        def _cur_intent_ratios() -> tuple[float, float]:
            return _settled_ratios_for_bounds(
                lambda s: base_lo.get(s, 0.0) - add_sell.get(s, 0.0),
                lambda s: base_hi.get(s, 0.0) + add_buy.get(s, 0.0),
            )

        clamped_intents: list[OrderIntent] = []

        for intent in output.intents:
            if intent.reduce_only or intent.qty_mode == "close_all":
                clamped_intents.append(intent)
                continue

            sym = intent.symbol
            b_lo = base_lo.get(sym, 0.0)
            b_hi = base_hi.get(sym, 0.0)
            ab = add_buy.get(sym, 0.0)
            as_ = add_sell.get(sym, 0.0)
            lo = b_lo - as_
            hi = b_hi + ab
            other_gross = math.fsum(_sym_worst_abs(s) for s in tracked_syms if s != sym)
            max_abs_sym = min(single_cap, max(0.0, self.max_gross_leverage - other_gross))

            if intent.qty_mode == "target_weight":
                cur_upper_ratio, cur_lower_ratio = _cur_intent_ratios()
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
                clamped_intents.append(replace(intent, qty_value=desired))
                tracked_syms.add(sym)
                # CR7-05: Convert the effective target (after optional instrument leverage clamp) into
                # marked-equity units before updating the reachable risk envelope.
                inst_ml_c = _inst_max_leverage(sym)
                if eff_enforce_inst_lev is True and inst_ml_c is not None:
                    eff_vals_c: tuple[float, ...] = (max(-inst_ml_c, min(inst_ml_c, desired)),)
                elif eff_enforce_inst_lev is False or inst_ml_c is None:
                    eff_vals_c = (desired,)
                else:
                    eff_cl_c = max(-inst_ml_c, min(inst_ml_c, desired))
                    eff_vals_c = (desired, eff_cl_c) if eff_cl_c != desired else (desired,)
                cand_m_c = [v * r for v in eff_vals_c for r in (cur_lower_ratio, cur_upper_ratio)]
                d_lo, d_hi = _apply_protective_reductions(sym, min(cand_m_c), max(cand_m_c))
                base_lo[sym] = d_lo if d_lo < b_lo else b_lo
                base_hi[sym] = d_hi if d_hi > b_hi else b_hi
                add_buy.setdefault(sym, ab)
                add_sell.setdefault(sym, as_)
                continue

            if intent.qty_mode == "equity_fraction":
                # CR7-06: Size against the current upper settled-equity ratio (reflecting any preceding
                # profit-realizing reductions in the same bar) and honor known instrument leverage caps.
                cur_ef_ratio, _ = _cur_intent_ratios()
                eff_ef = intent.qty_value
                if eff_enforce_inst_lev is True:
                    inst_ml_e = _inst_max_leverage(sym)
                    if inst_ml_e is not None and eff_ef > inst_ml_e:
                        eff_ef = inst_ml_e
                w = eff_ef * cur_ef_ratio
                if intent.side == "buy":
                    if hi + w <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_buy[sym] = ab + w
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_sell.setdefault(sym, as_)
                    else:
                        allowed_w = max_abs_sym - hi
                        if allowed_w > 1e-12 and cur_ef_ratio > 0.0:
                            clamped_intents.append(replace(intent, qty_value=allowed_w / cur_ef_ratio))
                            tracked_syms.add(sym)
                            add_buy[sym] = ab + allowed_w
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_sell.setdefault(sym, as_)
                else:
                    if -(lo - w) <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_sell[sym] = as_ + w
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_buy.setdefault(sym, ab)
                    else:
                        allowed_w = max_abs_sym + lo
                        if allowed_w > 1e-12 and cur_ef_ratio > 0.0:
                            clamped_intents.append(replace(intent, qty_value=allowed_w / cur_ef_ratio))
                            tracked_syms.add(sym)
                            add_sell[sym] = as_ + allowed_w
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_buy.setdefault(sym, ab)
                continue

            if intent.qty_mode == "contracts":
                ref_px = intent.limit_price or intent.stop_price or self._reference_price(ctx, sym)
                if equity <= 0.0 or ref_px is None or ref_px <= 0.0:
                    # Fail closed (CR1-33): do not pass an unpriceable contracts order through the decision-time cap.
                    continue
                mult = _contract_multiplier(ctx, sym)
                order_weight = (intent.qty_value * mult * ref_px) / equity
                if intent.side == "buy":
                    if hi + order_weight <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_buy[sym] = ab + order_weight
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_sell.setdefault(sym, as_)
                    else:
                        allowed_weight = max_abs_sym - hi
                        allowed_qty = (allowed_weight * equity) / (mult * ref_px)
                        if allowed_qty > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_qty))
                            tracked_syms.add(sym)
                            add_buy[sym] = ab + allowed_weight
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_sell.setdefault(sym, as_)
                else:
                    if -(lo - order_weight) <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_sell[sym] = as_ + order_weight
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_buy.setdefault(sym, ab)
                    else:
                        allowed_weight = max_abs_sym + lo
                        allowed_qty = (allowed_weight * equity) / (mult * ref_px)
                        if allowed_qty > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_qty))
                            tracked_syms.add(sym)
                            add_sell[sym] = as_ + allowed_weight
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_buy.setdefault(sym, ab)

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
