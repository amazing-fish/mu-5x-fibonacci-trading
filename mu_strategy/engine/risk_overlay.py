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


def _filter_reducing_intents(intents: Sequence[OrderIntent], ctx: BarContext) -> tuple[OrderIntent, ...]:
    """Keep only risk-reducing intents and cap non-reduce-only exits so combined exits cannot reverse past flat."""
    kept: list[OrderIntent] = []
    equity = ctx.portfolio.equity
    rem_base_by_sym: dict[str, float] = {}
    rem_weight_by_sym: dict[str, float] = {}

    for intent in intents:
        if intent.reduce_only or intent.qty_mode == "close_all":
            kept.append(intent if intent.reduce_only else replace(intent, reduce_only=True))
            continue

        sym = intent.symbol
        pos = ctx.portfolio.position(sym)
        pos_qty = pos.qty if pos is not None else 0.0

        if intent.qty_mode == "target_weight":
            if intent.qty_value == 0.0:
                kept.append(intent)
                continue
            if pos is None or pos.is_flat or equity <= 0.0:
                continue
            curr_w = (pos.notional / equity) * (1.0 if pos_qty > 0.0 else -1.0)
            if (curr_w > 0.0 and 0.0 <= intent.qty_value <= curr_w + 1e-12) or (
                curr_w < 0.0 and curr_w - 1e-12 <= intent.qty_value <= 0.0
            ):
                kept.append(intent)
            continue

        if pos is None or pos.is_flat:
            continue
        is_opposing = (pos_qty > 0.0 and intent.side == "sell") or (pos_qty < 0.0 and intent.side == "buy")
        if not is_opposing:
            continue

        if intent.qty_mode == "contracts":
            mult = _contract_multiplier(ctx, sym)
            rem_base = rem_base_by_sym.get(sym, abs(pos_qty))
            if rem_base <= 1e-12:
                continue
            req_base = intent.qty_value * mult
            allow_base = req_base if req_base <= rem_base else rem_base
            rem_base_by_sym[sym] = max(0.0, rem_base - allow_base)
            # Also track weight budget conservatively when equity & price are known.
            if equity > 0.0 and pos.notional > 0.0:
                curr_w_abs = pos.notional / equity
                rem_w = rem_weight_by_sym.get(sym, curr_w_abs)
                frac_used = (allow_base / abs(pos_qty)) * curr_w_abs
                rem_weight_by_sym[sym] = max(0.0, rem_w - frac_used)
            allow_contracts = allow_base / mult
            if allow_contracts > 1e-12:
                kept.append(replace(intent, qty_value=allow_contracts, reduce_only=True))
            continue

        if intent.qty_mode == "equity_fraction":
            if equity <= 0.0:
                continue
            curr_w_abs = pos.notional / equity
            rem_w = rem_weight_by_sym.get(sym, curr_w_abs)
            if rem_w <= 1e-12:
                continue
            allow_w = intent.qty_value if intent.qty_value <= rem_w else rem_w
            rem_weight_by_sym[sym] = max(0.0, rem_w - allow_w)
            if curr_w_abs > 0.0:
                rem_base = rem_base_by_sym.get(sym, abs(pos_qty))
                base_used = (allow_w / curr_w_abs) * abs(pos_qty)
                rem_base_by_sym[sym] = max(0.0, rem_base - base_used)
            if allow_w > 1e-12:
                kept.append(replace(intent, qty_value=allow_w, reduce_only=True))

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

        base = getattr(bars, "_series", None) or getattr(bars, "series", None)
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
            missing_intents: list[OrderIntent] = []
            present_intents: list[OrderIntent] = []
            for intent in output.intents:
                if intent.reduce_only or intent.qty_mode == "close_all":
                    present_intents.append(intent)
                    continue
                status, _ = _get_status(intent.symbol)
                if status == "missing":
                    missing_intents.append(intent)
                else:
                    present_intents.append(intent)
            if missing_intents:
                # Preserve only bounded reductions for symbols with missing volatility series.
                filtered_missing = _filter_reducing_intents(missing_intents, ctx)
                kept_intents = tuple(present_intents) + filtered_missing

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
    """Clamps signed target weights and risk-increasing order sizes to hard exposure limits."""

    def __init__(self, max_gross_leverage: float, max_single_weight: float) -> None:
        self.max_gross_leverage = _require_positive_finite("max_gross_leverage", max_gross_leverage)
        self.max_single_weight = _require_positive_finite("max_single_weight", max_single_weight)

    def reset(self) -> None:
        return None

    def _reference_price(self, ctx: BarContext, symbol: str) -> float | None:
        return _causal_reference_price(ctx, symbol)

    def filter(self, ctx: BarContext, output: StrategyOutput) -> StrategyOutput:
        if not output.targets and not output.intents:
            return output

        single_cap = min(self.max_single_weight, self.max_gross_leverage)
        equity = ctx.portfolio.equity

        # Current signed weights by symbol.
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

        # 3. Maintain conservative reachable weight interval [lo_w, hi_w] per symbol across
        #    existing positions, resting open orders, clamped targets, and emitted intents.
        #    This guarantees:
        #      - CR1-32: single shared exposure budget across targets + intents in the same output.
        #      - CR1-31: reducing orders never release exposure headroom before filling.
        #      - CR1-29: contract_value multiplier applied symmetrically to contracts orders.
        #      - CR1-33: unpriceable contracts orders fail closed (blocked) rather than bypassing cap.
        lo_by_sym: dict[str, float] = dict(existing_weights)
        hi_by_sym: dict[str, float] = dict(existing_weights)

        def _reserve_order_interval(order: OrderIntent) -> None:
            if order.reduce_only or order.qty_mode == "close_all":
                return
            sym = order.symbol
            lo = lo_by_sym.get(sym, 0.0)
            hi = hi_by_sym.get(sym, 0.0)
            if order.qty_mode == "target_weight":
                w = order.qty_value
                lo_by_sym[sym] = w if w < lo else lo
                hi_by_sym[sym] = w if w > hi else hi
                return
            if order.qty_mode == "equity_fraction":
                delta = order.qty_value if order.side == "buy" else -order.qty_value
            elif order.qty_mode == "contracts":
                ref_px = order.limit_price or order.stop_price or self._reference_price(ctx, sym)
                if equity <= 0.0 or ref_px is None or ref_px <= 0.0:
                    return
                mult = _contract_multiplier(ctx, sym)
                w = (order.qty_value * mult * ref_px) / equity
                delta = w if order.side == "buy" else -w
            else:
                return
            if delta > 0.0:
                hi_by_sym[sym] = hi + delta
                lo_by_sym.setdefault(sym, lo)
            elif delta < 0.0:
                lo_by_sym[sym] = lo + delta
                hi_by_sym.setdefault(sym, hi)

        # Reserve already-resting non-reduce-only open orders.
        for open_ord in ctx.portfolio.open_orders:
            _reserve_order_interval(open_ord)

        # Reserve clamped target exposure in the shared budget (never shrinking the envelope
        # before a reducing target is confirmed filled).
        for t in clamped_targets:
            sym = t.symbol
            lo = lo_by_sym.get(sym, 0.0)
            hi = hi_by_sym.get(sym, 0.0)
            w = t.target_weight
            lo_by_sym[sym] = w if w < lo else lo
            hi_by_sym[sym] = w if w > hi else hi

        def _sym_worst_abs(sym: str) -> float:
            lo = lo_by_sym.get(sym, 0.0)
            hi = hi_by_sym.get(sym, 0.0)
            a_lo = -lo if lo < 0.0 else lo
            a_hi = -hi if hi < 0.0 else hi
            return a_hi if a_hi >= a_lo else a_lo

        clamped_intents: list[OrderIntent] = []

        for intent in output.intents:
            if intent.reduce_only or intent.qty_mode == "close_all":
                clamped_intents.append(intent)
                continue

            sym = intent.symbol
            lo = lo_by_sym.get(sym, 0.0)
            hi = hi_by_sym.get(sym, 0.0)
            other_gross = math.fsum(_sym_worst_abs(s) for s in lo_by_sym if s != sym)
            max_abs_sym = min(single_cap, max(0.0, self.max_gross_leverage - other_gross))

            if intent.qty_mode == "target_weight":
                desired = max(-max_abs_sym, min(max_abs_sym, intent.qty_value))
                if desired == 0.0:
                    desired = 0.0
                clamped_intents.append(replace(intent, qty_value=desired))
                lo_by_sym[sym] = desired if desired < lo else lo
                hi_by_sym[sym] = desired if desired > hi else hi
                continue

            if intent.qty_mode == "equity_fraction":
                w = intent.qty_value
                if intent.side == "buy":
                    if hi + w <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        hi_by_sym[sym] = hi + w
                        lo_by_sym.setdefault(sym, lo)
                    else:
                        allowed_w = max_abs_sym - hi
                        if allowed_w > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_w))
                            hi_by_sym[sym] = hi + allowed_w
                            lo_by_sym.setdefault(sym, lo)
                else:
                    if -(lo - w) <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        lo_by_sym[sym] = lo - w
                        hi_by_sym.setdefault(sym, hi)
                    else:
                        allowed_w = max_abs_sym + lo
                        if allowed_w > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_w))
                            lo_by_sym[sym] = lo - allowed_w
                            hi_by_sym.setdefault(sym, hi)
                continue

            if intent.qty_mode == "contracts":
                ref_px = intent.limit_price or intent.stop_price or self._reference_price(ctx, sym)
                if equity <= 0.0 or ref_px is None or ref_px <= 0.0:
                    # Fail closed (CR1-33): do not pass an unpriceable contracts order through a hard cap.
                    continue
                mult = _contract_multiplier(ctx, sym)
                order_weight = (intent.qty_value * mult * ref_px) / equity
                if intent.side == "buy":
                    if hi + order_weight <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        hi_by_sym[sym] = hi + order_weight
                        lo_by_sym.setdefault(sym, lo)
                    else:
                        allowed_weight = max_abs_sym - hi
                        allowed_qty = (allowed_weight * equity) / (mult * ref_px)
                        if allowed_qty > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_qty))
                            hi_by_sym[sym] = hi + allowed_weight
                            lo_by_sym.setdefault(sym, lo)
                else:
                    if -(lo - order_weight) <= max_abs_sym + 1e-12:
                        clamped_intents.append(intent)
                        lo_by_sym[sym] = lo - order_weight
                        hi_by_sym.setdefault(sym, hi)
                    else:
                        allowed_weight = max_abs_sym + lo
                        allowed_qty = (allowed_weight * equity) / (mult * ref_px)
                        if allowed_qty > 1e-12:
                            clamped_intents.append(replace(intent, qty_value=allowed_qty))
                            lo_by_sym[sym] = lo - allowed_weight
                            hi_by_sym.setdefault(sym, hi)

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
