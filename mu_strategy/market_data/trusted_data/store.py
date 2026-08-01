from __future__ import annotations

import copy
import csv
import errno
import hashlib
import json
import logging
import os
import re
import time
import uuid
from bisect import bisect_left
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mu_strategy.fs_durability import fsync_directory as _fsync_directory
from mu_strategy.market_data.cache import CSV_FIELDS
from mu_strategy.market_data.utils import interval_to_ms
from mu_strategy.market_data.trusted_data.contracts import (
    AvailabilityState,
    DatasetHealth,
    DatasetStorage,
    HealthReason,
    IntegrityState,
    ManifestSchemaError,
    SegmentReference,
    TrustedManifestSnapshot,
    TrustedStorageLayout,
    trusted_manifest_snapshot_from_dict,
    utc_month_segment_id,
)
from mu_strategy.models import Candle

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOGGER = logging.getLogger(__name__)
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


class SegmentCorrectionError(RuntimeError):
    pass


class TrustedDataStore:
    def __init__(self, *, data_dir: Path):
        self.data_dir = Path(data_dir)

    @property
    def current_path(self) -> Path:
        return self.data_dir / "current.json"

    @property
    def generations_dir(self) -> Path:
        return self.data_dir / "generations"

    @property
    def segments_dir(self) -> Path:
        return self.data_dir / "segments"

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

    def segment_source_root(self, symbol: str, interval: str) -> Path:
        symbol = validate_storage_segment(symbol, field="symbol")
        interval = validate_storage_segment(interval, field="interval")
        return Path("segments") / "okx" / symbol / interval

    def segment_source_file(self, symbol: str, interval: str, segment_id: str) -> Path:
        segment_id = validate_storage_segment(segment_id, field="segment_id")
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", segment_id):
            raise ValueError("segment_id must be UTC YYYY-MM")
        return self.segment_source_root(symbol, interval) / f"{segment_id}.csv"

    def imported_segment_source_file(
        self,
        symbol: str,
        interval: str,
        segment_id: str,
        generation_id: str,
    ) -> Path:
        generation_id = validate_storage_segment(generation_id, field="generation_id")
        canonical = self.segment_source_file(symbol, interval, segment_id)
        return canonical.with_name(f"{segment_id}.import-{generation_id}.csv")

    def segment_path(self, symbol: str, interval: str, segment_id: str) -> Path:
        source_file = self.segment_source_file(symbol, interval, segment_id)
        return self._segment_path_from_source(source_file)

    def _segment_path_from_source(self, source_file: Path) -> Path:
        source_file = Path(source_file)
        if source_file.is_absolute() or any(part == ".." for part in source_file.parts):
            raise ValueError("trusted segment source_file must be a safe relative path")
        candidate = self.data_dir / source_file
        current = candidate
        while current != self.data_dir and current != current.parent:
            if current.is_symlink():
                raise ValueError("trusted segment path must not contain symlinks")
            current = current.parent
        resolved_root = self.segments_dir.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("trusted segment path must stay inside segments root") from exc
        return candidate

    def prepare_generation(self, generation_id: str) -> Path:
        root = self.generation_root(generation_id)
        self._ensure_data_directory(self.generations_dir)
        try:
            root.mkdir()
        except FileExistsError as exc:
            raise FileExistsError(f"trusted generation already exists: {generation_id}") from exc
        _fsync_directory(self.generations_dir)
        return root

    def read_csv(self, path: Path) -> list[Candle]:
        return self._read_csv_slice(path, start_row=0, rows=None, require_eof=True)

    def write_csv(self, candles: list[Candle], path: Path, *, required_prefix: bytes | None = None) -> None:
        path = Path(path)
        self._ensure_data_directory(path.parent)
        tmp_path = _tmp_path(path)
        try:
            with tmp_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for candle in candles:
                    writer.writerow(candle.to_csv_row())
                _flush_and_fsync(handle)
            if required_prefix is not None:
                candidate_bytes = tmp_path.read_bytes()
                if not candidate_bytes.startswith(required_prefix):
                    raise SegmentCorrectionError("growing segment must preserve its existing byte prefix")
            os.replace(tmp_path, path)
            _fsync_directory(path.parent)
        finally:
            _cleanup_tmp(tmp_path)

    def _ensure_data_directory(self, path: Path) -> None:
        data_root = self.data_dir.absolute()
        target = Path(path).absolute()
        try:
            relative = target.relative_to(data_root)
        except ValueError as exc:
            raise ValueError("trusted storage writer path must stay inside data_dir") from exc
        if not data_root.exists():
            data_root.mkdir(parents=True)
            _fsync_directory(data_root.parent)
        if data_root.is_symlink() or not data_root.is_dir():
            raise ValueError("trusted data_dir must be a real directory")
        current = data_root
        for part in relative.parts:
            child = current / part
            try:
                child.mkdir()
            except FileExistsError:
                if child.is_symlink() or not child.is_dir():
                    raise ValueError(f"trusted storage directory must be a real directory: {child}")
            else:
                _fsync_directory(current)
            current = child

    def empty_segmented_storage(self, symbol: str, interval: str) -> DatasetStorage:
        return DatasetStorage(
            layout=TrustedStorageLayout.SEGMENTED_CSV_V1,
            source_root=self.segment_source_root(symbol, interval),
        )

    def write_segmented_dataset(
        self,
        candles: list[Candle],
        *,
        symbol: str,
        interval: str,
        physical_candles: list[Candle] | None = None,
        import_generation_id: str | None = None,
    ) -> DatasetStorage:
        source_root = self.segment_source_root(symbol, interval)
        if not candles:
            return DatasetStorage(
                layout=TrustedStorageLayout.SEGMENTED_CSV_V1,
                source_root=source_root,
            )
        logical_partitions = _partition_candles_by_utc_month(candles)
        physical_partitions = _partition_candles_by_utc_month(
            physical_candles if physical_candles is not None else candles
        )
        segment_directory = self.data_dir / source_root
        self._ensure_data_directory(segment_directory)
        with _exclusive_dataset_writer_lock(segment_directory / ".write.lock"):
            return self._write_segmented_dataset_locked(
                logical_partitions,
                physical_partitions,
                symbol=symbol,
                interval=interval,
                source_root=source_root,
                import_generation_id=import_generation_id,
            )

    def _write_segmented_dataset_locked(
        self,
        logical_partitions: dict[str, list[Candle]],
        physical_partitions: dict[str, list[Candle]],
        *,
        symbol: str,
        interval: str,
        source_root: Path,
        import_generation_id: str | None,
    ) -> DatasetStorage:
        physical_rows_by_segment: dict[str, list[Candle]] = {}
        source_files_by_segment: dict[str, Path] = {}
        first_logical_segment_id = next(iter(logical_partitions))
        for segment_id, candidate_rows in physical_partitions.items():
            _validate_candidate_segment_continuity(
                candidate_rows,
                symbol=symbol,
                interval=interval,
                segment_id=segment_id,
            )
            if (
                import_generation_id is not None
                and segment_id == first_logical_segment_id
                and utc_month_segment_id(logical_partitions[segment_id][0].open_time_ms - 1) == segment_id
            ):
                source_file = self.imported_segment_source_file(
                    symbol,
                    interval,
                    segment_id,
                    import_generation_id,
                )
                path = self._segment_path_from_source(source_file)
            else:
                source_file = self.segment_source_file(symbol, interval, segment_id)
                path = self.segment_path(symbol, interval, segment_id)
            source_files_by_segment[segment_id] = source_file
            if not path.exists():
                self.write_csv(candidate_rows, path)
                physical_rows_by_segment[segment_id] = list(candidate_rows)
                continue

            existing_bytes = path.read_bytes()
            existing = self.read_csv(path)
            _validate_segment_candles(existing, segment_id=segment_id)
            existing_by_timestamp = {candle.open_time_ms: candle for candle in existing}
            for candidate in candidate_rows:
                if candidate.open_time_ms < existing[0].open_time_ms:
                    continue
                if candidate.open_time_ms > existing[-1].open_time_ms:
                    break
                stored = existing_by_timestamp.get(candidate.open_time_ms)
                if stored is None or stored.to_csv_row() != candidate.to_csv_row():
                    raise SegmentCorrectionError(
                        f"historical correction would rewrite trusted segment: {symbol}/{interval}/{segment_id}"
                    )
            suffix_start = bisect_left(
                [candle.open_time_ms for candle in candidate_rows],
                existing[-1].open_time_ms + 1,
            )
            suffix = candidate_rows[suffix_start:]
            if suffix:
                if self._has_later_segment(symbol, interval, segment_id):
                    raise SegmentCorrectionError(
                        f"closed trusted segment cannot grow: {symbol}/{interval}/{segment_id}"
                    )
                combined = [*existing, *suffix]
                _validate_candidate_segment_continuity(
                    combined,
                    symbol=symbol,
                    interval=interval,
                    segment_id=segment_id,
                )
                self.write_csv(combined, path, required_prefix=existing_bytes)
                physical_rows_by_segment[segment_id] = combined
            else:
                physical_rows_by_segment[segment_id] = existing

        references: list[SegmentReference] = []
        segment_items = list(logical_partitions.items())
        for index, (segment_id, segment_candles) in enumerate(segment_items):
            closed = index < len(segment_items) - 1
            physical_rows = physical_rows_by_segment.get(segment_id)
            if physical_rows is None:
                raise SegmentCorrectionError(
                    f"physical month lookbehind is missing logical segment: {symbol}/{interval}/{segment_id}"
                )
            physical_timestamps = [candle.open_time_ms for candle in physical_rows]
            start_row = bisect_left(physical_timestamps, segment_candles[0].open_time_ms)
            if not _canonical_candles_equal(
                physical_rows[start_row : start_row + len(segment_candles)],
                segment_candles,
            ):
                raise SegmentCorrectionError(
                    f"physical month lookbehind does not contain logical dataset: "
                    f"{symbol}/{interval}/{segment_id}"
                )
            if closed and start_row + len(segment_candles) != len(physical_rows):
                raise SegmentCorrectionError(
                    f"closed trusted segment reference must reach physical EOF: {symbol}/{interval}/{segment_id}"
                )
            references.append(
                SegmentReference(
                    segment_id=segment_id,
                    source_file=source_files_by_segment[segment_id],
                    start_row=start_row,
                    rows=len(segment_candles),
                    first_timestamp_ms=segment_candles[0].open_time_ms,
                    last_timestamp_ms=segment_candles[-1].open_time_ms,
                    content_sha256=candles_content_sha256(segment_candles),
                    closed=closed,
                )
            )
        return DatasetStorage(
            layout=TrustedStorageLayout.SEGMENTED_CSV_V1,
            source_root=source_root,
            segments=tuple(references),
        )

    def _has_later_segment(self, symbol: str, interval: str, segment_id: str) -> bool:
        segment_dir = self.data_dir / self.segment_source_root(symbol, interval)
        if not segment_dir.exists():
            return False
        return any(
            candidate.stem > segment_id
            for candidate in segment_dir.glob("*.csv")
            if re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", candidate.stem)
        )

    def read_generation_dataset(
        self,
        snapshot: TrustedManifestSnapshot,
        *,
        symbol: str,
        interval: str,
        generation_root: Path,
        generation_id: str,
        verify_logical: bool = True,
    ) -> list[Candle]:
        key = (symbol, interval)
        health = snapshot.datasets.get(key)
        storage = snapshot.storage_by_dataset.get(key)
        if health is None or storage is None:
            raise ManifestSchemaError(f"trusted manifest is missing dataset storage: {symbol}/{interval}")
        if storage.layout is TrustedStorageLayout.FLAT_CSV_V1:
            path = self.resolve_source_file(
                health.source_file,
                generation_root=generation_root,
                generation_id=generation_id,
            )
            candles = self.read_csv(path)
        elif storage.layout is TrustedStorageLayout.SEGMENTED_CSV_V1:
            candles = self._read_segmented_dataset(
                health,
                storage,
                symbol=symbol,
                interval=interval,
                generation_id=generation_id,
                imported_from_run_id=snapshot.imported_from_run_id,
            )
        else:
            raise ManifestSchemaError(f"unsupported trusted storage layout: {storage.layout}")
        if verify_logical:
            _validate_dataset_candles(candles, health, symbol=symbol, interval=interval)
        return candles

    def dataset_path(
        self,
        snapshot: TrustedManifestSnapshot,
        *,
        symbol: str,
        interval: str,
        generation_root: Path,
        generation_id: str,
    ) -> Path:
        key = (symbol, interval)
        health = snapshot.datasets.get(key)
        storage = snapshot.storage_by_dataset.get(key)
        if health is None or storage is None:
            return generation_root / self.generation_source_file(symbol, interval)
        if storage.layout is TrustedStorageLayout.FLAT_CSV_V1:
            self.resolve_source_file(
                health.source_file,
                generation_root=generation_root,
                generation_id=generation_id,
            )
            return generation_root / health.source_file
        if storage.layout is TrustedStorageLayout.SEGMENTED_CSV_V1:
            expected = self.segment_source_root(symbol, interval)
            if storage.source_root.as_posix() != expected.as_posix():
                raise ManifestSchemaError("schema-v4 dataset source_root does not match dataset key")
            return self.data_dir / expected
        raise ManifestSchemaError(f"unsupported trusted storage layout: {storage.layout}")

    def _read_csv_slice(
        self,
        path: Path,
        *,
        start_row: int,
        rows: int | None,
        require_eof: bool,
    ) -> list[Candle]:
        candles: list[Candle] = []
        with Path(path).open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != CSV_FIELDS:
                raise ManifestSchemaError("trusted CSV header must exactly match CSV_FIELDS")
            for _ in range(start_row):
                try:
                    row = next(reader)
                except StopIteration as exc:
                    raise ManifestSchemaError(
                        "trusted segment has fewer prefix rows than its manifest start_row"
                    ) from exc
                _candle_from_strict_csv_row(row)
            while rows is None or len(candles) < rows:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                candles.append(_candle_from_strict_csv_row(row))
            if rows is not None and len(candles) != rows:
                raise ManifestSchemaError("trusted segment has fewer rows than its manifest reference")
            if require_eof:
                try:
                    next(reader)
                except StopIteration:
                    pass
                else:
                    raise ManifestSchemaError("closed trusted segment has unreferenced extra rows")
        return candles

    def _read_segmented_dataset(
        self,
        health: DatasetHealth,
        storage: DatasetStorage,
        *,
        symbol: str,
        interval: str,
        generation_id: str,
        imported_from_run_id: str | None,
    ) -> list[Candle]:
        expected_root = self.segment_source_root(symbol, interval)
        if storage.source_root.as_posix() != expected_root.as_posix():
            raise ManifestSchemaError("schema-v4 dataset source_root does not match dataset key")
        candles: list[Candle] = []
        for index, reference in enumerate(storage.segments):
            if not _is_allowed_segment_source(
                reference.source_file,
                expected_root=expected_root,
                segment_id=reference.segment_id,
                generation_id=generation_id,
                imported_from_run_id=imported_from_run_id,
                first_reference=index == 0,
            ):
                raise ManifestSchemaError("schema-v4 segment source_file does not match dataset key")
            path = self._segment_path_from_source(reference.source_file)
            segment_candles = self._read_csv_slice(
                path,
                start_row=reference.start_row,
                rows=reference.rows,
                require_eof=reference.closed,
            )
            _validate_segment_candles(segment_candles, segment_id=reference.segment_id)
            if segment_candles[0].open_time_ms != reference.first_timestamp_ms:
                raise ManifestSchemaError("trusted segment first timestamp does not match manifest")
            if segment_candles[-1].open_time_ms != reference.last_timestamp_ms:
                raise ManifestSchemaError("trusted segment last timestamp does not match manifest")
            if candles_content_sha256(segment_candles) != reference.content_sha256:
                raise ManifestSchemaError("trusted segment content SHA-256 mismatch")
            candles.extend(segment_candles)
        return candles

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
                _validate_generation_storage(snapshot, generation_root)
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
        self.write_generation_manifest(generation_id, manifest)
        self.verify_generation(generation_id)
        publication_warnings = []
        warning_sink_token = _CURRENT_POINTER_WARNING_SINK.set(publication_warnings.append)
        try:
            self.replace_current(generation_id)
        finally:
            _CURRENT_POINTER_WARNING_SINK.reset(warning_sink_token)
        try:
            self.append_run_log(run_log_payload)
        except Exception as exc:
            publication_warnings.append(f"audit_log_append_failed: {exc}")
        return tuple(publication_warnings)

    def resolve_source_file(self, source_file: Path, *, generation_root: Path, generation_id: str) -> Path:
        source_file = Path(source_file)
        return _resolve_generation_source_file(source_file, generation_root)

    def read_generation_manifest(self, generation_id: str) -> ManifestReadResult:
        generation_id = validate_storage_segment(generation_id, field="generation_id")
        root = self.generation_root(generation_id)
        return self._read_manifest_file(
            self.generation_manifest_path(generation_id),
            generation_root=root,
            generation_id=generation_id,
        )

    def verify_generation(self, generation_id: str) -> ManifestReadResult:
        result = self.read_generation_manifest(generation_id)
        if (
            not result.ok
            or result.snapshot is None
            or result.generation_root is None
            or result.generation_id is None
        ):
            detail = result.message or (
                result.reason.value if result.reason is not None else "generation manifest is invalid"
            )
            raise ManifestSchemaError(detail)
        for symbol, interval in result.snapshot.datasets:
            self.read_generation_dataset(
                result.snapshot,
                symbol=symbol,
                interval=interval,
                generation_root=result.generation_root,
                generation_id=result.generation_id,
            )
        return result

    def import_flat_generation(
        self,
        source_generation_id: str,
        target_generation_id: str,
        *,
        publish: bool = False,
    ) -> ManifestReadResult:
        source_generation_id = validate_storage_segment(source_generation_id, field="source_generation_id")
        target_generation_id = validate_storage_segment(target_generation_id, field="target_generation_id")
        if source_generation_id == target_generation_id:
            raise ValueError("flat import target_generation_id must differ from source_generation_id")
        source_result = self.read_generation_manifest(source_generation_id)
        if not source_result.ok or source_result.snapshot is None or source_result.payload is None:
            detail = source_result.message or (
                source_result.reason.value if source_result.reason is not None else "source manifest is invalid"
            )
            raise ManifestSchemaError(f"flat import source generation is invalid: {detail}")
        source_snapshot = source_result.snapshot
        if source_snapshot.schema_version != 3:
            raise ManifestSchemaError("flat import source must be schema v3")

        self.prepare_generation(target_generation_id)
        symbols_payload: dict[str, dict[str, Any]] = {}
        for symbol, symbol_entry in (source_result.payload.get("symbols") or {}).items():
            copied_symbol_entry = {
                key: copy.deepcopy(value)
                for key, value in symbol_entry.items()
                if key != "intervals"
            }
            copied_symbol_entry["intervals"] = {}
            symbols_payload[str(symbol)] = copied_symbol_entry

        imported_candles: dict[tuple[str, str], list[Candle]] = {}
        for (symbol, interval), health in source_snapshot.datasets.items():
            candles = self.read_generation_dataset(
                source_snapshot,
                symbol=symbol,
                interval=interval,
                generation_root=source_result.generation_root or self.generation_root(source_generation_id),
                generation_id=source_generation_id,
            )
            storage = self.write_segmented_dataset(
                candles,
                symbol=symbol,
                interval=interval,
                import_generation_id=target_generation_id,
            )
            health_payload = health.to_dict()
            health_payload.pop("source_file", None)
            health_payload["storage"] = storage.to_dict()
            symbols_payload.setdefault(symbol, {"intervals": {}})["intervals"][interval] = health_payload
            imported_candles[(symbol, interval)] = candles

        manifest = {
            key: copy.deepcopy(value)
            for key, value in source_result.payload.items()
            if key not in {"schema_version", "storage_layout", "run_id", "symbols", "imported_from_run_id"}
        }
        manifest.update(
            {
                "schema_version": 4,
                "storage_layout": TrustedStorageLayout.SEGMENTED_CSV_V1.value,
                "run_id": target_generation_id,
                "symbols": symbols_payload,
                "imported_from_run_id": source_generation_id,
            }
        )
        self.write_generation_manifest(target_generation_id, manifest)
        target_result = self.verify_generation(target_generation_id)
        for key, source_candles in imported_candles.items():
            imported = self.read_generation_dataset(
                target_result.snapshot,
                symbol=key[0],
                interval=key[1],
                generation_root=target_result.generation_root or self.generation_root(target_generation_id),
                generation_id=target_generation_id,
            )
            if imported != source_candles:
                raise ManifestSchemaError(f"flat import candle round trip mismatch: {key[0]}/{key[1]}")
            if candles_content_sha256(imported) != source_snapshot.datasets[key].content_sha256:
                raise ManifestSchemaError(f"flat import logical SHA-256 mismatch: {key[0]}/{key[1]}")
        if publish:
            self.commit_generation_publication(
                target_generation_id,
                manifest,
                {
                    "run_id": target_generation_id,
                    "attempt_status": source_snapshot.attempt_status.value,
                    "snapshot_usability": source_snapshot.snapshot_usability.value,
                    "started_at_ms": source_snapshot.started_at_ms,
                    "completed_at_ms": source_snapshot.completed_at_ms,
                    "symbol_count": len({symbol for symbol, _ in source_snapshot.datasets}),
                    "invalid_count": sum(1 for health in source_snapshot.datasets.values() if not health.is_usable),
                    "provider_failures": [dict(value) for value in source_snapshot.provider_failures],
                    "warnings": list(source_snapshot.warnings),
                    "cycle_error": source_snapshot.cycle_error,
                    "imported_from_run_id": source_generation_id,
                },
            )
        return target_result

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


