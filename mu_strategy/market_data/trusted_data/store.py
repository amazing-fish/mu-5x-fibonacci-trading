from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mu_strategy.market_data.cache import CSV_FIELDS
from mu_strategy.market_data.trusted_data.contracts import (
    HealthReason,
    ManifestSchemaError,
    TrustedManifestSnapshot,
    trusted_manifest_snapshot_from_dict,
)
from mu_strategy.models import Candle

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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


class TrustedDataStore:
    def __init__(self, *, data_dir: Path):
        self.data_dir = Path(data_dir)

    def flat_cache_path(self, symbol: str, interval: str) -> Path:
        symbol = validate_storage_segment(symbol, field="symbol")
        interval = validate_storage_segment(interval, field="interval")
        return self.data_dir / "okx" / symbol / f"{interval}.csv"

    @property
    def flat_manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def current_path(self) -> Path:
        return self.data_dir / "current.json"

    @property
    def generations_dir(self) -> Path:
        return self.data_dir / "generations"

    @property
    def run_log_path(self) -> Path:
        return self.data_dir / "refresh_runs.jsonl"

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
            reader = csv.DictReader(handle)
            return [Candle.from_csv_row(row) for row in reader]

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

    def read_manifest(self, *, compatibility_mode: bool = False) -> ManifestReadResult:
        if self.current_path.exists():
            return self._read_current_manifest()
        path = self.flat_manifest_path
        if not path.exists():
            if compatibility_mode:
                payload = {"schema_version": 1, "symbols": {}, "status": "missing"}
                try:
                    return ManifestReadResult(
                        trusted_manifest_snapshot_from_dict(payload, compatibility_mode=True),
                        payload,
                        generation_root=self.data_dir,
                        manifest_path=path,
                    )
                except ManifestSchemaError as exc:
                    return ManifestReadResult(None, payload, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc))
            return ManifestReadResult(None, None, HealthReason.MANIFEST_MISSING)
        return self._read_manifest_file(path, compatibility_mode=compatibility_mode, generation_root=self.data_dir)

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
            compatibility_mode=False,
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
        compatibility_mode: bool = False,
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
            snapshot = trusted_manifest_snapshot_from_dict(payload, compatibility_mode=compatibility_mode)
            if generation_id is not None:
                if snapshot.run_id != generation_id:
                    raise ManifestSchemaError("generation manifest run_id must match current generation_id")
                _validate_generation_source_files(snapshot, generation_root)
        except Exception as exc:
            return ManifestReadResult(None, payload, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc), generation_root=generation_root, generation_id=generation_id, manifest_path=path)
        return ManifestReadResult(snapshot, payload, generation_root=generation_root, generation_id=generation_id, manifest_path=path)

    def write_manifest(self, manifest: dict[str, Any]) -> Path:
        path = self.flat_manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return path

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
        _atomic_write_text(self.current_path, json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True))
        return self.current_path

    def commit_generation_publication(
        self,
        generation_id: str,
        manifest: dict[str, Any],
        run_log_payload: dict[str, Any],
    ) -> None:
        self.write_generation_manifest(generation_id, manifest)
        self.replace_current(generation_id)
        try:
            self.append_run_log(run_log_payload)
        except Exception as exc:
            raise RuntimeError(f"audit log append failed: {exc}") from exc

    def resolve_source_file(self, source_file: Path, *, generation_root: Path, generation_id: str | None) -> Path:
        source_file = Path(source_file)
        if generation_id is None:
            if str(source_file) == "":
                raise ManifestSchemaError("manifest dataset source_file must be non-empty")
            return source_file if source_file.is_absolute() else self.data_dir / source_file
        return _resolve_generation_source_file(source_file, generation_root)

    def append_run_log(self, payload: dict[str, Any]) -> Path:
        path = self.run_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            _flush_and_fsync(handle)
        return path


def candles_content_sha256(candles: list[Candle]) -> str:
    digest = hashlib.sha256()
    for candle in candles:
        row = candle.to_csv_row()
        for field in CSV_FIELDS:
            digest.update(str(row[field]).encode("utf-8"))
            digest.update(b"\0")
        digest.update(b"\n")
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    tmp_path = _tmp_path(path)
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            _flush_and_fsync(handle)
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        _cleanup_tmp(tmp_path)


def _tmp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def _flush_and_fsync(handle) -> None:
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
