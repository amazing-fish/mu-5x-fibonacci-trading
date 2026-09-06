import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from mu_strategy.commands.render_signal_review import main
from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
from mu_strategy.models import EntryDecisionCode
from mu_strategy.notifications.events import AlertEvent, AlertKind, DeliveryState
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.observations import JsonlObservationRepository, Stage0ObservationCycle
from mu_strategy.scan_cycle import ScanCycle
from mu_strategy.service_health import (
    CycleHealth, HealthSnapshotUnstableError, HealthStore, Phase, RefreshHealth,
    ScanHealth, ServiceState, StepStatus,
)
from mu_strategy.signal_review import BEIJING, read_signal_review, review_window
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.viz.signal_review import render_signal_review
from tests.factories.scan_cycle import scan_result, trusted_scan_bundle


NOW = int(datetime(2026, 9, 6, 12, tzinfo=BEIJING).timestamp() * 1000)
SYMBOL = "MU-USDT-SWAP"


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

    def test_large_notification_window_reads_history_once_and_retains_complete_display_history(self):
        self.initialize([cycle(1)])
        with self.store.connection() as db, self.store.transaction(db):
            for number in range(3000):
                event = self.event(number)
                self.store.enqueue(db, event, now_ms=NOW)
                self.store.claim(db, now_ms=NOW + 1)
                self.store.finish(db, event.event_id, state=DeliveryState.CONFIRMED, now_ms=NOW + 2, code="smtp_accepted")
        statements = []
        original = self.store.connection

        @contextmanager
        def traced_connection(**kwargs):
            with original(**kwargs) as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(self.store, "connection", traced_connection):
            snapshot = self.store.review_snapshot(start_ms=self.window["start_ms"], end_ms=self.window["end_ms"], limit=2)
        self.assertEqual(3000, snapshot["total"])
        self.assertEqual(2, len(snapshot["records"]))
        self.assertEqual([3, 3], [len(row["history"]) for row in snapshot["records"]])
        self.assertEqual([self.event(2998).event_id, self.event(2999).event_id], [row["event_id"] for row in snapshot["records"]])
        history_reads = [sql for sql in statements if sql.startswith("SELECT") and "FROM delivery_history" in sql]
        self.assertEqual(1, len(history_reads))

    def test_invalid_history_in_undisplayed_window_event_still_rejects_source(self):
        self.initialize([cycle(1)])
        with self.store.connection() as db, self.store.transaction(db):
            for number in range(3):
                self.store.enqueue(db, self.event(number), now_ms=NOW)
            db.execute("UPDATE delivery_history SET action='' WHERE event_id=?", (self.event(0).event_id,))
        self.assertEqual("unavailable", self.read(display_limit=1)["sources"]["notifications"]["state"])

    def test_orphan_delivery_history_rejects_source_instead_of_reporting_zero_events(self):
        self.initialize([cycle(1)])
        with self.store.connection() as db, self.store.transaction(db):
            self.store.enqueue(db, self.event(1), now_ms=NOW)
        # Simulate an external partial deletion outside the guarded writer.
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DELETE FROM outbox")
            db.commit()
        source = self.read()["sources"]["notifications"]
        self.assertEqual("unavailable", source["state"])
        self.assertNotIn("total", source)
        self.assertEqual(2, main(["--data-dir", str(self.data_dir), "--output", str(self.root / "review.html")],
                                 clock=self.clock, stdout=io.StringIO()))

    def test_complete_history_of_outside_window_event_remains_valid(self):
        self.initialize([cycle(1)])
        with self.store.connection() as db, self.store.transaction(db):
            self.store.enqueue(db, self.event(1, at=NOW - 4 * 86400000), now_ms=NOW)
            self.store.claim(db, now_ms=NOW)
            self.store.finish(db, self.event(1).event_id, state=DeliveryState.CONFIRMED, now_ms=NOW + 1, code="smtp_accepted")
        source = self.read()["sources"]["notifications"]
        self.assertEqual("ok", source["state"])
        self.assertEqual(0, source["total"])
        self.assertEqual({"confirmed": 1}, source["all_counts"])

    def test_missing_lifecycle_rows_are_not_verified_even_outside_display_limit(self):
        self.initialize([cycle(1)])
        with self.store.connection() as db, self.store.transaction(db):
            self.store.enqueue(db, self.event(1), now_ms=NOW)
            self.store.claim(db, now_ms=NOW)
            self.store.finish(db, self.event(1).event_id, state=DeliveryState.CONFIRMED, now_ms=NOW + 1, code="smtp_accepted")
            self.store.enqueue(db, self.event(2), now_ms=NOW)
            original = [tuple(row) for row in db.execute("SELECT * FROM delivery_history")]
        for action in ("recorded", "attempt_started", "attempt_finished", "all"):
            with self.subTest(action=action):
                with self.store.connection() as db, self.store.transaction(db):
                    db.execute("DELETE FROM delivery_history WHERE event_id=? AND (action=? OR ?='all')",
                               (self.event(1).event_id, action, action))
                report = self.read(display_limit=1)
                self.assertFalse(report["sources_verified"])
                self.assertEqual("unavailable", report["sources"]["notifications"]["state"])
                output = self.root / "incomplete-history.html"
                self.assertEqual(2, main(["--data-dir", str(self.data_dir), "--output", str(output)], clock=self.clock, stdout=io.StringIO()))
                with self.store.connection() as db, self.store.transaction(db):
                    db.execute("DELETE FROM delivery_history")
                    db.executemany("INSERT INTO delivery_history VALUES (?,?,?,?,?)", original)

    def test_history_must_match_attempt_count_outcome_suppression_and_retry_deadline(self):
        self.initialize([cycle(1)])
        with self.store.connection() as db, self.store.transaction(db):
            self.store.enqueue(db, self.event(1), now_ms=NOW)
            self.store.claim(db, now_ms=NOW)
            self.store.finish(db, self.event(1).event_id, state=DeliveryState.CONFIRMED, now_ms=NOW + 1, code="smtp_accepted")
            original = tuple(db.execute("SELECT * FROM outbox").fetchone())
        for change in ("attempts=2", "state='failed'", "suppressed_reason='review_expired'", "next_attempt_ms=0", "retryable=1"):
            with self.subTest(change=change):
                with self.store.connection() as db, self.store.transaction(db):
                    db.execute("UPDATE outbox SET " + change)
                self.assertEqual("unavailable", self.read()["sources"]["notifications"]["state"])
                with self.store.connection() as db, self.store.transaction(db):
                    db.execute("UPDATE outbox SET state=?,attempts=?,next_attempt_ms=?,retryable=?,suppressed_reason=? WHERE event_id=?",
                               (*original[2:], original[0]))

    def test_valid_defer_unknown_manual_resolution_and_retry_history(self):
        self.initialize([cycle(1)])
        event = self.event(1)
        with self.store.connection() as db, self.store.transaction(db):
            self.store.enqueue(db, event, now_ms=NOW)
            self.store.claim(db, now_ms=NOW)
            self.store.defer_unstarted(db, event.event_id, now_ms=NOW + 1)
        self.assertEqual("ok", self.read()["sources"]["notifications"]["state"])
        with self.store.connection() as db, self.store.transaction(db):
            self.store.claim(db, now_ms=NOW + 30_001)
            self.store.finish(db, event.event_id, state=DeliveryState.UNKNOWN, now_ms=NOW + 30_002, code="smtp_tls_error")
        self.assertEqual("ok", self.read()["sources"]["notifications"]["state"])
        self.store.resolve(event.event_id, outcome=DeliveryState.FAILED, now_ms=NOW + 30_003)
        self.store.retry_failed(event.event_id, now_ms=NOW + 30_004)
        with self.store.connection() as db, self.store.transaction(db):
            self.store.claim(db, now_ms=NOW + 30_005)
            self.store.finish(db, event.event_id, state=DeliveryState.CONFIRMED, now_ms=NOW + 30_006, code="smtp_accepted")
            self.store.suppress(db, event.event_id, "decision_changed", NOW + 30_007)
        report = self.read()["sources"]["notifications"]
        self.assertEqual("ok", report["state"])
        self.assertEqual(2, report["records"][0]["attempts"])
        self.assertEqual(10, len(report["records"][0]["history"]))

    def test_scan_safety_limit_marks_incomplete_and_returns_nonzero(self):
        self.initialize([cycle(number) for number in range(4)])
        report = self.read(scan_limit=2)
        self.assertFalse(report["sources_verified"])
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
        self.assertIn("查询失败不等于服务已经停止", render_signal_review(report))

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
        self.assertIn('"strategy_code_version": "unknown"'.replace('"', '&quot;'), page)

    def test_output_is_read_only_and_escapes_untrusted_text(self):
        hostile = '</script><script>alert("unsafe")</script><img src=x onerror=alert(1)>'
        self.initialize([cycle(1, reason=hostile)])
        before = {p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        with patch("mu_strategy.notifications.service.EmailAlerts.collect", side_effect=AssertionError("must not collect")), \
             patch("mu_strategy.notifications.smtp.SmtpConfig.from_environment", side_effect=AssertionError("must not read secrets")), \
             patch.object(NotificationStore, "initialize", side_effect=AssertionError("must not initialize")), \
             patch.object(HealthStore, "prepare", side_effect=AssertionError("must not prepare")):
            page = render_signal_review(self.read())
        after = {p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
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


if __name__ == "__main__":
    unittest.main()
