from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mu_strategy.strategies.components import StrategyComponents
from mu_strategy.strategies.presets.fibonacci import preferred_fib_lookback

if TYPE_CHECKING:
    from mu_strategy.strategy import StrategyConfig


@dataclass(frozen=True)
class StrategyRuleDescriptor:
    strategy_rule_id: str
    strategy_name: str
    semantic_version: int
    side: str
    order_type: str

    def __post_init__(self) -> None:
        expected_suffix = f".v{self.semantic_version}"
        if (
            self.semantic_version <= 0
            or not re.fullmatch(r"[a-z0-9]+(?:[._][a-z0-9]+)*\.v[1-9][0-9]*", self.strategy_rule_id)
            or not self.strategy_rule_id.endswith(expected_suffix)
        ):
            raise ValueError("strategy_rule_id must be a versioned canonical identity")
        if not self.strategy_name:
            raise ValueError("strategy_name is required")
        if self.side != "buy" or self.order_type != "limit":
            raise ValueError("R0 rule descriptors must describe the long-only limit-entry shape")


@dataclass(frozen=True)
class StrategyGroup:
    name: str
    label: str
    config: "StrategyConfig"
    components: StrategyComponents = field(default_factory=StrategyComponents)

    @property
    def rule(self) -> StrategyRuleDescriptor:
        return strategy_rule_descriptor(self.name)


@dataclass(frozen=True)
class StrategyGroupRegistration:
    descriptor: StrategyRuleDescriptor
    label: str
    config_factory: Callable[[str], "StrategyConfig"]
    components: StrategyComponents = field(default_factory=StrategyComponents)
    selectable: bool = True
    is_default: bool = False
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("strategy group label is required")
        if self.is_default and not self.selectable:
            raise ValueError("default strategy groups must be selectable")
        if any(not alias for alias in self.aliases):
            raise ValueError("strategy group aliases must be non-empty")
        if len(self.aliases) != len(set(self.aliases)):
            raise ValueError("strategy group aliases must be unique")

    @property
    def name(self) -> str:
        return self.descriptor.strategy_name

    def build(self, symbol: str = "MUUSDT") -> StrategyGroup:
        return StrategyGroup(
            name=self.name,
            label=self.label,
            config=self.config_factory(symbol),
            components=self.components,
        )


def validate_strategy_rule_descriptors(descriptors: tuple[StrategyRuleDescriptor, ...]) -> None:
    ids = [descriptor.strategy_rule_id for descriptor in descriptors]
    names = [descriptor.strategy_name for descriptor in descriptors]
    if len(ids) != len(set(ids)):
        raise ValueError("strategy_rule_id values must be unique")
    if len(names) != len(set(names)):
        raise ValueError("strategy_name values must be unique")


def _config(**kwargs) -> "StrategyConfig":
    from mu_strategy.strategy import StrategyConfig

    return StrategyConfig(**kwargs)


def _baseline_config(symbol: str = "MUUSDT", **kwargs) -> "StrategyConfig":
    return _config(
        symbol=symbol,
        entry_execution="second_pullback",
        second_pullback_wait_bars=8,
        fib_lookback=preferred_fib_lookback(symbol),
        **kwargs,
    )


