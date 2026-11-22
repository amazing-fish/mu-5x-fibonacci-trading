"""Native long / short / long-short Time-Series Momentum strategy family."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from mu_strategy.strategies.features import FeatureCache, _unwrap_series_bounds
from mu_strategy.strategies.protocol import (
    EMPTY_OUTPUT,
    SUPPORTED_SIDE_MODES,
    BarContext,
    ParamSpace,
    ParamSpec,
    SideMode,
    StrategyOutput,
    TargetPosition,
)

__all__ = [
    "TSMomentumParams",
    "TSMomentumStrategy",
    "ts_momentum_param_space",
]

_isfinite = math.isfinite
_INF = math.inf


@dataclass(frozen=True, slots=True)
class TSMomentumParams:
    """Immutable parameters for `TSMomentumStrategy`."""

    interval: str = "1h"
    lookback_bars: int = 96
    deadband_return: float = 0.0
    target_weight: float = 1.0
    side_mode: SideMode = "long_short"
    use_deadband_hysteresis: bool = False
    vol_filter_max_ann_vol: float | None = None
    vol_period: int = 20
    stop_atr_mult: float | None = None
    atr_period: int = 14

    def __post_init__(self) -> None:
        if not isinstance(self.interval, str) or not self.interval.strip():
            raise ValueError("interval must be a non-empty string")
        if isinstance(self.lookback_bars, bool) or not isinstance(self.lookback_bars, int) or self.lookback_bars < 1:
            raise ValueError("lookback_bars must be an integer >= 1")
        if (
            isinstance(self.deadband_return, bool)
            or not isinstance(self.deadband_return, (int, float))
            or not _isfinite(self.deadband_return)
            or self.deadband_return < 0.0
        ):
            raise ValueError("deadband_return must be finite and >= 0")
        if (
            isinstance(self.target_weight, bool)
            or not isinstance(self.target_weight, (int, float))
            or not _isfinite(self.target_weight)
            or self.target_weight <= 0.0
        ):
            raise ValueError("target_weight must be finite and > 0")
        if self.side_mode not in SUPPORTED_SIDE_MODES:
            raise ValueError(f"unsupported side_mode: {self.side_mode!r}")
        if not isinstance(self.use_deadband_hysteresis, bool):
            raise ValueError("use_deadband_hysteresis must be a bool")
        if self.vol_filter_max_ann_vol is not None:
            if (
                isinstance(self.vol_filter_max_ann_vol, bool)
                or not isinstance(self.vol_filter_max_ann_vol, (int, float))
                or not _isfinite(self.vol_filter_max_ann_vol)
                or self.vol_filter_max_ann_vol <= 0.0
            ):
                raise ValueError("vol_filter_max_ann_vol must be positive and finite")
        if isinstance(self.vol_period, bool) or not isinstance(self.vol_period, int) or self.vol_period < 2:
            raise ValueError("vol_period must be an integer >= 2")
        if self.stop_atr_mult is not None:
            if (
                isinstance(self.stop_atr_mult, bool)
                or not isinstance(self.stop_atr_mult, (int, float))
                or not _isfinite(self.stop_atr_mult)
                or self.stop_atr_mult <= 0.0
            ):
                raise ValueError("stop_atr_mult must be positive and finite")
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int) or self.atr_period < 1:
            raise ValueError("atr_period must be an integer >= 1")


def ts_momentum_param_space() -> ParamSpace:
    return ParamSpace(
        (
            ParamSpec("interval", "categorical", "1h", choices=("15m", "1h", "4h", "1d")),
            ParamSpec("lookback_bars", "int", 96, min_value=12, max_value=720, step=12),
            ParamSpec("deadband_return", "float", 0.0, min_value=0.0, max_value=0.10, step=0.005),
            ParamSpec("target_weight", "float", 1.0, min_value=0.25, max_value=3.0, step=0.25),
            ParamSpec("side_mode", "categorical", "long_short", choices=("buy", "sell", "long_short")),
            ParamSpec("use_deadband_hysteresis", "bool", False),
        )
    )


def _resolve_features(market: Any, fallback: FeatureCache) -> FeatureCache:
    fc = getattr(market, "features", None)
    if fc is None:
        fc = getattr(market, "feature_cache", None)
    return fc if fc is not None else fallback


class TSMomentumStrategy:
    """Time-series momentum strategy emitting signed `TargetPosition` targets.

    Computes causal trailing return `(close[t] / close[t - lookback_bars]) - 1`
    with optional neutral deadband, hysteresis, annualized vol gate, and ATR stop.
    """

    __slots__ = (
        "params",
        "required_intervals",
        "warmup_bars",
        "_fallback_features",
        "_last_direction",
        "_last_target_weight",
        "_last_bar_ms",
        "_last_bar_idx",
        "_bars_seen",
        "_interval",
        "_lookback",
        "_deadband",
        "_target_weight",
        "_side_mode",
        "_use_hysteresis",
        "_vol_max",
        "_vol_period",
        "_stop_atr_mult",
        "_atr_period",
        "_simple_ls",
        "_cached_base",
        "_cached_start",
        "_cached_features",
        "_cached_gen",
        "_cached_closes",
        "_cached_close_times",
        "_cached_vol",
        "_cached_atr",
        "_fast_ctx",
        "_fast_acct",
        "_out_sym",
        "_out_long",
        "_out_short",
        "_out_flat",
    )

    strategy_id: str = "ts_momentum"
    version: str = "1.0.0"
    supported_sides: tuple[str, ...] = ("buy", "sell", "long_short")

    def __init__(self, params: TSMomentumParams | None = None, **kwargs: Any) -> None:
        if params is not None and kwargs:
            raise ValueError("pass either params or keyword overrides, not both")
        self.params: TSMomentumParams = params if params is not None else TSMomentumParams(**kwargs)
        self.required_intervals: tuple[str, ...] = (self.params.interval,)
        warmup = self.params.lookback_bars + 1
        if self.params.vol_filter_max_ann_vol is not None:
            warmup = max(warmup, self.params.vol_period + 1)
        if self.params.stop_atr_mult is not None:
            warmup = max(warmup, self.params.atr_period)
        self.warmup_bars: Mapping[str, int] = MappingProxyType({self.params.interval: warmup})
        self._fallback_features = FeatureCache()
        self._last_direction: int = 0
        self._last_target_weight: float = 0.0
        self._last_bar_ms: int | None = None
        self._last_bar_idx: int = -1
        self._bars_seen: int = 0

        # Hot-path scalar caches from immutable params.
        p = self.params
        self._interval = p.interval
        self._lookback = p.lookback_bars
        self._deadband = p.deadband_return
        self._target_weight = p.target_weight
        self._side_mode = p.side_mode
        self._use_hysteresis = p.use_deadband_hysteresis
        self._vol_max = p.vol_filter_max_ann_vol
        self._vol_period = p.vol_period
        self._stop_atr_mult = p.stop_atr_mult
        self._atr_period = p.atr_period
        self._simple_ls: bool = (
            p.deadband_return == 0.0
            and p.vol_filter_max_ann_vol is None
            and p.stop_atr_mult is None
            and p.side_mode == "long_short"
        )

        # Ephemeral read-only caches (never serialized; cleared on reset/restore_state).
        self._cached_base: Any = None
        self._cached_start: int = 0
        self._cached_features: Any = None
        self._cached_gen: int = -1
        self._cached_closes: Any = None
        self._cached_close_times: Any = None
        self._cached_vol: Any = None
        self._cached_atr: Any = None
        self._fast_ctx: Any = None
        self._fast_acct: Any = None
        self._out_sym: str | None = None
        self._out_long: StrategyOutput | None = None
        self._out_short: StrategyOutput | None = None
        self._out_flat: StrategyOutput | None = None

    def param_space(self) -> ParamSpace:
        return ts_momentum_param_space()

    def _clear_ephemeral_cache(self) -> None:
        self._cached_base = None
        self._cached_start = 0
        self._cached_features = None
        self._cached_gen = -1
        self._cached_closes = None
        self._cached_close_times = None
        self._cached_vol = None
        self._cached_atr = None
        self._fast_ctx = None
        self._fast_acct = None
        self._out_sym = None
        self._out_long = None
        self._out_short = None
        self._out_flat = None
        self._last_bar_idx = -1

    def reset(self) -> None:
        self._last_direction = 0
        self._last_target_weight = 0.0
        self._last_bar_ms = None
        self._bars_seen = 0
        self._clear_ephemeral_cache()

    def on_bar(self, ctx: BarContext) -> StrategyOutput:
        # Ultra-fast strictly causal duck-typed cursor path for standard long/short TSMOM.
        # `_fast_ctx` is only bound for a validated `BarSeries` with `start == 0` (finite positive closes).
        if ctx is self._fast_ctx:
            last_idx = self._fast_acct.bar_idx
            lookback = self._lookback
            if last_idx >= lookback:
                closes = self._cached_closes
                curr_close = closes[last_idx]
                past_close = closes[last_idx - lookback]
                if curr_close > past_close:
                    self._bars_seen += 1
                    self._last_bar_idx = last_idx
                    return self._out_long  # type: ignore[return-value]
                if curr_close < past_close:
                    self._bars_seen += 1
                    self._last_bar_idx = last_idx
                    return self._out_short  # type: ignore[return-value]
            else:
                self._bars_seen += 1
                self._last_bar_idx = last_idx
                return EMPTY_OUTPUT

        market = ctx.market
        symbol = ctx.symbol
        bars = market.closed_bars(symbol, self._interval)
        try:
            base = bars._series
            start = bars._start
            end = bars._end
        except AttributeError:
            base, start, end = _unwrap_series_bounds(bars)

        self._bars_seen += 1
        lookback = self._lookback

        # Fast path for standard long/short TSMOM on non-live immutable MarketView.
        if self._simple_ls and base is self._cached_base and symbol == self._out_sym:
            if end - start <= lookback:
                self._last_bar_idx = end - 1 if end > start else -1
                return EMPTY_OUTPUT
            last_idx = end - 1
            self._last_bar_idx = last_idx
            closes = self._cached_closes
            curr_close = closes[last_idx]
            past_close = closes[last_idx - lookback]
            if 0.0 < past_close < _INF and 0.0 < curr_close < _INF:
                if curr_close > past_close:
                    self._last_direction = 1
                    self._last_target_weight = self._target_weight
                    return self._out_long  # type: ignore[return-value]
                if curr_close < past_close:
                    self._last_direction = -1
                    self._last_target_weight = -self._target_weight
                    return self._out_short  # type: ignore[return-value]

        if end - start <= lookback:
            self._last_bar_ms = ctx.now_ms
            self._last_bar_idx = -1
            return EMPTY_OUTPUT

        vol_max = self._vol_max
        stop_mult = self._stop_atr_mult
        if vol_max is None and stop_mult is None:
            if base is not self._cached_base:
                self._cached_base = base
                self._cached_start = start
                self._cached_closes = base.closes
                self._cached_close_times = base.close_times
        else:
            try:
                features = market._features
            except AttributeError:
                features = getattr(market, "features", getattr(market, "feature_cache", None))
            if features is None:
                features = self._fallback_features
            feat_gen = features._generation
            if base is not self._cached_base or features is not self._cached_features or feat_gen != self._cached_gen:
                self._cached_base = base
                self._cached_start = start
                self._cached_features = features
                self._cached_gen = feat_gen
                self._cached_closes = base.closes
                self._cached_close_times = base.close_times
                self._cached_vol = (
                    features._realized_vol_array(base, self._vol_period, None)
                    if vol_max is not None
                    else None
                )
                self._cached_atr = (
                    features._atr_array(base, self._atr_period)
                    if stop_mult is not None
                    else None
                )

        if self._simple_ls and start == 0 and ctx is not self._fast_ctx:
            acct = getattr(market, "_acct", None)
            if acct is not None and getattr(market, "_p_sym", None) == symbol and getattr(market, "_p_iv", None) == self._interval:
                if symbol != self._out_sym:
                    w = self._target_weight
                    self._out_sym = symbol
                    self._out_long = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=w, reason="tsmom_long"),))
                    self._out_short = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=-w, reason="tsmom_short"),))
                    self._out_flat = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=0.0, reason="tsmom_flat"),))
                self._fast_ctx = ctx
                self._fast_acct = acct

        closes = self._cached_closes
        last_idx = end - 1
        self._last_bar_idx = last_idx
        curr_close = closes[last_idx]
        past_close = closes[last_idx - lookback]
        if not (0.0 < curr_close < _INF and 0.0 < past_close < _INF):
            return EMPTY_OUTPUT

        deadband = self._deadband
        if deadband == 0.0:
            if curr_close > past_close:
                desired_dir = 1
            elif curr_close < past_close:
                desired_dir = -1
            else:
                desired_dir = self._last_direction if self._use_hysteresis else 0
        else:
            momentum_ret = (curr_close / past_close) - 1.0
            if momentum_ret > deadband:
                desired_dir = 1
            elif momentum_ret < -deadband:
                desired_dir = -1
            else:
                desired_dir = self._last_direction if self._use_hysteresis else 0

        side_mode = self._side_mode
        if side_mode == "buy":
            if desired_dir < 0:
                desired_dir = 0
        elif side_mode == "sell":
            if desired_dir > 0:
                desired_dir = 0

        if desired_dir != 0 and vol_max is not None:
            vol = self._cached_vol[last_idx]
            if not (-_INF < vol < _INF) or vol > vol_max:
                desired_dir = 0

        if desired_dir == 0:
            prev_target = self._last_target_weight
            self._last_direction = 0
            self._last_target_weight = 0.0
            if prev_target == 0.0 and ctx.portfolio.position_qty(symbol) == 0.0:
                return EMPTY_OUTPUT
            if symbol != self._out_sym:
                w = self._target_weight
                self._out_sym = symbol
                self._out_long = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=w, reason="tsmom_long"),))
                self._out_short = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=-w, reason="tsmom_short"),))
                self._out_flat = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=0.0, reason="tsmom_flat"),))
            return self._out_flat  # type: ignore[return-value]

        desired_weight = self._target_weight if desired_dir > 0 else -self._target_weight
        self._last_direction = desired_dir
        self._last_target_weight = desired_weight

        if stop_mult is None:
            if symbol != self._out_sym:
                w = self._target_weight
                self._out_sym = symbol
                self._out_long = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=w, reason="tsmom_long"),))
                self._out_short = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=-w, reason="tsmom_short"),))
                self._out_flat = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=0.0, reason="tsmom_flat"),))
            return self._out_long if desired_dir > 0 else self._out_short  # type: ignore[return-value]

        atr_val = self._cached_atr[last_idx]
        stop_price: float | None = None
        if 0.0 < atr_val < _INF:
            if desired_dir > 0:
                sp = curr_close - (stop_mult * atr_val)
                stop_price = sp if sp > 1e-12 else 1e-12
            else:
                stop_price = curr_close + (stop_mult * atr_val)

        reason = "tsmom_long" if desired_dir > 0 else "tsmom_short"
        return StrategyOutput(
            targets=(
                TargetPosition(
                    symbol=symbol,
                    target_weight=desired_weight,
                    stop_price=stop_price,
                    reason=reason,
                ),
            ),
        )

    def snapshot_state(self) -> dict[str, Any]:
        last_bar_ms = self._last_bar_ms
        last_direction = self._last_direction
        last_target_weight = self._last_target_weight
        last_idx = self._last_bar_idx
        if last_idx >= 0 and self._cached_close_times is not None:
            last_bar_ms = int(self._cached_close_times[last_idx])
            if self._simple_ls and last_idx >= self._lookback and self._cached_closes is not None:
                curr_close = self._cached_closes[last_idx]
                past_close = self._cached_closes[last_idx - self._lookback]
                if 0.0 < past_close < _INF and 0.0 < curr_close < _INF:
                    if curr_close > past_close:
                        last_direction = 1
                        last_target_weight = self._target_weight
                    elif curr_close < past_close:
                        last_direction = -1
                        last_target_weight = -self._target_weight
        return {
            "schema_version": 1,
            "strategy_id": self.strategy_id,
            "last_direction": last_direction,
            "last_target_weight": last_target_weight,
            "last_bar_ms": last_bar_ms,
            "bars_seen": self._bars_seen,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema_version") != 1:
            raise ValueError(f"unsupported TSMomentumStrategy state version: {state.get('schema_version')!r}")
        direction = state["last_direction"]
        if direction not in (-1, 0, 1) or isinstance(direction, bool):
            raise ValueError("invalid last_direction in state")
        weight = float(state["last_target_weight"])
        if not _isfinite(weight):
            raise ValueError("invalid last_target_weight in state")
        last_bar_ms = state.get("last_bar_ms")
        if last_bar_ms is not None and (isinstance(last_bar_ms, bool) or not isinstance(last_bar_ms, int)):
            raise ValueError("invalid last_bar_ms in state")
        bars_seen = state.get("bars_seen", 0)
        if isinstance(bars_seen, bool) or not isinstance(bars_seen, int) or bars_seen < 0:
            raise ValueError("invalid bars_seen in state")

        self._last_direction = int(direction)
        self._last_target_weight = 0.0 if weight == 0.0 else weight
        self._last_bar_ms = last_bar_ms
        self._bars_seen = bars_seen
        self._clear_ephemeral_cache()
