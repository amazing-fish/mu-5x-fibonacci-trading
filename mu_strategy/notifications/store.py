from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from mu_strategy.canonical import canonical_json
from mu_strategy.fs_durability import fsync_directory
from mu_strategy.notifications.events import AlertEvent, DeliveryState, NotificationError, decode, digest, integer
from mu_strategy.service_health import HealthStore


SCHEMA = (
    "CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE streams (symbol TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE outbox (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL "
    "CHECK(state IN ('pending','confirmed','failed','unknown')), attempts INTEGER NOT NULL CHECK(attempts >= 0), "
    "next_attempt_ms INTEGER NOT NULL CHECK(next_attempt_ms >= 0), retryable INTEGER NOT NULL CHECK(retryable IN (0,1)), "
    "suppressed_reason TEXT)",
    "CREATE TABLE delivery_history (sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES outbox(event_id), "
    "at_ms INTEGER NOT NULL CHECK(at_ms >= 0), action TEXT NOT NULL, result TEXT NOT NULL)",
)


@dataclass(frozen=True)
class DeliveryRecord:
    event: AlertEvent
    state: DeliveryState
    attempts: int
    next_attempt_ms: int
    retryable: bool
    suppressed_reason: str | None

    def summary(self) -> dict:
        return {"event_id": self.event.event_id, "kind": self.event.kind.value, "state": self.state.value,
                "attempts": self.attempts, "next_attempt_ms": self.next_attempt_ms,
                "retryable": self.retryable, "suppressed_reason": self.suppressed_reason}


