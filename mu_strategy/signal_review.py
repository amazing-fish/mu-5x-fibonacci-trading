"""Read-only application view of signal, service and notification evidence."""
from __future__ import annotations

import copy
import hashlib
import logging
import re
import sqlite3
import threading
from collections import Counter, OrderedDict, deque
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from mu_strategy.market_data.trusted_data.contracts import SystemClock
from mu_strategy.manual_positions import ManualPositionLedger
from mu_strategy.notifications.events import NotificationError
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.observations import (
    JsonlObservationRepository,
    ObservationCorruptionError,
    _INDEX_ENTRY_BYTES,
    _MAX_RECORD_BYTES,
    _ZERO_CHAIN_SHA256,
    _symbols_hash16,
    _unpack_index_entry,
    canonical_payload_sha256,
)
from mu_strategy.core.trading_calendar import trading_window_schedule
from mu_strategy.strategies.registry import baseline_strategy_group, selected_strategy_groups
from mu_strategy.service_health import HealthStateError, HealthStore, health_view
from mu_strategy.signal_feedback import SignalFeedbackStore


logger = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
DISPLAY_LIMIT = 2000
SCAN_LIMIT = 100_000
_CACHE_MAX_ENTRIES = 16
_OBSERVATION_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_OBSERVATION_CACHE_LOCK = threading.Lock()


def review_window(*, now_ms: int, days: int = 7, from_date: str | None = None,
                  to_date: str | None = None) -> dict:
    if type(days) is not int or not 1 <= days <= 366:
        raise ValueError("days must be between 1 and 366")
    last = date.fromisoformat(to_date) if to_date else datetime.fromtimestamp(now_ms / 1000, BEIJING).date()
    first = date.fromisoformat(from_date) if from_date else last - timedelta(days=days - 1)
    if first < date(1970, 1, 2) or first > last or (last - first).days >= 366:
        raise ValueError("date window must contain 1 to 366 calendar days")
    return {
        "from_date": first.isoformat(), "to_date": last.isoformat(),
        "start_ms": int(datetime.combine(first, time.min, BEIJING).timestamp() * 1000),
        "end_ms": int(datetime.combine(last + timedelta(days=1), time.min, BEIJING).timestamp() * 1000),
    }


def _source(state: str, at_ms: int, message: str, **extra) -> dict:
    return {"state": state, "read_at_ms": at_ms, "message": message, **extra}


class _SnapshotChanged(Exception):
    pass


def _hash_file_prefix(path: Path, *, limit: int) -> bytes:
    """Compute BLAKE2b digest of the exact first ``limit`` bytes without holding locks."""
    hasher = hashlib.blake2b()
    remaining = limit
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(65_536, remaining))
            if not chunk:
                raise _SnapshotChanged()
            hasher.update(chunk)
            remaining -= len(chunk)
    return hasher.digest()


