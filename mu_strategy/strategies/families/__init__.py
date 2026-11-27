"""Built-in multi-family strategy catalog and default `StrategyFamilyRegistry`."""

from __future__ import annotations

from mu_strategy.strategies.families.donchian_atr import (
    DonchianATRParams,
    DonchianATRStrategy,
    donchian_atr_param_space,
)
from mu_strategy.strategies.families.fibonacci_pullback import (
    FibonacciPullbackParams,
    FibonacciPullbackStrategy,
    fibonacci_pullback_param_space,
)
from mu_strategy.strategies.families.mean_reversion import (
    MeanReversionParams,
    MeanReversionStrategy,
    mean_reversion_param_space,
)
from mu_strategy.strategies.families.ts_momentum import (
    TSMomentumParams,
    TSMomentumStrategy,
    ts_momentum_param_space,
)
from mu_strategy.strategies.protocol import (
    StrategyFamilyDescriptor,
    StrategyFamilyRegistry,
)

__all__ = [
    "TSMomentumParams",
    "TSMomentumStrategy",
    "ts_momentum_param_space",
    "DonchianATRParams",
    "DonchianATRStrategy",
    "donchian_atr_param_space",
    "MeanReversionParams",
    "MeanReversionStrategy",
    "mean_reversion_param_space",
    "FibonacciPullbackParams",
    "FibonacciPullbackStrategy",
    "fibonacci_pullback_param_space",
    "DEFAULT_STRATEGY_FAMILY_REGISTRY",
    "default_strategy_family_registry",
]


def _build_default_registry() -> StrategyFamilyRegistry:
    return StrategyFamilyRegistry(
        (
            StrategyFamilyDescriptor(
                family_id="ts_momentum",
                version=TSMomentumStrategy.version,
                display_name="Time-Series Momentum",
                supported_sides=("buy", "sell", "long_short"),
                default_intervals=("1h",),
                factory=TSMomentumStrategy,
                param_space=ts_momentum_param_space(),
                description="Native long/short/long-short trailing return momentum with optional deadband and vol gate.",
                aliases=("tsmom",),
            ),
            StrategyFamilyDescriptor(
                family_id="donchian_atr",
                version=DonchianATRStrategy.version,
                display_name="Donchian Breakout + ATR Trailing Stop",
                supported_sides=("buy", "sell", "long_short"),
                default_intervals=("1h",),
                factory=DonchianATRStrategy,
                param_space=donchian_atr_param_space(),
                description="Native long/short/long-short Donchian breakout with monotone stateful ATR trailing stop.",
                aliases=("donchian_breakout",),
            ),
            StrategyFamilyDescriptor(
                family_id="mean_reversion",
                version=MeanReversionStrategy.version,
                display_name="RSI + Z-Score Mean Reversion",
                supported_sides=("buy", "sell", "long_short"),
                default_intervals=("1h",),
                factory=MeanReversionStrategy,
                param_space=mean_reversion_param_space(),
                description="Native long/short/long-short RSI + Z-score mean reversion with optional volatility and trend gates.",
                aliases=("mean_reversion_zscore_rsi",),
            ),
            StrategyFamilyDescriptor(
                family_id="fibonacci_pullback",
                version=FibonacciPullbackStrategy.version,
                display_name="Fibonacci Pullback (Multi-Stage)",
                supported_sides=("buy",),
                default_intervals=("15m", "1h"),
                factory=FibonacciPullbackStrategy,
                param_space=fibonacci_pullback_param_space(),
                description="Ported multi-stage Fibonacci pullback with legacy-parity mode and improved Lab ATR/min stop floor.",
                aliases=("fib_pullback",),
            ),
        )
    )


DEFAULT_STRATEGY_FAMILY_REGISTRY: StrategyFamilyRegistry = _build_default_registry()


def default_strategy_family_registry() -> StrategyFamilyRegistry:
    """Return a fresh registry populated with all built-in strategy families."""
    return _build_default_registry()
