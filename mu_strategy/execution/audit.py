from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum, unique
from types import MappingProxyType
from typing import Any, Mapping

from mu_strategy.canonical import canonical_json, canonical_sha256
from mu_strategy.execution.intents import ExecutionEnvironment, OrderIntent
from mu_strategy.models import EntryDecisionCode


EXECUTION_AUDIT_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INTENT_ID_PATTERN = re.compile(r"^oi1_[0-9a-f]{64}$")
_LEVERAGE_ACTION_PATTERN = re.compile(r"^ma1_[0-9a-f]{64}$")
_CANCEL_ACTION_PATTERN = re.compile(r"^ca1_[0-9a-f]{64}$")
_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "event_type",
    "sequence",
    "occurred_at_ms",
    "audit_correlation_id",
    "actor_kind",
    "actor_id",
    "source_scan_id",
    "signal_lineage_id",
    "business_action_id",
    "environment",
    "intent_id",
    "intent_fingerprint",
    "decision_code",
    "payload",
}


@unique
class ActorKind(str, Enum):
    SYSTEM = "system"
    OPERATOR = "operator"


@unique
class MutationOperation(str, Enum):
    CONFIGURE_LEVERAGE = "configure_leverage"
    SUBMIT_ENTRY = "submit_entry"
    CANCEL_ORDER = "cancel_order"


