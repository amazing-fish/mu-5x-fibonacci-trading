"""Native long / short / long-short RSI + Z-Score Mean Reversion strategy."""

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
    "MeanReversionParams",
    "MeanReversionStrategy",
    "mean_reversion_param_space",
]

_isfinite = math.isfinite
_INF = math.inf


@dataclass(frozen=True, slots=True)
class MeanReversionParams:
    """Immutable parameters for `MeanReversionStrategy`."""

    interval: str = "1h"
    rsi_period: int = 14
    zscore_period: int = 20
    entry_z: float = 2.0
    exit_z: float = 0.25
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    max_ann_vol: float | None = 1.5
    vol_period: int = 20
    trend_ema_period: int | None = None
    stop_atr_mult: float | None = 3.0
    atr_period: int = 14
    max_hold_bars: int | None = None
    target_weight: float = 1.0
    side_mode: SideMode = "long_short"

    def __post_init__(self) -> None:
        if not isinstance(self.interval, str) or not self.interval.strip():
            raise ValueError("interval must be a non-empty string")
        if isinstance(self.rsi_period, bool) or not isinstance(self.rsi_period, int) or self.rsi_period < 2:
            raise ValueError("rsi_period must be an integer >= 2")
        if isinstance(self.zscore_period, bool) or not isinstance(self.zscore_period, int) or self.zscore_period < 2:
            raise ValueError("zscore_period must be an integer >= 2")
        if (
            isinstance(self.entry_z, bool)
            or not isinstance(self.entry_z, (int, float))
            or not _isfinite(self.entry_z)
            or self.entry_z <= 0.0
        ):
            raise ValueError("entry_z must be finite and > 0")
        if (
            isinstance(self.exit_z, bool)
            or not isinstance(self.exit_z, (int, float))
            or not _isfinite(self.exit_z)
            or self.exit_z < 0.0
            or self.exit_z >= self.entry_z
        ):
            raise ValueError("exit_z must be finite, >= 0, and < entry_z")
        if (
            isinstance(self.rsi_oversold, bool)
            or not isinstance(self.rsi_oversold, (int, float))
            or not _isfinite(self.rsi_oversold)
            or not (0.0 < self.rsi_oversold < 50.0)
        ):
            raise ValueError("rsi_oversold must be in (0, 50)")
        if (
            isinstance(self.rsi_overbought, bool)
            or not isinstance(self.rsi_overbought, (int, float))
            or not _isfinite(self.rsi_overbought)
            or not (50.0 < self.rsi_overbought < 100.0)
        ):
            raise ValueError("rsi_overbought must be in (50, 100)")
        if self.max_ann_vol is not None:
            if (
                isinstance(self.max_ann_vol, bool)
                or not isinstance(self.max_ann_vol, (int, float))
                or not _isfinite(self.max_ann_vol)
                or self.max_ann_vol <= 0.0
            ):
                raise ValueError("max_ann_vol must be finite and > 0")
        if isinstance(self.vol_period, bool) or not isinstance(self.vol_period, int) or self.vol_period < 2:
            raise ValueError("vol_period must be an integer >= 2")
        if self.trend_ema_period is not None:
            if (
                isinstance(self.trend_ema_period, bool)
                or not isinstance(self.trend_ema_period, int)
                or self.trend_ema_period < 2
            ):
                raise ValueError("trend_ema_period must be an integer >= 2")
        if self.stop_atr_mult is not None:
            if (
                isinstance(self.stop_atr_mult, bool)
                or not isinstance(self.stop_atr_mult, (int, float))
                or not _isfinite(self.stop_atr_mult)
                or self.stop_atr_mult <= 0.0
            ):
                raise ValueError("stop_atr_mult must be finite and > 0")
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int) or self.atr_period < 1:
            raise ValueError("atr_period must be an integer >= 1")
        if self.max_hold_bars is not None:
            if (
                isinstance(self.max_hold_bars, bool)
                or not isinstance(self.max_hold_bars, int)
                or self.max_hold_bars < 1
            ):
                raise ValueError("max_hold_bars must be an integer >= 1")
        if (
            isinstance(self.target_weight, bool)
            or not isinstance(self.target_weight, (int, float))
            or not _isfinite(self.target_weight)
            or self.target_weight <= 0.0
        ):
            raise ValueError("target_weight must be finite and > 0")
        if self.side_mode not in SUPPORTED_SIDE_MODES:
            raise ValueError(f"unsupported side_mode: {self.side_mode!r}")


