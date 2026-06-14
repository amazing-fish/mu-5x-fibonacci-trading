from __future__ import annotations

from mu_strategy.strategies.registry import default_strategy_groups, selected_strategy_groups


DEFAULT_MU_STRATEGY_NAMES = (
    "legacy_break_high",
    "baseline",
    "direct_next_open",
    "baseline_half_protect",
    "baseline_green_wide",
    "baseline_yellow_wide",
    "baseline_yellow_green_wide",
    "baseline_half_green_wide",
    "optimized_v2",
)


__all__ = ["DEFAULT_MU_STRATEGY_NAMES", "default_strategy_groups", "selected_strategy_groups"]