_STRATEGY_GROUP_REGISTRATIONS = (
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.legacy_break_high.long_limit.v1",
            "legacy_break_high",
            1,
            "buy",
            "limit",
        ),
        label="旧突破前高baseline备用",
        config_factory=lambda symbol: _config(symbol=symbol),
        is_default=True,
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline.second_pullback.long_limit.v1",
            "baseline",
            1,
            "buy",
            "limit",
        ),
        label="新baseline：二次回踩确认买入",
        config_factory=lambda symbol: _baseline_config(symbol),
        components=StrategyComponents(entry="二次回踩限价"),
        is_default=True,
        aliases=("second_pullback_limit_8",),
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.direct_next_open.long_limit.v1",
            "direct_next_open",
            1,
            "buy",
            "limit",
        ),
        label="回踩确认后下一根开盘买入",
        config_factory=lambda symbol: _config(
            symbol=symbol,
            entry_execution="direct_next_open",
        ),
        components=StrategyComponents(entry="下一根开盘直接买入"),
        is_default=True,
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_half_protect.long_limit.v1",
            "baseline_half_protect",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + 半保护止损",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            stop_tightening="half_protect",
        ),
        components=StrategyComponents(entry="二次回踩限价", exit="半保护止损"),
        is_default=True,
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_green_wide.long_limit.v1",
            "baseline_green_wide",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + green宽止损（yellow窄）",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            yellow_stop_tightening="baseline",
            green_stop_tightening="wide",
        ),
        components=StrategyComponents(
            entry="二次回踩限价",
            exit="yellow baseline / green 宽止损",
        ),
        is_default=True,
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_yellow_wide.long_limit.v1",
            "baseline_yellow_wide",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + yellow宽止损（green窄）",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            yellow_stop_tightening="wide",
            green_stop_tightening="baseline",
        ),
        components=StrategyComponents(
            entry="二次回踩限价",
            exit="yellow 宽止损 / green baseline",
        ),
        is_default=True,
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_yellow_green_wide.long_limit.v1",
            "baseline_yellow_green_wide",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + yellow/green均宽止损",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            yellow_stop_tightening="wide",
            green_stop_tightening="wide",
        ),
        components=StrategyComponents(
            entry="二次回踩限价",
            exit="yellow/green 均宽止损",
        ),
        is_default=True,
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_half_green_wide.long_limit.v1",
            "baseline_half_green_wide",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + 半保护 + green宽止损",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            stop_tightening="half_protect_green_wide",
        ),
        components=StrategyComponents(
            entry="二次回踩限价",
            exit="半保护 + green 宽止损",
        ),
        is_default=True,
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_delayed_tighten.long_limit.v1",
            "baseline_delayed_tighten",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + 加仓后线性渐进抬止损",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            stop_tightening="delayed_baseline",
            stop_transition_bars=8,
            stop_transition_curve="linear",
        ),
        components=StrategyComponents(
            entry="二次回踩限价",
            exit="加仓后8根15m K线性渐进抬止损",
        ),
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_delayed_tighten_slow_start.long_limit.v1",
            "baseline_delayed_tighten_slow_start",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + 加仓后慢启动抬止损",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            stop_tightening="delayed_baseline",
            stop_transition_bars=8,
            stop_transition_curve="slow_start",
        ),
        components=StrategyComponents(
            entry="二次回踩限价",
            exit="加仓后8根15m K慢启动抬止损",
        ),
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_delayed_tighten_fast_start.long_limit.v1",
            "baseline_delayed_tighten_fast_start",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + 加仓后快启动抬止损",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            stop_tightening="delayed_baseline",
            stop_transition_bars=8,
            stop_transition_curve="fast_start",
        ),
        components=StrategyComponents(
            entry="二次回踩限价",
            exit="加仓后8根15m K快启动抬止损",
        ),
        is_default=True,
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.baseline_delayed_tighten_smooth.long_limit.v1",
            "baseline_delayed_tighten_smooth",
            1,
            "buy",
            "limit",
        ),
        label="新baseline + 加仓后S曲线抬止损",
        config_factory=lambda symbol: _baseline_config(
            symbol,
            stop_tightening="delayed_baseline",
            stop_transition_bars=8,
            stop_transition_curve="smooth",
        ),
        components=StrategyComponents(
            entry="二次回踩限价",
            exit="加仓后8根15m K S曲线抬止损",
        ),
    ),
    StrategyGroupRegistration(
        descriptor=StrategyRuleDescriptor(
            "mu.optimized.long_limit.v2",
            "optimized_v2",
            2,
            "buy",
            "limit",
        ),
        label="优化策略 v2",
        config_factory=lambda symbol: _config(
            symbol=symbol,
            max_entry_above_fib_pct=0.01,
            yellow_max_entry_above_fib_pct=0.006,
            max_signal_range_pct=0.015,
            max_entry_above_signal_close_pct=0.006,
            block_reverse_fib_resistance=True,
        ),
        components=StrategyComponents(
            filters=(
                "1h regime",
                "15m RSI/MACD",
                "美股现金盘窗口",
                "首仓追价限制",
                "信号K宽度限制",
                "反向 Fibonacci 压力过滤",
            )
        ),
        is_default=True,
    ),
)


def _validate_strategy_group_registrations(
    registrations: tuple[StrategyGroupRegistration, ...],
) -> None:
    if not registrations:
        raise ValueError("at least one strategy group registration is required")
    validate_strategy_rule_descriptors(
        tuple(registration.descriptor for registration in registrations)
    )
    canonical_names = {registration.name for registration in registrations}
    seen_names = set(canonical_names)
    for registration in registrations:
        for alias in registration.aliases:
            if alias in seen_names:
                raise ValueError(f"strategy group name or alias is registered more than once: {alias}")
            seen_names.add(alias)


