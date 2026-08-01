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
    TrustedDatasetFileLease,
    TrustedDatasetFileSnapshot,
    TrustedLoadContext,
    TrustedManifestSnapshot,
    ValidationReport,
)
from mu_strategy.market_data.trusted_data.evaluate import DatasetEvaluationSeed, VALIDATION_FAILURE_REASONS, evaluate_candle_bundle
from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy, IntervalDependencyPlanner, TrustPolicy
from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
from mu_strategy.models import Candle


@dataclass(frozen=True)
class LoadTrustedBundleQuery:
    symbol: str
    intervals: tuple[str, ...]
    days: int
    now_ms: int | None = None


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
    ) -> TrustedLoadContext:
        context, manifest_result = self._open_context_result(now_ms=now_ms)
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
        required_keys = tuple(
            DatasetKey(resolved.inst_id, interval)
            for interval in plan.effective_intervals
        )
        if context is None:
            context, manifest_result = self._open_context_result(
                now_ms=query.now_ms,
                dataset_keys=required_keys,
            )
            if context is None:
                return TrustedBundle(
                    symbol=resolved.inst_id,
                    candles_by_interval={interval: [] for interval in plan.requested_intervals},
                    files_by_interval={interval: self._unpublished_dataset_path(resolved.inst_id, interval) for interval in plan.requested_intervals},
                    days=query.days,
                    health_by_interval={},
                    trust_decision=TrustDecision(
                        False,
                        manifest_result.reason or HealthReason.MANIFEST_BLOCKED,
                        manifest_result.message,
                    ),
                )
        elif context.dataset_file_snapshots is not None:
            context = self._extend_context_snapshot(context, required_keys)

        manifest = context.manifest
        file_snapshots_by_key = None
        if context.dataset_file_snapshots is not None:
            file_snapshots_by_key = {
                snapshot.key: snapshot
                for snapshot in context.dataset_file_snapshots
            }
        published_health_by_interval: dict[str, DatasetHealth] = {}
        path_by_interval: dict[str, Path] = {}
        seeds_by_interval: dict[str, DatasetEvaluationSeed] = {}
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
                seeds_by_interval[interval] = DatasetEvaluationSeed(
                    key=DatasetKey(resolved.inst_id, interval),
                    source_file=path,
                    candles=[],
                    prefailed_reason=HealthReason.NOT_PUBLISHED,
                    prefailed_availability=AvailabilityState.MISSING,
                    prefailed_freshness=FreshnessState.UNKNOWN,
                )
                continue
            published_health_by_interval[interval] = manifest_health
            file_snapshot = (
                file_snapshots_by_key.get(DatasetKey(resolved.inst_id, interval))
                if file_snapshots_by_key is not None
                else None
            )
            try:
                if file_snapshots_by_key is None:
                    candles = self.store.read_csv(path) if path.exists() else []
                elif file_snapshot is None:
                    raise FileNotFoundError(f"pinned dataset snapshot is missing: {path}")
                elif file_snapshot.source_file != path:
                    raise ManifestSchemaError("pinned dataset snapshot path must match its manifest dataset")
                elif file_snapshot.payload is None:
                    error_type = file_snapshot.error_type or "FileNotFoundError"
                    message = file_snapshot.message or f"pinned dataset snapshot is unavailable: {path}"
                    raise _PinnedDatasetSnapshotError(error_type, message)
                else:
                    candles = self.store.read_csv_bytes(file_snapshot.payload)
            except Exception as exc:
                if isinstance(exc, _PinnedDatasetSnapshotError):
                    error_type = exc.error_type
                    message = exc.message
                else:
                    error_type = type(exc).__name__
                    message = str(exc)
                seeds_by_interval[interval] = DatasetEvaluationSeed(
                    key=DatasetKey(resolved.inst_id, interval),
                    source_file=path,
                    candles=[],
                    prefailed_reason=HealthReason.CACHE_READ_FAILED,
                    prefailed_availability=AvailabilityState.MISSING,
                    prefailed_freshness=FreshnessState.STALE,
                    error_type=error_type,
                    message=message,
                )
                continue
            seeds_by_interval[interval] = DatasetEvaluationSeed(
                key=DatasetKey(resolved.inst_id, interval),
                source_file=path,
                candles=candles,
                empty_validation_reason=HealthReason.CACHE_MISSING,
            )

        def merge_published_health(interval: str, health: DatasetHealth, candles: list[Candle], raw_candles: list[Candle]) -> DatasetHealth:
            manifest_health = published_health_by_interval.get(interval)
            health = _verify_manifest_bound_content(health, manifest_health, raw_candles)
            return _merge_manifest_health(health, manifest_health)

        evaluation = evaluate_candle_bundle(
            symbol=resolved.inst_id,
            intervals=plan.effective_intervals,
            seeds_by_interval=seeds_by_interval,
            days=query.days,
            now_ms=context.observed_at_ms,
            freshness_policy=self.freshness_policy,
            post_process_health=merge_published_health,
        )
        health_by_interval = {
            interval: evaluation.health_by_key[(resolved.inst_id, interval)]
            for interval in plan.effective_intervals
        }
        candles_by_interval = {
            interval: evaluation.candles_by_key.get((resolved.inst_id, interval), [])
            for interval in plan.effective_intervals
        }
        decision = policy.decide(
            context=context,
            health_by_interval=health_by_interval,
            required_intervals=plan.effective_intervals,
            freshness_intervals=plan.requested_intervals,
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
        dataset_keys: tuple[DatasetKey, ...] | None = None,
    ):
        observed_at_ms = int(now_ms) if now_ms is not None else None
        with self.store.publication_snapshot_lock():
            return self._open_context_result_locked(
                observed_at_ms,
                dataset_keys=dataset_keys,
            )

    def _open_context_result_locked(
        self,
        observed_at_ms: int | None,
        *,
        dataset_keys: tuple[DatasetKey, ...] | None,
    ):
        manifest_result = self.store.read_manifest()
        if not manifest_result.ok or manifest_result.snapshot is None:
            return None, manifest_result
        if manifest_result.generation_id is None:
            return None, replace(
                manifest_result,
                reason=HealthReason.MALFORMED_MANIFEST,
                error_type="ManifestSchemaError",
                message="trusted manifest must be pinned to a generation",
            )
        file_snapshots = self._snapshot_dataset_files_locked(
            manifest_result.snapshot,
            manifest_result.generation_id,
            dataset_keys=dataset_keys,
        )
        file_leases, fallback_snapshots = self._lease_unrequested_dataset_files_locked(
            manifest_result.snapshot,
            manifest_result.generation_id,
            dataset_keys=dataset_keys,
        )
        if observed_at_ms is None:
            observed_at_ms = int(self.clock.now_ms())
        return (
            TrustedLoadContext(
                manifest=manifest_result.snapshot,
                observed_at_ms=observed_at_ms,
                generation_root=manifest_result.generation_root or self.store.data_dir,
                generation_id=manifest_result.generation_id,
                dataset_file_snapshots=(*file_snapshots, *fallback_snapshots),
                dataset_file_leases=file_leases,
            ),
            manifest_result,
        )

    def _extend_context_snapshot(
        self,
        context: TrustedLoadContext,
        required_keys: tuple[DatasetKey, ...],
    ) -> TrustedLoadContext:
        existing_snapshots = context.dataset_file_snapshots or ()
        existing_keys = {snapshot.key for snapshot in existing_snapshots}
        missing_keys = tuple(key for key in required_keys if key not in existing_keys)
        if not missing_keys:
            return context
        leases_by_key = {
            lease.key: lease
            for lease in (context.dataset_file_leases or ())
        }
        added_snapshots = []
        remaining_keys = []
        for key in missing_keys:
            lease = leases_by_key.get(key)
            if lease is None:
                remaining_keys.append(key)
                continue
            try:
                payload = lease.read_bytes()
            except Exception as exc:
                added_snapshots.append(
                    TrustedDatasetFileSnapshot(
                        key=key,
                        source_file=lease.source_file,
                        payload=None,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
            else:
                added_snapshots.append(
                    TrustedDatasetFileSnapshot(
                        key=key,
                        source_file=lease.source_file,
                        payload=payload,
                    )
                )
        if remaining_keys:
            with self.store.publication_snapshot_lock():
                added_snapshots.extend(
                    self._snapshot_dataset_files_locked(
                        context.manifest,
                        context.generation_id,
                        dataset_keys=tuple(remaining_keys),
                    )
                )
        return replace(
            context,
            dataset_file_snapshots=(*existing_snapshots, *added_snapshots),
        )

    def _snapshot_dataset_files_locked(
        self,
        manifest: TrustedManifestSnapshot,
        generation_id: str,
        *,
        dataset_keys: tuple[DatasetKey, ...] | None,
    ) -> tuple[TrustedDatasetFileSnapshot, ...]:
        if dataset_keys is None:
            health_items = manifest.datasets.items()
        else:
            unique_keys = tuple(dict.fromkeys(dataset_keys))
            health_items = (
                (key.tuple(), manifest.datasets[key.tuple()])
                for key in unique_keys
                if key.tuple() in manifest.datasets
            )
        file_snapshots = []
        for (symbol, interval), health in sorted(health_items, key=lambda item: item[0]):
            path = self.store.generation_cache_path(generation_id, symbol, interval)
            try:
                payload = self.store.read_file_bytes(path)
            except Exception as exc:
                file_snapshots.append(
                    TrustedDatasetFileSnapshot(
                        key=health.key,
                        source_file=path,
                        payload=None,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
            else:
                file_snapshots.append(
                    TrustedDatasetFileSnapshot(
                        key=health.key,
                        source_file=path,
                        payload=payload,
                    )
                )
        return tuple(file_snapshots)

    def _lease_unrequested_dataset_files_locked(
        self,
        manifest: TrustedManifestSnapshot,
        generation_id: str,
        *,
        dataset_keys: tuple[DatasetKey, ...] | None,
    ) -> tuple[tuple[TrustedDatasetFileLease, ...], tuple[TrustedDatasetFileSnapshot, ...]]:
        if dataset_keys is None:
            return (), ()
        requested_keys = set(dataset_keys)
        leases = []
        fallback_snapshots = []
        try:
            for (symbol, interval), health in sorted(manifest.datasets.items(), key=lambda item: item[0]):
                if health.key in requested_keys:
                    continue
                path = self.store.generation_cache_path(generation_id, symbol, interval)
                try:
                    handle = self.store.open_file_for_snapshot(path)
                except Exception as lease_error:
                    try:
                        payload = self.store.read_file_bytes(path)
                    except Exception as snapshot_error:
                        if health.availability != AvailabilityState.AVAILABLE:
                            fallback_snapshots.append(
                                TrustedDatasetFileSnapshot(
                                    key=health.key,
                                    source_file=path,
                                    payload=None,
                                    error_type=type(snapshot_error).__name__,
                                    message=str(snapshot_error),
                                )
                            )
                            continue
                        raise RuntimeError(
                            f"unable to preserve trusted dataset {health.key.tuple()} after "
                            f"{type(lease_error).__name__}: {lease_error}"
                        ) from snapshot_error
                    fallback_snapshots.append(
                        TrustedDatasetFileSnapshot(
                            key=health.key,
                            source_file=path,
                            payload=payload,
                        )
                    )
                    continue
                leases.append(
                    TrustedDatasetFileLease(
                        key=health.key,
                        source_file=path,
                        handle=handle,
                    )
                )
        except Exception:
            for lease in leases:
                lease.close()
            raise
        return tuple(leases), tuple(fallback_snapshots)

    def _dataset_path(
        self,
        symbol: str,
        interval: str,
        manifest_health: DatasetHealth | None,
        context: TrustedLoadContext,
    ) -> Path:
        if manifest_health is None:
            return self._default_dataset_path(symbol, interval, context)
        expected = self.store.generation_source_file(symbol, interval)
        if manifest_health.source_file.as_posix() != expected.as_posix():
            raise ManifestSchemaError("generation manifest source_file must equal okx/<symbol>/<interval>.csv")
        return self.store.generation_cache_path(context.generation_id, symbol, interval)

    def _default_dataset_path(self, symbol: str, interval: str, context: TrustedLoadContext) -> Path:
        return self.store.generation_cache_path(context.generation_id, symbol, interval)

    def _unpublished_dataset_path(self, symbol: str, interval: str) -> Path:
        return self.store.generations_dir / "unpublished" / self.store.generation_source_file(symbol, interval)


class _PinnedDatasetSnapshotError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


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
    if reason not in VALIDATION_FAILURE_REASONS:
        return health
    validation = health.validation
    if validation is not None and not validation.ok and validation.reason == reason:
        return health
    return replace(health, validation=ValidationReport(False, reason))


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
