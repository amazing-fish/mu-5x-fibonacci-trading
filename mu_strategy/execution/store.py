from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

from mu_strategy.canonical import canonical_json, canonical_sha256
from mu_strategy.execution.audit import (
    ActorKind,
    AuditEventType,
    EXECUTION_AUDIT_SCHEMA_VERSION,
    ExecutionAuditEvent,
    MutationOperation,
    ReservationState,
    idempotency_key_for,
    is_reservation_transition_allowed,
    okx_client_order_id_for,
)
from mu_strategy.execution.intents import (
    ExecutionEnvironment,
    IntentRevisionAction,
    IntentRevisionPlan,
    OrderIntent,
    OrderIntentRevisionError,
    classify_intent_revision,
)


EXECUTION_STORE_SCHEMA_VERSION = 1


class ExecutionStoreError(RuntimeError):
    pass


class ExecutionStoreSchemaError(ExecutionStoreError):
    pass


class ExecutionStoreConflictError(ExecutionStoreError):
    pass


class ExecutionStoreInvariantError(ExecutionStoreError):
    pass


@dataclass(frozen=True)
class ActionSelection:
    environment: ExecutionEnvironment
    business_action_id: str
    selected_intent_id: str
    selected_intent_fingerprint: str
    selection_version: int
    mutation_reserved: bool


@dataclass(frozen=True)
class MutationReservation:
    environment: ExecutionEnvironment
    operation: MutationOperation
    mutation_action_id: str
    idempotency_key: str
    business_action_id: str
    selected_intent_id: str
    selected_intent_fingerprint: str
    order_lineage_id: str
    client_order_id: str | None
    cancel_reason_code: str | None
    cancel_policy_version: str | None
    state: ReservationState


