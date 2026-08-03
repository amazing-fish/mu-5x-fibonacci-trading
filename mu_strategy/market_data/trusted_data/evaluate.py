from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from mu_strategy.market_data.trusted_data.contracts import (
    AvailabilityState,
    DatasetHealth,
    DatasetKey,
    FreshnessState,
    HealthReason,
    IntegrityState,
    RefreshAttemptStatus,
    SnapshotUsability,
    ValidationReport,
    derive_snapshot_usability,
)
from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy
from mu_strategy.market_data.trusted_data.validation import (
    aggregate_candles,
    clip_parent_candles_to_complete_base_window,
    normalize_and_validate_candles,
    validate_built_native_candles,
)
from mu_strategy.market_data.trusted_data.windowing import CandleWindowPlan, assess_requested_coverage, prune_candle_bundle, resolve_shared_window
from mu_strategy.models import Candle


VALIDATION_FAILURE_REASONS = {
    HealthReason.EMPTY,
    HealthReason.BUILT_EMPTY,
    HealthReason.NATIVE_EMPTY,
    HealthReason.BUILT_SAMPLE_COUNT_BELOW_MINIMUM,
    HealthReason.NATIVE_SAMPLE_COUNT_BELOW_MINIMUM,
    HealthReason.TIMESTAMP_MISALIGNED,
    HealthReason.TIMESTAMP_GAP,
    HealthReason.MISSING_IN_BUILT,
    HealthReason.MISSING_IN_NATIVE,
    HealthReason.OHLCV_MISMATCH,
    HealthReason.OHLCV_INVALID,
    HealthReason.CONTINUITY_GAP,
    HealthReason.CACHE_CONTENT_MISMATCH,
    HealthReason.INSUFFICIENT_COVERAGE,
}

ValidatedDatasetHook = Callable[[str, "DatasetEvaluationSeed", list[Candle]], str | None]
PostProcessHealthHook = Callable[[str, DatasetHealth, list[Candle], list[Candle]], DatasetHealth]


@dataclass(frozen=True)
class DatasetEvaluationSeed:
    key: DatasetKey
    source_file: Path
    candles: list[Candle]
    prefailed_reason: HealthReason | None = None
    empty_prefailed_reason: HealthReason | None = None
    prefailed_availability: AvailabilityState | None = None
    prefailed_freshness: FreshnessState = FreshnessState.STALE
    exception_reason: HealthReason | None = None
    empty_validation_reason: HealthReason | None = None
    error_type: str | None = None
    message: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatasetEvaluationResult:
    health_by_key: dict[tuple[str, str], DatasetHealth]
    candles_by_key: dict[tuple[str, str], list[Candle]]
    window_plan: CandleWindowPlan


@dataclass(frozen=True)
class PublicationHealthSummary:
    attempt_status: RefreshAttemptStatus
    snapshot_usability: SnapshotUsability
    total_count: int
    usable_count: int
    unusable_count: int
    zero_usable: bool
    partial_usable: bool
    all_usable: bool
    provider_failure_count: int
    cache_read_failure_count: int
    validation_failure_count: int


