import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from mu_strategy.commands.render_signal_review import main
from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
from mu_strategy.models import EntryDecisionCode
from mu_strategy.notifications.events import AlertEvent, AlertKind, DeliveryState
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.observations import JsonlObservationRepository, ObservationCorruptionError, Stage0ObservationCycle
from mu_strategy.scan_cycle import ScanCycle
from mu_strategy.service_health import (
    CycleHealth, HealthSnapshotUnstableError, HealthStore, Phase, RefreshHealth,
    ScanHealth, ServiceState, StepStatus,
)
from mu_strategy.signal_review import BEIJING, read_scan_evidence, read_signal_review, review_window
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.viz.signal_review import render_signal_review
from tests.factories.scan_cycle import scan_result, trusted_scan_bundle


NOW = int(datetime(2026, 9, 6, 12, tzinfo=BEIJING).timestamp() * 1000)
SYMBOL = "MU-USDT-SWAP"


def sqlite_logical_snapshot(root: Path) -> dict[str, dict[str, list[tuple]]]:
    """Read logical SQLite table rows via read-only connections (includes uncommitted-to-main WAL state)."""
    snapshot: dict[str, dict[str, list[tuple]]] = {}
    for db_path in sorted(root.rglob("*.sqlite3")):
        if not db_path.is_file():
            continue
        con = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        try:
            tables = [
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            snapshot[str(db_path.relative_to(root))] = {
                table: con.execute(f'SELECT * FROM "{table}"').fetchall()
                for table in tables
            }
        finally:
            con.close()
    return snapshot


def cycle(number, kind="wait", *, at=NOW, symbol=SYMBOL, reason="fixture reason"):
    group = baseline_strategy_group(symbol)
    observed = ScanCycle(clock=Mock(now_ms=lambda: at),
                         id_factory=iter((f"cycle-{number}", f"observation-{number}")).__next__)
    bundle = trusted_scan_bundle(symbol=symbol, allowed=kind != "blocked",
                                 reason=HealthReason.STALE_BY_CLOCK if kind == "blocked" else HealthReason.OK)
    bundle = replace(bundle, observed_at_ms=at, load_context=replace(bundle.load_context, observed_at_ms=at))
    code = EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY if kind == "ready" else EntryDecisionCode.WAITING_SECOND_PULLBACK
    scanner = Mock(side_effect=RuntimeError("fixture failure")) if kind == "failed" else Mock(return_value=scan_result(code, symbol=symbol, reason=reason))
    observed.scan_symbol(symbol=symbol, source="watchlist", bundle=bundle, requested_intervals=("15m", "1h"),
                         strategy_name=group.name, strategy_config=group.config, scanner=scanner, data_failure=None)
    return observed.observations()


class SignalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "live"
        self.clock = Mock(now_ms=lambda: NOW)
        self.window = review_window(now_ms=NOW, days=2)
        self.health = HealthStore(self.data_dir)
        self.store = NotificationStore(self.data_dir)
        self.log = JsonlObservationRepository(self.health.root / "observations.jsonl")

    def initialize(self, cycles):
        self.store.initialize()
        for item in cycles:
            self.log.append_cycle(item)
        last = cycles[-1]
        state = ServiceState(str(self.data_dir.resolve()), tuple(item.symbol for item in last.observations),
                             "run-1", True, Phase.IDLE, NOW - 1000, NOW, NOW + 330_000,
                             CycleHealth(1, "run-1", NOW, NOW,
                                         RefreshHealth(StepStatus.SUCCEEDED, "trusted-run", RefreshAttemptStatus.SUCCESS, SnapshotUsability.USABLE, 0),
                                         ScanHealth(StepStatus.SUCCEEDED, last, StepStatus.SUCCEEDED)), 0, (), 0)
        self.health.write(state)

    def read(self, **kwargs):
        return read_signal_review(self.data_dir, self.window, clock=self.clock, **kwargs)

    def event(self, number, *, at=NOW, kind=AlertKind.SERVICE_FAULT, observation=None, related=None):
        return AlertEvent(kind, f"{number:064x}", at, at + 300_000 if kind is AlertKind.ENTRY_REVIEW else None,
                          "ready" if kind is AlertKind.ENTRY_REVIEW else "decision_changed" if kind is AlertKind.SIGNAL_INVALIDATED else "health_event",
                          observation, related, ("runtime.stopped",) if kind is AlertKind.SERVICE_FAULT else ())

    def test_complete_mixed_window_deduplicates_cycles_and_separates_cycle_and_symbol_counts(self):
        first = cycle(1, "ready")
        second = cycle(2, "wait", symbol="BTC-USDT-SWAP")
        combined = Stage0ObservationCycle(first.cycle_id, first.created_at_ms,
                                         first.observations + (replace(second.observations[0], cycle_id=first.cycle_id),))
        self.initialize([cycle(0, at=NOW - 4 * 86400000), combined, cycle(3, "blocked"), cycle(4, "failed"), combined])
        source = self.read()["sources"]["observations"]
        self.assertEqual(3, source["total_cycles"])
        self.assertEqual(4, source["total_observations"])
        self.assertEqual(1, source["duplicate_cycles"])
        self.assertEqual({"ready_for_review": 1, "normal_no_action": 1, "data_gate_blocked": 1, "scan_failed": 1}, source["counts"])

    def test_conflicting_cycle_discards_all_log_statistics(self):
        first = cycle(1)
        changed = replace(first, observations=(replace(first.observations[0], compatibility_reason="different"),))
        self.initialize([first, changed])
        source = self.read()["sources"]["observations"]
        self.assertEqual("unavailable", source["state"])
        self.assertNotIn("counts", source)

    def test_more_than_fifty_notifications_use_complete_window_and_one_read_transaction(self):
        self.initialize([cycle(1)])
        with self.store.connection() as db, self.store.transaction(db):
            for number in range(65):
                self.store.enqueue(db, self.event(number), now_ms=NOW)
            self.store.enqueue(db, self.event(100, at=NOW - 4 * 86400000), now_ms=NOW)
        with patch.object(NotificationStore, "status", side_effect=AssertionError("recent 50 is not a window")):
            source = self.read()["sources"]["notifications"]
        self.assertEqual(65, source["total"])
        self.assertEqual(65, len(source["records"]))
        self.assertEqual({"pending": 65}, source["counts"])
        self.assertEqual({"pending": 66}, source["all_counts"])

    def test_display_limits_do_not_silently_truncate_window_counts(self):
        self.initialize([cycle(number) for number in range(5)])
        with self.store.connection() as db, self.store.transaction(db):
            for number in range(5):
                self.store.enqueue(db, self.event(number), now_ms=NOW)
        report = self.read(display_limit=2)
        self.assertEqual(5, report["sources"]["observations"]["total_observations"])
        self.assertEqual(5, report["sources"]["notifications"]["total"])
        for name in ("observations", "notifications"):
            self.assertEqual(2, len(report["sources"][name]["records"]))
            self.assertTrue(report["sources"][name]["display_truncated"])
        self.assertIn("筛选仅作用于这些明细", render_signal_review(report))


    def test_scan_safety_limit_marks_incomplete_and_returns_nonzero(self):
        self.initialize([cycle(number) for number in range(4)])
        report = self.read(scan_limit=2)
        self.assertFalse(report["sources_readable"])
        self.assertEqual("incomplete", report["sources"]["observations"]["state"])
        self.assertIn("不是完整窗口", render_signal_review(report))

    def test_invalid_marker_bad_record_and_unknown_database_schema_are_visible(self):
        self.initialize([cycle(1)])
        self.log.invalid_marker_path.write_text("uncommitted", encoding="utf-8")
        self.assertEqual("unavailable", self.read()["sources"]["observations"]["state"])
        self.log.invalid_marker_path.unlink()
        with self.log.path.open("ab") as handle:
            handle.write(b'{"incomplete":')
        with self.store.connection() as db:
            db.execute("PRAGMA user_version = 99")
        report = self.read()
        self.assertEqual("unavailable", report["sources"]["observations"]["state"])
        self.assertEqual("unavailable", report["sources"]["notifications"]["state"])
        self.assertEqual("ok", report["sources"]["service"]["state"])

    def test_unstable_health_is_query_unavailable_not_stopped(self):
        self.initialize([cycle(1)])
        with patch.object(HealthStore, "snapshot", side_effect=HealthSnapshotUnstableError()):
            report = self.read()
        self.assertEqual("unavailable", report["sources"]["service"]["state"])
        self.assertNotIn("view", report["sources"]["service"])
        self.assertEqual("ok", report["sources"]["observations"]["state"])
        self.assertIn("暂时无法读取服务状态", render_signal_review(report))

    def test_missing_sources_are_not_initialized_and_still_produce_an_explanatory_report(self):
        report_path = self.root / "reports" / "empty.html"
        result = main(["--data-dir", str(self.data_dir), "--output", str(report_path)], clock=self.clock, stdout=io.StringIO())
        self.assertEqual(2, result)
        self.assertTrue(report_path.is_file())
        self.assertFalse(self.data_dir.exists())
        self.assertFalse(self.health.root.exists())
        self.assertIn("尚无扫描日志", report_path.read_text(encoding="utf-8"))

    def test_window_is_beijing_calendar_dates_inclusive_with_end_exclusive(self):
        boundary = int(datetime(2026, 9, 6, tzinfo=BEIJING).timestamp() * 1000)
        selected = review_window(now_ms=boundary, days=1)
        self.assertEqual(boundary, selected["start_ms"])
        self.assertEqual(boundary + 86400000, selected["end_ms"])
        self.window = selected
        self.initialize([cycle(0, at=boundary - 1), cycle(1, at=boundary), cycle(2, at=boundary + 86400000)])
        self.assertEqual(1, self.read()["sources"]["observations"]["total_cycles"])
        for kwargs in ({"days": 0}, {"days": True}, {"from_date": "2026-09-07", "to_date": "2026-09-06"}):
            with self.assertRaises(ValueError):
                review_window(now_ms=NOW, **kwargs)

    def test_delivery_link_suppression_and_current_result_remain_separate(self):
        observed = cycle(1, "ready")
        self.initialize([observed])
        entry = self.event(1, kind=AlertKind.ENTRY_REVIEW, observation=observed.observations[0])
        invalid = self.event(2, kind=AlertKind.SIGNAL_INVALIDATED, observation=observed.observations[0], related=entry.event_id)
        with self.store.connection() as db, self.store.transaction(db):
            self.store.enqueue(db, entry, now_ms=NOW)
            self.store.claim(db, now_ms=NOW)
            self.store.finish(db, entry.event_id, state=DeliveryState.CONFIRMED, now_ms=NOW + 86400000 * 2, code="smtp_accepted")
            self.store.suppress(db, entry.event_id, "decision_changed", NOW + 1)
            self.store.enqueue(db, invalid, now_ms=NOW + 1)
            self.store.claim(db, now_ms=NOW + 1)  # Interrupted attempt stays unknown.
        report = self.read()
        source = report["sources"]["notifications"]
        self.assertEqual({"confirmed": 1, "unknown": 1}, source["counts"])
        self.assertEqual(1, source["suppressed"])
        self.assertEqual(NOW + 2 * 86400000, source["records"][0]["history"][2]["at_ms"])
        page = render_signal_review(report)
        self.assertIn(f'href="#event-{entry.event_id}"', page)
        self.assertIn("后续扫描决定已变化", page)
        self.assertIn("SMTP 接受不证明收件箱到达", page)
        self.assertIn("结果不明", page)
        self.assertIn("送达状态截至本次查询", page)

    def test_output_is_read_only_and_escapes_untrusted_text(self):
        hostile = '</script><script>alert("unsafe")</script><img src=x onerror=alert(1)>'
        self.initialize([cycle(1, reason=hostile)])
        before = {
            p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))
        }
        before_db = sqlite_logical_snapshot(self.root)
        with patch("mu_strategy.notifications.service.EmailAlerts.collect", side_effect=AssertionError("must not collect")), \
             patch("mu_strategy.notifications.smtp.SmtpConfig.from_environment", side_effect=AssertionError("must not read secrets")), \
             patch.object(NotificationStore, "initialize", side_effect=AssertionError("must not initialize")), \
             patch.object(HealthStore, "prepare", side_effect=AssertionError("must not prepare")):
            page = render_signal_review(self.read())
        after = {
            p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))
        }
        self.assertEqual(before, after)
        self.assertEqual(before_db, sqlite_logical_snapshot(self.root))
        self.assertNotIn(hostile, page)
        self.assertIn("&lt;/script&gt;", page)
        self.assertEqual(1, page.count("<script>"))
        self.assertNotIn('<script src=', page)
        self.assertNotIn('"strategy_code_version": "0296', page)

    def test_output_inside_either_input_tree_is_rejected_without_changes(self):
        self.initialize([cycle(1)])
        for path in (self.data_dir / "review.html", self.health.root / "review.html"):
            with self.assertRaises(SystemExit):
                main(["--data-dir", str(self.data_dir), "--output", str(path)], clock=self.clock)
            self.assertFalse(path.exists())

    def test_existing_report_is_preserved_when_atomic_replace_fails(self):
        self.initialize([cycle(1)])
        target = self.root / "report.html"
        target.write_text("prior report", encoding="utf-8")
        with patch("mu_strategy.commands.render_signal_review.os.replace", side_effect=OSError()):
            self.assertEqual(2, main(["--data-dir", str(self.data_dir), "--output", str(target)], clock=self.clock, stdout=io.StringIO()))
        self.assertEqual("prior report", target.read_text(encoding="utf-8"))
        self.assertFalse(list(self.root.glob("signal-review-*.tmp")))

    def test_tail_read_handles_ancient_history_and_caches_unchanged_log(self):
        ancient = [cycle(i, at=NOW - (20 - i) * 86_400_000) for i in range(10)]
        recent = [cycle(100, at=NOW - 600_000), cycle(101, at=NOW)]
        self.initialize([*ancient, *recent])
        first = self.read()["sources"]["observations"]
        self.assertEqual("ok", first["state"])
        self.assertEqual(2, first["total_cycles"])
        self.assertEqual(len(ancient) + len(recent), first["scanned_cycles"])
        with patch.object(JsonlObservationRepository, "iter_forward_cycles", side_effect=AssertionError("should use cache")):
            cached = self.read()["sources"]["observations"]
        self.assertEqual(first["total_cycles"], cached["total_cycles"])
        self.assertEqual(first["records"], cached["records"])
        evidence = read_scan_evidence(self.data_dir, recent[-1].cycle_id, recent[-1].observations[0].observation_id, clock=self.clock)
        self.assertEqual(recent[-1].observations[0].observation_id, evidence["observation_id"])
        # Appending invalidates cache and shows the new cycle immediately.
        self.log.append_cycle(cycle(102, at=NOW + 60_000))
        refreshed = self.read()["sources"]["observations"]
        self.assertEqual(3, refreshed["total_cycles"])
        # An invalid marker must bypass cache and fail closed.
        self.log.invalid_marker_path.write_text("interrupted", encoding="utf-8")
        self.assertEqual("unavailable", self.read()["sources"]["observations"]["state"])

    def test_non_monotonic_append_order_does_not_drop_earlier_in_window_cycle(self):
        recent_a = cycle(1, "ready", at=NOW - 600_000)
        ancient_b = cycle(2, "wait", at=NOW - 20 * 86_400_000)
        recent_c = cycle(3, "wait", at=NOW)
        self.initialize([recent_a, ancient_b, recent_c])
        source = self.read()["sources"]["observations"]
        self.assertEqual("ok", source["state"])
        self.assertTrue(source["complete"])
        self.assertEqual(2, source["total_cycles"])
        self.assertEqual(2, source["total_observations"])
        self.assertEqual(
            [recent_a.observations[0].observation_id, recent_c.observations[0].observation_id],
            [row["observation_id"] for row in source["records"]],
        )
        # Cached read must preserve the same complete non-monotonic totals.
        with patch.object(JsonlObservationRepository, "iter_forward_cycles", side_effect=AssertionError("should use cache")):
            cached = self.read()["sources"]["observations"]
        self.assertEqual(2, cached["total_cycles"])
        self.assertEqual(source["records"], cached["records"])

    def test_cache_invalidates_when_earlier_record_changes_with_same_size_mtime_and_tail(self):
        first = cycle(1, "wait", at=NOW - 600_000, reason="reason-alpha")
        second = cycle(2, "wait", at=NOW, reason="reason-tail-unchanged")
        self.initialize([first, second])
        before = self.read()["sources"]["observations"]
        self.assertEqual("reason-alpha", before["records"][0]["compatibility_reason"])

        stat = self.log.path.stat()
        raw = self.log.path.read_bytes()
        updated = raw.replace(b"reason-alpha", b"reason-bravo", 1)
        self.assertEqual(len(raw), len(updated))
        self.log.path.write_bytes(updated)
        os.utime(self.log.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        after = self.read()["sources"]["observations"]
        self.assertEqual("ok", after["state"])
        self.assertEqual("reason-bravo", after["records"][0]["compatibility_reason"])

    def test_exact_replay_preserves_first_publication_order_and_keeps_latest_distinct_cycle(self):
        first_a = cycle(1, "ready", at=NOW - 600_000)
        second_b = cycle(2, "wait", at=NOW)
        self.initialize([first_a, second_b])
        # Replay A byte-for-byte after B; service health still references B.
        self.log.append_cycle(first_a)
        report = self.read()
        source = report["sources"]["observations"]
        self.assertEqual("ok", source["state"])
        self.assertEqual(1, source["duplicate_cycles"])
        self.assertEqual(second_b.observations[0].observation_id, source["latest_all"][0]["observation_id"])
        self.assertEqual(second_b.observations[0].observation_id, source["latest"][0]["observation_id"])
        self.assertEqual(
            [first_a.observations[0].observation_id, second_b.observations[0].observation_id],
            [row["observation_id"] for row in source["records"]],
        )
        # current_conclusions must align with health (B), not reject due to replayed A.
        self.assertEqual(second_b.observations[0].observation_id, report["current_conclusions"][0]["latest"]["observation_id"])
        self.assertNotIn("未对齐", report["current_conclusions"][0]["message"])

    def test_scan_evidence_rejects_conflicting_cycle_ids_both_within_and_above_scan_limit(self):
        first = cycle(1, "ready", at=NOW - 600_000)
        conflict = replace(first, observations=(replace(first.observations[0], compatibility_reason="different"),))
        other = cycle(2, "wait", at=NOW)
        self.initialize([first, other, conflict])
        self.assertEqual("unavailable", self.read()["sources"]["observations"]["state"])
        with self.assertRaises(ObservationCorruptionError):
            read_scan_evidence(self.data_dir, first.cycle_id, first.observations[0].observation_id, clock=self.clock)

        # Also exercise indexed path (total cycles > scan_limit) via _read_observations directly.
        from mu_strategy.signal_review import _read_observations

        res = _read_observations(
            self.log.path,
            {"start_ms": 0, "end_ms": 2**63},
            clock=self.clock,
            display_limit=1,
            scan_limit=2,
            evidence_key=(first.cycle_id, first.observations[0].observation_id),
        )
        self.assertEqual("unavailable", res["state"])

    def test_indexed_log_exceeding_scan_limit_stays_complete_when_window_is_covered(self):
        ancient = [cycle(i, "wait", at=NOW - (25 - i) * 86_400_000) for i in range(12)]
        recent_1 = cycle(100, "ready", at=NOW - 600_000)
        recent_2 = cycle(101, "wait", at=NOW)
        self.initialize([*ancient, recent_1, recent_2])

        # Total cycles (14) > scan_limit (4), but monotonic prefix_max proves all in-window cycles are in the 2-cycle tail.
        report = self.read(scan_limit=4)
        source = report["sources"]["observations"]
        self.assertEqual("ok", source["state"])
        self.assertTrue(source["complete"])
        self.assertTrue(report["sources_readable"])
        self.assertEqual(2, source["total_cycles"])
        self.assertEqual(2, source["scanned_cycles"])
        self.assertEqual(recent_2.observations[0].observation_id, source["latest_all"][0]["observation_id"])
        self.assertNotIn("来源读取不完整", report["current_conclusions"][0]["message"])

        # Replaying an ancient prefix cycle at the end must NOT overwrite recent_2 as latest_all.
        self.log.append_cycle(ancient[0])
        replayed = self.read(scan_limit=4)["sources"]["observations"]
        self.assertEqual("ok", replayed["state"])
        self.assertEqual(1, replayed["duplicate_cycles"])
        self.assertEqual(recent_2.observations[0].observation_id, replayed["latest_all"][0]["observation_id"])

        # Non-monotonic log where an in-window cycle precedes > scan_limit older cycles MUST stay incomplete.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir2 = Path(tmp) / "live"
            health2 = HealthStore(data_dir2)
            store2 = NotificationStore(data_dir2)
            store2.initialize()
            log2 = JsonlObservationRepository(health2.root / "observations.jsonl")
            mixed = [cycle(1, "ready", at=NOW - 600_000), *[cycle(i + 2, "wait", at=NOW - 20 * 86_400_000) for i in range(8)], cycle(99, "wait", at=NOW)]
            for item in mixed:
                log2.append_cycle(item)
            res2 = read_signal_review(data_dir2, self.window, clock=self.clock, scan_limit=4)["sources"]["observations"]
            self.assertEqual("incomplete", res2["state"])
            self.assertFalse(res2["complete"])

    def test_damaged_evidence_index_returns_unavailable_instead_of_missing_above_scan_limit(self):
        from mu_strategy.observations import _INDEX_ENTRY_BYTES
        from mu_strategy.signal_review import _read_observations

        cycles = [cycle(i, "wait", at=NOW - (15 - i) * 86_400_000) for i in range(10)]
        self.initialize(cycles)
        target = cycles[1]

        # Validated index locates target above scan_limit.
        valid = _read_observations(
            self.log.path,
            {"start_ms": 0, "end_ms": 2**63},
            clock=self.clock,
            display_limit=1,
            scan_limit=4,
            evidence_key=(target.cycle_id, target.observations[0].observation_id),
        )
        self.assertEqual("ok", valid["state"])
        self.assertEqual(target.observations[0].observation_id, valid["evidence"]["observation_id"])

        # Corrupt only cycle_id_hash16 of entry #1 (offsets & timestamps remain intact!).
        raw_index = bytearray(self.log.index_path.read_bytes())
        hash_offset = 1 * _INDEX_ENTRY_BYTES + 32  # after <QQQq (32 bytes)
        raw_index[hash_offset] ^= 0xFF
        self.log.index_path.write_bytes(bytes(raw_index))

        damaged = _read_observations(
            self.log.path,
            {"start_ms": 0, "end_ms": 2**63},
            clock=self.clock,
            display_limit=1,
            scan_limit=4,
            evidence_key=(target.cycle_id, target.observations[0].observation_id),
        )
        self.assertEqual("unavailable", damaged["state"])
        self.assertIsNone(damaged.get("evidence"))

    def test_stale_index_after_same_length_prefix_repair_does_not_claim_complete_coverage(self):
        ancient = [cycle(i, "wait", at=NOW - (25 - i) * 86_400_000, reason="reason-alpha") for i in range(8)]
        recent = [cycle(100, "ready", at=NOW - 600_000), cycle(101, "wait", at=NOW)]
        self.initialize([*ancient, *recent])

        warm = self.read(scan_limit=4)["sources"]["observations"]
        self.assertEqual("ok", warm["state"])
        self.assertTrue(warm["complete"])

        # Same-length in-place edit in prefix (leaving .index and .commit untouched with stale timestamps/chain).
        stat = self.log.path.stat()
        raw = self.log.path.read_bytes()
        updated = raw.replace(b"reason-alpha", b"reason-bravo", 1)
        self.assertEqual(len(raw), len(updated))
        self.log.path.write_bytes(updated)
        os.utime(self.log.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        # Must NOT trust the stale index to claim complete coverage; falls back to bounded scan -> incomplete.
        after = self.read(scan_limit=4)["sources"]["observations"]
        self.assertEqual("incomplete", after["state"])
        self.assertFalse(after["complete"])

    def test_indexed_window_preserves_latest_all_for_prefix_only_symbols_independent_of_completeness(self):
        btc_old_1 = cycle(1, "wait", at=NOW - 20 * 86_400_000, symbol="BTC-USDT-SWAP")
        btc_old_2 = cycle(2, "ready", at=NOW - 19 * 86_400_000, symbol="BTC-USDT-SWAP")
        eth_old = cycle(3, "wait", at=NOW - 18 * 86_400_000, symbol="ETH-USDT-SWAP")
        more_ancient = [cycle(i, "wait", at=NOW - (17 - i) * 86_400_000, symbol=SYMBOL) for i in range(4, 10)]
        recent_mu_1 = cycle(100, "ready", at=NOW - 600_000, symbol=SYMBOL)
        recent_mu_2 = cycle(101, "wait", at=NOW, symbol=SYMBOL)
        self.initialize([btc_old_1, btc_old_2, eth_old, *more_ancient, recent_mu_1, recent_mu_2])

        # Complete indexed window (total=11 > scan_limit=4, suffix=2 <= 4).
        source = self.read(scan_limit=4)["sources"]["observations"]
        self.assertEqual("ok", source["state"])
        self.assertTrue(source["complete"])
        latest_all_by_symbol = {row["symbol"]: row["observation_id"] for row in source["latest_all"]}
        self.assertEqual(len(source["latest_all"]), len(latest_all_by_symbol), "latest_all must not duplicate symbols")
        self.assertEqual(
            {
                "BTC-USDT-SWAP": btc_old_2.observations[0].observation_id,
                "ETH-USDT-SWAP": eth_old.observations[0].observation_id,
                SYMBOL: recent_mu_2.observations[0].observation_id,
            },
            latest_all_by_symbol,
        )

        # Incomplete indexed window (5 recent MU cycles > scan_limit=4) must STILL preserve BTC and ETH in latest_all.
        for extra in range(102, 106):
            self.log.append_cycle(cycle(extra, "wait", at=NOW + extra * 1000, symbol=SYMBOL))
        inc = self.read(scan_limit=4)["sources"]["observations"]
        self.assertEqual("incomplete", inc["state"])
        self.assertFalse(inc["complete"])
        inc_by_symbol = {row["symbol"]: row["observation_id"] for row in inc["latest_all"]}
        self.assertEqual(len(inc["latest_all"]), len(inc_by_symbol))
        self.assertEqual(btc_old_2.observations[0].observation_id, inc_by_symbol["BTC-USDT-SWAP"])
        self.assertEqual(eth_old.observations[0].observation_id, inc_by_symbol["ETH-USDT-SWAP"])
        self.assertEqual("observation-105", inc_by_symbol[SYMBOL])

    def test_refresh_verification_does_not_hold_publication_fence_during_full_log_digest(self):
        from mu_strategy import signal_review as sr

        self.initialize([cycle(1, "wait", at=NOW - 600_000), cycle(2, "ready", at=NOW)])
        # Prime cache first so we also test a cache-hit refresh.
        self.assertEqual("ok", self.read()["sources"]["observations"]["state"])

        in_verify = threading.Event()
        release_verify = threading.Event()
        real_verify = sr._verify_snapshot_unlocked
        reader_result = {}

        def paused_verify(*args, **kwargs):
            in_verify.set()
            release_verify.wait()
            return real_verify(*args, **kwargs)

        def run_reader():
            with patch.object(sr, "_verify_snapshot_unlocked", side_effect=paused_verify):
                reader_result["obs"] = self.read()["sources"]["observations"]

        reader = threading.Thread(target=run_reader)
        reader.start()
        try:
            self.assertTrue(in_verify.wait(timeout=5), "reader did not reach unlocked verification")
            # Exclusive writer publication_fence MUST acquire without blocking while full-log verification runs!
            with self.log.publication_fence(wait=False):
                pass
            release_verify.set()
            reader.join(timeout=5)
        finally:
            release_verify.set()
            reader.join(timeout=5)

        self.assertEqual("ok", reader_result["obs"]["state"])

    def test_warm_cache_invalidates_and_rejects_when_index_offset_is_damaged(self):
        from mu_strategy.observations import _INDEX_ENTRY_BYTES

        ancient = [cycle(i, "wait", at=NOW - (20 - i) * 86_400_000) for i in range(8)]
        recent = [cycle(100, "ready", at=NOW - 600_000), cycle(101, "wait", at=NOW)]
        self.initialize([*ancient, *recent])

        warm = self.read(scan_limit=4)["sources"]["observations"]
        self.assertEqual("ok", warm["state"])
        self.assertTrue(warm["complete"])

        # Damage entry #1 offset in .index while preserving exact file size and mtime, without touching .jsonl/.commit.
        idx_stat = self.log.index_path.stat()
        raw_index = bytearray(self.log.index_path.read_bytes())
        raw_index[_INDEX_ENTRY_BYTES] ^= 0x01
        self.log.index_path.write_bytes(bytes(raw_index))
        os.utime(self.log.index_path, ns=(idx_stat.st_atime_ns, idx_stat.st_mtime_ns))

        after = self.read(scan_limit=4)["sources"]["observations"]
        self.assertEqual("unavailable", after["state"])


if __name__ == "__main__":
    unittest.main()
