"""Phase 2 Strategy Discovery Lab: 6-Gate validation and purged walk-forward engine."""

from __future__ import annotations

from mu_strategy.lab.discovery import (
    BenchmarkSummary,
    CandidateFoldResult,
    CandidateResult,
    DiscoveryConfig,
    DiscoveryReport,
    StrategyCatalogEntry,
    WalkForwardFold,
    build_walk_forward_folds,
    default_strategy_catalog,
    discover_strategies,
    export_winner_recipe,
    overlay_presets,
)
from mu_strategy.lab.gates import (
    GateCheck,
    GateConfig,
    GateStatus,
    SixGateEvaluation,
    Verdict,
    compute_deflated_sharpe_ratio,
    evaluate_six_gates,
)

__all__ = [
    "BenchmarkSummary",
    "CandidateFoldResult",
    "CandidateResult",
    "DiscoveryConfig",
    "DiscoveryReport",
    "GateCheck",
    "GateConfig",
    "GateStatus",
    "SixGateEvaluation",
    "StrategyCatalogEntry",
    "Verdict",
    "WalkForwardFold",
    "build_walk_forward_folds",
    "compute_deflated_sharpe_ratio",
    "default_strategy_catalog",
    "discover_strategies",
    "evaluate_six_gates",
    "export_winner_recipe",
    "overlay_presets",
]