class NotificationStore:
    def __init__(self, data_dir: Path):
        self.health = HealthStore(data_dir)
        self.path = self.health.root / "email.sqlite3"

    def initialize(self) -> None:
        self.health.prepare()
        with self.connection() as db, self.transaction(db):
            version = db.execute("PRAGMA user_version").fetchone()[0]
            definitions = db.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if version == 0:
                if definitions:
                    raise NotificationError("unknown email database schema")
                for statement in SCHEMA:
                    db.execute(statement)
                db.execute("PRAGMA user_version = 1")
                self.set_meta(db, "data_dir", str(self.health.data_dir))
            self.validate(db)
        fsync_directory(self.path.parent)

    def validate(self, db) -> None:
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise NotificationError("unsupported email database schema")
        actual = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name")]
        if sorted(actual) != sorted(SCHEMA):
            raise NotificationError("email database schema changed")
        if self.get_meta(db, "data_dir") != str(self.health.data_dir):
            raise NotificationError("email database belongs to another data directory")

    @contextmanager
    def connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        target = self.path.as_uri() + "?mode=ro" if readonly else str(self.path)
        db = sqlite3.connect(target, uri=readonly, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            if not readonly:
                db.execute("PRAGMA synchronous = FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, db) -> Iterator[None]:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    def get_meta(self, db, name: str, default=None):
        row = db.execute("SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
        if row is None:
            return default
        value = decode(row[0])
        if name in {"health_cursor", "runtime_sequence", "out_of_order_observations", "last_collection_ms"}:
            integer(value)
        elif name == "review_ms":
            integer(value, minimum=1)
        elif name == "log_unavailable_since_ms" and value is not None:
            integer(value)
        elif name in {"delivery_target", "last_cycle_sha256"}:
            digest(value)
        elif name == "source_ready" and type(value) is not bool:
            raise NotificationError("invalid source readiness")
        elif name == "runtime":
            if (not isinstance(value, list) or len(value) != 2 or value[0] not in {"running", "stopped", "interrupted", "unresponsive", "source_unavailable"}
                    or (value[1] is not None and (not isinstance(value[1], str) or not value[1]))):
                raise NotificationError("invalid persisted runtime")
        return value

    def set_meta(self, db, name: str, value) -> None:
        db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                   (name, canonical_json(value)))

    def stream(self, db, symbol: str) -> dict:
        row = db.execute("SELECT value FROM streams WHERE symbol=?", (symbol,)).fetchone()
        if row is None:
            return {"counter": 0, "created_at_ms": 0, "observed_at_ms": 0,
                    "active_event_id": None, "signal_key": None, "last_seen_ms": 0, "last_result": None}
        result = decode(row[0])
        if not isinstance(result, dict) or set(result) != {"counter", "created_at_ms", "observed_at_ms", "active_event_id", "signal_key", "last_seen_ms", "last_result"}:
            raise NotificationError("invalid signal stream")
        for key in ("counter", "created_at_ms", "observed_at_ms", "last_seen_ms"):
            integer(result[key])
        for key in ("active_event_id", "signal_key", "last_result"):
            if result[key] is not None:
                digest(result[key])
        if (result["active_event_id"] is None) != (result["signal_key"] is None):
            raise NotificationError("inconsistent signal stream")
        return result

    def save_stream(self, db, symbol: str, value: dict) -> None:
        db.execute("INSERT INTO streams VALUES (?,?) ON CONFLICT(symbol) DO UPDATE SET value=excluded.value",
                   (symbol, canonical_json(value)))
        self.stream(db, symbol)

    def symbols(self, db) -> tuple[str, ...]:
        return tuple(row[0] for row in db.execute("SELECT symbol FROM streams ORDER BY symbol"))

    def enqueue(self, db, event: AlertEvent, *, now_ms: int, suppressed_reason: str | None = None) -> None:
        canonical = AlertEvent.from_json(event.to_json())
        existing = self.record(db, event.event_id)
        if existing is not None:
            if existing.event != canonical:
                raise NotificationError("event identity reused with different evidence")
            return
        db.execute("INSERT INTO outbox VALUES (?,?,'pending',0,?,0,?)",
                   (event.event_id, canonical.to_json(), integer(now_ms), suppressed_reason))
        self.history(db, event.event_id, now_ms, "recorded", suppressed_reason or "pending")

    def record(self, db, event_id: str) -> DeliveryRecord | None:
        digest(event_id)
        row = db.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        event = AlertEvent.from_json(row["payload"])
        if event.event_id != row["event_id"] or row["retryable"] not in (0, 1):
            raise NotificationError("invalid persisted delivery")
        try:
            state = DeliveryState(row["state"])
        except ValueError as exc:
            raise NotificationError("invalid persisted delivery state") from exc
        result = DeliveryRecord(event, state, integer(row["attempts"]),
                                integer(row["next_attempt_ms"]), bool(row["retryable"]), row["suppressed_reason"])
        if result.state is not DeliveryState.PENDING and result.attempts == 0:
            raise NotificationError("delivery result without attempt")
        if result.suppressed_reason not in {None, "review_expired", "decision_changed", "source_unavailable", "signal_replaced", "historical_event"}:
            raise NotificationError("invalid delivery suppression")
        return result

    def history(self, db, event_id: str, now_ms: int, action: str, result: str) -> None:
        db.execute("INSERT INTO delivery_history(event_id,at_ms,action,result) VALUES (?,?,?,?)",
                   (event_id, integer(now_ms), action, result))

    def suppress(self, db, event_id: str, reason: str, now_ms: int) -> None:
        record = self.record(db, event_id)
        if record is None:
            raise NotificationError("missing entry evidence")
        if record.suppressed_reason is None:
            db.execute("UPDATE outbox SET suppressed_reason=? WHERE event_id=?", (reason, event_id))
            self.history(db, event_id, now_ms, "suppressed", reason)

    def claim(self, db, *, now_ms: int, max_attempts: int = 3) -> DeliveryRecord | None:
        row = db.execute("SELECT event_id FROM outbox WHERE suppressed_reason IS NULL AND attempts < ? "
                         "AND next_attempt_ms <= ? AND (state='pending' OR (state='failed' AND retryable=1)) "
                         "ORDER BY rowid LIMIT 1", (max_attempts, integer(now_ms))).fetchone()
        if row is None:
            return None
        record = self.record(db, row[0])
        db.execute("UPDATE outbox SET state='unknown', attempts=attempts+1, retryable=0 WHERE event_id=?", (row[0],))
        self.history(db, row[0], now_ms, "attempt_started", "unknown")
        return self.record(db, record.event.event_id)

    def finish(self, db, event_id: str, *, state: DeliveryState, now_ms: int, code: str, retryable: bool = False) -> None:
        record = self.record(db, event_id)
        if record is None or record.state is not DeliveryState.UNKNOWN or state is DeliveryState.PENDING:
            raise NotificationError("invalid delivery completion")
        delay = min(3600, 60 * 2 ** min(record.attempts - 1, 6)) * 1000
        db.execute("UPDATE outbox SET state=?, retryable=?, next_attempt_ms=? WHERE event_id=?",
                   (state.value, int(retryable and state is DeliveryState.FAILED), integer(now_ms) + delay, event_id))
        self.history(db, event_id, now_ms, "attempt_finished", code)

    def defer_unstarted(self, db, event_id: str, *, now_ms: int, reason: str = "source_snapshot_unavailable") -> None:
        """Release only a reservation whose transport has provably not been called."""
        record = self.record(db, event_id)
        if record is None or record.state is not DeliveryState.UNKNOWN or record.attempts < 1:
            raise NotificationError("invalid unstarted delivery reservation")
        if reason not in {"source_snapshot_unavailable", "source_not_caught_up", "entry_not_reviewable"}:
            raise NotificationError("invalid unstarted delivery reason")
        db.execute("UPDATE outbox SET state='pending',attempts=attempts-1,retryable=0,next_attempt_ms=? WHERE event_id=?",
                   (integer(now_ms) + 30_000, event_id))
        self.history(db, event_id, now_ms, "attempt_not_started", reason)

    def resolve(self, event_id: str, *, outcome: DeliveryState, now_ms: int) -> None:
        if outcome not in {DeliveryState.CONFIRMED, DeliveryState.FAILED}:
            raise NotificationError("operator resolution must be confirmed or failed")
        with self.connection() as db, self.transaction(db):
            self.validate(db)
            self.finish(db, event_id, state=outcome, now_ms=now_ms, code="operator_checked")

    def retry_failed(self, event_id: str, *, now_ms: int) -> None:
        with self.connection() as db, self.transaction(db):
            self.validate(db)
            record = self.record(db, event_id)
            if record is None or record.state is not DeliveryState.FAILED or record.attempts >= 3 or record.suppressed_reason is not None:
                raise NotificationError("only unsuppressed known failures below the attempt limit can retry")
            db.execute("UPDATE outbox SET retryable=1,next_attempt_ms=? WHERE event_id=?", (integer(now_ms), event_id))
            self.history(db, event_id, now_ms, "operator_retry", "cause_fixed")

    def status(self, *, event_id: str | None = None, limit: int = 50) -> dict:
        if not self.path.exists():
            return {"initialized": False, "records": [], "position_alerts": "unavailable_until_issue_85"}
        with self.connection(readonly=True) as db:
            db.execute("BEGIN")
            self.validate(db)
            if event_id is not None:
                record = self.record(db, event_id)
                if record is None:
                    raise NotificationError("unknown event id")
                rows = [record]
            else:
                rows = [self.record(db, row[0]) for row in db.execute("SELECT event_id FROM outbox ORDER BY rowid DESC LIMIT ?", (integer(limit, minimum=1),))]
            result = {"initialized": True, "position_alerts": "unavailable_until_issue_85",
                      "last_collection_ms": self.get_meta(db, "last_collection_ms"),
                      "out_of_order_observations": self.get_meta(db, "out_of_order_observations", 0),
                      "log_unavailable_since_ms": self.get_meta(db, "log_unavailable_since_ms"),
                      "counts": {row[0]: row[1] for row in db.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state")},
                      "suppressed": db.execute("SELECT COUNT(*) FROM outbox WHERE suppressed_reason IS NOT NULL").fetchone()[0],
                      "records": [row.summary() for row in rows]}
            if event_id is not None:
                result["event"] = rows[0].event.to_dict()
                result["history"] = [dict(row) for row in db.execute("SELECT sequence,at_ms,action,result FROM delivery_history WHERE event_id=? ORDER BY sequence", (event_id,))]
            return result
