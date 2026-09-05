from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from mu_strategy.canonical import canonical_json
from mu_strategy.fs_durability import fsync_directory
from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
from mu_strategy.observations import ObservationOutcome, Stage0ObservationCycle


SCHEMA_VERSION = 1
EVENT_LIMIT = 100
MAX_STATE_BYTES = 4 * 1024 * 1024


class StepStatus(str, Enum):
    NOT_RUN = "not_run"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class Phase(str, Enum):
    IDLE = "idle"
    REFRESH = "refresh"
    SCAN = "scan"


class ServiceBusyError(RuntimeError):
    pass


class HealthStateError(ValueError):
    pass


@dataclass(frozen=True)
class RefreshHealth:
    status: StepStatus
    run_id: str | None = None
    attempt_status: RefreshAttemptStatus | None = None
    snapshot_usability: SnapshotUsability | None = None
    exit_code: int | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value, "run_id": self.run_id,
            "attempt_status": self.attempt_status.value if self.attempt_status else None,
            "snapshot_usability": self.snapshot_usability.value if self.snapshot_usability else None,
            "exit_code": self.exit_code, "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: Any) -> RefreshHealth:
        value = _object(value, cls)
        result = cls(
            StepStatus(value["status"]), _optional_text(value["run_id"]),
            RefreshAttemptStatus(value["attempt_status"]) if value["attempt_status"] is not None else None,
            SnapshotUsability(value["snapshot_usability"]) if value["snapshot_usability"] is not None else None,
            _integer(value["exit_code"], minimum=-2**31) if value["exit_code"] is not None else None,
            _optional_text(value["error_code"]),
        )
        if result.status in {StepStatus.SUCCEEDED, StepStatus.DEGRADED}:
            expected = (RefreshAttemptStatus.SUCCESS, 0) if result.status is StepStatus.SUCCEEDED else (RefreshAttemptStatus.DEGRADED, 2)
            if (result.attempt_status, result.exit_code) != expected or result.snapshot_usability is not SnapshotUsability.USABLE or not result.run_id:
                raise HealthStateError("refresh success requires a matching usable publication")
        if result.status is StepStatus.SUCCEEDED and result.error_code is not None:
            raise HealthStateError("successful refresh cannot claim an error")
        return result


@dataclass(frozen=True)
class ScanHealth:
    status: StepStatus
    cycle: Stage0ObservationCycle | None = None
    persistence: StepStatus = StepStatus.NOT_RUN
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value, "cycle": self.cycle.to_dict() if self.cycle else None,
            "persistence": self.persistence.value, "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ScanHealth:
        value = _object(value, cls)
        result = cls(
            StepStatus(value["status"]), Stage0ObservationCycle.from_dict(value["cycle"]) if value["cycle"] is not None else None,
            StepStatus(value["persistence"]), _optional_text(value["error_code"]),
        )
        if result.status is StepStatus.SUCCEEDED:
            if result.cycle is None or result.persistence not in {StepStatus.SUCCEEDED, StepStatus.FAILED}:
                raise HealthStateError("completed scan requires a cycle and a persistence result")
        elif result.cycle is not None or result.persistence is not StepStatus.NOT_RUN:
            raise HealthStateError("incomplete scan cannot claim cycle or persistence evidence")
        if result.status is StepStatus.DEGRADED:
            raise HealthStateError("scan completion has no degraded state")
        if result.status is StepStatus.SUCCEEDED and result.persistence is StepStatus.SUCCEEDED and result.error_code is not None:
            raise HealthStateError("completed persisted scan cannot claim a process error")
        return result


@dataclass(frozen=True)
class CycleHealth:
    number: int
    started_at_ms: int
    completed_at_ms: int
    refresh: RefreshHealth
    scan: ScanHealth

    def problems(self) -> tuple[str, ...]:
        problems = []
        if self.refresh.status is not StepStatus.SUCCEEDED:
            problems.append(f"refresh.{self.refresh.status.value}")
        if self.scan.status is not StepStatus.SUCCEEDED:
            problems.append(f"scan.{self.scan.status.value}")
        if self.scan.cycle is not None:
            outcomes = {item.outcome for item in self.scan.cycle.observations}
            if ObservationOutcome.DATA_GATE_BLOCKED in outcomes:
                problems.append("data.blocked")
            if ObservationOutcome.SCAN_FAILED in outcomes:
                problems.append("scan.failed")
            if self.refresh.status is StepStatus.SUCCEEDED and any(
                item.trust_allowed and item.trusted_run_id != self.refresh.run_id for item in self.scan.cycle.observations
            ):
                problems.append("data.publication_changed")
        if self.scan.persistence is not StepStatus.SUCCEEDED:
            problems.append(f"persistence.{self.scan.persistence.value}")
        return tuple(problems)

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "started_at_ms": self.started_at_ms, "completed_at_ms": self.completed_at_ms,
                "refresh": self.refresh.to_dict(), "scan": self.scan.to_dict()}

    @classmethod
    def from_dict(cls, value: Any) -> CycleHealth:
        value = _object(value, cls)
        result = cls(_integer(value["number"], minimum=1), _integer(value["started_at_ms"]),
                     _integer(value["completed_at_ms"]), RefreshHealth.from_dict(value["refresh"]), ScanHealth.from_dict(value["scan"]))
        if result.completed_at_ms < result.started_at_ms:
            raise HealthStateError("cycle completion precedes start")
        return result


