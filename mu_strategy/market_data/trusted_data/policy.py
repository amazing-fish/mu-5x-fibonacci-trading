from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.market_data.trusted_data.contracts import (
    AvailabilityState,
    DatasetHealth,
    FreshnessAssessment,
    FreshnessState,
    HealthReason,
    IntegrityState,
    IntervalPlan,
    ManifestStatus,
    RefreshRunOutcome,
    TrustDecision,
    TrustedLoadContext,
)
from mu_strategy.market_data.utils import interval_to_ms


class IntervalDependencyPlanner:
    def plan(self, requested_intervals: tuple[str, ...]) -> IntervalPlan:
        requested = tuple(dict.fromkeys(requested_intervals))
        effective: list[str] = []
        needs_base = any(interval in {"15m", "1h"} for interval in requested)
        if needs_base:
            effective.append("5m")
        for interval in requested:
            if interval not in effective:
                effective.append(interval)
        return IntervalPlan(requested_intervals=requested, effective_intervals=tuple(effective))


@dataclass(frozen=True)
class FreshnessPolicy:
    max_staleness_bars: int = 3

    def assess(
        self,
        *,
        now_ms: int,
        interval: str,
        last_confirmed_open_time_ms: int | None,
    ) -> FreshnessAssessment:
        if last_confirmed_open_time_ms is None:
            return FreshnessAssessment(FreshnessState.STALE, HealthReason.CACHE_MISSING)
        max_age_ms = interval_to_ms(interval) * max(1, self.max_staleness_bars)
        age_ms = now_ms - last_confirmed_open_time_ms
        if age_ms > max_age_ms:
            return FreshnessAssessment(FreshnessState.STALE, HealthReason.STALE_BY_CLOCK, age_ms, max_age_ms)
        return FreshnessAssessment(FreshnessState.FRESH, HealthReason.OK, age_ms, max_age_ms)


@dataclass(frozen=True)
class TrustPolicy:
    name: str
    require_manifest_success: bool = True
    require_fresh: bool = True
    allow_invalid: bool = False

    def decide(
        self,
        *,
        context: TrustedLoadContext,
        health_by_interval: dict[str, DatasetHealth],
        required_intervals: tuple[str, ...],
    ) -> TrustDecision:
        if self.require_manifest_success:
            if context.manifest.outcome == RefreshRunOutcome.FAILED:
                return TrustDecision(False, HealthReason.RUN_FAILED)
            if context.manifest.outcome == RefreshRunOutcome.PARTIAL:
                return TrustDecision(False, HealthReason.RUN_PARTIAL)
            if context.manifest.status == ManifestStatus.INVALID:
                return TrustDecision(False, HealthReason.MANIFEST_INVALID)
            if context.manifest.status == ManifestStatus.STALE:
                return TrustDecision(False, HealthReason.MANIFEST_STALE)
        for interval in required_intervals:
            health = health_by_interval.get(interval)
            if health is None:
                return TrustDecision(False, HealthReason.CACHE_MISSING)
            if health.availability != AvailabilityState.AVAILABLE:
                return TrustDecision(False, health.primary_reason)
            if health.integrity != IntegrityState.VALID and not self.allow_invalid:
                return TrustDecision(False, health.primary_reason)
            if health.freshness == FreshnessState.STALE and self.require_fresh:
                return TrustDecision(False, health.primary_reason)
        return TrustDecision(True, HealthReason.OK)


def trading_strict_policy() -> TrustPolicy:
    return TrustPolicy(name="trading_strict", require_manifest_success=True, require_fresh=True)


def research_strict_policy() -> TrustPolicy:
    return TrustPolicy(name="research_strict", require_manifest_success=True, require_fresh=True)


def observe_only_policy() -> TrustPolicy:
    return TrustPolicy(name="observe_only", require_manifest_success=False, require_fresh=False, allow_invalid=True)
