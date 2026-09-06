"""Read-only application view of signal, service and notification evidence."""
from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter, deque
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from mu_strategy.market_data.trusted_data.contracts import SystemClock
from mu_strategy.notifications.events import NotificationError
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.observations import JsonlObservationRepository, ObservationCorruptionError
from mu_strategy.service_health import HealthStateError, HealthStore, health_view


BEIJING = timezone(timedelta(hours=8))
DISPLAY_LIMIT = 2000
SCAN_LIMIT = 100_000


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


def _read_observations(path: Path, window: dict, *, clock, display_limit: int, scan_limit: int) -> dict:
    repository = JsonlObservationRepository(path)
    counts = Counter()
    rows = deque(maxlen=display_limit)
    latest = {}
    seen = {}
    total_cycles = total_observations = duplicates = read_cycles = 0
    cursor = (0, 0, None)
    first_at = last_at = None
    try:
        # Capture a finite prefix; normal later appends need not keep a report
        # running forever. The final batch may also include a concurrent append.
        try:
            initial_bytes = path.stat().st_size
        except FileNotFoundError:
            repository.read_batch()  # Still reject a failed-write marker.
            return _source("missing", clock.now_ms(), "尚无扫描日志。")
        while True:
            if read_cycles >= scan_limit:
                complete = False
                break
            batch, following = repository.read_batch(
                offset=cursor[0], anchor_start=cursor[1], anchor_sha256=cursor[2],
                limit=min(250, scan_limit - read_cycles),
            )
            read_cycles += len(batch)
            if not batch and following[0] < initial_bytes:
                raise ObservationCorruptionError("observation log was truncated during review")
            for cycle in batch:
                fingerprint = hashlib.sha256(cycle.to_json().encode("utf-8")).hexdigest()
                if cycle.cycle_id in seen:
                    if seen[cycle.cycle_id] != fingerprint:
                        raise ObservationCorruptionError("cycle identity conflicts")
                    duplicates += 1
                    continue
                seen[cycle.cycle_id] = fingerprint
                if not window["start_ms"] <= cycle.created_at_ms < window["end_ms"]:
                    continue
                total_cycles += 1
                first_at = cycle.created_at_ms if first_at is None else min(first_at, cycle.created_at_ms)
                last_at = cycle.created_at_ms if last_at is None else max(last_at, cycle.created_at_ms)
                for observation in cycle.observations:
                    payload = observation.to_dict()
                    rows.append(payload)
                    # This is last in the validated append order, not a claim
                    # about unknown service-run/attempt identity.
                    latest[observation.symbol] = payload
                    counts[observation.outcome.value] += 1
                    total_observations += 1
            cursor = following
            if not batch or cursor[0] >= initial_bytes:
                complete = True
                break
        return _source(
            "ok" if complete else "incomplete", clock.now_ms(),
            "已读取扫描日志。" if complete else
            "达到扫描读取上限；以下只是已读取部分，不能代表完整观察窗口。",
            records=list(rows), latest=list(latest.values()), counts=dict(counts),
            total_cycles=total_cycles, total_observations=total_observations,
            duplicate_cycles=duplicates, scanned_cycles=read_cycles, read_through_byte=cursor[0],
            complete=complete, first_at_ms=first_at, last_at_ms=last_at,
            display_limit=display_limit, display_truncated=total_observations > display_limit,
        )
    except (OSError, ObservationCorruptionError):
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
                          view=current)
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
    return {
        "schema_version": 1, "data_dir": str(data_dir), "window": window,
        "started_at_ms": started_at, "generated_at_ms": clock.now_ms(), "sources": sources,
        "sources_readable": all(item["state"] == "ok" for item in sources.values()),
    }


def validate_review_output(data_dir: Path, output: Path) -> None:
    """Derived reports may never replace trusted data or service evidence."""
    roots = (Path(data_dir).resolve(), HealthStore(data_dir).root.resolve())
    target = Path(output).resolve()
    if Path(output).suffix.lower() != ".html":
        raise ValueError("output must be an .html report")
    if any(target == root or target.is_relative_to(root) for root in roots):
        raise ValueError("report output cannot be inside trusted data or service state")
