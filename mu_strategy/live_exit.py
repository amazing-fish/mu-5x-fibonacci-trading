"""Pure shadow exit observations for live position snapshots."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mu_strategy.models import Candle
from mu_strategy.strategies.position_rules import (
    PositionFillSnapshot,
    PositionStateSnapshot,
    tighten_stop,
)
from mu_strategy.strategy import StrategyConfig

__all__ = [
    "ExitObservation",
    "MappedPositionSnapshot",
    "OkxExitObservation",
    "evaluate_exit",
    "map_okx_position",
    "observe_okx_position",
]


@dataclass(frozen=True)
class ExitObservation:
    """One candle's exit decision using the backtest event order.

    The backtest tests the candle low against the stop carried into the
    candle. Only a position that survives that test receives the tightened
    stop for the next candle. ``stop_after_candle_if_open`` is therefore a
    conditional next value, not the trigger used for this candle.
    """

    candle_open_time_ms: int
    latest_close: float
    stop_before_candle: float
    stop_after_candle_if_open: float
    exit_triggered: bool
    exit_reason: str | None
    trigger_basis: str
    latest_close_at_or_below_tightened_stop: bool
    transition_fill_count: int
    transition_start: float


@dataclass(frozen=True)
class MappedPositionSnapshot:
    """An explicitly degraded mapping from an OKX aggregate position row."""

    symbol: str
    position_size: float | None
    average_entry_price: float | None
    assumption_snapshot: PositionStateSnapshot | None
    state_quality: str
    known_fields: tuple[str, ...]
    unknown_fields: tuple[str, ...]
    assumptions: tuple[str, ...]
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class OkxExitObservation:
    """Serializable shadow output for one OKX position row.

    ``decision_status`` remains ``unknown`` for aggregate OKX rows because
    the exchange response does not contain the fill history or carried stop
    state. ``assumption_evaluation`` is deliberately separate from the actual
    decision so consumers cannot mistake the degraded estimate for fact.
    """

    symbol: str
    decision_status: str
    state_quality: str
    position_size: float | None
    average_entry_price: float | None
    known_fields: tuple[str, ...]
    unknown_fields: tuple[str, ...]
    assumptions: tuple[str, ...]
    unavailable_reason: str | None
    assumption_evaluation: ExitObservation | None


def evaluate_exit(
    position: PositionStateSnapshot,
    candle: Candle,
    *,
    index: int,
    candles: Sequence[Candle],
    regime: str,
    config: StrategyConfig,
) -> ExitObservation:
    """Evaluate a known position without mutating state or placing orders."""

    exit_triggered = candle.low <= position.stop_price
    outcome = tighten_stop(
        position,
        candle,
        index=index,
        candles=candles,
        regime=regime,
        config=config,
    )
    return ExitObservation(
        candle_open_time_ms=candle.open_time_ms,
        latest_close=candle.close,
        stop_before_candle=position.stop_price,
        stop_after_candle_if_open=outcome.stop_price,
        exit_triggered=exit_triggered,
        exit_reason="stop" if exit_triggered else None,
        trigger_basis="candle_low_at_or_below_stop_before_candle",
        latest_close_at_or_below_tightened_stop=candle.close <= outcome.stop_price,
        transition_fill_count=outcome.transition_fill_count,
        transition_start=outcome.transition_start,
    )


def map_okx_position(
    row: Mapping[str, Any],
    *,
    candle: Candle,
    config: StrategyConfig,
) -> MappedPositionSnapshot:
    """Map an aggregate OKX position row without hiding missing state.

    OKX exposes an aggregate average price and size, not the fill history,
    stage, or stop-transition state required by ``PositionStateSnapshot``.
    When the aggregate fields are usable, this function builds a clearly
    labelled single-fill/stage-one assumption snapshot for diagnostics only.
    """

    symbol = str(row.get("instId") or "")
    position_size = _finite_float(row.get("pos"))
    average_entry_price = _finite_float(row.get("avgPx"))
    known_fields = tuple(
        field
        for field, value in (
            ("instId", symbol or None),
            ("pos", position_size),
            ("avgPx", average_entry_price),
        )
        if value is not None
    )
    unknown_fields = (
        "fills",
        "stop_price",
        "initial_stop_price",
        "entry_anchor",
        "max_stage",
        "stop_transition_fill_count",
        "stop_transition_start",
    )
    position_side = str(row.get("posSide") or "net").lower()
    if not symbol:
        return MappedPositionSnapshot(
            symbol="",
            position_size=position_size,
            average_entry_price=average_entry_price,
            assumption_snapshot=None,
            state_quality="unavailable",
            known_fields=known_fields,
            unknown_fields=("instId", *unknown_fields),
            assumptions=(),
            unavailable_reason="missing_instrument_id",
        )
    if position_size is None or position_size == 0:
        return MappedPositionSnapshot(
            symbol=symbol,
            position_size=position_size,
            average_entry_price=average_entry_price,
            assumption_snapshot=None,
            state_quality="unavailable",
            known_fields=known_fields,
            unknown_fields=("pos", *unknown_fields) if position_size is None else unknown_fields,
            assumptions=(),
            unavailable_reason="missing_or_zero_position_size",
        )
    if position_side == "short" or (position_side != "long" and position_size < 0):
        return MappedPositionSnapshot(
            symbol=symbol,
            position_size=position_size,
            average_entry_price=average_entry_price,
            assumption_snapshot=None,
            state_quality="unavailable",
            known_fields=known_fields,
            unknown_fields=unknown_fields,
            assumptions=(),
            unavailable_reason="short_position_not_supported_by_long_strategy",
        )
    if average_entry_price is None or average_entry_price <= 0:
        return MappedPositionSnapshot(
            symbol=symbol,
            position_size=position_size,
            average_entry_price=average_entry_price,
            assumption_snapshot=None,
            state_quality="unavailable",
            known_fields=known_fields,
            unknown_fields=("avgPx", *unknown_fields) if average_entry_price is None else unknown_fields,
            assumptions=(),
            unavailable_reason="missing_or_invalid_average_entry_price",
        )

    assumed_initial_stop = average_entry_price * (1 - config.initial_stop_pct)
    snapshot = PositionStateSnapshot(
        fills=(
            PositionFillSnapshot(
                time_ms=candle.open_time_ms,
                price=average_entry_price,
                units=abs(position_size),
            ),
        ),
        stop_price=assumed_initial_stop,
        entry_anchor=average_entry_price,
        initial_stop_price=assumed_initial_stop,
        max_stage=1,
    )
    return MappedPositionSnapshot(
        symbol=symbol,
        position_size=position_size,
        average_entry_price=average_entry_price,
        assumption_snapshot=snapshot,
        state_quality="degraded",
        known_fields=known_fields,
        unknown_fields=unknown_fields,
        assumptions=(
            "fills=single_synthetic_fill_at_avgPx",
            "synthetic_fill_time=latest_closed_candle",
            "entry_anchor=avgPx",
            "stop_price=avgPx*(1-initial_stop_pct)",
            "initial_stop_price=assumed_stop_price",
            "max_stage=1",
            "stop_transition_state=defaults",
        ),
    )


def observe_okx_position(
    row: Mapping[str, Any],
    *,
    candles: Sequence[Candle],
    regime: str,
    config: StrategyConfig,
    unavailable_reason: str | None = None,
) -> OkxExitObservation:
    """Return an honest shadow observation for one aggregate OKX row."""

    if not candles:
        symbol = str(row.get("instId") or "")
        position_size = _finite_float(row.get("pos"))
        average_entry_price = _finite_float(row.get("avgPx"))
        known_fields = tuple(
            field
            for field, value in (
                ("instId", symbol or None),
                ("pos", position_size),
                ("avgPx", average_entry_price),
            )
            if value is not None
        )
        return OkxExitObservation(
            symbol=symbol,
            decision_status="unknown",
            state_quality="unavailable",
            position_size=position_size,
            average_entry_price=average_entry_price,
            known_fields=known_fields,
            unknown_fields=(
                "closed_15m_candles",
                "fills",
                "stop_price",
                "initial_stop_price",
                "entry_anchor",
                "max_stage",
                "stop_transition_fill_count",
                "stop_transition_start",
            ),
            assumptions=(),
            unavailable_reason=unavailable_reason or "no_closed_15m_candles",
            assumption_evaluation=None,
        )

    index = len(candles) - 1
    candle = candles[index]
    mapped = map_okx_position(row, candle=candle, config=config)
    evaluation = None
    if mapped.assumption_snapshot is not None:
        evaluation = evaluate_exit(
            mapped.assumption_snapshot,
            candle,
            index=index,
            candles=candles,
            regime=regime,
            config=config,
        )
    return OkxExitObservation(
        symbol=mapped.symbol,
        decision_status="unknown",
        state_quality=mapped.state_quality,
        position_size=mapped.position_size,
        average_entry_price=mapped.average_entry_price,
        known_fields=mapped.known_fields,
        unknown_fields=mapped.unknown_fields,
        assumptions=mapped.assumptions,
        unavailable_reason=unavailable_reason or mapped.unavailable_reason,
        assumption_evaluation=evaluation,
    )


def _finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