@unique
class ReservationState(str, Enum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    NOT_SENT_FAILED = "not_sent_failed"
    UNKNOWN = "unknown"
    INVALIDATED = "invalidated"


@unique
class AuditEventType(str, Enum):
    INTENT_CREATED = "intent.created"
    INTENT_REUSED = "intent.reused"
    INTENT_SUPERSEDED = "intent.superseded"
    IDEMPOTENCY_RESERVED = "idempotency.reserved"
    RESERVATION_STATE_CHANGED = "idempotency.reservation_state_changed"


_PAYLOAD_FIELDS = {
    AuditEventType.INTENT_CREATED: {"intent"},
    AuditEventType.INTENT_REUSED: {
        "existing_intent_id",
        "existing_intent_fingerprint",
        "duplicate_source_scan_id",
    },
    AuditEventType.INTENT_SUPERSEDED: {
        "old_intent_id",
        "new_intent_id",
        "selection_version",
    },
    AuditEventType.IDEMPOTENCY_RESERVED: {
        "operation",
        "mutation_action_id",
        "idempotency_key",
        "order_lineage_id",
        "client_order_id",
        "selected_intent_fingerprint",
        "cancel_reason_code",
        "cancel_policy_version",
    },
    AuditEventType.RESERVATION_STATE_CHANGED: {
        "operation",
        "mutation_action_id",
        "idempotency_key",
        "old_state",
        "new_state",
    },
}


@dataclass(frozen=True)
class ExecutionAuditEvent:
    schema_version: int
    event_id: str
    event_type: AuditEventType
    sequence: int
    occurred_at_ms: int
    audit_correlation_id: str
    actor_kind: ActorKind
    actor_id: str
    source_scan_id: str
    signal_lineage_id: str
    business_action_id: str
    environment: ExecutionEnvironment
    intent_id: str
    intent_fingerprint: str
    decision_code: EntryDecisionCode
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != EXECUTION_AUDIT_SCHEMA_VERSION:
            raise ValueError("unsupported execution audit schema_version")
        _require_token(self.event_id, "event_id")
        if not isinstance(self.event_type, AuditEventType):
            raise ValueError("event_type must be a closed AuditEventType")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("sequence must be a positive integer")
        if type(self.occurred_at_ms) is not int or self.occurred_at_ms < 0:
            raise ValueError("occurred_at_ms must be a non-negative integer")
        if not isinstance(self.actor_kind, ActorKind):
            raise ValueError("actor_kind must be a closed ActorKind")
        for value, label in (
            (self.audit_correlation_id, "audit_correlation_id"),
            (self.actor_id, "actor_id"),
            (self.source_scan_id, "source_scan_id"),
            (self.signal_lineage_id, "signal_lineage_id"),
            (self.business_action_id, "business_action_id"),
        ):
            _require_token(value, label)
        if self.audit_correlation_id != self.business_action_id:
            raise ValueError("intent audit_correlation_id must equal business_action_id")
        if not isinstance(self.environment, ExecutionEnvironment):
            raise ValueError("environment must be a closed ExecutionEnvironment")
        if not isinstance(self.intent_id, str) or _INTENT_ID_PATTERN.fullmatch(self.intent_id) is None:
            raise ValueError("intent_id must be canonical")
        if not isinstance(self.intent_fingerprint, str) or _SHA256_PATTERN.fullmatch(self.intent_fingerprint) is None:
            raise ValueError("intent_fingerprint must be canonical")
        if not isinstance(self.decision_code, EntryDecisionCode):
            raise ValueError("decision_code must be a closed EntryDecisionCode")
        canonical_payload = _canonical_payload_copy(self.payload)
        expected = _PAYLOAD_FIELDS[self.event_type]
        if set(canonical_payload) != expected:
            raise ValueError(
                f"{self.event_type.value} payload fields must be exactly {sorted(expected)}"
            )
        self._validate_payload(canonical_payload)
        object.__setattr__(self, "payload", _freeze_json(canonical_payload))

    def _validate_payload(self, payload: dict[str, Any]) -> None:
        if self.event_type is AuditEventType.INTENT_CREATED:
            intent = OrderIntent.from_dict(payload["intent"])
            _require_event_intent_match(self, intent)
            return
        if self.event_type is AuditEventType.INTENT_REUSED:
            if payload["existing_intent_id"] != self.intent_id:
                raise ValueError("intent.reused existing_intent_id mismatch")
            if payload["existing_intent_fingerprint"] != self.intent_fingerprint:
                raise ValueError("intent.reused existing_intent_fingerprint mismatch")
            _require_token(payload["duplicate_source_scan_id"], "duplicate_source_scan_id")
            return
        if self.event_type is AuditEventType.INTENT_SUPERSEDED:
            for name in ("old_intent_id", "new_intent_id"):
                value = payload[name]
                if not isinstance(value, str) or _INTENT_ID_PATTERN.fullmatch(value) is None:
                    raise ValueError(f"intent.superseded {name} must be canonical")
            if payload["new_intent_id"] != self.intent_id:
                raise ValueError("intent.superseded new_intent_id mismatch")
            if type(payload["selection_version"]) is not int or payload["selection_version"] <= 1:
                raise ValueError("intent.superseded selection_version must be greater than one")
            return
        for name in ("operation", "mutation_action_id", "idempotency_key"):
            _require_token(payload[name], name)
        try:
            operation = MutationOperation(payload["operation"])
        except ValueError as exc:
            raise ValueError("audit operation must be a closed MutationOperation") from exc
        mutation_action_id = payload["mutation_action_id"]
        if operation is MutationOperation.SUBMIT_ENTRY:
            if mutation_action_id != self.business_action_id:
                raise ValueError("submit mutation_action_id must equal business_action_id")
        elif operation is MutationOperation.CONFIGURE_LEVERAGE:
            if _LEVERAGE_ACTION_PATTERN.fullmatch(mutation_action_id) is None:
                raise ValueError("leverage mutation_action_id must be canonical")
        elif _CANCEL_ACTION_PATTERN.fullmatch(mutation_action_id) is None:
            raise ValueError("cancel mutation_action_id must be canonical")
        expected_key = idempotency_key_for(
            self.environment,
            operation,
            mutation_action_id,
        )
        if payload["idempotency_key"] != expected_key:
            raise ValueError("audit idempotency_key does not match its mutation identity")
        if self.event_type is AuditEventType.IDEMPOTENCY_RESERVED:
            _require_token(payload["order_lineage_id"], "order_lineage_id")
            client_order_id = payload["client_order_id"]
            if payload["selected_intent_fingerprint"] != self.intent_fingerprint:
                raise ValueError("idempotency.reserved selected_intent_fingerprint mismatch")
            cancel_reason_code = payload["cancel_reason_code"]
            cancel_policy_version = payload["cancel_policy_version"]
            if operation is MutationOperation.SUBMIT_ENTRY:
                if client_order_id != okx_client_order_id_for(expected_key):
                    raise ValueError("submit client_order_id does not match idempotency_key")
                if cancel_reason_code is not None or cancel_policy_version is not None:
                    raise ValueError("submit reservation cannot carry cancel metadata")
            elif operation is MutationOperation.CANCEL_ORDER:
                if client_order_id is not None:
                    raise ValueError("cancel reservation cannot carry client_order_id")
                _require_token(cancel_reason_code, "cancel_reason_code")
                _require_token(cancel_policy_version, "cancel_policy_version")
            elif (
                client_order_id is not None
                or cancel_reason_code is not None
                or cancel_policy_version is not None
            ):
                raise ValueError("leverage reservation cannot carry order or cancel metadata")
            return
        try:
            old_state = ReservationState(payload["old_state"])
            new_state = ReservationState(payload["new_state"])
        except ValueError as exc:
            raise ValueError("audit states must be closed ReservationState values") from exc
        if not is_reservation_transition_allowed(old_state, new_state):
            raise ValueError(
                f"illegal reservation state transition: {old_state.value} -> {new_state.value}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "sequence": self.sequence,
            "occurred_at_ms": self.occurred_at_ms,
            "audit_correlation_id": self.audit_correlation_id,
            "actor_kind": self.actor_kind.value,
            "actor_id": self.actor_id,
            "source_scan_id": self.source_scan_id,
            "signal_lineage_id": self.signal_lineage_id,
            "business_action_id": self.business_action_id,
            "environment": self.environment.value,
            "intent_id": self.intent_id,
            "intent_fingerprint": self.intent_fingerprint,
            "decision_code": self.decision_code.value,
            "payload": _thaw_json(self.payload),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> "ExecutionAuditEvent":
        if not isinstance(payload, dict) or set(payload) != _EVENT_FIELDS:
            raise ValueError(f"execution audit event fields must be exactly {sorted(_EVENT_FIELDS)}")
        try:
            return cls(
                schema_version=payload["schema_version"],
                event_id=payload["event_id"],
                event_type=AuditEventType(payload["event_type"]),
                sequence=payload["sequence"],
                occurred_at_ms=payload["occurred_at_ms"],
                audit_correlation_id=payload["audit_correlation_id"],
                actor_kind=ActorKind(payload["actor_kind"]),
                actor_id=payload["actor_id"],
                source_scan_id=payload["source_scan_id"],
                signal_lineage_id=payload["signal_lineage_id"],
                business_action_id=payload["business_action_id"],
                environment=ExecutionEnvironment(payload["environment"]),
                intent_id=payload["intent_id"],
                intent_fingerprint=payload["intent_fingerprint"],
                decision_code=EntryDecisionCode(payload["decision_code"]),
                payload=payload["payload"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("execution audit"):
                raise
            raise ValueError(f"invalid execution audit event: {exc}") from exc

    @classmethod
    def from_json(cls, payload: str) -> "ExecutionAuditEvent":
        if not isinstance(payload, str):
            raise ValueError("execution audit JSON must be text")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("execution audit JSON is malformed") from exc
        event = cls.from_dict(parsed)
        if event.to_json() != payload:
            raise ValueError("execution audit JSON must use canonical encoding")
        return event


def _canonical_payload_copy(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("event payload must be an object")
    try:
        copied = json.loads(canonical_json(dict(payload)))
    except (TypeError, ValueError) as exc:
        raise ValueError("event payload must be canonical finite JSON") from exc
    if not isinstance(copied, dict):
        raise ValueError("event payload must be an object")
    return copied


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _require_token(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be non-empty single-line text")


def _require_event_intent_match(event: ExecutionAuditEvent, intent: OrderIntent) -> None:
    checks = {
        "audit_correlation_id": intent.audit_correlation_id,
        "signal_lineage_id": intent.signal_lineage_id,
        "business_action_id": intent.business_action_id,
        "environment": intent.environment,
        "intent_id": intent.intent_id,
        "intent_fingerprint": intent.intent_fingerprint,
        "decision_code": intent.decision_code,
    }
    for name, expected in checks.items():
        if getattr(event, name) != expected:
            raise ValueError(f"intent.created {name} mismatch")


def idempotency_key_for(
    environment: ExecutionEnvironment,
    operation: MutationOperation,
    mutation_action_id: str,
) -> str:
    if not isinstance(environment, ExecutionEnvironment):
        raise TypeError("environment must be an ExecutionEnvironment")
    if not isinstance(operation, MutationOperation):
        raise TypeError("operation must be a MutationOperation")
    _require_token(mutation_action_id, "mutation_action_id")
    return canonical_sha256(
        {
            "environment": environment.value,
            "mutation_action_id": mutation_action_id,
            "operation": operation.value,
        }
    )


def okx_client_order_id_for(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or _SHA256_PATTERN.fullmatch(idempotency_key) is None:
        raise ValueError("idempotency_key must be 64 lowercase hex characters")
    digest = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest().upper()
    return "OD" + digest[:20]


def is_reservation_transition_allowed(
    old_state: ReservationState,
    new_state: ReservationState,
) -> bool:
    if not isinstance(old_state, ReservationState) or not isinstance(new_state, ReservationState):
        raise TypeError("reservation states must be ReservationState values")
    return new_state in _ALLOWED_RESERVATION_TRANSITIONS[old_state]


_ALLOWED_RESERVATION_TRANSITIONS = {
    ReservationState.RESERVED: frozenset(
        {ReservationState.DISPATCHING, ReservationState.INVALIDATED}
    ),
    ReservationState.DISPATCHING: frozenset(
        {
            ReservationState.ACKNOWLEDGED,
            ReservationState.REJECTED,
            ReservationState.NOT_SENT_FAILED,
            ReservationState.UNKNOWN,
        }
    ),
    ReservationState.NOT_SENT_FAILED: frozenset({ReservationState.RESERVED}),
    ReservationState.ACKNOWLEDGED: frozenset(),
    ReservationState.REJECTED: frozenset(),
    ReservationState.UNKNOWN: frozenset(),
    ReservationState.INVALIDATED: frozenset(),
}
