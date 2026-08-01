from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
import logging
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mu_strategy.fs_durability import fsync_directory as _fsync_directory
from mu_strategy.market_data.cache import CSV_FIELDS
from mu_strategy.market_data.trusted_data.contracts import (
    HealthReason,
    GenerationReclamationFailure,
    GenerationReclamationReport,
    ManifestSchemaError,
    TrustedManifestSnapshot,
    trusted_manifest_snapshot_from_dict,
)
from mu_strategy.models import Candle

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOGGER = logging.getLogger(__name__)
DEFAULT_RUN_LOG_MAX_LINES = 1_000
_CURRENT_POINTER_DIRECTORY_SYNC_WARNING_PREFIX = "current_pointer_directory_sync_failed:"
# Keep post-commit warnings structured without allowing -Werror to turn them into false failures.
_CURRENT_POINTER_WARNING_SINK: ContextVar[Callable[[str], None] | None] = ContextVar(
    "trusted_current_pointer_warning_sink",
    default=None,
)


@dataclass(frozen=True)
class ManifestReadResult:
    snapshot: TrustedManifestSnapshot | None
    payload: dict[str, Any] | None = None
    reason: HealthReason | None = None
    error_type: str | None = None
    message: str | None = None
    generation_root: Path | None = None
    generation_id: str | None = None
    manifest_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.snapshot is not None and self.reason is None


@dataclass(frozen=True)
class GenerationRetentionPolicy:
    keep_recent: int

    def __post_init__(self) -> None:
        if not isinstance(self.keep_recent, int) or isinstance(self.keep_recent, bool) or self.keep_recent < 1:
            raise ValueError("keep_recent must be an integer of at least 1")


