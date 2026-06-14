from __future__ import annotations

from mu_strategy.strategy import StrategyConfig


def planned_margin_steps(config: StrategyConfig) -> tuple[float, ...]:
    return tuple(config.margin_steps)


def initial_stop_price(current_price: float, config: StrategyConfig) -> float:
    return current_price * (1 - config.initial_stop_pct)
