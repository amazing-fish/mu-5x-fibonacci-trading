from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from mu_strategy.market_data.symbols import ResolvedSymbol
from mu_strategy.market_data.trusted_data.contracts import (
    DatasetHealth,
    HealthReason,
    TrustDecision,
    TrustedBundle,
    TrustedLoadContext,
    UniverseSnapshot,
    ValidationReport,
    health_reason,
)
from mu_strategy.models import Candle


@dataclass(frozen=True)
class CandleValidationResult:
    ok: bool
    reason: str = "ok"
    missing_in_built: list[int] = field(default_factory=list)
    missing_in_native: list[int] = field(default_factory=list)
    misaligned_timestamps: list[int] = field(default_factory=list)
    timestamp_gaps: list[dict[str, int]] = field(default_factory=list)
    value_mismatches: list[dict[str, int | float | str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DataStatus:
    symbol: str
    interval: str
    rows: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None
    updated_at_ms: int
    source_file: Path
    is_valid: bool = True
    is_stale: bool = False
    reason: str = "ok"
    error_type: str | None = None
    message: str | None = None
    warnings: tuple[str, ...] = ()
    validation: CandleValidationResult | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["source_file"] = str(self.source_file)
        if self.validation is not None:
            payload["validation"] = self.validation.to_dict()
        return payload


@dataclass(frozen=True)
class CandleBundle:
    symbol: ResolvedSymbol
    candles_by_interval: dict[str, list[Candle]]
    files_by_interval: dict[str, Path]
    days: int
    statuses_by_interval: dict[str, DataStatus] = field(default_factory=dict)
    run_id: str | None = None
    trust_decision: TrustDecision | None = None
    universe_snapshot: UniverseSnapshot | None = None
    load_context: TrustedLoadContext | None = None
    observed_at_ms: int | None = None


def candle_bundle_from_trusted_bundle(resolved: ResolvedSymbol, bundle: TrustedBundle) -> CandleBundle:
    statuses = {
        interval: data_status_from_health(health)
        for interval, health in bundle.health_by_interval.items()
    }
    return CandleBundle(
        symbol=resolved,
        candles_by_interval=bundle.candles_by_interval,
        files_by_interval=bundle.files_by_interval,
        days=bundle.days,
        statuses_by_interval=statuses,
        run_id=bundle.run_id,
        trust_decision=bundle.trust_decision,
        universe_snapshot=bundle.universe_snapshot,
        load_context=bundle.load_context,
        observed_at_ms=bundle.load_context.observed_at_ms if bundle.load_context else None,
    )


def ensure_trusted_candle_bundle(bundle):
    if getattr(bundle, "trust_decision", None) is not None:
        return bundle
    decision = trust_decision_from_legacy_statuses(getattr(bundle, "statuses_by_interval", None) or {})
    if isinstance(bundle, CandleBundle):
        return replace(bundle, trust_decision=decision)
    try:
        setattr(bundle, "trust_decision", decision)
    except Exception:
        pass
    return bundle


def trust_decision_from_legacy_statuses(statuses: dict[str, DataStatus]) -> TrustDecision:
    if not statuses:
        return TrustDecision(False, HealthReason.MANIFEST_BLOCKED, "legacy candle bundle is missing trusted status")
    for status in statuses.values():
        if not status.is_valid:
            return TrustDecision(False, health_reason(status.reason), status.message)
        if status.is_stale:
            return TrustDecision(False, health_reason(status.reason), status.message)
    return TrustDecision(True, HealthReason.OK)


def trusted_bundle_error(bundle: CandleBundle) -> str | None:
    decision = ensure_trusted_candle_bundle(bundle).trust_decision
    if decision is not None and not decision.allowed:
        return f"trusted data blocked: {decision.reason.value}"
    return None


def data_status_from_health(health: DatasetHealth) -> DataStatus:
    payload = health.to_dict()
    return DataStatus(
        symbol=health.key.symbol,
        interval=health.key.interval,
        rows=health.rows,
        first_timestamp_ms=health.first_timestamp_ms,
        last_timestamp_ms=health.last_timestamp_ms,
        updated_at_ms=health.updated_at_ms,
        source_file=health.source_file,
        is_valid=bool(payload["is_valid"]),
        is_stale=bool(payload["is_stale"]),
        reason=str(payload["reason"]),
        error_type=health.error_type,
        message=health.message,
        warnings=health.warnings,
        validation=validation_result_from_report(health.validation) if health.validation else None,
    )


def validation_result_from_report(report: ValidationReport) -> CandleValidationResult:
    return CandleValidationResult(
        ok=report.ok,
        reason=report.reason.value,
        missing_in_built=list(report.missing_in_built),
        missing_in_native=list(report.missing_in_native),
        misaligned_timestamps=list(report.misaligned_timestamps),
        timestamp_gaps=[dict(value) for value in report.timestamp_gaps],
        value_mismatches=[dict(value) for value in report.value_mismatches],
    )


def trust_error_payload(symbol: str, bundle: CandleBundle) -> dict[str, Any] | None:
    normalized = ensure_trusted_candle_bundle(bundle)
    decision = normalized.trust_decision
    if decision is None or decision.allowed:
        return None
    manifest = normalized.load_context.manifest if normalized.load_context else None
    return {
        "symbol": symbol,
        "reason": "market_data_invalid",
        "interval": None,
        "status_reason": decision.reason.value,
        "error_type": None,
        "message": decision.message,
        "latest_open_time_ms": None,
        "source_file": "",
        "run_id": normalized.run_id,
        "attempt_status": manifest.attempt_status.value if manifest else None,
        "snapshot_usability": manifest.snapshot_usability.value if manifest else None,
    }
