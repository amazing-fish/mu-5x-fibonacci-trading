from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum, unique
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Protocol

from mu_strategy import file_locks
from mu_strategy.canonical import canonical_json, canonical_sha256
from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.core.trading_calendar import calendar_sha256
from mu_strategy.fs_durability import fsync_directory as _fsync_directory
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import EntryDecisionCode, EntryDecisionStage, EntryDisposition, entry_decision_metadata


OBSERVATION_SCHEMA_VERSION = 2
_EVALUATION_FIELDS = {"evaluated_candle_open_ms", "evaluated_candle_close_ms", "calendar_id", "calendar_sha256", "calendar_session", "trading_windows_et"}
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
    evaluated_candle_open_ms: int | None = None
    evaluated_candle_close_ms: int | None = None
    calendar_id: str | None = None
    calendar_sha256: str | None = None
    calendar_session: str | None = None
    trading_windows_et: tuple[tuple[str, str], ...] | None = None

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

        context = [getattr(self, name) for name in _EVALUATION_FIELDS]
        if any(value is not None for value in context):
            if any(value is None for value in context):
                raise ValueError("scan evaluation context must be complete")
            if (type(self.evaluated_candle_open_ms) is not int or self.evaluated_candle_open_ms < 0
                    or type(self.evaluated_candle_close_ms) is not int
                    or self.evaluated_candle_close_ms != self.evaluated_candle_open_ms + 900_000):
                raise ValueError("scan evaluation must identify one 15m candle")
            if self.calendar_sha256 != calendar_sha256(self.calendar_id):
                raise ValueError("scan calendar digest mismatch")
            if self.calendar_session not in {"open", "early_close", "holiday", "weekend"}:
                raise ValueError("unsupported scan calendar session")
            windows = self.trading_windows_et
            if (not isinstance(windows, (list, tuple)) or not windows
                    or any(not isinstance(w, (list, tuple)) or len(w) != 2
                           or any(not isinstance(t, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t) for t in w)
                           or w[0] > w[1] for w in windows)):
                raise ValueError("invalid evaluated trading windows")
            object.__setattr__(self, "trading_windows_et", tuple(tuple(w) for w in windows))

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
            **{name: getattr(result, name) for name in _EVALUATION_FIELDS},
        )

    def to_dict(self, *, schema_version=OBSERVATION_SCHEMA_VERSION) -> dict[str, Any]:
        payload = asdict(self)
        if schema_version == 1:
            for name in _EVALUATION_FIELDS:
                payload.pop(name)
        elif self.trading_windows_et is not None:
            payload["trading_windows_et"] = [list(w) for w in self.trading_windows_et]
        return payload

    @classmethod
    def from_dict(cls, payload: Any, *, schema_version=OBSERVATION_SCHEMA_VERSION) -> "Stage0ScanResult":
        if not isinstance(payload, dict):
            raise ObservationSchemaError("scan_result must be an object")
        expected = set(cls.__dataclass_fields__)
        if schema_version == 1:
            expected -= _EVALUATION_FIELDS
        _require_exact_fields(payload, expected, "scan_result")
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
        if type(self.schema_version) is not int or self.schema_version not in (1, 2):
            raise ObservationSchemaError(f"unsupported observation schema_version: {self.schema_version}")
        if not self.observation_id or not self.cycle_id or not self.symbol:
            raise ValueError("observation_id, cycle_id, and symbol are required")
        if not _SHA256_PATTERN.fullmatch(self.strategy_config_fingerprint):
            raise ValueError("strategy_config_fingerprint must be lowercase SHA-256")
        if not _SHA256_PATTERN.fullmatch(self.result_fingerprint):
            raise ValueError("result_fingerprint must be lowercase SHA-256")
        immutable_hashes = MappingProxyType(dict(self.content_sha256_by_interval))
        object.__setattr__(self, "content_sha256_by_interval", immutable_hashes)
        if self.schema_version == 1 and self.scan_result is not None and any(
                getattr(self.scan_result, name) is not None for name in _EVALUATION_FIELDS):
            raise ObservationSchemaError("V1 cannot contain V2 evaluation context")
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
            "scan_result": self.scan_result.to_dict(schema_version=self.schema_version) if self.scan_result else None,
            "result_fingerprint": self.result_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Stage0Observation":
        if not isinstance(payload, dict):
            raise ObservationSchemaError("observation must be an object")
        expected = {field for field in cls.__dataclass_fields__}
        _require_exact_fields(payload, expected, "observation")
        if type(payload.get("schema_version")) is not int or payload.get("schema_version") not in (1, 2):
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
                scan_result=Stage0ScanResult.from_dict(payload["scan_result"], schema_version=payload["schema_version"])
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
        if type(self.schema_version) is not int or self.schema_version not in (1, 2):
            raise ObservationSchemaError(f"unsupported cycle schema_version: {self.schema_version}")
        if not self.cycle_id:
            raise ValueError("cycle_id is required")
        ids: set[str] = set()
        for observation in self.observations:
            if observation.schema_version != self.schema_version:
                raise ValueError("observation schema_version must match cycle")
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
        if type(payload.get("schema_version")) is not int or payload.get("schema_version") not in (1, 2):
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


_MAX_RECORD_BYTES = 4 * 1024 * 1024


def _unique_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationCorruptionError("duplicate observation field")
        result[key] = value
    return result


def _decode_cycle_bytes(raw: bytes, *, strict_unique: bool = True) -> Stage0ObservationCycle:
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_unique_json_fields if strict_unique else None)
        return Stage0ObservationCycle.from_dict(payload)
    except ObservationCorruptionError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ObservationCorruptionError(f"invalid observation record: {type(exc).__name__}") from exc


_COMMIT_SLOT_BYTES = 160
_COMMIT_SLOT_VERSION = "v2"
_INDEX_ENTRY_STRUCT = struct.Struct("<QQQq16s16s16s")
_INDEX_ENTRY_BYTES = _INDEX_ENTRY_STRUCT.size
_ZERO_CHAIN_SHA256 = "0" * 64


@dataclass(frozen=True)
class ObservationCommitState:
    seq: int
    offset: int
    index_offset: int
    prefix_max_created_at_ms: int
    chain_sha256: str


