from __future__ import annotations

from mu_strategy.strategies.registry import (
    default_strategy_groups,
    default_strategy_names,
    selected_strategy_groups,
)


DEFAULT_MU_STRATEGY_NAMES = default_strategy_names()


__all__ = ["DEFAULT_MU_STRATEGY_NAMES", "default_strategy_groups", "selected_strategy_groups"]