class SQLiteExecutionStore:
    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5_000):
        self.path = Path(path)
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be a positive integer")
        self.busy_timeout_ms = busy_timeout_ms
        if not self.path.parent.exists() or not self.path.parent.is_dir():
            raise ExecutionStoreSchemaError("execution database parent directory must already exist")
        if self.path.exists() and not self.path.is_file():
            raise ExecutionStoreSchemaError("execution database path must be a regular file")
        self._initialize()

    def record_intent(
        self,
        intent: OrderIntent,
        *,
        source_scan_id: str,
        actor_kind: ActorKind,
        actor_id: str,
        occurred_at_ms: int,
        event_id: str,
    ) -> IntentRevisionPlan:
        intent = _canonical_intent(intent)
        _require_event_metadata(source_scan_id, actor_kind, actor_id, occurred_at_ms, event_id)
        try:
            with self._transaction() as connection:
                selection = self._load_action_selection_connection(
                    connection,
                    intent.environment,
                    intent.business_action_id,
                )
                if selection is None:
                    self._require_no_orphaned_action_evidence(
                        connection,
                        intent.environment,
                        intent.business_action_id,
                    )
                    self._insert_or_verify_intent(
                        connection,
                        intent,
                        source_scan_id=source_scan_id,
                    )
                    connection.execute(
                        """
                        INSERT INTO action_selections (
                            environment, business_action_id, selected_intent_id,
                            selected_intent_fingerprint, selection_version, mutation_reserved
                        ) VALUES (?, ?, ?, ?, 1, 0)
                        """,
                        (
                            intent.environment.value,
                            intent.business_action_id,
                            intent.intent_id,
                            intent.intent_fingerprint,
                        ),
                    )
                    plan = IntentRevisionPlan(IntentRevisionAction.CREATE, intent)
                    event_type = AuditEventType.INTENT_CREATED
                    payload = {"intent": intent.to_dict()}
                else:
                    existing = self._load_intent_connection(connection, selection.selected_intent_id)
                    if existing is None:
                        raise ExecutionStoreInvariantError("selected intent is missing")
                    try:
                        plan = classify_intent_revision(
                            existing,
                            intent,
                            mutation_reserved=selection.mutation_reserved,
                        )
                    except OrderIntentRevisionError as exc:
                        raise ExecutionStoreConflictError(str(exc)) from exc
                    if plan.action is IntentRevisionAction.CONFLICT:
                        raise ExecutionStoreConflictError(
                            "business action already has a mutation reservation"
                        )
                    if plan.action is IntentRevisionAction.REUSE:
                        event_type = AuditEventType.INTENT_REUSED
                        payload = {
                            "existing_intent_id": existing.intent_id,
                            "existing_intent_fingerprint": existing.intent_fingerprint,
                            "duplicate_source_scan_id": source_scan_id,
                        }
                        intent = existing
                    elif plan.action is IntentRevisionAction.SUPERSEDE:
                        self._insert_or_verify_intent(
                            connection,
                            intent,
                            source_scan_id=source_scan_id,
                        )
                        new_version = selection.selection_version + 1
                        updated = connection.execute(
                            """
                            UPDATE action_selections
                            SET selected_intent_id = ?, selected_intent_fingerprint = ?,
                                selection_version = ?
                            WHERE environment = ? AND business_action_id = ?
                              AND selection_version = ? AND mutation_reserved = 0
                            """,
                            (
                                intent.intent_id,
                                intent.intent_fingerprint,
                                new_version,
                                intent.environment.value,
                                intent.business_action_id,
                                selection.selection_version,
                            ),
                        )
                        if updated.rowcount != 1:
                            raise ExecutionStoreConflictError(
                                "intent supersession lost the action-selection fence"
                            )
                        event_type = AuditEventType.INTENT_SUPERSEDED
                        payload = {
                            "old_intent_id": existing.intent_id,
                            "new_intent_id": intent.intent_id,
                            "selection_version": new_version,
                        }
                    else:
                        raise ExecutionStoreInvariantError("unsupported intent revision action")
                self._append_event(
                    connection,
                    intent=intent,
                    source_scan_id=source_scan_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    occurred_at_ms=occurred_at_ms,
                    event_id=event_id,
                    event_type=event_type,
                    payload=payload,
                )
                return plan
        except sqlite3.IntegrityError as exc:
            raise ExecutionStoreConflictError(_integrity_error_message(exc)) from exc

    def load_intent(self, intent_id: str) -> OrderIntent | None:
        _require_token(intent_id, "intent_id")
        with self._connection() as connection:
            return self._load_intent_connection(connection, intent_id)

    def reserve_mutation(
        self,
        intent: OrderIntent,
        *,
        operation: MutationOperation,
        mutation_action_id: str,
        order_lineage_id: str,
        actor_kind: ActorKind,
        actor_id: str,
        occurred_at_ms: int,
        event_id: str,
        cancel_reason_code: str | None = None,
        cancel_policy_version: str | None = None,
    ) -> MutationReservation:
        intent = _canonical_intent(intent)
        if not isinstance(operation, MutationOperation):
            raise TypeError("operation must be a MutationOperation")
        _require_token(mutation_action_id, "mutation_action_id")
        _require_token(order_lineage_id, "order_lineage_id")
        _require_event_metadata(
            intent.source_observation_id,
            actor_kind,
            actor_id,
            occurred_at_ms,
            event_id,
        )
        _validate_mutation_action(
            intent,
            operation,
            mutation_action_id,
            order_lineage_id,
            cancel_reason_code,
            cancel_policy_version,
        )
        idempotency_key = idempotency_key_for(
            intent.environment,
            operation,
            mutation_action_id,
        )
        client_order_id = (
            okx_client_order_id_for(idempotency_key)
            if operation is MutationOperation.SUBMIT_ENTRY
            else None
        )
        reservation = MutationReservation(
            environment=intent.environment,
            operation=operation,
            mutation_action_id=mutation_action_id,
            idempotency_key=idempotency_key,
            business_action_id=intent.business_action_id,
            selected_intent_id=intent.intent_id,
            selected_intent_fingerprint=intent.intent_fingerprint,
            order_lineage_id=order_lineage_id,
            client_order_id=client_order_id,
            cancel_reason_code=cancel_reason_code,
            cancel_policy_version=cancel_policy_version,
            state=ReservationState.RESERVED,
        )
        try:
            with self._transaction() as connection:
                selection = self._load_action_selection_connection(
                    connection,
                    intent.environment,
                    intent.business_action_id,
                )
                if selection is None:
                    raise ExecutionStoreConflictError(
                        "mutation reservation requires a persisted action selection"
                    )
                if (
                    selection.selected_intent_id != intent.intent_id
                    or selection.selected_intent_fingerprint != intent.intent_fingerprint
                ):
                    raise ExecutionStoreConflictError(
                        "mutation reservation does not target the selected intent revision"
                    )
                stored_intent = self._load_intent_connection(connection, intent.intent_id)
                if stored_intent != intent:
                    raise ExecutionStoreInvariantError(
                        "selected intent bytes do not match reservation input"
                    )
                selected_source_scan_id = self._load_intent_source_scan_id_connection(
                    connection,
                    intent.intent_id,
                )
                existing = self._load_reservation_connection(
                    connection,
                    intent.environment,
                    operation,
                    mutation_action_id,
                )
                if existing is not None:
                    if _reservation_facts(existing) != _reservation_facts(reservation):
                        raise ExecutionStoreConflictError(
                            "mutation action reservation maps to different facts"
                        )
                    return existing
                key_row = connection.execute(
                    "SELECT * FROM reservations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if key_row is not None:
                    raise ExecutionStoreConflictError(
                        "idempotency key already maps to different reservation facts"
                    )
                if client_order_id is not None:
                    client_row = connection.execute(
                        "SELECT idempotency_key FROM reservations WHERE client_order_id = ?",
                        (client_order_id,),
                    ).fetchone()
                    if client_row is not None and client_row["idempotency_key"] != idempotency_key:
                        raise ExecutionStoreConflictError(
                            "client order ID collision maps to a different full idempotency key"
                        )
                if operation is MutationOperation.CANCEL_ORDER:
                    self._require_cancel_slot(
                        connection,
                        environment=intent.environment,
                        order_lineage_id=order_lineage_id,
                    )
                connection.execute(
                    """
                    INSERT INTO reservations (
                        environment, operation, mutation_action_id, idempotency_key,
                        business_action_id, selected_intent_id, selected_intent_fingerprint,
                        order_lineage_id, client_order_id, cancel_reason_code,
                        cancel_policy_version, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reservation.environment.value,
                        reservation.operation.value,
                        reservation.mutation_action_id,
                        reservation.idempotency_key,
                        reservation.business_action_id,
                        reservation.selected_intent_id,
                        reservation.selected_intent_fingerprint,
                        reservation.order_lineage_id,
                        reservation.client_order_id,
                        reservation.cancel_reason_code,
                        reservation.cancel_policy_version,
                        reservation.state.value,
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE action_selections
                    SET mutation_reserved = 1
                    WHERE environment = ? AND business_action_id = ?
                      AND selection_version = ?
                      AND selected_intent_id = ?
                      AND selected_intent_fingerprint = ?
                    """,
                    (
                        intent.environment.value,
                        intent.business_action_id,
                        selection.selection_version,
                        intent.intent_id,
                        intent.intent_fingerprint,
                    ),
                )
                if updated.rowcount != 1:
                    raise ExecutionStoreConflictError(
                        "mutation reservation lost the action-selection fence"
                    )
                self._append_event(
                    connection,
                    intent=intent,
                    source_scan_id=selected_source_scan_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    occurred_at_ms=occurred_at_ms,
                    event_id=event_id,
                    event_type=AuditEventType.IDEMPOTENCY_RESERVED,
                    payload={
                        "operation": operation.value,
                        "mutation_action_id": mutation_action_id,
                        "idempotency_key": idempotency_key,
                        "order_lineage_id": order_lineage_id,
                        "client_order_id": client_order_id,
                        "selected_intent_fingerprint": intent.intent_fingerprint,
                        "cancel_reason_code": cancel_reason_code,
                        "cancel_policy_version": cancel_policy_version,
                    },
                )
                return reservation
        except sqlite3.IntegrityError as exc:
            raise ExecutionStoreConflictError(_integrity_error_message(exc)) from exc

    def load_reservation(
        self,
        environment: ExecutionEnvironment,
        operation: MutationOperation,
        mutation_action_id: str,
    ) -> MutationReservation | None:
        if not isinstance(environment, ExecutionEnvironment):
            raise TypeError("environment must be an ExecutionEnvironment")
        if not isinstance(operation, MutationOperation):
            raise TypeError("operation must be a MutationOperation")
        _require_token(mutation_action_id, "mutation_action_id")
        with self._connection() as connection:
            return self._load_reservation_connection(
                connection,
                environment,
                operation,
                mutation_action_id,
            )

    def transition_reservation(
        self,
        environment: ExecutionEnvironment,
        operation: MutationOperation,
        mutation_action_id: str,
        *,
        expected_state: ReservationState,
        new_state: ReservationState,
        actor_kind: ActorKind,
        actor_id: str,
        occurred_at_ms: int,
        event_id: str,
    ) -> MutationReservation:
        if not isinstance(environment, ExecutionEnvironment):
            raise TypeError("environment must be an ExecutionEnvironment")
        if not isinstance(operation, MutationOperation):
            raise TypeError("operation must be a MutationOperation")
        if not isinstance(expected_state, ReservationState) or not isinstance(new_state, ReservationState):
            raise TypeError("reservation states must be ReservationState values")
        _require_token(mutation_action_id, "mutation_action_id")
        _require_event_metadata(
            "persisted-reservation",
            actor_kind,
            actor_id,
            occurred_at_ms,
            event_id,
        )
        if not is_reservation_transition_allowed(expected_state, new_state):
            raise ExecutionStoreConflictError(
                f"illegal reservation transition: {expected_state.value} -> {new_state.value}"
            )
        try:
            with self._transaction() as connection:
                reservation = self._load_reservation_connection(
                    connection,
                    environment,
                    operation,
                    mutation_action_id,
                )
                if reservation is None:
                    raise ExecutionStoreConflictError("reservation does not exist")
                if reservation.state is not expected_state:
                    raise ExecutionStoreConflictError(
                        "reservation state changed before the expected transition"
                    )
                intent = self._load_intent_connection(
                    connection,
                    reservation.selected_intent_id,
                )
                if intent is None:
                    raise ExecutionStoreInvariantError("reservation intent is missing")
                selected_source_scan_id = self._load_intent_source_scan_id_connection(
                    connection,
                    reservation.selected_intent_id,
                )
                updated = connection.execute(
                    """
                    UPDATE reservations SET state = ?
                    WHERE environment = ? AND operation = ? AND mutation_action_id = ?
                      AND state = ?
                    """,
                    (
                        new_state.value,
                        environment.value,
                        operation.value,
                        mutation_action_id,
                        expected_state.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise ExecutionStoreConflictError(
                        "reservation state transition lost its compare-and-swap"
                    )
                self._append_event(
                    connection,
                    intent=intent,
                    source_scan_id=selected_source_scan_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    occurred_at_ms=occurred_at_ms,
                    event_id=event_id,
                    event_type=AuditEventType.RESERVATION_STATE_CHANGED,
                    payload={
                        "operation": operation.value,
                        "mutation_action_id": mutation_action_id,
                        "idempotency_key": reservation.idempotency_key,
                        "old_state": expected_state.value,
                        "new_state": new_state.value,
                    },
                )
                return replace(reservation, state=new_state)
        except sqlite3.IntegrityError as exc:
            raise ExecutionStoreConflictError(_integrity_error_message(exc)) from exc

    def load_action_selection(
        self,
        environment: ExecutionEnvironment,
        business_action_id: str,
    ) -> ActionSelection | None:
        if not isinstance(environment, ExecutionEnvironment):
            raise TypeError("environment must be an ExecutionEnvironment")
        _require_token(business_action_id, "business_action_id")
        with self._connection() as connection:
            return self._load_action_selection_connection(
                connection,
                environment,
                business_action_id,
            )

    def read_events(self, audit_correlation_id: str) -> tuple[ExecutionAuditEvent, ...]:
        _require_token(audit_correlation_id, "audit_correlation_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, sequence, event_type, event_json
                FROM audit_events
                WHERE audit_correlation_id = ?
                ORDER BY sequence
                """,
                (audit_correlation_id,),
            ).fetchall()
        events: list[ExecutionAuditEvent] = []
        for row in rows:
            try:
                event = ExecutionAuditEvent.from_json(row["event_json"])
            except ValueError as exc:
                raise ExecutionStoreInvariantError("persisted audit event is corrupt") from exc
            if (
                event.event_id != row["event_id"]
                or event.sequence != row["sequence"]
                or event.event_type.value != row["event_type"]
                or event.audit_correlation_id != audit_correlation_id
            ):
                raise ExecutionStoreInvariantError("persisted audit event index mismatch")
            events.append(event)
        return tuple(events)

    def _initialize(self) -> None:
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            user_objects = connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            if version == 0:
                if user_objects:
                    raise ExecutionStoreSchemaError(
                        "execution store contains unknown persistent schema objects"
                    )
                with self._transaction(connection=connection):
                    for statement in _SCHEMA_STATEMENTS:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {EXECUTION_STORE_SCHEMA_VERSION}")
                version = EXECUTION_STORE_SCHEMA_VERSION
            if version != EXECUTION_STORE_SCHEMA_VERSION:
                raise ExecutionStoreSchemaError(
                    f"unsupported execution store schema version: {version}"
                )
            expected = {"intents", "action_selections", "reservations", "audit_events"}
            actual = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if actual != expected:
                raise ExecutionStoreSchemaError("execution store schema tables are incomplete or unknown")
            unexpected_objects = connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE type IN ('index', 'trigger', 'view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            if unexpected_objects:
                raise ExecutionStoreSchemaError(
                    "execution store contains unknown persistent schema objects"
                )
            definitions = {
                row["name"]: _normalize_schema_sql(row["sql"])
                for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            expected_definitions = {
                name: _normalize_schema_sql(statement)
                for name, statement in _SCHEMA_SQL_BY_TABLE.items()
            }
            if definitions != expected_definitions:
                raise ExecutionStoreSchemaError("execution store schema definition is corrupt")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ExecutionStoreError("cannot open execution database") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            if journal_mode != "wal":
                raise ExecutionStoreSchemaError("execution store requires WAL journal mode")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(
        self,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> Iterator[sqlite3.Connection]:
        if connection is None:
            with self._connection() as owned:
                with self._transaction(connection=owned) as transaction:
                    yield transaction
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def _insert_or_verify_intent(
        self,
        connection: sqlite3.Connection,
        intent: OrderIntent,
        *,
        source_scan_id: str,
    ) -> None:
        _require_token(source_scan_id, "source_scan_id")
        intent_json = canonical_json(intent.to_dict())
        row = connection.execute(
            "SELECT * FROM intents WHERE intent_id = ?",
            (intent.intent_id,),
        ).fetchone()
        if row is not None:
            existing = self._intent_from_row(row)
            if existing != intent or row["intent_json"] != intent_json:
                raise ExecutionStoreConflictError("intent_id maps to different immutable bytes")
            return
        connection.execute(
            """
            INSERT INTO intents (
                intent_id, environment, business_action_id, intent_fingerprint,
                source_scan_id, created_at_ms, intent_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent.intent_id,
                intent.environment.value,
                intent.business_action_id,
                intent.intent_fingerprint,
                source_scan_id,
                intent.created_at_ms,
                intent_json,
            ),
        )

    def _load_intent_connection(
        self,
        connection: sqlite3.Connection,
        intent_id: str,
    ) -> OrderIntent | None:
        row = connection.execute(
            "SELECT * FROM intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        return None if row is None else self._intent_from_row(row)

    def _load_intent_source_scan_id_connection(
        self,
        connection: sqlite3.Connection,
        intent_id: str,
    ) -> str:
        row = connection.execute(
            "SELECT source_scan_id FROM intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise ExecutionStoreInvariantError("persisted intent is missing")
        try:
            _require_token(row["source_scan_id"], "source_scan_id")
        except ValueError as exc:
            raise ExecutionStoreInvariantError(
                "persisted intent source_scan_id is corrupt"
            ) from exc
        return row["source_scan_id"]

    def _intent_from_row(self, row: sqlite3.Row) -> OrderIntent:
        try:
            payload = json.loads(row["intent_json"])
            intent = OrderIntent.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise ExecutionStoreInvariantError("persisted intent is corrupt") from exc
        if canonical_json(intent.to_dict()) != row["intent_json"]:
            raise ExecutionStoreInvariantError("persisted intent is not canonical")
        try:
            _require_token(row["source_scan_id"], "source_scan_id")
        except ValueError as exc:
            raise ExecutionStoreInvariantError("persisted intent source_scan_id is corrupt") from exc
        if (
            intent.intent_id != row["intent_id"]
            or intent.environment.value != row["environment"]
            or intent.business_action_id != row["business_action_id"]
            or intent.intent_fingerprint != row["intent_fingerprint"]
            or intent.created_at_ms != row["created_at_ms"]
        ):
            raise ExecutionStoreInvariantError("persisted intent index mismatch")
        return intent

    def _require_no_orphaned_action_evidence(
        self,
        connection: sqlite3.Connection,
        environment: ExecutionEnvironment,
        business_action_id: str,
    ) -> None:
        intent_exists = connection.execute(
            "SELECT 1 FROM intents WHERE environment = ? AND business_action_id = ? LIMIT 1",
            (environment.value, business_action_id),
        ).fetchone()
        reservation_exists = connection.execute(
            "SELECT 1 FROM reservations WHERE environment = ? AND business_action_id = ? LIMIT 1",
            (environment.value, business_action_id),
        ).fetchone()
        event_exists = connection.execute(
            "SELECT 1 FROM audit_events WHERE audit_correlation_id = ? LIMIT 1",
            (business_action_id,),
        ).fetchone()
        if intent_exists is not None or reservation_exists is not None or event_exists is not None:
            raise ExecutionStoreInvariantError(
                "persisted business-action evidence exists without action selection"
            )

    def _load_action_selection_connection(
        self,
        connection: sqlite3.Connection,
        environment: ExecutionEnvironment,
        business_action_id: str,
    ) -> ActionSelection | None:
        row = connection.execute(
            """
            SELECT * FROM action_selections
            WHERE environment = ? AND business_action_id = ?
            """,
            (environment.value, business_action_id),
        ).fetchone()
        if row is None:
            return None
        if row["mutation_reserved"] not in (0, 1):
            raise ExecutionStoreInvariantError("persisted mutation_reserved is invalid")
        if type(row["selection_version"]) is not int or row["selection_version"] <= 0:
            raise ExecutionStoreInvariantError("persisted selection_version is invalid")
        try:
            selection = ActionSelection(
                environment=ExecutionEnvironment(row["environment"]),
                business_action_id=row["business_action_id"],
                selected_intent_id=row["selected_intent_id"],
                selected_intent_fingerprint=row["selected_intent_fingerprint"],
                selection_version=row["selection_version"],
                mutation_reserved=bool(row["mutation_reserved"]),
            )
        except (TypeError, ValueError) as exc:
            raise ExecutionStoreInvariantError(
                "persisted action selection is corrupt"
            ) from exc
        selected_intent = self._load_intent_connection(
            connection,
            selection.selected_intent_id,
        )
        if (
            selected_intent is None
            or selection.environment is not selected_intent.environment
            or selection.business_action_id != selected_intent.business_action_id
            or selection.selected_intent_fingerprint
            != selected_intent.intent_fingerprint
        ):
            raise ExecutionStoreInvariantError(
                "persisted action selection does not bind its selected intent"
            )
        return selection

    def _load_reservation_connection(
        self,
        connection: sqlite3.Connection,
        environment: ExecutionEnvironment,
        operation: MutationOperation,
        mutation_action_id: str,
    ) -> MutationReservation | None:
        row = connection.execute(
            """
            SELECT * FROM reservations
            WHERE environment = ? AND operation = ? AND mutation_action_id = ?
            """,
            (environment.value, operation.value, mutation_action_id),
        ).fetchone()
        if row is None:
            return None
        reservation = _reservation_from_row(row)
        self._validate_reservation_binding(connection, reservation)
        return reservation

    def _validate_reservation_binding(
        self,
        connection: sqlite3.Connection,
        reservation: MutationReservation,
    ) -> None:
        intent = self._load_intent_connection(
            connection,
            reservation.selected_intent_id,
        )
        if intent is None:
            raise ExecutionStoreInvariantError("persisted reservation intent is missing")
        if (
            reservation.environment is not intent.environment
            or reservation.business_action_id != intent.business_action_id
            or reservation.selected_intent_fingerprint != intent.intent_fingerprint
        ):
            raise ExecutionStoreInvariantError(
                "persisted reservation environment or intent binding mismatch"
            )
        selection = self._load_action_selection_connection(
            connection,
            reservation.environment,
            reservation.business_action_id,
        )
        if (
            selection is None
            or selection.selected_intent_id != reservation.selected_intent_id
            or selection.selected_intent_fingerprint
            != reservation.selected_intent_fingerprint
            or not selection.mutation_reserved
        ):
            raise ExecutionStoreInvariantError(
                "persisted reservation action-selection binding mismatch"
            )
        try:
            _validate_mutation_action(
                intent,
                reservation.operation,
                reservation.mutation_action_id,
                reservation.order_lineage_id,
                reservation.cancel_reason_code,
                reservation.cancel_policy_version,
            )
        except (TypeError, ValueError, ExecutionStoreConflictError) as exc:
            raise ExecutionStoreInvariantError(
                "persisted reservation operation identity mismatch"
            ) from exc

    def _require_cancel_slot(
        self,
        connection: sqlite3.Connection,
        *,
        environment: ExecutionEnvironment,
        order_lineage_id: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT * FROM reservations
            WHERE environment = ? AND operation = ? AND order_lineage_id = ?
            """,
            (
                environment.value,
                MutationOperation.CANCEL_ORDER.value,
                order_lineage_id,
            ),
        ).fetchall()
        for row in rows:
            reservation = _reservation_from_row(row)
            self._validate_reservation_binding(connection, reservation)
            if reservation.state is not ReservationState.REJECTED:
                raise ExecutionStoreConflictError(
                    "cancel lineage already has a non-rejected reservation; reuse its same key"
                )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        intent: OrderIntent,
        source_scan_id: str,
        actor_kind: ActorKind,
        actor_id: str,
        occurred_at_ms: int,
        event_id: str,
        event_type: AuditEventType,
        payload: dict,
    ) -> ExecutionAuditEvent:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM audit_events WHERE audit_correlation_id = ?",
                (intent.audit_correlation_id,),
            ).fetchone()[0]
        )
        event = ExecutionAuditEvent(
            schema_version=EXECUTION_AUDIT_SCHEMA_VERSION,
            event_id=event_id,
            event_type=event_type,
            sequence=sequence,
            occurred_at_ms=occurred_at_ms,
            audit_correlation_id=intent.audit_correlation_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            source_scan_id=source_scan_id,
            signal_lineage_id=intent.signal_lineage_id,
            business_action_id=intent.business_action_id,
            environment=intent.environment,
            intent_id=intent.intent_id,
            intent_fingerprint=intent.intent_fingerprint,
            decision_code=intent.decision_code,
            payload=payload,
        )
        try:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, audit_correlation_id, sequence, event_type, event_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.audit_correlation_id,
                    event.sequence,
                    event.event_type.value,
                    event.to_json(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ExecutionStoreConflictError(
                "event_id or audit sequence already exists"
            ) from exc
        return event


def submit_mutation_action_id(intent: OrderIntent) -> str:
    _require_intent(intent)
    return intent.business_action_id


def leverage_mutation_action_id(intent: OrderIntent) -> str:
    _require_intent(intent)
    return "ma1_" + canonical_sha256(
        {
            "business_action_id": intent.business_action_id,
            "environment": intent.environment.value,
            "leverage": intent.leverage,
            "operation": MutationOperation.CONFIGURE_LEVERAGE.value,
            "td_mode": intent.td_mode,
        }
    )


def cancel_mutation_action_id(
    intent: OrderIntent,
    *,
    order_lineage_id: str,
    reason_code: str,
    policy_version: str,
) -> str:
    _require_intent(intent)
    for value, label in (
        (order_lineage_id, "order_lineage_id"),
        (reason_code, "reason_code"),
        (policy_version, "policy_version"),
    ):
        _require_token(value, label)
    return "ca1_" + canonical_sha256(
        {
            "business_action_id": intent.business_action_id,
            "environment": intent.environment.value,
            "operation": MutationOperation.CANCEL_ORDER.value,
            "order_lineage_id": order_lineage_id,
            "policy_version": policy_version,
            "reason_code": reason_code,
        }
    )


def _require_intent(intent: OrderIntent) -> None:
    if not isinstance(intent, OrderIntent):
        raise TypeError("intent must be an OrderIntent")
    OrderIntent.from_dict(intent.to_dict())


def _require_token(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be non-empty single-line text")


def _canonical_intent(intent: OrderIntent) -> OrderIntent:
    _require_intent(intent)
    return OrderIntent.from_dict(intent.to_dict())


def _require_event_metadata(
    source_scan_id: str,
    actor_kind: ActorKind,
    actor_id: str,
    occurred_at_ms: int,
    event_id: str,
) -> None:
    _require_token(source_scan_id, "source_scan_id")
    if not isinstance(actor_kind, ActorKind):
        raise TypeError("actor_kind must be an ActorKind")
    _require_token(actor_id, "actor_id")
    if type(occurred_at_ms) is not int or occurred_at_ms < 0:
        raise ValueError("occurred_at_ms must be a non-negative integer")
    _require_token(event_id, "event_id")


def _integrity_error_message(exc: sqlite3.IntegrityError) -> str:
    text = str(exc).lower()
    if "event" in text:
        return "event_id or audit sequence already exists"
    if "intent" in text:
        return "intent identity already maps to different facts"
    return "execution store uniqueness constraint rejected conflicting facts"


def _validate_mutation_action(
    intent: OrderIntent,
    operation: MutationOperation,
    mutation_action_id: str,
    order_lineage_id: str,
    cancel_reason_code: str | None,
    cancel_policy_version: str | None,
) -> None:
    if operation is MutationOperation.SUBMIT_ENTRY:
        expected = submit_mutation_action_id(intent)
    elif operation is MutationOperation.CONFIGURE_LEVERAGE:
        expected = leverage_mutation_action_id(intent)
    else:
        if cancel_reason_code is None or cancel_policy_version is None:
            raise ValueError("cancel reservation requires reason code and policy version")
        expected = cancel_mutation_action_id(
            intent,
            order_lineage_id=order_lineage_id,
            reason_code=cancel_reason_code,
            policy_version=cancel_policy_version,
        )
    if mutation_action_id != expected:
        raise ExecutionStoreConflictError(
            "mutation_action_id does not match the canonical operation identity"
        )
    if operation is not MutationOperation.CANCEL_ORDER and (
        cancel_reason_code is not None or cancel_policy_version is not None
    ):
        raise ValueError("cancel metadata is valid only for cancel reservations")


def _reservation_facts(reservation: MutationReservation) -> tuple:
    return (
        reservation.environment,
        reservation.operation,
        reservation.mutation_action_id,
        reservation.idempotency_key,
        reservation.business_action_id,
        reservation.selected_intent_id,
        reservation.selected_intent_fingerprint,
        reservation.order_lineage_id,
        reservation.client_order_id,
        reservation.cancel_reason_code,
        reservation.cancel_policy_version,
    )


def _reservation_from_row(row: sqlite3.Row) -> MutationReservation:
    try:
        reservation = MutationReservation(
            environment=ExecutionEnvironment(row["environment"]),
            operation=MutationOperation(row["operation"]),
            mutation_action_id=row["mutation_action_id"],
            idempotency_key=row["idempotency_key"],
            business_action_id=row["business_action_id"],
            selected_intent_id=row["selected_intent_id"],
            selected_intent_fingerprint=row["selected_intent_fingerprint"],
            order_lineage_id=row["order_lineage_id"],
            client_order_id=row["client_order_id"],
            cancel_reason_code=row["cancel_reason_code"],
            cancel_policy_version=row["cancel_policy_version"],
            state=ReservationState(row["state"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionStoreInvariantError("persisted reservation is corrupt") from exc
    for value, label in (
        (reservation.mutation_action_id, "mutation_action_id"),
        (reservation.business_action_id, "business_action_id"),
        (reservation.selected_intent_id, "selected_intent_id"),
        (reservation.selected_intent_fingerprint, "selected_intent_fingerprint"),
        (reservation.order_lineage_id, "order_lineage_id"),
    ):
        try:
            _require_token(value, label)
        except ValueError as exc:
            raise ExecutionStoreInvariantError("persisted reservation is corrupt") from exc
    expected_key = idempotency_key_for(
        reservation.environment,
        reservation.operation,
        reservation.mutation_action_id,
    )
    if reservation.idempotency_key != expected_key:
        raise ExecutionStoreInvariantError("persisted reservation idempotency key mismatch")
    if reservation.operation is MutationOperation.SUBMIT_ENTRY:
        if reservation.client_order_id != okx_client_order_id_for(reservation.idempotency_key):
            raise ExecutionStoreInvariantError("persisted submit client order ID mismatch")
    elif reservation.client_order_id is not None:
        raise ExecutionStoreInvariantError("non-submit reservation has a client order ID")
    if reservation.operation is MutationOperation.CANCEL_ORDER:
        if reservation.cancel_reason_code is None or reservation.cancel_policy_version is None:
            raise ExecutionStoreInvariantError("persisted cancel reservation metadata is incomplete")
    elif reservation.cancel_reason_code is not None or reservation.cancel_policy_version is not None:
        raise ExecutionStoreInvariantError("non-cancel reservation has cancel metadata")
    return reservation


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE intents (
        intent_id TEXT PRIMARY KEY,
        environment TEXT NOT NULL CHECK (environment IN ('demo', 'production')),
        business_action_id TEXT NOT NULL,
        intent_fingerprint TEXT NOT NULL,
        source_scan_id TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
        intent_json TEXT NOT NULL,
        UNIQUE (environment, business_action_id, intent_fingerprint)
    )
    """,
    """
    CREATE TABLE action_selections (
        environment TEXT NOT NULL CHECK (environment IN ('demo', 'production')),
        business_action_id TEXT NOT NULL,
        selected_intent_id TEXT NOT NULL REFERENCES intents(intent_id),
        selected_intent_fingerprint TEXT NOT NULL,
        selection_version INTEGER NOT NULL CHECK (selection_version > 0),
        mutation_reserved INTEGER NOT NULL CHECK (mutation_reserved IN (0, 1)),
        PRIMARY KEY (environment, business_action_id)
    )
    """,
    """
    CREATE TABLE reservations (
        environment TEXT NOT NULL CHECK (environment IN ('demo', 'production')),
        operation TEXT NOT NULL CHECK (
            operation IN ('configure_leverage', 'submit_entry', 'cancel_order')
        ),
        mutation_action_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        business_action_id TEXT NOT NULL,
        selected_intent_id TEXT NOT NULL REFERENCES intents(intent_id),
        selected_intent_fingerprint TEXT NOT NULL,
        order_lineage_id TEXT NOT NULL,
        client_order_id TEXT UNIQUE,
        cancel_reason_code TEXT,
        cancel_policy_version TEXT,
        state TEXT NOT NULL CHECK (
            state IN (
                'reserved', 'dispatching', 'acknowledged', 'rejected',
                'not_sent_failed', 'unknown', 'invalidated'
            )
        ),
        PRIMARY KEY (environment, operation, mutation_action_id),
        FOREIGN KEY (environment, business_action_id)
            REFERENCES action_selections(environment, business_action_id)
    )
    """,
    """
    CREATE TABLE audit_events (
        event_id TEXT PRIMARY KEY,
        audit_correlation_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        event_type TEXT NOT NULL CHECK (
            event_type IN (
                'intent.created', 'intent.reused', 'intent.superseded',
                'idempotency.reserved', 'idempotency.reservation_state_changed'
            )
        ),
        event_json TEXT NOT NULL,
        UNIQUE (audit_correlation_id, sequence)
    )
    """,
)

_SCHEMA_SQL_BY_TABLE = dict(
    zip(
        ("intents", "action_selections", "reservations", "audit_events"),
        _SCHEMA_STATEMENTS,
        strict=True,
    )
)


def _normalize_schema_sql(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.strip())