@dataclass(frozen=True)
class ObservationIndexEntry:
    offset: int
    length: int
    created_at_ms: int
    prefix_max_created_at_ms: int
    cycle_id_hash16: bytes
    content_hash16: bytes
    symbols_hash16: bytes


def _symbols_hash16(cycle: Stage0ObservationCycle) -> bytes:
    symbols = sorted({observation.symbol for observation in cycle.observations})
    return hashlib.sha256(canonical_json(symbols).encode("utf-8")).digest()[:16]


def _write_all(handle, payload: bytes) -> None:
    written = 0
    view = memoryview(payload)
    while written < len(payload):
        chunk = handle.write(view[written:])
        if chunk is None or chunk <= 0:
            raise OSError(f"short observation write: {written}/{len(payload)} bytes")
        written += chunk


def _encode_commit_slot(state: ObservationCommitState) -> bytes:
    payload = (
        f"{_COMMIT_SLOT_VERSION} {state.seq} {state.offset} {state.index_offset} "
        f"{state.prefix_max_created_at_ms} {state.chain_sha256}"
    )
    tag = hashlib.sha256(payload.encode("ascii")).hexdigest()[:16]
    line = f"{payload} {tag}"
    if len(line) >= _COMMIT_SLOT_BYTES:
        raise OSError("observation commit slot overflow")
    return (line.ljust(_COMMIT_SLOT_BYTES - 1) + "\n").encode("ascii")


def _decode_commit_slot(raw: bytes) -> ObservationCommitState | None:
    if len(raw) != _COMMIT_SLOT_BYTES or not raw.endswith(b"\n"):
        return None
    try:
        parts = raw.decode("ascii").strip().split()
    except UnicodeDecodeError:
        return None
    if len(parts) != 7 or parts[0] != _COMMIT_SLOT_VERSION:
        return None
    try:
        seq = int(parts[1])
        offset = int(parts[2])
        index_offset = int(parts[3])
        prefix_max = int(parts[4])
    except ValueError:
        return None
    chain_sha256 = parts[5]
    tag = parts[6]
    if (
        seq < 0
        or offset < 0
        or index_offset < 0
        or index_offset != seq * _INDEX_ENTRY_BYTES
        or (seq == 0 and (offset != 0 or prefix_max != -1))
        or (seq > 0 and prefix_max < 0)
        or not _SHA256_PATTERN.fullmatch(chain_sha256)
    ):
        return None
    payload = f"{_COMMIT_SLOT_VERSION} {seq} {offset} {index_offset} {prefix_max} {chain_sha256}"
    expected_tag = hashlib.sha256(payload.encode("ascii")).hexdigest()[:16]
    if tag != expected_tag:
        return None
    return ObservationCommitState(
        seq=seq,
        offset=offset,
        index_offset=index_offset,
        prefix_max_created_at_ms=prefix_max,
        chain_sha256=chain_sha256,
    )


def _pack_index_entry(
    *,
    offset: int,
    length: int,
    created_at_ms: int,
    prefix_max_created_at_ms: int,
    cycle_id_hash16: bytes,
    content_hash16: bytes,
    symbols_hash16: bytes,
) -> bytes:
    return _INDEX_ENTRY_STRUCT.pack(
        offset,
        length,
        created_at_ms,
        prefix_max_created_at_ms,
        cycle_id_hash16,
        content_hash16,
        symbols_hash16,
    )


def _unpack_index_entry(raw: bytes) -> ObservationIndexEntry:
    if len(raw) != _INDEX_ENTRY_BYTES:
        raise ObservationCorruptionError("truncated observation index entry")
    offset, length, created_at_ms, prefix_max, cycle_id_hash16, content_hash16, symbols_hash16 = (
        _INDEX_ENTRY_STRUCT.unpack(raw)
    )
    return ObservationIndexEntry(
        offset=offset,
        length=length,
        created_at_ms=created_at_ms,
        prefix_max_created_at_ms=prefix_max,
        cycle_id_hash16=cycle_id_hash16,
        content_hash16=content_hash16,
        symbols_hash16=symbols_hash16,
    )


class _RetryLockedRead(Exception):
    pass


class JsonlObservationRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.invalid_marker_path = Path(f"{self.path}.invalid")
        self.lock_path = Path(f"{self.path}.lock")
        self.commit_path = Path(f"{self.path}.commit")
        self.index_path = Path(f"{self.path}.index")
        self.dirsync_path = Path(f"{self.path}.dirsync")

    def publication_fence(
        self,
        *,
        wait: bool = True,
        shared: bool = False,
        timeout_seconds: float | None = None,
    ):
        """Serialize committed appends with a consumer's local pre-send readiness check."""
        kwargs: dict[str, Any] = {"wait": wait}
        if shared:
            kwargs["shared"] = True
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = timeout_seconds
        return file_locks.locked_file(self.lock_path, **kwargs)

    @contextmanager
    def read_fence(self, *, wait: bool = True, timeout_seconds: float | None = None) -> Iterator[bool]:
        """Acquire a shared reader lock on an existing lock file without mutating missing/read-only dirs."""
        try:
            stream = self.lock_path.open("rb")
        except FileNotFoundError:
            yield False
            return
        with stream:
            kwargs: dict[str, Any] = {"shared": True, "wait": wait}
            if timeout_seconds is not None:
                kwargs["timeout_seconds"] = timeout_seconds
            file_locks.lock_file(stream, **kwargs)
            try:
                yield True
            finally:
                file_locks.unlock_file(stream)

    def inspect_commit_slots(self) -> tuple[ObservationCommitState | None, bool]:
        """Inspect both ping-pong commit slots, returning ``(highest_valid_state, has_unreadable_slot)``.

        Every repository initialized or migrated by this code maintains a 2-slot (320-byte) commit file.
        Any file length other than ``2 * _COMMIT_SLOT_BYTES`` (including exact 160-byte truncation of
        slot 1) sets ``has_unreadable_slot=True`` so automatic tail repair will never truncate bytes that
        a missing or damaged second slot could have acknowledged.
        """
        try:
            raw = self.commit_path.read_bytes()
        except FileNotFoundError:
            return None, False
        if not raw or len(raw) > 2 * _COMMIT_SLOT_BYTES:
            raise ObservationCorruptionError("corrupted observation commit boundary")
        has_unreadable_slot = len(raw) != 2 * _COMMIT_SLOT_BYTES
        candidates: list[ObservationCommitState] = []
        slot_count = len(raw) // _COMMIT_SLOT_BYTES
        for slot_idx in range(slot_count):
            start = slot_idx * _COMMIT_SLOT_BYTES
            parsed = _decode_commit_slot(raw[start : start + _COMMIT_SLOT_BYTES])
            if parsed is None:
                has_unreadable_slot = True
            else:
                candidates.append(parsed)
        if not candidates:
            raise ObservationCorruptionError("corrupted observation commit boundary")
        if len(candidates) == 2 and abs(candidates[0].seq - candidates[1].seq) > 1:
            has_unreadable_slot = True
        return max(candidates, key=lambda item: item.seq), has_unreadable_slot

    def read_commit_state(self) -> ObservationCommitState | None:
        """Read the highest valid slot from the 2-slot ping-pong commit file, or None if absent."""
        state, _ = self.inspect_commit_slots()
        return state

    @staticmethod
    def read_index_entry(index_handle, index: int) -> ObservationIndexEntry:
        index_handle.seek(index * _INDEX_ENTRY_BYTES)
        return _unpack_index_entry(index_handle.read(_INDEX_ENTRY_BYTES))

    @staticmethod
    def find_first_index_with_prefix_max_ge(index_handle, count: int, target_ms: int) -> int:
        """Binary search monotonic prefix_max_created_at_ms for the first entry >= target_ms."""
        lo, hi = 0, count
        while lo < hi:
            mid = (lo + hi) // 2
            entry = JsonlObservationRepository.read_index_entry(index_handle, mid)
            if entry.prefix_max_created_at_ms < target_ms:
                lo = mid + 1
            else:
                hi = mid
        return lo

    @staticmethod
    def read_cycle_at(handle, *, offset: int, length: int) -> tuple[Stage0ObservationCycle, str]:
        """Read and decode a single record at an exact byte span, returning (cycle, canonical_sha256)."""
        if length <= 0 or length > _MAX_RECORD_BYTES:
            raise ObservationCorruptionError("observation record incomplete or oversized")
        handle.seek(offset)
        raw = handle.read(length)
        if len(raw) != length or not raw.endswith(b"\n"):
            raise ObservationCorruptionError("observation record incomplete or oversized")
        cycle = _decode_cycle_bytes(raw)
        return cycle, canonical_sha256(cycle.to_dict())

    def iter_forward_cycles(self, *, start_offset: int = 0) -> Iterator[tuple[int, int, Stage0ObservationCycle, str]]:
        """Yield validated (offset, length, cycle, canonical_sha256) tuples in publication order from start_offset."""
        if type(start_offset) is not int or start_offset < 0:
            raise ObservationCorruptionError("invalid start_offset")
        while True:
            locked = False
            yielded_any = False
            try:
                with self.read_fence() as locked:
                    self._reject_invalid_marker()
                    commit = self.read_commit_state()
                    try:
                        handle = self.path.open("rb")
                    except FileNotFoundError:
                        self._reject_invalid_marker()
                        if not locked and self.lock_path.exists():
                            continue
                        if start_offset or (commit is not None and commit.offset > 0):
                            raise ObservationCorruptionError("observation log missing before durable commit boundary")
                        return
                    if not locked and self.lock_path.exists():
                        handle.close()
                        continue
                    with handle:
                        handle.seek(0, os.SEEK_END)
                        file_size = handle.tell()
                        if commit is not None and file_size < commit.offset:
                            raise ObservationCorruptionError("observation log truncated before durable commit boundary")
                        if commit is not None and file_size > commit.offset:
                            raise ObservationCorruptionError("uncommitted observation bytes beyond durable commit boundary")
                        if start_offset > file_size:
                            raise ObservationCorruptionError("observation start_offset exceeds file size")
                        handle.seek(start_offset)
                        while True:
                            pos = handle.tell()
                            raw = handle.readline(_MAX_RECORD_BYTES + 1)
                            if not raw:
                                break
                            if len(raw) > _MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                                raise ObservationCorruptionError("observation record incomplete or oversized")
                            cycle = _decode_cycle_bytes(raw)
                            digest = canonical_sha256(cycle.to_dict())
                            if not yielded_any and not locked and self.lock_path.exists():
                                raise _RetryLockedRead()
                            yielded_any = True
                            yield pos, len(raw), cycle, digest
                    self._reject_invalid_marker()
                    return
            except _RetryLockedRead:
                continue
            except ObservationCorruptionError:
                if not yielded_any and not locked and self.lock_path.exists():
                    continue
                raise
            except OSError as exc:
                raise ObservationCorruptionError(f"observation repository read failed: {type(exc).__name__}") from exc

    def repair_uncommitted_tail(self) -> bool:
        """Truncate and fsync provably uncommitted trailing log/index bytes past a clean commit boundary.

        Explicit safety policy: if any commit slot is unreadable (cannot distinguish post-acknowledgment
        slot damage from a torn slot write), fail closed without truncating so acknowledged cycles are
        never silently deleted.
        """
        try:
            if not self.path.parent.exists():
                return False
            with self.publication_fence():
                if self.invalid_marker_path.exists():
                    raise OSError("observation repository has an unresolved invalid marker")
                commit, has_unreadable_slot = self.inspect_commit_slots()
                if commit is None:
                    return False
                if not self.path.exists() or not self.index_path.exists():
                    raise OSError("observation log or index missing for committed repository")
                return self._repair_uncommitted_tail_locked(commit, has_unreadable_slot=has_unreadable_slot)
        except (OSError, ObservationCorruptionError) as exc:
            raise ObservationWriteError(f"observation commit-boundary repair failed: {type(exc).__name__}: {exc}") from exc

    def _repair_uncommitted_tail_locked(
        self,
        commit: ObservationCommitState,
        *,
        has_unreadable_slot: bool,
    ) -> bool:
        """Must be called under publication_fence().

        Truncates trailing bytes ONLY when every slot in ``.commit`` is valid and consistent (proving
        no commit slot write could cover the extra bytes). When any commit slot is unreadable, fails
        closed without truncating so a previously acknowledged cycle is never silently deleted.
        """
        log_size = self.path.stat().st_size
        index_size = self.index_path.stat().st_size
        if log_size < commit.offset or index_size < commit.index_offset:
            raise OSError("observation log or index truncated before durable commit boundary")
        if log_size == commit.offset and index_size == commit.index_offset:
            return False
        if has_unreadable_slot:
            raise OSError(
                "observation commit boundary has an unreadable slot; refusing automatic tail truncation "
                "to preserve potentially acknowledged cycles"
            )

        # Verify the committed boundary is intact before discarding uncommitted trailing bytes.
        if commit.seq > 0:
            with self.index_path.open("rb") as idx_handle, self.path.open("rb") as log_handle:
                last_entry = self.read_index_entry(idx_handle, commit.seq - 1)
                if (
                    last_entry.offset + last_entry.length != commit.offset
                    or last_entry.prefix_max_created_at_ms != commit.prefix_max_created_at_ms
                ):
                    raise OSError("committed index boundary disagrees with durable commit state")
                last_cycle, last_digest = self.read_cycle_at(
                    log_handle, offset=last_entry.offset, length=last_entry.length
                )
                if (
                    last_cycle.created_at_ms != last_entry.created_at_ms
                    or hashlib.sha256(last_cycle.cycle_id.encode("utf-8")).digest()[:16] != last_entry.cycle_id_hash16
                    or bytes.fromhex(last_digest)[:16] != last_entry.content_hash16
                    or _symbols_hash16(last_cycle) != last_entry.symbols_hash16
                ):
                    raise OSError("committed log boundary disagrees with durable index state")

        try:
            if log_size > commit.offset:
                with self.path.open("r+b", buffering=0) as log_handle:
                    log_handle.truncate(commit.offset)
                    os.fsync(log_handle.fileno())
            if index_size > commit.index_offset:
                with self.index_path.open("r+b", buffering=0) as index_handle:
                    index_handle.truncate(commit.index_offset)
                    os.fsync(index_handle.fileno())
        except BaseException:
            self._quarantine_invalid_marker("commit-boundary-repair")
            raise
        return True

    def append_cycle(self, cycle: Stage0ObservationCycle) -> None:
        encoded = (cycle.to_json() + "\n").encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            raise ObservationWriteError(
                f"observation cycle exceeds maximum record size ({len(encoded)} > {_MAX_RECORD_BYTES} bytes)"
            )
        try:
            self._ensure_parent_directories_staged()
            with self.publication_fence():
                if self.invalid_marker_path.exists():
                    raise OSError("observation repository has an unresolved invalid marker")
                self._flush_dirsync_marker()
                commit, has_unreadable_slot = self._ensure_storage_initialized()
                self._repair_uncommitted_tail_locked(commit, has_unreadable_slot=has_unreadable_slot)
                cycle_id_hash16 = hashlib.sha256(cycle.cycle_id.encode("utf-8")).digest()[:16]
                content_hash16 = hashlib.sha256(encoded[:-1]).digest()[:16]
                symbols_hash16 = _symbols_hash16(cycle)
                new_prefix_max = (
                    cycle.created_at_ms
                    if commit.seq == 0
                    else max(commit.prefix_max_created_at_ms, cycle.created_at_ms)
                )
                with self.path.open("ab", buffering=0) as log_handle, self.index_path.open("ab", buffering=0) as index_handle:
                    original_size = os.fstat(log_handle.fileno()).st_size
                    original_index_size = os.fstat(index_handle.fileno()).st_size
                    if original_size != commit.offset or original_index_size != commit.index_offset:
                        raise OSError("observation log size disagrees with durable commit boundary")
                    index_entry = _pack_index_entry(
                        offset=original_size,
                        length=len(encoded),
                        created_at_ms=cycle.created_at_ms,
                        prefix_max_created_at_ms=new_prefix_max,
                        cycle_id_hash16=cycle_id_hash16,
                        content_hash16=content_hash16,
                        symbols_hash16=symbols_hash16,
                    )
                    new_chain = hashlib.sha256(
                        bytes.fromhex(commit.chain_sha256) + encoded + index_entry
                    ).hexdigest()
                    index_written = False
                    try:
                        _write_all(log_handle, encoded)
                        os.fsync(log_handle.fileno())
                        index_written = True
                        _write_all(index_handle, index_entry)
                        os.fsync(index_handle.fileno())
                    except BaseException:
                        rollback_ok = True
                        try:
                            log_handle.truncate(original_size)
                            os.fsync(log_handle.fileno())
                        except OSError:
                            rollback_ok = False
                        if index_written:
                            try:
                                index_handle.truncate(original_index_size)
                                os.fsync(index_handle.fileno())
                            except OSError:
                                rollback_ok = False
                        if not rollback_ok:
                            self._quarantine_invalid_marker(cycle.cycle_id)
                        raise
                next_commit = ObservationCommitState(
                    seq=commit.seq + 1,
                    offset=original_size + len(encoded),
                    index_offset=original_index_size + _INDEX_ENTRY_BYTES,
                    prefix_max_created_at_ms=new_prefix_max,
                    chain_sha256=new_chain,
                )
                try:
                    self._write_commit_slot(next_commit)
                except BaseException:
                    self._quarantine_invalid_marker(cycle.cycle_id)
                    raise
        except (OSError, ObservationCorruptionError) as exc:
            raise ObservationWriteError(f"observation cycle write failed: {type(exc).__name__}: {exc}") from exc

    def read_cycles(self) -> tuple[Stage0ObservationCycle, ...]:
        while True:
            locked = False
            try:
                with self.read_fence() as locked:
                    self._reject_invalid_marker()
                    commit = self.read_commit_state()
                    try:
                        handle = self.path.open("rb")
                    except FileNotFoundError:
                        self._reject_invalid_marker()
                        if not locked and self.lock_path.exists():
                            continue
                        if commit is not None and commit.offset > 0:
                            raise ObservationCorruptionError("observation log missing before durable commit boundary")
                        return ()
                    cycles: list[Stage0ObservationCycle] = []
                    with handle:
                        for line_number, raw_line in enumerate(handle, start=1):
                            if not raw_line.endswith(b"\n"):
                                raise ObservationCorruptionError(
                                    f"corrupted observation record at line {line_number}: incomplete"
                                )
                            try:
                                payload = json.loads(raw_line.decode("utf-8"))
                                cycle = Stage0ObservationCycle.from_dict(payload)
                            except (UnicodeDecodeError, json.JSONDecodeError, ObservationSchemaError, ValueError) as exc:
                                raise ObservationCorruptionError(
                                    f"corrupted observation record at line {line_number}: {type(exc).__name__}"
                                ) from exc
                            if commit is not None and handle.tell() > commit.offset:
                                raise ObservationCorruptionError(
                                    f"corrupted observation record at line {line_number}: uncommitted trailing record"
                                )
                            cycles.append(cycle)
                        final_pos = handle.tell()
                    if commit is not None and final_pos != commit.offset:
                        raise ObservationCorruptionError("observation log size disagrees with durable commit boundary")
                    self._reject_invalid_marker()
                    if not locked and self.lock_path.exists():
                        continue
                    return tuple(cycles)
            except ObservationCorruptionError:
                if not locked and self.lock_path.exists():
                    continue
                raise
            except OSError as exc:
                raise ObservationCorruptionError(f"observation repository read failed: {type(exc).__name__}") from exc

    def read_batch(
        self, *, offset: int = 0, anchor_start: int = 0, anchor_sha256: str | None = None, limit: int = 100,
    ) -> tuple[tuple[Stage0ObservationCycle, ...], tuple[int, int, str | None]]:
        """Read a bounded committed tail; bind the cursor to its preceding record.

        The caller commits returned evidence and cursor together. Truncation or
        replacement of the consumed boundary requires explicit reconciliation.
        """
        if (type(offset) is not int or type(anchor_start) is not int or type(limit) is not int
                or not 1 <= limit <= 1000 or not 0 <= anchor_start <= offset
                or (offset == 0 and (anchor_start != 0 or anchor_sha256 is not None))
                or (offset > 0 and (anchor_start >= offset or not isinstance(anchor_sha256, str)
                                   or not _SHA256_PATTERN.fullmatch(anchor_sha256)))):
            raise ObservationCorruptionError("invalid observation cursor")
        while True:
            locked = False
            try:
                with self.read_fence() as locked:
                    self._reject_invalid_marker()
                    commit = self.read_commit_state()
                    cursor = (offset, anchor_start, anchor_sha256)
                    try:
                        handle = self.path.open("rb")
                    except FileNotFoundError:
                        self._reject_invalid_marker()
                        if not locked and self.lock_path.exists():
                            continue
                        if offset or (commit is not None and commit.offset > 0):
                            raise ObservationCorruptionError("observation log missing at persisted cursor")
                        return (), cursor
                    cycles = []
                    with handle:
                        if commit is not None:
                            file_size = self.path.stat().st_size
                            if file_size < commit.offset:
                                raise ObservationCorruptionError("observation log truncated before durable commit boundary")
                            if file_size > commit.offset:
                                raise ObservationCorruptionError("uncommitted observation bytes beyond durable commit boundary")
                        if offset:
                            handle.seek(anchor_start)
                            anchor = handle.readline(_MAX_RECORD_BYTES + 1)
                            if handle.tell() != offset or not anchor.endswith(b"\n") or hashlib.sha256(anchor).hexdigest() != anchor_sha256:
                                raise ObservationCorruptionError("observation cursor boundary changed")
                        for _ in range(limit):
                            start = handle.tell()
                            raw = handle.readline(_MAX_RECORD_BYTES + 1)
                            if not raw:
                                break
                            if len(raw) > _MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                                raise ObservationCorruptionError("observation record incomplete or oversized")
                            cycles.append(_decode_cycle_bytes(raw))
                            cursor = (handle.tell(), start, hashlib.sha256(raw).hexdigest())
                    self._reject_invalid_marker()
                    if not locked and self.lock_path.exists():
                        continue
                    return tuple(cycles), cursor
            except ObservationCorruptionError:
                if not locked and self.lock_path.exists():
                    continue
                raise
            except OSError as exc:
                raise ObservationCorruptionError(f"observation repository read failed: {type(exc).__name__}") from exc

    def iter_tail_cycles(
        self,
        *,
        max_cycles: int | None = None,
        chunk_size: int = 65_536,
    ) -> Iterator[Stage0ObservationCycle]:
        """Yield validated cycles from newest to oldest via reverse chunk scans from EOF."""
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise ObservationCorruptionError("invalid max_cycles")
        if type(chunk_size) is not int or chunk_size < 1:
            raise ObservationCorruptionError("invalid chunk_size")
        while True:
            locked = False
            yielded_any = False
            try:
                with self.read_fence() as locked:
                    self._reject_invalid_marker()
                    commit = self.read_commit_state()
                    try:
                        handle = self.path.open("rb")
                    except FileNotFoundError:
                        self._reject_invalid_marker()
                        if not locked and self.lock_path.exists():
                            continue
                        if commit is not None and commit.offset > 0:
                            raise ObservationCorruptionError("observation log missing before durable commit boundary")
                        return
                    if not locked and self.lock_path.exists():
                        handle.close()
                        continue
                    yielded = 0
                    with handle:
                        handle.seek(0, os.SEEK_END)
                        pos = handle.tell()
                        if commit is not None and pos < commit.offset:
                            raise ObservationCorruptionError("observation log truncated before durable commit boundary")
                        if commit is not None and pos > commit.offset:
                            # Validate affected trailing record(s) first so corrupt/duplicate fields surface
                            # their specific diagnostic, then fail closed without yielding uncommitted data.
                            scan_from = commit.offset
                            if scan_from > 0:
                                handle.seek(scan_from - 1)
                                if handle.read(1) != b"\n":
                                    back_start = max(0, scan_from - _MAX_RECORD_BYTES)
                                    handle.seek(back_start)
                                    prefix_chunk = handle.read(scan_from - back_start)
                                    nl_pos = prefix_chunk.rfind(b"\n")
                                    scan_from = back_start + nl_pos + 1 if nl_pos >= 0 else back_start
                            handle.seek(scan_from)
                            trailing = handle.read(_MAX_RECORD_BYTES + 1)
                            if not trailing.endswith(b"\n") or len(trailing) > _MAX_RECORD_BYTES:
                                raise ObservationCorruptionError("observation record incomplete or oversized")
                            for line in trailing.splitlines():
                                _decode_cycle_bytes(line)
                            raise ObservationCorruptionError("uncommitted observation bytes beyond durable commit boundary")
                        if pos == 0:
                            self._reject_invalid_marker()
                            if not locked and self.lock_path.exists():
                                continue
                            return
                        remainder = b""
                        at_eof = True
                        while pos > 0 and (max_cycles is None or yielded < max_cycles):
                            read_size = min(chunk_size, pos)
                            pos -= read_size
                            handle.seek(pos)
                            chunk = handle.read(read_size)
                            if len(chunk) != read_size:
                                raise ObservationCorruptionError("observation log shrank during reverse read")
                            buffer = chunk + remainder
                            if at_eof:
                                at_eof = False
                                if not buffer.endswith(b"\n"):
                                    raise ObservationCorruptionError("observation record incomplete or oversized")
                                buffer = buffer[:-1]
                            parts = buffer.split(b"\n")
                            if pos > 0:
                                remainder = parts[0]
                                if len(remainder) + 1 > _MAX_RECORD_BYTES:
                                    raise ObservationCorruptionError("observation record incomplete or oversized")
                                lines = parts[1:]
                            else:
                                remainder = b""
                                lines = parts
                            for raw_line in reversed(lines):
                                if len(raw_line) + 1 > _MAX_RECORD_BYTES:
                                    raise ObservationCorruptionError("observation record incomplete or oversized")
                                decoded = _decode_cycle_bytes(raw_line)
                                if not yielded_any and not locked and self.lock_path.exists():
                                    raise _RetryLockedRead()
                                yielded_any = True
                                yield decoded
                                yielded += 1
                                if max_cycles is not None and yielded >= max_cycles:
                                    break
                    self._reject_invalid_marker()
                    return
            except _RetryLockedRead:
                continue
            except ObservationCorruptionError:
                if not yielded_any and not locked and self.lock_path.exists():
                    continue
                raise
            except OSError as exc:
                raise ObservationCorruptionError(f"observation repository read failed: {type(exc).__name__}") from exc

    def read_tail(
        self,
        *,
        limit: int = 100,
        min_created_at_ms: int | None = None,
        chunk_size: int = 65_536,
    ) -> tuple[Stage0ObservationCycle, ...]:
        """Read up to ``limit`` most recent cycles from EOF in chronological order."""
        if type(limit) is not int or limit < 1:
            raise ObservationCorruptionError("invalid tail limit")
        if min_created_at_ms is not None and (type(min_created_at_ms) is not int or min_created_at_ms < 0):
            raise ObservationCorruptionError("invalid min_created_at_ms")
        collected: list[Stage0ObservationCycle] = []
        with closing(self.iter_tail_cycles(chunk_size=chunk_size)) as tail:
            for cycle in tail:
                if min_created_at_ms is not None and cycle.created_at_ms < min_created_at_ms:
                    continue
                collected.append(cycle)
                if len(collected) >= limit:
                    break
        self._reject_invalid_marker()
        collected.reverse()
        return tuple(collected)

    def _reject_invalid_marker(self) -> None:
        if self.invalid_marker_path.exists():
            raise ObservationCorruptionError("observation repository has an unresolved failed-write marker")

    def _quarantine_invalid_marker(self, cycle_id: str) -> None:
        """Write an invalid marker only when write rollback or commit advancement fails ambiguously."""
        try:
            with self.invalid_marker_path.open("wb") as marker:
                marker.write(cycle_id.encode("utf-8"))
                marker.flush()
                try:
                    os.fsync(marker.fileno())
                except OSError:
                    pass
            try:
                self._fsync_parent_directory()
            except OSError:
                pass
        except OSError:
            pass

    def _open_sync_commit_handle(self):
        """Open existing commit file with write-through I/O so slot writes hit stable storage before returning."""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            kernel32.CreateFileW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            generic_read_write = 0x80000000 | 0x40000000
            share_all = 0x00000001 | 0x00000002 | 0x00000004
            open_existing = 3
            file_flag_write_through = 0x80000000
            invalid_handle = ctypes.c_void_p(-1).value
            win_handle = kernel32.CreateFileW(
                str(self.commit_path),
                generic_read_write,
                share_all,
                None,
                open_existing,
                file_flag_write_through,
                None,
            )
            if win_handle in (None, invalid_handle):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                fd = msvcrt.open_osfhandle(win_handle, 0)
            except BaseException:
                kernel32.CloseHandle(win_handle)
                raise
            try:
                return os.fdopen(fd, "r+b", buffering=0)
            except BaseException:
                os.close(fd)
                raise
        sync_flag = getattr(os, "O_SYNC", 0) or getattr(os, "O_DSYNC", 0)
        fd = os.open(self.commit_path, os.O_RDWR | sync_flag)
        try:
            return os.fdopen(fd, "r+b", buffering=0)
        except BaseException:
            os.close(fd)
            raise

    def _write_commit_slot(self, state: ObservationCommitState) -> None:
        """Update the target ping-pong slot via synchronous write-through I/O + fsync."""
        slot_bytes = _encode_commit_slot(state)
        slot_offset = (state.seq % 2) * _COMMIT_SLOT_BYTES
        with self._open_sync_commit_handle() as commit_handle:
            commit_handle.seek(slot_offset)
            _write_all(commit_handle, slot_bytes)
            os.fsync(commit_handle.fileno())

    def _write_commit_slot_atomic(self, state: ObservationCommitState) -> None:
        """Publish an initial 2-slot (320-byte) commit file atomically so partial init never poisons retries."""
        slot_bytes = _encode_commit_slot(state)
        tmp_path = self.path.parent / f".{self.commit_path.name}.init-{uuid.uuid4().hex}"
        try:
            with tmp_path.open("wb", buffering=0) as tmp_handle:
                # Populate both ping-pong slots so every repository starts with a full 2-slot commit file.
                _write_all(tmp_handle, slot_bytes + slot_bytes)
                os.fsync(tmp_handle.fileno())
            os.replace(tmp_path, self.commit_path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    _MIGRATION_COMPLETE_BYTES = b"complete\n"

    @classmethod
    def _is_migration_marker_active(cls, commit_staging: Path) -> bool:
        try:
            return commit_staging.read_bytes() != cls._MIGRATION_COMPLETE_BYTES
        except FileNotFoundError:
            return False

    @classmethod
    def _retire_migration_marker(cls, commit_staging: Path) -> None:
        """Durably transition the migration marker to ``complete`` in-place before unlinking.

        Because ``complete`` is fsynced to the marker inode before ``unlink()``, a power loss that
        rolls back the directory-entry removal can never revive an active migration marker, while an
        ``os.fsync`` failure before ``unlink()`` leaves the marker present so a new repository instance
        always retries the sync and cleanup before appending.
        """
        with commit_staging.open("r+b", buffering=0) as staging_handle:
            staging_handle.seek(0)
            _write_all(staging_handle, cls._MIGRATION_COMPLETE_BYTES)
            staging_handle.truncate(len(cls._MIGRATION_COMPLETE_BYTES))
            os.fsync(staging_handle.fileno())
        try:
            commit_staging.unlink()
        except FileNotFoundError:
            pass

    def _ensure_storage_initialized(self) -> tuple[ObservationCommitState, bool]:
        """Initialize durable commit + coverage index on initial log creation or legacy bootstrap."""
        log_exists = self.path.exists()
        log_size = self.path.stat().st_size if log_exists else 0
        commit_exists = self.commit_path.exists()
        index_exists = self.index_path.exists()
        commit_staging = Path(f"{self.path}.commit.staging")

        if log_size == 0:
            needs_commit_upgrade = not commit_exists
            if commit_exists:
                existing, has_unreadable_slot = self.inspect_commit_slots()
                if existing is not None and existing.offset > 0:
                    raise OSError("observation log truncated before durable commit boundary")
                if has_unreadable_slot:
                    # Support retrying an empty repository left with a valid 1-slot (160-byte)
                    # seq=0/offset=0 commit file from an earlier revision; reject all other damage.
                    if (
                        existing is not None
                        and existing.seq == 0
                        and existing.offset == 0
                        and self.commit_path.stat().st_size == _COMMIT_SLOT_BYTES
                        and (not index_exists or self.index_path.stat().st_size == 0)
                    ):
                        needs_commit_upgrade = True
                    else:
                        raise OSError("observation commit boundary has an unreadable slot")
            initial = ObservationCommitState(
                seq=0,
                offset=0,
                index_offset=0,
                prefix_max_created_at_ms=-1,
                chain_sha256=_ZERO_CHAIN_SHA256,
            )
            if needs_commit_upgrade:
                self._write_commit_slot_atomic(initial)
            if not log_exists:
                self.path.open("ab").close()
            if not index_exists or self.index_path.stat().st_size != 0:
                with self.index_path.open("wb", buffering=0) as idx:
                    os.fsync(idx.fileno())
            self._fsync_parent_directory()
            return initial, False

        migration_active = self._is_migration_marker_active(commit_staging)

        if not commit_exists and (not index_exists or migration_active):
            return self._bootstrap_legacy_storage(commit_staging=commit_staging), False

        if commit_exists and not index_exists and migration_active:
            commit, has_unreadable_slot = self.inspect_commit_slots()
            if commit is None:
                raise OSError("observation commit boundary is unreadable")
            if has_unreadable_slot:
                raise OSError(
                    "observation commit boundary has an unreadable slot; refusing to rebuild index "
                    "or remove migration marker until the commit file is restored"
                )
            self._rebuild_index_to_commit_boundary(commit, commit_staging=commit_staging)
            return commit, False

        if not commit_exists or not index_exists:
            raise OSError("observation commit boundary or index is missing for non-empty log")

        if commit_staging.exists():
            # Prior migration published both sidecars (or failed while retiring the marker);
            # sync parent directory entries and durably retire + unlink the marker before appending.
            self._fsync_parent_directory()
            self._retire_migration_marker(commit_staging)

        commit, has_unreadable_slot = self.inspect_commit_slots()
        if commit is None:
            raise OSError("observation commit boundary is unreadable")
        return commit, has_unreadable_slot

    def _rebuild_index_to_commit_boundary(
        self,
        commit: ObservationCommitState,
        *,
        commit_staging: Path,
    ) -> None:
        """Rebuild a missing index strictly up to ``commit.offset``, leaving any trailing bytes for normal repair."""
        if self.path.stat().st_size < commit.offset:
            raise OSError("observation log truncated before durable commit boundary")
        seq = 0
        offset = 0
        prefix_max = -1
        chain = _ZERO_CHAIN_SHA256
        index_chunks: list[bytes] = []
        with self.path.open("rb") as handle:
            while offset < commit.offset:
                start = offset
                remaining = commit.offset - offset
                raw = handle.readline(min(_MAX_RECORD_BYTES + 1, remaining))
                if not raw:
                    raise OSError("observation log ended before durable commit boundary")
                if len(raw) > _MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                    raise OSError("committed observation record incomplete or oversized")
                cycle = _decode_cycle_bytes(raw)
                prefix_max = cycle.created_at_ms if seq == 0 else max(prefix_max, cycle.created_at_ms)
                cycle_id_hash16 = hashlib.sha256(cycle.cycle_id.encode("utf-8")).digest()[:16]
                content_hash16 = bytes.fromhex(canonical_sha256(cycle.to_dict()))[:16]
                symbols_hash16 = _symbols_hash16(cycle)
                entry = _pack_index_entry(
                    offset=start,
                    length=len(raw),
                    created_at_ms=cycle.created_at_ms,
                    prefix_max_created_at_ms=prefix_max,
                    cycle_id_hash16=cycle_id_hash16,
                    content_hash16=content_hash16,
                    symbols_hash16=symbols_hash16,
                )
                index_chunks.append(entry)
                chain = hashlib.sha256(bytes.fromhex(chain) + raw + entry).hexdigest()
                seq += 1
                offset += len(raw)
        if (
            seq != commit.seq
            or offset != commit.offset
            or seq * _INDEX_ENTRY_BYTES != commit.index_offset
            or prefix_max != commit.prefix_max_created_at_ms
            or chain != commit.chain_sha256
        ):
            raise OSError("rebuilt observation index disagrees with durable commit boundary")

        tmp_index = self.path.parent / f".{self.index_path.name}.rebuild-{uuid.uuid4().hex}"
        try:
            with tmp_index.open("wb", buffering=0) as idx_handle:
                if index_chunks:
                    _write_all(idx_handle, b"".join(index_chunks))
                os.fsync(idx_handle.fileno())
            os.replace(tmp_index, self.index_path)
            self._fsync_parent_directory()
            self._retire_migration_marker(commit_staging)
        except BaseException:
            try:
                tmp_index.unlink()
            except OSError:
                pass
            raise

    def _bootstrap_legacy_storage(self, *, commit_staging: Path) -> ObservationCommitState:
        """Stage and atomically publish durable 2-slot commit + index sidecars for a legacy JSONL log."""
        seq = 0
        offset = 0
        index_offset = 0
        prefix_max = -1
        chain = _ZERO_CHAIN_SHA256
        index_chunks: list[bytes] = []
        with self.path.open("rb") as handle:
            while True:
                start = handle.tell()
                raw = handle.readline(_MAX_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > _MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                    raise OSError("legacy observation record incomplete or oversized")
                cycle = _decode_cycle_bytes(raw)
                prefix_max = cycle.created_at_ms if seq == 0 else max(prefix_max, cycle.created_at_ms)
                cycle_id_hash16 = hashlib.sha256(cycle.cycle_id.encode("utf-8")).digest()[:16]
                content_hash16 = bytes.fromhex(canonical_sha256(cycle.to_dict()))[:16]
                symbols_hash16 = _symbols_hash16(cycle)
                entry = _pack_index_entry(
                    offset=start,
                    length=len(raw),
                    created_at_ms=cycle.created_at_ms,
                    prefix_max_created_at_ms=prefix_max,
                    cycle_id_hash16=cycle_id_hash16,
                    content_hash16=content_hash16,
                    symbols_hash16=symbols_hash16,
                )
                index_chunks.append(entry)
                chain = hashlib.sha256(bytes.fromhex(chain) + raw + entry).hexdigest()
                seq += 1
                offset = handle.tell()
                index_offset += _INDEX_ENTRY_BYTES
        state = ObservationCommitState(
            seq=seq,
            offset=offset,
            index_offset=index_offset,
            prefix_max_created_at_ms=prefix_max,
            chain_sha256=chain,
        )
        slot_bytes = _encode_commit_slot(state)
        tmp_index = self.path.parent / f".{self.index_path.name}.migrate-{uuid.uuid4().hex}"
        tmp_commit = self.path.parent / f".{self.commit_path.name}.migrate-{uuid.uuid4().hex}"
        try:
            with tmp_index.open("wb", buffering=0) as idx_handle:
                if index_chunks:
                    _write_all(idx_handle, b"".join(index_chunks))
                os.fsync(idx_handle.fileno())
            with tmp_commit.open("wb", buffering=0) as commit_handle:
                # Always initialize both ping-pong slots (320 bytes) for every migrated repository.
                _write_all(commit_handle, slot_bytes + slot_bytes)
                os.fsync(commit_handle.fileno())
            # Persist and directory-sync the migration marker BEFORE exposing either final sidecar so
            # a power loss after the first os.replace() always leaves the marker durable for retry.
            with commit_staging.open("wb", buffering=0) as staging_handle:
                _write_all(staging_handle, b"migrating\n")
                os.fsync(staging_handle.fileno())
            self._fsync_parent_directory()
            os.replace(tmp_index, self.index_path)
            os.replace(tmp_commit, self.commit_path)
            self._fsync_parent_directory()
            self._retire_migration_marker(commit_staging)
            return state
        except BaseException:
            for leftover in (tmp_index, tmp_commit):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            # Only remove commit_staging if neither final sidecar was exposed yet.
            if not self.index_path.exists() and not self.commit_path.exists():
                try:
                    commit_staging.unlink()
                except OSError:
                    pass
            raise

    def _ensure_parent_directories_staged(self) -> None:
        """Create missing parent directories atomically with a durable .dirsync list before visibility."""
        for _ in range(8):
            if self.path.parent.exists():
                return
            missing: list[Path] = []
            directory = self.path.parent
            while not directory.exists():
                missing.append(directory)
                parent = directory.parent
                if parent == directory:
                    raise OSError("observation repository has no existing parent directory")
                directory = parent
            top_missing = missing[-1]
            existing_ancestor = top_missing.parent
            staging_root = existing_ancestor / f".{top_missing.name}.init-{uuid.uuid4().hex}"
            try:
                rel_leaf = self.path.parent.relative_to(top_missing)
                staged_leaf = staging_root / rel_leaf if rel_leaf != Path(".") else staging_root
                staged_leaf.mkdir(parents=True, exist_ok=True)
                staged_marker = staged_leaf / self.dirsync_path.name
                marker_bytes = json.dumps([str(item) for item in missing], ensure_ascii=True).encode("utf-8")
                with staged_marker.open("wb", buffering=0) as marker_handle:
                    _write_all(marker_handle, marker_bytes)
                    os.fsync(marker_handle.fileno())
                try:
                    os.rename(staging_root, top_missing)
                except OSError:
                    shutil.rmtree(staging_root, ignore_errors=True)
                    if top_missing.exists():
                        continue
                    raise
                return
            except BaseException:
                shutil.rmtree(staging_root, ignore_errors=True)
                raise
        if not self.path.parent.exists():
            raise OSError("failed to initialize observation repository parent directories")

    def _flush_dirsync_marker(self) -> None:
        """Sync any newly created ancestor directory entries recorded in .dirsync before first commit."""
        if not self.dirsync_path.exists():
            return
        try:
            raw_dirs = json.loads(self.dirsync_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OSError(f"unreadable observation dirsync marker: {exc}") from exc
        if not isinstance(raw_dirs, list) or not all(isinstance(item, str) and item for item in raw_dirs):
            raise OSError("invalid observation dirsync marker")
        for directory_str in raw_dirs:
            _fsync_directory(Path(directory_str).parent)
        self.dirsync_path.unlink()

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
        "scan_result": observation.scan_result.to_dict(schema_version=observation.schema_version) if observation.scan_result else None,
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