def _validate_mean_reversion_space(params: Mapping[str, Any]) -> None:
    entry_z = params.get("entry_z")
    exit_z = params.get("exit_z")
    if entry_z is not None and exit_z is not None and exit_z >= entry_z:
        raise ValueError(f"exit_z ({exit_z}) must be < entry_z ({entry_z})")
    rsi_oversold = params.get("rsi_oversold")
    rsi_overbought = params.get("rsi_overbought")
    if rsi_oversold is not None and rsi_overbought is not None and rsi_oversold >= rsi_overbought:
        raise ValueError(f"rsi_oversold ({rsi_oversold}) must be < rsi_overbought ({rsi_overbought})")


def mean_reversion_param_space() -> ParamSpace:
    return ParamSpace(
        (
            ParamSpec("interval", "categorical", "1h", choices=("15m", "1h", "4h", "1d")),
            ParamSpec("rsi_period", "int", 14, min_value=2, max_value=28, step=2),
            ParamSpec("zscore_period", "int", 20, min_value=10, max_value=60, step=5),
            ParamSpec("entry_z", "float", 2.0, min_value=1.0, max_value=3.5, step=0.25),
            ParamSpec("exit_z", "float", 0.25, min_value=0.0, max_value=1.0, step=0.25),
            ParamSpec("rsi_oversold", "float", 35.0, min_value=15.0, max_value=45.0, step=5.0),
            ParamSpec("rsi_overbought", "float", 65.0, min_value=55.0, max_value=85.0, step=5.0),
            ParamSpec("max_ann_vol", "float", 1.5, min_value=0.4, max_value=3.0, step=0.2),
            ParamSpec("target_weight", "float", 1.0, min_value=0.25, max_value=3.0, step=0.25),
            ParamSpec("side_mode", "categorical", "long_short", choices=("buy", "sell", "long_short")),
        ),
        constraints=(_validate_mean_reversion_space,),
    )


def _resolve_features(market: Any, fallback: FeatureCache) -> FeatureCache:
    fc = getattr(market, "features", None)
    if fc is None:
        fc = getattr(market, "feature_cache", None)
    return fc if fc is not None else fallback