def _verify_snapshot_unlocked(
    repository: JsonlObservationRepository,
    *,
    log_size: int,
    commit,
    index_size: int | None,
) -> tuple[bytes, bytes | None, str]:
    """Compute bounded content digests and verify commit-chain binding outside read_fence().

    Returns ``(log_digest, index_digest, index_status)`` where ``index_status`` is one of:
    - ``"none"``: no commit+index pair for this snapshot
    - ``"verified"``: index offsets, prefix_max monotonicity, and SHA-256 chain all match commit
    - ``"structural_corrupt"``: index sidecar size, offsets, lengths, or prefix_max are corrupted
    - ``"chain_mismatch"``: index structure is intact but log/index bytes disagree with commit.chain_sha256
    """
    if commit is None or commit.seq == 0 or index_size is None:
        return _hash_file_prefix(repository.path, limit=log_size), None, "none"

    if index_size != commit.index_offset or commit.index_offset != commit.seq * _INDEX_ENTRY_BYTES:
        return (
            _hash_file_prefix(repository.path, limit=log_size),
            _hash_file_prefix(repository.index_path, limit=index_size),
            "structural_corrupt",
        )

    log_hasher = hashlib.blake2b()
    index_hasher = hashlib.blake2b()
    chain = bytes.fromhex(_ZERO_CHAIN_SHA256)
    expected_offset = 0
    expected_prefix_max: int | None = None
    structural_corrupt = False
    chunk_entries = 1024

    with repository.index_path.open("rb") as index_handle, repository.path.open("rb") as log_handle:
        remaining = commit.seq
        while remaining > 0:
            take = min(remaining, chunk_entries)
            raw_index = index_handle.read(take * _INDEX_ENTRY_BYTES)
            if len(raw_index) != take * _INDEX_ENTRY_BYTES:
                raise _SnapshotChanged()
            index_hasher.update(raw_index)
            if not structural_corrupt:
                mv = memoryview(raw_index)
                for idx in range(take):
                    entry_bytes = mv[idx * _INDEX_ENTRY_BYTES : (idx + 1) * _INDEX_ENTRY_BYTES].tobytes()
                    entry = _unpack_index_entry(entry_bytes)
                    if (
                        entry.offset != expected_offset
                        or entry.length <= 0
                        or entry.length > _MAX_RECORD_BYTES
                        or expected_offset + entry.length > log_size
                    ):
                        structural_corrupt = True
                        break
                    expected_prefix_max = (
                        entry.created_at_ms
                        if expected_prefix_max is None
                        else max(expected_prefix_max, entry.created_at_ms)
                    )
                    if entry.prefix_max_created_at_ms != expected_prefix_max:
                        structural_corrupt = True
                        break
                    raw_record = log_handle.read(entry.length)
                    if len(raw_record) != entry.length:
                        raise _SnapshotChanged()
                    log_hasher.update(raw_record)
                    chain = hashlib.sha256(chain + raw_record + entry_bytes).digest()
                    expected_offset += entry.length
            remaining -= take

        if structural_corrupt or expected_offset != log_size:
            leftover = log_size - expected_offset
            while leftover > 0:
                chunk = log_handle.read(min(65_536, leftover))
                if not chunk:
                    raise _SnapshotChanged()
                log_hasher.update(chunk)
                leftover -= len(chunk)
            return log_hasher.digest(), index_hasher.digest(), "structural_corrupt"

    if chain.hex() != commit.chain_sha256:
        return log_hasher.digest(), index_hasher.digest(), "chain_mismatch"
    return log_hasher.digest(), index_hasher.digest(), "verified"


def _scan_indexed_evidence(
    repository: JsonlObservationRepository,
    *,
    total_count: int,
    commit_offset: int,
    evidence_key: tuple[str, str],
) -> tuple[object | None, int]:
    """Validate index integrity and cycle-ID uniqueness across all entries before resolving evidence_key."""
    target_cycle_id, target_obs_id = evidence_key
    target_id_hash16 = hashlib.sha256(target_cycle_id.encode("utf-8")).digest()[:16]
    id_to_first: dict[bytes, tuple[bytes, int, int]] = {}
    matching_spans: list[tuple[int, int]] = []
    chunk_entries = 1024
    expected_offset = 0
    expected_prefix_max: int | None = None
    with repository.index_path.open("rb") as index_handle, repository.path.open("rb") as log_handle:
        remaining = total_count
        while remaining > 0:
            take = min(remaining, chunk_entries)
            raw = index_handle.read(take * _INDEX_ENTRY_BYTES)
            if len(raw) != take * _INDEX_ENTRY_BYTES:
                raise ObservationCorruptionError("truncated observation index")
            mv = memoryview(raw)
            for idx in range(take):
                entry = _unpack_index_entry(mv[idx * _INDEX_ENTRY_BYTES : (idx + 1) * _INDEX_ENTRY_BYTES].tobytes())
                if entry.offset != expected_offset or entry.length <= 0:
                    raise ObservationCorruptionError("observation index offset discontinuity")
                expected_offset += entry.length
                expected_prefix_max = (
                    entry.created_at_ms
                    if expected_prefix_max is None
                    else max(expected_prefix_max, entry.created_at_ms)
                )
                if entry.prefix_max_created_at_ms != expected_prefix_max:
                    raise ObservationCorruptionError("observation index prefix_max corrupted")
                first = id_to_first.get(entry.cycle_id_hash16)
                if first is None:
                    id_to_first[entry.cycle_id_hash16] = (entry.content_hash16, entry.offset, entry.length)
                elif first[0] != entry.content_hash16:
                    c1, d1 = repository.read_cycle_at(log_handle, offset=first[1], length=first[2])
                    c2, d2 = repository.read_cycle_at(log_handle, offset=entry.offset, length=entry.length)
                    if c1.cycle_id == c2.cycle_id and d1 != d2:
                        raise ObservationCorruptionError("cycle identity conflicts")
                if entry.cycle_id_hash16 == target_id_hash16:
                    matching_spans.append((entry.offset, entry.length))
            remaining -= take
        if expected_offset != commit_offset:
            raise ObservationCorruptionError("observation index end disagrees with commit boundary")

        target_digest: str | None = None
        evidence_obs = None
        for offset, length in matching_spans:
            cycle, digest = repository.read_cycle_at(log_handle, offset=offset, length=length)
            if cycle.cycle_id != target_cycle_id:
                continue
            if target_digest is not None and target_digest != digest:
                raise ObservationCorruptionError("cycle identity conflicts")
            target_digest = digest
            if evidence_obs is None:
                for observation in cycle.observations:
                    if observation.observation_id == target_obs_id:
                        evidence_obs = observation
                        break
        return evidence_obs, len(matching_spans)


