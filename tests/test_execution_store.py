import json
import inspect
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.execution import OKXInstrumentSpec, OrderIntentFactory
from mu_strategy.execution import audit as audit_module
from mu_strategy.execution import store as store_module
from mu_strategy.execution.audit import (
    ActorKind,
    AuditEventType,
    ExecutionAuditEvent,
)
from mu_strategy.execution.intents import ExecutionEnvironment
from mu_strategy.execution.store import (
    ExecutionStoreConflictError,
    ExecutionStoreInvariantError,
    ExecutionStoreSchemaError,
    MutationOperation,
    ReservationState,
    SQLiteExecutionStore,
    cancel_mutation_action_id,
    idempotency_key_for,
    leverage_mutation_action_id,
    okx_client_order_id_for,
    submit_mutation_action_id,
)
from tests.test_order_intents import _factory_for, _observation, _release


class ExecutionIdentityContractTests(unittest.TestCase):
    def setUp(self):
        release = _release()
        factory, _ = _factory_for(release)
        self.intent = factory.create_demo_intent(
            observation=_observation(),
            strategy_release_id=release.strategy_release_id,
            instrument_spec=OKXInstrumentSpec(
                inst_id="MU-USDT-SWAP",
                tick_size=Decimal("0.1"),
                lot_size=Decimal("0.01"),
                contract_value=Decimal("1"),
            ),
            notional_usdt="10",
            created_at_ms=1_700_000_061_000,
        )

    def test_mutation_identities_are_operation_and_environment_specific(self):
        submit_action = submit_mutation_action_id(self.intent)
        leverage_action = leverage_mutation_action_id(self.intent)
        cancel_action = cancel_mutation_action_id(
            self.intent,
            order_lineage_id="ord1_" + "a" * 64,
            reason_code="operator_request",
            policy_version="cancel-v1",
        )

        self.assertEqual(self.intent.business_action_id, submit_action)
        self.assertRegex(leverage_action, r"^ma1_[0-9a-f]{64}$")
        self.assertRegex(cancel_action, r"^ca1_[0-9a-f]{64}$")
        self.assertEqual(len({submit_action, leverage_action, cancel_action}), 3)

        demo_key = idempotency_key_for(
            ExecutionEnvironment.DEMO,
            MutationOperation.SUBMIT_ENTRY,
            submit_action,
        )
        production_key = idempotency_key_for(
            ExecutionEnvironment.PRODUCTION,
            MutationOperation.SUBMIT_ENTRY,
            submit_action,
        )
        leverage_key = idempotency_key_for(
            ExecutionEnvironment.DEMO,
            MutationOperation.CONFIGURE_LEVERAGE,
            leverage_action,
        )
        self.assertRegex(demo_key, r"^[0-9a-f]{64}$")
        self.assertEqual(3, len({demo_key, production_key, leverage_key}))
        self.assertRegex(okx_client_order_id_for(demo_key), r"^OD[0-9A-F]{20}$")

    def test_execution_package_exports_transactional_store_contract(self):
        from mu_strategy.execution import (
            ActorKind as ExportedActorKind,
            AuditEventType as ExportedAuditEventType,
            ExecutionAuditEvent as ExportedExecutionAuditEvent,
            MutationOperation as ExportedMutationOperation,
            ReservationState as ExportedReservationState,
            SQLiteExecutionStore as ExportedSQLiteExecutionStore,
        )

        self.assertIs(ActorKind, ExportedActorKind)
        self.assertIs(AuditEventType, ExportedAuditEventType)
        self.assertIs(ExecutionAuditEvent, ExportedExecutionAuditEvent)
        self.assertIs(MutationOperation, ExportedMutationOperation)
        self.assertIs(ReservationState, ExportedReservationState)
        self.assertIs(SQLiteExecutionStore, ExportedSQLiteExecutionStore)

    def test_audit_event_round_trip_is_closed_and_canonical(self):
        event = ExecutionAuditEvent(
            schema_version=1,
            event_id="evt-1",
            event_type=AuditEventType.INTENT_CREATED,
            sequence=1,
            occurred_at_ms=1_700_000_061_100,
            audit_correlation_id=self.intent.audit_correlation_id,
            actor_kind=ActorKind.SYSTEM,
            actor_id="stage1-store",
            source_scan_id=self.intent.source_observation_id,
            signal_lineage_id=self.intent.signal_lineage_id,
            business_action_id=self.intent.business_action_id,
            environment=self.intent.environment,
            intent_id=self.intent.intent_id,
            intent_fingerprint=self.intent.intent_fingerprint,
            decision_code=self.intent.decision_code,
            payload={"intent": self.intent.to_dict()},
        )

        restored = ExecutionAuditEvent.from_json(event.to_json())
        self.assertEqual(event, restored)
        self.assertEqual(event.to_json(), restored.to_json())
        with self.assertRaises(TypeError):
            event.payload["intent"]["symbol"] = "BTC-USDT-SWAP"

        unknown = json.loads(event.to_json())
        unknown["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "fields"):
            ExecutionAuditEvent.from_dict(unknown)

        wrong_payload = json.loads(event.to_json())
        wrong_payload["payload"]["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "payload"):
            ExecutionAuditEvent.from_dict(wrong_payload)

        with self.assertRaisesRegex(ValueError, "sequence"):
            replace(event, sequence=0)

        with self.assertRaisesRegex(ValueError, "finite JSON"):
            replace(
                event,
                event_type=AuditEventType.INTENT_REUSED,
                payload={
                    "existing_intent_id": self.intent.intent_id,
                    "existing_intent_fingerprint": self.intent.intent_fingerprint,
                    "duplicate_source_scan_id": float("nan"),
                },
            )

        with self.assertRaisesRegex(ValueError, "operation"):
            replace(
                event,
                event_type=AuditEventType.IDEMPOTENCY_RESERVED,
                payload={
                    "operation": "unknown_operation",
                    "mutation_action_id": self.intent.business_action_id,
                    "idempotency_key": "a" * 64,
                    "order_lineage_id": "ord1_" + "b" * 64,
                    "client_order_id": None,
                    "selected_intent_fingerprint": self.intent.intent_fingerprint,
                    "cancel_reason_code": None,
                    "cancel_policy_version": None,
                },
            )

        with self.assertRaisesRegex(ValueError, "state"):
            replace(
                event,
                event_type=AuditEventType.RESERVATION_STATE_CHANGED,
                payload={
                    "operation": MutationOperation.SUBMIT_ENTRY.value,
                    "mutation_action_id": self.intent.business_action_id,
                    "idempotency_key": idempotency_key_for(
                        self.intent.environment,
                        MutationOperation.SUBMIT_ENTRY,
                        self.intent.business_action_id,
                    ),
                    "old_state": "invented",
                    "new_state": ReservationState.UNKNOWN.value,
                },
            )

        submit_key = idempotency_key_for(
            self.intent.environment,
            MutationOperation.SUBMIT_ENTRY,
            self.intent.business_action_id,
        )
        with self.assertRaisesRegex(ValueError, "client_order_id"):
            replace(
                event,
                event_type=AuditEventType.IDEMPOTENCY_RESERVED,
                payload={
                    "operation": MutationOperation.SUBMIT_ENTRY.value,
                    "mutation_action_id": self.intent.business_action_id,
                    "idempotency_key": submit_key,
                    "order_lineage_id": "ord1_" + "b" * 64,
                    "client_order_id": None,
                    "selected_intent_fingerprint": self.intent.intent_fingerprint,
                    "cancel_reason_code": None,
                    "cancel_policy_version": None,
                },
            )

        with self.assertRaisesRegex(ValueError, "transition"):
            replace(
                event,
                event_type=AuditEventType.RESERVATION_STATE_CHANGED,
                payload={
                    "operation": MutationOperation.SUBMIT_ENTRY.value,
                    "mutation_action_id": self.intent.business_action_id,
                    "idempotency_key": submit_key,
                    "old_state": ReservationState.ACKNOWLEDGED.value,
                    "new_state": ReservationState.RESERVED.value,
                },
            )


class _SQLiteExecutionFixture:
    def setUp(self):
        self.release = _release()
        self.factory, _ = _factory_for(self.release)
        self.instrument = OKXInstrumentSpec(
            inst_id="MU-USDT-SWAP",
            tick_size=Decimal("0.1"),
            lot_size=Decimal("0.01"),
            contract_value=Decimal("1"),
        )
        self.intent = self._intent(_observation(), created_at_ms=1_700_000_061_000)

    def _intent(self, observation, *, created_at_ms):
        return self.factory.create_demo_intent(
            observation=observation,
            strategy_release_id=self.release.strategy_release_id,
            instrument_spec=self.instrument,
            notional_usdt="10",
            created_at_ms=created_at_ms,
        )


class SQLiteExecutionStoreIntentTests(_SQLiteExecutionFixture, unittest.TestCase):

    def test_create_reuse_supersede_and_restart_are_atomic_and_ordered(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = SQLiteExecutionStore(path)

            created = store.record_intent(
                self.intent,
                source_scan_id="scan-1",
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_100,
                event_id="evt-created",
            )
            self.assertEqual("create", created.action.value)

            reused = store.record_intent(
                self.intent,
                source_scan_id="scan-retry",
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_200,
                event_id="evt-reused",
            )
            self.assertEqual("reuse", reused.action.value)
            self.assertEqual(self.intent, reused.intent)

            revision = self._intent(
                _observation(
                    observation_id="obs-2",
                    run_id="e" * 32,
                    hashes=(("15m", "1" * 64), ("1h", "2" * 64), ("5m", "3" * 64)),
                ),
                created_at_ms=1_700_000_061_300,
            )
            superseded = store.record_intent(
                revision,
                source_scan_id="scan-2",
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_300,
                event_id="evt-superseded",
            )
            self.assertEqual("supersede", superseded.action.value)
            self.assertEqual(self.intent.intent_id, superseded.supersedes_intent_id)

            selection = store.load_action_selection(
                self.intent.environment,
                self.intent.business_action_id,
            )
            self.assertEqual(revision.intent_id, selection.selected_intent_id)
            self.assertEqual(revision.intent_fingerprint, selection.selected_intent_fingerprint)
            self.assertEqual(2, selection.selection_version)
            self.assertFalse(selection.mutation_reserved)
            self.assertEqual(self.intent, store.load_intent(self.intent.intent_id))
            self.assertEqual(revision, store.load_intent(revision.intent_id))

            events = store.read_events(self.intent.audit_correlation_id)
            self.assertEqual((1, 2, 3), tuple(event.sequence for event in events))
            self.assertEqual(
                (
                    AuditEventType.INTENT_CREATED,
                    AuditEventType.INTENT_REUSED,
                    AuditEventType.INTENT_SUPERSEDED,
                ),
                tuple(event.event_type for event in events),
            )

            restarted = SQLiteExecutionStore(path)
            self.assertEqual(selection, restarted.load_action_selection(
                self.intent.environment,
                self.intent.business_action_id,
            ))
            self.assertEqual(events, restarted.read_events(self.intent.audit_correlation_id))

    def test_event_failure_rolls_back_intent_and_selection_changes(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteExecutionStore(Path(tmp) / "execution.sqlite3")
            store.record_intent(
                self.intent,
                source_scan_id="scan-1",
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_100,
                event_id="evt-duplicate",
            )
            revision = self._intent(
                _observation(
                    observation_id="obs-2",
                    run_id="e" * 32,
                    hashes=(("15m", "1" * 64), ("1h", "2" * 64), ("5m", "3" * 64)),
                ),
                created_at_ms=1_700_000_061_300,
            )

            with self.assertRaisesRegex(ExecutionStoreConflictError, "event_id"):
                store.record_intent(
                    revision,
                    source_scan_id="scan-2",
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="stage1-store",
                    occurred_at_ms=1_700_000_061_300,
                    event_id="evt-duplicate",
                )

            selection = store.load_action_selection(
                self.intent.environment,
                self.intent.business_action_id,
            )
            self.assertEqual(self.intent.intent_id, selection.selected_intent_id)
            self.assertEqual(1, selection.selection_version)
            self.assertIsNone(store.load_intent(revision.intent_id))
            self.assertEqual(1, len(store.read_events(self.intent.audit_correlation_id)))

    def test_unsupported_schema_refuses_to_open(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA user_version = 99")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(ExecutionStoreSchemaError, "schema version"):
                SQLiteExecutionStore(path)

    def test_corrupt_current_schema_refuses_to_open(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            connection = sqlite3.connect(path)
            try:
                for table in ("intents", "action_selections", "reservations", "audit_events"):
                    connection.execute(f"CREATE TABLE {table} (wrong_column TEXT)")
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ExecutionStoreSchemaError, "schema definition"):
                SQLiteExecutionStore(path)

    def test_unknown_persistent_trigger_or_view_refuses_to_open(self):
        schema_objects = (
            "CREATE TRIGGER erase_audit AFTER INSERT ON audit_events "
            "BEGIN DELETE FROM audit_events WHERE event_id = NEW.event_id; END",
            "CREATE VIEW mutable_intent_ids AS SELECT intent_id FROM intents",
        )
        for index, statement in enumerate(schema_objects):
            with self.subTest(statement=statement), TemporaryDirectory() as tmp:
                path = Path(tmp) / f"execution-{index}.sqlite3"
                SQLiteExecutionStore(path)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(statement)
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaisesRegex(ExecutionStoreSchemaError, "schema objects"):
                    SQLiteExecutionStore(path)

    def test_corrupt_action_selection_binding_fails_closed(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = SQLiteExecutionStore(path)
            store.record_intent(
                self.intent,
                source_scan_id="scan-1",
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_100,
                event_id="evt-created",
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE action_selections SET selected_intent_fingerprint = ?",
                    ("b" * 64,),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ExecutionStoreInvariantError, "action selection"):
                store.load_action_selection(
                    self.intent.environment,
                    self.intent.business_action_id,
                )

    def test_missing_action_selection_with_existing_evidence_fails_closed(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = SQLiteExecutionStore(path)
            store.record_intent(
                self.intent,
                source_scan_id="scan-1",
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_100,
                event_id="evt-created",
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("DELETE FROM action_selections")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ExecutionStoreInvariantError, "without action selection"):
                store.record_intent(
                    self.intent,
                    source_scan_id="scan-retry",
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="stage1-store",
                    occurred_at_ms=1_700_000_061_200,
                    event_id="evt-retry",
                )
            self.assertEqual(1, len(store.read_events(self.intent.audit_correlation_id)))

class SQLiteExecutionReservationTests(_SQLiteExecutionFixture, unittest.TestCase):
    def test_submit_reservation_is_atomic_idempotent_and_restart_readable(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = SQLiteExecutionStore(path)
            self._record(store, self.intent, "evt-created")
            action_id = submit_mutation_action_id(self.intent)

            reservation = store.reserve_mutation(
                self.intent,
                operation=MutationOperation.SUBMIT_ENTRY,
                mutation_action_id=action_id,
                order_lineage_id="ord1_" + "a" * 64,
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_200,
                event_id="evt-submit-reserved",
            )
            self.assertEqual(ReservationState.RESERVED, reservation.state)
            self.assertEqual(
                okx_client_order_id_for(reservation.idempotency_key),
                reservation.client_order_id,
            )
            self.assertTrue(
                store.load_action_selection(
                    self.intent.environment,
                    self.intent.business_action_id,
                ).mutation_reserved
            )

            duplicate = store.reserve_mutation(
                self.intent,
                operation=MutationOperation.SUBMIT_ENTRY,
                mutation_action_id=action_id,
                order_lineage_id="ord1_" + "a" * 64,
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_300,
                event_id="evt-submit-duplicate",
            )
            self.assertEqual(reservation, duplicate)
            events = store.read_events(self.intent.audit_correlation_id)
            self.assertEqual(2, len(events))
            self.assertEqual(
                ("scan-recorded", "scan-recorded"),
                tuple(event.source_scan_id for event in events),
            )

            restarted = SQLiteExecutionStore(path)
            self.assertEqual(
                reservation,
                restarted.load_reservation(
                    self.intent.environment,
                    MutationOperation.SUBMIT_ENTRY,
                    action_id,
                ),
            )

            revision = self._revision("obs-after-reservation", "4", 1_700_000_061_400)
            with self.assertRaisesRegex(ExecutionStoreConflictError, "reservation"):
                restarted.record_intent(
                    revision,
                    source_scan_id="scan-after-reservation",
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="stage1-store",
                    occurred_at_ms=1_700_000_061_400,
                    event_id="evt-after-reservation",
                )

    def test_reservation_event_failure_rolls_back_fence_and_reservation(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteExecutionStore(Path(tmp) / "execution.sqlite3")
            self._record(store, self.intent, "evt-duplicate")
            action_id = submit_mutation_action_id(self.intent)

            with self.assertRaisesRegex(ExecutionStoreConflictError, "event_id"):
                store.reserve_mutation(
                    self.intent,
                    operation=MutationOperation.SUBMIT_ENTRY,
                    mutation_action_id=action_id,
                    order_lineage_id="ord1_" + "0" * 64,
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="stage1-store",
                    occurred_at_ms=1_700_000_061_200,
                    event_id="evt-duplicate",
                )

            self.assertFalse(
                store.load_action_selection(
                    self.intent.environment,
                    self.intent.business_action_id,
                ).mutation_reserved
            )
            self.assertIsNone(
                store.load_reservation(
                    self.intent.environment,
                    MutationOperation.SUBMIT_ENTRY,
                    action_id,
                )
            )
            self.assertEqual(1, len(store.read_events(self.intent.audit_correlation_id)))

    def test_not_sent_retry_reuses_same_reservation_key_after_restart(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = SQLiteExecutionStore(path)
            self._record(store, self.intent, "evt-created")
            action_id = leverage_mutation_action_id(self.intent)
            reserved = store.reserve_mutation(
                self.intent,
                operation=MutationOperation.CONFIGURE_LEVERAGE,
                mutation_action_id=action_id,
                order_lineage_id=self.intent.business_action_id,
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_200,
                event_id="evt-leverage-reserved",
            )
            store.transition_reservation(
                self.intent.environment,
                MutationOperation.CONFIGURE_LEVERAGE,
                action_id,
                expected_state=ReservationState.RESERVED,
                new_state=ReservationState.DISPATCHING,
                actor_kind=ActorKind.SYSTEM,
                actor_id="fake-adapter",
                occurred_at_ms=1_700_000_061_300,
                event_id="evt-leverage-dispatching",
            )
            store.transition_reservation(
                self.intent.environment,
                MutationOperation.CONFIGURE_LEVERAGE,
                action_id,
                expected_state=ReservationState.DISPATCHING,
                new_state=ReservationState.NOT_SENT_FAILED,
                actor_kind=ActorKind.SYSTEM,
                actor_id="fake-adapter",
                occurred_at_ms=1_700_000_061_400,
                event_id="evt-leverage-not-sent",
            )

            restarted = SQLiteExecutionStore(path)
            retry = restarted.transition_reservation(
                self.intent.environment,
                MutationOperation.CONFIGURE_LEVERAGE,
                action_id,
                expected_state=ReservationState.NOT_SENT_FAILED,
                new_state=ReservationState.RESERVED,
                actor_kind=ActorKind.SYSTEM,
                actor_id="retry-gate",
                occurred_at_ms=1_700_000_061_500,
                event_id="evt-leverage-retry",
            )
            duplicate = restarted.reserve_mutation(
                self.intent,
                operation=MutationOperation.CONFIGURE_LEVERAGE,
                mutation_action_id=action_id,
                order_lineage_id=self.intent.business_action_id,
                actor_kind=ActorKind.SYSTEM,
                actor_id="retry-gate",
                occurred_at_ms=1_700_000_061_600,
                event_id="evt-leverage-reserve-duplicate",
            )
            self.assertEqual(reserved.idempotency_key, retry.idempotency_key)
            self.assertEqual(retry, duplicate)
            self.assertEqual(
                {"scan-recorded"},
                {
                    event.source_scan_id
                    for event in restarted.read_events(self.intent.audit_correlation_id)
                },
            )
            self.assertEqual(ReservationState.RESERVED, duplicate.state)

    def test_supersession_and_first_reservation_race_has_exactly_one_winner(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            SQLiteExecutionStore(path)
            seed = SQLiteExecutionStore(path)
            self._record(seed, self.intent, "evt-created")
            revision = self._revision("obs-race", "5", 1_700_000_061_300)
            barrier = threading.Barrier(2)

            def supersede():
                barrier.wait()
                try:
                    SQLiteExecutionStore(path).record_intent(
                        revision,
                        source_scan_id="scan-race",
                        actor_kind=ActorKind.SYSTEM,
                        actor_id="race-supersede",
                        occurred_at_ms=1_700_000_061_300,
                        event_id="evt-race-supersede",
                    )
                    return "supersede"
                except ExecutionStoreConflictError:
                    return "conflict"

            def reserve():
                barrier.wait()
                try:
                    SQLiteExecutionStore(path).reserve_mutation(
                        self.intent,
                        operation=MutationOperation.SUBMIT_ENTRY,
                        mutation_action_id=submit_mutation_action_id(self.intent),
                        order_lineage_id="ord1_" + "b" * 64,
                        actor_kind=ActorKind.SYSTEM,
                        actor_id="race-reserve",
                        occurred_at_ms=1_700_000_061_300,
                        event_id="evt-race-reserve",
                    )
                    return "reserve"
                except ExecutionStoreConflictError:
                    return "conflict"

            with ThreadPoolExecutor(max_workers=2) as executor:
                supersede_future = executor.submit(supersede)
                reserve_future = executor.submit(reserve)
                results = {supersede_future.result(), reserve_future.result()}

            self.assertEqual(2, len(results))
            self.assertIn("conflict", results)
            self.assertTrue("supersede" in results or "reserve" in results)
            final = SQLiteExecutionStore(path).load_action_selection(
                self.intent.environment,
                self.intent.business_action_id,
            )
            if "reserve" in results:
                self.assertEqual(self.intent.intent_id, final.selected_intent_id)
                self.assertTrue(final.mutation_reserved)
            else:
                self.assertEqual(revision.intent_id, final.selected_intent_id)
                self.assertFalse(final.mutation_reserved)

    def test_three_revision_action_wide_reservation_blocks_stale_revision(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteExecutionStore(Path(tmp) / "execution.sqlite3")
            self._record(store, self.intent, "evt-created")
            revision_b = self._revision("obs-b", "6", 1_700_000_061_300)
            store.record_intent(
                revision_b,
                source_scan_id="scan-b",
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_300,
                event_id="evt-b",
            )
            store.reserve_mutation(
                revision_b,
                operation=MutationOperation.SUBMIT_ENTRY,
                mutation_action_id=submit_mutation_action_id(revision_b),
                order_lineage_id="ord1_" + "c" * 64,
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_400,
                event_id="evt-b-reserved",
            )
            revision_c = self._revision("obs-c", "7", 1_700_000_061_500)

            for stale in (self.intent, revision_c):
                with self.assertRaisesRegex(ExecutionStoreConflictError, "reservation"):
                    store.record_intent(
                        stale,
                        source_scan_id="scan-stale",
                        actor_kind=ActorKind.SYSTEM,
                        actor_id="stage1-store",
                        occurred_at_ms=1_700_000_061_500,
                        event_id="evt-stale-" + stale.intent_id[-8:],
                    )

            final = store.load_action_selection(
                self.intent.environment,
                self.intent.business_action_id,
            )
            self.assertEqual(revision_b.intent_id, final.selected_intent_id)
            self.assertTrue(final.mutation_reserved)

    def test_truncated_submit_client_id_collision_fails_closed(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteExecutionStore(Path(tmp) / "execution.sqlite3")
            self._record(store, self.intent, "evt-created-a")
            other = self._intent(
                _observation(
                    observation_id="obs-other-action",
                    signal_time_ms=1_700_000_900_000,
                    observed_at_ms=1_700_000_960_000,
                ),
                created_at_ms=1_700_000_961_000,
            )
            self._record(store, other, "evt-created-b")

            with patch(
                "mu_strategy.execution.store.okx_client_order_id_for",
                return_value="OD" + "A" * 20,
            ), patch(
                "mu_strategy.execution.audit.okx_client_order_id_for",
                return_value="OD" + "A" * 20,
            ):
                store.reserve_mutation(
                    self.intent,
                    operation=MutationOperation.SUBMIT_ENTRY,
                    mutation_action_id=submit_mutation_action_id(self.intent),
                    order_lineage_id="ord1_" + "d" * 64,
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="stage1-store",
                    occurred_at_ms=1_700_000_061_200,
                    event_id="evt-reserved-a",
                )
                with self.assertRaisesRegex(ExecutionStoreConflictError, "client order"):
                    store.reserve_mutation(
                        other,
                        operation=MutationOperation.SUBMIT_ENTRY,
                        mutation_action_id=submit_mutation_action_id(other),
                        order_lineage_id="ord1_" + "e" * 64,
                        actor_kind=ActorKind.SYSTEM,
                        actor_id="stage1-store",
                        occurred_at_ms=1_700_000_961_200,
                        event_id="evt-reserved-b",
                    )

    def test_operation_identity_mismatch_fails_before_reservation(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteExecutionStore(Path(tmp) / "execution.sqlite3")
            self._record(store, self.intent, "evt-created")
            with self.assertRaisesRegex(ExecutionStoreConflictError, "mutation_action_id"):
                store.reserve_mutation(
                    self.intent,
                    operation=MutationOperation.SUBMIT_ENTRY,
                    mutation_action_id=leverage_mutation_action_id(self.intent),
                    order_lineage_id="ord1_" + "9" * 64,
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="stage1-store",
                    occurred_at_ms=1_700_000_061_200,
                    event_id="evt-wrong-operation",
                )
            self.assertFalse(
                store.load_action_selection(
                    self.intent.environment,
                    self.intent.business_action_id,
                ).mutation_reserved
            )
            self.assertEqual(1, len(store.read_events(self.intent.audit_correlation_id)))

    def test_sequential_cancel_rules_and_same_key_retry_survive_restart(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = SQLiteExecutionStore(path)
            self._record(store, self.intent, "evt-created")
            lineage = "ord1_" + "f" * 64
            first_action = cancel_mutation_action_id(
                self.intent,
                order_lineage_id=lineage,
                reason_code="operator_request",
                policy_version="cancel-v1",
            )
            first = store.reserve_mutation(
                self.intent,
                operation=MutationOperation.CANCEL_ORDER,
                mutation_action_id=first_action,
                order_lineage_id=lineage,
                cancel_reason_code="operator_request",
                cancel_policy_version="cancel-v1",
                actor_kind=ActorKind.OPERATOR,
                actor_id="operator-1",
                occurred_at_ms=1_700_000_061_200,
                event_id="evt-cancel-1",
            )
            same = store.reserve_mutation(
                self.intent,
                operation=MutationOperation.CANCEL_ORDER,
                mutation_action_id=first_action,
                order_lineage_id=lineage,
                cancel_reason_code="operator_request",
                cancel_policy_version="cancel-v1",
                actor_kind=ActorKind.OPERATOR,
                actor_id="operator-1",
                occurred_at_ms=1_700_000_061_300,
                event_id="evt-cancel-1-duplicate",
            )
            self.assertEqual(first, same)

            second_action = cancel_mutation_action_id(
                self.intent,
                order_lineage_id=lineage,
                reason_code="risk_stop",
                policy_version="cancel-v2",
            )
            with self.assertRaisesRegex(ExecutionStoreConflictError, "cancel"):
                store.reserve_mutation(
                    self.intent,
                    operation=MutationOperation.CANCEL_ORDER,
                    mutation_action_id=second_action,
                    order_lineage_id=lineage,
                    cancel_reason_code="risk_stop",
                    cancel_policy_version="cancel-v2",
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="risk-gate",
                    occurred_at_ms=1_700_000_061_400,
                    event_id="evt-cancel-2-early",
                )

            dispatching = store.transition_reservation(
                self.intent.environment,
                MutationOperation.CANCEL_ORDER,
                first_action,
                expected_state=ReservationState.RESERVED,
                new_state=ReservationState.DISPATCHING,
                actor_kind=ActorKind.SYSTEM,
                actor_id="demo-adapter",
                occurred_at_ms=1_700_000_061_500,
                event_id="evt-cancel-1-dispatching",
            )
            self.assertEqual(ReservationState.DISPATCHING, dispatching.state)
            rejected = store.transition_reservation(
                self.intent.environment,
                MutationOperation.CANCEL_ORDER,
                first_action,
                expected_state=ReservationState.DISPATCHING,
                new_state=ReservationState.REJECTED,
                actor_kind=ActorKind.SYSTEM,
                actor_id="demo-adapter",
                occurred_at_ms=1_700_000_061_600,
                event_id="evt-cancel-1-rejected",
            )
            self.assertEqual(ReservationState.REJECTED, rejected.state)

            second = store.reserve_mutation(
                self.intent,
                operation=MutationOperation.CANCEL_ORDER,
                mutation_action_id=second_action,
                order_lineage_id=lineage,
                cancel_reason_code="risk_stop",
                cancel_policy_version="cancel-v2",
                actor_kind=ActorKind.SYSTEM,
                actor_id="risk-gate",
                occurred_at_ms=1_700_000_061_700,
                event_id="evt-cancel-2",
            )
            store.transition_reservation(
                self.intent.environment,
                MutationOperation.CANCEL_ORDER,
                second_action,
                expected_state=ReservationState.RESERVED,
                new_state=ReservationState.DISPATCHING,
                actor_kind=ActorKind.SYSTEM,
                actor_id="demo-adapter",
                occurred_at_ms=1_700_000_061_800,
                event_id="evt-cancel-2-dispatching",
            )
            unknown = store.transition_reservation(
                self.intent.environment,
                MutationOperation.CANCEL_ORDER,
                second_action,
                expected_state=ReservationState.DISPATCHING,
                new_state=ReservationState.UNKNOWN,
                actor_kind=ActorKind.SYSTEM,
                actor_id="demo-adapter",
                occurred_at_ms=1_700_000_061_900,
                event_id="evt-cancel-2-unknown",
            )
            self.assertEqual(ReservationState.UNKNOWN, unknown.state)

            third_action = cancel_mutation_action_id(
                self.intent,
                order_lineage_id=lineage,
                reason_code="signal_expired",
                policy_version="cancel-v3",
            )
            with self.assertRaisesRegex(ExecutionStoreConflictError, "cancel"):
                store.reserve_mutation(
                    self.intent,
                    operation=MutationOperation.CANCEL_ORDER,
                    mutation_action_id=third_action,
                    order_lineage_id=lineage,
                    cancel_reason_code="signal_expired",
                    cancel_policy_version="cancel-v3",
                    actor_kind=ActorKind.SYSTEM,
                    actor_id="expiry-gate",
                    occurred_at_ms=1_700_000_062_000,
                    event_id="evt-cancel-3",
                )

            restarted = SQLiteExecutionStore(path)
            self.assertEqual(
                unknown,
                restarted.load_reservation(
                    self.intent.environment,
                    MutationOperation.CANCEL_ORDER,
                    second_action,
                ),
            )
            self.assertEqual(second.idempotency_key, unknown.idempotency_key)

    def test_corrupt_persisted_intent_fails_closed(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = SQLiteExecutionStore(path)
            self._record(store, self.intent, "evt-created")
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE intents SET intent_json = ? WHERE intent_id = ?",
                    ('{"schema_version":1}', self.intent.intent_id),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ExecutionStoreInvariantError, "corrupt"):
                store.load_intent(self.intent.intent_id)

    def test_cross_environment_reservation_corruption_fails_closed_after_restart(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            store = SQLiteExecutionStore(path)
            self._record(store, self.intent, "evt-created")
            action_id = leverage_mutation_action_id(self.intent)
            store.reserve_mutation(
                self.intent,
                operation=MutationOperation.CONFIGURE_LEVERAGE,
                mutation_action_id=action_id,
                order_lineage_id=self.intent.business_action_id,
                actor_kind=ActorKind.SYSTEM,
                actor_id="stage1-store",
                occurred_at_ms=1_700_000_061_200,
                event_id="evt-leverage-reserved",
            )
            production_key = idempotency_key_for(
                ExecutionEnvironment.PRODUCTION,
                MutationOperation.CONFIGURE_LEVERAGE,
                action_id,
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE reservations SET environment = ?, idempotency_key = ? "
                    "WHERE mutation_action_id = ?",
                    (ExecutionEnvironment.PRODUCTION.value, production_key, action_id),
                )
                connection.commit()
            finally:
                connection.close()

            restarted = SQLiteExecutionStore(path)
            with self.assertRaisesRegex(ExecutionStoreInvariantError, "environment"):
                restarted.load_reservation(
                    ExecutionEnvironment.PRODUCTION,
                    MutationOperation.CONFIGURE_LEVERAGE,
                    action_id,
                )

    def test_execution_store_has_no_broker_or_application_reachability(self):
        source = inspect.getsource(audit_module) + inspect.getsource(store_module)
        for forbidden in (
            "mu_strategy.live",
            "mu_strategy.demo_trading",
            "OKX_API_KEY",
            "OKX_SECRET_KEY",
            "submit_order(",
            "cancel_order(",
            "set_leverage(",
        ):
            self.assertNotIn(forbidden, source)

    def _record(self, store, intent, event_id):
        return store.record_intent(
            intent,
            source_scan_id="scan-recorded",
            actor_kind=ActorKind.SYSTEM,
            actor_id="stage1-store",
            occurred_at_ms=intent.created_at_ms,
            event_id=event_id,
        )

    def _revision(self, observation_id, digit, created_at_ms):
        return self._intent(
            _observation(
                observation_id=observation_id,
                run_id=digit * 32,
                hashes=(("15m", digit * 64), ("1h", chr(ord(digit) + 1) * 64), ("5m", chr(ord(digit) + 2) * 64)),
            ),
            created_at_ms=created_at_ms,
        )


if __name__ == "__main__":
    unittest.main()
