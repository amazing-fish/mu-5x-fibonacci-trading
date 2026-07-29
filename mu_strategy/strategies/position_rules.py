"""Pure position-management rules shared by backtest and non-backtest callers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mu_strategy.models import Candle
from mu_strategy.strategy import StrategyConfig, is_preferred_us_cash_window, recent_higher_low

__all__ = [
    "PositionFillSnapshot",
    "PositionStateSnapshot",
    "PyramidAddDecision",
    "apply_stop_transition_curve",
    "baseline_stop_target",
    "decide_pyramid_add",
    "interpolate",
    "previous_baseline_stop_target",
    "resolved_stop_tightening",
    "stop_transition_progress",
    "tighten_stop",
    "tighten_stop_baseline",
    "tighten_stop_delayed_baseline",
    "tighten_stop_green_wide",
    "tighten_stop_half_protect",
]


@dataclass(frozen=True)
class PositionFillSnapshot:
    time_ms: int
    price: float
    units: float


@dataclass(frozen=True)
class PositionStateSnapshot:
    """Immutable inputs for one exit or pyramid-add evaluation.

    The transition fields identify the stop anchor for the current fill count.
    A caller that performs an add owns advancing that bookkeeping before its
    next delayed-stop evaluation.
    """

    fills: tuple[PositionFillSnapshot, ...]
    stop_price: float
    entry_anchor: float
    initial_stop_price: float = 0.0
    max_stage: int = 1
    stop_transition_fill_count: int = 0
    stop_transition_start: float = 0.0

    @property
    def entry_price(self) -> float:
        units = sum(fill.units for fill in self.fills)
        if not units:
            return 0.0
        return sum(fill.price * fill.units for fill in self.fills) / units


@dataclass(frozen=True)
class PyramidAddDecision:
    should_add: bool
    stage: int | None = None
    fill_price: float | None = None
    margin_fraction: float | None = None

    def __post_init__(self) -> None:
        details = (self.stage, self.fill_price, self.margin_fraction)
        if self.should_add and any(value is None for value in details):
            raise ValueError("add decision requires stage, fill_price, and margin_fraction")
        if not self.should_add and any(value is not None for value in details):
            raise ValueError("no-add decision cannot contain fill details")


def decide_pyramid_add(
    position: PositionStateSnapshot,
    candle: Candle,
    *,
    rsi_value: float,
    macd_hist: float,
    previous_macd_hist: float,
    regime: str,
    config: StrategyConfig,
) -> PyramidAddDecision:
    next_stage = position.max_stage + 1
    if next_stage > len(config.margin_steps):
        return PyramidAddDecision(False)
    if not is_preferred_us_cash_window(candle.open_time_ms, config):
        return PyramidAddDecision(False)

    threshold = config.add_thresholds[next_stage - 2]
    trigger_price = position.entry_anchor * (1 + threshold)
    fill_price = _buy_stop_fill_price(candle, trigger_price)
    if fill_price is None:
        return PyramidAddDecision(False)
    if rsi_value < config.rsi_add_floor or macd_hist < previous_macd_hist:
        return PyramidAddDecision(False)
    if next_stage >= 3 and regime != config.full_size_regime:
        return PyramidAddDecision(False)

    return PyramidAddDecision(
        should_add=True,
        stage=next_stage,
        fill_price=fill_price,
        margin_fraction=config.margin_steps[next_stage - 1],
    )


def _buy_stop_fill_price(candle: Candle, trigger_price: float) -> float | None:
    if candle.high < trigger_price:
        return None
    if candle.open > trigger_price:
        return candle.open
    return trigger_price


def tighten_stop(
    position: PositionStateSnapshot,
    candle: Candle,
    *,
    index: int,
    candles: Sequence[Candle],
    regime: str,
    config: StrategyConfig,
) -> float:
    if index < 0 or index >= len(candles) or candles[index].open_time_ms != candle.open_time_ms:
        raise ValueError("current candle must match candles[index]")
    initial_stop = position.initial_stop_price or position.stop_price
    first_entry = position.fills[0].price
    mode = resolved_stop_tightening(regime, config)

    if mode == "baseline":
        return tighten_stop_baseline(position, index=index, candles=candles, config=config)
    if mode == "half_protect":
        return tighten_stop_half_protect(
            position,
            index=index,
            candles=candles,
            config=config,
            initial_stop=initial_stop,
            first_entry=first_entry,
        )
    if mode == "wide":
        return tighten_stop_green_wide(
            position,
            index=index,
            candles=candles,
            config=config,
            initial_stop=initial_stop,
            first_entry=first_entry,
        )
    if mode == "delayed_baseline":
        return tighten_stop_delayed_baseline(position, index=index, candles=candles, config=config)
    raise ValueError(f"unsupported stop_tightening: {mode}")


def resolved_stop_tightening(regime: str, config: StrategyConfig) -> str:
    if regime == "yellow" and config.yellow_stop_tightening is not None:
        return config.yellow_stop_tightening
    if regime == "green" and config.green_stop_tightening is not None:
        return config.green_stop_tightening

    if config.stop_tightening == "green_wide":
        return "wide" if regime == "green" else "baseline"
    if config.stop_tightening == "half_protect_green_wide":
        return "wide" if regime == "green" else "half_protect"
    return config.stop_tightening


def tighten_stop_baseline(
    position: PositionStateSnapshot,
    *,
    index: int,
    candles: Sequence[Candle],
    config: StrategyConfig,
) -> float:
    stop_price = position.stop_price
    if position.max_stage >= 2:
        stop_price = max(stop_price, position.fills[0].price)
    if position.max_stage >= 3:
        stop_price = max(stop_price, position.entry_price)
    if position.max_stage >= 4:
        higher_low = recent_higher_low(candles, index)
        if higher_low:
            stop_price = max(stop_price, higher_low * (1 - config.stop_buffer_pct))
    return stop_price


def tighten_stop_half_protect(
    position: PositionStateSnapshot,
    *,
    index: int,
    candles: Sequence[Candle],
    config: StrategyConfig,
    initial_stop: float,
    first_entry: float,
) -> float:
    stop_price = position.stop_price
    if position.max_stage >= 2:
        stop_price = max(stop_price, (initial_stop + first_entry) / 2)
    if position.max_stage >= 3:
        stop_price = max(stop_price, first_entry)
    if position.max_stage >= 4:
        higher_low = recent_higher_low(candles, index)
        if higher_low:
            stop_price = max(stop_price, position.entry_price, higher_low * (1 - config.stop_buffer_pct))
    return stop_price


def tighten_stop_green_wide(
    position: PositionStateSnapshot,
    *,
    index: int,
    candles: Sequence[Candle],
    config: StrategyConfig,
    initial_stop: float,
    first_entry: float,
) -> float:
    stop_price = position.stop_price
    if position.max_stage >= 2:
        stop_price = max(stop_price, (initial_stop + first_entry) / 2)
    if position.max_stage >= 3:
        stop_price = max(stop_price, first_entry)
    if position.max_stage >= 4:
        higher_low = recent_higher_low(candles, index)
        if higher_low:
            stop_price = max(
                stop_price,
                first_entry,
                higher_low * (1 - config.green_wide_stop_buffer_pct),
            )
    return stop_price


def tighten_stop_delayed_baseline(
    position: PositionStateSnapshot,
    *,
    index: int,
    candles: Sequence[Candle],
    config: StrategyConfig,
) -> float:
    transition_start = position.stop_transition_start
    if position.stop_transition_fill_count != len(position.fills):
        transition_start = position.stop_price
    progress = apply_stop_transition_curve(
        stop_transition_progress(position, index=index, candles=candles, config=config),
        config.stop_transition_curve,
    )
    start = transition_start or previous_baseline_stop_target(position)
    target = baseline_stop_target(position, index=index, candles=candles, config=config)
    transitioned = interpolate(start, target, progress)
    return max(position.stop_price, transitioned)


def stop_transition_progress(
    position: PositionStateSnapshot,
    *,
    index: int,
    candles: Sequence[Candle],
    config: StrategyConfig,
) -> float:
    if config.stop_transition_bars <= 0:
        return 1.0
    fill_index = _fill_index(candles, position.fills[-1].time_ms)
    elapsed = max(0, index - fill_index)
    return min(1.0, elapsed / config.stop_transition_bars)


def apply_stop_transition_curve(progress: float, curve: str) -> float:
    if curve == "linear":
        return progress
    if curve == "slow_start":
        return progress * progress
    if curve == "fast_start":
        return 1 - ((1 - progress) * (1 - progress))
    if curve == "smooth":
        return (3 * progress * progress) - (2 * progress * progress * progress)
    raise ValueError(f"unsupported stop_transition_curve: {curve}")


def baseline_stop_target(
    position: PositionStateSnapshot,
    *,
    index: int,
    candles: Sequence[Candle],
    config: StrategyConfig,
) -> float:
    target = position.initial_stop_price or position.stop_price
    if position.max_stage >= 2:
        target = max(target, position.fills[0].price)
    if position.max_stage >= 3:
        target = max(target, position.entry_price)
    if position.max_stage >= 4:
        higher_low = recent_higher_low(candles, index)
        if higher_low:
            target = max(target, higher_low * (1 - config.stop_buffer_pct))
    return target


def previous_baseline_stop_target(position: PositionStateSnapshot) -> float:
    initial_stop = position.initial_stop_price or position.stop_price
    if position.max_stage <= 2:
        return initial_stop
    if position.max_stage == 3:
        return position.fills[0].price
    return _fills_entry_price(position.fills[:-1])


def interpolate(start: float, target: float, progress: float) -> float:
    return start + ((target - start) * progress)


def _fill_index(candles: Sequence[Candle], fill_time_ms: int) -> int:
    for index, candle in enumerate(candles):
        if candle.open_time_ms == fill_time_ms:
            return index
    return 0


def _fills_entry_price(fills: tuple[PositionFillSnapshot, ...]) -> float:
    units = sum(fill.units for fill in fills)
    if not units:
        return 0.0
    return sum(fill.price * fill.units for fill in fills) / units
