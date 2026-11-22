"""Native long / short / long-short Donchian Breakout + ATR Trailing Stop strategy."""

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
    _fast_target_output,
)

__all__ = [
    "DonchianATRParams",
    "DonchianATRStrategy",
    "donchian_atr_param_space",
]

_isfinite = math.isfinite
_INF = math.inf


@dataclass(frozen=True, slots=True)
class DonchianATRParams:
    """Immutable parameters for `DonchianATRStrategy`."""

    interval: str = "1h"
    entry_period: int = 20
    exit_period: int = 10
    atr_period: int = 14
    atr_stop_mult: float = 2.5
    target_weight: float = 1.0
    side_mode: SideMode = "long_short"
    allow_flip: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.interval, str) or not self.interval.strip():
            raise ValueError("interval must be a non-empty string")
        if isinstance(self.entry_period, bool) or not isinstance(self.entry_period, int) or self.entry_period < 2:
            raise ValueError("entry_period must be an integer >= 2")
        if isinstance(self.exit_period, bool) or not isinstance(self.exit_period, int) or self.exit_period < 1:
            raise ValueError("exit_period must be an integer >= 1")
        if self.exit_period > self.entry_period:
            raise ValueError("exit_period must be <= entry_period")
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int) or self.atr_period < 1:
            raise ValueError("atr_period must be an integer >= 1")
        if (
            isinstance(self.atr_stop_mult, bool)
            or not isinstance(self.atr_stop_mult, (int, float))
            or not _isfinite(self.atr_stop_mult)
            or self.atr_stop_mult <= 0.0
        ):
            raise ValueError("atr_stop_mult must be finite and > 0")
        if (
            isinstance(self.target_weight, bool)
            or not isinstance(self.target_weight, (int, float))
            or not _isfinite(self.target_weight)
            or self.target_weight <= 0.0
        ):
            raise ValueError("target_weight must be finite and > 0")
        if self.side_mode not in SUPPORTED_SIDE_MODES:
            raise ValueError(f"unsupported side_mode: {self.side_mode!r}")
        if not isinstance(self.allow_flip, bool):
            raise ValueError("allow_flip must be a bool")


def _validate_donchian_atr_space(params: Mapping[str, Any]) -> None:
    entry_period = params.get("entry_period")
    exit_period = params.get("exit_period")
    if entry_period is not None and exit_period is not None and exit_period > entry_period:
        raise ValueError(f"exit_period ({exit_period}) must be <= entry_period ({entry_period})")


def donchian_atr_param_space() -> ParamSpace:
    return ParamSpace(
        (
            ParamSpec("interval", "categorical", "1h", choices=("15m", "1h", "4h", "1d")),
            ParamSpec("entry_period", "int", 20, min_value=10, max_value=120, step=5),
            ParamSpec("exit_period", "int", 10, min_value=5, max_value=60, step=5),
            ParamSpec("atr_period", "int", 14, min_value=7, max_value=42, step=7),
            ParamSpec("atr_stop_mult", "float", 2.5, min_value=1.0, max_value=5.0, step=0.5),
            ParamSpec("target_weight", "float", 1.0, min_value=0.25, max_value=3.0, step=0.25),
            ParamSpec("side_mode", "categorical", "long_short", choices=("buy", "sell", "long_short")),
            ParamSpec("allow_flip", "bool", True),
        ),
        constraints=(_validate_donchian_atr_space,),
    )


def _resolve_features(market: Any, fallback: FeatureCache) -> FeatureCache:
    fc = getattr(market, "features", None)
    if fc is None:
        fc = getattr(market, "feature_cache", None)
    return fc if fc is not None else fallback


