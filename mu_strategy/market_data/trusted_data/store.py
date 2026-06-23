from __future__ import annotations

import csv
import json
import os
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


@dataclass(frozen=True)
class ManifestReadResult:
    snapshot: TrustedManifestSnapshot | None
    payload: dict[str, Any] | None = None
    reason: HealthReason | None = None
    error_type: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.snapshot is not None and self.reason is None


class TrustedDataStore:
    def __init__(self, *, data_dir: Path):
        self.data_dir = Path(data_dir)

    def cache_path(self, symbol: str, interval: str) -> Path:
        return self.data_dir / "okx" / symbol / f"{interval}.csv"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def run_log_path(self) -> Path:
        return self.data_dir / "refresh_runs.jsonl"

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
        finally:
            _cleanup_tmp(tmp_path)

    def read_manifest(self, *, compatibility_mode: bool = False) -> ManifestReadResult:
        path = self.manifest_path
        if not path.exists():
            if compatibility_mode:
                payload = {"schema_version": 1, "symbols": {}, "status": "missing"}
                try:
                    return ManifestReadResult(
                        trusted_manifest_snapshot_from_dict(payload, compatibility_mode=True),
                        payload,
                    )
                except ManifestSchemaError as exc:
                    return ManifestReadResult(None, payload, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc))
            return ManifestReadResult(None, None, HealthReason.MANIFEST_MISSING)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return ManifestReadResult(None, None, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc))
        if not isinstance(payload, dict):
            return ManifestReadResult(None, None, HealthReason.MALFORMED_MANIFEST, "TypeError", "manifest root must be object")
        try:
            snapshot = trusted_manifest_snapshot_from_dict(payload, compatibility_mode=compatibility_mode)
        except Exception as exc:
            return ManifestReadResult(None, payload, HealthReason.MALFORMED_MANIFEST, type(exc).__name__, str(exc))
        return ManifestReadResult(snapshot, payload)

    def write_manifest(self, manifest: dict[str, Any]) -> Path:
        path = self.manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return path

    def append_run_log(self, payload: dict[str, Any]) -> Path:
        path = self.run_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            _flush_and_fsync(handle)
        return path


def _atomic_write_text(path: Path, text: str) -> None:
    tmp_path = _tmp_path(path)
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            _flush_and_fsync(handle)
        os.replace(tmp_path, path)
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


def _cleanup_tmp(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
