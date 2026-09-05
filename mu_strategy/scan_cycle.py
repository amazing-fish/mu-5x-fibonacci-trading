from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable

from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.market_data.trusted_data.compat import CandleBundle
from mu_strategy.market_data.trusted_data.contracts import Clock, HealthReason, SystemClock
from mu_strategy.market_data.trusted_data.policy import IntervalDependencyPlanner
from mu_strategy.models import EntryDecisionCode
from mu_strategy.observations import (
    ObservationFailureCode,
    STAGE0_TRUST_POLICY_NAME,
    STAGE0_TRUST_POLICY_VERSION,
    Stage0Observation,
    Stage0ObservationCycle,
    TrustedObservationReference,
    build_stage0_observation,
    canonical_payload_sha256,
    sanitize_observation_text,
)


Scanner = Callable[..., EntryScanResult]


@dataclass(frozen=True)
class ScanDataFailure:
    """A loader/gate failure with a separate versionless presentation payload."""

    code: ObservationFailureCode
    health_reason: HealthReason
    payload: dict[str, Any]


@dataclass(frozen=True)
class ScanCycleOutcome:
    scan_result: EntryScanResult | None
    data_error: dict[str, Any] | None
    observation: Stage0Observation


class ScanCycle:
    """Evaluate each symbol once; persistence is an optional downstream consumer."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        id_factory: Callable[[], str] | None = None,
    ):
        self._clock = clock or SystemClock()
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.cycle_id = self._id_factory()
        self.created_at_ms = self._clock.now_ms()
        self._observations: list[Stage0Observation] = []

    def scan_symbol(
        self,
        *,
        symbol: str,
        source: str,
        bundle: CandleBundle | None,
        requested_intervals: tuple[str, ...],
        strategy_name: str,
        strategy_config: Any,
        scanner: Scanner,
        data_failure: ScanDataFailure | None,
    ) -> ScanCycleOutcome:
        trusted: TrustedObservationReference | None = None
        if bundle is not None:
            try:
                trusted = _trusted_reference(symbol, bundle, requested_intervals=requested_intervals)
            except ValueError as exc:
                if data_failure is None:
                    data_failure = ScanDataFailure(
                        ObservationFailureCode.TRUSTED_PROVENANCE_INCOMPLETE,
                        HealthReason.MALFORMED_MANIFEST,
                        {
                            "symbol": symbol,
                            "reason": "trusted_provenance_incomplete",
                            "error_type": type(exc).__name__,
                            "message": sanitize_observation_text(str(exc)),
                        },
                    )

        if data_failure is not None:
            data_error = data_failure.payload
            if trusted is None or trusted.allowed:
                trusted = _blocked_reference(
                    requested_intervals=requested_intervals,
                    reason=data_failure.health_reason,
                )
            observation = self._build_observation(
                symbol=symbol,
                source=source,
                bundle=bundle,
                trusted=trusted,
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                result=None,
                failure_code=data_failure.code,
                error_type=data_error.get("error_type"),
                error_message=data_error.get("message") or data_error.get("reason"),
            )
            return self._record(ScanCycleOutcome(None, data_error, observation))

        if trusted is None or not trusted.allowed:
            decision_reason = trusted.reason if trusted is not None else HealthReason.MALFORMED_MANIFEST
            data_error = {
                "symbol": symbol,
                "reason": "trusted_data_blocked",
                "status_reason": decision_reason.value,
                "message": decision_reason.value,
            }
            blocked = trusted or _blocked_reference(
                requested_intervals=requested_intervals,
                reason=decision_reason,
            )
            observation = self._build_observation(
                symbol=symbol,
                source=source,
                bundle=bundle,
                trusted=blocked,
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                result=None,
                failure_code=ObservationFailureCode.TRUSTED_DATA_BLOCKED,
                error_message=decision_reason.value,
            )
            return self._record(ScanCycleOutcome(None, data_error, observation))

        try:
            result = scanner(
                symbol,
                bundle.candles_by_interval.get("15m", []),
                bundle.candles_by_interval.get("1h", []),
                config=strategy_config,
            )
        except Exception as exc:
            data_error = {
                "symbol": symbol,
                "reason": "scanner_failed",
                "error_type": type(exc).__name__,
                "message": sanitize_observation_text(str(exc)),
            }
            observation = self._build_observation(
                symbol=symbol,
                source=source,
                bundle=bundle,
                trusted=trusted,
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                result=None,
                failure_code=ObservationFailureCode.SCANNER_EXCEPTION,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return self._record(ScanCycleOutcome(None, data_error, observation))

        if not isinstance(result, EntryScanResult) or result.decision_code is EntryDecisionCode.UNKNOWN:
            result_type = type(result).__name__
            message = (
                "scanner returned EntryDecisionCode.UNKNOWN"
                if isinstance(result, EntryScanResult)
                else f"scanner returned {result_type} instead of EntryScanResult"
            )
            return self._record_invalid_result(
                symbol=symbol,
                source=source,
                bundle=bundle,
                trusted=trusted,
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                message=message,
            )

        try:
            observation = self._build_observation(
                symbol=symbol,
                source=source,
                bundle=bundle,
                trusted=trusted,
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                result=result,
            )
        except (TypeError, ValueError) as exc:
            return self._record_invalid_result(
                symbol=symbol,
                source=source,
                bundle=bundle,
                trusted=trusted,
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                message=f"scanner result failed canonical validation: {exc}",
            )
        return self._record(ScanCycleOutcome(result, None, observation))

    def observations(self) -> Stage0ObservationCycle:
        return Stage0ObservationCycle(
            cycle_id=self.cycle_id,
            created_at_ms=self.created_at_ms,
            observations=tuple(self._observations),
        )

    def _record(self, result: ScanCycleOutcome) -> ScanCycleOutcome:
        self._observations.append(result.observation)
        return result

    def _record_invalid_result(
        self,
        *,
        symbol: str,
        source: str,
        bundle: CandleBundle,
        trusted: TrustedObservationReference,
        strategy_name: str,
        strategy_config: Any,
        message: str,
    ) -> ScanCycleOutcome:
        data_error = {
            "symbol": symbol,
            "reason": "scanner_result_invalid",
            "error_type": "InvalidScannerResult",
            "message": sanitize_observation_text(message),
        }
        observation = self._build_observation(
            symbol=symbol,
            source=source,
            bundle=bundle,
            trusted=trusted,
            strategy_name=strategy_name,
            strategy_config=strategy_config,
            result=None,
            failure_code=ObservationFailureCode.SCANNER_RESULT_INVALID,
            error_type=data_error["error_type"],
            error_message=data_error["message"],
        )
        return self._record(ScanCycleOutcome(None, data_error, observation))

    def _build_observation(
        self,
        *,
        symbol: str,
        source: str,
        bundle: CandleBundle | None,
        trusted: TrustedObservationReference,
        strategy_name: str,
        strategy_config: Any,
        result: EntryScanResult | None,
        failure_code: ObservationFailureCode | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> Stage0Observation:
        observed_at_ms = (
            bundle.observed_at_ms
            if isinstance(bundle, CandleBundle) and bundle.observed_at_ms is not None
            else self.created_at_ms
        )
        provenance = (
            "canonical_trusted_generation"
            if isinstance(bundle, CandleBundle) and bundle.load_context is not None
            else "trusted_load_failure"
        )
        observation = build_stage0_observation(
            observation_id="pending",
            cycle_id=self.cycle_id,
            symbol=symbol,
            created_at_ms=self.created_at_ms,
            observed_at_ms=observed_at_ms,
            trusted=trusted,
            strategy_name=strategy_name,
            strategy_config_fingerprint=canonical_payload_sha256(strategy_config),
            result=result,
            compatibility_source=source,
            provenance=provenance,
            failure_code=failure_code,
            error_type=error_type,
            error_message=error_message,
        )
        return replace(observation, observation_id=self._id_factory())


def _trusted_reference(
    symbol: str,
    bundle: CandleBundle,
    *,
    requested_intervals: tuple[str, ...],
) -> TrustedObservationReference:
    if not isinstance(bundle, CandleBundle):
        raise ValueError("canonical trusted CandleBundle is required")
    context = bundle.load_context
    decision = bundle.trust_decision
    if context is None or decision is None:
        raise ValueError("canonical trusted load context and TrustDecision are required")
    manifest = context.manifest
    run_id = bundle.run_id or manifest.run_id
    if not run_id or run_id != manifest.run_id or context.generation_id != manifest.run_id:
        raise ValueError("trusted run_id and generation identity must match")
    effective_intervals = IntervalDependencyPlanner().plan(requested_intervals).effective_intervals
    if not set(effective_intervals).issubset(manifest.effective_intervals):
        raise ValueError("trusted manifest does not contain the effective interval dependency set")
    content_hashes: list[tuple[str, str]] = []
    for interval in effective_intervals:
        health = manifest.datasets.get((symbol, interval))
        if health is None or health.content_sha256 is None:
            if decision.allowed:
                raise ValueError(f"trusted content SHA-256 is missing for {interval}")
            continue
        content_hashes.append((interval, health.content_sha256))
    return TrustedObservationReference(
        run_id=run_id,
        requested_intervals=requested_intervals,
        effective_intervals=effective_intervals,
        content_sha256_by_interval=tuple(content_hashes),
        policy_name=STAGE0_TRUST_POLICY_NAME,
        policy_version=STAGE0_TRUST_POLICY_VERSION,
        allowed=decision.allowed,
        reason=decision.reason,
    )


def _blocked_reference(
    *,
    requested_intervals: tuple[str, ...],
    reason: HealthReason,
) -> TrustedObservationReference:
    plan = IntervalDependencyPlanner().plan(requested_intervals)
    return TrustedObservationReference(
        run_id=None,
        requested_intervals=plan.requested_intervals,
        effective_intervals=plan.effective_intervals,
        content_sha256_by_interval=(),
        policy_name=STAGE0_TRUST_POLICY_NAME,
        policy_version=STAGE0_TRUST_POLICY_VERSION,
        allowed=False,
        reason=reason,
    )
