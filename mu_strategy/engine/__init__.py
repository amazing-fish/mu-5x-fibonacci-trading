"""Layer 3 unified composable backtest engine, fill/liquidation model, risk overlays, and metrics."""

from __future__ import annotations

from mu_strategy.engine.fills import (
    ExecutionModel,
    FillQuote,
    IntrabarPolicy,
    compute_liquidation_price,
    compute_position_liquidation_price,
)
from mu_strategy.engine.metrics import (
    MS_PER_YEAR,
    CostAttribution,
    SimulationResult,
    TradeRecord,
    compute_buy_and_hold_benchmark,
    compute_simulation_metrics,
)
from mu_strategy.engine.risk_overlay import (
    DailyLossLimitOverlay,
    DrawdownCircuitBreaker,
    ExposureCapOverlay,
    RiskOverlay,
    SessionWindowOverlay,
    VolTargetOverlay,
    apply_risk_overlays,
)
from mu_strategy.engine.simulator import (
    FundingSensitivityResult,
    Simulator,
    SimulatorConfig,
)

__all__ = [
    "CostAttribution",
    "DailyLossLimitOverlay",
    "DrawdownCircuitBreaker",
    "ExecutionModel",
    "ExposureCapOverlay",
    "FillQuote",
    "FundingSensitivityResult",
    "IntrabarPolicy",
    "MS_PER_YEAR",
    "RiskOverlay",
    "SessionWindowOverlay",
    "SimulationResult",
    "Simulator",
    "SimulatorConfig",
    "TradeRecord",
    "VolTargetOverlay",
    "apply_risk_overlays",
    "compute_buy_and_hold_benchmark",
    "compute_liquidation_price",
    "compute_position_liquidation_price",
    "compute_simulation_metrics",
]
