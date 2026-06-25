from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from mu_strategy.models import Candle


class AvailabilityState(Enum):
    AVAILABLE = "available"
    MISSING = "missing"


class IntegrityState(Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class FreshnessState(Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class RefreshAttemptStatus(Enum):
    SUCCESS = "success"
    DEGRADED = "degraded"
    FAILED = "failed"


class SnapshotUsability(Enum):
    USABLE = "usable"
    STALE = "stale"
    INVALID = "invalid"


class HealthReason(Enum):
    OK = "ok"
    NOT_PUBLISHED = "not_published"
    CACHE_MISSING = "cache_missing"
    CACHE_READ_FAILED = "cache_read_failed"
    EMPTY = "empty"
    REFRESH_FAILED = "refresh_failed"
    INCREMENTAL_REFRESH_FAILED = "incremental_refresh_failed"
    MANIFEST_MISSING = "manifest_missing"
    MALFORMED_MANIFEST = "malformed_manifest"
    MANIFEST_BLOCKED = "manifest_blocked"
    RUN_FAILED = "run_failed"
    RUN_DEGRADED = "run_degraded"
    MANIFEST_INVALID = "manifest_invalid"
    MANIFEST_STALE = "manifest_stale"
    FRESHNESS_UNKNOWN = "freshness_unknown"
    STALE_BY_CLOCK = "stale_by_clock"
    FUTURE_TIMESTAMP = "future_timestamp"
    BUILT_EMPTY = "built_empty"
    NATIVE_EMPTY = "native_empty"
    BUILT_SAMPLE_COUNT_BELOW_MINIMUM = "built_sample_count_below_minimum"
    NATIVE_SAMPLE_COUNT_BELOW_MINIMUM = "native_sample_count_below_minimum"
    TIMESTAMP_MISALIGNED = "timestamp_misaligned"
    TIMESTAMP_GAP = "timestamp_gap"
    MISSING_IN_BUILT = "missing_in_built"
    MISSING_IN_NATIVE = "missing_in_native"
    OHLCV_MISMATCH = "ohlcv_mismatch"
    OHLCV_INVALID = "ohlcv_invalid"
    CONTINUITY_GAP = "continuity_gap"
    CACHE_CONTENT_MISMATCH = "cache_content_mismatch"


class Clock(Protocol):
    def now_ms(self) -> int:
        ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class ManifestSchemaError(ValueError):
    pass


class TrustedConsumerRefreshError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatasetKey:
    symbol: str
    interval: str

    def tuple(self) -> tuple[str, str]:
        return (self.symbol, self.interval)


@dataclass(frozen=True)
class IntervalPlan:
    requested_intervals: tuple[str, ...]
    effective_intervals: tuple[str, ...]


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    reason: HealthReason = HealthReason.OK
    missing_in_built: tuple[int, ...] = ()
    missing_in_native: tuple[int, ...] = ()
    misaligned_timestamps: tuple[int, ...] = ()
    timestamp_gaps: tuple[dict[str, int], ...] = ()
    value_mismatches: tuple[dict[str, int | float | str], ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason.value,
            "missing_in_built": list(self.missing_in_built),
            "missing_in_native": list(self.missing_in_native),
            "misaligned_timestamps": list(self.misaligned_timestamps),
            "timestamp_gaps": [dict(value) for value in self.timestamp_gaps],
            "value_mismatches": list(self.value_mismatches),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None, *, strict: bool = False) -> "ValidationReport | None":
        if not isinstance(payload, dict):
            return None
        return cls(
            ok=bool(payload.get("ok")),
            reason=health_reason(payload.get("reason"), strict=strict),
            missing_in_built=tuple(int(value) for value in payload.get("missing_in_built") or ()),
            missing_in_native=tuple(int(value) for value in payload.get("missing_in_native") or ()),
            misaligned_timestamps=tuple(int(value) for value in payload.get("misaligned_timestamps") or ()),
            timestamp_gaps=tuple(dict(value) for value in payload.get("timestamp_gaps") or ()),
            value_mismatches=tuple(dict(value) for value in payload.get("value_mismatches") or ()),
            warnings=tuple(str(value) for value in payload.get("warnings") or ()),
        )


@dataclass(frozen=True)
class FreshnessAssessment:
    state: FreshnessState
    reason: HealthReason
    age_ms: int | None = None
    max_age_ms: int | None = None


@dataclass(frozen=True)
class DatasetHealth:
    key: DatasetKey
    availability: AvailabilityState
    integrity: IntegrityState
    freshness: FreshnessState
    reasons: tuple[HealthReason, ...]
    rows: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None
    source_file: Path
    validation: ValidationReport | None = None
    updated_at_ms: int = 0
    error_type: str | None = None
    message: str | None = None
    warnings: tuple[str, ...] = ()
    content_sha256: str | None = None

    @property
    def is_usable(self) -> bool:
        return (
            self.availability == AvailabilityState.AVAILABLE
            and self.integrity == IntegrityState.VALID
            and self.freshness == FreshnessState.FRESH
            and self.rows > 0
        )

    @property
    def primary_reason(self) -> HealthReason:
        return self.reasons[0] if self.reasons else HealthReason.OK

    def to_dict(self) -> dict[str, Any]:
        reason = self.primary_reason
        if reason == HealthReason.OK and self.freshness == FreshnessState.UNKNOWN:
            reason = HealthReason.FRESHNESS_UNKNOWN
        elif reason == HealthReason.OK and self.freshness == FreshnessState.STALE:
            reason = HealthReason.STALE_BY_CLOCK
        return {
            "symbol": self.key.symbol,
            "interval": self.key.interval,
            "availability": self.availability.value,
            "integrity": self.integrity.value,
            "freshness": self.freshness.value,
            "reasons": [reason.value for reason in self.reasons],
            "rows": self.rows,
            "first_timestamp_ms": self.first_timestamp_ms,
            "last_timestamp_ms": self.last_timestamp_ms,
            "updated_at_ms": self.updated_at_ms,
            "source_file": self.source_file.as_posix(),
            "is_valid": self.integrity == IntegrityState.VALID and self.availability == AvailabilityState.AVAILABLE,
            "is_stale": self.freshness != FreshnessState.FRESH,
            "reason": reason.value,
            "error_type": self.error_type,
            "message": self.message,
            "warnings": list(self.warnings),
            "content_sha256": self.content_sha256,
            "validation": self.validation.to_dict() if self.validation else None,
        }

    @classmethod
    def from_dict(cls, symbol: str, interval: str, payload: dict[str, Any], *, strict: bool = True) -> "DatasetHealth":
        if strict:
            payload_symbol = _required_str(payload, "symbol")
            payload_interval = _required_str(payload, "interval")
            if payload_symbol != symbol or payload_interval != interval:
                raise ManifestSchemaError("manifest dataset key must match payload symbol/interval")
            reasons = _required_str_tuple(payload, "reasons")
            availability = AvailabilityState(_required_str(payload, "availability"))
            integrity = IntegrityState(_required_str(payload, "integrity"))
            freshness = FreshnessState(_required_str(payload, "freshness"))
            rows = _required_int(payload, "rows")
        else:
            payload_symbol = str(payload.get("symbol") or symbol)
            payload_interval = str(payload.get("interval") or interval)
            reasons = tuple(payload.get("reasons") or [payload.get("reason") or HealthReason.OK.value])
            availability = _availability(payload.get("availability"), payload)
            integrity = _integrity(payload.get("integrity"), payload)
            freshness = _freshness(payload.get("freshness"), payload)
            rows = int(payload.get("rows") or 0)
        health = cls(
            key=DatasetKey(payload_symbol, payload_interval),
            availability=availability,
            integrity=integrity,
            freshness=freshness,
            reasons=tuple(health_reason(value, strict=strict) for value in reasons),
            rows=rows,
            first_timestamp_ms=_optional_int(payload.get("first_timestamp_ms")),
            last_timestamp_ms=_optional_int(payload.get("last_timestamp_ms")),
            updated_at_ms=int(payload.get("updated_at_ms") or 0),
            source_file=Path(payload.get("source_file") or ""),
            validation=ValidationReport.from_dict(payload.get("validation"), strict=strict),
            error_type=payload.get("error_type"),
            message=payload.get("message"),
            warnings=tuple(str(value) for value in payload.get("warnings") or ()),
            content_sha256=str(payload["content_sha256"]) if payload.get("content_sha256") is not None else None,
        )
        if strict:
            _validate_dataset_health(health)
        return health


@dataclass(frozen=True)
class UniverseSnapshot:
    crypto_top: tuple[dict[str, Any], ...] = ()
    stock_token_top: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "crypto_top": [dict(item) for item in self.crypto_top],
            "stock_token_top": [dict(item) for item in self.stock_token_top],
        }


@dataclass(frozen=True)
class TrustedManifestSnapshot:
    schema_version: int
    run_id: str
    attempt_status: RefreshAttemptStatus
    snapshot_usability: SnapshotUsability
    started_at_ms: int
    completed_at_ms: int
    requested_intervals: tuple[str, ...]
    effective_intervals: tuple[str, ...]
    universe_snapshot: UniverseSnapshot
    datasets: dict[tuple[str, str], DatasetHealth] = field(default_factory=dict)
    provider_failures: tuple[dict[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    cycle_error: dict[str, str] | None = None


@dataclass(frozen=True)
class TrustedLoadContext:
    manifest: TrustedManifestSnapshot
    observed_at_ms: int
    generation_root: Path = Path(".")
    generation_id: str | None = None


def derive_snapshot_usability(
    datasets: dict[tuple[str, str], DatasetHealth],
) -> SnapshotUsability:
    if not datasets:
        return SnapshotUsability.INVALID
    if any(health.availability != AvailabilityState.AVAILABLE for health in datasets.values()):
        return SnapshotUsability.INVALID
    if any(health.integrity != IntegrityState.VALID for health in datasets.values()):
        return SnapshotUsability.INVALID
    if any(health.freshness == FreshnessState.UNKNOWN for health in datasets.values()):
        return SnapshotUsability.INVALID
    if any(health.freshness == FreshnessState.STALE for health in datasets.values()):
        return SnapshotUsability.STALE
    return SnapshotUsability.USABLE


@dataclass(frozen=True)
class RefreshRun:
    run_id: str
    attempt_status: RefreshAttemptStatus
    snapshot_usability: SnapshotUsability
    started_at_ms: int
    completed_at_ms: int
    requested_intervals: tuple[str, ...]
    effective_intervals: tuple[str, ...]
    universe_snapshot: UniverseSnapshot
    datasets: dict[tuple[str, str], DatasetHealth] = field(default_factory=dict)
    provider_failures: tuple[dict[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    cycle_error: dict[str, str] | None = None

    def to_manifest(self) -> dict[str, Any]:
        symbols: dict[str, dict[str, Any]] = {}
        for (symbol, interval), health in self.datasets.items():
            symbol_payload = symbols.setdefault(symbol, {"intervals": {}})
            symbol_payload["intervals"][interval] = health.to_dict()
        for item in [*self.universe_snapshot.crypto_top, *self.universe_snapshot.stock_token_top]:
            inst_id = item.get("inst_id")
            if not inst_id:
                continue
            symbols.setdefault(str(inst_id), {"intervals": {}})
            symbols[str(inst_id)]["source"] = item.get("source")
            symbols[str(inst_id)]["last"] = item.get("last")
            symbols[str(inst_id)]["volume_ccy_24h"] = item.get("volume_ccy_24h")
        return {
            "schema_version": 3,
            "run_id": self.run_id,
            "attempt_status": self.attempt_status.value,
            "snapshot_usability": self.snapshot_usability.value,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "updated_at_ms": self.completed_at_ms,
            "requested_intervals": list(self.requested_intervals),
            "effective_intervals": list(self.effective_intervals),
            "intervals": list(self.effective_intervals),
            "universes": self.universe_snapshot.to_dict(),
            "symbols": symbols,
            "provider_failures": [dict(value) for value in self.provider_failures],
            "warnings": list(self.warnings),
            "cycle_error": self.cycle_error,
        }

    def run_log_payload(self) -> dict[str, Any]:
        invalid_count = sum(1 for health in self.datasets.values() if not health.is_usable)
        return {
            "run_id": self.run_id,
            "attempt_status": self.attempt_status.value,
            "snapshot_usability": self.snapshot_usability.value,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "symbol_count": len({symbol for symbol, _ in self.datasets}),
            "invalid_count": invalid_count,
            "provider_failures": [dict(value) for value in self.provider_failures],
            "warnings": list(self.warnings),
            "cycle_error": self.cycle_error,
        }


@dataclass(frozen=True)
class TrustDecision:
    allowed: bool
    reason: HealthReason = HealthReason.OK
    message: str | None = None


@dataclass(frozen=True)
class TrustedBundle:
    symbol: str
    candles_by_interval: dict[str, list[Candle]]
    files_by_interval: dict[str, Path]
    days: int
    health_by_interval: dict[str, DatasetHealth]
    trust_decision: TrustDecision
    run_id: str | None = None
    universe_snapshot: UniverseSnapshot | None = None
    load_context: TrustedLoadContext | None = None


def trusted_manifest_snapshot_from_dict(
    payload: dict[str, Any],
    *,
    compatibility_mode: bool = False,
) -> TrustedManifestSnapshot:
    schema_version = payload.get("schema_version")
    if not _is_int(schema_version):
        if compatibility_mode and (schema_version is None or schema_version == 1):
            return _legacy_manifest_snapshot(payload)
        raise ManifestSchemaError("manifest schema_version must be integer v3")
    if int(schema_version) != 3:
        if compatibility_mode and int(schema_version) == 1:
            return _legacy_manifest_snapshot(payload)
        if compatibility_mode and int(schema_version) == 2:
            return _legacy_manifest_snapshot(payload)
        raise ManifestSchemaError(f"unsupported manifest schema_version: {schema_version}")

    run_id = _required_str(payload, "run_id")
    attempt_status = _required_enum(payload, "attempt_status", RefreshAttemptStatus)
    snapshot_usability = _required_enum(payload, "snapshot_usability", SnapshotUsability)
    requested_intervals = _required_str_tuple(payload, "requested_intervals")
    effective_intervals = _required_str_tuple(payload, "effective_intervals")
    symbols = _required_dict(payload, "symbols")
    universes = _required_dict(payload, "universes")
    snapshot = TrustedManifestSnapshot(
        schema_version=3,
        run_id=run_id,
        attempt_status=attempt_status,
        snapshot_usability=snapshot_usability,
        started_at_ms=_required_int(payload, "started_at_ms"),
        completed_at_ms=_required_int(payload, "completed_at_ms"),
        requested_intervals=requested_intervals,
        effective_intervals=effective_intervals,
        universe_snapshot=_universe_snapshot_from_dict(universes),
        datasets=_datasets_from_symbols(symbols),
        provider_failures=_provider_failures(payload.get("provider_failures") or ()),
        warnings=_optional_str_tuple(payload, "warnings"),
        cycle_error=_optional_str_dict(payload.get("cycle_error")),
    )
    _validate_manifest_snapshot(snapshot)
    return snapshot


def health_reason(value: Any, *, strict: bool = False) -> HealthReason:
    if isinstance(value, HealthReason):
        return value
    try:
        return HealthReason(str(value))
    except Exception:
        if strict:
            raise ManifestSchemaError(f"unknown health reason: {value}") from None
        return HealthReason.REFRESH_FAILED


def _availability(value: Any, payload: dict[str, Any]) -> AvailabilityState:
    if value:
        return AvailabilityState(str(value))
    return AvailabilityState.AVAILABLE if payload.get("rows") else AvailabilityState.MISSING


def _integrity(value: Any, payload: dict[str, Any]) -> IntegrityState:
    if value:
        return IntegrityState(str(value))
    return IntegrityState.VALID if bool(payload.get("is_valid", True)) else IntegrityState.INVALID


def _freshness(value: Any, payload: dict[str, Any]) -> FreshnessState:
    if value:
        return FreshnessState(str(value))
    return FreshnessState.STALE if bool(payload.get("is_stale")) else FreshnessState.FRESH


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _legacy_manifest_snapshot(payload: dict[str, Any]) -> TrustedManifestSnapshot:
    symbols = payload.get("symbols") if isinstance(payload.get("symbols"), dict) else {}
    status_value = str(payload.get("status") or "invalid")
    snapshot_usability = (
        SnapshotUsability.USABLE
        if status_value == "ok"
        else SnapshotUsability.STALE
        if status_value == "stale"
        else SnapshotUsability.INVALID
    )
    outcome_value = str(payload.get("outcome") or "")
    attempt_status = (
        RefreshAttemptStatus.FAILED
        if outcome_value == "failed" or snapshot_usability == SnapshotUsability.INVALID
        else RefreshAttemptStatus.DEGRADED
        if outcome_value == "partial"
        else RefreshAttemptStatus.SUCCESS
    )
    intervals = tuple(str(item) for item in payload.get("intervals") or ())
    if not intervals:
        intervals = tuple(dict.fromkeys(interval for _, interval in _datasets_from_symbols(symbols)))
    universe_payload = payload.get("universes") if isinstance(payload.get("universes"), dict) else {}
    return TrustedManifestSnapshot(
        schema_version=1,
        run_id=str(payload.get("run_id") or "legacy-manifest"),
        attempt_status=attempt_status,
        snapshot_usability=snapshot_usability,
        started_at_ms=int(payload.get("started_at_ms") or 0),
        completed_at_ms=int(payload.get("completed_at_ms") or payload.get("updated_at_ms") or 0),
        requested_intervals=intervals,
        effective_intervals=intervals,
        universe_snapshot=_universe_snapshot_from_dict(universe_payload),
        datasets=_datasets_from_symbols(symbols, strict=False),
        provider_failures=_provider_failures(payload.get("provider_failures") or ()),
        warnings=tuple(str(value) for value in payload.get("warnings") or ()),
        cycle_error=_optional_str_dict(payload.get("cycle_error")),
    )


def _datasets_from_symbols(symbols: dict[str, Any], *, strict: bool = True) -> dict[tuple[str, str], DatasetHealth]:
    datasets: dict[tuple[str, str], DatasetHealth] = {}
    for symbol, symbol_payload in symbols.items():
        if not isinstance(symbol_payload, dict):
            raise ManifestSchemaError("manifest symbols entries must be objects")
        intervals = symbol_payload.get("intervals") or {}
        if not isinstance(intervals, dict):
            raise ManifestSchemaError("manifest interval entries must be objects")
        for interval, payload in intervals.items():
            if not isinstance(payload, dict):
                raise ManifestSchemaError("manifest dataset health entries must be objects")
            health = DatasetHealth.from_dict(str(symbol), str(interval), payload, strict=strict)
            datasets[(health.key.symbol, health.key.interval)] = health
    return datasets


def _validate_dataset_health(health: DatasetHealth) -> None:
    if health.rows < 0:
        raise ManifestSchemaError("manifest dataset rows must be non-negative")
    if health.availability == AvailabilityState.AVAILABLE:
        if health.rows <= 0:
            raise ManifestSchemaError("available manifest dataset must have rows")
        if health.first_timestamp_ms is None or health.last_timestamp_ms is None:
            raise ManifestSchemaError("available manifest dataset must have timestamp range")
        if health.first_timestamp_ms > health.last_timestamp_ms:
            raise ManifestSchemaError("manifest dataset timestamp range is inverted")
    if health.availability == AvailabilityState.MISSING:
        if health.rows != 0:
            raise ManifestSchemaError("missing manifest dataset cannot have rows")
        if health.integrity == IntegrityState.VALID:
            raise ManifestSchemaError("missing manifest dataset cannot have valid integrity")
    if health.integrity == IntegrityState.VALID and health.availability != AvailabilityState.AVAILABLE:
        raise ManifestSchemaError("valid manifest dataset must be available")


def _validate_manifest_snapshot(snapshot: TrustedManifestSnapshot) -> None:
    effective = set(snapshot.effective_intervals)
    requested = set(snapshot.requested_intervals)
    if not requested.issubset(effective):
        raise ManifestSchemaError("manifest requested_intervals must be a subset of effective_intervals")
    if any(interval in effective for interval in ("15m", "1h")) and "5m" not in effective:
        raise ManifestSchemaError("manifest effective_intervals must include 5m when native intervals are present")
    derived_usability = derive_snapshot_usability(snapshot.datasets)
    if snapshot.snapshot_usability != derived_usability:
        raise ManifestSchemaError(
            "manifest snapshot_usability must match derived usability: "
            f"declared={snapshot.snapshot_usability.value}, derived={derived_usability.value}"
        )
    if snapshot.snapshot_usability == SnapshotUsability.USABLE:
        for row in [*snapshot.universe_snapshot.crypto_top, *snapshot.universe_snapshot.stock_token_top]:
            inst_id = str(row.get("inst_id") or row.get("instId") or "")
            if not inst_id:
                raise ManifestSchemaError("manifest universe row must include inst_id")
            for interval in snapshot.effective_intervals:
                if (inst_id, interval) not in snapshot.datasets:
                    raise ManifestSchemaError("usable manifest catalog is incomplete for canonical universe")


def _universe_snapshot_from_dict(payload: dict[str, Any]) -> UniverseSnapshot:
    return UniverseSnapshot(
        crypto_top=_universe_rows(payload.get("crypto_top") or ()),
        stock_token_top=_universe_rows(payload.get("stock_token_top") or ()),
    )


def _universe_rows(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise ManifestSchemaError("manifest universe rows must be arrays")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ManifestSchemaError("manifest universe row must be object")
        rows.append(dict(item))
    return tuple(rows)


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ManifestSchemaError(f"manifest {key} must be object")
    return value


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestSchemaError(f"manifest {key} must be non-empty string")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not _is_int(value):
        raise ManifestSchemaError(f"manifest {key} must be integer")
    return int(value)


def _required_str_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, (list, tuple)):
        raise ManifestSchemaError(f"manifest {key} must be array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ManifestSchemaError(f"manifest {key} entries must be strings")
    return tuple(value)


def _optional_str_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ManifestSchemaError(f"manifest {key} must be array")
    if any(not isinstance(item, str) for item in value):
        raise ManifestSchemaError(f"manifest {key} entries must be strings")
    return tuple(value)


def _required_enum(payload: dict[str, Any], key: str, enum_type):
    value = payload.get(key)
    if not isinstance(value, str):
        raise ManifestSchemaError(f"manifest {key} must be string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ManifestSchemaError(f"unknown manifest {key}: {value}") from exc


def _optional_str_dict(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ManifestSchemaError("manifest cycle_error must be object or null")
    return {str(key): str(item) for key, item in value.items()}


def _provider_failures(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        raise ManifestSchemaError("manifest provider_failures must be an array")
    failures: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ManifestSchemaError("manifest provider_failures entries must be objects")
        failure = {str(key): str(payload_value) for key, payload_value in item.items()}
        for key in ("symbol", "interval", "reason", "error_type", "message"):
            if not failure.get(key):
                raise ManifestSchemaError(f"manifest provider failure must include {key}")
        failures.append(failure)
    return tuple(failures)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