def _scan_indexed_window(
    repository: JsonlObservationRepository,
    *,
    total_count: int,
    commit_offset: int,
    window: dict,
    display_limit: int,
    scan_limit: int,
) -> tuple[bool, deque, dict, dict, Counter, int, int, int, int, int | None, int | None]:
    """Use verified monotonic prefix_max_created_at_ms coverage index when total_count exceeds scan_limit."""
    with repository.index_path.open("rb") as index_handle, repository.path.open("rb") as log_handle:
        first_in_window = repository.find_first_index_with_prefix_max_ge(index_handle, total_count, window["start_ms"])
        start_idx = min(first_in_window, total_count - 1) if total_count > 0 else 0
        window_covered = (total_count - start_idx) <= scan_limit
        if not window_covered:
            start_idx = total_count - scan_limit
        complete = window_covered

        suffix_count = total_count - start_idx
        index_handle.seek(start_idx * _INDEX_ENTRY_BYTES)
        suffix_raw = index_handle.read(suffix_count * _INDEX_ENTRY_BYTES)
        if len(suffix_raw) != suffix_count * _INDEX_ENTRY_BYTES:
            raise ObservationCorruptionError("truncated observation index")
        suffix_entries = [
            _unpack_index_entry(suffix_raw[i * _INDEX_ENTRY_BYTES : (i + 1) * _INDEX_ENTRY_BYTES])
            for i in range(suffix_count)
        ]

        suffix_id_hashes = {entry.cycle_id_hash16 for entry in suffix_entries}
        prefix_matches: dict[bytes, list[tuple[int, int, bytes]]] = {}
        prefix_first_by_id: dict[bytes, tuple[bytes, int, int]] = {}
        last_distinct_by_symbols: dict[bytes, tuple[int, object]] = {}
        expected_offset = 0
        expected_prefix_max: int | None = None
        if start_idx > 0:
            index_handle.seek(0)
            chunk_entries = 1024
            remaining = start_idx
            global_idx = 0
            while remaining > 0:
                take = min(remaining, chunk_entries)
                raw = index_handle.read(take * _INDEX_ENTRY_BYTES)
                if len(raw) != take * _INDEX_ENTRY_BYTES:
                    raise ObservationCorruptionError("truncated observation index")
                mv = memoryview(raw)
                for idx in range(take):
                    entry = _unpack_index_entry(mv[idx * _INDEX_ENTRY_BYTES : (idx + 1) * _INDEX_ENTRY_BYTES].tobytes())
                    if entry.offset != expected_offset or entry.length <= 0:
                        raise ObservationCorruptionError("observation index offset discontinuity")
                    expected_offset += entry.length
                    expected_prefix_max = (
                        entry.created_at_ms
                        if expected_prefix_max is None
                        else max(expected_prefix_max, entry.created_at_ms)
                    )
                    if entry.prefix_max_created_at_ms != expected_prefix_max:
                        raise ObservationCorruptionError("observation index prefix_max corrupted")
                    first = prefix_first_by_id.get(entry.cycle_id_hash16)
                    if first is None:
                        prefix_first_by_id[entry.cycle_id_hash16] = (entry.content_hash16, entry.offset, entry.length)
                        last_distinct_by_symbols[entry.symbols_hash16] = (global_idx, entry)
                    elif first[0] != entry.content_hash16:
                        c1, d1 = repository.read_cycle_at(log_handle, offset=first[1], length=first[2])
                        c2, d2 = repository.read_cycle_at(log_handle, offset=entry.offset, length=entry.length)
                        if c1.cycle_id == c2.cycle_id and d1 != d2:
                            raise ObservationCorruptionError("cycle identity conflicts")
                        if c1.cycle_id != c2.cycle_id:
                            last_distinct_by_symbols[entry.symbols_hash16] = (global_idx, entry)
                    if entry.cycle_id_hash16 in suffix_id_hashes:
                        prefix_matches.setdefault(entry.cycle_id_hash16, []).append(
                            (entry.offset, entry.length, entry.content_hash16)
                        )
                    global_idx += 1
                remaining -= take

        counts = Counter()
        rows: deque = deque(maxlen=display_limit)
        latest_obs: dict = {}
        latest_all_obs: dict = {}
        seen: dict[str, str] = {}
        suffix_distinct_symbols_hashes: set[bytes] = set()
        total_cycles = total_observations = duplicates = read_cycles = 0
        first_at = last_at = None

        for entry in suffix_entries:
            if entry.offset != expected_offset or entry.length <= 0:
                raise ObservationCorruptionError("observation index offset discontinuity")
            expected_offset += entry.length
            expected_prefix_max = (
                entry.created_at_ms
                if expected_prefix_max is None
                else max(expected_prefix_max, entry.created_at_ms)
            )
            if entry.prefix_max_created_at_ms != expected_prefix_max:
                raise ObservationCorruptionError("observation index prefix_max corrupted")

            read_cycles += 1
            cycle, digest = repository.read_cycle_at(log_handle, offset=entry.offset, length=entry.length)
            if (
                cycle.created_at_ms != entry.created_at_ms
                or hashlib.sha256(cycle.cycle_id.encode("utf-8")).digest()[:16] != entry.cycle_id_hash16
                or bytes.fromhex(digest)[:16] != entry.content_hash16
                or _symbols_hash16(cycle) != entry.symbols_hash16
            ):
                raise ObservationCorruptionError("observation index disagrees with log")

            if entry.cycle_id_hash16 in prefix_matches:
                replayed_from_prefix = False
                for p_offset, p_length, _ in prefix_matches[entry.cycle_id_hash16]:
                    p_cycle, p_digest = repository.read_cycle_at(log_handle, offset=p_offset, length=p_length)
                    if p_cycle.cycle_id != cycle.cycle_id:
                        continue
                    if p_digest != digest:
                        raise ObservationCorruptionError("cycle identity conflicts")
                    replayed_from_prefix = True
                    seen[cycle.cycle_id] = digest
                if replayed_from_prefix:
                    duplicates += 1
                    continue

            previous_digest = seen.get(cycle.cycle_id)
            if previous_digest is not None:
                if previous_digest != digest:
                    raise ObservationCorruptionError("cycle identity conflicts")
                duplicates += 1
                continue
            seen[cycle.cycle_id] = digest
            suffix_distinct_symbols_hashes.add(entry.symbols_hash16)

            for observation in cycle.observations:
                latest_all_obs[observation.symbol] = observation

            if cycle.created_at_ms < window["start_ms"] or cycle.created_at_ms >= window["end_ms"]:
                continue

            total_cycles += 1
            first_at = cycle.created_at_ms if first_at is None else min(first_at, cycle.created_at_ms)
            last_at = cycle.created_at_ms if last_at is None else max(last_at, cycle.created_at_ms)
            for observation in cycle.observations:
                counts[observation.outcome.value] += 1
                total_observations += 1
                latest_obs[observation.symbol] = observation
                rows.append(observation)

        if expected_offset != commit_offset:
            raise ObservationCorruptionError("observation index end disagrees with commit boundary")

        # Preserve the latest distinct observation for every symbol represented only in the prefix,
        # independent of window completeness, while letting suffix observations win without duplicates.
        if last_distinct_by_symbols:
            prefix_candidates = [
                (g_idx, p_entry)
                for sym_hash, (g_idx, p_entry) in last_distinct_by_symbols.items()
                if sym_hash not in suffix_distinct_symbols_hashes
            ]
            if prefix_candidates:
                prefix_candidates.sort(key=lambda item: item[0])
                prefix_latest_all: dict = {}
                for _, p_entry in prefix_candidates:
                    p_cycle, p_digest = repository.read_cycle_at(
                        log_handle, offset=p_entry.offset, length=p_entry.length
                    )
                    if (
                        p_cycle.created_at_ms != p_entry.created_at_ms
                        or hashlib.sha256(p_cycle.cycle_id.encode("utf-8")).digest()[:16] != p_entry.cycle_id_hash16
                        or bytes.fromhex(p_digest)[:16] != p_entry.content_hash16
                        or _symbols_hash16(p_cycle) != p_entry.symbols_hash16
                    ):
                        raise ObservationCorruptionError("observation index disagrees with log")
                    for observation in p_cycle.observations:
                        prefix_latest_all[observation.symbol] = observation
                for symbol, observation in prefix_latest_all.items():
                    latest_all_obs.setdefault(symbol, observation)

        return (
            complete,
            rows,
            latest_obs,
            latest_all_obs,
            counts,
            total_cycles,
            total_observations,
            duplicates,
            read_cycles,
            first_at,
            last_at,
        )


