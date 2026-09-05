import io
import json
import smtplib
import sqlite3
import ssl
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from mu_strategy.commands.email_alerts import main
from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
from mu_strategy.models import EntryDecisionCode
from mu_strategy.notifications.events import AlertEvent, AlertKind, DeliveryState, NotificationError
from mu_strategy.notifications.service import EmailAlerts
from mu_strategy.notifications.smtp import SendResult, SmtpConfig, SmtpTransport, render_message
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.observations import JsonlObservationRepository, ObservationCorruptionError
from mu_strategy.scan_cycle import ScanCycle
from mu_strategy.service_health import CycleHealth, HealthEvent, HealthSnapshotUnstableError, HealthStateError, HealthStore, Phase, RefreshHealth, ScanHealth, ServiceState, StepStatus
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.signal_service import ServiceConfig, SignalService
from tests.factories.scan_cycle import scan_result, trusted_scan_bundle


SYMBOL = "MU-USDT-SWAP"
CONFIG = SmtpConfig("smtp.126.com", "sender@example.test", "reader@example.test", "fake-test-authorization")


class Clock:
    value = 1_000_000

    def now_ms(self):
        return self.value


class EmailAlertsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / "live"
        self.clock = Clock()
        self.health = HealthStore(self.data_dir)
        self.alerts = EmailAlerts(self.data_dir, clock=self.clock, health=self.health)
        self.alerts.initialize()
        self.sequence = 0
        self.state = None
        self.running = True
        self.snapshot = patch.object(self.health, "snapshot", side_effect=lambda: (self.state, self.running)).start()
        self.addCleanup(patch.stopall)
        self.transport = Mock(target_fingerprint=CONFIG.target_fingerprint,
                              send=Mock(return_value=SendResult(DeliveryState.CONFIRMED, "smtp_accepted")))

    def publish(self, kind="ready", *, signal=42, generation="trusted-run", events=None, at=None, append=True, config=None, symbol=SYMBOL, run_id="service-1"):
        self.sequence += 1
        at = self.clock.value if at is None else at
        group = baseline_strategy_group(symbol)
        clock = Mock(now_ms=Mock(return_value=at))
        cycle = ScanCycle(clock=clock, id_factory=iter((f"cycle-{self.sequence}", f"observation-{self.sequence}")).__next__)
        bundle = trusted_scan_bundle(symbol=symbol, allowed=kind != "blocked", reason=HealthReason.STALE_BY_CLOCK if kind == "blocked" else HealthReason.OK)
        manifest = replace(bundle.load_context.manifest, run_id=generation)
        bundle = replace(bundle, run_id=generation, observed_at_ms=at,
                         load_context=replace(bundle.load_context, manifest=manifest, generation_id=generation))
        code = EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY if kind == "ready" else EntryDecisionCode.WAITING_SECOND_PULLBACK
        result = replace(scan_result(code, symbol=symbol), signal_time_ms=signal)
        scanner = Mock(side_effect=RuntimeError("failure")) if kind == "failed" else Mock(return_value=result)
        cycle.scan_symbol(symbol=symbol, source="watchlist", bundle=bundle, requested_intervals=("15m", "1h"),
                          strategy_name=group.name, strategy_config=config or group.config, scanner=scanner, data_failure=None)
        observed = cycle.observations()
        if append:
            self.alerts.observations.append_cycle(observed)
        events = events if events is not None else (HealthEvent(1, 900_000, "started", ()),)
        self.state = ServiceState(str(self.data_dir.resolve()), (symbol,), run_id, True, Phase.IDLE,
                                  min(900_000, at), at, at + 330_000,
                                  CycleHealth(self.sequence, run_id, at, at,
                                              RefreshHealth(StepStatus.SUCCEEDED, generation, RefreshAttemptStatus.SUCCESS, SnapshotUsability.USABLE, 0),
                                              ScanHealth(StepStatus.SUCCEEDED, observed, StepStatus.SUCCEEDED)),
                                  0, events, events[-1].sequence)
        self.state = ServiceState.from_dict(self.state.to_dict())
        return observed

    def records(self, kind=None):
        records = self.alerts.store.status()["records"]
        return records if kind is None else [record for record in records if record["kind"] == kind.value]

    def entry_id(self):
        return self.records(AlertKind.ENTRY_REVIEW)[0]["event_id"]

    def test_collect_dry_run_and_send_are_separate(self):
        self.publish()
        result = self.alerts.collect()
        self.assertTrue(result["caught_up"])
        self.assertEqual("pending", self.records()[0]["state"])
        self.transport.send.assert_not_called()
        self.assertEqual(1, self.alerts.deliver(self.transport))
        self.assertEqual("confirmed", self.records()[0]["state"])
        self.assertEqual(0, self.alerts.deliver(self.transport))

    def test_generation_refresh_and_restart_do_not_duplicate_same_ready_signal(self):
        self.publish()
        self.alerts.collect()
        first = self.entry_id()
        self.alerts.deliver(self.transport)
        self.clock.value += 1000
        self.publish(generation="new-generation")
        self.alerts = EmailAlerts(self.data_dir, clock=self.clock, health=self.health)
        self.alerts.initialize()
        self.alerts.collect()
        self.assertEqual([first], [record["event_id"] for record in self.records(AlertKind.ENTRY_REVIEW)])
        self.assertEqual(0, self.alerts.deliver(self.transport))

    def test_invalidation_and_new_ready_transition_are_traced(self):
        self.publish()
        self.alerts.collect()
        self.alerts.deliver(self.transport)
        original = self.entry_id()
        self.clock.value += 1000
        self.publish("wait")
        self.alerts.collect()
        invalid = self.records(AlertKind.SIGNAL_INVALIDATED)[0]
        evidence = self.alerts.store.status(event_id=invalid["event_id"])
        self.assertEqual(original, evidence["event"]["related_event_id"])
        self.assertEqual("decision_changed", evidence["event"]["reason"])
        self.assertIsNone(invalid["suppressed_reason"])
        self.clock.value += 1000
        self.publish()
        self.alerts.collect()
        self.assertEqual(2, len(self.records(AlertKind.ENTRY_REVIEW)))

    def test_unknown_data_withdraws_trust_without_claiming_strategy_reversal(self):
        self.publish()
        self.alerts.collect()
        self.alerts.deliver(self.transport)
        self.clock.value += 1
        self.publish("blocked")
        self.alerts.collect()
        invalid = self.records(AlertKind.SIGNAL_INVALIDATED)[0]
        self.assertEqual("source_unavailable", self.alerts.store.status(event_id=invalid["event_id"])["event"]["reason"])

    def test_old_ready_is_preserved_but_never_sent(self):
        self.publish(at=self.clock.value - 300_001)
        self.alerts.collect()
        self.assertEqual("review_expired", self.records(AlertKind.ENTRY_REVIEW)[0]["suppressed_reason"])
        self.assertEqual(0, self.alerts.deliver(self.transport))
        self.transport.send.assert_not_called()

    def test_expiry_boundary_is_exclusive(self):
        self.publish()
        self.alerts.collect()
        self.clock.value += 300_000
        self.alerts.deliver(self.transport)
        self.transport.send.assert_not_called()
        self.assertEqual("review_expired", self.records(AlertKind.ENTRY_REVIEW)[0]["suppressed_reason"])

    def test_no_delivery_when_newer_health_cycle_has_not_been_consumed(self):
        self.publish()
        self.alerts.collect()
        self.clock.value += 1
        self.publish("wait")
        self.assertEqual(0, self.alerts.deliver(self.transport))
        self.transport.send.assert_not_called()

    def test_same_ready_new_cycle_defers_then_sends_original_event_after_catching_up(self):
        self.publish()
        self.alerts.collect()
        original = self.entry_id()
        self.clock.value += 1
        self.publish(generation="new-generation")
        self.assertEqual(0, self.alerts.deliver(self.transport))
        record = self.alerts.store.status(event_id=original)["records"][0]
        self.assertEqual(("pending", 0, None), (record["state"], record["attempts"], record["suppressed_reason"]))
        self.clock.value += 30_000
        self.assertTrue(self.alerts.collect()["caught_up"])
        self.assertEqual(1, self.alerts.deliver(self.transport))
        self.assertEqual(original, self.transport.send.call_args.args[0].event_id)

    def test_temporarily_unreadable_log_defers_delivery_without_terminal_suppression(self):
        self.publish()
        self.alerts.collect()
        self.alerts.observation_unavailable()
        self.assertEqual(0, self.alerts.deliver(self.transport))
        self.assertIsNone(self.records()[0]["suppressed_reason"])
        self.clock.value += 30_000
        self.alerts.collect()
        self.assertEqual(1, self.alerts.deliver(self.transport))

    def test_new_ready_log_waits_for_health_publication_without_being_withdrawn(self):
        self.publish("failed")
        self.alerts.collect()
        previous = self.state
        self.clock.value += 1
        self.publish()
        current = self.state
        self.state = previous
        self.assertFalse(self.alerts.collect()["caught_up"])
        original = self.entry_id()
        self.alerts.deliver(self.transport)
        self.transport.send.assert_not_called()
        self.assertIsNone(self.records(AlertKind.ENTRY_REVIEW)[0]["suppressed_reason"])
        self.state = current
        self.clock.value += 30_000
        self.assertEqual(0, self.alerts.collect()["cycles_consumed"])
        self.assertEqual(1, self.alerts.deliver(self.transport))
        self.assertEqual(original, self.transport.send.call_args.args[0].event_id)

    def test_new_scope_log_is_not_withdrawn_using_previous_scope_health(self):
        self.publish("wait")
        self.alerts.collect()
        previous = self.state
        self.clock.value += 1
        self.publish(symbol="BTC-USDT-SWAP", run_id="service-2")
        current = self.state
        self.state = previous
        self.assertFalse(self.alerts.collect()["caught_up"])
        self.assertIsNone(self.records(AlertKind.ENTRY_REVIEW)[0]["suppressed_reason"])
        self.alerts.deliver(self.transport)
        self.transport.send.assert_not_called()
        self.state = current
        self.clock.value += 30_000
        self.assertTrue(self.alerts.collect()["caught_up"])
        self.assertEqual(1, self.alerts.deliver(self.transport))

    def test_collection_clock_is_read_after_new_log_and_health(self):
        original_read = self.alerts.observations.read_batch
        def publish_during_read(**kwargs):
            self.clock.value += 1
            self.publish()
            return original_read(**kwargs)
        with patch.object(self.alerts.observations, "read_batch", side_effect=publish_during_read):
            self.assertTrue(self.alerts.collect()["caught_up"])
        self.assertEqual(1, self.alerts.deliver(self.transport))

    def test_backlog_larger_than_batch_advances_without_premature_scope_invalidation(self):
        cycles = []
        for index in range(1001):
            self.clock.value += 1
            cycles.append(self.publish("wait" if index < 1000 else "ready", append=False))
        self.alerts.observations.path.write_text("".join(json.dumps(cycle.to_dict()) + "\n" for cycle in cycles), encoding="utf-8")
        first = self.alerts.collect()
        self.assertEqual((1000, False), (first["cycles_consumed"], first["caught_up"]))
        self.assertEqual(0, self.alerts.deliver(self.transport))
        second = self.alerts.collect()
        self.assertEqual((1, True), (second["cycles_consumed"], second["caught_up"]))
        self.assertEqual(1, self.alerts.deliver(self.transport))

    def test_clock_rollback_new_run_accepts_current_cycle_and_keeps_old_log_ordering(self):
        old = self.publish()
        self.alerts.collect()
        self.alerts.deliver(self.transport)
        self.clock.value -= 500_000
        self.publish(run_id="service-2")
        self.alerts.collect()
        entries = self.records(AlertKind.ENTRY_REVIEW)
        self.assertEqual(2, len(entries))
        self.assertIsNone(entries[0]["suppressed_reason"])
        self.alerts.deliver(self.transport)
        self.assertEqual("confirmed", self.records(AlertKind.ENTRY_REVIEW)[0]["state"])
        self.alerts.observations.append_cycle(old)
        self.assertFalse(self.alerts.collect()["caught_up"])
        self.assertEqual(2, len(self.records(AlertKind.ENTRY_REVIEW)))

    def test_clock_rollback_log_before_health_is_reconciled_without_new_log_bytes(self):
        self.publish("wait")
        self.alerts.collect()
        previous = self.state
        self.clock.value -= 500_000
        self.publish(run_id="service-2")
        current = self.state
        self.state = previous
        self.assertFalse(self.alerts.collect()["caught_up"])
        self.assertEqual([], self.records(AlertKind.ENTRY_REVIEW))
        self.state = current
        self.assertEqual(0, self.alerts.collect()["cycles_consumed"])
        self.alerts.deliver(self.transport)
        self.assertEqual("confirmed", self.records(AlertKind.ENTRY_REVIEW)[0]["state"])

    def test_same_signal_new_service_run_without_rollback_is_not_duplicated(self):
        self.publish()
        self.alerts.collect()
        self.alerts.deliver(self.transport)
        self.clock.value += 1
        self.publish(run_id="service-2")
        self.alerts.collect()
        self.assertEqual(1, len(self.records(AlertKind.ENTRY_REVIEW)))
        self.assertEqual(0, self.alerts.deliver(self.transport))

    def test_removed_symbol_pending_entry_is_suppressed_after_valid_service_restart(self):
        self.publish()
        self.alerts.collect()
        original = self.entry_id()
        self.clock.value += 1
        self.publish("wait", symbol="BTC-USDT-SWAP", run_id="service-2")
        self.assertTrue(self.alerts.collect()["caught_up"])
        self.assertEqual("source_unavailable", self.alerts.store.status(event_id=original)["records"][0]["suppressed_reason"])
        self.alerts.deliver(self.transport)
        self.transport.send.assert_not_called()

    def test_removed_symbol_with_confirmed_or_unknown_entry_gets_invalidation(self):
        for outcome in (DeliveryState.CONFIRMED, DeliveryState.UNKNOWN):
            with self.subTest(outcome=outcome):
                self.clock.value += 100
                self.publish(signal=self.clock.value, run_id="service-3")
                self.alerts.collect()
                original = self.entry_id()
                self.transport.send.return_value = SendResult(outcome, "smtp_accepted" if outcome is DeliveryState.CONFIRMED else "smtp_result_unknown")
                self.alerts.deliver(self.transport)
                self.clock.value += 1
                self.publish("wait", symbol="BTC-USDT-SWAP", run_id="service-4")
                self.alerts.collect()
                related = [row for row in self.records(AlertKind.SIGNAL_INVALIDATED)
                           if self.alerts.store.status(event_id=row["event_id"])["event"]["related_event_id"] == original]
                self.assertEqual(1, len(related))
                self.assertIsNone(related[0]["suppressed_reason"])

    def test_send_time_symbol_check_does_not_trust_global_caught_up_flag(self):
        self.publish()
        self.alerts.collect()
        self.clock.value += 1
        current = self.publish("wait", symbol="BTC-USDT-SWAP", run_id="service-2")
        from mu_strategy.canonical import canonical_sha256
        with self.alerts.store.connection() as db, self.alerts.store.transaction(db):
            self.alerts.store.set_meta(db, "last_cycle_sha256", canonical_sha256(current.to_dict()))
        self.alerts.deliver(self.transport)
        self.transport.send.assert_not_called()

    def test_unstable_pre_send_snapshot_defers_without_spending_smtp_attempt(self):
        self.publish()
        self.alerts.collect()
        self.snapshot.side_effect = HealthSnapshotUnstableError("retry")
        self.assertEqual(0, self.alerts.deliver(self.transport))
        self.assertEqual(("pending", 0), (self.records()[0]["state"], self.records()[0]["attempts"]))
        self.transport.send.assert_not_called()
        self.snapshot.side_effect = lambda: (self.state, self.running)
        self.clock.value += 30_000
        self.alerts.collect()
        self.assertEqual(1, self.alerts.deliver(self.transport))
        self.assertEqual("confirmed", self.records()[0]["state"])

    def test_new_signal_and_config_changes_get_new_identities(self):
        self.publish()
        self.alerts.collect()
        self.clock.value += 1
        self.publish(signal=43)
        self.alerts.collect()
        self.clock.value += 1
        config = replace(baseline_strategy_group(SYMBOL).config, leverage=3)
        self.publish(signal=43, config=config)
        self.alerts.collect()
        self.assertEqual(3, len(self.records(AlertKind.ENTRY_REVIEW)))

    def test_duplicate_and_out_of_order_records_do_not_resurrect_old_signal(self):
        original = self.publish()
        self.alerts.collect()
        self.clock.value += 10
        self.publish("wait")
        self.alerts.collect()
        self.alerts.observations.append_cycle(original)
        self.alerts.collect()
        self.assertEqual(1, len(self.records(AlertKind.ENTRY_REVIEW)))
        with self.alerts.store.connection() as db:
            self.assertEqual(1, self.alerts.store.get_meta(db, "out_of_order_observations"))

    def test_identical_duplicate_is_ignored(self):
        cycle = self.publish()
        self.alerts.collect()
        self.alerts.observations.append_cycle(cycle)
        self.alerts.collect()
        self.assertEqual(1, len(self.records(AlertKind.ENTRY_REVIEW)))

    def test_input_cursor_and_outbox_commit_or_rollback_together(self):
        self.publish()
        with patch.object(self.alerts, "_health_events", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.alerts.collect()
        self.assertEqual([], self.records())
        with self.alerts.store.connection() as db:
            self.assertIsNone(self.alerts.store.get_meta(db, "observation_cursor"))
        self.alerts.collect()
        self.assertEqual(1, len(self.records()))

    def test_health_events_are_deduplicated_and_normal_wait_is_not_failure(self):
        self.publish("wait")
        self.alerts.collect()
        self.assertEqual([], self.records())
        events = (HealthEvent(1, 900_000, "started", ()), HealthEvent(2, self.clock.value, "fault", ("refresh.failed",)),
                  HealthEvent(3, self.clock.value, "recovered", ()))
        self.state = replace(self.state, events=events, event_sequence=3)
        self.alerts.collect()
        self.alerts.collect()
        self.assertEqual(2, len(self.records()))

    def test_health_cursor_gap_requires_explicit_reconciliation(self):
        self.publish("wait", events=(HealthEvent(9, self.clock.value, "fault", ("refresh.failed",)),))
        with self.assertRaises(HealthStateError):
            self.alerts.collect()
        self.assertEqual([], self.records())
        self.alerts.reconcile_health()
        self.alerts.collect()
        with self.alerts.store.connection() as db:
            self.assertEqual(9, self.alerts.store.get_meta(db, "health_cursor"))
            self.assertTrue(self.alerts.store.get_meta(db, "health_reconciliation")["missing_history_acknowledged"])

    def test_stopped_runtime_and_recovery_are_not_repeated_each_poll(self):
        self.publish("wait")
        self.alerts.collect()
        self.running = False
        self.alerts.collect()
        self.alerts.collect()
        self.assertEqual(1, len(self.records(AlertKind.SERVICE_FAULT)))
        self.running = True
        self.alerts.collect()
        self.assertEqual(1, len(self.records(AlertKind.SERVICE_RECOVERED)))

    def test_durable_stop_restart_between_polls_emits_fault_and_healthy_recovery_once(self):
        self.publish("wait")
        self.alerts.collect()
        events = self.state.events + (HealthEvent(2, self.clock.value, "stopped", ()),
                                      HealthEvent(3, self.clock.value + 1, "restarted", ()))
        self.clock.value += 2
        self.publish("wait", run_id="service-2", events=events)
        self.alerts.collect()
        self.alerts.collect()
        self.assertEqual(1, len(self.records(AlertKind.SERVICE_FAULT)))
        self.assertEqual(1, len(self.records(AlertKind.SERVICE_RECOVERED)))

    def test_durable_stop_does_not_duplicate_runtime_fault_or_recover_before_healthy(self):
        self.publish("wait")
        self.alerts.collect()
        events = self.state.events + (HealthEvent(2, self.clock.value, "stopped", ()),)
        self.state = replace(self.state, running=False, events=events, event_sequence=2)
        self.running = False
        self.alerts.collect()
        self.assertEqual(1, len(self.records(AlertKind.SERVICE_FAULT)))
        self.state = replace(self.state, running=True, run_id="service-2", last_cycle=None)
        self.alerts.collect()
        self.assertEqual([], self.records(AlertKind.SERVICE_RECOVERED))
        self.clock.value += 1
        self.running = True
        self.publish("wait", run_id="service-2", events=events)
        self.alerts.collect()
        self.assertEqual(1, len(self.records(AlertKind.SERVICE_RECOVERED)))

    def test_startup_does_not_notify_or_endorse_old_cycle(self):
        self.publish("wait")
        self.state = replace(self.state, run_id="service-2", last_cycle=None)
        self.running = False
        self.alerts.collect()
        self.assertEqual([], self.records())

    def test_source_read_failure_can_notify_independently_of_broken_source(self):
        self.publish()
        self.alerts.collect()
        self.alerts.source_unavailable()
        self.alerts.source_unavailable()
        self.alerts.deliver(self.transport)
        self.assertEqual(1, self.transport.send.call_count)
        self.assertEqual(AlertKind.SERVICE_FAULT, self.transport.send.call_args.args[0].kind)

    def test_smtp_unknown_is_not_automatically_retried(self):
        self.publish()
        self.alerts.collect()
        self.transport.send.return_value = SendResult(DeliveryState.UNKNOWN, "smtp_result_unknown")
        self.alerts.deliver(self.transport)
        self.assertEqual("unknown", self.records()[0]["state"])
        self.alerts.deliver(self.transport)
        self.assertEqual(1, self.transport.send.call_count)

    def test_process_interruption_keeps_committed_unknown_reservation(self):
        self.publish()
        self.alerts.collect()
        self.transport.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.alerts.deliver(self.transport)
        self.assertEqual("unknown", self.records()[0]["state"])
        self.transport.send.side_effect = None
        self.alerts.deliver(self.transport)
        self.assertEqual(1, self.transport.send.call_count)

    def test_result_commit_failure_keeps_unknown_and_does_not_resend(self):
        self.publish()
        self.alerts.collect()
        with patch.object(self.alerts.store, "finish", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.alerts.deliver(self.transport)
        self.assertEqual("unknown", self.records()[0]["state"])
        self.alerts.deliver(self.transport)
        self.assertEqual(1, self.transport.send.call_count)

    def test_send_holds_write_fence_but_status_can_read_unknown(self):
        self.publish()
        self.alerts.collect()
        def send(event):
            self.assertEqual("unknown", self.alerts.store.status()["records"][0]["state"])
            with self.alerts.store.connection() as db:
                db.execute("PRAGMA busy_timeout=0")
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("BEGIN IMMEDIATE")
            return SendResult(DeliveryState.CONFIRMED, "smtp_accepted")
        self.transport.send.side_effect = send
        self.alerts.deliver(self.transport)

    def test_known_transient_failure_has_bounded_backoff(self):
        self.publish("wait", events=(HealthEvent(1, self.clock.value, "fault", ("refresh.failed",)),))
        self.alerts.collect()
        self.transport.send.return_value = SendResult(DeliveryState.FAILED, "smtp_connection_failed", True)
        self.assertEqual(1, self.alerts.deliver(self.transport))
        self.assertEqual(0, self.alerts.deliver(self.transport))
        self.clock.value += 60_000
        self.assertEqual(1, self.alerts.deliver(self.transport))
        self.clock.value += 120_000
        self.assertEqual(1, self.alerts.deliver(self.transport))
        self.clock.value += 1000_000
        self.assertEqual(0, self.alerts.deliver(self.transport))
        self.assertEqual(3, self.records()[0]["attempts"])

    def test_operator_resolution_is_audited_and_only_for_unknown(self):
        self.publish()
        self.alerts.collect()
        self.transport.send.return_value = SendResult(DeliveryState.UNKNOWN, "smtp_result_unknown")
        self.alerts.deliver(self.transport)
        event_id = self.entry_id()
        self.alerts.store.resolve(event_id, outcome=DeliveryState.CONFIRMED, now_ms=self.clock.value)
        evidence = self.alerts.store.status(event_id=event_id)
        self.assertEqual("operator_checked", evidence["history"][-1]["result"])
        with self.assertRaises(NotificationError):
            self.alerts.store.resolve(event_id, outcome=DeliveryState.FAILED, now_ms=self.clock.value)

    def test_destination_change_rejected_without_transmitting(self):
        self.publish()
        self.alerts.collect()
        self.alerts.deliver(self.transport)
        self.transport.target_fingerprint = "different"
        with self.assertRaises(NotificationError):
            self.alerts.deliver(self.transport)
        self.assertEqual(1, self.transport.send.call_count)

    def test_event_roundtrip_and_invalid_current_contract(self):
        self.publish()
        self.alerts.collect()
        payload = self.alerts.store.status(event_id=self.entry_id())["event"]
        self.assertEqual(payload, AlertEvent.from_json(json.dumps(payload)).to_dict())
        mutations = [dict(payload, schema_version=True), dict(payload, schema_version=2), dict(payload, event_id="a" * 64),
                     dict(payload, unknown=1), dict(payload, observation=None), dict(payload, occurred_at_ms=-1),
                     dict(payload, review_until_ms=payload["occurred_at_ms"]), dict(payload, problems=["bad\nheader"])]
        missing = dict(payload)
        missing.pop("identity")
        mutations.append(missing)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(NotificationError):
                AlertEvent.from_json(json.dumps(value))
        with self.assertRaises(NotificationError):
            AlertEvent.from_json('{"schema_version":1,"schema_version":1}')

    def test_schema_corruption_and_wrong_directory_fail_closed(self):
        with self.alerts.store.connection() as db:
            db.execute("CREATE TABLE foreign_table (value TEXT)")
        with self.assertRaises(NotificationError):
            self.alerts.store.status()

    def test_review_policy_change_is_explicitly_rejected(self):
        with self.assertRaises(NotificationError):
            EmailAlerts(self.data_dir, review_seconds=60).initialize()

    def test_manual_retry_requires_known_failure_and_keeps_attempt_count(self):
        self.publish()
        self.alerts.collect()
        self.transport.send.return_value = SendResult(DeliveryState.FAILED, "smtp_authentication_failed")
        self.alerts.deliver(self.transport)
        self.alerts.store.retry_failed(self.entry_id(), now_ms=self.clock.value)
        self.transport.send.return_value = SendResult(DeliveryState.CONFIRMED, "smtp_accepted")
        self.alerts.deliver(self.transport)
        self.assertEqual(("confirmed", 2), (self.records()[0]["state"], self.records()[0]["attempts"]))
        with self.assertRaises(NotificationError):
            self.alerts.store.retry_failed(self.entry_id(), now_ms=self.clock.value)

    def test_unknown_cannot_use_known_failure_retry_shortcut(self):
        self.publish()
        self.alerts.collect()
        self.transport.send.return_value = SendResult(DeliveryState.UNKNOWN, "smtp_result_unknown")
        self.alerts.deliver(self.transport)
        with self.assertRaises(NotificationError):
            self.alerts.store.retry_failed(self.entry_id(), now_ms=self.clock.value)

    def test_resolving_unknown_failed_does_not_implicitly_confirm_cause_fixed(self):
        self.publish()
        self.alerts.collect()
        self.transport.send.return_value = SendResult(DeliveryState.UNKNOWN, "smtp_result_unknown")
        self.alerts.deliver(self.transport)
        event_id = self.entry_id()
        self.alerts.store.resolve(event_id, outcome=DeliveryState.FAILED, now_ms=self.clock.value)
        self.clock.value += 60_000
        self.assertEqual(0, self.alerts.deliver(self.transport))
        self.assertFalse(self.records()[0]["retryable"])
        self.alerts.store.retry_failed(event_id, now_ms=self.clock.value)
        self.transport.send.return_value = SendResult(DeliveryState.CONFIRMED, "smtp_accepted")
        self.assertEqual(1, self.alerts.deliver(self.transport))
        self.assertEqual("confirmed", self.records()[0]["state"])

    def test_normal_writer_marker_overlap_defers_without_false_withdrawal(self):
        self.publish()
        self.alerts.collect()
        self.alerts.deliver(self.transport)
        original_id = self.entry_id()
        self.clock.value += 1
        cycle = self.publish(append=False)
        original_marker = self.alerts.observations._write_invalid_marker
        def while_appending(*args, **kwargs):
            original_marker(*args, **kwargs)
            output = io.StringIO()
            self.assertEqual(2, main(["run", "--once", "--data-dir", str(self.data_dir)], stdout=output,
                                    factory=Mock(return_value=self.alerts)))
            self.assertEqual("observation_append_pending_retry", json.loads(output.getvalue())["error_code"])
            self.assertEqual([], self.records(AlertKind.SIGNAL_INVALIDATED))
            self.assertIsNone(self.alerts.store.status(event_id=original_id)["records"][0]["suppressed_reason"])
        with patch.object(self.alerts.observations, "_write_invalid_marker", side_effect=while_appending):
            self.alerts.observations.append_cycle(cycle)
        self.alerts.collect()
        self.assertEqual([original_id], [row["event_id"] for row in self.records(AlertKind.ENTRY_REVIEW)])
        self.assertEqual([], self.records(AlertKind.SIGNAL_INVALIDATED))
        self.assertIsNone(self.alerts.store.status()["log_unavailable_since_ms"])

    def test_persistent_log_error_eventually_invalidates_and_survives_notifier_restart(self):
        self.publish()
        self.alerts.collect()
        self.alerts.deliver(self.transport)
        self.assertFalse(self.alerts.observation_unavailable())
        self.alerts = EmailAlerts(self.data_dir, clock=self.clock, health=self.health)
        self.alerts.initialize()
        self.clock.value += 30_000
        self.assertTrue(self.alerts.observation_unavailable())
        self.assertEqual(1, len(self.records(AlertKind.SIGNAL_INVALIDATED)))
        self.assertEqual(1, len(self.records(AlertKind.SERVICE_FAULT)))

    def test_log_permission_error_uses_source_retry_and_persistent_fault_path(self):
        self.publish()
        self.alerts.collect()
        self.alerts.deliver(self.transport)
        original_open = Path.open
        def log_denied(path, *args, **kwargs):
            if path == self.alerts.observations.path:
                raise PermissionError("private diagnostic must not appear")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", new=log_denied):
            for delay, code in ((0, "observation_append_pending_retry"), (30_000, "observation_source_unavailable")):
                self.clock.value += delay
                output = io.StringIO()
                self.assertEqual(2, main(["run", "--once", "--data-dir", str(self.data_dir)], stdout=output,
                                        factory=Mock(return_value=self.alerts)))
                self.assertEqual(code, json.loads(output.getvalue())["error_code"])
                self.assertNotIn("private diagnostic", output.getvalue())
        self.assertEqual(1, len(self.records(AlertKind.SIGNAL_INVALIDATED)))
        self.assertEqual(1, len(self.records(AlertKind.SERVICE_FAULT)))

    def test_log_seek_and_read_io_errors_are_normalized(self):
        self.publish()
        _, cursor = self.alerts.observations.read_batch()
        for operation in ("seek", "readline"):
            with self.subTest(operation=operation):
                handle = Mock()
                handle.__enter__ = Mock(return_value=handle)
                handle.__exit__ = Mock(return_value=False)
                getattr(handle, operation).side_effect = OSError("device unavailable")
                with patch.object(Path, "open", return_value=handle):
                    with self.assertRaises(ObservationCorruptionError):
                        self.alerts.observations.read_batch(offset=cursor[0], anchor_start=cursor[1], anchor_sha256=cursor[2])

    def test_conflicting_equal_timestamp_observation_rolls_back_cursor(self):
        self.publish()
        self.alerts.collect()
        self.publish("wait")
        with self.assertRaises(NotificationError):
            self.alerts.collect()
        self.assertEqual(1, len(self.records()))

    def test_database_directory_identity_and_event_payload_are_validated(self):
        self.publish()
        self.alerts.collect()
        with self.alerts.store.connection() as db:
            db.execute("UPDATE metadata SET value=? WHERE name='data_dir'", (json.dumps("other-directory"),))
        with self.assertRaises(NotificationError):
            self.alerts.store.status()
        with self.alerts.store.connection() as db:
            self.alerts.store.set_meta(db, "data_dir", str(self.data_dir.resolve()))
            db.execute("UPDATE outbox SET payload='{}'")
        with self.assertRaises(NotificationError):
            self.alerts.store.status()

    def test_duplicate_json_fields_and_missing_log_at_cursor_are_rejected(self):
        self.publish()
        path = self.alerts.observations.path
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace('"schema_version":1', '"schema_version":1,"schema_version":1', 1), encoding="utf-8")
        with self.assertRaises(ObservationCorruptionError):
            self.alerts.collect()
        path.write_text(original, encoding="utf-8")
        self.alerts.collect()
        path.unlink()
        with self.assertRaises(ObservationCorruptionError):
            self.alerts.collect()

    def test_source_history_disappearance_fails_closed(self):
        self.publish()
        self.alerts.collect()
        self.state = None
        with self.assertRaises(HealthStateError):
            self.alerts.collect()

    def test_email_template_retains_evidence_and_does_not_embed_diagnostics(self):
        self.publish()
        self.alerts.collect()
        with self.alerts.store.connection() as db:
            event = self.alerts.store.record(db, self.entry_id()).event
        message = render_message(event, CONFIG)
        body = message.get_content()
        for text in ("trusted-run", "策略代码版本: unknown", "不是交易所保护单", "实际成交需人工记录", "本提醒人工复核截止", event.event_id):
            self.assertIn(text, body)
        self.assertNotIn("typed scan result", body)
        self.assertIsNone(message.get("Bcc"))
        self.assertEqual("text/plain", message.get_content_type())

    def test_smtp_checks_expiry_after_slow_login_before_data(self):
        self.publish()
        self.alerts.collect()
        with self.alerts.store.connection() as db:
            event = self.alerts.store.record(db, self.entry_id()).event
        client = Mock(send_message=Mock(return_value={}))
        client.login.side_effect = lambda *args: setattr(self.clock, "value", event.review_until_ms)
        result = SmtpTransport(CONFIG, factory=Mock(return_value=client), clock=self.clock).send(event)
        self.assertEqual((DeliveryState.FAILED, "entry_review_expired"), (result.state, result.code))
        client.send_message.assert_not_called()

    def test_signal_service_committed_cycle_to_fake_smtp_end_to_end(self):
        cycle = self.publish(append=False)
        alerts = EmailAlerts(self.data_dir, clock=self.clock)
        alerts.initialize()
        refresh = {"run_id": "trusted-run", "attempt_status": "success", "snapshot_usability": "usable", "usable": True}
        scan = {"schema_version": 1, "scan": ScanHealth(StepStatus.SUCCEEDED, cycle, StepStatus.SUCCEEDED).to_dict()}
        def run(argv, *, timeout):
            if "scan-once" in argv:
                alerts.observations.append_cycle(cycle)
                return subprocess.CompletedProcess(argv, 0, json.dumps(scan), "")
            return subprocess.CompletedProcess(argv, 0, json.dumps(refresh), "")
        client = Mock(send_message=Mock(return_value={}))
        transport = SmtpTransport(CONFIG, factory=Mock(return_value=client), clock=self.clock)
        def on_cycle(view):
            self.assertTrue(view["healthy"])
            self.assertTrue(alerts.collect()["caught_up"])
            self.assertEqual(1, alerts.deliver(transport))
        SignalService(ServiceConfig(data_dir=self.data_dir), clock=self.clock,
                      processes=Mock(run=Mock(side_effect=run))).run(cycles=1, on_cycle=on_cycle)
        self.assertEqual(1, client.send_message.call_count)
        self.assertEqual("confirmed", alerts.store.status()["records"][0]["state"])
        alerts.collect()
        self.assertEqual(1, len([row for row in alerts.store.status()["records"] if row["kind"] == "service_fault"]))

    def test_bounded_log_cursor_detects_truncation_replacement_and_invalid_marker(self):
        first = self.publish()
        records, cursor = self.alerts.observations.read_batch(limit=1)
        self.assertEqual((first,), records)
        self.clock.value += 1
        second = self.publish("wait")
        records, _ = self.alerts.observations.read_batch(offset=cursor[0], anchor_start=cursor[1], anchor_sha256=cursor[2])
        self.assertEqual((second,), records)
        path = self.alerts.observations.path
        original = path.read_bytes()
        path.write_bytes(b"{}\n")
        with self.assertRaises(ObservationCorruptionError):
            self.alerts.observations.read_batch(offset=cursor[0], anchor_start=cursor[1], anchor_sha256=cursor[2])
        path.write_bytes(original)
        self.alerts.observations.invalid_marker_path.write_text("uncommitted", encoding="utf-8")
        with self.assertRaises(ObservationCorruptionError):
            self.alerts.observations.read_batch()

    def test_incomplete_or_malformed_observation_record_is_not_skipped(self):
        path = self.alerts.observations.path
        for raw in (b"{}", b"{}\n", b"\xff\n"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(ObservationCorruptionError):
                    self.alerts.collect()
                self.assertEqual([], self.records())


class SmtpAndCommandTests(unittest.TestCase):
    def event(self):
        return AlertEvent(AlertKind.SERVICE_FAULT, "a" * 64, 1000, None, "health_event", problems=("refresh.failed",))

    def client(self):
        return Mock(send_message=Mock(return_value={}))

    def test_ssl_authentication_and_single_recipient_with_fixed_message_id(self):
        client = self.client()
        factory = Mock(return_value=client)
        result = SmtpTransport(CONFIG, factory=factory).send(self.event())
        self.assertEqual(DeliveryState.CONFIRMED, result.state)
        self.assertEqual(("smtp.126.com", 465), factory.call_args.args)
        self.assertTrue(factory.call_args.kwargs["context"].check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, factory.call_args.kwargs["context"].verify_mode)
        self.assertEqual([CONFIG.recipient], client.send_message.call_args.kwargs["to_addrs"])
        message = client.send_message.call_args.args[0]
        self.assertIn(self.event().event_id, message["Message-ID"])
        self.assertEqual("text/plain", message.get_content_type())
        self.assertNotIn(CONFIG.authorization_code, message.as_string())

    def test_transport_failure_matrix_has_no_raw_server_diagnostics(self):
        cases = [
            ("connect", TimeoutError("secret@example.test"), DeliveryState.FAILED, True),
            ("login", smtplib.SMTPAuthenticationError(535, b"secret"), DeliveryState.FAILED, False),
            ("send", smtplib.SMTPRecipientsRefused({CONFIG.recipient: (550, b"secret")}), DeliveryState.FAILED, False),
            ("send", smtplib.SMTPDataError(451, b"secret"), DeliveryState.FAILED, True),
            ("send", TimeoutError("secret"), DeliveryState.UNKNOWN, False),
            ("send", smtplib.SMTPServerDisconnected("secret"), DeliveryState.UNKNOWN, False),
        ]
        for phase, error, expected, retryable in cases:
            with self.subTest(phase=phase, error=type(error).__name__):
                client = self.client()
                factory = Mock(return_value=client)
                if phase == "connect":
                    factory.side_effect = error
                else:
                    (client.login if phase == "login" else client.send_message).side_effect = error
                result = SmtpTransport(CONFIG, factory=factory).send(self.event())
                self.assertEqual((expected, retryable), (result.state, result.retryable))
                self.assertNotIn("secret", repr(result))

    def test_close_error_after_acceptance_does_not_downgrade_delivery(self):
        client = self.client()
        client.close.side_effect = OSError("close")
        self.assertEqual(DeliveryState.CONFIRMED, SmtpTransport(CONFIG, factory=Mock(return_value=client)).send(self.event()).state)

    def test_missing_credentials_and_header_injection_rejected(self):
        with self.assertRaises(NotificationError):
            SmtpConfig.from_environment({})
        for address in ("reader@example.test\r\nBcc: other@example.test", "Display <reader@example.test>", "one@example.test,two@example.test"):
            with self.subTest(address=address), self.assertRaises(NotificationError):
                SmtpConfig(CONFIG.host, CONFIG.sender, address, "fake")
        self.assertNotIn(CONFIG.authorization_code, repr(CONFIG))

    def test_status_is_read_only_and_does_not_initialize(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "live"
            output = io.StringIO()
            self.assertEqual(0, main(["status", "--data-dir", str(data)], stdout=output))
            self.assertFalse(json.loads(output.getvalue())["initialized"])
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_default_dry_run_does_not_read_smtp_environment_or_create_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = Mock()
            environment = Mock(side_effect=AssertionError("must not access credentials"))
            self.assertEqual(0, main(["run", "--once", "--data-dir", str(Path(directory) / "live")], stdout=io.StringIO(),
                                    environment=environment, transport_factory=factory))
            factory.assert_not_called()

    def test_send_configuration_fails_before_any_persistent_state(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            code = main(["run", "--once", "--send", "--data-dir", str(Path(directory) / "live")], stdout=output, environment={})
            self.assertEqual(2, code)
            self.assertIn("MU_SMTP_AUTHORIZATION_CODE", output.getvalue())
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_unstable_snapshot_retries_without_creating_false_stop_event(self):
        alerts = Mock()
        alerts.collect.side_effect = HealthSnapshotUnstableError("unstable")
        output = io.StringIO()
        self.assertEqual(2, main(["run", "--once"], stdout=output, factory=Mock(return_value=alerts)))
        self.assertEqual("health_snapshot_unstable", json.loads(output.getvalue())["error_code"])
        alerts.source_unavailable.assert_not_called()
        alerts.deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
