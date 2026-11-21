from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.market_data.trusted_data.compat import CandleBundle, candle_bundle_from_trusted_bundle
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
    TrustedConsumerRefreshError,
    TrustedBundle,
    TrustedLoadContext,
    ValidationReport,
)
from mu_strategy.market_data.trusted_data.evaluate import (
    DatasetEvaluationSeed,
    VALIDATION_FAILURE_REASONS,
    coverage_warning,
    evaluate_candle_bundle,
)
from mu_strategy.market_data.trusted_data.policy import (
    FreshnessPolicy,
    IntervalDependencyPlanner,
    TrustPolicy,
    research_strict_policy,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
from mu_strategy.market_data.trusted_data.windowing import assess_requested_coverage_bounds
from mu_strategy.market_data.utils import DAY_MS, interval_to_ms
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
        if context is None:
            context, manifest_result = self._open_context_result(now_ms=query.now_ms)
            if context is None:
                return TrustedBundle(
                    symbol=resolved.inst_id,
                    candles_by_interval={interval: [] for interval in plan.requested_intervals},
                    files_by_interval={
                        interval: self._unpublished_dataset_path(resolved.inst_id, interval)
                        for interval in plan.requested_intervals
                    },
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
        path_by_interval: dict[str, Path] = {}
        for interval in plan.effective_intervals:
            manifest_health = manifest.datasets.get((resolved.inst_id, interval))
            try:
                path = self._dataset_path(resolved.inst_id, interval, manifest_health, context)
            except ManifestSchemaError as exc:
                return TrustedBundle(
                    symbol=resolved.inst_id,
                    candles_by_interval={requested: [] for requested in plan.requested_intervals},
                    files_by_interval={
                        requested: self._default_dataset_path(resolved.inst_id, requested, context)
                        for requested in plan.requested_intervals
                    },
                    days=query.days,
                    health_by_interval={},
                    trust_decision=TrustDecision(False, HealthReason.MALFORMED_MANIFEST, str(exc)),
                    run_id=manifest.run_id,
                    universe_snapshot=manifest.universe_snapshot,
                    load_context=context,
                )
            path_by_interval[interval] = path
            if manifest_health is not None:
                published_health_by_interval[interval] = manifest_health

        # When 5m was not requested and both 5m and all requested native intervals are already
        # published and cross-validated in the manifest, cryptographically verify the 5m disk
        # bytes against the manifest SHA-256 directly without parsing 5m Candle objects or
        # re-aggregating 5m->15m/1h; fall back to full 5m load on any mismatch or read error.
        skip_base_disk_load = self._can_skip_base_disk_load(
            plan.requested_intervals,
            plan.effective_intervals,
            published_health_by_interval,
        )
        verified_base_hash: str | None = None
        if skip_base_disk_load:
            verified_base_hash = self.store.verify_generation_dataset_bytes(
                manifest,
                symbol=resolved.inst_id,
                interval="5m",
                generation_root=context.generation_root,
                generation_id=context.generation_id,
            )
            if verified_base_hash is None:
                skip_base_disk_load = False
        load_intervals = (
            self.planner.plan(query.intervals, include_base_for_validation=False).effective_intervals
            if skip_base_disk_load
            else plan.effective_intervals
        )

        seeds_by_interval: dict[str, DatasetEvaluationSeed] = {}
        precomputed_hashes: dict[str, str | None] = {}
        for interval in load_intervals:
            path = path_by_interval[interval]
            manifest_health = published_health_by_interval.get(interval)
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
            try:
                candles, content_hash = self.store.read_generation_dataset_with_hash(
                    manifest,
                    symbol=resolved.inst_id,
                    interval=interval,
                    generation_root=context.generation_root,
                    generation_id=context.generation_id,
                    verify_logical=False,
                    compute_hash=manifest_health.integrity == IntegrityState.VALID,
                )
            except Exception as exc:
                seeds_by_interval[interval] = DatasetEvaluationSeed(
                    key=DatasetKey(resolved.inst_id, interval),
                    source_file=path,
                    candles=[],
                    prefailed_reason=HealthReason.CACHE_READ_FAILED,
                    prefailed_availability=AvailabilityState.MISSING,
                    prefailed_freshness=FreshnessState.STALE,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                continue
            precomputed_hashes[interval] = content_hash
            seeds_by_interval[interval] = DatasetEvaluationSeed(
                key=DatasetKey(resolved.inst_id, interval),
                source_file=path,
                candles=candles,
                empty_validation_reason=HealthReason.CACHE_MISSING,
            )

        def merge_published_health(
            interval: str,
            health: DatasetHealth,
            candles: list[Candle],
            raw_candles: list[Candle],
        ) -> DatasetHealth:
            manifest_health = published_health_by_interval.get(interval)
            health = _verify_manifest_bound_content(
                health,
                manifest_health,
                raw_candles,
                precomputed_hash=precomputed_hashes.get(interval),
            )
            return _merge_manifest_health(health, manifest_health)

        window_anchor_bounds_ms: tuple[int, int] | None = None
        synthesized_base_health: DatasetHealth | None = None
        if skip_base_disk_load:
            manifest_five = published_health_by_interval["5m"]
            assert manifest_five.first_timestamp_ms is not None and manifest_five.last_timestamp_ms is not None
            assert verified_base_hash is not None
            window_anchor_bounds_ms = (manifest_five.first_timestamp_ms, manifest_five.last_timestamp_ms)
            synthesized_base_health = self._health_from_manifest_base(
                manifest_five,
                source_file=path_by_interval["5m"],
                days=query.days,
                now_ms=context.observed_at_ms,
                verified_content_sha256=verified_base_hash,
            )

        evaluation = evaluate_candle_bundle(
            symbol=resolved.inst_id,
            intervals=load_intervals,
            seeds_by_interval=seeds_by_interval,
            days=query.days,
            now_ms=context.observed_at_ms,
            freshness_policy=self.freshness_policy,
            post_process_health=merge_published_health,
            validate_physical=False,
            window_anchor_bounds_ms=window_anchor_bounds_ms,
        )

        health_by_interval: dict[str, DatasetHealth] = {}
        for interval in plan.effective_intervals:
            if interval == "5m" and synthesized_base_health is not None:
                health_by_interval["5m"] = synthesized_base_health
            else:
                health_by_interval[interval] = evaluation.health_by_key[(resolved.inst_id, interval)]
        candles_by_interval = {
            interval: evaluation.candles_by_key.get((resolved.inst_id, interval), [])
            for interval in load_intervals
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

    def _can_skip_base_disk_load(
        self,
        requested_intervals: tuple[str, ...],
        effective_intervals: tuple[str, ...],
        published_health_by_interval: dict[str, DatasetHealth],
    ) -> bool:
        if "5m" in requested_intervals or "5m" not in effective_intervals:
            return False
        manifest_five = published_health_by_interval.get("5m")
        if (
            manifest_five is None
            or manifest_five.availability != AvailabilityState.AVAILABLE
            or manifest_five.integrity != IntegrityState.VALID
            or not manifest_five.content_sha256
            or manifest_five.rows <= 0
            or manifest_five.first_timestamp_ms is None
            or manifest_five.last_timestamp_ms is None
        ):
            return False
        for interval in requested_intervals:
            health = published_health_by_interval.get(interval)
            if (
                health is None
                or health.availability != AvailabilityState.AVAILABLE
                or health.integrity != IntegrityState.VALID
                or not health.content_sha256
                or health.validation is None
                or not health.validation.ok
            ):
                return False
        return True

    def _health_from_manifest_base(
        self,
        manifest_five: DatasetHealth,
        *,
        source_file: Path,
        days: int,
        now_ms: int,
        verified_content_sha256: str,
    ) -> DatasetHealth:
        assert manifest_five.first_timestamp_ms is not None and manifest_five.last_timestamp_ms is not None
        assert verified_content_sha256 == manifest_five.content_sha256
        step_ms = interval_to_ms("5m")
        window_end_ms = manifest_five.last_timestamp_ms
        window_start_ms = window_end_ms - (days * DAY_MS)
        rem = window_start_ms % step_ms
        aligned_start_ms = window_start_ms if rem == 0 else window_start_ms + (step_ms - rem)
        actual_start_ms = max(manifest_five.first_timestamp_ms, aligned_start_ms)
        pruned_rows = min(manifest_five.rows, ((window_end_ms - actual_start_ms) // step_ms) + 1)

        freshness = self.freshness_policy.assess(
            now_ms=now_ms,
            interval="5m",
            last_confirmed_open_time_ms=window_end_ms,
        )
        local_health = DatasetHealth(
            key=manifest_five.key,
            availability=AvailabilityState.AVAILABLE,
            integrity=IntegrityState.VALID,
            freshness=freshness.state,
            reasons=(freshness.reason,),
            rows=pruned_rows,
            first_timestamp_ms=actual_start_ms,
            last_timestamp_ms=window_end_ms,
            updated_at_ms=now_ms,
            source_file=source_file,
            validation=manifest_five.validation or ValidationReport(True, HealthReason.OK),
            content_sha256=verified_content_sha256,
        )
        merged = _merge_manifest_health(local_health, manifest_five)
        coverage = assess_requested_coverage_bounds(
            actual_start_ms,
            window_end_ms,
            interval="5m",
            requested_days=days,
            window_end_time_ms=window_end_ms,
        )
        warning = coverage_warning(coverage)
        return replace(
            merged,
            requested_days=coverage.requested_days,
            effective_days=coverage.effective_days,
            coverage_state=coverage.coverage_state,
            warnings=(*merged.warnings, warning) if warning and warning not in merged.warnings else merged.warnings,
        )

    def _open_context_result(
        self,
        *,
        now_ms: int | None = None,
    ):
        observed_at_ms = int(now_ms if now_ms is not None else self.clock.now_ms())
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
        return self.store.dataset_path(
            context.manifest,
            symbol=symbol,
            interval=interval,
            generation_root=context.generation_root,
            generation_id=context.generation_id,
        )

    def _default_dataset_path(self, symbol: str, interval: str, context: TrustedLoadContext) -> Path:
        return self.store.generation_cache_path(context.generation_id, symbol, interval)

    def _unpublished_dataset_path(self, symbol: str, interval: str) -> Path:
        return self.store.generations_dir / "unpublished" / self.store.generation_source_file(symbol, interval)


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
    *,
    precomputed_hash: str | None = None,
) -> DatasetHealth:
    expected_hash = manifest_health.content_sha256 if manifest_health is not None else None
    if manifest_health is None or cache_health.integrity != IntegrityState.VALID or not cached_candles:
        return cache_health
    if manifest_health.integrity != IntegrityState.VALID:
        return cache_health
    actual_hash = precomputed_hash if precomputed_hash is not None else candles_content_sha256(cached_candles)
    if len(cached_candles) != manifest_health.rows:
        return replace(
            cache_health,
            integrity=IntegrityState.INVALID,
            freshness=FreshnessState.STALE,
            reasons=(HealthReason.CACHE_CONTENT_MISMATCH,),
            content_sha256=actual_hash,
            message="manifest dataset row count does not match cached candles",
        )
    if (
        cached_candles[0].open_time_ms != manifest_health.first_timestamp_ms
        or cached_candles[-1].open_time_ms != manifest_health.last_timestamp_ms
    ):
        return replace(
            cache_health,
            integrity=IntegrityState.INVALID,
            freshness=FreshnessState.STALE,
            reasons=(HealthReason.CACHE_CONTENT_MISMATCH,),
            content_sha256=actual_hash,
            message="manifest dataset timestamp bounds do not match cached candles",
        )
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


TRUSTED_CONSUMER_REFRESH_ERROR = (
    "trusted bundle loading is cache-only; run "
    "python -m mu_strategy.commands.refresh_market_data "
    "before loading trusted data"
)


def load_trusted_candle_bundle(
    symbol: str,
    *,
    intervals: tuple[str, ...] = ("15m", "1h"),
    days: int = 28,
    data_dir: Path = Path("data/live"),
    refresh: bool = False,
    policy: TrustPolicy | None = None,
    freshness_policy: FreshnessPolicy | None = None,
    max_staleness_bars: int = 3,
    clock: Clock | None = None,
    context: TrustedLoadContext | None = None,
) -> CandleBundle:
    if refresh:
        raise TrustedConsumerRefreshError(TRUSTED_CONSUMER_REFRESH_ERROR)
    resolved = resolve_okx_swap_symbol(symbol)
    store = TrustedDataStore(data_dir=Path(data_dir))
    requested_intervals = tuple(dict.fromkeys(intervals))
    resolved_freshness_policy = freshness_policy or FreshnessPolicy(max_staleness_bars=max_staleness_bars)
    bundle = LoadTrustedBundle(store, clock=clock, freshness_policy=resolved_freshness_policy).execute(
        LoadTrustedBundleQuery(
            resolved.inst_id,
            intervals=requested_intervals,
            days=days,
        ),
        policy or research_strict_policy(),
        context=context,
    )
    return candle_bundle_from_trusted_bundle(resolved, bundle)