class MeanReversionStrategy:
    """RSI + Z-score mean-reversion strategy with optional trend & volatility gates.

    Enters long on oversold dislocation (`z <= -entry_z` and `rsi <= rsi_oversold`),
    enters short on overbought dislocation (`z >= +entry_z` and `rsi >= rsi_overbought`),
    and exits when price reverts inside `exit_z` (or optional time/ATR stop hits).
    """

    __slots__ = (
        "params",
        "required_intervals",
        "warmup_bars",
        "_fallback_features",
        "_position_dir",
        "_bars_in_position",
        "_entry_stop",
        "_pending_stop",
        "_last_bar_ms",
        "_last_bar_idx",
        "_interval",
        "_rsi_period",
        "_zscore_period",
        "_entry_z",
        "_exit_z",
        "_rsi_oversold",
        "_rsi_overbought",
        "_max_ann_vol",
        "_vol_period",
        "_trend_ema_period",
        "_stop_atr_mult",
        "_atr_period",
        "_max_hold_bars",
        "_target_weight",
        "_allow_long",
        "_allow_short",
        "_min_bars",
        "_cached_base",
        "_cached_features",
        "_cached_gen",
        "_cached_closes",
        "_cached_close_times",
        "_cached_zscore",
        "_cached_rsi",
        "_cached_vol",
        "_cached_ema",
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

    strategy_id: str = "mean_reversion"
    version: str = "1.0.0"
    supported_sides: tuple[str, ...] = ("buy", "sell", "long_short")
    supports_ephemeral_context: bool = True

    def __init__(self, params: MeanReversionParams | None = None, **kwargs: Any) -> None:
        if params is not None and kwargs:
            raise ValueError("pass either params or keyword overrides, not both")
        self.params: MeanReversionParams = params if params is not None else MeanReversionParams(**kwargs)
        self.required_intervals: tuple[str, ...] = (self.params.interval,)
        warmup = max(self.params.rsi_period + 1, self.params.zscore_period)
        if self.params.max_ann_vol is not None:
            warmup = max(warmup, self.params.vol_period + 1)
        if self.params.trend_ema_period is not None:
            warmup = max(warmup, self.params.trend_ema_period)
        if self.params.stop_atr_mult is not None:
            warmup = max(warmup, self.params.atr_period)
        self.warmup_bars: Mapping[str, int] = MappingProxyType({self.params.interval: warmup})
        self._fallback_features = FeatureCache()

        self._position_dir: int = 0
        self._bars_in_position: int = 0
        self._entry_stop: float | None = None
        self._pending_stop: float | None = None
        self._last_bar_ms: int | None = None
        self._last_bar_idx: int = -1

        # Hot-path scalar caches from immutable params.
        p = self.params
        self._interval = p.interval
        self._rsi_period = p.rsi_period
        self._zscore_period = p.zscore_period
        self._entry_z = p.entry_z
        self._exit_z = p.exit_z
        self._rsi_oversold = p.rsi_oversold
        self._rsi_overbought = p.rsi_overbought
        self._max_ann_vol = p.max_ann_vol
        self._vol_period = p.vol_period
        self._trend_ema_period = p.trend_ema_period
        self._stop_atr_mult = p.stop_atr_mult
        self._atr_period = p.atr_period
        self._max_hold_bars = p.max_hold_bars
        self._target_weight = p.target_weight
        self._allow_long = p.side_mode != "sell"
        self._allow_short = p.side_mode != "buy"
        self._min_bars = max(p.rsi_period + 1, p.zscore_period)

        # Ephemeral read-only caches (never serialized; cleared on reset/restore_state).
        self._cached_base: Any = None
        self._cached_features: Any = None
        self._cached_gen: int = -1
        self._cached_closes: Any = None
        self._cached_close_times: Any = None
        self._cached_zscore: Any = None
        self._cached_rsi: Any = None
        self._cached_vol: Any = None
        self._cached_ema: Any = None
        self._cached_atr: Any = None
        self._fast_mkt: Any = None
        self._fast_pf: Any = None
        self._fast_acct: Any = None
        self._fast_sym: str | None = None
        self._cached_hold_sym: str | None = None
        self._cached_hold_dir: int = 0
        self._cached_hold_stop: float | None = -1.0
        self._cached_hold_out: StrategyOutput | None = None
        self._cached_flat_outs: dict[tuple[str, str], StrategyOutput] = {}

    def param_space(self) -> ParamSpace:
        return mean_reversion_param_space()

    def _clear_ephemeral_cache(self) -> None:
        self._cached_base = None
        self._cached_features = None
        self._cached_gen = -1
        self._cached_closes = None
        self._cached_close_times = None
        self._cached_zscore = None
        self._cached_rsi = None
        self._cached_vol = None
        self._cached_ema = None
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
        self._bars_in_position = 0
        self._entry_stop = None
        self._pending_stop = None
        self._last_bar_ms = None
        self._clear_ephemeral_cache()

    def _clear_position(self) -> None:
        self._position_dir = 0
        self._bars_in_position = 0
        self._entry_stop = None

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
            count = last_idx + 1
            if count < self._min_bars:
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

            count = end - start
            if count < self._min_bars:
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
                zscore_arr = features._zscore_array(base, self._zscore_period)
                rsi_arr = features._rsi_array(base, self._rsi_period)
                vol_arr = (
                    features._realized_vol_array(base, self._vol_period, None)
                    if self._max_ann_vol is not None
                    else None
                )
                ema_arr = (
                    features._ema_array(base, self._trend_ema_period)
                    if self._trend_ema_period is not None
                    else None
                )
                atr_arr = (
                    features._atr_array(base, self._atr_period)
                    if self._stop_atr_mult is not None
                    else None
                )
                self._cached_base = base
                self._cached_features = features
                self._cached_gen = feat_gen
                self._cached_closes = base.closes
                self._cached_close_times = base.close_times
                self._cached_zscore = zscore_arr
                self._cached_rsi = rsi_arr
                self._cached_vol = vol_arr
                self._cached_ema = ema_arr
                self._cached_atr = atr_arr

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

        z = self._cached_zscore[last_idx]

        # Fast flat reject: skip RSI/close/vol reads on ~90% of flat bars where |z| < entry_z.
        if pos_qty == 0.0:
            if self._position_dir != 0:
                self._clear_position()
            entry_z = self._entry_z
            if not (-_INF < z < _INF) or (-entry_z < z < entry_z):
                self._pending_stop = None
                return EMPTY_OUTPUT
            rsi_val = self._cached_rsi[last_idx]
            if not (-_INF < rsi_val < _INF):
                return EMPTY_OUTPUT
            curr_close = self._cached_closes[last_idx]
            if not (0.0 < curr_close < _INF):
                return EMPTY_OUTPUT
            long_signal = self._allow_long and z <= -entry_z and rsi_val <= self._rsi_oversold
            short_signal = (not long_signal) and self._allow_short and z >= entry_z and rsi_val >= self._rsi_overbought
            if not (long_signal or short_signal):
                self._pending_stop = None
                return EMPTY_OUTPUT
        else:
            if not (-_INF < z < _INF):
                return EMPTY_OUTPUT
            curr_close = self._cached_closes[last_idx]
            if not (0.0 < curr_close < _INF):
                return EMPTY_OUTPUT

            if pos_qty > 0.0:
                if self._position_dir <= 0:
                    self._position_dir = 1
                    self._bars_in_position = 1
                    self._entry_stop = self._pending_stop
                    self._pending_stop = None
                else:
                    self._bars_in_position += 1

                max_hold = self._max_hold_bars
                time_stop = max_hold is not None and self._bars_in_position >= max_hold
                if z >= -self._exit_z or time_stop:
                    reason = "mr_time_stop_long" if time_stop and z < -self._exit_z else "mr_reverted_long"
                    self._clear_position()
                    self._pending_stop = None
                    return self._flat_output(symbol, reason)
                stop = self._entry_stop
                if self._cached_hold_stop == stop and self._cached_hold_dir == 1 and self._cached_hold_sym == symbol:
                    return self._cached_hold_out  # type: ignore[return-value]
                out = _fast_target_output(symbol, self._target_weight, stop, "mr_hold_long")
                self._cached_hold_sym = symbol
                self._cached_hold_dir = 1
                self._cached_hold_stop = stop
                self._cached_hold_out = out
                return out

            # pos_qty < 0.0
            if self._position_dir >= 0:
                self._position_dir = -1
                self._bars_in_position = 1
                self._entry_stop = self._pending_stop
                self._pending_stop = None
            else:
                self._bars_in_position += 1

            max_hold = self._max_hold_bars
            time_stop = max_hold is not None and self._bars_in_position >= max_hold
            if z <= self._exit_z or time_stop:
                reason = "mr_time_stop_short" if time_stop and z > self._exit_z else "mr_reverted_short"
                self._clear_position()
                self._pending_stop = None
                return self._flat_output(symbol, reason)
            stop = self._entry_stop
            if self._cached_hold_stop == stop and self._cached_hold_dir == -1 and self._cached_hold_sym == symbol:
                return self._cached_hold_out  # type: ignore[return-value]
            out = _fast_target_output(symbol, -self._target_weight, stop, "mr_hold_short")
            self._cached_hold_sym = symbol
            self._cached_hold_dir = -1
            self._cached_hold_stop = stop
            self._cached_hold_out = out
            return out

        # Check optional volatility gate.
        max_vol = self._max_ann_vol
        if max_vol is not None:
            if count <= self._vol_period:
                return EMPTY_OUTPUT
            vol = self._cached_vol[last_idx]
            if not (-_INF < vol < _INF) or vol > max_vol:
                self._pending_stop = None
                return EMPTY_OUTPUT

        # Check optional trend EMA gate.
        if self._trend_ema_period is not None:
            trend_ema = self._cached_ema[last_idx]
            if not (-_INF < trend_ema < _INF):
                return EMPTY_OUTPUT
            if long_signal and curr_close < trend_ema:
                self._pending_stop = None
                return EMPTY_OUTPUT
            if short_signal and curr_close > trend_ema:
                self._pending_stop = None
                return EMPTY_OUTPUT

        atr_dist: float | None = None
        stop_mult = self._stop_atr_mult
        if stop_mult is not None:
            if count < self._atr_period:
                self._pending_stop = None
                return EMPTY_OUTPUT
            atr_val = self._cached_atr[last_idx]
            if not (0.0 < atr_val < _INF):
                self._pending_stop = None
                return EMPTY_OUTPUT
            atr_dist = stop_mult * atr_val

        if long_signal:
            if atr_dist is not None:
                sp = curr_close - atr_dist
                stop_px: float | None = sp if sp > 1e-12 else 1e-12
            else:
                stop_px = None
            self._pending_stop = stop_px
            return _fast_target_output(symbol, self._target_weight, stop_px, "mr_entry_long")

        if atr_dist is not None:
            stop_px = curr_close + atr_dist
            if not (0.0 < stop_px < _INF):
                self._pending_stop = None
                return EMPTY_OUTPUT
        else:
            stop_px = None
        self._pending_stop = stop_px
        return _fast_target_output(symbol, -self._target_weight, stop_px, "mr_entry_short")

    def snapshot_state(self) -> dict[str, Any]:
        last_bar_ms = self._last_bar_ms
        if self._last_bar_idx >= 0 and self._cached_close_times is not None:
            last_bar_ms = int(self._cached_close_times[self._last_bar_idx])
        return {
            "schema_version": 1,
            "strategy_id": self.strategy_id,
            "position_dir": self._position_dir,
            "bars_in_position": self._bars_in_position,
            "entry_stop": self._entry_stop,
            "pending_stop": self._pending_stop,
            "last_bar_ms": last_bar_ms,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema_version") != 1:
            raise ValueError(f"unsupported MeanReversionStrategy state version: {state.get('schema_version')!r}")
        pos_dir = state["position_dir"]
        if pos_dir not in (-1, 0, 1) or isinstance(pos_dir, bool):
            raise ValueError("invalid position_dir in state")
        bars_in_pos = state["bars_in_position"]
        if isinstance(bars_in_pos, bool) or not isinstance(bars_in_pos, int) or bars_in_pos < 0:
            raise ValueError("invalid bars_in_position in state")

        def _opt_pos_float(key: str) -> float | None:
            val = state.get(key)
            if val is None:
                return None
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not _isfinite(val) or val <= 0.0:
                raise ValueError(f"invalid {key} in state")
            return float(val)

        entry_stop = _opt_pos_float("entry_stop")
        pending_stop = _opt_pos_float("pending_stop")

        last_bar_ms = state.get("last_bar_ms")
        if last_bar_ms is not None and (isinstance(last_bar_ms, bool) or not isinstance(last_bar_ms, int)):
            raise ValueError("invalid last_bar_ms in state")

        self._position_dir = int(pos_dir)
        self._bars_in_position = bars_in_pos
        self._entry_stop = entry_stop
        self._pending_stop = pending_stop
        self._last_bar_ms = last_bar_ms
        self._clear_ephemeral_cache()
