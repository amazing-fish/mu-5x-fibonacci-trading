"""Port of the Fibonacci Pullback strategy onto the Layer 2 `Strategy` protocol.

Supports two explicit modes:
1. `legacy_parity=True` (e.g. `FibonacciPullbackStrategy.from_legacy_config(cfg)`):
   Reproduces `mu_strategy.backtest.run_backtest` rules faithfully on the unified
   Layer 3 `Simulator` under zero slippage/funding, including 15m Fibonacci
   retests, 1h traffic-light regimes, `second_pullback`/`break_high`/`direct_next_open`
   entries, 4-stage pyramiding, and all stop-tightening curves.
2. Default Lab mode (`legacy_parity=False`):
   Uses non-overfit `fib_lookback=32` default (never hardcoded `lb=8`) and a
   configurable `min_stop_pct` + `atr_stop_multiplier` initial stop floor to
   eliminate micro-wick same-bar stop-outs.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from mu_strategy.core.risk import resolve_initial_stop
from mu_strategy.core.trading_calendar import CONTINUOUS_CALENDAR, LEGACY_CALENDAR
from mu_strategy.models import Candle
from mu_strategy.strategies.features import FeatureCache, _unwrap_series_bounds
from mu_strategy.strategies.position_rules import (
    PositionFillSnapshot,
    PositionStateSnapshot,
    decide_pyramid_add,
    tighten_stop,
)
from mu_strategy.strategies.protocol import (
    EMPTY_OUTPUT,
    BarContext,
    OrderIntent,
    ParamSpace,
    ParamSpec,
    PriceExclusionBand,
    SessionGate,
    StrategyOutput,
)
from mu_strategy.strategy import (
    StrategyConfig,
    is_preferred_us_cash_window,
    one_hour_regime,
)

__all__ = [
    "FibonacciPullbackParams",
    "FibonacciPullbackStrategy",
    "fibonacci_pullback_param_space",
]

_isfinite = math.isfinite
_VALID_ENTRY_MODES = frozenset({"second_pullback", "break_high", "direct_next_open"})


def _validate_margin_steps(steps: Sequence[float]) -> None:
    if not steps:
        raise ValueError("margin_steps must not be empty")
    for idx, step in enumerate(steps):
        if isinstance(step, bool) or not isinstance(step, (int, float)) or not _isfinite(step):
            raise ValueError(f"margin_steps[{idx}] must be finite, got {step!r}")
        if idx == 0 and step <= 0.0:
            raise ValueError(f"first margin_step must be positive, got {step!r}")
        if step < 0.0:
            raise ValueError(f"margin_steps[{idx}] must be non-negative, got {step!r}")


def _validate_stage_schedule(margin_steps: Sequence[float], add_thresholds: Sequence[float]) -> None:
    _validate_margin_steps(margin_steps)
    required_thresholds = len(margin_steps) - 1
    if len(add_thresholds) < required_thresholds:
        raise ValueError(
            f"add_thresholds must contain at least {required_thresholds} entries for {len(margin_steps)} "
            f"margin_steps, got {len(add_thresholds)}"
        )
    for idx, threshold in enumerate(add_thresholds):
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not _isfinite(threshold) or threshold < 0.0:
            raise ValueError(f"add_thresholds[{idx}] must be finite and non-negative, got {threshold!r}")


@dataclass(frozen=True, slots=True)
class FibonacciPullbackParams:
    """Parameters for `FibonacciPullbackStrategy`."""

    symbol: str = "MUUSDT"
    primary_interval: str = "15m"
    regime_interval: str = "1h"
    legacy_parity: bool = False
    legacy_config: StrategyConfig | None = None

    entry_execution: str = "second_pullback"
    second_pullback_wait_bars: int = 8
    fib_lookback: int = 32
    fib_tolerance_pct: float = 0.002
    leverage: float = 5.0
    margin_steps: tuple[float, ...] = (0.20, 0.20, 0.20, 0.40)
    initial_stop_pct: float = 0.02
    min_stop_pct: float = 0.006
    atr_stop_multiplier: float = 1.0
    atr_period: int = 14
    stop_tightening: str = "baseline"
    add_thresholds: tuple[float, ...] = (0.02, 0.04, 0.06)
    rsi_floor: float = 45.0
    rsi_add_floor: float = 50.0
    allowed_regimes: tuple[str, ...] = ("green", "yellow")
    full_size_regime: str = "green"
    stop_buffer_pct: float = 0.0005
    stop_transition_bars: int = 8
    stop_transition_curve: str = "linear"
    use_session_window: bool = True
    trading_calendar_id: str = LEGACY_CALENDAR
    trading_windows_et: tuple[tuple[str, str], ...] = (("09:45", "11:30"), ("14:30", "15:45"))

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(self.primary_interval, str) or not self.primary_interval.strip():
            raise ValueError("primary_interval must be a non-empty string")
        if not isinstance(self.regime_interval, str) or not self.regime_interval.strip():
            raise ValueError("regime_interval must be a non-empty string")
        if not isinstance(self.legacy_parity, bool):
            raise ValueError("legacy_parity must be a bool")
        if self.legacy_config is not None and not isinstance(self.legacy_config, StrategyConfig):
            raise ValueError("legacy_config must be a StrategyConfig or None")
        if self.entry_execution not in _VALID_ENTRY_MODES:
            raise ValueError(f"unsupported entry_execution: {self.entry_execution!r}")
        if (
            isinstance(self.second_pullback_wait_bars, bool)
            or not isinstance(self.second_pullback_wait_bars, int)
            or self.second_pullback_wait_bars < 1
        ):
            raise ValueError("second_pullback_wait_bars must be an integer >= 1")
        if isinstance(self.fib_lookback, bool) or not isinstance(self.fib_lookback, int) or self.fib_lookback < 2:
            raise ValueError("fib_lookback must be an integer >= 2")
        if (
            isinstance(self.initial_stop_pct, bool)
            or not isinstance(self.initial_stop_pct, (int, float))
            or not _isfinite(self.initial_stop_pct)
            or not (0.0 < self.initial_stop_pct < 1.0)
        ):
            raise ValueError("initial_stop_pct must be in (0, 1)")
        if (
            isinstance(self.min_stop_pct, bool)
            or not isinstance(self.min_stop_pct, (int, float))
            or not _isfinite(self.min_stop_pct)
            or not (0.0 <= self.min_stop_pct < 1.0)
        ):
            raise ValueError("min_stop_pct must be in [0, 1)")
        if (
            isinstance(self.atr_stop_multiplier, bool)
            or not isinstance(self.atr_stop_multiplier, (int, float))
            or not _isfinite(self.atr_stop_multiplier)
            or self.atr_stop_multiplier < 0.0
        ):
            raise ValueError("atr_stop_multiplier must be finite and >= 0")
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int) or self.atr_period < 1:
            raise ValueError("atr_period must be an integer >= 1")
        if (
            isinstance(self.leverage, bool)
            or not isinstance(self.leverage, (int, float))
            or not _isfinite(self.leverage)
            or self.leverage <= 0.0
        ):
            raise ValueError("leverage must be positive and finite")
        if not isinstance(self.use_session_window, bool):
            raise ValueError("use_session_window must be a bool")
        _validate_stage_schedule(self.margin_steps, self.add_thresholds)


def fibonacci_pullback_param_space() -> ParamSpace:
    return ParamSpace(
        (
            ParamSpec("entry_execution", "categorical", "second_pullback", choices=("second_pullback", "break_high", "direct_next_open")),
            ParamSpec("fib_lookback", "int", 32, min_value=16, max_value=96, step=8),
            ParamSpec("second_pullback_wait_bars", "int", 8, min_value=2, max_value=16, step=2),
            ParamSpec("initial_stop_pct", "float", 0.02, min_value=0.01, max_value=0.05, step=0.005),
            ParamSpec("min_stop_pct", "float", 0.006, min_value=0.0, max_value=0.02, step=0.002),
            ParamSpec("atr_stop_multiplier", "float", 1.0, min_value=0.0, max_value=3.0, step=0.5),
            ParamSpec("rsi_floor", "float", 45.0, min_value=35.0, max_value=55.0, step=5.0),
            ParamSpec("stop_tightening", "categorical", "baseline", choices=("baseline", "half_protect", "green_wide", "delayed_baseline")),
            ParamSpec("use_session_window", "bool", True),
        )
    )


class _BarSeriesCandleAdapter(Sequence[Candle]):
    """Zero-allocation lazy `Sequence[Candle]` view bounded to `[start, end)`."""

    __slots__ = ("_base", "_start", "_end")

    def __init__(self, base: Any, start: int, end: int) -> None:
        self._base = base
        self._start = start
        self._end = end

    def __len__(self) -> int:
        return self._end - self._start

    def __getitem__(self, index: int) -> Candle:  # type: ignore[override]
        length = self._end - self._start
        if index < 0:
            index += length
        if index < 0 or index >= length:
            raise IndexError("candle index out of range")
        i = self._start + index
        base = self._base
        return Candle(
            open_time_ms=int(base.open_times[i]),
            open=float(base.opens[i]),
            high=float(base.highs[i]),
            low=float(base.lows[i]),
            close=float(base.closes[i]),
            volume=float(base.volumes[i]),
        )


@dataclass(slots=True)
class _PendingSecondPullback:
    fib_level: float
    expires_bar_idx: int
    signal_stop: float


@dataclass(slots=True)
class _PendingImmediateEntry:
    mode: str
    signal_stop: float
    trigger_high: float | None = None
    max_entry_price: float | None = None


def _resolve_features(market: Any, fallback: FeatureCache) -> FeatureCache:
    fc = getattr(market, "features", None)
    if fc is None:
        fc = getattr(market, "feature_cache", None)
    return fc if fc is not None else fallback


def _nearest_fib_retest_from_arrays(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    start: int,
    curr_abs_idx: int,
    fib_lookback: int,
    fib_tolerance_pct: float,
) -> float | None:
    rel_idx = curr_abs_idx - start
    if rel_idx <= 0 or fib_lookback <= 0:
        return None
    win_start = max(start, curr_abs_idx - fib_lookback)
    if curr_abs_idx - win_start < 2:
        return None

    low_idx = win_start
    low_val = lows[win_start]
    high_idx = win_start
    high_val = highs[win_start]
    for idx in range(win_start + 1, curr_abs_idx):
        lo = lows[idx]
        hi = highs[idx]
        if lo < low_val:
            low_val = lo
            low_idx = idx
        if hi > high_val:
            high_val = hi
            high_idx = idx

    if low_idx >= high_idx or high_val <= low_val:
        return None

    span = high_val - low_val
    curr_low = lows[curr_abs_idx]
    curr_close = closes[curr_abs_idx]
    tol_factor = 1.0 + fib_tolerance_pct
    best_level: float | None = None
    best_dist = math.inf
    for ratio in (0.382, 0.5, 0.618):
        level = round(high_val - span * ratio, 8)
        if curr_low <= level * tol_factor and curr_close >= level:
            dist = abs(curr_close - level)
            if dist < best_dist:
                best_dist = dist
                best_level = level
    return best_level


def _reverse_fib_exclusion_bands_from_arrays(
    highs: Sequence[float],
    lows: Sequence[float],
    start: int,
    curr_abs_idx: int,
    lookback: int,
    tolerance_pct: float,
) -> tuple[PriceExclusionBand, ...]:
    """Compute causal reverse-Fibonacci resistance bands known at ``curr_abs_idx``.

    Evaluated at fill time against the actual fill price (e.g. ``next_bar.open`` for
    ``direct_next_open`` or ``max(next_bar.open, curr_high)`` for ``break_high``) so
    resistance checks match legacy ``should_execute_entry`` without look-ahead.
    """
    rel_idx = curr_abs_idx - start
    if rel_idx <= 0 or lookback <= 0:
        return ()
    win_start = max(start, curr_abs_idx - lookback)
    if curr_abs_idx - win_start < 2:
        return ()

    high_idx = win_start
    high_val = highs[win_start]
    low_idx = win_start
    low_val = lows[win_start]
    for idx in range(win_start + 1, curr_abs_idx):
        hi = highs[idx]
        lo = lows[idx]
        if hi > high_val:
            high_val = hi
            high_idx = idx
        if lo < low_val:
            low_val = lo
            low_idx = idx

    if high_idx >= low_idx or high_val <= low_val:
        return ()

    span = high_val - low_val
    return tuple(
        PriceExclusionBand(center=low_val + span * ratio, tolerance_pct=tolerance_pct)
        for ratio in (0.382, 0.5, 0.618)
    )


class FibonacciPullbackStrategy:
    """Single-source Fibonacci Pullback strategy on the Layer 2 `Strategy` protocol."""

    strategy_id: str = "fibonacci_pullback"
    version: str = "2.0.0"
    supported_sides: tuple[str, ...] = ("buy",)

    def __init__(self, params: FibonacciPullbackParams | None = None, **kwargs: Any) -> None:
        if params is not None and kwargs:
            raise ValueError("pass either params or keyword overrides, not both")
        self.params: FibonacciPullbackParams = params if params is not None else FibonacciPullbackParams(**kwargs)
        self._config: StrategyConfig = self._build_effective_config(self.params)
        _validate_stage_schedule(self._config.margin_steps, self._config.add_thresholds)
        self._session_gate: SessionGate | None = (
            SessionGate(
                trading_calendar_id=self._config.trading_calendar_id,
                trading_windows_et=self._config.trading_windows_et,
                trading_calendar_sha256=self._config.trading_calendar_sha256 or "",
            )
            if (self.params.legacy_parity or self.params.use_session_window)
            else None
        )
        self.required_intervals: tuple[str, ...] = (
            self.params.primary_interval,
            self.params.regime_interval,
        )
        if self.params.legacy_parity:
            # Legacy `run_backtest` begins evaluating on bar index 1 without a long warmup skip.
            self.warmup_bars: Mapping[str, int] = MappingProxyType(
                {self.params.primary_interval: 1, self.params.regime_interval: 0}
            )
        else:
            primary_warmup = max(self._config.fib_lookback, self.params.atr_period, 26)
            self.warmup_bars = MappingProxyType(
                {self.params.primary_interval: primary_warmup, self.params.regime_interval: 26}
            )

        self._fallback_features = FeatureCache()

        # Stateful position & order lifecycle (serialized by snapshot_state / restore_state).
        self._last_qty: float = 0.0
        self._fills: list[tuple[int, float, float]] = []
        self._stop_price: float = 0.0
        self._entry_anchor: float = 0.0
        self._initial_stop_price: float = 0.0
        self._max_stage: int = 0
        self._stop_transition_fill_count: int = 0
        self._stop_transition_start: float = 0.0
        self._pending_entry: _PendingSecondPullback | None = None
        self._pending_immediate: _PendingImmediateEntry | None = None
        self._submitted_entry_bar_idx: int | None = None
        self._pending_add_stage: int | None = None
        self._last_bar_ms: int | None = None

        # Ephemeral read-only caches (never serialized; cleared on reset/restore_state).
        self._cached_base: Any = None
        self._cached_features: Any = None
        self._cached_gen: int = -1
        self._cached_open_times: Any = None
        self._cached_close_times: Any = None
        self._cached_opens: Any = None
        self._cached_highs: Any = None
        self._cached_lows: Any = None
        self._cached_closes: Any = None
        self._cached_rsi14: Any = None
        self._cached_hist: Any = None
        self._cached_atr: Any = None
        self._cached_h1_base: Any = None
        self._cached_h1_features: Any = None
        self._cached_h1_gen: int = -1
        self._cached_h1_close_times: Any = None
        self._cached_h1_closes: Any = None
        self._cached_h1_ema21: Any = None
        self._cached_h1_rsi14: Any = None
        self._cached_h1_hist: Any = None

    @classmethod
    def from_legacy_config(
        cls,
        config: StrategyConfig,
        *,
        primary_interval: str = "15m",
        regime_interval: str = "1h",
        legacy_parity: bool = True,
    ) -> FibonacciPullbackStrategy:
        """Construct a parity-ready strategy directly from an existing `StrategyConfig`."""
        return cls(
            FibonacciPullbackParams(
                symbol=config.symbol,
                primary_interval=primary_interval,
                regime_interval=regime_interval,
                legacy_parity=legacy_parity,
                legacy_config=config,
                entry_execution=config.entry_execution,
                second_pullback_wait_bars=config.second_pullback_wait_bars,
                fib_lookback=config.fib_lookback,
                fib_tolerance_pct=config.fib_tolerance_pct,
                leverage=config.leverage,
                margin_steps=config.margin_steps,
                initial_stop_pct=config.initial_stop_pct,
                min_stop_pct=0.0 if legacy_parity else 0.006,
                atr_stop_multiplier=0.0 if legacy_parity else 1.0,
                stop_tightening=config.stop_tightening,
                add_thresholds=config.add_thresholds,
                rsi_floor=config.rsi_floor,
                rsi_add_floor=config.rsi_add_floor,
                allowed_regimes=config.allowed_regimes,
                full_size_regime=config.full_size_regime,
                stop_buffer_pct=config.stop_buffer_pct,
                stop_transition_bars=config.stop_transition_bars,
                stop_transition_curve=config.stop_transition_curve,
                use_session_window=True,
                trading_calendar_id=config.trading_calendar_id,
                trading_windows_et=config.trading_windows_et,
            )
        )

    @staticmethod
    def _build_effective_config(params: FibonacciPullbackParams) -> StrategyConfig:
        if params.legacy_config is not None:
            if params.legacy_parity or params.use_session_window:
                return params.legacy_config
            return replace(
                params.legacy_config,
                trading_calendar_id=CONTINUOUS_CALENDAR,
                trading_calendar_sha256=None,
                trading_windows_et=(("00:00", "23:59"),),
            )
        calendar_id = params.trading_calendar_id if params.use_session_window else CONTINUOUS_CALENDAR
        windows = params.trading_windows_et if params.use_session_window else (("00:00", "23:59"),)
        return StrategyConfig(
            symbol=params.symbol,
            entry_execution=params.entry_execution,
            second_pullback_wait_bars=params.second_pullback_wait_bars,
            stop_tightening=params.stop_tightening,
            leverage=params.leverage,
            margin_steps=params.margin_steps,
            initial_stop_pct=params.initial_stop_pct,
            add_thresholds=params.add_thresholds,
            rsi_floor=params.rsi_floor,
            rsi_add_floor=params.rsi_add_floor,
            fib_tolerance_pct=params.fib_tolerance_pct,
            fib_lookback=params.fib_lookback,
            stop_buffer_pct=params.stop_buffer_pct,
            stop_transition_bars=params.stop_transition_bars,
            stop_transition_curve=params.stop_transition_curve,
            allowed_regimes=params.allowed_regimes,
            full_size_regime=params.full_size_regime,
            trading_windows_et=windows,
            trading_calendar_id=calendar_id,
        )

    @property
    def effective_config(self) -> StrategyConfig:
        return self._config

    def param_space(self) -> ParamSpace:
        return fibonacci_pullback_param_space()

    def _clear_ephemeral_cache(self) -> None:
        self._cached_base = None
        self._cached_features = None
        self._cached_gen = -1
        self._cached_open_times = None
        self._cached_close_times = None
        self._cached_opens = None
        self._cached_highs = None
        self._cached_lows = None
        self._cached_closes = None
        self._cached_rsi14 = None
        self._cached_hist = None
        self._cached_atr = None
        self._cached_h1_base = None
        self._cached_h1_features = None
        self._cached_h1_gen = -1
        self._cached_h1_close_times = None
        self._cached_h1_closes = None
        self._cached_h1_ema21 = None
        self._cached_h1_rsi14 = None
        self._cached_h1_hist = None

    def reset(self) -> None:
        self._last_qty = 0.0
        self._fills = []
        self._stop_price = 0.0
        self._entry_anchor = 0.0
        self._initial_stop_price = 0.0
        self._max_stage = 0
        self._stop_transition_fill_count = 0
        self._stop_transition_start = 0.0
        self._pending_entry = None
        self._pending_immediate = None
        self._submitted_entry_bar_idx = None
        self._pending_add_stage = None
        self._last_bar_ms = None
        self._clear_ephemeral_cache()

    def _clear_position(self) -> None:
        self._fills = []
        self._stop_price = 0.0
        self._entry_anchor = 0.0
        self._initial_stop_price = 0.0
        self._max_stage = 0
        self._stop_transition_fill_count = 0
        self._stop_transition_start = 0.0
        self._pending_add_stage = None

    def _window_allowed(self, ts_ms: int) -> bool:
        if not self.params.legacy_parity and not self.params.use_session_window:
            return True
        return is_preferred_us_cash_window(ts_ms, self._config)

    def _compute_initial_stop(
        self,
        entry_price: float,
        signal_stop: float | None,
        atr_val: float | None,
    ) -> float:
        if self.params.legacy_parity:
            return resolve_initial_stop(entry_price, signal_stop, self._config)

        max_stop_pct = max(self._config.initial_stop_pct, self.params.min_stop_pct)
        hard_stop = entry_price * (1.0 - max_stop_pct)
        if signal_stop is not None and signal_stop < entry_price:
            base_stop = max(hard_stop, signal_stop)
        else:
            base_stop = hard_stop

        floor_price = entry_price
        if self.params.min_stop_pct > 0.0:
            floor_price = min(floor_price, entry_price * (1.0 - self.params.min_stop_pct))
        if (
            self.params.atr_stop_multiplier > 0.0
            and atr_val is not None
            and _isfinite(atr_val)
            and atr_val > 0.0
        ):
            floor_price = min(floor_price, entry_price - (self.params.atr_stop_multiplier * atr_val))

        stop = min(base_stop, floor_price)
        stop = max(hard_stop, stop)
        return max(1e-12, min(stop, entry_price * (1.0 - 1e-9)))

    def _hourly_regime_at(self, ctx: BarContext, target_ts_ms: int, features: FeatureCache) -> str | None:
        market = ctx.market
        regime_iv = self.params.regime_interval
        has_series = getattr(market, "has_series", None)
        if has_series is not None and not has_series(ctx.symbol, regime_iv):
            return "yellow" if self.params.legacy_parity else None
        try:
            h1_bars = market.closed_bars(ctx.symbol, regime_iv)
        except KeyError:
            return "yellow" if self.params.legacy_parity else None

        try:
            h1_base = h1_bars._series
            h1_start = h1_bars.start
            h1_end = h1_bars.end
        except AttributeError:
            h1_base, h1_start, h1_end = _unwrap_series_bounds(h1_bars)
        if h1_end <= h1_start:
            return "yellow" if self.params.legacy_parity else None

        feat_gen = getattr(features, "_generation", 0)
        if (
            h1_base is not self._cached_h1_base
            or features is not self._cached_h1_features
            or feat_gen != self._cached_h1_gen
        ):
            self._cached_h1_base = h1_base
            self._cached_h1_features = features
            self._cached_h1_gen = feat_gen
            self._cached_h1_close_times = h1_base.close_times
            self._cached_h1_closes = h1_base.closes
            self._cached_h1_ema21 = features._ema_array(h1_base, 21)
            self._cached_h1_rsi14 = features._rsi_array(h1_base, 14)
            _, _, self._cached_h1_hist = features._macd_arrays(h1_base, 12, 26, 9)

        # Strictly causal: bounded by h1_end (already <= ctx.now_ms) AND close_time_ms <= target_ts_ms.
        k = bisect_right(self._cached_h1_close_times, target_ts_ms, h1_start, h1_end) - 1
        if k < h1_start:
            return "yellow" if self.params.legacy_parity else None

        h1_close = float(self._cached_h1_closes[k])
        h1_ema21 = float(self._cached_h1_ema21[k])
        h1_rsi14 = float(self._cached_h1_rsi14[k])
        hist_arr = self._cached_h1_hist
        h1_hist = float(hist_arr[k])
        h1_prev_hist = float(hist_arr[k - 1]) if k > h1_start else h1_hist
        if not (_isfinite(h1_close) and _isfinite(h1_ema21) and _isfinite(h1_hist) and _isfinite(h1_prev_hist)):
            return "yellow" if self.params.legacy_parity else None
        if not _isfinite(h1_rsi14):
            if self.params.legacy_parity:
                h1_rsi14 = 50.0
            else:
                return None
        return one_hour_regime(h1_close, h1_ema21, h1_rsi14, h1_hist, h1_prev_hist)

    def _position_snapshot(self) -> PositionStateSnapshot:
        return PositionStateSnapshot(
            fills=tuple(PositionFillSnapshot(time_ms=t, price=p, units=u) for t, p, u in self._fills),
            stop_price=self._stop_price,
            entry_anchor=self._entry_anchor,
            initial_stop_price=self._initial_stop_price,
            max_stage=self._max_stage,
            stop_transition_fill_count=self._stop_transition_fill_count,
            stop_transition_start=self._stop_transition_start,
        )

    def _emit_second_pullback_intents(
        self,
        ctx: BarContext,
        curr_rel_idx: int,
        atr_val: float | None,
    ) -> StrategyOutput:
        pending = self._pending_entry
        if pending is None:
            return EMPTY_OUTPUT
        next_bar_idx = curr_rel_idx + 1
        if next_bar_idx > pending.expires_bar_idx:
            return EMPTY_OUTPUT

        entry_qty_fraction = self._config.margin_steps[0] * self._config.leverage
        initial_stop = self._compute_initial_stop(pending.fib_level, pending.signal_stop, atr_val)
        entry_intent = OrderIntent(
            intent_id=f"fib_sp_entry_{curr_rel_idx}",
            symbol=ctx.symbol,
            side="buy",
            order_type="limit",
            qty_mode="equity_fraction",
            qty_value=entry_qty_fraction,
            limit_price=pending.fib_level,
            reduce_only=False,
            expires_after_bars=1,
            tag="fib_second_pullback_entry",
            session_gate=self._session_gate,
        )
        stop_intent = OrderIntent(
            intent_id=f"fib_sp_init_stop_{curr_rel_idx}",
            symbol=ctx.symbol,
            side="sell",
            order_type="stop_market",
            qty_mode="close_all",
            qty_value=0.0,
            stop_price=initial_stop,
            reduce_only=True,
            expires_after_bars=1,
            tag="initial_stop",
        )
        self._submitted_entry_bar_idx = next_bar_idx
        return StrategyOutput(
            intents=(entry_intent, stop_intent),
            diagnostics=MappingProxyType(
                {
                    "mode": "second_pullback",
                    "fib_level": pending.fib_level,
                    "initial_stop": initial_stop,
                    "expires_bar_idx": pending.expires_bar_idx,
                }
            ),
        )

    def on_bar(self, ctx: BarContext) -> StrategyOutput:
        cfg = self._config
        market = ctx.market
        symbol = ctx.symbol
        bars = market.closed_bars(symbol, self.params.primary_interval)
        try:
            base = bars._series
            start = bars.start
            end = bars.end
        except AttributeError:
            base, start, end = _unwrap_series_bounds(bars)
        count = end - start
        self._last_bar_ms = ctx.now_ms

        if count < 2:
            return EMPTY_OUTPUT

        features = getattr(market, "features", None)
        if features is None:
            features = getattr(market, "feature_cache", None)
        if features is None:
            features = self._fallback_features
        feat_gen = getattr(features, "_generation", 0)

        if base is not self._cached_base or features is not self._cached_features or feat_gen != self._cached_gen:
            self._cached_base = base
            self._cached_features = features
            self._cached_gen = feat_gen
            self._cached_open_times = base.open_times
            self._cached_close_times = base.close_times
            self._cached_opens = base.opens
            self._cached_highs = base.highs
            self._cached_lows = base.lows
            self._cached_closes = base.closes
            self._cached_rsi14 = features._rsi_array(base, 14)
            _, _, self._cached_hist = features._macd_arrays(base, 12, 26, 9)
            self._cached_atr = (
                features._atr_array(base, self.params.atr_period)
                if (not self.params.legacy_parity and self.params.atr_stop_multiplier > 0.0)
                else None
            )

        curr_abs_idx = end - 1
        curr_rel_idx = count - 1
        curr_open_ms = int(self._cached_open_times[curr_abs_idx])
        next_open_ms = int(self._cached_close_times[curr_abs_idx])
        curr_high = float(self._cached_highs[curr_abs_idx])
        curr_low = float(self._cached_lows[curr_abs_idx])
        curr_close = float(self._cached_closes[curr_abs_idx])

        if not (_isfinite(curr_close) and curr_close > 0.0):
            return EMPTY_OUTPUT

        atr_val: float | None = None
        if self._cached_atr is not None and count >= self.params.atr_period:
            atr_val = float(self._cached_atr[curr_abs_idx])

        pos = ctx.portfolio.positions.get(symbol)
        curr_qty = pos.qty if pos is not None else 0.0
        prev_qty = self._last_qty

        # ------------------------------------------------------------------
        # 1. Synchronize internal position / fill state with realized fills.
        # ------------------------------------------------------------------
        just_entered_second_pullback = False

        if prev_qty > 0.0 and curr_qty <= 0.0:
            # Open position exited/stopped on this bar -> do not re-enter on same bar.
            self._clear_position()
            self._pending_entry = None
            self._pending_immediate = None
            self._submitted_entry_bar_idx = None
            self._last_qty = 0.0
            return EMPTY_OUTPUT

        if prev_qty <= 0.0 and curr_qty <= 0.0:
            self._last_qty = 0.0
            # Check if a submitted second_pullback limit filled AND stopped out on this same bar.
            # Matches legacy `backtest.py`: second_pullback skips re-scanning the fill bar (L245-248),
            # whereas break_high / direct_next_open advance to `next_candle` with position=None and
            # still scan that bar for a fresh signal (L309-316).
            if self._submitted_entry_bar_idx == curr_rel_idx:
                self._submitted_entry_bar_idx = None
                if self._pending_entry is not None and self._window_allowed(curr_open_ms):
                    touch_through = bool(getattr(ctx, "require_touch_through", False))
                    filled_touch = (
                        curr_low < self._pending_entry.fib_level
                        if touch_through
                        else curr_low <= self._pending_entry.fib_level
                    )
                    if filled_touch:
                        self._pending_entry = None
                        return EMPTY_OUTPUT
                self._pending_immediate = None
            elif self._pending_immediate is not None:
                self._pending_immediate = None

        elif prev_qty <= 0.0 and curr_qty > 0.0:
            # Fresh entry filled on this bar!
            entry_price = pos.avg_entry_price if pos is not None and pos.avg_entry_price > 0.0 else curr_close
            signal_stop: float | None = None
            stop_entry_price = entry_price
            if self._pending_entry is not None:
                signal_stop = self._pending_entry.signal_stop
                just_entered_second_pullback = True
            elif self._pending_immediate is not None:
                signal_stop = self._pending_immediate.signal_stop
                if (
                    self._pending_immediate.trigger_high is not None
                    and math.isclose(entry_price, self._pending_immediate.trigger_high, rel_tol=0.0, abs_tol=1e-11)
                ):
                    stop_entry_price = self._pending_immediate.trigger_high

            init_stop = self._compute_initial_stop(stop_entry_price, signal_stop, atr_val)
            self._fills = [(curr_open_ms, entry_price, curr_qty)]
            self._entry_anchor = entry_price
            self._initial_stop_price = init_stop
            self._stop_price = init_stop
            self._max_stage = 1
            self._stop_transition_fill_count = 0
            self._stop_transition_start = 0.0
            self._pending_add_stage = None
            self._pending_entry = None
            self._pending_immediate = None
            self._submitted_entry_bar_idx = None
            self._last_qty = curr_qty

            if just_entered_second_pullback:
                # Matches legacy `backtest.py:240-248`: after second_pullback fill bar,
                # carry initial stop into next bar without running tighten/pyramid on fill bar.
                stop_intent = OrderIntent(
                    intent_id=f"fib_stop_{curr_rel_idx}",
                    symbol=ctx.symbol,
                    side="sell",
                    order_type="stop_market",
                    qty_mode="close_all",
                    qty_value=0.0,
                    stop_price=self._stop_price,
                    reduce_only=True,
                    expires_after_bars=1,
                    tag="stop",
                )
                return StrategyOutput(
                    intents=(stop_intent,),
                    diagnostics=MappingProxyType(
                        {"event": "entry_filled", "stage": 1, "stop_price": self._stop_price}
                    ),
                )

        else:
            # Continuing open position (`prev_qty > 0` and `curr_qty > 0`).
            if curr_qty > prev_qty + 1e-12:
                prev_cost = sum(p * u for _, p, u in self._fills)
                delta_qty = curr_qty - prev_qty
                avg_px = pos.avg_entry_price if pos is not None else curr_close
                add_px = ((avg_px * curr_qty) - prev_cost) / delta_qty
                if not _isfinite(add_px) or add_px <= 0.0:
                    add_px = avg_px
                next_stage = self._pending_add_stage if self._pending_add_stage is not None else (self._max_stage + 1)
                self._fills.append((curr_open_ms, add_px, delta_qty))
                self._max_stage = next_stage
            self._pending_add_stage = None
            self._last_qty = curr_qty

        # ------------------------------------------------------------------
        # 2. Open-position management: tighten stop + plan pyramid add.
        # ------------------------------------------------------------------
        if curr_qty > 0.0:
            rsi_val = float(self._cached_rsi14[curr_abs_idx])
            if not _isfinite(rsi_val):
                rsi_val = 50.0 if self.params.legacy_parity else rsi_val
            hist_val = float(self._cached_hist[curr_abs_idx])
            prev_hist_val = float(self._cached_hist[curr_abs_idx - 1])
            curr_regime = self._hourly_regime_at(ctx, curr_open_ms, features)
            tighten_regime = curr_regime if curr_regime is not None else "yellow"
            candle_view = _BarSeriesCandleAdapter(base, start, end)
            curr_candle = candle_view[curr_rel_idx]
            outcome = tighten_stop(
                self._position_snapshot(),
                curr_candle,
                index=curr_rel_idx,
                candles=candle_view,
                regime=tighten_regime,
                config=cfg,
            )
            self._stop_price = outcome.stop_price
            self._stop_transition_fill_count = outcome.transition_fill_count
            self._stop_transition_start = outcome.transition_start

            intents: list[OrderIntent] = [
                OrderIntent(
                    intent_id=f"fib_stop_{curr_rel_idx}",
                    symbol=ctx.symbol,
                    side="sell",
                    order_type="stop_market",
                    qty_mode="close_all",
                    qty_value=0.0,
                    stop_price=self._stop_price,
                    reduce_only=True,
                    expires_after_bars=1,
                    tag="stop",
                )
            ]

            if curr_regime is not None and _isfinite(rsi_val) and ctx.portfolio.equity > 0.0:
                snapshot = self._position_snapshot()
                plan = decide_pyramid_add(
                    snapshot,
                    curr_candle,
                    rsi_value=rsi_val,
                    macd_hist=hist_val,
                    previous_macd_hist=prev_hist_val,
                    regime=curr_regime,
                    config=cfg,
                )
                if plan.should_add:
                    # Re-check regime at next bar open (strictly causal: close_time_ms <= ctx.now_ms).
                    next_regime = self._hourly_regime_at(ctx, next_open_ms, features)
                    if next_regime is not None:
                        exec_check = decide_pyramid_add(
                            snapshot,
                            curr_candle,
                            rsi_value=rsi_val,
                            macd_hist=hist_val,
                            previous_macd_hist=prev_hist_val,
                            regime=next_regime,
                            config=cfg,
                        )
                        if (
                            exec_check.should_add
                            and plan.stage is not None
                            and plan.margin_fraction is not None
                            and plan.margin_fraction > 0.0
                            and plan.trigger_price is not None
                        ):
                            intents.append(
                                OrderIntent(
                                    intent_id=f"fib_add_s{plan.stage}_{curr_rel_idx}",
                                    symbol=ctx.symbol,
                                    side="buy",
                                    order_type="stop_market",
                                    qty_mode="equity_fraction",
                                    qty_value=plan.margin_fraction * cfg.leverage,
                                    stop_price=plan.trigger_price,
                                    reduce_only=False,
                                    expires_after_bars=1,
                                    tag=f"fib_pyramid_stage_{plan.stage}",
                                    session_gate=self._session_gate,
                                )
                            )
                            self._pending_add_stage = plan.stage

            return StrategyOutput(
                intents=tuple(intents),
                diagnostics=MappingProxyType(
                    {
                        "regime": curr_regime,
                        "stage": self._max_stage,
                        "stop_price": self._stop_price,
                    }
                ),
            )

        # ------------------------------------------------------------------
        # 4. Flat: manage pending `second_pullback` order or scan new signal.
        # ------------------------------------------------------------------
        if ctx.portfolio.equity <= 0.0:
            self._pending_entry = None
            return EMPTY_OUTPUT

        if cfg.entry_execution == "second_pullback" and self._pending_entry is not None:
            if curr_rel_idx > self._pending_entry.expires_bar_idx:
                self._pending_entry = None
                # Matches legacy `backtest.py:228-229`: expire and fall through to scan curr bar.
            else:
                # Pending entry still alive on this bar and did not fill -> do not scan a new signal.
                return self._emit_second_pullback_intents(ctx, curr_rel_idx, atr_val)

        if not self.params.legacy_parity and count < self.warmup_bars[self.params.primary_interval]:
            return EMPTY_OUTPUT

        if not self._window_allowed(curr_open_ms):
            return EMPTY_OUTPUT

        fib_level = _nearest_fib_retest_from_arrays(
            self._cached_highs,
            self._cached_lows,
            self._cached_closes,
            start,
            curr_abs_idx,
            cfg.fib_lookback,
            cfg.fib_tolerance_pct,
        )
        if fib_level is None:
            return EMPTY_OUTPUT

        rsi_val = float(self._cached_rsi14[curr_abs_idx])
        if not _isfinite(rsi_val):
            if self.params.legacy_parity:
                rsi_val = 50.0
            else:
                return EMPTY_OUTPUT

        curr_regime = self._hourly_regime_at(ctx, curr_open_ms, features)
        if curr_regime is None or curr_regime not in cfg.allowed_regimes:
            return EMPTY_OUTPUT
        if rsi_val < cfg.rsi_floor:
            return EMPTY_OUTPUT
        hist_val = float(self._cached_hist[curr_abs_idx])
        prev_hist_val = float(self._cached_hist[curr_abs_idx - 1])
        if hist_val < prev_hist_val and hist_val < 0.0:
            return EMPTY_OUTPUT

        tolerance = fib_level * cfg.fib_tolerance_pct
        if not (curr_low <= fib_level + tolerance and curr_close >= fib_level):
            return EMPTY_OUTPUT

        signal_stop = max(curr_close * (1.0 - cfg.initial_stop_pct), curr_low)

        # Dispatch by entry_execution mode.
        if cfg.entry_execution == "second_pullback":
            self._pending_entry = _PendingSecondPullback(
                fib_level=fib_level,
                expires_bar_idx=curr_rel_idx + cfg.second_pullback_wait_bars,
                signal_stop=signal_stop,
            )
            return self._emit_second_pullback_intents(ctx, curr_rel_idx, atr_val)

        # `direct_next_open` or `break_high`: execution happens on the immediately following bar
        # and session eligibility is enforced at fill time against the actual fill bar open via `session_gate`.
        signal_range_pct = (curr_high - curr_low) / curr_close if curr_close else 0.0
        if cfg.max_signal_range_pct is not None and signal_range_pct > cfg.max_signal_range_pct:
            return EMPTY_OUTPUT

        max_above_fib = cfg.max_entry_above_fib_pct
        if curr_regime == "yellow" and cfg.yellow_max_entry_above_fib_pct is not None:
            max_above_fib = cfg.yellow_max_entry_above_fib_pct

        max_entry_price: float | None = None
        if max_above_fib is not None:
            max_entry_price = fib_level * (1.0 + max_above_fib)
        if cfg.max_entry_above_signal_close_pct is not None:
            cap_close = curr_close * (1.0 + cfg.max_entry_above_signal_close_pct)
            max_entry_price = cap_close if max_entry_price is None else min(max_entry_price, cap_close)

        entry_qty_fraction = cfg.margin_steps[0] * cfg.leverage
        excluded_bands: tuple[PriceExclusionBand, ...] = (
            _reverse_fib_exclusion_bands_from_arrays(
                base.highs,
                base.lows,
                start,
                curr_abs_idx,
                cfg.reverse_fib_lookback,
                cfg.reverse_fib_tolerance_pct,
            )
            if cfg.block_reverse_fib_resistance
            else ()
        )

        if cfg.entry_execution == "direct_next_open":
            if max_entry_price is not None and not (_isfinite(max_entry_price) and max_entry_price > 0.0):
                return EMPTY_OUTPUT
            initial_stop = self._compute_initial_stop(curr_close, signal_stop, atr_val)
            self._pending_immediate = _PendingImmediateEntry(
                mode="direct_next_open",
                signal_stop=signal_stop,
                max_entry_price=max_entry_price,
            )
            self._submitted_entry_bar_idx = curr_rel_idx + 1
            if max_entry_price is not None:
                dno_entry = OrderIntent(
                    intent_id=f"fib_dno_entry_{curr_rel_idx}",
                    symbol=ctx.symbol,
                    side="buy",
                    order_type="limit",
                    qty_mode="equity_fraction",
                    qty_value=entry_qty_fraction,
                    limit_price=max_entry_price,
                    reduce_only=False,
                    expires_after_bars=1,
                    tag="fib_direct_next_open_entry",
                    time_in_force="open_only",
                    session_gate=self._session_gate,
                    excluded_price_bands=excluded_bands,
                )
            else:
                dno_entry = OrderIntent(
                    intent_id=f"fib_dno_entry_{curr_rel_idx}",
                    symbol=ctx.symbol,
                    side="buy",
                    order_type="market",
                    qty_mode="equity_fraction",
                    qty_value=entry_qty_fraction,
                    reduce_only=False,
                    expires_after_bars=1,
                    tag="fib_direct_next_open_entry",
                    time_in_force="open_only",
                    session_gate=self._session_gate,
                    excluded_price_bands=excluded_bands,
                )
            return StrategyOutput(
                intents=(
                    dno_entry,
                    OrderIntent(
                        intent_id=f"fib_dno_init_stop_{curr_rel_idx}",
                        symbol=ctx.symbol,
                        side="sell",
                        order_type="stop_market",
                        qty_mode="close_all",
                        qty_value=0.0,
                        stop_price=initial_stop,
                        reduce_only=True,
                        expires_after_bars=1,
                        tag="initial_stop",
                    ),
                ),
                diagnostics=MappingProxyType(
                    {
                        "mode": "direct_next_open",
                        "fib_level": fib_level,
                        "initial_stop": initial_stop,
                        "max_entry_price": max_entry_price,
                    }
                ),
            )

        # `break_high`: strict `next_candle.high > curr_high`, fill at `max(next_open, curr_high)`.
        trigger_stop = math.nextafter(curr_high, math.inf)
        if max_entry_price is not None and trigger_stop > max_entry_price:
            return EMPTY_OUTPUT

        initial_stop = self._compute_initial_stop(curr_high, signal_stop, atr_val)
        self._pending_immediate = _PendingImmediateEntry(
            mode="break_high",
            signal_stop=signal_stop,
            trigger_high=curr_high,
            max_entry_price=max_entry_price,
        )
        self._submitted_entry_bar_idx = curr_rel_idx + 1

        if max_entry_price is not None:
            entry_intent = OrderIntent(
                intent_id=f"fib_bh_entry_{curr_rel_idx}",
                symbol=ctx.symbol,
                side="buy",
                order_type="stop_limit",
                qty_mode="equity_fraction",
                qty_value=entry_qty_fraction,
                stop_price=trigger_stop,
                limit_price=max_entry_price,
                reduce_only=False,
                expires_after_bars=1,
                tag="fib_break_high_entry",
                session_gate=self._session_gate,
                excluded_price_bands=excluded_bands,
            )
        else:
            entry_intent = OrderIntent(
                intent_id=f"fib_bh_entry_{curr_rel_idx}",
                symbol=ctx.symbol,
                side="buy",
                order_type="stop_market",
                qty_mode="equity_fraction",
                qty_value=entry_qty_fraction,
                stop_price=trigger_stop,
                reduce_only=False,
                expires_after_bars=1,
                tag="fib_break_high_entry",
                session_gate=self._session_gate,
                excluded_price_bands=excluded_bands,
            )

        stop_intent = OrderIntent(
            intent_id=f"fib_bh_init_stop_{curr_rel_idx}",
            symbol=ctx.symbol,
            side="sell",
            order_type="stop_market",
            qty_mode="close_all",
            qty_value=0.0,
            stop_price=initial_stop,
            reduce_only=True,
            expires_after_bars=1,
            tag="initial_stop",
        )
        return StrategyOutput(
            intents=(entry_intent, stop_intent),
            diagnostics=MappingProxyType(
                {
                    "mode": "break_high",
                    "fib_level": fib_level,
                    "trigger_high": curr_high,
                    "initial_stop": initial_stop,
                }
            ),
        )

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "strategy_id": self.strategy_id,
            "last_qty": self._last_qty,
            "fills": [[t, p, u] for t, p, u in self._fills],
            "stop_price": self._stop_price,
            "entry_anchor": self._entry_anchor,
            "initial_stop_price": self._initial_stop_price,
            "max_stage": self._max_stage,
            "stop_transition_fill_count": self._stop_transition_fill_count,
            "stop_transition_start": self._stop_transition_start,
            "pending_entry": (
                {
                    "fib_level": self._pending_entry.fib_level,
                    "expires_bar_idx": self._pending_entry.expires_bar_idx,
                    "signal_stop": self._pending_entry.signal_stop,
                }
                if self._pending_entry is not None
                else None
            ),
            "pending_immediate": (
                {
                    "mode": self._pending_immediate.mode,
                    "signal_stop": self._pending_immediate.signal_stop,
                    "trigger_high": self._pending_immediate.trigger_high,
                    "max_entry_price": self._pending_immediate.max_entry_price,
                }
                if self._pending_immediate is not None
                else None
            ),
            "submitted_entry_bar_idx": self._submitted_entry_bar_idx,
            "pending_add_stage": self._pending_add_stage,
            "last_bar_ms": self._last_bar_ms,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema_version") != 1:
            raise ValueError(f"unsupported FibonacciPullbackStrategy state version: {state.get('schema_version')!r}")

        last_qty = float(state["last_qty"])
        if not _isfinite(last_qty) or last_qty < 0.0:
            raise ValueError("invalid last_qty in state")

        fills_raw = state.get("fills", [])
        fills: list[tuple[int, float, float]] = []
        for entry in fills_raw:
            t, p, u = entry
            if isinstance(t, bool) or not isinstance(t, int):
                raise ValueError("invalid fill timestamp in state")
            pf = float(p)
            uf = float(u)
            if not (_isfinite(pf) and _isfinite(uf)) or pf <= 0.0 or uf <= 0.0:
                raise ValueError("invalid fill price/units in state")
            fills.append((t, pf, uf))

        stop_price = float(state["stop_price"])
        entry_anchor = float(state["entry_anchor"])
        initial_stop = float(state["initial_stop_price"])
        transition_start = float(state["stop_transition_start"])
        for val, label in (
            (stop_price, "stop_price"),
            (entry_anchor, "entry_anchor"),
            (initial_stop, "initial_stop_price"),
            (transition_start, "stop_transition_start"),
        ):
            if not _isfinite(val) or val < 0.0:
                raise ValueError(f"invalid {label} in state")

        max_stage = state["max_stage"]
        transition_count = state["stop_transition_fill_count"]
        if isinstance(max_stage, bool) or not isinstance(max_stage, int) or max_stage < 0:
            raise ValueError("invalid max_stage in state")
        if isinstance(transition_count, bool) or not isinstance(transition_count, int) or transition_count < 0:
            raise ValueError("invalid stop_transition_fill_count in state")

        pe_raw = state.get("pending_entry")
        pending_entry: _PendingSecondPullback | None = None
        if pe_raw is not None:
            pending_entry = _PendingSecondPullback(
                fib_level=float(pe_raw["fib_level"]),
                expires_bar_idx=int(pe_raw["expires_bar_idx"]),
                signal_stop=float(pe_raw["signal_stop"]),
            )

        pi_raw = state.get("pending_immediate")
        pending_immediate: _PendingImmediateEntry | None = None
        if pi_raw is not None:
            pending_immediate = _PendingImmediateEntry(
                mode=str(pi_raw["mode"]),
                signal_stop=float(pi_raw["signal_stop"]),
                trigger_high=float(pi_raw["trigger_high"]) if pi_raw.get("trigger_high") is not None else None,
                max_entry_price=(
                    float(pi_raw["max_entry_price"]) if pi_raw.get("max_entry_price") is not None else None
                ),
            )

        self._last_qty = 0.0 if last_qty == 0.0 else last_qty
        self._fills = fills
        self._stop_price = 0.0 if stop_price == 0.0 else stop_price
        self._entry_anchor = 0.0 if entry_anchor == 0.0 else entry_anchor
        self._initial_stop_price = 0.0 if initial_stop == 0.0 else initial_stop
        self._max_stage = max_stage
        self._stop_transition_fill_count = transition_count
        self._stop_transition_start = 0.0 if transition_start == 0.0 else transition_start
        self._pending_entry = pending_entry
        self._pending_immediate = pending_immediate
        self._submitted_entry_bar_idx = state.get("submitted_entry_bar_idx")
        self._pending_add_stage = state.get("pending_add_stage")
        self._last_bar_ms = state.get("last_bar_ms")
        self._clear_ephemeral_cache()