def _snapshot_meta_locked(repository: JsonlObservationRepository, path: Path):
    """Capture tiny repository metadata snapshot while holding read_fence()."""
    if repository.invalid_marker_path.exists():
        raise ObservationCorruptionError("observation repository has an unresolved failed-write marker")
    commit = repository.read_commit_state()
    try:
        log_stat = path.stat()
    except FileNotFoundError:
        return None, commit, None
    if commit is not None and log_stat.st_size != commit.offset:
        raise ObservationCorruptionError("observation log size disagrees with durable commit boundary")
    try:
        idx_stat = repository.index_path.stat()
    except FileNotFoundError:
        idx_stat = None
    return (
        (log_stat.st_size, log_stat.st_mtime_ns, getattr(log_stat, "st_ino", 0)),
        commit,
        (idx_stat.st_size, idx_stat.st_mtime_ns, getattr(idx_stat, "st_ino", 0)) if idx_stat is not None else None,
    )


def _read_observations(path: Path, window: dict, *, clock, display_limit: int, scan_limit: int, evidence_key=None) -> dict:
    repository = JsonlObservationRepository(path)
    resolved_path = str(path.resolve())
    try:
        unlocked_attempts = 0
        while True:
            locked = False
            counts = Counter()
            rows: deque = deque(maxlen=display_limit)
            latest_obs, latest_all_obs, evidence_obs = {}, {}, None
            seen: dict[str, str] = {}
            total_cycles = total_observations = duplicates = read_cycles = 0
            first_at = last_at = None
            complete = True

            # Step 1: Capture lightweight metadata under read_fence(), then release before hashing.
            try:
                with repository.read_fence() as locked:
                    log_meta, commit, index_meta = _snapshot_meta_locked(repository, path)
                    if log_meta is None:
                        if repository.invalid_marker_path.exists():
                            raise ObservationCorruptionError("observation repository has an unresolved failed-write marker")
                        if not locked and repository.lock_path.exists():
                            continue
                        if commit is not None and commit.offset > 0:
                            raise ObservationCorruptionError("observation log missing before durable commit boundary")
                        return _source("missing", clock.now_ms(), "尚无扫描日志。")
                    if not locked and repository.lock_path.exists():
                        continue
            except _SnapshotChanged:
                continue
            except ObservationCorruptionError:
                if not locked and repository.lock_path.exists():
                    continue
                raise

            initial_bytes = log_meta[0]
            index_size = index_meta[0] if index_meta is not None else None

            # Step 2: Verify full log/index digests and commit-chain binding OUTSIDE read_fence()
            # so cache hits and refresh verification never block writers on publication_fence().
            if unlocked_attempts < 8:
                try:
                    log_digest, index_digest, index_status = _verify_snapshot_unlocked(
                        repository,
                        log_size=initial_bytes,
                        commit=commit,
                        index_size=index_size,
                    )
                except (FileNotFoundError, _SnapshotChanged):
                    unlocked_attempts += 1
                    continue
            else:
                log_digest = index_digest = None
                index_status = "none"

            # Step 3: Re-acquire read_fence(), recheck snapshot, serve cache hit or run read.
            try:
                with repository.read_fence() as locked:
                    recheck_log, recheck_commit, recheck_index = _snapshot_meta_locked(repository, path)
                    if recheck_log is None:
                        if not locked and repository.lock_path.exists():
                            continue
                        if recheck_commit is not None and recheck_commit.offset > 0:
                            raise ObservationCorruptionError("observation log missing before durable commit boundary")
                        return _source("missing", clock.now_ms(), "尚无扫描日志。")
                    if unlocked_attempts < 8:
                        if (recheck_log, recheck_commit, recheck_index) != (log_meta, commit, index_meta):
                            unlocked_attempts += 1
                            continue
                    else:
                        log_meta, commit, index_meta = recheck_log, recheck_commit, recheck_index
                        initial_bytes = log_meta[0]
                        index_size = index_meta[0] if index_meta is not None else None
                        log_digest, index_digest, index_status = _verify_snapshot_unlocked(
                            repository,
                            log_size=initial_bytes,
                            commit=commit,
                            index_size=index_size,
                        )

                    if index_status == "structural_corrupt":
                        raise ObservationCorruptionError("observation index is structurally corrupted")

                    cache_key = (
                        resolved_path,
                        initial_bytes,
                        log_digest,
                        index_digest,
                        index_status,
                        commit.chain_sha256 if commit is not None else None,
                        window["start_ms"],
                        window["end_ms"],
                        display_limit,
                        scan_limit,
                        evidence_key,
                    )
                    if not locked and repository.lock_path.exists():
                        continue
                    with _OBSERVATION_CACHE_LOCK:
                        cached = _OBSERVATION_CACHE.get(cache_key)
                        if cached is not None:
                            _OBSERVATION_CACHE.move_to_end(cache_key)
                            if not repository.invalid_marker_path.exists():
                                result = copy.deepcopy(cached)
                                result["read_at_ms"] = clock.now_ms()
                                return result

                    index_verified = (
                        index_status == "verified"
                        and commit is not None
                        and commit.seq > 0
                    )

                    if evidence_key is not None and commit is not None and commit.seq > scan_limit:
                        if not index_verified:
                            raise ObservationCorruptionError(
                                "observation index cannot be verified against durable commit boundary"
                            )
                        evidence_obs, read_cycles = _scan_indexed_evidence(
                            repository,
                            total_count=commit.seq,
                            commit_offset=commit.offset,
                            evidence_key=evidence_key,
                        )
                        complete = True
                    elif evidence_key is None and index_verified and commit.seq > scan_limit:
                        (
                            complete,
                            rows,
                            latest_obs,
                            latest_all_obs,
                            counts,
                            total_cycles,
                            total_observations,
                            duplicates,
                            read_cycles,
                            first_at,
                            last_at,
                        ) = _scan_indexed_window(
                            repository,
                            total_count=commit.seq,
                            commit_offset=commit.offset,
                            window=window,
                            display_limit=display_limit,
                            scan_limit=scan_limit,
                        )
                    else:
                        stream = repository.iter_forward_cycles()
                        try:
                            for _, _, cycle, digest in stream:
                                if read_cycles >= scan_limit:
                                    complete = False
                                    break
                                read_cycles += 1
                                previous_digest = seen.get(cycle.cycle_id)
                                if previous_digest is not None:
                                    if previous_digest != digest:
                                        raise ObservationCorruptionError("cycle identity conflicts")
                                    duplicates += 1
                                    continue
                                seen[cycle.cycle_id] = digest

                                if evidence_key is not None:
                                    if evidence_obs is None and cycle.cycle_id == evidence_key[0]:
                                        for observation in cycle.observations:
                                            if observation.observation_id == evidence_key[1]:
                                                evidence_obs = observation
                                                break
                                    # Continue validating remaining records for conflicting cycle IDs.
                                    continue

                                for observation in cycle.observations:
                                    latest_all_obs[observation.symbol] = observation

                                if cycle.created_at_ms < window["start_ms"] or cycle.created_at_ms >= window["end_ms"]:
                                    continue

                                total_cycles += 1
                                first_at = cycle.created_at_ms if first_at is None else min(first_at, cycle.created_at_ms)
                                last_at = cycle.created_at_ms if last_at is None else max(last_at, cycle.created_at_ms)
                                for observation in cycle.observations:
                                    counts[observation.outcome.value] += 1
                                    total_observations += 1
                                    latest_obs[observation.symbol] = observation
                                    rows.append(observation)
                        finally:
                            close_stream = getattr(stream, "close", None)
                            if close_stream is not None:
                                close_stream()

                    if repository.invalid_marker_path.exists():
                        raise ObservationCorruptionError("observation repository has an unresolved failed-write marker")
                    if path.stat().st_size < initial_bytes:
                        raise ObservationCorruptionError("observation log was truncated during review")
                    if not locked and repository.lock_path.exists():
                        continue
            except _SnapshotChanged:
                continue
            except ObservationCorruptionError:
                if not locked and repository.lock_path.exists():
                    continue
                raise

            serialized = {}

            def _payload(observation) -> dict:
                key = id(observation)
                value = serialized.get(key)
                if value is None:
                    value = observation.to_dict()
                    serialized[key] = value
                return value

            result = _source(
                "ok" if complete else "incomplete", clock.now_ms(),
                "已读取扫描日志。" if complete else
                "达到扫描读取上限；以下只是已读取部分，不能代表完整观察窗口。",
                records=[_payload(item) for item in rows],
                latest=[_payload(item) for item in latest_obs.values()],
                latest_all=[_payload(item) for item in latest_all_obs.values()],
                evidence=_payload(evidence_obs) if (complete and evidence_obs is not None) else None,
                counts=dict(counts),
                total_cycles=total_cycles, total_observations=total_observations,
                duplicate_cycles=duplicates, scanned_cycles=read_cycles, read_through_byte=initial_bytes,
                complete=complete, first_at_ms=first_at, last_at_ms=last_at,
                display_limit=display_limit, display_truncated=total_observations > display_limit,
            )
            with _OBSERVATION_CACHE_LOCK:
                _OBSERVATION_CACHE[cache_key] = copy.deepcopy(result)
                _OBSERVATION_CACHE.move_to_end(cache_key)
                while len(_OBSERVATION_CACHE) > _CACHE_MAX_ENTRIES:
                    _OBSERVATION_CACHE.popitem(last=False)
            return result
    except (OSError, ObservationCorruptionError) as exc:
        logger.warning("Observation log unavailable at %s: %s", path, exc)
        # Never publish plausible-looking partial statistics from a corrupt log.
        return _source("unavailable", clock.now_ms(), "扫描日志读取失败，请检查日志及 .invalid 标记。")


