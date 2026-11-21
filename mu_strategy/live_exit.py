"""Pure shadow exit observations for live position snapshots."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mu_strategy.core.risk import evaluate_risk_exit
from mu_strategy.core.trading_calendar import CalendarUnavailable
from mu_strategy.models import Candle
from mu_strategy.strategies.position_rules import (
    PositionFillSnapshot,
    PositionStateSnapshot,
    tighten_stop,
)
from mu_strategy.strategy import StrategyConfig, is_preferred_us_cash_window

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

    If the reference calendar is unavailable, a breached carried stop still
    gives a confirmed exit. Otherwise ``exit_triggered`` is unknown (None),
    with ``calendar_error`` explaining the unevaluated session condition.
    """

    candle_open_time_ms: int
    latest_close: float
    stop_before_candle: float
    stop_after_candle_if_open: float
    exit_triggered: bool | None
    exit_reason: str | None
    trigger_basis: str
    latest_close_at_or_below_tightened_stop: bool
    transition_fill_count: int
    transition_start: float
    calendar_error: str | None = None


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

    ``decision_status`` reports the actionable shadow exit status derived from
    ``assumption_evaluation``: ``"exit"`` when a stop or non-session liquidation
    condition triggered, ``"hold"`` when no exit triggered, or ``"unknown"``
    when the row/candles are unavailable or the calendar could not be evaluated.
    ``state_quality`` and ``assumptions`` continue to disclose aggregate-row limits.
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

    calendar_error: str | None = None
    in_session: bool | None
    try:
        in_session = is_preferred_us_cash_window(candle.open_time_ms, config)
    except CalendarUnavailable as exc:
        # A missing reference session cannot invalidate a known carried stop,
        # nor justify claiming that every exit condition is clear.
        in_session = None
        calendar_error = str(exc)

    risk = evaluate_risk_exit(
        candle,
        entry_price=position.entry_price,
        stop_price=position.stop_price,
        config=config,
        stop_reason="stop",
        in_session=True if in_session is None else in_session,
    )
    if risk is not None:
        exit_triggered: bool | None = True
        exit_reason: str | None = risk.reason
        trigger_basis = risk.trigger_basis
    elif calendar_error is not None:
        exit_triggered = None
        exit_reason = None
        trigger_basis = "calendar_unavailable"
    else:
        exit_triggered = False
        exit_reason = None
        trigger_basis = "none"

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
        exit_reason=exit_reason,
        trigger_basis=trigger_basis,
        latest_close_at_or_below_tightened_stop=candle.close <= outcome.stop_price,
        transition_fill_count=outcome.transition_fill_count,
        transition_start=outcome.transition_start,
        calendar_error=calendar_error,
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
            f"leverage={config.leverage}",
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
    """Return an honest, actionable shadow observation for one aggregate OKX row."""

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
    if evaluation is None or evaluation.exit_triggered is None:
        decision_status = "unknown"
    elif evaluation.exit_triggered:
        decision_status = "exit"
    else:
        decision_status = "hold"
    return OkxExitObservation(
        symbol=mapped.symbol,
        decision_status=decision_status,
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