def evaluate_candle_bundle(
    *,
    symbol: str,
    intervals: tuple[str, ...],
    seeds_by_interval: dict[str, DatasetEvaluationSeed],
    days: int,
    now_ms: int,
    freshness_policy: FreshnessPolicy,
    on_validated_candles: ValidatedDatasetHook | None = None,
    post_process_health: PostProcessHealthHook | None = None,
    retain_invalid_candles_for_reasons: tuple[HealthReason, ...] = (),
    allow_timestamp_gap_built_native_inputs: bool = False,
    raise_os_errors: bool = False,
    raise_exceptions: tuple[type[Exception], ...] = (),
) -> DatasetEvaluationResult:
    raw_candles_by_interval = {
        interval: seeds_by_interval[interval].candles
        for interval in intervals
    }
    physical_candles_by_interval, physical_validation_by_interval = validate_physical_candle_bundle(
        intervals,
        raw_candles_by_interval,
    )
    window_plan = resolve_shared_window(raw_candles_by_interval, days=days)
    pruned_candles_by_interval = prune_candle_bundle(raw_candles_by_interval, plan=window_plan)
    logical_five = pruned_candles_by_interval.get("5m") or []
    if logical_five:
        for interval in ("15m", "1h"):
            if interval not in pruned_candles_by_interval:
                continue
            pruned_candles_by_interval[interval] = clip_parent_candles_to_complete_base_window(
                pruned_candles_by_interval[interval],
                base_candles=logical_five,
                interval=interval,
            )
    health_by_key: dict[tuple[str, str], DatasetHealth] = {}
    candles_by_key: dict[tuple[str, str], list[Candle]] = {}

    for interval in intervals:
        seed = seeds_by_interval[interval]
        key = seed.key.tuple()
        candles = pruned_candles_by_interval.get(interval) or []
        prefailed_reason = seed.prefailed_reason
        if prefailed_reason is None and seed.empty_prefailed_reason is not None and not candles:
            prefailed_reason = seed.empty_prefailed_reason
        if prefailed_reason is not None:
            health_by_key[key] = make_dataset_health(
                seed.key.symbol,
                interval,
                seed.source_file,
                candles,
                now_ms=now_ms,
                availability=seed.prefailed_availability or (AvailabilityState.AVAILABLE if candles else AvailabilityState.MISSING),
                integrity=IntegrityState.INVALID,
                freshness=seed.prefailed_freshness,
                reason=prefailed_reason,
                error_type=seed.error_type,
                message=seed.message,
                warnings=seed.warnings,
            )
            candles_by_key[key] = []
            continue

        try:
            normalized, validation = normalize_and_validate_candles(candles, interval=interval)
            if not validation.ok:
                reason = seed.empty_validation_reason if not normalized and seed.empty_validation_reason is not None else validation.reason
                health = make_dataset_health(
                    seed.key.symbol,
                    interval,
                    seed.source_file,
                    normalized,
                    now_ms=now_ms,
                    availability=AvailabilityState.AVAILABLE if normalized else AvailabilityState.MISSING,
                    integrity=IntegrityState.INVALID,
                    freshness=FreshnessState.STALE,
                    reason=reason,
                    validation=validation,
                    error_type=seed.error_type,
                    message=seed.message,
                    warnings=seed.warnings,
                )
                if post_process_health is not None:
                    health = post_process_health(interval, health, normalized, seed.candles)
                health_by_key[key] = health
                keep_invalid_candles = validation.reason in retain_invalid_candles_for_reasons and health.integrity == IntegrityState.INVALID
                candles_by_key[key] = normalized if keep_invalid_candles else []
                continue

            freshness = freshness_policy.assess(
                now_ms=now_ms,
                interval=interval,
                last_confirmed_open_time_ms=normalized[-1].open_time_ms if normalized else None,
            )
            health = make_dataset_health(
                seed.key.symbol,
                interval,
                seed.source_file,
                normalized,
                now_ms=now_ms,
                availability=AvailabilityState.AVAILABLE,
                integrity=IntegrityState.VALID,
                freshness=freshness.state,
                reason=freshness.reason,
                validation=validation,
                error_type=seed.error_type,
                message=seed.message,
                warnings=seed.warnings,
            )
            if post_process_health is not None:
                health = post_process_health(interval, health, normalized, seed.candles)
            health_by_key[key] = health
            candles_by_key[key] = normalized if health.integrity == IntegrityState.VALID else []
        except Exception as exc:
            if (raise_os_errors and isinstance(exc, OSError)) or isinstance(exc, raise_exceptions):
                raise
            failure_candles = candles
            reason = seed.exception_reason or HealthReason.REFRESH_FAILED
            failure = exception_failure(exc)
            health_by_key[key] = make_dataset_health(
                seed.key.symbol,
                interval,
                seed.source_file,
                failure_candles,
                now_ms=now_ms,
                availability=AvailabilityState.AVAILABLE if failure_candles else AvailabilityState.MISSING,
                integrity=IntegrityState.INVALID,
                freshness=FreshnessState.STALE,
                reason=reason,
                error_type=failure["error_type"],
                message=failure["message"],
                warnings=seed.warnings,
            )
            candles_by_key[key] = []

    apply_built_native_validation(
        symbol,
        health_by_key,
        candles_by_key,
        allow_timestamp_gap_inputs=allow_timestamp_gap_built_native_inputs,
    )
    apply_requested_coverage_gate(
        symbol,
        intervals,
        health_by_key,
        candles_by_key,
        days=days,
        window_end_time_ms=window_plan.end_time_ms,
    )
    apply_physical_validation_gate(
        symbol,
        intervals,
        health_by_key,
        candles_by_key,
        physical_validation_by_interval,
    )
    if on_validated_candles is not None:
        for interval in intervals:
            seed = seeds_by_interval[interval]
            key = seed.key.tuple()
            health = health_by_key.get(key)
            candles = candles_by_key.get(key) or []
            if (
                health is None
                or health.availability is not AvailabilityState.AVAILABLE
                or health.integrity is not IntegrityState.VALID
                or not candles
            ):
                continue
            try:
                content_sha256 = on_validated_candles(
                    interval,
                    replace(seed, candles=physical_candles_by_interval[interval]),
                    candles,
                )
            except Exception as exc:
                if (raise_os_errors and isinstance(exc, OSError)) or isinstance(exc, raise_exceptions):
                    raise
                failure = exception_failure(exc)
                health_by_key[key] = make_dataset_health(
                    seed.key.symbol,
                    interval,
                    seed.source_file,
                    candles,
                    now_ms=now_ms,
                    availability=AvailabilityState.AVAILABLE,
                    integrity=IntegrityState.INVALID,
                    freshness=FreshnessState.STALE,
                    reason=seed.exception_reason or HealthReason.REFRESH_FAILED,
                    error_type=failure["error_type"],
                    message=failure["message"],
                    warnings=seed.warnings,
                )
                candles_by_key[key] = []
            else:
                health_by_key[key] = replace(health, content_sha256=content_sha256)
    return DatasetEvaluationResult(health_by_key, candles_by_key, window_plan)


