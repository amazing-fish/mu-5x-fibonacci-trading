"""Read-only application view of signal, service and notification evidence."""
from __future__ import annotations

import copy
import logging
import re
import sqlite3
import threading
from collections import Counter, OrderedDict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from mu_strategy.market_data.trusted_data.contracts import SystemClock
from mu_strategy.manual_positions import ManualPositionLedger
from mu_strategy.notifications.events import NotificationError
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.observations import JsonlObservationRepository, ObservationCorruptionError, canonical_payload_sha256
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


def _tail_fingerprint(path: Path, size: int) -> bytes:
    if size <= 0:
        return b""
    read_len = min(64, size)
    with path.open("rb") as handle:
        handle.seek(size - read_len)
        return handle.read(read_len)


def _read_observations(path: Path, window: dict, *, clock, display_limit: int, scan_limit: int, evidence_key=None) -> dict:
    repository = JsonlObservationRepository(path)
    try:
        while True:
            locked = False
            counts = Counter()
            rows_rev = []
            latest_obs, latest_all_obs, evidence_obs = {}, {}, None
            seen = {}
            total_cycles = total_observations = duplicates = read_cycles = 0
            first_at = last_at = None
            try:
                with repository.read_fence() as locked:
                    if repository.invalid_marker_path.exists():
                        raise ObservationCorruptionError("observation repository has an unresolved failed-write marker")
                    try:
                        stat = path.stat()
                    except FileNotFoundError:
                        if repository.invalid_marker_path.exists():
                            raise ObservationCorruptionError("observation repository has an unresolved failed-write marker")
                        if not locked and repository.lock_path.exists():
                            continue
                        return _source("missing", clock.now_ms(), "尚无扫描日志。")
                    initial_bytes = stat.st_size
                    cache_key = (
                        str(path.resolve()),
                        initial_bytes,
                        stat.st_mtime_ns,
                        stat.st_ino,
                        _tail_fingerprint(path, initial_bytes),
                        window["start_ms"],
                        window["end_ms"],
                        display_limit,
                        scan_limit,
                        evidence_key,
                    )
                    with _OBSERVATION_CACHE_LOCK:
                        cached = _OBSERVATION_CACHE.get(cache_key)
                        if cached is not None:
                            _OBSERVATION_CACHE.move_to_end(cache_key)
                            if not repository.invalid_marker_path.exists():
                                result = copy.deepcopy(cached)
                                result["read_at_ms"] = clock.now_ms()
                                return result

                    complete = True
                    tail = repository.iter_tail_cycles()
                    try:
                        for cycle in tail:
                            if read_cycles >= scan_limit:
                                complete = False
                                break
                            read_cycles += 1
                            previous = seen.get(cycle.cycle_id)
                            if previous is not None:
                                if previous != cycle:
                                    raise ObservationCorruptionError("cycle identity conflicts")
                                duplicates += 1
                                continue
                            seen[cycle.cycle_id] = cycle

                            for observation in reversed(cycle.observations):
                                if observation.symbol not in latest_all_obs:
                                    latest_all_obs[observation.symbol] = observation
                                if evidence_key is not None and evidence_obs is None and (cycle.cycle_id, observation.observation_id) == evidence_key:
                                    evidence_obs = observation

                            if evidence_key is not None:
                                if evidence_obs is not None:
                                    break
                                continue

                            # Repository does not enforce monotonic timestamps (clock rollbacks are valid),
                            # so continue scanning past older records up to scan_limit rather than breaking early.
                            if cycle.created_at_ms < window["start_ms"] or cycle.created_at_ms >= window["end_ms"]:
                                continue

                            total_cycles += 1
                            first_at = cycle.created_at_ms if first_at is None else min(first_at, cycle.created_at_ms)
                            last_at = cycle.created_at_ms if last_at is None else max(last_at, cycle.created_at_ms)
                            for observation in reversed(cycle.observations):
                                counts[observation.outcome.value] += 1
                                total_observations += 1
                                if observation.symbol not in latest_obs:
                                    latest_obs[observation.symbol] = observation
                                if len(rows_rev) < display_limit:
                                    rows_rev.append(observation)
                    finally:
                        close_tail = getattr(tail, "close", None)
                        if close_tail is not None:
                            close_tail()

                    if repository.invalid_marker_path.exists():
                        raise ObservationCorruptionError("observation repository has an unresolved failed-write marker")
                    if path.stat().st_size < initial_bytes:
                        raise ObservationCorruptionError("observation log was truncated during review")
                    if not locked and repository.lock_path.exists():
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
                records=[_payload(item) for item in reversed(rows_rev)],
                latest=[_payload(item) for item in reversed(list(latest_obs.values()))],
                latest_all=[_payload(item) for item in reversed(list(latest_all_obs.values()))],
                evidence=_payload(evidence_obs) if evidence_obs is not None else None,
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
