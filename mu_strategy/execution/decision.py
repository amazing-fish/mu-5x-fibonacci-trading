from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.execution.plan import initial_stop_price, planned_margin_steps
from mu_strategy.models import (
    Candle,
    EntryDecisionCode,
    EntryDecisionStage,
    EntryDisposition,
    entry_decision_metadata,
    execution_action_for,
)
from mu_strategy.strategy import (
    StrategyConfig,
    is_preferred_us_cash_window,
    nearest_fib_retest_level,
    should_enter_long,
    should_execute_entry,
)


@dataclass(frozen=True)
class ExecutionDecision:
    action: str
    reason: str
    initial_stop: float | None = None
    margin_steps: tuple[float, ...] = ()
    fib_level: float | None = None
    decision_code: EntryDecisionCode = EntryDecisionCode.UNKNOWN

    def __post_init__(self) -> None:
        if self.decision_code is not EntryDecisionCode.UNKNOWN:
            object.__setattr__(self, "action", execution_action_for(self.disposition))

    @property
    def disposition(self) -> EntryDisposition:
        return entry_decision_metadata(self.decision_code).disposition

    @property
    def stage(self) -> EntryDecisionStage:
        return entry_decision_metadata(self.decision_code).stage


def execution_decision(
    candles: list[Candle],
    *,
    config: StrategyConfig,
    regime: str,
    rsi14: float,
    macd_hist: float,
    macd_hist_prev: float,
    now_index: int | None = None,
    current_price: float | None = None,
) -> ExecutionDecision:
    if not candles:
        return _typed_execution_decision(EntryDecisionCode.NO_CANDLES, "no candles")
    index = len(candles) - 1 if now_index is None else now_index
    if index < 0 or index >= len(candles):
        raise IndexError("now_index out of range")

    candle = candles[index]
    if not is_preferred_us_cash_window(candle.open_time_ms, config):
        return _typed_execution_decision(
            EntryDecisionCode.CURRENT_BAR_OUTSIDE_TRADING_WINDOW,
            "outside preferred US cash session",
        )

    fib_level = nearest_fib_retest_level(candles, index, config)
    if fib_level is None:
        return _typed_execution_decision(
            EntryDecisionCode.NO_CONFIRMED_FIB_RETEST,
            "no confirmed Fibonacci retest",
        )

    signal = should_enter_long(candle, fib_level, regime, rsi14, macd_hist, macd_hist_prev, config)
    if not signal.allowed:
        return _typed_execution_decision(signal.decision_code, signal.reason, fib_level=fib_level)

    if config.entry_execution == "second_pullback":
        return _typed_execution_decision(
            EntryDecisionCode.WAITING_SECOND_PULLBACK,
            "waiting for second pullback",
            fib_level=fib_level,
        )

    if index + 1 >= len(candles):
        return _typed_execution_decision(
            EntryDecisionCode.NEXT_CANDLE_REQUIRED,
            "next candle required for execution gate",
            fib_level=fib_level,
        )

    next_candle = candles[index + 1]
    if not is_preferred_us_cash_window(next_candle.open_time_ms, config):
        return _typed_execution_decision(
            EntryDecisionCode.NEXT_FILL_OUTSIDE_TRADING_WINDOW,
            "next fill bar outside preferred US cash session",
            fib_level=fib_level,
        )

    execution = should_execute_entry(candles, index, next_candle, fib_level, regime, config)
    if not execution.allowed:
        return _typed_execution_decision(execution.decision_code, execution.reason, fib_level=fib_level)
    if execution.entry_price is None:
        return _typed_execution_decision(
            EntryDecisionCode.EXECUTION_PRICE_UNAVAILABLE,
            execution.reason,
            fib_level=fib_level,
        )

    return _typed_execution_decision(
        EntryDecisionCode.EXECUTION_ACCEPTED,
        signal.reason,
        initial_stop=initial_stop_price(execution.entry_price, config),
        margin_steps=planned_margin_steps(config),
        fib_level=fib_level,
    )


def _typed_execution_decision(
    decision_code: EntryDecisionCode,
    reason: str,
    *,
    initial_stop: float | None = None,
    margin_steps: tuple[float, ...] = (),
    fib_level: float | None = None,
) -> ExecutionDecision:
    if decision_code is EntryDecisionCode.UNKNOWN:
        raise ValueError("production execution decisions cannot use UNKNOWN")
    disposition = entry_decision_metadata(decision_code).disposition
    return ExecutionDecision(
        execution_action_for(disposition),
        reason,
        initial_stop,
        margin_steps,
        fib_level,
        decision_code,
    )