@dataclass(frozen=True)
class HealthEvent:
    sequence: int
    at_ms: int
    kind: str
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "at_ms": self.at_ms, "kind": self.kind, "problems": list(self.problems)}

    @classmethod
    def from_dict(cls, value: Any) -> HealthEvent:
        value = _object(value, cls)
        kind = _text(value["kind"])
        if kind not in {"started", "restarted", "fault", "recovered", "stopped", "interruption_acknowledged"}:
            raise HealthStateError("unknown health event kind")
        return cls(_integer(value["sequence"], minimum=1), _integer(value["at_ms"]), kind, _texts(value["problems"]))


@dataclass(frozen=True)
class ServiceState:
    data_dir: str
    symbols: tuple[str, ...]
    run_id: str
    running: bool
    phase: Phase
    started_at_ms: int
    updated_at_ms: int
    deadline_ms: int
    last_cycle: CycleHealth | None
    consecutive_failures: int
    events: tuple[HealthEvent, ...]
    event_sequence: int
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "data_dir": self.data_dir, "symbols": list(self.symbols),
            "run_id": self.run_id, "running": self.running, "phase": self.phase.value,
            "started_at_ms": self.started_at_ms,
            "updated_at_ms": self.updated_at_ms, "deadline_ms": self.deadline_ms,
            "last_cycle": self.last_cycle.to_dict() if self.last_cycle else None,
            "consecutive_failures": self.consecutive_failures,
            "events": [event.to_dict() for event in self.events], "event_sequence": self.event_sequence,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ServiceState:
        value = _object(value, cls)
        if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
            raise HealthStateError("unsupported health schema")
        if type(value["running"]) is not bool or type(value["events"]) is not list:
            raise HealthStateError("invalid running/events type")
        result = cls(
            _text(value["data_dir"]), _texts(value["symbols"]), _text(value["run_id"]), value["running"],
            Phase(value["phase"]), _integer(value["started_at_ms"]), _integer(value["updated_at_ms"]), _integer(value["deadline_ms"]),
            CycleHealth.from_dict(value["last_cycle"]) if value["last_cycle"] is not None else None,
            _integer(value["consecutive_failures"]), tuple(HealthEvent.from_dict(item) for item in value["events"]),
            _integer(value["event_sequence"]),
        )
        if not result.symbols or len(set(result.symbols)) != len(result.symbols):
            raise HealthStateError("service symbols must be nonempty and unique")
        if result.started_at_ms > result.updated_at_ms or result.deadline_ms < result.updated_at_ms or (not result.running and result.phase is not Phase.IDLE):
            raise HealthStateError("invalid service phase/deadline")
        if len(result.events) > EVENT_LIMIT:
            raise HealthStateError("health event retention exceeded")
        expected = list(range(result.event_sequence - len(result.events) + 1, result.event_sequence + 1))
        if [event.sequence for event in result.events] != expected or bool(result.events) != bool(result.event_sequence):
            raise HealthStateError("non-contiguous event sequence")
        if result.last_cycle is not None:
            if result.last_cycle.completed_at_ms > result.updated_at_ms:
                raise HealthStateError("last cycle is newer than service state")
            cycle = result.last_cycle.scan.cycle
            if cycle is not None and sorted(item.symbol for item in cycle.observations) != sorted(result.symbols):
                raise HealthStateError("scan cycle must cover each configured symbol exactly once")
        return result

    def events_since(self, sequence: int) -> tuple[HealthEvent, ...]:
        _integer(sequence)
        oldest = self.events[0].sequence - 1 if self.events else self.event_sequence
        if not oldest <= sequence <= self.event_sequence:
            raise HealthStateError("event cursor outside retained range; reconcile current health explicitly")
        return tuple(event for event in self.events if event.sequence > sequence)


class HealthStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        if not self.data_dir.name:
            raise ValueError("data directory must not be a filesystem root")
        self.root = self.data_dir.parent / f"{self.data_dir.name}-signal-service"
        self.path = self.root / "health.json"
        self.lock_path = self.root / "service.lock"
        self.liveness_path = self.root / "supervisor.lock"

    def prepare(self) -> None:
        missing = []
        directory = self.root
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        # Repair the deepest existing entry before extending it: it may be the
        # last mkdir from an earlier attempt whose parent fsync failed.
        if directory != directory.parent:
            fsync_directory(directory.parent)
        for directory in reversed(missing):
            directory.mkdir(exist_ok=True)
            fsync_directory(directory.parent)

    def read(self) -> ServiceState | None:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(raw) > MAX_STATE_BYTES:
            raise HealthStateError("health state exceeds size limit")
        try:
            state = ServiceState.from_dict(decode_health_json(raw))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HealthStateError("health state is corrupt or unsupported") from exc
        if state.data_dir != str(self.data_dir):
            raise HealthStateError("health state belongs to another data directory")
        return state

    def write(self, state: ServiceState) -> None:
        canonical = ServiceState.from_dict(state.to_dict())
        if canonical.data_dir != str(self.data_dir):
            raise HealthStateError("health state belongs to another data directory")
        raw = canonical_json(canonical.to_dict()).encode("utf-8") + b"\n"
        if len(raw) > MAX_STATE_BYTES:
            raise HealthStateError("health state exceeds size limit")
        temporary = None
        self.prepare()
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix="health-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            fsync_directory(self.root)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        self.prepare()
        with self.lock_path.open("a+b") as stream:
            _lock(stream)
            try:
                with self.liveness_path.open("a+b") as liveness:
                    # Queries share this second lock. Waiting behind those brief
                    # probes cannot turn a first start into an instance conflict.
                    _lock(liveness, wait=True)
                    try:
                        yield
                    finally:
                        _unlock(liveness)
            finally:
                _unlock(stream)

    def is_running(self) -> bool:
        try:
            stream = self.liveness_path.open("r+b")
        except FileNotFoundError:
            return False
        with stream:
            try:
                _lock(stream, shared=True)
            except ServiceBusyError:
                return True
            _unlock(stream)
            return False


def health_view(state: ServiceState | None, *, running: bool, now_ms: int) -> dict[str, Any]:
    if state is None:
        return {"runtime": "not_started", "healthy": False, "problems": ["runtime.not_started"]}
    if not running:
        runtime = "interrupted" if state.running else "stopped"
    elif not state.running:
        runtime = "starting"
    elif now_ms < state.updated_at_ms or now_ms > state.deadline_ms:
        runtime = "unresponsive"
    else:
        runtime = "running"
    problems = list(state.last_cycle.problems()) if state.last_cycle else ["no_completed_cycle"]
    if state.last_cycle and state.last_cycle.started_at_ms < state.started_at_ms:
        problems.append("no_completed_cycle_in_current_run")
    if runtime != "running":
        problems.append(f"runtime.{runtime}")
    observations = state.last_cycle.scan.cycle.observations if state.last_cycle and state.last_cycle.scan.cycle else ()
    data_status = "unknown" if not observations else ("allowed" if all(item.trust_allowed for item in observations) else "blocked")
    return {
        "runtime": runtime, "healthy": not problems, "problems": problems,
        "phase": state.phase.value, "run_id": state.run_id, "updated_at_ms": state.updated_at_ms,
        "started_at_ms": state.started_at_ms,
        "deadline_ms": state.deadline_ms, "consecutive_failures": state.consecutive_failures,
        "data_at_last_scan": {"status": data_status, "reasons": sorted({item.trust_reason.value for item in observations}),
                              "checked_at_ms": state.last_cycle.completed_at_ms if state.last_cycle else None},
        "last_cycle": state.last_cycle.to_dict() if state.last_cycle else None,
        "event_sequence": state.event_sequence, "events": [event.to_dict() for event in state.events],
    }


def _lock(stream, *, shared: bool = False, wait: bool = False) -> None:
    stream.seek(0)
    try:
        if os.name == "nt":
            _windows_lock(stream, shared=shared, wait=wait)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError as exc:
        raise ServiceBusyError("signal service already owns this data directory") from exc


def decode_health_json(raw: str | bytes) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HealthStateError("duplicate JSON field")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique_object)


def _unlock(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        _windows_lock(stream, release=True)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _windows_lock(stream, *, shared: bool = False, wait: bool = False, release: bool = False) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = api.UnlockFileEx if release else api.LockFileEx
    operation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    if not release:
        operation.argtypes += [wintypes.DWORD]
    operation.argtypes += [ctypes.POINTER(Overlapped)]
    operation.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(stream.fileno())
    overlap = Overlapped()
    args = [handle, 0, 1, 0, ctypes.byref(overlap)] if release else [
        handle, (0 if wait else 1) | (0 if shared else 2), 0, 1, 0, ctypes.byref(overlap),
    ]
    if not operation(*args):
        code = ctypes.get_last_error()
        if not release and code == 33:  # ERROR_LOCK_VIOLATION
            raise ServiceBusyError("signal service already owns this data directory")
        raise ctypes.WinError(code)


def _object(value: Any, cls) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {item.name for item in fields(cls)}:
        raise HealthStateError(f"invalid {cls.__name__} fields")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise HealthStateError("invalid integer")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise HealthStateError("invalid text")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _texts(value: Any) -> tuple[str, ...]:
    if type(value) is not list:
        raise HealthStateError("expected text list")
    return tuple(_text(item) for item in value)
