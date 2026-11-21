from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import uuid
from bisect import bisect_left
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mu_strategy.file_locks import locked_file
from mu_strategy.fs_durability import fsync_directory as _fsync_directory
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
from mu_strategy.market_data.utils import (
    CSV_FIELDS,
    day_iso_prefix,
    format_open_time_iso,
    interval_to_ms,
)
from mu_strategy.models import Candle

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UTC_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_CSV_HEADER_BYTES = ",".join(CSV_FIELDS).encode("utf-8")
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
        if not _UTC_MONTH_RE.fullmatch(segment_id):
            raise ValueError("segment_id must be UTC YYYY-MM")
        return self.segment_source_root(symbol, interval) / f"{segment_id}.csv"

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
        candles, _ = self._read_csv_slice_with_hash(path, start_row=0, rows=None, require_eof=True)
        return candles

    def write_csv(self, candles: list[Candle], path: Path, *, required_prefix: bytes | None = None) -> None:
        path = Path(path)
        self._ensure_data_directory(path.parent)
        tmp_path = _tmp_path(path)
        try:
            with tmp_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(CSV_FIELDS)
                writer.writerows(
                    (
                        candle.open_time_ms,
                        format_open_time_iso(candle.open_time_ms),
                        f"{candle.open:.8f}",
                        f"{candle.high:.8f}",
                        f"{candle.low:.8f}",
                        f"{candle.close:.8f}",
                        f"{candle.volume:.8f}",
                    )
                    for candle in candles
                )
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
        require_complete_start_month: bool = False,
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
        with locked_file(segment_directory / ".write.lock", wait=True):
            return self._write_segmented_dataset_locked(
                logical_partitions,
                physical_partitions,
                symbol=symbol,
                interval=interval,
                source_root=source_root,
                require_complete_start_month=require_complete_start_month,
            )

    def _write_segmented_dataset_locked(
        self,
        logical_partitions: dict[str, list[Candle]],
        physical_partitions: dict[str, list[Candle]],
        *,
        symbol: str,
        interval: str,
        source_root: Path,
        require_complete_start_month: bool,
    ) -> DatasetStorage:
        physical_rows_by_segment: dict[str, list[Candle]] = {}
        first_logical_segment_id = next(iter(logical_partitions))
        retained_predecessor_ids: set[str] = set()
        prior_physical_segment_ids = [
            segment_id
            for segment_id in physical_partitions
            if segment_id < first_logical_segment_id
        ]
        if prior_physical_segment_ids and not self.segment_path(symbol, interval, first_logical_segment_id).exists():
            existing_predecessor_ids = [
                segment_id
                for segment_id in prior_physical_segment_ids
                if self.segment_path(symbol, interval, segment_id).exists()
            ]
            if existing_predecessor_ids:
                first_retained_id = max(existing_predecessor_ids)
                retained_predecessor_ids = {
                    segment_id
                    for segment_id in prior_physical_segment_ids
                    if segment_id >= first_retained_id
                }
        physical_partitions = {
            segment_id: rows
            for segment_id, rows in physical_partitions.items()
            if segment_id >= first_logical_segment_id or segment_id in retained_predecessor_ids
        }
        physical_items = list(physical_partitions.items())
        _validate_candidate_partitions_continuity(
            physical_items,
            symbol=symbol,
            interval=interval,
            validate_cross_partition=any(
                not self.segment_path(symbol, interval, segment_id).exists()
                for segment_id, _ in physical_items
            ),
        )
        if physical_items:
            last_segment_id, last_candidate_rows = physical_items[-1]
            if not self.segment_path(symbol, interval, last_segment_id).exists():
                self._validate_next_canonical_segment_adjacency(
                    symbol,
                    interval,
                    last_segment_id,
                    last_candidate_rows[-1],
                )
        for segment_id, candidate_rows in physical_partitions.items():
            path = self.segment_path(symbol, interval, segment_id)
            if not path.exists():
                if (
                    require_complete_start_month
                    and segment_id == first_logical_segment_id
                    and utc_month_segment_id(candidate_rows[0].open_time_ms - 1) == segment_id
                ):
                    raise SegmentCorrectionError(
                        f"canonical trusted segment requires complete month lookbehind: "
                        f"{symbol}/{interval}/{segment_id}"
                    )
                self._validate_previous_canonical_segment_adjacency(
                    symbol,
                    interval,
                    segment_id,
                    candidate_rows[0],
                )
                self.write_csv(candidate_rows, path)
                physical_rows_by_segment[segment_id] = list(candidate_rows)
                continue

            existing_bytes = path.read_bytes()
            existing = self.read_csv(path)
            _validate_segment_candles(existing, segment_id=segment_id)
            _validate_candidate_overlap(
                existing,
                candidate_rows,
                symbol=symbol,
                interval=interval,
                segment_id=segment_id,
                target="trusted segment",
            )
            suffix_start = bisect_left(
                candidate_rows,
                existing[-1].open_time_ms + 1,
                key=_candle_open_time_ms,
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
            start_row = bisect_left(
                physical_rows,
                segment_candles[0].open_time_ms,
                key=_candle_open_time_ms,
            )
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
                    source_file=self.segment_source_file(symbol, interval, segment_id),
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
            if _UTC_MONTH_RE.fullmatch(candidate.stem)
        )

    def _validate_previous_canonical_segment_adjacency(
        self,
        symbol: str,
        interval: str,
        segment_id: str,
        first_candidate: Candle,
    ) -> None:
        segment_dir = self.data_dir / self.segment_source_root(symbol, interval)
        previous_ids = [
            candidate.stem
            for candidate in segment_dir.glob("*.csv")
            if _UTC_MONTH_RE.fullmatch(candidate.stem) and candidate.stem < segment_id
        ]
        if not previous_ids:
            return
        previous_id = max(previous_ids)
        previous_path = self.segment_path(symbol, interval, previous_id)
        previous_rows = self.read_csv(previous_path)
        _validate_segment_candles(previous_rows, segment_id=previous_id)
        _validate_candidate_segment_continuity(
            previous_rows,
            symbol=symbol,
            interval=interval,
            segment_id=previous_id,
        )
        expected_first_ms = previous_rows[-1].open_time_ms + interval_to_ms(interval)
        if first_candidate.open_time_ms != expected_first_ms:
            raise SegmentCorrectionError(
                f"new trusted segment is not adjacent to its canonical predecessor: "
                f"{symbol}/{interval}/{previous_id}->{segment_id}"
            )

    def _validate_next_canonical_segment_adjacency(
        self,
        symbol: str,
        interval: str,
        segment_id: str,
        last_candidate: Candle,
    ) -> None:
        segment_dir = self.data_dir / self.segment_source_root(symbol, interval)
        next_ids = [
            candidate.stem
            for candidate in segment_dir.glob("*.csv")
            if _UTC_MONTH_RE.fullmatch(candidate.stem) and candidate.stem > segment_id
        ]
        if not next_ids:
            return
        next_id = min(next_ids)
        next_path = self.segment_path(symbol, interval, next_id)
        next_rows = self.read_csv(next_path)
        _validate_segment_candles(next_rows, segment_id=next_id)
        _validate_candidate_segment_continuity(
            next_rows,
            symbol=symbol,
            interval=interval,
            segment_id=next_id,
        )
        expected_first_ms = last_candidate.open_time_ms + interval_to_ms(interval)
        if next_rows[0].open_time_ms != expected_first_ms:
            raise SegmentCorrectionError(
                f"new trusted segment is not adjacent to its canonical successor: "
                f"{symbol}/{interval}/{segment_id}->{next_id}"
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
        candles, _ = self.read_generation_dataset_with_hash(
            snapshot,
            symbol=symbol,
            interval=interval,
            generation_root=generation_root,
            generation_id=generation_id,
            verify_logical=verify_logical,
        )
        return candles

    def read_generation_dataset_with_hash(
        self,
        snapshot: TrustedManifestSnapshot,
        *,
        symbol: str,
        interval: str,
        generation_root: Path,
        generation_id: str,
        verify_logical: bool = True,
        compute_hash: bool = False,
    ) -> tuple[list[Candle], str | None]:
        key = (symbol, interval)
        health = snapshot.datasets.get(key)
        storage = snapshot.storage_by_dataset.get(key)
        if health is None or storage is None:
            raise ManifestSchemaError(f"trusted manifest is missing dataset storage: {symbol}/{interval}")
        expected_hash = health.content_sha256 if (verify_logical or compute_hash) else None
        if storage.layout is TrustedStorageLayout.FLAT_CSV_V1:
            path = self.resolve_source_file(
                health.source_file,
                generation_root=generation_root,
                generation_id=generation_id,
            )
            if "read_csv" in self.__dict__ or type(self).read_csv is not TrustedDataStore.read_csv:
                candles = self.read_csv(path)
                content_hash = candles_content_sha256(candles) if (verify_logical or compute_hash) else None
            else:
                candles, content_hash = self._read_csv_slice_with_hash(
                    path,
                    start_row=0,
                    rows=None,
                    require_eof=True,
                    expected_sha256=expected_hash,
                    need_hash=verify_logical or compute_hash,
                )
        elif storage.layout is TrustedStorageLayout.SEGMENTED_CSV_V1:
            candles, content_hash = self._read_segmented_dataset(
                storage,
                symbol=symbol,
                interval=interval,
                expected_logical_sha256=expected_hash,
                need_logical_hash=verify_logical or compute_hash,
            )
        else:
            raise ManifestSchemaError(f"unsupported trusted storage layout: {storage.layout}")
        if verify_logical:
            _validate_dataset_candles(
                candles,
                health,
                symbol=symbol,
                interval=interval,
                precomputed_sha256=content_hash,
            )
        return candles, content_hash

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

    def _read_csv_slice_with_hash(
        self,
        path: Path,
        *,
        start_row: int,
        rows: int | None,
        require_eof: bool,
        expected_sha256: str | None = None,
        need_hash: bool = False,
        logical_digest: Any | None = None,
    ) -> tuple[list[Candle], str | None]:
        raw = Path(path).read_bytes()
        if b'"' not in raw and b"\x00" not in raw:
            newline_index = raw.find(b"\n")
            if newline_index == -1:
                header_bytes = raw.rstrip(b"\r")
                body_bytes = b""
            else:
                header_bytes = raw[:newline_index].rstrip(b"\r")
                body_bytes = raw[newline_index + 1 :]
            if header_bytes != _CSV_HEADER_BYTES:
                raise ManifestSchemaError("trusted CSV header must exactly match CSV_FIELDS")
            if not body_bytes:
                if start_row > 0:
                    raise ManifestSchemaError(
                        "trusted segment has fewer prefix rows than its manifest start_row"
                    )
                if rows is not None and rows > 0:
                    raise ManifestSchemaError("trusted segment has fewer rows than its manifest reference")
                return [], (hashlib.sha256().hexdigest() if need_hash else None)

            body_text = body_bytes.decode("utf-8")
            if "\r\n" in body_text:
                body_text = body_text.replace("\r\n", "\n")
            if "\r" not in body_text:
                if body_text.endswith("\n"):
                    lines = body_text[:-1].split("\n")
                else:
                    lines = body_text.split("\n")
                total_lines = len(lines)
                if total_lines < start_row:
                    raise ManifestSchemaError(
                        "trusted segment has fewer prefix rows than its manifest start_row"
                    )
                _int = int
                _float = float
                _candle_cls = Candle
                for prefix_index in range(start_row):
                    _parse_strict_csv_parts(lines[prefix_index].split(","), _int, _float, _candle_cls)

                if rows is None:
                    end_row = total_lines
                else:
                    end_row = start_row + rows
                    if end_row > total_lines:
                        raise ManifestSchemaError("trusted segment has fewer rows than its manifest reference")
                    if require_eof and end_row < total_lines:
                        raise ManifestSchemaError("closed trusted segment has unreferenced extra rows")

                candles: list[Candle] = []
                append = candles.append
                for line_index in range(start_row, end_row):
                    parts = lines[line_index].split(",")
                    if len(parts) != 7:
                        raise ManifestSchemaError("trusted CSV row does not match CSV_FIELDS")
                    append(
                        _candle_cls(
                            _int(parts[0]),
                            _float(parts[2]),
                            _float(parts[3]),
                            _float(parts[4]),
                            _float(parts[5]),
                            _float(parts[6]),
                        )
                    )

                if not need_hash and logical_digest is None:
                    return candles, None

                # Compute canonical SHA-256 directly from unquoted raw CSV bytes in C.
                if start_row == 0 and end_row == total_lines:
                    norm_bytes = body_bytes.replace(b"\r\n", b"\n")
                    if norm_bytes and not norm_bytes.endswith(b"\n"):
                        norm_bytes += b"\n"
                else:
                    slice_text = "\n".join(lines[start_row:end_row])
                    norm_bytes = (slice_text + "\n").encode("utf-8") if slice_text else b""
                canonical_bytes = norm_bytes.replace(b",", b"\x00").replace(b"\n", b"\x00\n")
                fast_hash = hashlib.sha256(canonical_bytes).hexdigest()
                if expected_sha256 is not None and fast_hash == expected_sha256:
                    if logical_digest is not None:
                        logical_digest.update(canonical_bytes)
                    return candles, fast_hash
                # Fallback to canonical candle serialization if raw CSV formatting was non-canonical or mismatched.
                actual_hash = candles_content_sha256(candles, digest_sink=logical_digest)
                return candles, actual_hash

        # Fallback: full RFC CSV parser via csv.reader (e.g., quoted fields or bare CR).
        candles = self._read_csv_slice_reader(path, start_row=start_row, rows=rows, require_eof=require_eof)
        if not need_hash and logical_digest is None:
            return candles, None
        actual_hash = candles_content_sha256(candles, digest_sink=logical_digest)
        return candles, actual_hash

    def _read_csv_slice_reader(
        self,
        path: Path,
        *,
        start_row: int,
        rows: int | None,
        require_eof: bool,
    ) -> list[Candle]:
        candles: list[Candle] = []
        append = candles.append
        _int = int
        _float = float
        _candle_cls = Candle
        with Path(path).open("r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header != CSV_FIELDS:
                raise ManifestSchemaError("trusted CSV header must exactly match CSV_FIELDS")
            for _ in range(start_row):
                try:
                    row = next(reader)
                except StopIteration as exc:
                    raise ManifestSchemaError(
                        "trusted segment has fewer prefix rows than its manifest start_row"
                    ) from exc
                _parse_strict_csv_parts(row, _int, _float, _candle_cls)
            while rows is None or len(candles) < rows:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                append(_parse_strict_csv_parts(row, _int, _float, _candle_cls))
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
        storage: DatasetStorage,
        *,
        symbol: str,
        interval: str,
        expected_logical_sha256: str | None = None,
        need_logical_hash: bool = False,
    ) -> tuple[list[Candle], str | None]:
        expected_root = self.segment_source_root(symbol, interval)
        if storage.source_root.as_posix() != expected_root.as_posix():
            raise ManifestSchemaError("schema-v4 dataset source_root does not match dataset key")
        candles: list[Candle] = []
        logical_digest = hashlib.sha256() if need_logical_hash else None
        for reference in storage.segments:
            if not _is_canonical_segment_source(
                reference.source_file,
                expected_root=expected_root,
                segment_id=reference.segment_id,
            ):
                raise ManifestSchemaError("schema-v4 segment source_file does not match dataset key")
            path = self._segment_path_from_source(reference.source_file)
            segment_candles, segment_hash = self._read_csv_slice_with_hash(
                path,
                start_row=reference.start_row,
                rows=reference.rows,
                require_eof=reference.closed,
                expected_sha256=reference.content_sha256,
                need_hash=True,
                logical_digest=logical_digest,
            )
            _validate_segment_candles(segment_candles, segment_id=reference.segment_id)
            if segment_candles[0].open_time_ms != reference.first_timestamp_ms:
                raise ManifestSchemaError("trusted segment first timestamp does not match manifest")
            if segment_candles[-1].open_time_ms != reference.last_timestamp_ms:
                raise ManifestSchemaError("trusted segment last timestamp does not match manifest")
            if segment_hash != reference.content_sha256:
                raise ManifestSchemaError("trusted segment content SHA-256 mismatch")
            candles.extend(segment_candles)
        logical_hash = logical_digest.hexdigest() if logical_digest is not None else None
        if expected_logical_sha256 is not None and logical_hash is not None and logical_hash != expected_logical_sha256:
            # Guard against rare non-canonical formatting edge cases across segments.
            logical_hash = candles_content_sha256(candles)
        return candles, logical_hash

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
            return ManifestReadResult(
                None,
                pointer if isinstance(pointer, dict) else None,
                HealthReason.MALFORMED_MANIFEST,
                type(exc).__name__,
                str(exc),
            )
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
            return ManifestReadResult(
                None,
                None,
                HealthReason.MALFORMED_MANIFEST,
                type(exc).__name__,
                str(exc),
                generation_root=generation_root,
                generation_id=generation_id,
                manifest_path=path,
            )
        if not isinstance(payload, dict):
            return ManifestReadResult(
                None,
                None,
                HealthReason.MALFORMED_MANIFEST,
                "TypeError",
                "manifest root must be object",
                generation_root=generation_root,
                generation_id=generation_id,
                manifest_path=path,
            )
        try:
            snapshot = trusted_manifest_snapshot_from_dict(payload)
            if generation_id is not None:
                if snapshot.run_id != generation_id:
                    raise ManifestSchemaError("generation manifest run_id must match current generation_id")
                _validate_generation_storage(snapshot, generation_root)
        except Exception as exc:
            return ManifestReadResult(
                None,
                payload,
                HealthReason.MALFORMED_MANIFEST,
                type(exc).__name__,
                str(exc),
                generation_root=generation_root,
                generation_id=generation_id,
                manifest_path=path,
            )
        return ManifestReadResult(
            snapshot,
            payload,
            generation_root=generation_root,
            generation_id=generation_id,
            manifest_path=path,
        )

    def write_generation_manifest(self, generation_id: str, manifest: dict[str, Any]) -> Path:
        generation_id = validate_storage_segment(generation_id, field="generation_id")
        if manifest.get("run_id") != generation_id:
            raise ManifestSchemaError("generation manifest run_id must match generation_id")
        path = self.generation_manifest_path(generation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            path,
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
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
        publication_warnings: list[str] = []
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

    def append_run_log(self, payload: dict[str, Any]) -> Path:
        path = self.run_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            _flush_and_fsync(handle)
        return path


def candles_content_sha256(candles: list[Candle], *, digest_sink: Any | None = None) -> str:
    digest = hashlib.sha256()
    if not candles:
        return digest.hexdigest()
    buf: list[str] = []
    append = buf.append
    get_prefix = day_iso_prefix
    for candle in candles:
        ms = candle.open_time_ms
        seconds, rem_ms = divmod(ms, 1000)
        day, sec_in_day = divmod(seconds, 86_400)
        prefix = get_prefix(day)
        hh, rem = divmod(sec_in_day, 3600)
        mm, ss = divmod(rem, 60)
        if rem_ms:
            append(
                f"{ms}\0{prefix}{hh:02d}:{mm:02d}:{ss:02d}.{rem_ms:03d}000+00:00\0"
                f"{candle.open:.8f}\0{candle.high:.8f}\0{candle.low:.8f}\0{candle.close:.8f}\0{candle.volume:.8f}\0\n"
            )
        else:
            append(
                f"{ms}\0{prefix}{hh:02d}:{mm:02d}:{ss:02d}+00:00\0"
                f"{candle.open:.8f}\0{candle.high:.8f}\0{candle.low:.8f}\0{candle.close:.8f}\0{candle.volume:.8f}\0\n"
            )
        if len(buf) >= 2048:
            chunk = "".join(buf).encode("utf-8")
            digest.update(chunk)
            if digest_sink is not None:
                digest_sink.update(chunk)
            buf.clear()
    if buf:
        chunk = "".join(buf).encode("utf-8")
        digest.update(chunk)
        if digest_sink is not None:
            digest_sink.update(chunk)
    return digest.hexdigest()


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
            for reference in storage.segments:
                if not _is_canonical_segment_source(
                    reference.source_file,
                    expected_root=expected_root,
                    segment_id=reference.segment_id,
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


def _is_canonical_segment_source(
    source_file: Path,
    *,
    expected_root: Path,
    segment_id: str,
) -> bool:
    return source_file.as_posix() == (expected_root / f"{segment_id}.csv").as_posix()


def _validate_segment_candles(candles: list[Candle], *, segment_id: str) -> None:
    if not candles:
        raise ManifestSchemaError("trusted segment cannot be empty")
    previous_timestamp: int | None = None
    for candle in candles:
        ts = candle.open_time_ms
        if utc_month_segment_id(ts) != segment_id:
            raise ManifestSchemaError("trusted segment candle belongs to a different UTC month")
        if previous_timestamp is not None and ts <= previous_timestamp:
            raise ManifestSchemaError("trusted segment candles must be strictly ordered")
        previous_timestamp = ts


def _validate_candidate_segment_continuity(
    candles: list[Candle],
    *,
    symbol: str,
    interval: str,
    segment_id: str,
) -> None:
    expected_interval_ms = interval_to_ms(interval)
    for index in range(1, len(candles)):
        if candles[index].open_time_ms - candles[index - 1].open_time_ms != expected_interval_ms:
            raise SegmentCorrectionError(
                f"trusted segment candidate contains a timestamp gap: {symbol}/{interval}/{segment_id}"
            )


def _validate_candidate_overlap(
    existing: list[Candle],
    candidate_rows: list[Candle],
    *,
    symbol: str,
    interval: str,
    segment_id: str,
    target: str,
) -> None:
    existing_by_timestamp = {candle.open_time_ms: candle for candle in existing}
    first_existing = existing[0].open_time_ms
    last_existing = existing[-1].open_time_ms
    for candidate in candidate_rows:
        ts = candidate.open_time_ms
        if ts < first_existing:
            continue
        if ts > last_existing:
            break
        stored = existing_by_timestamp.get(ts)
        if stored is None or not _canonical_candle_equal(stored, candidate):
            raise SegmentCorrectionError(
                f"historical correction would rewrite {target}: {symbol}/{interval}/{segment_id}"
            )


def _validate_candidate_partitions_continuity(
    partitions: list[tuple[str, list[Candle]]],
    *,
    symbol: str,
    interval: str,
    validate_cross_partition: bool,
) -> None:
    expected_interval_ms = interval_to_ms(interval)
    previous_segment_id: str | None = None
    previous_last: Candle | None = None
    for segment_id, candles in partitions:
        _validate_candidate_segment_continuity(
            candles,
            symbol=symbol,
            interval=interval,
            segment_id=segment_id,
        )
        if (
            validate_cross_partition
            and previous_last is not None
            and candles[0].open_time_ms - previous_last.open_time_ms != expected_interval_ms
        ):
            raise SegmentCorrectionError(
                f"trusted segment candidates contain a timestamp gap across UTC months: "
                f"{symbol}/{interval}/{previous_segment_id}->{segment_id}"
            )
        previous_segment_id = segment_id
        previous_last = candles[-1]


def _validate_dataset_candles(
    candles: list[Candle],
    health: DatasetHealth,
    *,
    symbol: str,
    interval: str,
    precomputed_sha256: str | None = None,
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
    actual_sha256 = precomputed_sha256 if precomputed_sha256 is not None else candles_content_sha256(candles)
    if actual_sha256 != health.content_sha256:
        raise ManifestSchemaError(f"trusted dataset content SHA-256 mismatch: {symbol}/{interval}")


def _parse_strict_csv_parts(row: list[str], _int, _float, _candle_cls) -> Candle:
    if len(row) != 7:
        raise ManifestSchemaError("trusted CSV row does not match CSV_FIELDS")
    return _candle_cls(
        _int(row[0]),
        _float(row[2]),
        _float(row[3]),
        _float(row[4]),
        _float(row[5]),
        _float(row[6]),
    )


def _candle_open_time_ms(candle: Candle) -> int:
    return candle.open_time_ms


def _canonical_candle_equal(left: Candle, right: Candle) -> bool:
    if left == right:
        return True
    return (
        left.open_time_ms == right.open_time_ms
        and f"{left.open:.8f}" == f"{right.open:.8f}"
        and f"{left.high:.8f}" == f"{right.high:.8f}"
        and f"{left.low:.8f}" == f"{right.low:.8f}"
        and f"{left.close:.8f}" == f"{right.close:.8f}"
        and f"{left.volume:.8f}" == f"{right.volume:.8f}"
    )


def _canonical_candles_equal(left: list[Candle], right: list[Candle]) -> bool:
    return len(left) == len(right) and all(
        _canonical_candle_equal(left_candle, right_candle)
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