class TrustedDataStore:
    def __init__(
        self,
        *,
        data_dir: Path,
        run_log_max_lines: int = DEFAULT_RUN_LOG_MAX_LINES,
        retention_policy: GenerationRetentionPolicy | None = None,
        reclamation_dry_run: bool = False,
    ):
        if not isinstance(run_log_max_lines, int) or isinstance(run_log_max_lines, bool) or run_log_max_lines < 1:
            raise ValueError("run_log_max_lines must be an integer of at least 1")
        self.data_dir = Path(data_dir)
        self.run_log_max_lines = run_log_max_lines
        self.retention_policy = retention_policy
        self.reclamation_dry_run = reclamation_dry_run
        self.last_reclamation_report: GenerationReclamationReport | None = None

    @property
    def current_path(self) -> Path:
        return self.data_dir / "current.json"

    @property
    def generations_dir(self) -> Path:
        return self.data_dir / "generations"

    @property
    def run_log_path(self) -> Path:
        return self.data_dir / "refresh_runs.jsonl"

    @contextmanager
    def publication_snapshot_lock(self):
        if not self.data_dir.is_dir():
            yield
            return
        with _trusted_store_lock(self.data_dir):
            yield

    def generation_root(self, generation_id: str) -> Path:
        generation_id = validate_storage_segment(generation_id, field="generation_id")
        return self.generations_dir / generation_id

    def generation_source_file(self, symbol: str, interval: str) -> Path:
        symbol = validate_storage_segment(symbol, field="symbol")
        interval = validate_storage_segment(interval, field="interval")
        return Path("okx") / symbol / f"{interval}.csv"

    def generation_manifest_path(self, generation_id: str) -> Path:
        return self.generation_root(generation_id) / "manifest.json"

    def generation_cache_path(self, generation_id: str, symbol: str, interval: str) -> Path:
        return self.generation_root(generation_id) / self.generation_source_file(symbol, interval)

    def prepare_generation(self, generation_id: str) -> Path:
        root = self.generation_root(generation_id)
        if root.exists():
            raise FileExistsError(f"trusted generation already exists: {generation_id}")
        root.mkdir(parents=True)
        _fsync_directory(self.generations_dir)
        return root

    def read_csv(self, path: Path) -> list[Candle]:
        with Path(path).open("r", newline="", encoding="utf-8") as handle:
            return _read_candles_csv(handle)

    def read_csv_bytes(self, payload: bytes) -> list[Candle]:
        with io.StringIO(payload.decode("utf-8"), newline="") as handle:
            return _read_candles_csv(handle)

    def read_file_bytes(self, path: Path) -> bytes:
        return Path(path).read_bytes()

    def write_csv(self, candles: list[Candle], path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _tmp_path(path)
        try:
            with tmp_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for candle in candles:
                    writer.writerow(candle.to_csv_row())
                _flush_and_fsync(handle)
            os.replace(tmp_path, path)
            _fsync_directory(path.parent)
        finally:
            _cleanup_tmp(tmp_path)

    def read_manifest(self) -> ManifestReadResult:
        if not self.current_path.exists():
            return ManifestReadResult(None, None, HealthReason.MANIFEST_MISSING)
        return self._read_current_manifest()

    def _read_current_manifest(self) -> ManifestReadResult:
        try:
            pointer = json.loads(self.current_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return ManifestReadResult(None, None, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc))
        try:
            generation_id, manifest_path = self._resolve_current_pointer(pointer)
        except Exception as exc:
            return ManifestReadResult(None, pointer if isinstance(pointer, dict) else None, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc))
        if not manifest_path.exists():
            return ManifestReadResult(
                None,
                pointer,
                HealthReason.MALFORMED_MANIFEST,
                "FileNotFoundError",
                f"current generation manifest is missing: {manifest_path}",
                generation_root=self.generation_root(generation_id),
                generation_id=generation_id,
                manifest_path=manifest_path,
            )
        return self._read_manifest_file(
            manifest_path,
            generation_root=self.generation_root(generation_id),
            generation_id=generation_id,
        )

    def _resolve_current_pointer(self, pointer: Any) -> tuple[str, Path]:
        if not isinstance(pointer, dict):
            raise ManifestSchemaError("current pointer root must be object")
        if pointer.get("schema_version") != 1:
            raise ManifestSchemaError("current pointer schema_version must be 1")
        generation_id = pointer.get("generation_id")
        manifest_value = pointer.get("manifest")
        if not isinstance(generation_id, str) or not generation_id:
            raise ManifestSchemaError("current pointer generation_id must be non-empty string")
        generation_id = validate_storage_segment(generation_id, field="generation_id")
        if not isinstance(manifest_value, str) or not manifest_value:
            raise ManifestSchemaError("current pointer manifest must be non-empty string")
        expected_value = f"generations/{generation_id}/manifest.json"
        if manifest_value != expected_value:
            raise ManifestSchemaError("current pointer manifest must target its generation manifest")
        return generation_id, self.generation_manifest_path(generation_id)

    def _read_manifest_file(
        self,
        path: Path,
        *,
        generation_root: Path,
        generation_id: str | None = None,
    ) -> ManifestReadResult:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return ManifestReadResult(None, None, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc), generation_root=generation_root, generation_id=generation_id, manifest_path=path)
        if not isinstance(payload, dict):
            return ManifestReadResult(None, None, HealthReason.MALFORMED_MANIFEST, "TypeError", "manifest root must be object", generation_root=generation_root, generation_id=generation_id, manifest_path=path)
        try:
            snapshot = trusted_manifest_snapshot_from_dict(payload)
            if generation_id is not None:
                if snapshot.run_id != generation_id:
                    raise ManifestSchemaError("generation manifest run_id must match current generation_id")
                _validate_generation_source_files(snapshot, generation_root)
        except Exception as exc:
            return ManifestReadResult(None, payload, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc), generation_root=generation_root, generation_id=generation_id, manifest_path=path)
        return ManifestReadResult(snapshot, payload, generation_root=generation_root, generation_id=generation_id, manifest_path=path)

    def write_generation_manifest(self, generation_id: str, manifest: dict[str, Any]) -> Path:
        generation_id = validate_storage_segment(generation_id, field="generation_id")
        if manifest.get("run_id") != generation_id:
            raise ManifestSchemaError("generation manifest run_id must match generation_id")
        path = self.generation_manifest_path(generation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        result = self._read_manifest_file(path, generation_root=self.generation_root(generation_id), generation_id=generation_id)
        if not result.ok:
            message = result.message or (result.reason.value if result.reason is not None else "generation manifest is malformed")
            raise ManifestSchemaError(message)
        return path

    def replace_current(self, generation_id: str) -> Path:
        generation_id = validate_storage_segment(generation_id, field="generation_id")
        manifest_path = self.generation_manifest_path(generation_id)
        result = self._read_manifest_file(manifest_path, generation_root=self.generation_root(generation_id), generation_id=generation_id)
        if not result.ok:
            message = result.message or (result.reason.value if result.reason is not None else "generation manifest is malformed")
            raise ManifestSchemaError(message)
        pointer = {
            "schema_version": 1,
            "generation_id": generation_id,
            "manifest": f"generations/{generation_id}/manifest.json",
        }
        self.current_path.parent.mkdir(parents=True, exist_ok=True)
        sync_error = _atomic_write_text(
            self.current_path,
            json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True),
            return_post_replace_directory_sync_error=True,
        )
        if sync_error is not None:
            message = f"current_pointer_directory_sync_failed: {type(sync_error).__name__}: {sync_error}"
            warning_sink = _CURRENT_POINTER_WARNING_SINK.get()
            if warning_sink is None:
                _LOGGER.warning(message)
            else:
                warning_sink(message)
        return self.current_path

    def commit_generation_publication(
        self,
        generation_id: str,
        manifest: dict[str, Any],
        run_log_payload: dict[str, Any],
    ) -> tuple[str, ...]:
        with self.publication_snapshot_lock():
            return self._commit_generation_publication_locked(
                generation_id,
                manifest,
                run_log_payload,
            )

    def _commit_generation_publication_locked(
        self,
        generation_id: str,
        manifest: dict[str, Any],
        run_log_payload: dict[str, Any],
    ) -> tuple[str, ...]:
        self.write_generation_manifest(generation_id, manifest)
        publication_warnings = []
        warning_sink_token = _CURRENT_POINTER_WARNING_SINK.set(publication_warnings.append)
        try:
            self.replace_current(generation_id)
        finally:
            _CURRENT_POINTER_WARNING_SINK.reset(warning_sink_token)
        log_payload = dict(run_log_payload)
        if self.retention_policy is not None:
            pointer_durability_unconfirmed = any(
                warning.startswith(_CURRENT_POINTER_DIRECTORY_SYNC_WARNING_PREFIX)
                for warning in publication_warnings
            )
            if pointer_durability_unconfirmed:
                failure = GenerationReclamationFailure(
                    None,
                    "CurrentPointerDurabilityUnconfirmed",
                    "reclamation skipped because current pointer directory sync failed",
                )
                reclamation = GenerationReclamationReport(
                    dry_run=self.reclamation_dry_run,
                    keep_recent=self.retention_policy.keep_recent,
                    current_generation_id=generation_id,
                    failures=(failure,),
                )
            else:
                try:
                    reclamation = self.reclaim_generations(
                        self.retention_policy,
                        dry_run=self.reclamation_dry_run,
                        _lock_held=True,
                    )
                except Exception as exc:
                    failure = GenerationReclamationFailure(None, type(exc).__name__, str(exc))
                    reclamation = GenerationReclamationReport(
                        dry_run=self.reclamation_dry_run,
                        keep_recent=self.retention_policy.keep_recent,
                        current_generation_id=None,
                        failures=(failure,),
                    )
            self.last_reclamation_report = reclamation
            publication_warnings.extend(reclamation.warnings())
            log_payload["reclamation"] = reclamation.to_dict()
        if publication_warnings:
            log_payload["warnings"] = [
                *list(log_payload.get("warnings") or ()),
                *publication_warnings,
            ]
        try:
            self.append_run_log(log_payload)
        except Exception as exc:
            publication_warnings.append(f"audit_log_append_failed: {exc}")
        return tuple(publication_warnings)

    def resolve_source_file(self, source_file: Path, *, generation_root: Path, generation_id: str) -> Path:
        source_file = Path(source_file)
        return _resolve_generation_source_file(source_file, generation_root)

    def append_run_log(self, payload: dict[str, Any]) -> Path:
        path = self.run_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        existing_lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        if len(existing_lines) >= self.run_log_max_lines:
            keep_count = self.run_log_max_lines - 1
            retained = existing_lines[-keep_count:] if keep_count else []
            _atomic_write_text(path, "".join((*retained, line)))
            return path
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            _flush_and_fsync(handle)
        return path

    def reclaim_generations(
        self,
        policy: GenerationRetentionPolicy,
        *,
        dry_run: bool = False,
        _lock_held: bool = False,
    ) -> GenerationReclamationReport:
        if not _lock_held:
            with self.publication_snapshot_lock():
                return self.reclaim_generations(
                    policy,
                    dry_run=dry_run,
                    _lock_held=True,
                )
        failures: list[GenerationReclamationFailure] = []
        try:
            current_generation_id = self._current_generation_id_for_reclamation()
            entries = tuple(self.generations_dir.iterdir())
        except Exception as exc:
            failures.append(GenerationReclamationFailure(None, type(exc).__name__, str(exc)))
            return GenerationReclamationReport(
                dry_run=dry_run,
                keep_recent=policy.keep_recent,
                current_generation_id=None,
                failures=tuple(failures),
            )

        generations: list[tuple[Path, int]] = []
        for entry in entries:
            try:
                target = self._validated_reclamation_target(entry)
                manifest_path = target / "manifest.json"
                if not manifest_path.exists():
                    continue
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    raise ValueError(f"trusted generation manifest must be a regular file: {target.name}")
                generations.append((target, target.stat().st_mtime_ns))
            except Exception as exc:
                failures.append(GenerationReclamationFailure(entry.name or None, type(exc).__name__, str(exc)))

        try:
            current_target = self._validated_reclamation_target(self.generations_dir / current_generation_id)
        except Exception as exc:
            failures.append(GenerationReclamationFailure(current_generation_id, type(exc).__name__, str(exc)))
            return GenerationReclamationReport(
                dry_run=dry_run,
                keep_recent=policy.keep_recent,
                current_generation_id=current_generation_id,
                failures=tuple(failures),
            )

        ordered = [
            path
            for path, _ in sorted(
                generations,
                key=lambda item: (item[1], item[0].name),
            )
        ]
        protected_ids = {path.name for path in ordered[-policy.keep_recent :]}
        protected_ids.add(current_target.name)
        candidates = tuple(path for path in ordered if path.name not in protected_ids)
        sizes: dict[str, int] = {}
        for candidate in candidates:
            try:
                sizes[candidate.name] = _directory_file_bytes(candidate)
            except Exception as exc:
                failures.append(GenerationReclamationFailure(candidate.name, type(exc).__name__, str(exc)))

        candidate_ids = tuple(candidate.name for candidate in candidates)
        bytes_reclaimable = sum(sizes.values())
        if dry_run:
            return GenerationReclamationReport(
                dry_run=True,
                keep_recent=policy.keep_recent,
                current_generation_id=current_generation_id,
                candidate_ids=candidate_ids,
                bytes_reclaimable=bytes_reclaimable,
                failures=tuple(failures),
            )

        removed_ids: list[str] = []
        bytes_reclaimed = 0
        failed_ids = {failure.generation_id for failure in failures}
        for candidate in candidates:
            if candidate.name in failed_ids:
                continue
            try:
                if self._current_generation_id_for_reclamation() == candidate.name:
                    raise RuntimeError("generation became current during reclamation")
                target = self._validated_reclamation_target(candidate)
                shutil.rmtree(target)
            except Exception as exc:
                failures.append(GenerationReclamationFailure(candidate.name, type(exc).__name__, str(exc)))
                continue
            removed_ids.append(candidate.name)
            bytes_reclaimed += sizes[candidate.name]
        if removed_ids:
            try:
                _fsync_directory(self.generations_dir)
            except Exception as exc:
                failures.append(GenerationReclamationFailure(None, type(exc).__name__, str(exc)))
        return GenerationReclamationReport(
            dry_run=False,
            keep_recent=policy.keep_recent,
            current_generation_id=current_generation_id,
            candidate_ids=candidate_ids,
            removed_ids=tuple(removed_ids),
            bytes_reclaimable=bytes_reclaimable,
            bytes_reclaimed=bytes_reclaimed,
            failures=tuple(failures),
        )

    def _current_generation_id_for_reclamation(self) -> str:
        pointer = json.loads(self.current_path.read_text(encoding="utf-8"))
        generation_id, _ = self._resolve_current_pointer(pointer)
        return generation_id

    def _validated_reclamation_target(self, target: Path) -> Path:
        target = Path(target)
        generation_id = validate_storage_segment(target.name, field="generation_id")
        if target.is_symlink():
            raise ValueError(f"trusted generation must not be a symlink: {generation_id}")
        resolved_root = self.generations_dir.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        if resolved_target.parent != resolved_root:
            raise ValueError(f"trusted generation must stay directly inside generations/: {generation_id}")
        if not resolved_target.is_dir():
            raise ValueError(f"trusted generation must be a directory: {generation_id}")
        return resolved_target