def read_signal_review(data_dir: Path, window: dict, *, clock=None,
                       display_limit: int = DISPLAY_LIMIT, scan_limit: int = SCAN_LIMIT) -> dict:
    if type(display_limit) is not int or display_limit < 1 or type(scan_limit) is not int or scan_limit < 1:
        raise ValueError("review limits must be positive integers")
    clock = clock or SystemClock()
    data_dir = Path(data_dir).resolve()
    health = HealthStore(data_dir)
    started_at = clock.now_ms()
    try:
        state, running = health.snapshot()
        at = clock.now_ms()
        current = health_view(state, running=running, now_ms=at)
        service = _source("ok" if state is not None else "missing", at,
                          "仅代表本次查询时的服务状态。" if state is not None else "尚无服务记录。",
                          view=current, symbols=list(state.symbols) if state else [])
    except (OSError, HealthStateError):
        service = _source("unavailable", clock.now_ms(), "暂时无法读取服务状态。")
    observations = _read_observations(health.root / "observations.jsonl", window, clock=clock,
                                      display_limit=display_limit, scan_limit=scan_limit)
    try:
        store = NotificationStore(data_dir)
        try:
            store.path.stat()
        except FileNotFoundError:
            notifications = _source("missing", clock.now_ms(), "尚无通知记录。")
        else:
            snapshot = store.review_snapshot(start_ms=window["start_ms"], end_ms=window["end_ms"], limit=display_limit)
            notifications = _source("ok", clock.now_ms(),
                                    "已读取通知记录；送达状态截至本次查询。",
                                    **snapshot)
    except (OSError, sqlite3.Error, NotificationError):
        notifications = _source("unavailable", clock.now_ms(), "通知记录读取失败，请检查数据目录。")
    sources = {"service": service, "observations": observations, "notifications": notifications}
    feedback_store = SignalFeedbackStore(data_dir)
    try:
        feedback = {"records": feedback_store.read(), "available": True}
    except (OSError, sqlite3.Error):
        feedback = {"records": {}, "available": False}
    return {
        "schema_version": 1, "data_dir": str(data_dir), "window": window,
        "started_at_ms": started_at, "generated_at_ms": clock.now_ms(), "sources": sources,
        "sources_readable": all(item["state"] == "ok" for item in sources.values()),
        "feedback": {**feedback, "path": str(feedback_store.path)},
        "positions": ManualPositionLedger(data_dir).view(),
        "current_conclusions": current_conclusions(service, observations, now_ms=clock.now_ms()),
    }