def validate_physical_candle_bundle(
    intervals: tuple[str, ...],
    raw_candles_by_interval: dict[str, list[Candle]],
) -> tuple[dict[str, list[Candle]], dict[str, ValidationReport]]:
    normalized_by_interval: dict[str, list[Candle]] = {}
    validation_by_interval: dict[str, ValidationReport] = {}
    for interval in intervals:
        normalized, report = normalize_and_validate_candles(
            raw_candles_by_interval.get(interval) or [],
            interval=interval,
        )
        normalized_by_interval[interval] = normalized
        validation_by_interval[interval] = report

    base_report = validation_by_interval.get("5m")
    five = normalized_by_interval.get("5m") or []
    if base_report is None or not base_report.ok or not five:
        if base_report is not None and not base_report.ok:
            for interval in ("15m", "1h"):
                native_report = validation_by_interval.get(interval)
                if native_report is None or not native_report.ok:
                    continue
                validation_by_interval[interval] = ValidationReport(
                    False,
                    base_report.reason,
                    warnings=("physical 5m validation failed; built/native validation is unavailable",),
                )
        return normalized_by_interval, validation_by_interval
    for interval in ("15m", "1h"):
        native_report = validation_by_interval.get(interval)
        if native_report is None or not native_report.ok:
            continue
        try:
            built = aggregate_candles(five, interval=interval, ohlc_policy="okx_native")
        except ValueError as exc:
            report = ValidationReport(False, HealthReason.OHLCV_INVALID, warnings=(str(exc),))
        else:
            built = clip_parent_candles_to_complete_base_window(
                built,
                base_candles=five,
                interval=interval,
            )
            native = normalized_by_interval.get(interval) or []
            compared_native = clip_parent_candles_to_complete_base_window(
                native,
                base_candles=five,
                interval=interval,
            )
            normalized_by_interval[interval] = compared_native
            report = validate_built_native_candles(built, compared_native, interval=interval)
        validation_by_interval[interval] = report
    return normalized_by_interval, validation_by_interval