_validate_strategy_group_registrations(_STRATEGY_GROUP_REGISTRATIONS)
_REGISTRATION_BY_CANONICAL_NAME = {
    registration.name: registration
    for registration in _STRATEGY_GROUP_REGISTRATIONS
}
_REGISTRATION_BY_SELECTION_NAME = dict(_REGISTRATION_BY_CANONICAL_NAME)
for _registration in _STRATEGY_GROUP_REGISTRATIONS:
    _REGISTRATION_BY_SELECTION_NAME.update(
        (alias, _registration)
        for alias in _registration.aliases
    )


def strategy_group_registrations() -> tuple[StrategyGroupRegistration, ...]:
    return _STRATEGY_GROUP_REGISTRATIONS


def default_strategy_names() -> tuple[str, ...]:
    return tuple(
        registration.name
        for registration in _STRATEGY_GROUP_REGISTRATIONS
        if registration.is_default
    )


def strategy_rule_descriptor(strategy_name: str) -> StrategyRuleDescriptor:
    try:
        return _REGISTRATION_BY_SELECTION_NAME[strategy_name].descriptor
    except KeyError as exc:
        raise ValueError(f"strategy has no registered rule identity: {strategy_name}") from exc


def _build_registered_strategy_group(
    strategy_name: str,
    symbol: str,
) -> StrategyGroup:
    return _REGISTRATION_BY_CANONICAL_NAME[strategy_name].build(symbol)


def baseline_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("baseline", symbol)


def legacy_break_high_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("legacy_break_high", symbol)


def direct_next_open_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("direct_next_open", symbol)


def second_pullback_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    baseline = baseline_strategy_group(symbol)
    return StrategyGroup(
        name="second_pullback_limit_8",
        label="回踩确认后等待二次回踩买入",
        config=baseline.config,
        components=baseline.components,
    )


def baseline_half_protect_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("baseline_half_protect", symbol)


def baseline_green_wide_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("baseline_green_wide", symbol)


def baseline_yellow_wide_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("baseline_yellow_wide", symbol)


def baseline_yellow_green_wide_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("baseline_yellow_green_wide", symbol)


def baseline_half_green_wide_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("baseline_half_green_wide", symbol)


def baseline_delayed_tighten_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("baseline_delayed_tighten", symbol)


def baseline_delayed_tighten_slow_start_strategy_group(
    symbol: str = "MUUSDT",
) -> StrategyGroup:
    return _build_registered_strategy_group(
        "baseline_delayed_tighten_slow_start",
        symbol,
    )


def baseline_delayed_tighten_fast_start_strategy_group(
    symbol: str = "MUUSDT",
) -> StrategyGroup:
    return _build_registered_strategy_group(
        "baseline_delayed_tighten_fast_start",
        symbol,
    )


def baseline_delayed_tighten_smooth_strategy_group(
    symbol: str = "MUUSDT",
) -> StrategyGroup:
    return _build_registered_strategy_group(
        "baseline_delayed_tighten_smooth",
        symbol,
    )


def optimized_strategy_group(symbol: str = "MUUSDT") -> StrategyGroup:
    return _build_registered_strategy_group("optimized_v2", symbol)


def default_strategy_groups(symbol: str = "MUUSDT") -> list[StrategyGroup]:
    return [
        registration.build(symbol)
        for registration in _STRATEGY_GROUP_REGISTRATIONS
        if registration.is_default
    ]


def selected_strategy_groups(
    symbol: str,
    names: list[str] | None = None,
) -> list[StrategyGroup]:
    if not names:
        return default_strategy_groups(symbol)

    selected_names: list[str] = []
    for value in names:
        selected_names.extend(name.strip() for name in value.split(",") if name.strip())

    registrations: list[StrategyGroupRegistration] = []
    unknown: list[str] = []
    for name in selected_names:
        registration = _REGISTRATION_BY_SELECTION_NAME.get(name)
        if registration is None or not registration.selectable:
            unknown.append(name)
        else:
            registrations.append(registration)
    if unknown:
        raise ValueError(f"unknown strategy group(s): {', '.join(unknown)}")

    built: dict[str, StrategyGroup] = {}
    for registration in registrations:
        built.setdefault(registration.name, registration.build(symbol))
    return [
        built[registration.name]
        for registration in registrations
    ]
