from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.market_data.trusted_data.contracts import (
    AvailabilityState,
    Clock,
    DatasetHealth,
    DatasetKey,
    FreshnessState,
    HealthReason,
    IntegrityState,
    ManifestSchemaError,
    SystemClock,
    TrustDecision,
    TrustedBundle,
    TrustedLoadContext,
    ValidationReport,
)
from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy, IntervalDependencyPlanner, TrustPolicy
from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
from mu_strategy.market_data.trusted_data.validation import (
    aggregate_candles,
    normalize_and_validate_candles,
    validate_built_native_candles,
)
from mu_strategy.market_data.trusted_data.windowing import prune_candle_bundle, resolve_shared_window
from mu_strategy.models import Candle


_VALIDATION_FAILURE_REASONS = {
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
}


@dataclass(frozen=True)
class LoadTrustedBundleQuery:
    symbol: str
    intervals: tuple[str, ...]
    days: int
    now_ms: int | None = None
    compatibility_mode: bool = False


class LoadTrustedBundle:
    def __init__(
        self,
        store: TrustedDataStore,
        *,
        planner: IntervalDependencyPlanner | None = None,
        freshness_policy: FreshnessPolicy | None = None,
        clock: Clock | None = None,
    ):
        self.store = store
        self.planner = planner or IntervalDependencyPlanner()
        self.freshness_policy = freshness_policy or FreshnessPolicy(max_staleness_bars=3)
        self.clock = clock or SystemClock()

    def open_context(
        self,
        *,
        now_ms: int | None = None,
        compatibility_mode: bool = False,
    ) -> TrustedLoadContext:
        context, manifest_result = self._open_context_result(now_ms=now_ms, compatibility_mode=compatibility_mode)
        if context is None:
            reason = manifest_result.reason or HealthReason.MANIFEST_BLOCKED
            message = manifest_result.message or reason.value
            raise RuntimeError(f"{reason.value}: {message}")
        return context

    def execute(
        self,
        query: LoadTrustedBundleQuery,
        policy: TrustPolicy,
        *,
        context: TrustedLoadContext | None = None,
    ) -> TrustedBundle:
        resolved = resolve_okx_swap_symbol(query.symbol)
        plan = self.planner.plan(query.intervals)
        if context is None:
            context, manifest_result = self._open_context_result(now_ms=query.now_ms, compatibility_mode=query.compatibility_mode)
            if context is None:
                return TrustedBundle(
                    symbol=resolved.inst_id,
                    candles_by_interval={interval: [] for interval in plan.requested_intervals},
                    files_by_interval={interval: self.store.flat_cache_path(resolved.inst_id, interval) for interval in plan.requested_intervals},
                    days=query.days,
                    health_by_interval={},
                    trust_decision=TrustDecision(
                        False,
                        manifest_result.reason or HealthReason.MANIFEST_BLOCKED,
                        manifest_result.message,
                    ),
                )

        manifest = context.manifest
        published_health_by_interval: dict[str, DatasetHealth] = {}
        raw_candles_by_interval: dict[str, list[Candle]] = {}
        candles_by_interval: dict[str, list[Candle]] = {}
        health_by_interval: dict[str, DatasetHealth] = {}
        path_by_interval: dict[str, Path] = {}
        for interval in plan.effective_intervals:
            manifest_health = manifest.datasets.get((resolved.inst_id, interval))
            try:
                path = self._dataset_path(resolved.inst_id, interval, manifest_health, context)
            except ManifestSchemaError as exc:
                return TrustedBundle(
                    symbol=resolved.inst_id,
                    candles_by_interval={requested: [] for requested in plan.requested_intervals},
                    files_by_interval={requested: self._default_dataset_path(resolved.inst_id, requested, context) for requested in plan.requested_intervals},
                    days=query.days,
                    health_by_interval={},
                    trust_decision=TrustDecision(False, HealthReason.MALFORMED_MANIFEST, str(exc)),
                    run_id=manifest.run_id,
                    universe_snapshot=manifest.universe_snapshot,
                    load_context=context,
                )
            path_by_interval[interval] = path
            if manifest_health is None:
                health_by_interval[interval] = _not_published_health(resolved.inst_id, interval, path)
                raw_candles_by_interval[interval] = []
                continue
            published_health_by_interval[interval] = manifest_health
            try:
                raw_candles_by_interval[interval] = self.store.read_csv(path) if path.exists() else []
            except Exception as exc:
                health_by_interval[interval] = _failed_health(
                    resolved.inst_id,
                    interval,
                    path,
                    reason=HealthReason.CACHE_READ_FAILED,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                raw_candles_by_interval[interval] = []

        window_plan = resolve_shared_window(raw_candles_by_interval, days=query.days)
        pruned_candles_by_interval = prune_candle_bundle(raw_candles_by_interval, plan=window_plan)
        for interval in plan.effective_intervals:
            if interval in health_by_interval:
                candles_by_interval[interval] = []
                continue
            path = path_by_interval[interval]
            candles = pruned_candles_by_interval.get(interval) or []
            candles, validation = normalize_and_validate_candles(candles, interval=interval)
            if not validation.ok:
                health = _health_from_candles(
                    resolved.inst_id,
                    interval,
                    path,
                    candles,
                    reason=validation.reason,
                    integrity=IntegrityState.INVALID,
                    freshness=FreshnessState.STALE,
                    validation=validation,
                    updated_at_ms=context.observed_at_ms,
                )
            else:
                freshness = self.freshness_policy.assess(
                    now_ms=context.observed_at_ms,
                    interval=interval,
                    last_confirmed_open_time_ms=candles[-1].open_time_ms if candles else None,
                )
                health = _health_from_candles(
                    resolved.inst_id,
                    interval,
                    path,
                    candles,
                    reason=freshness.reason,
                    integrity=IntegrityState.VALID,
                    freshness=freshness.state,
                    validation=validation,
                    updated_at_ms=context.observed_at_ms,
                )
            manifest_health = published_health_by_interval.get(interval)
            cached_candles = raw_candles_by_interval.get(interval) or []
            health = _verify_manifest_bound_content(health, manifest_health, cached_candles)
            health = _merge_manifest_health(health, manifest_health)
            health_by_interval[interval] = health
            candles_by_interval[interval] = candles if health.integrity == IntegrityState.VALID else []

        self._attach_built_native_validation(health_by_interval, candles_by_interval)
        decision = policy.decide(
            context=context,
            health_by_interval=health_by_interval,
            required_intervals=plan.effective_intervals,
        )
        requested_candles = {
            interval: candles_by_interval.get(interval, [])
            for interval in plan.requested_intervals
        }
        requested_files = {
            interval: path_by_interval.get(interval, self._default_dataset_path(resolved.inst_id, interval, context))
            for interval in plan.requested_intervals
        }
        return TrustedBundle(
            symbol=resolved.inst_id,
            candles_by_interval=requested_candles,
            files_by_interval=requested_files,
            days=query.days,
            health_by_interval=health_by_interval,
            trust_decision=decision,
            run_id=manifest.run_id,
            universe_snapshot=manifest.universe_snapshot,
            load_context=context,
        )

    def _open_context_result(
        self,
        *,
        now_ms: int | None = None,
        compatibility_mode: bool = False,
    ):
        observed_at_ms = int(now_ms if now_ms is not None else self.clock.now_ms())
        manifest_result = self.store.read_manifest(compatibility_mode=compatibility_mode)
        if not manifest_result.ok or manifest_result.snapshot is None:
            return None, manifest_result
        return (
            TrustedLoadContext(
                manifest=manifest_result.snapshot,
                observed_at_ms=observed_at_ms,
                generation_root=manifest_result.generation_root or self.store.data_dir,
                generation_id=manifest_result.generation_id,
            ),
            manifest_result,
        )

    def _dataset_path(
        self,
        symbol: str,
        interval: str,
        manifest_health: DatasetHealth | None,
        context: TrustedLoadContext,
    ) -> Path:
        if manifest_health is None:
            return self._default_dataset_path(symbol, interval, context)
        if context.generation_id is None:
            return self.store.flat_cache_path(symbol, interval)
        expected = self.store.generation_source_file(symbol, interval)
        if manifest_health.source_file.as_posix() != expected.as_posix():
            raise ManifestSchemaError("generation manifest source_file must equal okx/<symbol>/<interval>.csv")
        return self.store.generation_cache_path(context.generation_id, symbol, interval)

    def _default_dataset_path(self, symbol: str, interval: str, context: TrustedLoadContext) -> Path:
        if context.generation_id is None:
            return self.store.flat_cache_path(symbol, interval)
        return self.store.generation_cache_path(context.generation_id, symbol, interval)

    def _attach_built_native_validation(
        self,
        health_by_interval: dict[str, DatasetHealth],
        candles_by_interval: dict[str, list[Candle]],
    ) -> None:
        base_health = health_by_interval.get("5m")
        if base_health is None or not _has_validation_inputs(base_health):
            return
        five = candles_by_interval.get("5m") or []
        for interval in ("15m", "1h"):
            native_health = health_by_interval.get(interval)
            if native_health is None or not _has_validation_inputs(native_health):
                continue
            report = validate_built_native_candles(
                aggregate_candles(five, interval=interval),
                candles_by_interval.get(interval) or [],
                interval=interval,
            )
            health_by_interval[interval] = replace(
                native_health,
                integrity=IntegrityState.VALID if report.ok else IntegrityState.INVALID,
                freshness=native_health.freshness if report.ok else FreshnessState.STALE,
                reasons=(native_health.primary_reason if report.ok else report.reason,),
                validation=report,
            )
            if not report.ok:
                candles_by_interval[interval] = []


def _merge_manifest_health(cache_health: DatasetHealth, manifest_health: DatasetHealth | None) -> DatasetHealth:
    if manifest_health is None:
        return _ensure_validation_report_consistency(cache_health)
    merged = replace(
        cache_health,
        availability=_worst_availability(cache_health.availability, manifest_health.availability),
        integrity=_worst_integrity(cache_health.integrity, manifest_health.integrity),
        freshness=_worst_freshness(cache_health.freshness, manifest_health.freshness),
        reasons=_merge_reasons(manifest_health.reasons, cache_health.reasons),
        validation=_merge_validation_report(cache_health, manifest_health),
        error_type=manifest_health.error_type or cache_health.error_type,
        message=manifest_health.message or cache_health.message,
        warnings=_merge_warnings(manifest_health.warnings, cache_health.warnings),
        content_sha256=cache_health.content_sha256 or manifest_health.content_sha256,
    )
    return _ensure_validation_report_consistency(merged)


def _verify_manifest_bound_content(
    cache_health: DatasetHealth,
    manifest_health: DatasetHealth | None,
    cached_candles: list[Candle],
) -> DatasetHealth:
    expected_hash = manifest_health.content_sha256 if manifest_health is not None else None
    if manifest_health is None or cache_health.integrity != IntegrityState.VALID or not cached_candles:
        return cache_health
    if manifest_health.integrity != IntegrityState.VALID:
        return cache_health
    actual_hash = candles_content_sha256(cached_candles)
    if not expected_hash:
        return replace(
            cache_health,
            integrity=IntegrityState.INVALID,
            freshness=FreshnessState.STALE,
            reasons=(HealthReason.CACHE_CONTENT_MISMATCH,),
            content_sha256=actual_hash,
            message="manifest dataset is missing content_sha256",
        )
    if actual_hash == expected_hash:
        return replace(cache_health, content_sha256=actual_hash)
    return replace(
        cache_health,
        integrity=IntegrityState.INVALID,
        freshness=FreshnessState.STALE,
        reasons=(HealthReason.CACHE_CONTENT_MISMATCH,),
        content_sha256=actual_hash,
    )


def _merge_validation_report(
    cache_health: DatasetHealth,
    manifest_health: DatasetHealth,
) -> ValidationReport | None:
    manifest_validation = manifest_health.validation
    cache_validation = cache_health.validation
    if manifest_validation is not None and not manifest_validation.ok:
        return manifest_validation
    if cache_validation is not None and not cache_validation.ok:
        return cache_validation
    if cache_validation is not None:
        return cache_validation
    return manifest_validation


def _ensure_validation_report_consistency(health: DatasetHealth) -> DatasetHealth:
    reason = health.primary_reason
    if reason not in _VALIDATION_FAILURE_REASONS:
        return health
    validation = health.validation
    if validation is not None and not validation.ok and validation.reason == reason:
        return health
    return replace(health, validation=ValidationReport(False, reason))


def _not_published_health(symbol: str, interval: str, path: Path) -> DatasetHealth:
    return DatasetHealth(
        key=DatasetKey(symbol, interval),
        availability=AvailabilityState.MISSING,
        integrity=IntegrityState.INVALID,
        freshness=FreshnessState.UNKNOWN,
        reasons=(HealthReason.NOT_PUBLISHED,),
        rows=0,
        first_timestamp_ms=None,
        last_timestamp_ms=None,
        source_file=path,
    )


def _health_from_candles(
    symbol: str,
    interval: str,
    path: Path,
    candles: list[Candle],
    *,
    reason: HealthReason,
    integrity: IntegrityState,
    freshness: FreshnessState,
    validation,
    updated_at_ms: int = 0,
) -> DatasetHealth:
    return DatasetHealth(
        key=DatasetKey(symbol, interval),
        availability=AvailabilityState.AVAILABLE if candles else AvailabilityState.MISSING,
        integrity=integrity if candles else IntegrityState.INVALID,
        freshness=freshness,
        reasons=(reason if candles else HealthReason.CACHE_MISSING,),
        rows=len(candles),
        first_timestamp_ms=candles[0].open_time_ms if candles else None,
        last_timestamp_ms=candles[-1].open_time_ms if candles else None,
        source_file=path,
        validation=validation,
        updated_at_ms=updated_at_ms,
        content_sha256=candles_content_sha256(candles) if candles and integrity == IntegrityState.VALID else None,
    )


def _failed_health(
    symbol: str,
    interval: str,
    path: Path,
    *,
    reason: HealthReason,
    error_type: str,
    message: str,
) -> DatasetHealth:
    return DatasetHealth(
        key=DatasetKey(symbol, interval),
        availability=AvailabilityState.MISSING,
        integrity=IntegrityState.INVALID,
        freshness=FreshnessState.STALE,
        reasons=(reason,),
        rows=0,
        first_timestamp_ms=None,
        last_timestamp_ms=None,
        source_file=path,
        error_type=error_type,
        message=message,
    )


def _worst_availability(left: AvailabilityState, right: AvailabilityState) -> AvailabilityState:
    if AvailabilityState.MISSING in {left, right}:
        return AvailabilityState.MISSING
    return AvailabilityState.AVAILABLE


def _worst_integrity(left: IntegrityState, right: IntegrityState) -> IntegrityState:
    order = {
        IntegrityState.VALID: 0,
        IntegrityState.UNKNOWN: 1,
        IntegrityState.INVALID: 2,
    }
    return left if order[left] >= order[right] else right


def _worst_freshness(left: FreshnessState, right: FreshnessState) -> FreshnessState:
    order = {
        FreshnessState.FRESH: 0,
        FreshnessState.UNKNOWN: 1,
        FreshnessState.STALE: 2,
    }
    return left if order[left] >= order[right] else right


def _merge_reasons(*groups: tuple[HealthReason, ...]) -> tuple[HealthReason, ...]:
    values: list[HealthReason] = []
    for group in groups:
        for reason in group:
            if reason not in values:
                values.append(reason)
    non_ok = [reason for reason in values if reason != HealthReason.OK]
    return tuple(non_ok or [HealthReason.OK])


def _merge_warnings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for group in groups:
        for warning in group:
            if warning not in values:
                values.append(warning)
    return tuple(values)


def _has_validation_inputs(health: DatasetHealth) -> bool:
    return (
        health.availability == AvailabilityState.AVAILABLE
        and health.integrity == IntegrityState.VALID
        and health.rows > 0
    )
