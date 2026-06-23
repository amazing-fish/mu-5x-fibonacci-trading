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


class RefreshRunOutcome(Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ManifestStatus(Enum):
    OK = "ok"
    STALE = "stale"
    INVALID = "invalid"


class HealthReason(Enum):
    OK = "ok"
    CACHE_MISSING = "cache_missing"
    CACHE_READ_FAILED = "cache_read_failed"
    EMPTY = "empty"
    REFRESH_FAILED = "refresh_failed"
    INCREMENTAL_REFRESH_FAILED = "incremental_refresh_failed"
    MANIFEST_MISSING = "manifest_missing"
    MALFORMED_MANIFEST = "malformed_manifest"
    MANIFEST_BLOCKED = "manifest_blocked"
    RUN_FAILED = "run_failed"
    RUN_PARTIAL = "run_partial"
    MANIFEST_INVALID = "manifest_invalid"
    MANIFEST_STALE = "manifest_stale"
    STALE_BY_CLOCK = "stale_by_clock"
    BUILT_EMPTY = "built_empty"
    NATIVE_EMPTY = "native_empty"
    BUILT_SAMPLE_COUNT_BELOW_MINIMUM = "built_sample_count_below_minimum"
    NATIVE_SAMPLE_COUNT_BELOW_MINIMUM = "native_sample_count_below_minimum"
    TIMESTAMP_MISALIGNED = "timestamp_misaligned"
    MISSING_IN_BUILT = "missing_in_built"
    MISSING_IN_NATIVE = "missing_in_native"
    OHLCV_MISMATCH = "ohlcv_mismatch"
    OHLCV_INVALID = "ohlcv_invalid"
    CONTINUITY_GAP = "continuity_gap"


class Clock(Protocol):
    def now_ms(self) -> int:
        ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class ManifestSchemaError(ValueError):
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
    value_mismatches: tuple[dict[str, int | float | str], ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason.value,
            "missing_in_built": list(self.missing_in_built),
            "missing_in_native": list(self.missing_in_native),
            "misaligned_timestamps": list(self.misaligned_timestamps),
            "value_mismatches": list(self.value_mismatches),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ValidationReport | None":
        if not isinstance(payload, dict):
            return None
        return cls(
            ok=bool(payload.get("ok")),
            reason=health_reason(payload.get("reason")),
            missing_in_built=tuple(int(value) for value in payload.get("missing_in_built") or ()),
            missing_in_native=tuple(int(value) for value in payload.get("missing_in_native") or ()),
            misaligned_timestamps=tuple(int(value) for value in payload.get("misaligned_timestamps") or ()),
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

    @property
    def is_usable(self) -> bool:
        return (
            self.availability == AvailabilityState.AVAILABLE
            and self.integrity == IntegrityState.VALID
            and self.freshness != FreshnessState.STALE
            and self.rows > 0
        )

    @property
    def primary_reason(self) -> HealthReason:
        return self.reasons[0] if self.reasons else HealthReason.OK

    def to_dict(self) -> dict[str, Any]:
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
            "source_file": str(self.source_file),
            "is_valid": self.integrity == IntegrityState.VALID and self.availability == AvailabilityState.AVAILABLE,
            "is_stale": self.freshness == FreshnessState.STALE,
            "reason": self.primary_reason.value,
            "error_type": self.error_type,
            "message": self.message,
            "warnings": list(self.warnings),
            "validation": self.validation.to_dict() if self.validation else None,
        }

    @classmethod
    def from_dict(cls, symbol: str, interval: str, payload: dict[str, Any]) -> "DatasetHealth":
        reasons = payload.get("reasons")
        if not reasons:
            reasons = [payload.get("reason") or HealthReason.OK.value]
        availability = _availability(payload.get("availability"), payload)
        integrity = _integrity(payload.get("integrity"), payload)
        freshness = _freshness(payload.get("freshness"), payload)
        return cls(
            key=DatasetKey(str(payload.get("symbol") or symbol), str(payload.get("interval") or interval)),
            availability=availability,
            integrity=integrity,
            freshness=freshness,
            reasons=tuple(health_reason(value) for value in reasons),
            rows=int(payload.get("rows") or 0),
            first_timestamp_ms=_optional_int(payload.get("first_timestamp_ms")),
            last_timestamp_ms=_optional_int(payload.get("last_timestamp_ms")),
            updated_at_ms=int(payload.get("updated_at_ms") or 0),
            source_file=Path(payload.get("source_file") or ""),
            validation=ValidationReport.from_dict(payload.get("validation")),
            error_type=payload.get("error_type"),
            message=payload.get("message"),
            warnings=tuple(str(value) for value in payload.get("warnings") or ()),
        )


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
    outcome: RefreshRunOutcome
    status: ManifestStatus
    started_at_ms: int
    completed_at_ms: int
    requested_intervals: tuple[str, ...]
    effective_intervals: tuple[str, ...]
    universe_snapshot: UniverseSnapshot
    datasets: dict[tuple[str, str], DatasetHealth] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    cycle_error: dict[str, str] | None = None


@dataclass(frozen=True)
class TrustedLoadContext:
    manifest: TrustedManifestSnapshot
    observed_at_ms: int


@dataclass(frozen=True)
class RefreshRun:
    run_id: str
    outcome: RefreshRunOutcome
    started_at_ms: int
    completed_at_ms: int
    requested_intervals: tuple[str, ...]
    effective_intervals: tuple[str, ...]
    universe_snapshot: UniverseSnapshot
    datasets: dict[tuple[str, str], DatasetHealth] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    cycle_error: dict[str, str] | None = None

    def manifest_status(self) -> str:
        if self.outcome == RefreshRunOutcome.FAILED:
            return "invalid"
        if any(health.availability != AvailabilityState.AVAILABLE for health in self.datasets.values()):
            return "invalid"
        if any(health.integrity != IntegrityState.VALID for health in self.datasets.values()):
            return "invalid"
        if any(health.freshness == FreshnessState.STALE for health in self.datasets.values()):
            return "stale"
        return "ok"

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
            "schema_version": 2,
            "run_id": self.run_id,
            "outcome": self.outcome.value,
            "status": self.manifest_status(),
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "updated_at_ms": self.completed_at_ms,
            "requested_intervals": list(self.requested_intervals),
            "effective_intervals": list(self.effective_intervals),
            "intervals": list(self.effective_intervals),
            "universes": self.universe_snapshot.to_dict(),
            "symbols": symbols,
            "warnings": list(self.warnings),
            "cycle_error": self.cycle_error,
        }

    def run_log_payload(self) -> dict[str, Any]:
        invalid_count = sum(1 for health in self.datasets.values() if not health.is_usable)
        return {
            "run_id": self.run_id,
            "outcome": self.outcome.value,
            "status": self.manifest_status(),
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "symbol_count": len({symbol for symbol, _ in self.datasets}),
            "invalid_count": invalid_count,
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
        raise ManifestSchemaError("manifest schema_version must be integer v2")
    if int(schema_version) != 2:
        if compatibility_mode and int(schema_version) == 1:
            return _legacy_manifest_snapshot(payload)
        raise ManifestSchemaError(f"unsupported manifest schema_version: {schema_version}")

    run_id = _required_str(payload, "run_id")
    outcome = _required_enum(payload, "outcome", RefreshRunOutcome)
    status = _required_enum(payload, "status", ManifestStatus)
    requested_intervals = _required_str_tuple(payload, "requested_intervals")
    effective_intervals = _required_str_tuple(payload, "effective_intervals")
    symbols = _required_dict(payload, "symbols")
    universes = _required_dict(payload, "universes")
    return TrustedManifestSnapshot(
        schema_version=2,
        run_id=run_id,
        outcome=outcome,
        status=status,
        started_at_ms=_required_int(payload, "started_at_ms"),
        completed_at_ms=_required_int(payload, "completed_at_ms"),
        requested_intervals=requested_intervals,
        effective_intervals=effective_intervals,
        universe_snapshot=_universe_snapshot_from_dict(universes),
        datasets=_datasets_from_symbols(symbols),
        warnings=_optional_str_tuple(payload, "warnings"),
        cycle_error=_optional_str_dict(payload.get("cycle_error")),
    )


def health_reason(value: Any) -> HealthReason:
    if isinstance(value, HealthReason):
        return value
    try:
        return HealthReason(str(value))
    except Exception:
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
    status = ManifestStatus.OK if status_value == "ok" else ManifestStatus.STALE if status_value == "stale" else ManifestStatus.INVALID
    outcome = RefreshRunOutcome.SUCCESS if status in {ManifestStatus.OK, ManifestStatus.STALE} else RefreshRunOutcome.FAILED
    intervals = tuple(str(item) for item in payload.get("intervals") or ())
    if not intervals:
        intervals = tuple(dict.fromkeys(interval for _, interval in _datasets_from_symbols(symbols)))
    universe_payload = payload.get("universes") if isinstance(payload.get("universes"), dict) else {}
    return TrustedManifestSnapshot(
        schema_version=1,
        run_id=str(payload.get("run_id") or "legacy-manifest"),
        outcome=outcome,
        status=status,
        started_at_ms=int(payload.get("started_at_ms") or 0),
        completed_at_ms=int(payload.get("completed_at_ms") or payload.get("updated_at_ms") or 0),
        requested_intervals=intervals,
        effective_intervals=intervals,
        universe_snapshot=_universe_snapshot_from_dict(universe_payload),
        datasets=_datasets_from_symbols(symbols),
        warnings=tuple(str(value) for value in payload.get("warnings") or ()),
        cycle_error=_optional_str_dict(payload.get("cycle_error")),
    )


def _datasets_from_symbols(symbols: dict[str, Any]) -> dict[tuple[str, str], DatasetHealth]:
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
            health = DatasetHealth.from_dict(str(symbol), str(interval), payload)
            datasets[(health.key.symbol, health.key.interval)] = health
    return datasets


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


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
