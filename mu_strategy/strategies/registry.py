from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mu_strategy.strategies.components import StrategyComponents

if TYPE_CHECKING:
    from mu_strategy.strategy import StrategyConfig


@dataclass(frozen=True)
class StrategyGroup:
    name: str
    label: str
    config: "StrategyConfig"
    components: StrategyComponents = field(default_factory=StrategyComponents)


def _config(**kwargs) -> "StrategyConfig":
    from mu_strategy.strategy import StrategyConfig

    return StrategyConfig(**kwargs)


def baseline_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "baseline",
        "新baseline：二次回踩确认买入",
        _config(symbol=symbol, entry_execution="second_pullback", second_pullback_wait_bars=8),
        StrategyComponents(entry="二次回踩限价"),
    )


def legacy_break_high_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup("legacy_break_high", "旧突破前高baseline备用", _config(symbol=symbol))


def direct_next_open_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "direct_next_open",
        "回踩确认后下一根开盘买入",
        _config(symbol=symbol, entry_execution="direct_next_open"),
        StrategyComponents(entry="下一根开盘直接买入"),
    )


def second_pullback_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "second_pullback_limit_8",
        "回踩确认后等待二次回踩买入",
        _config(symbol=symbol, entry_execution="second_pullback", second_pullback_wait_bars=8),
        StrategyComponents(entry="二次回踩限价"),
    )


def baseline_half_protect_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "baseline_half_protect",
        "新baseline + 半保护止损",
        _config(
            symbol=symbol,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
            stop_tightening="half_protect",
        ),
        StrategyComponents(entry="二次回踩限价", exit="半保护止损"),
    )


def baseline_green_wide_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "baseline_green_wide",
        "新baseline + green宽止损（yellow窄）",
        _config(
            symbol=symbol,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
            yellow_stop_tightening="baseline",
            green_stop_tightening="wide",
        ),
        StrategyComponents(entry="二次回踩限价", exit="yellow baseline / green 宽止损"),
    )


def baseline_yellow_wide_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "baseline_yellow_wide",
        "新baseline + yellow宽止损（green窄）",
        _config(
            symbol=symbol,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
            yellow_stop_tightening="wide",
            green_stop_tightening="baseline",
        ),
        StrategyComponents(entry="二次回踩限价", exit="yellow 宽止损 / green baseline"),
    )


def baseline_yellow_green_wide_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "baseline_yellow_green_wide",
        "新baseline + yellow/green均宽止损",
        _config(
            symbol=symbol,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
            yellow_stop_tightening="wide",
            green_stop_tightening="wide",
        ),
        StrategyComponents(entry="二次回踩限价", exit="yellow/green 均宽止损"),
    )


def baseline_half_green_wide_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "baseline_half_green_wide",
        "新baseline + 半保护 + green宽止损",
        _config(
            symbol=symbol,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
            stop_tightening="half_protect_green_wide",
        ),
        StrategyComponents(entry="二次回踩限价", exit="半保护 + green 宽止损"),
    )


def optimized_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return StrategyGroup(
        "optimized_v2",
        "优化策略 v2",
        _config(
            symbol=symbol,
            max_entry_above_fib_pct=0.01,
            yellow_max_entry_above_fib_pct=0.006,
            max_signal_range_pct=0.015,
            max_entry_above_signal_close_pct=0.006,
            block_reverse_fib_resistance=True,
        ),
        StrategyComponents(
            filters=(
                "1h regime",
                "15m RSI/MACD",
                "美股现金盘窗口",
                "首仓追价限制",
                "信号K宽度限制",
                "反向 Fibonacci 压力过滤",
            )
        ),
    )


def default_strategy_groups(symbol: str = "MUUSDT") -> list[StrategyGroup]:
    return [
        legacy_break_high_strategy_group(symbol),
        baseline_strategy_group(symbol),
        direct_next_open_strategy_group(symbol),
        baseline_half_protect_strategy_group(symbol),
        baseline_green_wide_strategy_group(symbol),
        baseline_yellow_wide_strategy_group(symbol),
        baseline_yellow_green_wide_strategy_group(symbol),
        baseline_half_green_wide_strategy_group(symbol),
        optimized_strategy_group(symbol),
    ]


def selected_strategy_groups(symbol: str, names: list[str] | None = None) -> list[StrategyGroup]:
    groups = default_strategy_groups(symbol)
    if not names:
        return groups
    by_name = {group.name: group for group in groups}
    by_name["second_pullback_limit_8"] = baseline_strategy_group(symbol)
    selected_names: list[str] = []
    for value in names:
        selected_names.extend(name.strip() for name in value.split(",") if name.strip())
    unknown = [name for name in selected_names if name not in by_name]
    if unknown:
        raise ValueError(f"unknown strategy group(s): {', '.join(unknown)}")
    return [by_name[name] for name in selected_names]