def candles_content_sha256(candles: list[Candle]) -> str:
    digest = hashlib.sha256()
    for candle in candles:
        row = candle.to_csv_row()
        for field in CSV_FIELDS:
            digest.update(str(row[field]).encode("utf-8"))
            digest.update(b"\0")
        digest.update(b"\n")
    return digest.hexdigest()


def _read_candles_csv(handle) -> list[Candle]:
    reader = csv.DictReader(handle)
    return [Candle.from_csv_row(row) for row in reader]


def _directory_file_bytes(root: Path) -> int:
    total = 0
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in directory_names:
            path = current / name
            if path.is_symlink():
                total += path.lstat().st_size
        for name in file_names:
            total += (current / name).lstat().st_size
    return total


@contextmanager
def _trusted_store_lock(data_dir: Path):
    if os.name == "posix":
        import fcntl

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(data_dir, flags)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        return
    if os.name == "nt":
        with _windows_named_mutex(data_dir):
            yield
        return
    raise OSError(errno.ENOTSUP, f"trusted store locking is unsupported on platform: {os.name}")


@contextmanager
def _windows_named_mutex(data_dir: Path):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    canonical_path = str(Path(data_dir).resolve()).casefold()
    mutex_name = f"Local\\mu_strategy_trusted_store_{hashlib.sha256(canonical_path.encode('utf-8')).hexdigest()}"
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    wait_result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
    if wait_result not in {0x00000000, 0x00000080}:
        error = ctypes.WinError(ctypes.get_last_error()) if wait_result == 0xFFFFFFFF else OSError(f"unexpected mutex wait result: {wait_result}")
        kernel32.CloseHandle(handle)
        raise error

    body_error = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        release_error = None
        if not kernel32.ReleaseMutex(handle):
            release_error = ctypes.WinError(ctypes.get_last_error())
        if not kernel32.CloseHandle(handle) and release_error is None:
            release_error = ctypes.WinError(ctypes.get_last_error())
        if release_error is not None and body_error is None:
            raise release_error


