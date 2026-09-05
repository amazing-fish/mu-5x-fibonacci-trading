from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mu_strategy.canonical import canonical_json, canonical_sha256
from mu_strategy.observations import ObservationOutcome, Stage0Observation


class NotificationError(ValueError):
    pass


class AlertKind(str, Enum):
    ENTRY_REVIEW = "entry_review"
    SIGNAL_INVALIDATED = "signal_invalidated"
    SERVICE_FAULT = "service_fault"
    SERVICE_RECOVERED = "service_recovered"


class DeliveryState(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


def integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise NotificationError("invalid notification integer")
    return value


def digest(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise NotificationError("invalid notification digest")
    return value


def decode(raw: str) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NotificationError("duplicate notification field")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(NotificationError("nonfinite JSON")))
    except (ValueError, TypeError) as exc:
        raise NotificationError("invalid notification JSON") from exc


@dataclass(frozen=True)
class AlertEvent:
    kind: AlertKind
    identity: str
    occurred_at_ms: int
    review_until_ms: int | None
    reason: str
    observation: Stage0Observation | None = None
    related_event_id: str | None = None
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AlertKind):
            raise NotificationError("invalid alert kind")
        digest(self.identity)
        integer(self.occurred_at_ms)
        if self.reason not in {"ready", "decision_changed", "source_unavailable", "review_expired",
                               "signal_replaced", "health_event", "runtime_changed"}:
            raise NotificationError("invalid notification reason")
        if self.review_until_ms is not None:
            integer(self.review_until_ms, minimum=self.occurred_at_ms + 1)
        if self.related_event_id is not None:
            digest(self.related_event_id)
        if not isinstance(self.problems, tuple) or any(
            not isinstance(item, str) or re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", item) is None for item in self.problems
        ):
            raise NotificationError("invalid health problem")
        if self.kind in {AlertKind.ENTRY_REVIEW, AlertKind.SIGNAL_INVALIDATED}:
            if self.observation is None or self.problems:
                raise NotificationError("signal reminder requires an observation")
            Stage0Observation.from_dict(self.observation.to_dict())
            if self.kind is AlertKind.ENTRY_REVIEW:
                result = self.observation.scan_result
                if (self.observation.outcome is not ObservationOutcome.READY_FOR_REVIEW or result is None
                        or result.signal_time_ms is None or result.trigger_price is None or result.initial_stop is None
                        or self.review_until_ms is None or self.related_event_id is not None or self.reason != "ready"):
                    raise NotificationError("entry reminder requires complete ready evidence")
            elif self.related_event_id is None or self.review_until_ms is not None:
                raise NotificationError("invalidation requires its entry identity")
        elif self.observation is not None or self.related_event_id is not None or self.review_until_ms is not None:
            raise NotificationError("health reminder cannot invent signal context")

    @property
    def event_id(self) -> str:
        return canonical_sha256({"kind": self.kind.value, "identity": self.identity})

    def to_dict(self) -> dict:
        return {"schema_version": 1, "event_id": self.event_id, "kind": self.kind.value, "identity": self.identity,
                "occurred_at_ms": self.occurred_at_ms, "review_until_ms": self.review_until_ms,
                "reason": self.reason, "observation": self.observation.to_dict() if self.observation else None,
                "related_event_id": self.related_event_id, "problems": list(self.problems)}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, raw: str) -> AlertEvent:
        value = decode(raw)
        expected = {"schema_version", "event_id", "kind", "identity", "occurred_at_ms", "review_until_ms",
                    "reason", "observation", "related_event_id", "problems"}
        if not isinstance(value, dict) or set(value) != expected or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise NotificationError("unsupported alert schema")
        if not isinstance(value["problems"], list):
            raise NotificationError("invalid alert problems")
        try:
            result = cls(AlertKind(value["kind"]), value["identity"], value["occurred_at_ms"], value["review_until_ms"],
                         value["reason"], Stage0Observation.from_dict(value["observation"]) if value["observation"] is not None else None,
                         value["related_event_id"], tuple(value["problems"]))
        except (TypeError, ValueError, KeyError) as exc:
            raise NotificationError("invalid alert payload") from exc
        if result.event_id != value["event_id"]:
            raise NotificationError("alert identity mismatch")
        return result
