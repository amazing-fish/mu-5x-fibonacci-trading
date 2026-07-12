from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum, unique
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from mu_strategy.canonical import canonical_json, canonical_sha256
from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import EntryDecisionCode, EntryDecisionStage, EntryDisposition, entry_decision_metadata


OBSERVATION_SCHEMA_VERSION = 1
DEFAULT_STAGE0_OBSERVATION_LOG = Path("data/observations/stage0.jsonl")
STAGE0_TRUST_POLICY_NAME = "trading_strict"
STAGE0_TRUST_POLICY_VERSION = 1
MAX_DIAGNOSTIC_LENGTH = 512
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_NAME = (
    r"(?:okx[_ -]?)?api[_ -]?key|(?:okx[_ -]?)?secret(?:[_ -]?key)?|(?:okx[_ -]?)?passphrase|authorization|"
    r"signature|(?:access[_ -]?)?token|cookie|set[_ -]?cookie|x[_ -]?api[_ -]?key|"
    r"ok[_ -]access[_ -](?:key|sign|passphrase)"
)
_SECRET_JSON_PATTERN = re.compile(
    rf"(?i)([\"'](?:{_SENSITIVE_NAME})[\"']\s*:\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^,\s}]+)"
)
_SECRET_VALUE_PATTERN = re.compile(
    rf"(?i)\b({_SENSITIVE_NAME})\b(\s*[:=]\s*)"
    r"(?:Bearer\s+[^\s,;]+|\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


@unique
class ObservationOutcome(str, Enum):
    DATA_GATE_BLOCKED = "data_gate_blocked"
    SCAN_FAILED = "scan_failed"
    NORMAL_NO_ACTION = "normal_no_action"
    READY_FOR_REVIEW = "ready_for_review"


@unique
class ObservationFailureCode(str, Enum):
    TRUSTED_DATA_BLOCKED = "trusted_data_blocked"
    TRUSTED_DATA_LOAD_FAILED = "trusted_data_load_failed"
    TRUSTED_PROVENANCE_INCOMPLETE = "trusted_provenance_incomplete"
    SCANNER_EXCEPTION = "scanner_exception"
    SCANNER_RESULT_INVALID = "scanner_result_invalid"


class ObservationSchemaError(ValueError):
    pass


class ObservationCorruptionError(RuntimeError):
    pass


class ObservationWriteError(RuntimeError):
    pass


class ObservationCycleInvalidError(RuntimeError):
    pass


class ObservationRepository(Protocol):
    def append_cycle(self, cycle: "Stage0ObservationCycle") -> None:
        ...


@dataclass(frozen=True)
class TrustedObservationReference:
    run_id: str | None
    requested_intervals: tuple[str, ...]
    effective_intervals: tuple[str, ...]
    content_sha256_by_interval: tuple[tuple[str, str], ...]
    policy_name: str
    policy_version: int
    allowed: bool
    reason: HealthReason

    def __post_init__(self) -> None:
        if not self.requested_intervals:
            raise ValueError("requested_intervals must not be empty")
        if not set(self.requested_intervals).issubset(self.effective_intervals):
            raise ValueError("requested_intervals must be a subset of effective_intervals")
        if self.policy_name != STAGE0_TRUST_POLICY_NAME or self.policy_version != STAGE0_TRUST_POLICY_VERSION:
            raise ValueError("Stage 0 requires the trading_strict policy identity/version")
        hashes = dict(self.content_sha256_by_interval)
        if len(hashes) != len(self.content_sha256_by_interval):
            raise ValueError("content SHA-256 intervals must be unique")
        for interval, digest in hashes.items():
            if not interval or not _SHA256_PATTERN.fullmatch(digest):
                raise ValueError("content SHA-256 values must be lowercase 64-character hex")
        if self.allowed:
            if self.reason is not HealthReason.OK:
                raise ValueError("allowed trusted decision must have reason OK")
            if not self.run_id:
                raise ValueError("allowed observation requires trusted run_id")
            missing = set(self.effective_intervals) - set(hashes)
            if missing:
                raise ValueError(f"allowed observation requires content SHA-256 for {sorted(missing)}")


@dataclass(frozen=True)
class Stage0ScanResult:
    symbol: str
    last_close: float | None
    regime_1h: str
    rsi14: float | None
    macd_hist: float | None
    macd_hist_prev: float | None
    fib_level: float | None
    fib_distance_pct: float | None
    trigger_price: float | None
    initial_stop: float | None
    signal_time_ms: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise TypeError("scan_result symbol must be a non-empty string")
        if not isinstance(self.regime_1h, str) or not self.regime_1h:
            raise TypeError("scan_result regime_1h must be a non-empty string")
        numeric_fields = (
            "last_close",
            "rsi14",
            "macd_hist",
            "macd_hist_prev",
            "fib_level",
            "fib_distance_pct",
            "trigger_price",
            "initial_stop",
        )
        for field_name in numeric_fields:
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"scan_result {field_name} must be numeric or null")
            try:
                normalized = float(value)
            except OverflowError as exc:
                raise ValueError(f"scan_result {field_name} must be finite") from exc
            if not math.isfinite(normalized):
                raise ValueError(f"scan_result {field_name} must be finite")
            object.__setattr__(self, field_name, normalized)
        if self.signal_time_ms is not None:
            if isinstance(self.signal_time_ms, bool) or not isinstance(self.signal_time_ms, int):
                raise TypeError("scan_result signal_time_ms must be an integer or null")
            if self.signal_time_ms < 0:
                raise ValueError("scan_result signal_time_ms must be non-negative")

    @classmethod
    def from_entry_result(cls, result: EntryScanResult) -> "Stage0ScanResult":
        return cls(
            symbol=result.symbol,
            last_close=result.last_close,
            regime_1h=result.regime_1h,
            rsi14=result.rsi14,
            macd_hist=result.macd_hist,
            macd_hist_prev=result.macd_hist_prev,
            fib_level=result.fib_level,
            fib_distance_pct=result.fib_distance_pct,
            trigger_price=result.trigger_price,
            initial_stop=result.initial_stop,
            signal_time_ms=result.signal_time_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_close": self.last_close,
            "regime_1h": self.regime_1h,
            "rsi14": self.rsi14,
            "macd_hist": self.macd_hist,
            "macd_hist_prev": self.macd_hist_prev,
            "fib_level": self.fib_level,
            "fib_distance_pct": self.fib_distance_pct,
            "trigger_price": self.trigger_price,
            "initial_stop": self.initial_stop,
            "signal_time_ms": self.signal_time_ms,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Stage0ScanResult":
        if not isinstance(payload, dict):
            raise ObservationSchemaError("scan_result must be an object")
        _require_exact_fields(payload, set(cls.__dataclass_fields__), "scan_result")
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise ObservationSchemaError(f"invalid scan_result: {exc}") from exc


@dataclass(frozen=True)
class Stage0Observation:
    observation_id: str
    cycle_id: str
    symbol: str
    created_at_ms: int
    observed_at_ms: int
    trusted_run_id: str | None
    requested_intervals: tuple[str, ...]
    effective_intervals: tuple[str, ...]
    content_sha256_by_interval: Mapping[str, str]
    trust_policy_name: str
    trust_policy_version: int
    trust_allowed: bool
    trust_reason: HealthReason
    strategy_name: str
    strategy_config_fingerprint: str
    decision_code: EntryDecisionCode | None
    disposition: EntryDisposition | None
    decision_stage: EntryDecisionStage | None
    compatibility_action: str | None
    compatibility_reason: str | None
    compatibility_source: str
    provenance: str
    outcome: ObservationOutcome
    failure_code: ObservationFailureCode | None
    error_type: str | None
    error_message: str | None
    scan_result: Stage0ScanResult | None
    result_fingerprint: str
    schema_version: int = OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ObservationSchemaError(f"unsupported observation schema_version: {self.schema_version}")
        if not self.observation_id or not self.cycle_id or not self.symbol:
            raise ValueError("observation_id, cycle_id, and symbol are required")
        if not _SHA256_PATTERN.fullmatch(self.strategy_config_fingerprint):
            raise ValueError("strategy_config_fingerprint must be lowercase SHA-256")
        if not _SHA256_PATTERN.fullmatch(self.result_fingerprint):
            raise ValueError("result_fingerprint must be lowercase SHA-256")
        immutable_hashes = MappingProxyType(dict(self.content_sha256_by_interval))
        object.__setattr__(self, "content_sha256_by_interval", immutable_hashes)
        _validate_observation_semantics(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "cycle_id": self.cycle_id,
            "symbol": self.symbol,
            "created_at_ms": self.created_at_ms,
            "observed_at_ms": self.observed_at_ms,
            "trusted_run_id": self.trusted_run_id,
            "requested_intervals": list(self.requested_intervals),
            "effective_intervals": list(self.effective_intervals),
            "content_sha256_by_interval": dict(self.content_sha256_by_interval),
            "trust_policy_name": self.trust_policy_name,
            "trust_policy_version": self.trust_policy_version,
            "trust_allowed": self.trust_allowed,
            "trust_reason": self.trust_reason.value,
            "strategy_name": self.strategy_name,
            "strategy_config_fingerprint": self.strategy_config_fingerprint,
            "decision_code": self.decision_code.value if self.decision_code else None,
            "disposition": self.disposition.value if self.disposition else None,
            "decision_stage": self.decision_stage.value if self.decision_stage else None,
            "compatibility_action": self.compatibility_action,
            "compatibility_reason": self.compatibility_reason,
            "compatibility_source": self.compatibility_source,
            "provenance": self.provenance,
            "outcome": self.outcome.value,
            "failure_code": self.failure_code.value if self.failure_code else None,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "scan_result": self.scan_result.to_dict() if self.scan_result else None,
            "result_fingerprint": self.result_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Stage0Observation":
        if not isinstance(payload, dict):
            raise ObservationSchemaError("observation must be an object")
        expected = {field for field in cls.__dataclass_fields__}
        _require_exact_fields(payload, expected, "observation")
        if payload.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
            raise ObservationSchemaError(f"unsupported observation schema_version: {payload.get('schema_version')}")
        try:
            observation = cls(
                schema_version=payload["schema_version"],
                observation_id=_required_text(payload, "observation_id"),
                cycle_id=_required_text(payload, "cycle_id"),
                symbol=_required_text(payload, "symbol"),
                created_at_ms=_required_int(payload, "created_at_ms"),
                observed_at_ms=_required_int(payload, "observed_at_ms"),
                trusted_run_id=_optional_text(payload.get("trusted_run_id")),
                requested_intervals=_text_tuple(payload, "requested_intervals"),
                effective_intervals=_text_tuple(payload, "effective_intervals"),
                content_sha256_by_interval=_text_mapping(payload, "content_sha256_by_interval"),
                trust_policy_name=_required_text(payload, "trust_policy_name"),
                trust_policy_version=_required_int(payload, "trust_policy_version"),
                trust_allowed=_required_bool(payload, "trust_allowed"),
                trust_reason=HealthReason(payload["trust_reason"]),
                strategy_name=_required_text(payload, "strategy_name"),
                strategy_config_fingerprint=_required_text(payload, "strategy_config_fingerprint"),
                decision_code=_optional_enum(payload.get("decision_code"), EntryDecisionCode),
                disposition=_optional_enum(payload.get("disposition"), EntryDisposition),
                decision_stage=_optional_enum(payload.get("decision_stage"), EntryDecisionStage),
                compatibility_action=_optional_text(payload.get("compatibility_action")),
                compatibility_reason=_optional_text(payload.get("compatibility_reason")),
                compatibility_source=_required_text(payload, "compatibility_source"),
                provenance=_required_text(payload, "provenance"),
                outcome=ObservationOutcome(payload["outcome"]),
                failure_code=_optional_enum(payload.get("failure_code"), ObservationFailureCode),
                error_type=_optional_text(payload.get("error_type")),
                error_message=_optional_text(payload.get("error_message")),
                scan_result=Stage0ScanResult.from_dict(payload["scan_result"])
                if payload.get("scan_result") is not None
                else None,
                result_fingerprint=_required_text(payload, "result_fingerprint"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ObservationSchemaError):
                raise
            raise ObservationSchemaError(f"invalid observation: {exc}") from exc
        expected_fingerprint = _result_fingerprint(observation)
        if observation.result_fingerprint != expected_fingerprint:
            raise ObservationSchemaError("observation result_fingerprint does not match canonical content")
        return observation


@dataclass(frozen=True)
class Stage0ObservationCycle:
    cycle_id: str
    created_at_ms: int
    observations: tuple[Stage0Observation, ...]
    schema_version: int = OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ObservationSchemaError(f"unsupported cycle schema_version: {self.schema_version}")
        if not self.cycle_id:
            raise ValueError("cycle_id is required")
        ids: set[str] = set()
        for observation in self.observations:
            if observation.cycle_id != self.cycle_id:
                raise ValueError("observation cycle_id must match cycle")
            if observation.observation_id in ids:
                raise ValueError("observation_id must be unique within a cycle")
            ids.add(observation.observation_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cycle_id": self.cycle_id,
            "created_at_ms": self.created_at_ms,
            "observations": [observation.to_dict() for observation in self.observations],
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> "Stage0ObservationCycle":
        if not isinstance(payload, dict):
            raise ObservationSchemaError("cycle must be an object")
        _require_exact_fields(payload, {"schema_version", "cycle_id", "created_at_ms", "observations"}, "cycle")
        if payload.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
            raise ObservationSchemaError(f"unsupported cycle schema_version: {payload.get('schema_version')}")
        observations = payload.get("observations")
        if not isinstance(observations, list):
            raise ObservationSchemaError("cycle observations must be a list")
        try:
            return cls(
                schema_version=payload["schema_version"],
                cycle_id=_required_text(payload, "cycle_id"),
                created_at_ms=_required_int(payload, "created_at_ms"),
                observations=tuple(Stage0Observation.from_dict(item) for item in observations),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ObservationSchemaError):
                raise
            raise ObservationSchemaError(f"invalid cycle: {exc}") from exc


class JsonlObservationRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.invalid_marker_path = Path(f"{self.path}.invalid")

    def append_cycle(self, cycle: Stage0ObservationCycle) -> None:
        encoded = (cycle.to_json() + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.invalid_marker_path.exists():
                raise OSError("observation repository has an unresolved invalid marker")
            self._write_invalid_marker(cycle.cycle_id, exclusive=True)
            log_existed = self.path.exists()
            with self.path.open("ab") as handle:
                written = handle.write(encoded)
                if written != len(encoded):
                    raise OSError(f"short observation write: {written}/{len(encoded)} bytes")
                handle.flush()
                os.fsync(handle.fileno())
            if not log_existed:
                self._fsync_parent_directory()
            self.invalid_marker_path.unlink()
            try:
                self._fsync_parent_directory()
            except OSError:
                try:
                    self._write_invalid_marker(cycle.cycle_id, exclusive=False)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise ObservationWriteError(f"observation cycle write failed: {type(exc).__name__}") from exc

    def read_cycles(self) -> tuple[Stage0ObservationCycle, ...]:
        self._reject_invalid_marker()
        if not self.path.exists():
            return ()
        cycles: list[Stage0ObservationCycle] = []
        try:
            with self.path.open("rb") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if not raw_line.endswith(b"\n"):
                        raise ObservationCorruptionError(f"corrupted observation record at line {line_number}: incomplete")
                    try:
                        payload = json.loads(raw_line.decode("utf-8"))
                        cycles.append(Stage0ObservationCycle.from_dict(payload))
                    except (UnicodeDecodeError, json.JSONDecodeError, ObservationSchemaError, ValueError) as exc:
                        raise ObservationCorruptionError(
                            f"corrupted observation record at line {line_number}: {type(exc).__name__}"
                        ) from exc
        except OSError as exc:
            raise ObservationCorruptionError(f"observation repository read failed: {type(exc).__name__}") from exc
        self._reject_invalid_marker()
        return tuple(cycles)

    def _reject_invalid_marker(self) -> None:
        if self.invalid_marker_path.exists():
            raise ObservationCorruptionError("observation repository has an unresolved failed-write marker")

    def _write_invalid_marker(self, cycle_id: str, *, exclusive: bool) -> None:
        mode = "xb" if exclusive else "wb"
        with self.invalid_marker_path.open(mode) as marker:
            marker.write(cycle_id.encode("utf-8"))
            marker.flush()
            os.fsync(marker.fileno())
        self._fsync_parent_directory()

    def _fsync_parent_directory(self) -> None:
        _fsync_directory(self.path.parent)


def build_stage0_observation(
    *,
    observation_id: str,
    cycle_id: str,
    symbol: str,
    created_at_ms: int,
    observed_at_ms: int,
    trusted: TrustedObservationReference,
    strategy_name: str,
    strategy_config_fingerprint: str,
    result: EntryScanResult | None,
    compatibility_source: str,
    provenance: str,
    failure_code: ObservationFailureCode | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> Stage0Observation:
    if result is not None and result.symbol != symbol:
        raise ValueError("scan result symbol must match observation symbol")
    if result is not None and result.decision_code is EntryDecisionCode.UNKNOWN:
        raise ValueError("scan result decision_code must not be UNKNOWN")
    if failure_code is not None and result is not None:
        raise ValueError("typed failure and scan result are mutually exclusive")
    if failure_code is None and result is None:
        raise ValueError("observation requires a typed scan result or failure_code")

    decision_code = result.decision_code if result is not None else None
    disposition = result.disposition if result is not None else None
    decision_stage = result.stage if result is not None else None
    if failure_code in {
        ObservationFailureCode.TRUSTED_DATA_BLOCKED,
        ObservationFailureCode.TRUSTED_DATA_LOAD_FAILED,
        ObservationFailureCode.TRUSTED_PROVENANCE_INCOMPLETE,
    }:
        outcome = ObservationOutcome.DATA_GATE_BLOCKED
        decision_code = EntryDecisionCode.MARKET_DATA_UNAVAILABLE
        disposition = EntryDisposition.BLOCK
        decision_stage = EntryDecisionStage.INPUT
    elif failure_code is not None:
        outcome = ObservationOutcome.SCAN_FAILED
    elif not trusted.allowed or (disposition is EntryDisposition.BLOCK and decision_stage is EntryDecisionStage.INPUT):
        outcome = ObservationOutcome.DATA_GATE_BLOCKED
    elif disposition is EntryDisposition.READY:
        outcome = ObservationOutcome.READY_FOR_REVIEW
    elif disposition in {EntryDisposition.WAIT, EntryDisposition.BLOCK}:
        outcome = ObservationOutcome.NORMAL_NO_ACTION
    else:
        raise ValueError("scan result decision_code does not map to a closed observation outcome")

    scan_result = Stage0ScanResult.from_entry_result(result) if result is not None else None
    draft = Stage0Observation(
        observation_id=observation_id,
        cycle_id=cycle_id,
        symbol=symbol,
        created_at_ms=created_at_ms,
        observed_at_ms=observed_at_ms,
        trusted_run_id=trusted.run_id,
        requested_intervals=trusted.requested_intervals,
        effective_intervals=trusted.effective_intervals,
        content_sha256_by_interval=dict(trusted.content_sha256_by_interval),
        trust_policy_name=trusted.policy_name,
        trust_policy_version=trusted.policy_version,
        trust_allowed=trusted.allowed,
        trust_reason=trusted.reason,
        strategy_name=strategy_name,
        strategy_config_fingerprint=strategy_config_fingerprint,
        decision_code=decision_code,
        disposition=disposition,
        decision_stage=decision_stage,
        compatibility_action=result.action if result is not None else None,
        compatibility_reason=sanitize_observation_text(result.reason) if result is not None else None,
        compatibility_source=sanitize_observation_text(compatibility_source),
        provenance=sanitize_observation_text(provenance),
        outcome=outcome,
        failure_code=failure_code,
        error_type=sanitize_observation_text(error_type) if error_type else None,
        error_message=sanitize_observation_text(error_message) if error_message else None,
        scan_result=scan_result,
        result_fingerprint="0" * 64,
    )
    object.__setattr__(draft, "result_fingerprint", _result_fingerprint(draft))
    return draft


def sanitize_observation_text(value: str) -> str:
    sanitized = _SECRET_JSON_PATTERN.sub(lambda match: f'{match.group(1)}"[REDACTED]"', str(value))
    sanitized = _SECRET_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", sanitized)
    sanitized = " ".join(sanitized.replace("\r", " ").replace("\n", " ").split())
    return sanitized[:MAX_DIAGNOSTIC_LENGTH]


def canonical_payload_sha256(payload: Any) -> str:
    if is_dataclass(payload) and not isinstance(payload, type):
        payload = asdict(payload)
    return canonical_sha256(payload)


def _result_fingerprint(observation: Stage0Observation) -> str:
    payload = {
        "schema_version": observation.schema_version,
        "symbol": observation.symbol,
        "trusted": {
            "run_id": observation.trusted_run_id,
            "requested_intervals": list(observation.requested_intervals),
            "effective_intervals": list(observation.effective_intervals),
            "content_sha256_by_interval": dict(observation.content_sha256_by_interval),
            "policy_name": observation.trust_policy_name,
            "policy_version": observation.trust_policy_version,
            "allowed": observation.trust_allowed,
            "reason": observation.trust_reason.value,
        },
        "strategy_name": observation.strategy_name,
        "strategy_config_fingerprint": observation.strategy_config_fingerprint,
        "decision_code": observation.decision_code.value if observation.decision_code else None,
        "disposition": observation.disposition.value if observation.disposition else None,
        "decision_stage": observation.decision_stage.value if observation.decision_stage else None,
        "outcome": observation.outcome.value,
        "failure_code": observation.failure_code.value if observation.failure_code else None,
        "scan_result": observation.scan_result.to_dict() if observation.scan_result else None,
        "provenance": observation.provenance,
    }
    return canonical_sha256(payload)


def _validate_observation_semantics(observation: Stage0Observation) -> None:
    if observation.created_at_ms < 0 or observation.observed_at_ms < 0:
        raise ValueError("observation timestamps must be non-negative")
    if not observation.requested_intervals:
        raise ValueError("observation requested_intervals must not be empty")
    if not set(observation.requested_intervals).issubset(observation.effective_intervals):
        raise ValueError("observation requested_intervals must be a subset of effective_intervals")
    if (
        observation.trust_policy_name != STAGE0_TRUST_POLICY_NAME
        or observation.trust_policy_version != STAGE0_TRUST_POLICY_VERSION
    ):
        raise ValueError("Stage 0 observation requires the trading_strict policy identity/version")
    for interval, digest in observation.content_sha256_by_interval.items():
        if not interval or not _SHA256_PATTERN.fullmatch(digest):
            raise ValueError("observation content SHA-256 values must be lowercase 64-character hex")
    if observation.trust_allowed:
        if observation.trust_reason is not HealthReason.OK:
            raise ValueError("allowed observation trust reason must be OK")
        if not observation.trusted_run_id:
            raise ValueError("allowed observation requires trusted run_id")
        missing = set(observation.effective_intervals) - set(observation.content_sha256_by_interval)
        if missing:
            raise ValueError(f"allowed observation requires content SHA-256 for {sorted(missing)}")
    if observation.scan_result is not None and observation.scan_result.symbol != observation.symbol:
        raise ValueError("observation scan_result symbol must match")
    for diagnostic in (
        observation.compatibility_reason,
        observation.compatibility_source,
        observation.provenance,
        observation.error_type,
        observation.error_message,
    ):
        if diagnostic is not None and sanitize_observation_text(diagnostic) != diagnostic:
            raise ValueError("observation diagnostic fields must be sanitized")

    data_failures = {
        ObservationFailureCode.TRUSTED_DATA_BLOCKED,
        ObservationFailureCode.TRUSTED_DATA_LOAD_FAILED,
        ObservationFailureCode.TRUSTED_PROVENANCE_INCOMPLETE,
    }
    scanner_failures = {
        ObservationFailureCode.SCANNER_EXCEPTION,
        ObservationFailureCode.SCANNER_RESULT_INVALID,
    }
    if observation.failure_code in data_failures:
        if observation.trust_allowed or observation.scan_result is not None:
            raise ValueError("data-gate failure cannot be trusted-allowed or contain a scan result")
        if (
            observation.decision_code is not EntryDecisionCode.MARKET_DATA_UNAVAILABLE
            or observation.disposition is not EntryDisposition.BLOCK
            or observation.decision_stage is not EntryDecisionStage.INPUT
        ):
            raise ValueError("data-gate failure decision metadata must be the canonical input block")
        expected_outcome = ObservationOutcome.DATA_GATE_BLOCKED
    elif observation.failure_code in scanner_failures:
        if not observation.trust_allowed or observation.scan_result is not None:
            raise ValueError("scanner failure requires allowed trusted data and no scan result")
        if any(
            value is not None
            for value in (observation.decision_code, observation.disposition, observation.decision_stage)
        ):
            raise ValueError("scanner failure cannot claim typed decision metadata")
        expected_outcome = ObservationOutcome.SCAN_FAILED
    elif observation.failure_code is None:
        if observation.scan_result is None:
            raise ValueError("successful observation requires a scan result")
        if not observation.trust_allowed:
            raise ValueError("successful observation requires allowed trusted data")
        if any(
            value is None
            for value in (observation.decision_code, observation.disposition, observation.decision_stage)
        ):
            raise ValueError("successful observation requires typed decision metadata")
        if observation.decision_code is EntryDecisionCode.UNKNOWN:
            raise ValueError("successful observation decision_code must not be UNKNOWN")
        metadata = entry_decision_metadata(observation.decision_code)
        if observation.disposition is not metadata.disposition or observation.decision_stage is not metadata.stage:
            raise ValueError("observation decision metadata does not match decision_code catalog")
        if (
            observation.disposition is EntryDisposition.BLOCK
            and observation.decision_stage is EntryDecisionStage.INPUT
        ):
            expected_outcome = ObservationOutcome.DATA_GATE_BLOCKED
        elif observation.disposition is EntryDisposition.READY:
            expected_outcome = ObservationOutcome.READY_FOR_REVIEW
        elif observation.disposition in {EntryDisposition.WAIT, EntryDisposition.BLOCK}:
            expected_outcome = ObservationOutcome.NORMAL_NO_ACTION
        else:
            raise ValueError("typed decision metadata does not map to a closed observation outcome")
    else:
        raise ValueError("unsupported observation failure_code")
    if observation.outcome is not expected_outcome:
        raise ValueError("observation outcome does not match typed control fields")


def _fsync_directory(directory: Path) -> None:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_write = 0x40000000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    handle = create_file(
        str(directory),
        generic_write,
        share_read_write_delete,
        None,
        open_existing,
        file_flag_backup_semantics,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(directory))
    try:
        if not flush_file_buffers(handle):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(directory))
    finally:
        close_handle(handle)


def _require_exact_fields(payload: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise ObservationSchemaError(f"{label} has unknown fields: {sorted(unknown)}")
    if missing:
        raise ObservationSchemaError(f"{label} is missing fields: {sorted(missing)}")


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ObservationSchemaError(f"{key} must be a non-empty string")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ObservationSchemaError("optional text field must be a string or null")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ObservationSchemaError(f"{key} must be an integer")
    return value


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ObservationSchemaError(f"{key} must be a boolean")
    return value


def _text_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ObservationSchemaError(f"{key} must be a string list")
    return tuple(value)


def _text_mapping(payload: dict[str, Any], key: str) -> dict[str, str]:
    value = payload.get(key)
    if not isinstance(value, dict) or any(
        not isinstance(item_key, str) or not isinstance(item_value, str) for item_key, item_value in value.items()
    ):
        raise ObservationSchemaError(f"{key} must be a string mapping")
    return dict(value)


def _optional_enum(value: Any, enum_type):
    if value is None:
        return None
    return enum_type(value)