def apply_physical_validation_gate(
    symbol: str,
    intervals: tuple[str, ...],
    health_by_key: dict[tuple[str, str], DatasetHealth],
    candles_by_key: dict[tuple[str, str], list[Candle]],
    validation_by_interval: dict[str, ValidationReport],
) -> None:
    for interval in intervals:
        key = (symbol, interval)
        health = health_by_key.get(key)
        report = validation_by_interval.get(interval)
        if (
            health is None
            or report is None
            or report.ok
            or health.availability is not AvailabilityState.AVAILABLE
            or health.integrity is not IntegrityState.VALID
        ):
            continue
        health_by_key[key] = replace(
            health,
            integrity=IntegrityState.INVALID,
            freshness=FreshnessState.STALE,
            reasons=(report.reason,),
            validation=report,
        )
        candles_by_key[key] = []


def classify_publication_health(
    datasets: dict[tuple[str, str], DatasetHealth],
    *,
    provider_failures: tuple[dict[str, str], ...] = (),
) -> PublicationHealthSummary:
    total_count = len(datasets)
    usable_count = sum(1 for health in datasets.values() if health.is_usable)
    unusable_count = total_count - usable_count
    zero_usable = total_count == 0 or usable_count == 0
    partial_usable = 0 < usable_count < total_count
    all_usable = total_count > 0 and usable_count == total_count
    cache_read_failure_count = sum(1 for health in datasets.values() if health.primary_reason == HealthReason.CACHE_READ_FAILED)
    validation_failure_count = sum(1 for health in datasets.values() if health.primary_reason in VALIDATION_FAILURE_REASONS)
    snapshot_usability = derive_snapshot_usability(datasets)
    if zero_usable:
        attempt_status = RefreshAttemptStatus.FAILED
    elif partial_usable:
        attempt_status = RefreshAttemptStatus.DEGRADED
    elif provider_failures or cache_read_failure_count or validation_failure_count:
        attempt_status = RefreshAttemptStatus.DEGRADED
    else:
        attempt_status = RefreshAttemptStatus.SUCCESS
    return PublicationHealthSummary(
        attempt_status=attempt_status,
        snapshot_usability=snapshot_usability,
        total_count=total_count,
        usable_count=usable_count,
        unusable_count=unusable_count,
        zero_usable=zero_usable,
        partial_usable=partial_usable,
        all_usable=all_usable,
        provider_failure_count=len(provider_failures),
        cache_read_failure_count=cache_read_failure_count,
        validation_failure_count=validation_failure_count,
    )


