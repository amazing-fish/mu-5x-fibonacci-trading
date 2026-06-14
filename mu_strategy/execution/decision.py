from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.execution.plan import initial_stop_price, planned_margin_steps
from mu_strategy.models import Candle
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
        return ExecutionDecision("wait", "no candles")
    index = len(candles) - 1 if now_index is None else now_index
    if index < 0 or index >= len(candles):
        raise IndexError("now_index out of range")

    candle = candles[index]
    if not is_preferred_us_cash_window(candle.open_time_ms, config):
        return ExecutionDecision("wait", "outside preferred US cash session")

    fib_level = nearest_fib_retest_level(candles, index, config)
    if fib_level is None:
        return ExecutionDecision("wait", "no confirmed Fibonacci retest")

    signal = should_enter_long(candle, fib_level, regime, rsi14, macd_hist, macd_hist_prev, config)
    if not signal.allowed:
        action = "block" if _is_hard_block(signal.reason) else "wait"
        return ExecutionDecision(action, signal.reason, fib_level=fib_level)

    if config.entry_execution == "second_pullback":
        return ExecutionDecision("wait", "waiting for second pullback", fib_level=fib_level)

    if index + 1 >= len(candles):
        return ExecutionDecision("wait", "next candle required for execution gate", fib_level=fib_level)

    next_candle = candles[index + 1]
    if not is_preferred_us_cash_window(next_candle.open_time_ms, config):
        return ExecutionDecision("wait", "next fill bar outside preferred US cash session", fib_level=fib_level)

    execution = should_execute_entry(candles, index, next_candle, fib_level, regime, config)
    if not execution.allowed or execution.entry_price is None:
        action = "block" if _is_hard_block(execution.reason) else "wait"
        return ExecutionDecision(action, execution.reason, fib_level=fib_level)

    return ExecutionDecision(
        "allow",
        signal.reason,
        initial_stop=initial_stop_price(execution.entry_price, config),
        margin_steps=planned_margin_steps(config),
        fib_level=fib_level,
    )


def _is_hard_block(reason: str) -> bool:
    return any(
        fragment in reason
        for fragment in (
            "regime blocks",
            "RSI below",
            "MACD",
            "signal candle too wide",
            "entry too far",
            "reverse Fibonacci resistance",
        )
    )