class DonchianATRStrategy:
    """Donchian channel breakout with monotone stateful ATR trailing stop.

    Compares bar `t` close against the prior `entry_period`/`exit_period` Donchian
    channel (`offset=-2`) so breakout evaluation is strictly out-of-sample on bar `t`.
    Emits `TargetPosition` with explicit protective `stop_price` that ratchets
    favorably as price moves in the position's direction.
    """

    __slots__ = (
        "params",
        "required_intervals",
        "warmup_bars",
        "_fallback_features",
        "_position_dir",
        "_trailing_stop",
        "_highest_close",
        "_lowest_close",
        "_pending_initial_stop",
        "_last_bar_ms",
        "_last_bar_idx",
        "_interval",
        "_entry_period",
        "_exit_period",
        "_atr_period",
        "_atr_stop_mult",
        "_target_weight",
        "_allow_long",
        "_allow_short",
        "_allow_flip",
        "_min_bars",
        "_cached_base",
        "_cached_features",
        "_cached_gen",
        "_cached_closes",
        "_cached_close_times",
        "_cached_entry_high",
        "_cached_entry_low",
        "_cached_exit_high",
        "_cached_exit_low",
        "_cached_atr",
        "_fast_mkt",
        "_fast_pf",
        "_fast_acct",
        "_fast_sym",
        "_cached_hold_sym",
        "_cached_hold_dir",
        "_cached_hold_stop",
        "_cached_hold_out",
        "_cached_flat_outs",
    )

    strategy_id: str = "donchian_atr"
    version: str = "1.0.0"
    supported_sides: tuple[str, ...] = ("buy", "sell", "long_short")
    supports_ephemeral_context: bool = True

    def __init__(self, params: DonchianATRParams | None = None, **kwargs: Any) -> None:
        if params is not None and kwargs:
            raise ValueError("pass either params or keyword overrides, not both")
        self.params: DonchianATRParams = params if params is not None else DonchianATRParams(**kwargs)
        self.required_intervals: tuple[str, ...] = (self.params.interval,)
        warmup = max(self.params.entry_period + 1, self.params.exit_period + 1, self.params.atr_period)
        self.warmup_bars: Mapping[str, int] = MappingProxyType({self.params.interval: warmup})
        self._fallback_features = FeatureCache()

        self._position_dir: int = 0
        self._trailing_stop: float | None = None
        self._highest_close: float | None = None
        self._lowest_close: float | None = None
        self._pending_initial_stop: float | None = None
        self._last_bar_ms: int | None = None
        self._last_bar_idx: int = -1

        # Hot-path scalar caches from immutable params.
        p = self.params
        self._interval = p.interval
        self._entry_period = p.entry_period
        self._exit_period = p.exit_period
        self._atr_period = p.atr_period
        self._atr_stop_mult = p.atr_stop_mult
        self._target_weight = p.target_weight
        self._allow_long = p.side_mode != "sell"
        self._allow_short = p.side_mode != "buy"
        self._allow_flip = p.allow_flip
        self._min_bars = max(p.entry_period + 1, p.exit_period + 1, p.atr_period)

        # Ephemeral read-only caches (never serialized; cleared on reset/restore_state).
        self._cached_base: Any = None
        self._cached_features: Any = None
        self._cached_gen: int = -1
        self._cached_closes: Any = None
        self._cached_close_times: Any = None
        self._cached_entry_high: Any = None
        self._cached_entry_low: Any = None
        self._cached_exit_high: Any = None
        self._cached_exit_low: Any = None
        self._cached_atr: Any = None
        self._fast_mkt: Any = None
        self._fast_pf: Any = None
        self._fast_acct: Any = None
        self._fast_sym: str | None = None
        self._cached_hold_sym: str | None = None
        self._cached_hold_dir: int = 0
        self._cached_hold_stop: float = -1.0
        self._cached_hold_out: StrategyOutput | None = None
        self._cached_flat_outs: dict[tuple[str, str], StrategyOutput] = {}

    def param_space(self) -> ParamSpace:
        return donchian_atr_param_space()

    def _clear_ephemeral_cache(self) -> None:
        self._cached_base = None
        self._cached_features = None
        self._cached_gen = -1
        self._cached_closes = None
        self._cached_close_times = None
        self._cached_entry_high = None
        self._cached_entry_low = None
        self._cached_exit_high = None
        self._cached_exit_low = None
        self._cached_atr = None
        self._fast_mkt = None
        self._fast_pf = None
        self._fast_acct = None
        self._fast_sym = None
        self._cached_hold_sym = None
        self._cached_hold_dir = 0
        self._cached_hold_stop = -1.0
        self._cached_hold_out = None
        self._cached_flat_outs.clear()
        self._last_bar_idx = -1

    def reset(self) -> None:
        self._position_dir = 0
        self._trailing_stop = None
        self._highest_close = None
        self._lowest_close = None
        self._pending_initial_stop = None
        self._last_bar_ms = None
        self._clear_ephemeral_cache()

    def _clear_position_state(self) -> None:
        self._position_dir = 0
        self._trailing_stop = None
        self._highest_close = None
        self._lowest_close = None

    def _flat_output(self, symbol: str, reason: str) -> StrategyOutput:
        key = (symbol, reason)
        out = self._cached_flat_outs.get(key)
        if out is None:
            out = StrategyOutput(targets=(TargetPosition(symbol=symbol, target_weight=0.0, reason=reason),))
            self._cached_flat_outs[key] = out
        return out

    def on_bar(self, ctx: BarContext) -> StrategyOutput:
        market = ctx.market
        portfolio = ctx.portfolio
        symbol = ctx.symbol

        if (
            market is self._fast_mkt
            and portfolio is self._fast_pf
            and symbol == self._fast_sym
            and self._cached_features._generation == self._cached_gen
        ):
            acct = self._fast_acct
            last_idx = acct.bar_idx
            if last_idx + 1 < self._min_bars:
                self._last_bar_idx = last_idx
                return EMPTY_OUTPUT
            self._last_bar_idx = last_idx
            pos_qty = acct.pos_qty
        else:
            bars = market.closed_bars(symbol, self._interval)
            try:
                base = bars._series
                start = bars._start
                end = bars._end
            except AttributeError:
                base, start, end = _unwrap_series_bounds(bars)

            if end - start < self._min_bars:
                self._last_bar_ms = ctx.now_ms
                self._last_bar_idx = -1
                return EMPTY_OUTPUT

            try:
                features = market._features
            except AttributeError:
                features = getattr(market, "features", getattr(market, "feature_cache", None))
            if features is None:
                features = self._fallback_features
            feat_gen = features._generation

            if base is not self._cached_base or features is not self._cached_features or feat_gen != self._cached_gen:
                self._fast_mkt = None
                self._fast_pf = None
                self._fast_acct = None
                self._fast_sym = None
                self._cached_base = base
                self._cached_features = features
                self._cached_gen = feat_gen
                self._cached_closes = base.closes
                self._cached_close_times = base.close_times
                self._cached_entry_high = features._donchian_high_array(base, self._entry_period)
                self._cached_entry_low = features._donchian_low_array(base, self._entry_period)
                self._cached_exit_high = features._donchian_high_array(base, self._exit_period)
                self._cached_exit_low = features._donchian_low_array(base, self._exit_period)
                self._cached_atr = features._atr_array(base, self._atr_period)

            if start == 0:
                acct = getattr(market, "_acct", None)
                if (
                    acct is not None
                    and getattr(portfolio, "_acct", None) is acct
                    and getattr(market, "_p_sym", None) == symbol
                    and getattr(market, "_p_iv", None) == self._interval
                ):
                    self._fast_mkt = market
                    self._fast_pf = portfolio
                    self._fast_acct = acct
                    self._fast_sym = symbol

            last_idx = end - 1
            self._last_bar_idx = last_idx
            pos_qty = portfolio.position_qty(symbol)

        prev_idx = last_idx - 1
        curr_close = self._cached_closes[last_idx]
        if not (0.0 < curr_close < _INF):
            return EMPTY_OUTPUT

        # Fast flat path: only read exit/ATR when an entry breakout actually triggers.
        if pos_qty == 0.0:
            if self._position_dir != 0:
                self._clear_position_state()
            if self._allow_long:
                entry_high = self._cached_entry_high[prev_idx]
                if -_INF < entry_high < _INF and curr_close > entry_high:
                    atr_val = self._cached_atr[last_idx]
                    if 0.0 < atr_val < _INF:
                        stop_px = curr_close - (self._atr_stop_mult * atr_val)
                        if stop_px < 1e-12:
                            stop_px = 1e-12
                        self._pending_initial_stop = stop_px
                        return _fast_target_output(symbol, self._target_weight, stop_px, "donchian_breakout_long")
                    return EMPTY_OUTPUT
            if self._allow_short:
                entry_low = self._cached_entry_low[prev_idx]
                if -_INF < entry_low < _INF and curr_close < entry_low:
                    atr_val = self._cached_atr[last_idx]
                    if 0.0 < atr_val < _INF:
                        stop_px = curr_close + (self._atr_stop_mult * atr_val)
                        if 0.0 < stop_px < _INF:
                            self._pending_initial_stop = stop_px
                            return _fast_target_output(symbol, -self._target_weight, stop_px, "donchian_breakout_short")
                    self._pending_initial_stop = None
                    return EMPTY_OUTPUT
            self._pending_initial_stop = None
            return EMPTY_OUTPUT

        atr_val = self._cached_atr[last_idx]
        if not (0.0 < atr_val < _INF):
            return EMPTY_OUTPUT
        atr_dist = self._atr_stop_mult * atr_val

        # Synchronize stateful trailing stop with realized portfolio position.
        if pos_qty > 0.0:
            exit_low = self._cached_exit_low[prev_idx]
            if not (-_INF < exit_low < _INF):
                return EMPTY_OUTPUT
            if self._position_dir <= 0:
                pos = portfolio.position(symbol)
                avg_px = pos.avg_entry_price if pos is not None else 0.0
                anchor = avg_px if avg_px > curr_close else curr_close
                self._position_dir = 1
                self._highest_close = anchor
                self._lowest_close = None
                init_stop = anchor - atr_dist
                if init_stop < 1e-12:
                    init_stop = 1e-12
                pending_stop = self._pending_initial_stop
                if pending_stop is not None and pending_stop < curr_close and pending_stop > init_stop:
                    init_stop = pending_stop
                self._trailing_stop = init_stop
                self._pending_initial_stop = None
            else:
                self._pending_initial_stop = None

            highest = self._highest_close
            if highest is None or curr_close > highest:
                highest = curr_close
                self._highest_close = highest
            candidate_stop = highest - atr_dist
            if candidate_stop < 1e-12:
                candidate_stop = 1e-12
            stop = self._trailing_stop
            if stop is None or candidate_stop > stop:
                stop = candidate_stop
                self._trailing_stop = stop

            if self._allow_flip and self._allow_short:
                entry_low = self._cached_entry_low[prev_idx]
                if -_INF < entry_low < _INF and curr_close < entry_low:
                    short_stop = curr_close + atr_dist
                    if 0.0 < short_stop < _INF:
                        self._pending_initial_stop = short_stop
                        return _fast_target_output(symbol, -self._target_weight, short_stop, "donchian_flip_short")

            if curr_close < exit_low or curr_close <= stop:
                reason = "donchian_trailing_stop_long" if curr_close <= stop else "donchian_exit_long"
                self._pending_initial_stop = None
                return self._flat_output(symbol, reason)

            if self._cached_hold_stop == stop and self._cached_hold_dir == 1 and self._cached_hold_sym == symbol:
                return self._cached_hold_out  # type: ignore[return-value]
            out = _fast_target_output(symbol, self._target_weight, stop, "donchian_hold_long")
            self._cached_hold_sym = symbol
            self._cached_hold_dir = 1
            self._cached_hold_stop = stop
            self._cached_hold_out = out
            return out

        # pos_qty < 0.0
        exit_high = self._cached_exit_high[prev_idx]
        if not (-_INF < exit_high < _INF):
            return EMPTY_OUTPUT
        if self._position_dir >= 0:
            pos = portfolio.position(symbol)
            avg_px = pos.avg_entry_price if pos is not None else 0.0
            anchor = avg_px if (0.0 < avg_px < curr_close) else curr_close
            self._position_dir = -1
            self._lowest_close = anchor
            self._highest_close = None
            raw_init_stop = anchor + atr_dist
            init_stop_short: float | None = raw_init_stop if 0.0 < raw_init_stop < _INF else None
            pending_stop = self._pending_initial_stop
            if (
                pending_stop is not None
                and 0.0 < pending_stop < _INF
                and pending_stop > curr_close
                and (init_stop_short is None or pending_stop < init_stop_short)
            ):
                init_stop_short = pending_stop
            self._trailing_stop = init_stop_short
            self._pending_initial_stop = None
        else:
            self._pending_initial_stop = None

        lowest = self._lowest_close
        if lowest is None or curr_close < lowest:
            lowest = curr_close
            self._lowest_close = lowest
        candidate_stop = lowest + atr_dist
        stop = self._trailing_stop
        if 0.0 < candidate_stop < _INF and (stop is None or candidate_stop < stop):
            stop = candidate_stop
            self._trailing_stop = stop

        if self._allow_flip and self._allow_long:
            entry_high = self._cached_entry_high[prev_idx]
            if -_INF < entry_high < _INF and curr_close > entry_high:
                long_stop = curr_close - atr_dist
                if long_stop < 1e-12:
                    long_stop = 1e-12
                self._pending_initial_stop = long_stop
                return _fast_target_output(symbol, self._target_weight, long_stop, "donchian_flip_long")

        if curr_close > exit_high or (stop is not None and curr_close >= stop):
            reason = "donchian_trailing_stop_short" if (stop is not None and curr_close >= stop) else "donchian_exit_short"
            self._pending_initial_stop = None
            return self._flat_output(symbol, reason)

        if stop is None or not (0.0 < stop < _INF):
            return EMPTY_OUTPUT

        if self._cached_hold_stop == stop and self._cached_hold_dir == -1 and self._cached_hold_sym == symbol:
            return self._cached_hold_out  # type: ignore[return-value]
        out = _fast_target_output(symbol, -self._target_weight, stop, "donchian_hold_short")
        self._cached_hold_sym = symbol
        self._cached_hold_dir = -1
        self._cached_hold_stop = stop
        self._cached_hold_out = out
        return out

    def snapshot_state(self) -> dict[str, Any]:
        last_bar_ms = self._last_bar_ms
        if self._last_bar_idx >= 0 and self._cached_close_times is not None:
            last_bar_ms = int(self._cached_close_times[self._last_bar_idx])
        return {
            "schema_version": 1,
            "strategy_id": self.strategy_id,
            "position_dir": self._position_dir,
            "trailing_stop": self._trailing_stop,
            "highest_close": self._highest_close,
            "lowest_close": self._lowest_close,
            "pending_initial_stop": self._pending_initial_stop,
            "last_bar_ms": last_bar_ms,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema_version") != 1:
            raise ValueError(f"unsupported DonchianATRStrategy state version: {state.get('schema_version')!r}")
        pos_dir = state["position_dir"]
        if pos_dir not in (-1, 0, 1) or isinstance(pos_dir, bool):
            raise ValueError("invalid position_dir in state")

        def _opt_pos_float(key: str) -> float | None:
            val = state.get(key)
            if val is None:
                return None
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not _isfinite(val) or val <= 0.0:
                raise ValueError(f"invalid {key} in state")
            return float(val)

        trailing_stop = _opt_pos_float("trailing_stop")
        highest_close = _opt_pos_float("highest_close")
        lowest_close = _opt_pos_float("lowest_close")
        pending_initial_stop = _opt_pos_float("pending_initial_stop")

        last_bar_ms = state.get("last_bar_ms")
        if last_bar_ms is not None and (isinstance(last_bar_ms, bool) or not isinstance(last_bar_ms, int)):
            raise ValueError("invalid last_bar_ms in state")

        self._position_dir = int(pos_dir)
        self._trailing_stop = trailing_stop
        self._highest_close = highest_close
        self._lowest_close = lowest_close
        self._pending_initial_stop = pending_initial_stop
        self._last_bar_ms = last_bar_ms
        self._clear_ephemeral_cache()