def apply_built_native_validation(
    symbol: str,
    health_by_key: dict[tuple[str, str], DatasetHealth],
    candles_by_key: dict[tuple[str, str], list[Candle]],
    *,
    allow_timestamp_gap_inputs: bool = False,
) -> None:
    base_health = health_by_key.get((symbol, "5m"))
    if base_health is None or not _has_validation_inputs(base_health, allow_timestamp_gap_inputs=allow_timestamp_gap_inputs):
        return
    five = candles_by_key.get((symbol, "5m")) or []
    for interval in ("15m", "1h"):
        key = (symbol, interval)
        native_health = health_by_key.get(key)
        if native_health is None or not _has_validation_inputs(native_health, allow_timestamp_gap_inputs=allow_timestamp_gap_inputs):
            continue
        try:
            built = aggregate_candles(five, interval=interval, ohlc_policy="okx_native")
        except ValueError as exc:
            report = ValidationReport(False, HealthReason.OHLCV_INVALID, warnings=(str(exc),))
        else:
            built = clip_parent_candles_to_complete_base_window(built, base_candles=five, interval=interval)
            native = clip_parent_candles_to_complete_base_window(candles_by_key.get(key) or [], base_candles=five, interval=interval)
            report = validate_built_native_candles(
                built,
                native,
                interval=interval,
            )
        if report.ok and native_health.integrity != IntegrityState.VALID:
            continue
        health_by_key[key] = replace(
            native_health,
            integrity=IntegrityState.VALID if report.ok else IntegrityState.INVALID,
            freshness=native_health.freshness if report.ok else FreshnessState.STALE,
            reasons=(native_health.primary_reason if report.ok else report.reason,),
            validation=report,
        )
        if not report.ok:
            candles_by_key[key] = []


def apply_requested_coverage_gate(
    symbol: str,
    intervals: tuple[str, ...],
    health_by_key: dict[tuple[str, str], DatasetHealth],
    candles_by_key: dict[tuple[str, str], list[Candle]],
    *,
    days: int,
    window_end_time_ms: int | None,
) -> None:
    for interval in intervals:
        key = (symbol, interval)
        health = health_by_key.get(key)
        candles = candles_by_key.get(key) or []
        if health is None or not candles:
            continue
        if health.availability != AvailabilityState.AVAILABLE or health.integrity != IntegrityState.VALID:
            continue
        coverage = assess_requested_coverage(
            candles,
            interval=interval,
            requested_days=days,
            window_end_time_ms=window_end_time_ms,
        )
        warning = _coverage_warning(coverage)
        health_by_key[key] = replace(
            health,
            requested_days=coverage.requested_days,
            effective_days=coverage.effective_days,
            coverage_state=coverage.coverage_state,
            warnings=(*health.warnings, warning) if warning and warning not in health.warnings else health.warnings,
        )


def make_dataset_health(
    symbol: str,
    interval: str,
    path: Path,
    candles: list[Candle],
    *,
    now_ms: int,
    availability: AvailabilityState,
    integrity: IntegrityState,
    freshness: FreshnessState,
    reason: HealthReason,
    validation: ValidationReport | None = None,
    error_type: str | None = None,
    message: str | None = None,
    warnings: tuple[str, ...] = (),
    content_sha256: str | None = None,
) -> DatasetHealth:
    return DatasetHealth(
        key=DatasetKey(symbol, interval),
        availability=availability,
        integrity=integrity,
        freshness=freshness,
        reasons=(reason,),
        rows=len(candles),
        first_timestamp_ms=candles[0].open_time_ms if candles else None,
        last_timestamp_ms=candles[-1].open_time_ms if candles else None,
        updated_at_ms=now_ms,
        source_file=path,
        validation=validation,
        error_type=error_type,
        message=message,
        warnings=warnings,
        content_sha256=content_sha256,
    )


def _coverage_warning(coverage) -> str | None:
    if coverage.coverage_state != "partial_available_history" or coverage.effective_days is None:
        return None
    return f"partial_available_history:requested_days={coverage.requested_days}:effective_days={coverage.effective_days:.2f}"


def exception_failure(exc: Exception) -> dict[str, str]:
    error_type = type(exc).__name__
    message = str(exc).strip() or error_type
    return {"error_type": error_type, "message": message}


def _has_validation_inputs(health: DatasetHealth, *, allow_timestamp_gap_inputs: bool) -> bool:
    if (
        health.availability == AvailabilityState.AVAILABLE
        and health.integrity == IntegrityState.VALID
        and health.rows > 0
    ):
        return True
    return (
        allow_timestamp_gap_inputs
        and health.availability == AvailabilityState.AVAILABLE
        and health.rows > 0
        and health.validation is not None
        and health.validation.reason == HealthReason.TIMESTAMP_GAP
    )
