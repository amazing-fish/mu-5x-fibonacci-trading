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
        rest_ef_buy: dict[str, float] = {}
        rest_ef_sell: dict[str, float] = {}
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
                # CR8-05: Preserve instrument max_leverage enforcement on resting equity_fraction orders
                # while sizing them dynamically against post-target/post-protective settled equity!
                raw_ef_r = open_ord.qty_value
                if eff_enforce_inst_lev is True:
                    inst_ml_r = _inst_max_leverage(osym)
                    if inst_ml_r is not None and raw_ef_r > inst_ml_r:
                        raw_ef_r = inst_ml_r
                if open_ord.side == "buy":
                    rest_ef_buy[osym] = rest_ef_buy.get(osym, 0.0) + raw_ef_r
                else:
                    rest_ef_sell[osym] = rest_ef_sell.get(osym, 0.0) + raw_ef_r
            elif open_ord.qty_mode == "contracts":
                ref_px = open_ord.limit_price or open_ord.stop_price or self._reference_price(ctx, osym)
                if equity > 0.0 and ref_px is not None and ref_px > 0.0:
                    mult = _contract_multiplier(ctx, osym)
                    w = (open_ord.qty_value * mult * ref_px) / equity
                    if open_ord.side == "buy":
                        rest_buy_fixed[osym] = rest_buy_fixed.get(osym, 0.0) + w
                    else:
                        rest_sell_fixed[osym] = rest_sell_fixed.get(osym, 0.0) + w

        def _eval_phase_15_16(
            sym_t_lo: Mapping[str, float],
            sym_t_hi: Mapping[str, float],
        ) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float], float, float]:
            """Evaluate post-target Phase 1.5 protective reductions and Phase 1.6 resting orders (CR8-04 / CR8-05)."""
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

            if rest_tw_orders:
                for osym, w_tw in rest_tw_orders:
                    inst_ml_r = _inst_max_leverage(osym)
                    if eff_enforce_inst_lev is True and inst_ml_r is not None:
                        r_vals: tuple[float, ...] = (max(-inst_ml_r, min(inst_ml_r, w_tw)),)
                    elif eff_enforce_inst_lev is False or inst_ml_r is None:
                        r_vals = (w_tw,)
                    else:
                        r_cl = max(-inst_ml_r, min(inst_ml_r, w_tw))
                        r_vals = (w_tw, r_cl) if r_cl != w_tw else (w_tw,)
                    r_cands = [v * r for v in r_vals for r in (r_lo_16, r_up_16)]
                    tw_lo_m, tw_hi_m = min(r_cands), max(r_cands)
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
                r_up_16, r_lo_16 = _settled_ratios_for_bounds(
                    lambda s: lo_15.get(s, 0.0) - rest_sell_fixed.get(s, 0.0),
                    lambda s: hi_15.get(s, 0.0) + rest_buy_fixed.get(s, 0.0),
                )

            rb_eff = {
                s: rest_buy_fixed.get(s, 0.0) + rest_ef_buy.get(s, 0.0) * r_up_16
                for s in all_known_syms
            }
            rs_eff = {
                s: rest_sell_fixed.get(s, 0.0) + rest_ef_sell.get(s, 0.0) * r_up_16
                for s in all_known_syms
            }
            return lo_15, hi_15, rb_eff, rs_eff, r_up_16, r_lo_16

        # 2. Clamp individual TargetPositions in marked-equity units against single_cap AND resting orders
        #    (accounting for post-target settled equity growth on resting equity_fraction orders, CR2-29 / CR7-10 / CR8-05),
        #    then convert capped results back to the simulator's sizing basis.
        clamped_targets: list[TargetPosition] = []
        target_marked: list[float] = []
        targeted_symbols: set[str] = set()
        for t in output.targets:
            tsym = t.symbol
            w_raw = t.target_weight
            w_m = w_raw * target_upper_ratio
            ew = existing_weights.get(tsym, 0.0)
            clamped_s = False

            def _single_env(x_m: float, _sym: str = tsym) -> tuple[float, float]:
                l15, h15, rbe, rse, _, _ = _eval_phase_15_16({_sym: x_m}, {_sym: x_m})
                return l15.get(_sym, 0.0) - rse.get(_sym, 0.0), h15.get(_sym, 0.0) + rbe.get(_sym, 0.0)

            e_lo, e_hi = _single_env(w_m)
            # Same-sign clamping toward 0.0 (standard cap against single_cap + resting orders).
            if w_m > 0.0 and e_hi > single_cap + 1e-12:
                if _single_env(0.0)[1] <= single_cap + 1e-12:
                    lo_b, hi_b = 0.0, w_m
                    for _ in range(52):
                        mid = 0.5 * (lo_b + hi_b)
                        if _single_env(mid)[1] <= single_cap:
                            lo_b = mid
                        else:
                            hi_b = mid
                    w_m = lo_b
                else:
                    w_m = 0.0
                clamped_s = True
            elif w_m < 0.0 and -e_lo > single_cap + 1e-12:
                if -_single_env(0.0)[0] <= single_cap + 1e-12:
                    lo_b, hi_b = w_m, 0.0
                    for _ in range(52):
                        mid = 0.5 * (lo_b + hi_b)
                        if -_single_env(mid)[0] <= single_cap:
                            hi_b = mid
                        else:
                            lo_b = mid
                    w_m = hi_b
                else:
                    w_m = 0.0
                clamped_s = True

            # Opposing-side check (CR8-05): reducing a profitable position can raise settled_equity and
            # enlarge a resting opposing equity_fraction order past single_cap; pull w_m back toward ew.
            e_lo, e_hi = _single_env(w_m)
            if ew > 0.0 and w_m < ew and -e_lo > single_cap + 1e-12:
                cap_hi = min(ew, single_cap) if _single_env(ew)[1] > single_cap + 1e-12 else ew
                if cap_hi > w_m:
                    if -_single_env(cap_hi)[0] <= single_cap + 1e-12:
                        lo_b, hi_b = w_m, cap_hi
                        for _ in range(52):
                            mid = 0.5 * (lo_b + hi_b)
                            if -_single_env(mid)[0] <= single_cap:
                                hi_b = mid
                            else:
                                lo_b = mid
                        w_m = hi_b
                    else:
                        w_m = cap_hi
                    clamped_s = True
            elif ew < 0.0 and w_m > ew and e_hi > single_cap + 1e-12:
                cap_lo = max(ew, -single_cap) if -_single_env(ew)[0] > single_cap + 1e-12 else ew
                if cap_lo < w_m:
                    if _single_env(cap_lo)[1] <= single_cap + 1e-12:
                        lo_b, hi_b = cap_lo, w_m
                        for _ in range(52):
                            mid = 0.5 * (lo_b + hi_b)
                            if _single_env(mid)[1] <= single_cap:
                                lo_b = mid
                            else:
                                hi_b = mid
                        w_m = lo_b
                    else:
                        w_m = cap_lo
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
        #    in marked-equity units, aggregating the min/max envelope across EVERY surviving target per symbol
        #    with post-target settled-equity updates (CR4-01 / CR7-10 / CR8-04 / CR8-05).
        if clamped_targets:
            sym_t_lo: dict[str, float] = {}
            sym_t_hi: dict[str, float] = {}
            for t, tw_m in zip(clamped_targets, target_marked):
                tsym = t.symbol
                sym_t_lo[tsym] = tw_m if tsym not in sym_t_lo else min(sym_t_lo[tsym], tw_m)
                sym_t_hi[tsym] = tw_m if tsym not in sym_t_hi else max(sym_t_hi[tsym], tw_m)
            lo_all, hi_all, rb_all, rs_all, _, _ = _eval_phase_15_16(sym_t_lo, sym_t_hi)
            untouched_gross = 0.0
            for usym in all_known_syms:
                if usym not in targeted_symbols:
                    u_lo = lo_all.get(usym, 0.0) - rs_all.get(usym, 0.0)
                    u_hi = hi_all.get(usym, 0.0) + rb_all.get(usym, 0.0)
                    untouched_gross += max(abs(u_lo), abs(u_hi))
            allowed_targeted_gross = max(0.0, self.max_gross_leverage - untouched_gross)
            sym_worst: dict[str, float] = {}
            for tsym in targeted_symbols:
                e_lo = lo_all.get(tsym, 0.0) - rs_all.get(tsym, 0.0)
                e_hi = hi_all.get(tsym, 0.0) + rb_all.get(tsym, 0.0)
                sym_worst[tsym] = max(abs(e_lo), abs(e_hi))
            total_target_worst = math.fsum(sym_worst.values())
            if total_target_worst > allowed_targeted_gross + 1e-12:
                # Reserve the unavoidable floor gross from existing positions + resting orders on targeted symbols,
                # then proportionally scale the incremental target weights within remaining headroom.
                zero_map = {tsym: 0.0 for tsym in targeted_symbols}
                lo_z, hi_z, rb_z, rs_z, _, _ = _eval_phase_15_16(zero_map, zero_map)
                lo_ew, hi_ew, rb_ew, rs_ew, _, _ = _eval_phase_15_16({}, {})
                base_floors: dict[str, float] = {}
                inc_contribs: dict[str, float] = {}
                for tsym in targeted_symbols:
                    fl_z = max(abs(lo_z.get(tsym, 0.0) - rs_z.get(tsym, 0.0)), abs(hi_z.get(tsym, 0.0) + rb_z.get(tsym, 0.0)))
                    fl_e = max(abs(lo_ew.get(tsym, 0.0) - rs_ew.get(tsym, 0.0)), abs(hi_ew.get(tsym, 0.0) + rb_ew.get(tsym, 0.0)))
                    fl = fl_z if fl_z <= fl_e else fl_e
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
                                base_pos = max(0.0, fl_sym - rb_all.get(tsym, 0.0))
                                w_scaled_m = (base_pos + (w_cur_m - base_pos) * ratio) if w_cur_m > base_pos else w_cur_m
                            elif w_cur_m < 0.0:
                                base_neg = max(0.0, fl_sym - rs_all.get(tsym, 0.0))
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

        # 4. Seed conservative reachable weight state per symbol (in marked-equity units) from the exact
        #    post-target Phase 1.5/1.6 evaluator, separating absolute target bounds [base_lo, base_hi]
        #    from pending incremental additions (fixed + dynamic resting equity_fraction orders).
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

        base_lo, base_hi, _, _, init_r_up, _ = _eval_phase_15_16(target_lo_by_sym, target_hi_by_sym)
        add_buy_fixed: dict[str, float] = {sym: rest_buy_fixed.get(sym, 0.0) for sym in all_known_syms}
        add_sell_fixed: dict[str, float] = {sym: rest_sell_fixed.get(sym, 0.0) for sym in all_known_syms}
        has_dyn_rest_ef = bool(rest_ef_buy or rest_ef_sell)

        def _cur_intent_ratios(
            extra_lo: tuple[str, float] | None = None,
            extra_hi: tuple[str, float] | None = None,
            extra_sell: tuple[str, float] | None = None,
            extra_buy: tuple[str, float] | None = None,
        ) -> tuple[float, float]:
            def _lo_s(s: str) -> float:
                bl = base_lo.get(s, 0.0)
                if extra_lo is not None and s == extra_lo[0] and extra_lo[1] < bl:
                    bl = extra_lo[1]
                asf = add_sell_fixed.get(s, 0.0) + (extra_sell[1] if (extra_sell is not None and s == extra_sell[0]) else 0.0)
                return bl - asf

            def _hi_s(s: str) -> float:
                bh = base_hi.get(s, 0.0)
                if extra_hi is not None and s == extra_hi[0] and extra_hi[1] > bh:
                    bh = extra_hi[1]
                abf = add_buy_fixed.get(s, 0.0) + (extra_buy[1] if (extra_buy is not None and s == extra_buy[0]) else 0.0)
                return bh + abf

            return _settled_ratios_for_bounds(_lo_s, _hi_s)

        def _add_buy_total(sym: str, r_up: float) -> float:
            return add_buy_fixed.get(sym, 0.0) + rest_ef_buy.get(sym, 0.0) * r_up

        def _add_sell_total(sym: str, r_up: float) -> float:
            return add_sell_fixed.get(sym, 0.0) + rest_ef_sell.get(sym, 0.0) * r_up

        def _apply_all_protective_to_target(sym: str, lo_val: float, hi_val: float, r_up: float) -> tuple[float, float]:
            lo_val, hi_val = _apply_red_maps(
                sym, lo_val, hi_val, mkt_red_sell, mkt_red_buy, mkt_red_sell_floor, mkt_red_buy_ceil
            )
            lo_val, hi_val = _apply_red_maps(
                sym, lo_val, hi_val, post_red_sell_fixed, post_red_buy_fixed, post_red_sell_floor, post_red_buy_ceil
            )
            for psym, pside, raw_ef in post_prot_ef_orders:
                if psym == sym:
                    w_red = raw_ef * r_up
                    if pside == "sell" and lo_val > 0.0:
                        lo_val = max(0.0, lo_val - w_red)
                    elif pside == "buy" and hi_val < 0.0:
                        hi_val = min(0.0, hi_val + w_red)
            return lo_val, hi_val

        def _sym_worst_abs(sym: str, r_up: float) -> float:
            lo_s = base_lo.get(sym, 0.0) - _add_sell_total(sym, r_up)
            hi_s = base_hi.get(sym, 0.0) + _add_buy_total(sym, r_up)
            a_lo = -lo_s if lo_s < 0.0 else lo_s
            a_hi = -hi_s if hi_s < 0.0 else hi_s
            return a_hi if a_hi >= a_lo else a_lo

        clamped_intents: list[OrderIntent] = []

        for intent in output.intents:
            if intent.reduce_only or intent.qty_mode == "close_all":
                clamped_intents.append(intent)
                continue

            sym = intent.symbol
            cur_upper_ratio, cur_lower_ratio = _cur_intent_ratios()
            b_lo = base_lo.get(sym, 0.0)
            b_hi = base_hi.get(sym, 0.0)
            ab = _add_buy_total(sym, cur_upper_ratio)
            as_ = _add_sell_total(sym, cur_upper_ratio)
            lo = b_lo - as_
            hi = b_hi + ab
            other_gross = math.fsum(_sym_worst_abs(s, cur_upper_ratio) for s in tracked_syms if s != sym)
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
                # CR8-05: When resting equity_fraction orders exist, ensure a profit-realizing
                # target_weight reduction cannot enlarge a resting opposing order past max_abs_sym.
                if has_dyn_rest_ef and cur_upper_ratio > 0.0:
                    ew_s = existing_weights.get(sym, 0.0)
                    inst_ml_chk = _inst_max_leverage(sym)

                    def _tw_post_env(cand_w: float) -> tuple[float, float]:
                        if eff_enforce_inst_lev is True and inst_ml_chk is not None:
                            ev_t: tuple[float, ...] = (max(-inst_ml_chk, min(inst_ml_chk, cand_w)),)
                        elif eff_enforce_inst_lev is False or inst_ml_chk is None:
                            ev_t = (cand_w,)
                        else:
                            ec_t = max(-inst_ml_chk, min(inst_ml_chk, cand_w))
                            ev_t = (cand_w, ec_t) if ec_t != cand_w else (cand_w,)
                        cm_t = [v * r for v in ev_t for r in (cur_lower_ratio, cur_upper_ratio)]
                        lp, hp = min(cm_t), max(cm_t)
                        ru_t, _ = _cur_intent_ratios(extra_lo=(sym, lp), extra_hi=(sym, hp))
                        la, ha = _apply_all_protective_to_target(sym, lp, hp, ru_t)
                        bl_t = la if la < b_lo else b_lo
                        bh_t = ha if ha > b_hi else b_hi
                        return bl_t - _add_sell_total(sym, ru_t), bh_t + _add_buy_total(sym, ru_t)

                    tw_lo_chk, tw_hi_chk = _tw_post_env(desired)
                    if intent.side == "sell" and ew_s > 0.0 and -tw_lo_chk > max_abs_sym + 1e-12:
                        hi_w = ew_s / cur_upper_ratio
                        if hi_w > desired:
                            if -_tw_post_env(hi_w)[0] <= max_abs_sym + 1e-12:
                                lo_b, hi_b = desired, hi_w
                                for _ in range(48):
                                    mid = 0.5 * (lo_b + hi_b)
                                    if -_tw_post_env(mid)[0] <= max_abs_sym:
                                        hi_b = mid
                                    else:
                                        lo_b = mid
                                desired = hi_b
                            else:
                                desired = hi_w
                    elif intent.side == "buy" and ew_s < 0.0 and tw_hi_chk > max_abs_sym + 1e-12:
                        lo_w = ew_s / cur_upper_ratio
                        if lo_w < desired:
                            if _tw_post_env(lo_w)[1] <= max_abs_sym + 1e-12:
                                lo_b, hi_b = lo_w, desired
                                for _ in range(48):
                                    mid = 0.5 * (lo_b + hi_b)
                                    if _tw_post_env(mid)[1] <= max_abs_sym:
                                        lo_b = mid
                                    else:
                                        hi_b = mid
                                desired = lo_b
                            else:
                                desired = lo_w
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
                d_lo_pre, d_hi_pre = min(cand_m_c), max(cand_m_c)
                post_r_up, _ = _cur_intent_ratios(extra_lo=(sym, d_lo_pre), extra_hi=(sym, d_hi_pre))
                d_lo, d_hi = _apply_all_protective_to_target(sym, d_lo_pre, d_hi_pre, post_r_up)
                base_lo[sym] = d_lo if d_lo < b_lo else b_lo
                base_hi[sym] = d_hi if d_hi > b_hi else b_hi
                add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                continue

            if intent.qty_mode == "equity_fraction":
                # CR7-06: Size against the current upper settled-equity ratio (reflecting any preceding
                # profit-realizing reductions in the same bar) and honor known instrument leverage caps.
                cur_ef_ratio = cur_upper_ratio
                eff_ef = intent.qty_value
                if eff_enforce_inst_lev is True:
                    inst_ml_e = _inst_max_leverage(sym)
                    if inst_ml_e is not None and eff_ef > inst_ml_e:
                        eff_ef = inst_ml_e
                w = eff_ef * cur_ef_ratio
                if intent.side == "buy":
                    if has_dyn_rest_ef:
                        r_after_b, _ = _cur_intent_ratios(extra_buy=(sym, w))
                        hi_after_b = b_hi + _add_buy_total(sym, r_after_b) + w
                    else:
                        hi_after_b = hi + w
                    if hi_after_b <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_buy_fixed[sym] = add_buy_fixed.get(sym, 0.0) + w
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                    else:
                        allowed_w = max_abs_sym - hi
                        if has_dyn_rest_ef and allowed_w > 1e-12:
                            lo_b, hi_b = 0.0, min(w, allowed_w)
                            for _ in range(48):
                                mid = 0.5 * (lo_b + hi_b)
                                r_m, _ = _cur_intent_ratios(extra_buy=(sym, mid))
                                if b_hi + _add_buy_total(sym, r_m) + mid <= max_abs_sym:
                                    lo_b = mid
                                else:
                                    hi_b = mid
                            allowed_w = lo_b
                        if allowed_w > 1e-12 and cur_ef_ratio > 0.0:
                            clamped_intents.append(replace(intent, qty_value=allowed_w / cur_ef_ratio))
                            tracked_syms.add(sym)
                            add_buy_fixed[sym] = add_buy_fixed.get(sym, 0.0) + allowed_w
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                else:
                    if has_dyn_rest_ef:
                        r_after_s, _ = _cur_intent_ratios(extra_sell=(sym, w))
                        lo_after_s = b_lo - _add_sell_total(sym, r_after_s) - w
                    else:
                        lo_after_s = lo - w
                    if -lo_after_s <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_sell_fixed[sym] = add_sell_fixed.get(sym, 0.0) + w
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                    else:
                        allowed_w = max_abs_sym + lo
                        if has_dyn_rest_ef and allowed_w > 1e-12:
                            lo_b, hi_b = 0.0, min(w, allowed_w)
                            for _ in range(48):
                                mid = 0.5 * (lo_b + hi_b)
                                r_m, _ = _cur_intent_ratios(extra_sell=(sym, mid))
                                if -(b_lo - _add_sell_total(sym, r_m) - mid) <= max_abs_sym:
                                    lo_b = mid
                                else:
                                    hi_b = mid
                            allowed_w = lo_b
                        if allowed_w > 1e-12 and cur_ef_ratio > 0.0:
                            clamped_intents.append(replace(intent, qty_value=allowed_w / cur_ef_ratio))
                            tracked_syms.add(sym)
                            add_sell_fixed[sym] = add_sell_fixed.get(sym, 0.0) + allowed_w
                            base_lo.setdefault(sym, b_lo)
                            base_hi.setdefault(sym, b_hi)
                            add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                continue

            if intent.qty_mode == "contracts":
                ref_px = intent.limit_price or intent.stop_price or self._reference_price(ctx, sym)
                if equity <= 0.0 or ref_px is None or ref_px <= 0.0:
                    # Fail closed (CR1-33): do not pass an unpriceable contracts order through the decision-time cap.
                    continue
                mult = _contract_multiplier(ctx, sym)
                order_weight = (intent.qty_value * mult * ref_px) / equity
                if intent.side == "buy":
                    if has_dyn_rest_ef:
                        r_after_cb, _ = _cur_intent_ratios(extra_buy=(sym, order_weight))
                        hi_after_cb = b_hi + _add_buy_total(sym, r_after_cb) + order_weight
                    else:
                        hi_after_cb = hi + order_weight
                    if hi_after_cb <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_buy_fixed[sym] = add_buy_fixed.get(sym, 0.0) + order_weight
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_sell_fixed.setdefault(sym, rest_sell_fixed.get(sym, 0.0))
                    else:
                        allowed_weight = max_abs_sym - hi
                        if has_dyn_rest_ef and allowed_weight > 1e-12:
                            lo_b, hi_b = 0.0, min(order_weight, allowed_weight)
                            for _ in range(48):
                                mid = 0.5 * (lo_b + hi_b)
                                r_m, _ = _cur_intent_ratios(extra_buy=(sym, mid))
                                if b_hi + _add_buy_total(sym, r_m) + mid <= max_abs_sym:
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
                    if has_dyn_rest_ef:
                        r_after_cs, _ = _cur_intent_ratios(extra_sell=(sym, order_weight))
                        lo_after_cs = b_lo - _add_sell_total(sym, r_after_cs) - order_weight
                    else:
                        lo_after_cs = lo - order_weight
                    if -lo_after_cs <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        tracked_syms.add(sym)
                        add_sell_fixed[sym] = add_sell_fixed.get(sym, 0.0) + order_weight
                        base_lo.setdefault(sym, b_lo)
                        base_hi.setdefault(sym, b_hi)
                        add_buy_fixed.setdefault(sym, rest_buy_fixed.get(sym, 0.0))
                    else:
                        allowed_weight = max_abs_sym + lo
                        if has_dyn_rest_ef and allowed_weight > 1e-12:
                            lo_b, hi_b = 0.0, min(order_weight, allowed_weight)
                            for _ in range(48):
                                mid = 0.5 * (lo_b + hi_b)
                                r_m, _ = _cur_intent_ratios(extra_sell=(sym, mid))
                                if -(b_lo - _add_sell_total(sym, r_m) - mid) <= max_abs_sym:
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