@contextmanager
def _exclusive_dataset_writer_lock(path: Path):
    path = Path(path)
    created = False
    try:
        handle = path.open("x+b")
        created = True
    except FileExistsError:
        handle = path.open("r+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"1")
            _flush_and_fsync(handle)
        if created:
            _fsync_directory(path.parent)
        handle.seek(0)
        if os.name == "posix":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        raise OSError(errno.ENOTSUP, f"trusted segment locking is unsupported on platform: {os.name}")
    finally:
        handle.close()


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


def _validate_generation_storage(snapshot: TrustedManifestSnapshot, generation_root: Path) -> None:
    for key, health in snapshot.datasets.items():
        symbol = validate_storage_segment(health.key.symbol, field="symbol")
        interval = validate_storage_segment(health.key.interval, field="interval")
        storage = snapshot.storage_by_dataset.get(key)
        if storage is None:
            raise ManifestSchemaError("generation manifest dataset storage is missing")
        if storage.layout is TrustedStorageLayout.FLAT_CSV_V1:
            expected = Path("okx") / symbol / f"{interval}.csv"
            if health.source_file.as_posix() != expected.as_posix():
                raise ManifestSchemaError("generation manifest source_file must equal okx/<symbol>/<interval>.csv")
            _resolve_generation_source_file(health.source_file, generation_root)
            continue
        if storage.layout is TrustedStorageLayout.SEGMENTED_CSV_V1:
            expected_root = Path("segments") / "okx" / symbol / interval
            if storage.source_root.as_posix() != expected_root.as_posix():
                raise ManifestSchemaError(
                    "schema-v4 dataset source_root must equal segments/okx/<symbol>/<interval>"
                )
            for index, reference in enumerate(storage.segments):
                if not _is_allowed_segment_source(
                    reference.source_file,
                    expected_root=expected_root,
                    segment_id=reference.segment_id,
                    generation_id=snapshot.run_id,
                    imported_from_run_id=snapshot.imported_from_run_id,
                    first_reference=index == 0,
                ):
                    raise ManifestSchemaError(
                        "schema-v4 segment source_file must match symbol/interval/segment_id"
                    )
            continue
        raise ManifestSchemaError(f"unsupported trusted storage layout: {storage.layout}")


def _partition_candles_by_utc_month(candles: list[Candle]) -> dict[str, list[Candle]]:
    partitions: dict[str, list[Candle]] = {}
    previous_timestamp: int | None = None
    previous_segment_id: str | None = None
    for candle in candles:
        if previous_timestamp is not None and candle.open_time_ms <= previous_timestamp:
            raise ManifestSchemaError("trusted dataset candles must be strictly ordered")
        segment_id = utc_month_segment_id(candle.open_time_ms)
        if previous_segment_id is not None and segment_id < previous_segment_id:
            raise ManifestSchemaError("trusted dataset UTC month segments must be ordered")
        partitions.setdefault(segment_id, []).append(candle)
        previous_timestamp = candle.open_time_ms
        previous_segment_id = segment_id
    return partitions


def _is_allowed_segment_source(
    source_file: Path,
    *,
    expected_root: Path,
    segment_id: str,
    generation_id: str,
    imported_from_run_id: str | None,
    first_reference: bool,
) -> bool:
    canonical = expected_root / f"{segment_id}.csv"
    if source_file.as_posix() == canonical.as_posix():
        return True
    imported = expected_root / f"{segment_id}.import-{generation_id}.csv"
    return (
        imported_from_run_id is not None
        and first_reference
        and source_file.as_posix() == imported.as_posix()
    )


def _validate_segment_candles(candles: list[Candle], *, segment_id: str) -> None:
    if not candles:
        raise ManifestSchemaError("trusted segment cannot be empty")
    previous_timestamp: int | None = None
    for candle in candles:
        if utc_month_segment_id(candle.open_time_ms) != segment_id:
            raise ManifestSchemaError("trusted segment candle belongs to a different UTC month")
        if previous_timestamp is not None and candle.open_time_ms <= previous_timestamp:
            raise ManifestSchemaError("trusted segment candles must be strictly ordered")
        previous_timestamp = candle.open_time_ms


def _validate_candidate_segment_continuity(
    candles: list[Candle],
    *,
    symbol: str,
    interval: str,
    segment_id: str,
) -> None:
    expected_interval_ms = interval_to_ms(interval)
    for previous, current in zip(candles, candles[1:]):
        if current.open_time_ms - previous.open_time_ms != expected_interval_ms:
            raise SegmentCorrectionError(
                f"trusted segment candidate contains a timestamp gap: {symbol}/{interval}/{segment_id}"
            )


def _validate_dataset_candles(
    candles: list[Candle],
    health: DatasetHealth,
    *,
    symbol: str,
    interval: str,
) -> None:
    if health.integrity is not IntegrityState.VALID:
        return
    if health.availability is AvailabilityState.AVAILABLE and not candles:
        raise ManifestSchemaError(f"trusted dataset is empty: {symbol}/{interval}")
    if len(candles) != health.rows:
        raise ManifestSchemaError(f"trusted dataset row count mismatch: {symbol}/{interval}")
    if not candles:
        return
    if candles[0].open_time_ms != health.first_timestamp_ms:
        raise ManifestSchemaError(f"trusted dataset first timestamp mismatch: {symbol}/{interval}")
    if candles[-1].open_time_ms != health.last_timestamp_ms:
        raise ManifestSchemaError(f"trusted dataset last timestamp mismatch: {symbol}/{interval}")
    if not health.content_sha256:
        raise ManifestSchemaError(f"trusted dataset has no content SHA-256: {symbol}/{interval}")
    if candles_content_sha256(candles) != health.content_sha256:
        raise ManifestSchemaError(f"trusted dataset content SHA-256 mismatch: {symbol}/{interval}")


def _candle_from_strict_csv_row(row: dict[str | None, str | list[str] | None]) -> Candle:
    if None in row or any(row.get(field) is None for field in CSV_FIELDS):
        raise ManifestSchemaError("trusted CSV row does not match CSV_FIELDS")
    return Candle.from_csv_row({field: str(row[field]) for field in CSV_FIELDS})


def _canonical_candles_equal(left: list[Candle], right: list[Candle]) -> bool:
    return len(left) == len(right) and all(
        left_candle.to_csv_row() == right_candle.to_csv_row()
        for left_candle, right_candle in zip(left, right)
    )


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
