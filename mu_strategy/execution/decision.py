from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.execution.plan import initial_stop_price, planned_margin_steps
from mu_strategy.models import Candle
from mu_strategy.strategy import StrategyConfig, nearest_fib_retest_level, should_enter_long


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
    fib_level = nearest_fib_retest_level(candles, index, config)
    if fib_level is None:
        return ExecutionDecision("wait", "no confirmed Fibonacci retest")

    signal = should_enter_long(candle, fib_level, regime, rsi14, macd_hist, macd_hist_prev, config)
    if not signal.allowed:
        action = "block" if _is_hard_block(signal.reason) else "wait"
        return ExecutionDecision(action, signal.reason, fib_level=fib_level)

    price = candle.close if current_price is None else current_price
    return ExecutionDecision(
        "allow",
        signal.reason,
        initial_stop=initial_stop_price(price, config),
        margin_steps=planned_margin_steps(config),
        fib_level=fib_level,
    )


def _is_hard_block(reason: str) -> bool:
    return "regime blocks" in reason or "RSI below" in reason or "MACD" in reason