def _atomic_write_text(
    path: Path,
    text: str,
    *,
    return_post_replace_directory_sync_error: bool = False,
) -> OSError | None:
    tmp_path = _tmp_path(path)
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            _flush_and_fsync(handle)
        os.replace(tmp_path, path)
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            if return_post_replace_directory_sync_error:
                return exc
            raise
        return None
    finally:
        _cleanup_tmp(tmp_path)


def _tmp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def _flush_and_fsync(handle) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _cleanup_tmp(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _resolve_generation_source_file(source_file: Path, generation_root: Path) -> Path:
    if source_file.is_absolute():
        raise ManifestSchemaError("generation manifest source_file must be relative")
    if any(part == ".." for part in source_file.parts):
        raise ManifestSchemaError("generation manifest source_file cannot contain '..'")
    resolved = (generation_root / source_file).resolve()
    root = generation_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ManifestSchemaError("generation manifest source_file must stay inside generation root")
    return resolved


def _validate_generation_source_files(snapshot: TrustedManifestSnapshot, generation_root: Path) -> None:
    for health in snapshot.datasets.values():
        expected = Path("okx") / validate_storage_segment(health.key.symbol, field="symbol") / f"{validate_storage_segment(health.key.interval, field='interval')}.csv"
        if health.source_file.as_posix() != expected.as_posix():
            raise ManifestSchemaError("generation manifest source_file must equal okx/<symbol>/<interval>.csv")
        _resolve_generation_source_file(health.source_file, generation_root)


def validate_storage_segment(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if value != value.strip():
        raise ValueError(f"{field} must not have surrounding whitespace")
    if value in {"", ".", ".."}:
        raise ValueError(f"{field} must be a storage segment")
    if any(char in value for char in ("/", "\\", ":", "\0")):
        raise ValueError(f"{field} must not contain path separators or drive syntax")
    if not _SEGMENT_RE.fullmatch(value):
        raise ValueError(f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,127}}")
    return value