def current_conclusions(service, observations, *, now_ms):
    """Current calendar projection and last verified scan are distinct evidence."""
    latest = {row["symbol"]: row for row in observations.get("latest_all", [])}
    view = service.get("view", {})
    last_cycle = view.get("last_cycle") or {}
    cycle = (last_cycle.get("scan") or {}).get("cycle") or {}
    health_rows = {row["symbol"]: row for row in cycle.get("observations", [])}
    results = []
    for symbol in sorted(set(service.get("symbols", [])) | set(latest)):
        row = latest.get(symbol)
        item = {"symbol": symbol, "as_of_ms": now_ms, "latest": row, "schedule": None,
                "status": "unavailable", "message": "当前策略结论无法核实"}
        try:
            config = (selected_strategy_groups(symbol, [row["strategy_name"]])[0].config if row
                      else baseline_strategy_group(symbol).config)
            item["schedule"] = trading_window_schedule(now_ms, config)
        except (ValueError, OSError, TypeError):
            item["message"] = "日历或标的配置不可用，停止判断入场"
            results.append(item)
            continue
        result = (row.get("scan_result") or {}) if row else {}
        missing = [label for key, label in (
            ("evaluated_candle_open_ms", "评估 K 线开始时间"),
            ("evaluated_candle_close_ms", "评估 K 线结束时间"),
            ("calendar_id", "参考日历 ID"), ("calendar_sha256", "日历内容指纹"),
            ("calendar_session", "评估时日历状态"), ("trading_windows_et", "评估时策略窗口"),
        ) if result.get(key) is None]
        if service.get("state") != "ok" or observations.get("state") != "ok":
            item["message"] = "来源读取不完整，当前策略结论无法核实"
        elif not row or health_rows.get(symbol) != row:
            item["message"] = "扫描日志与最新服务周期未对齐，仅保留最后已知记录"
        elif row["outcome"] in {"scan_failed", "data_gate_blocked"}:
            item["message"] = "最近一轮扫描失败，暂无可采信结论" if row["outcome"] == "scan_failed" else "最近一轮数据校验未通过，停止判断入场"
        elif (not view.get("healthy") or view.get("runtime") != "running"
              or last_cycle.get("service_run_id") != view.get("run_id")):
            item["message"] = "服务未处于可核实的正常运行状态，仅保留最后已知记录"
        elif missing:
            item["message"] = "最近记录缺少评估依据，当前结论待核实。缺少：" + "、".join(missing) + "。"
        elif not 0 <= now_ms - row["observed_at_ms"] <= 600_000:
            item["message"] = "最近扫描已超过 10 分钟或时间异常，等待新一轮确认"
        elif not 0 <= now_ms - result["evaluated_candle_close_ms"] <= 1_800_000:
            item["message"] = "评估 K 线已超过 30 分钟或尚未收盘，等待新一轮确认"
        elif row["strategy_config_fingerprint"] != canonical_payload_sha256(config):
            item["message"] = "最近扫描与当前策略配置不同，等待新配置扫描"
        elif not item["schedule"]["allowed"]:
            item.update(status="waiting", message="参考市场休市，等待下一策略窗口" if item["schedule"]["reason"] == "reference_market_closed"
                        else "当前在策略时段外，等待下一窗口")
        elif row["decision_code"] in {"reference_market_closed", "current_bar_outside_trading_window"}:
            item.update(status="waiting", message="当前已进入策略窗口，等待窗口内已收盘 K 线的扫描")
        else:
            item.update(status="current", message="最近一轮可用于人工复核" if row["outcome"] == "ready_for_review" else "最近一轮仍在等待策略条件")
        results.append(item)
    return results


def read_scan_evidence(data_dir, cycle_id, observation_id, *, clock=None):
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
           for value in (cycle_id, observation_id)):
        raise ValueError("invalid observation identity")
    clock = clock or SystemClock()
    result = _read_observations(HealthStore(Path(data_dir)).root / "observations.jsonl",
                                {"start_ms": 0, "end_ms": 2**63}, clock=clock, display_limit=1,
                                scan_limit=SCAN_LIMIT, evidence_key=(cycle_id, observation_id))
    if result["state"] != "ok":
        raise ObservationCorruptionError("evidence source is unavailable or incomplete")
    return result["evidence"]


def validate_review_output(data_dir: Path, output: Path) -> None:
    """Derived reports may never replace trusted data or service evidence."""
    roots = (Path(data_dir).resolve(), HealthStore(data_dir).root.resolve())
    target = Path(output).resolve()
    if Path(output).suffix.lower() != ".html":
        raise ValueError("output must be an .html report")
    if any(target == root or target.is_relative_to(root) for root in roots):
        raise ValueError("report output cannot be inside trusted data or service state")
