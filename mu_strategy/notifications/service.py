from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from mu_strategy.canonical import canonical_sha256
from mu_strategy.file_locks import FileLockBusyError
from mu_strategy.market_data.trusted_data.contracts import Clock, SystemClock
from mu_strategy.notifications.events import AlertEvent, AlertKind, DeliveryState, NotificationError, integer
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.observations import JsonlObservationRepository, ObservationCorruptionError, ObservationOutcome, Stage0Observation
from mu_strategy.service_health import HealthStateError, HealthStore, StepStatus, health_view


class EmailAlerts:
    def __init__(self, data_dir: Path, *, review_seconds: int = 300, clock: Clock | None = None,
                 health: HealthStore | None = None):
        self.review_ms = integer(review_seconds, minimum=1) * 1000
        self.clock = clock or SystemClock()
        self.store = NotificationStore(data_dir)
        self.health = health or self.store.health
        if self.health.data_dir != self.store.health.data_dir:
            raise NotificationError("notification health directory mismatch")
        self.observations = JsonlObservationRepository(self.health.root / "observations.jsonl")

    def initialize(self) -> None:
        self.store.initialize()
        with self.store.connection() as db, self.store.transaction(db):
            previous = self.store.get_meta(db, "review_ms")
            if previous is not None and previous != self.review_ms:
                raise NotificationError("review window differs from persisted notification policy")
            self.store.set_meta(db, "review_ms", self.review_ms)

    def _snapshot(self):
        try:
            return self.health.snapshot()
        except OSError as exc:
            raise HealthStateError("notification health source unreadable") from exc

    def collect(self) -> dict:
        """Commit a bounded log batch, its cursor and all resulting reminders together."""
        with self.store.connection() as db, self.store.transaction(db):
            self.store.validate(db)
            cursor = self.store.get_meta(db, "observation_cursor", [0, 0, None])
            if not isinstance(cursor, list) or len(cursor) != 3:
                raise NotificationError("invalid persisted observation cursor")
            cycles, next_cursor = self.observations.read_batch(offset=cursor[0], anchor_start=cursor[1], anchor_sha256=cursor[2], limit=1000)
            state, running = self._snapshot()
            now = self.clock.now_ms()
            view = health_view(state, running=running, now_ms=now)
            current = state.last_cycle.scan.cycle if state and state.last_cycle else None
            current_hash = canonical_sha256(current.to_dict()) if current else None
            current_run = state.run_id if current and state.last_cycle.service_run_id == state.run_id else None
            for cycle in cycles:
                cycle_hash = canonical_sha256(cycle.to_dict())
                previous_hash = self.store.cycle_digest(db, cycle.cycle_id)
                if previous_hash is not None and previous_hash != cycle_hash:
                    raise ObservationCorruptionError("observation cycle identity reused with different content")
                if previous_hash is None:
                    self.store.record_cycle(db, cycle.cycle_id, cycle_hash)
                    for observation in cycle.observations:
                        self._observe(db, observation, now, service_run_id=current_run if cycle_hash == current_hash else None)
                    self.store.set_meta(db, "last_cycle_sha256", cycle_hash)
            self.store.set_meta(db, "observation_cursor", list(next_cursor))
            caught_up = current is not None and self.store.get_meta(db, "last_cycle_sha256") == current_hash
            # The log commits before health. A matching health cycle may arrive
            # on a later poll with no new log bytes, including after clock rollback.
            if caught_up and current_run is not None:
                for observation in current.observations:
                    if self.store.stream(db, observation.symbol)["service_run_id"] != current_run:
                        self._observe(db, observation, now, service_run_id=current_run)
            self._health_events(db, state, now)
            if view["runtime"] != "running" or view["healthy"]:
                self._runtime(db, view["runtime"], state.run_id if state else None, now)
            runtime_failed = view["runtime"] in {"stopped", "interrupted", "unresponsive"}
            for symbol in self.store.symbols(db):
                stream = self.store.stream(db, symbol)
                if stream["active_event_id"] is None:
                    continue
                latest = state.last_cycle if state else None
                # A completed failed scan/write has no committed cycle to match.
                # It invalidates evidence preceding that attempt, not newer log
                # results awaiting their own health publication.
                failed_attempt = (latest is not None and latest.service_run_id == state.run_id
                                  and (latest.scan.status is not StepStatus.SUCCEEDED or latest.scan.persistence is not StepStatus.SUCCEEDED)
                                  and max(stream["created_at_ms"], stream["observed_at_ms"]) <= latest.started_at_ms)
                if runtime_failed or failed_attempt or (caught_up and current_run is not None and (not view["healthy"] or symbol not in state.symbols)):
                    self._invalidate(db, symbol, stream, "source_unavailable", now)
                elif now < stream["last_seen_ms"]:
                    self._invalidate(db, symbol, stream, "source_unavailable", now)
                # Signal continuity follows the service's phase/deadline and
                # committed decisions. The email deadline only limits sending;
                # applying it here would create a new signal each slow cycle.
            self.store.set_meta(db, "source_ready", bool(view["healthy"] and caught_up))
            self.store.set_meta(db, "last_collection_ms", now)
            self.store.set_meta(db, "log_unavailable_since_ms", None)
        return {"runtime": view["runtime"], "source_healthy": view["healthy"], "caught_up": caught_up,
                "cycles_consumed": len(cycles), "position_alerts": "unavailable_until_issue_85"}

    def _observe(self, db, observation: Stage0Observation, now: int, *, service_run_id: str | None = None) -> None:
        observation = Stage0Observation.from_dict(observation.to_dict())
        stream = self.store.stream(db, observation.symbol)
        if service_run_id is not None and stream["service_run_id"] != service_run_id:
            # Only a matching authoritative cycle can establish a new ordering
            # epoch. Historical log records carry no service run identity.
            if stream["active_event_id"] and observation.observed_at_ms < stream["last_seen_ms"]:
                self._invalidate(db, observation.symbol, stream, "source_unavailable", now)
            stream.update(service_run_id=service_run_id, created_at_ms=0, observed_at_ms=0, last_result=None)
        # Future-dated history can belong to a run before clock rollback. It
        # cannot establish the current ordering watermark or a signal lifetime.
        if (observation.created_at_ms > now or observation.observed_at_ms > now
                or observation.created_at_ms < stream["created_at_ms"] or observation.observed_at_ms < stream["observed_at_ms"]):
            self.store.set_meta(db, "out_of_order_observations", integer(self.store.get_meta(db, "out_of_order_observations", 0)) + 1)
            return
        if stream["last_result"] is not None and observation.created_at_ms == stream["created_at_ms"] and observation.observed_at_ms == stream["observed_at_ms"]:
            if stream["last_result"] != observation.result_fingerprint:
                raise NotificationError("conflicting observations at identical timestamps")
            return
        stream.update(created_at_ms=observation.created_at_ms, observed_at_ms=observation.observed_at_ms, last_result=observation.result_fingerprint)
        if observation.outcome is ObservationOutcome.READY_FOR_REVIEW:
            result = observation.scan_result
            if result is None or result.signal_time_ms is None:
                raise NotificationError("ready observation lacks signal identity")
            key = self._signal_key(observation)
            if stream["signal_key"] == key:
                stream["last_seen_ms"] = observation.observed_at_ms
            else:
                if stream["active_event_id"]:
                    self._invalidate(db, observation.symbol, stream, "signal_replaced", now)
                stream["counter"] += 1
                event = AlertEvent(AlertKind.ENTRY_REVIEW, canonical_sha256({"signal": key, "transition": stream["counter"]}),
                                   observation.observed_at_ms, observation.observed_at_ms + self.review_ms, "ready", observation)
                expired = now < observation.observed_at_ms or now >= event.review_until_ms
                self.store.enqueue(db, event, now_ms=now, suppressed_reason="review_expired" if expired else None)
                stream.update(active_event_id=event.event_id, signal_key=key, last_seen_ms=observation.observed_at_ms)
        elif stream["active_event_id"]:
            reason = "decision_changed" if observation.outcome is ObservationOutcome.NORMAL_NO_ACTION else "source_unavailable"
            self._invalidate(db, observation.symbol, stream, reason, now, observation=observation)
        self.store.save_stream(db, observation.symbol, stream)

    @staticmethod
    def _signal_key(observation: Stage0Observation) -> str | None:
        if observation.outcome is not ObservationOutcome.READY_FOR_REVIEW or observation.scan_result is None:
            return None
        return canonical_sha256({"symbol": observation.symbol, "strategy": observation.strategy_name,
                                 "config": observation.strategy_config_fingerprint,
                                 "signal_time_ms": observation.scan_result.signal_time_ms,
                                 "decision": observation.decision_code.value})

    def _invalidate(self, db, symbol, stream, reason: str, now: int, *, observation=None) -> None:
        entry = self.store.record(db, stream["active_event_id"])
        if entry is None:
            raise NotificationError("active reminder has no persisted entry")
        event = AlertEvent(AlertKind.SIGNAL_INVALIDATED, canonical_sha256({"entry": entry.event.event_id, "reason": reason}),
                           now, None, reason, observation or entry.event.observation, entry.event.event_id)
        # Preserve the invalidation for review without mailing a withdrawal of
        # an entry that was definitely never accepted by SMTP.
        unseen = entry.state not in {DeliveryState.CONFIRMED, DeliveryState.UNKNOWN}
        self.store.enqueue(db, event, now_ms=now, suppressed_reason="historical_event" if unseen else None)
        self.store.suppress(db, entry.event.event_id, reason, now)
        stream.update(active_event_id=None, signal_key=None)
        self.store.save_stream(db, symbol, stream)

    def _health_events(self, db, state, now: int) -> None:
        cursor = integer(self.store.get_meta(db, "health_cursor", 0))
        if state is None:
            if cursor:
                raise HealthStateError("service health history disappeared")
            return
        for event in state.events_since(cursor):
            if event.kind in {"fault", "recovered", "stopped"}:
                kind = AlertKind.SERVICE_RECOVERED if event.kind == "recovered" else AlertKind.SERVICE_FAULT
                alert = AlertEvent(kind, canonical_sha256({"source": str(self.health.data_dir), "event": event.to_dict()}),
                                   event.at_ms, None, "health_event",
                                   problems=("runtime.stopped",) if event.kind == "stopped" else event.problems)
                self.store.enqueue(db, alert, now_ms=now)
                if event.kind == "stopped":
                    # Sampling may miss a complete stop/restart between polls.
                    # Recovery is emitted only once a current healthy run exists.
                    self.store.set_meta(db, "runtime", ["stopped", state.run_id])
        self.store.set_meta(db, "health_cursor", state.event_sequence)

    def _runtime(self, db, runtime: str, run_id: str | None, now: int) -> None:
        if runtime in {"not_started", "starting"}:
            return
        previous = self.store.get_meta(db, "runtime")
        fault = runtime != "running"
        key = [runtime, run_id] if fault else ["running", None]
        if previous == key:
            return
        if fault or (previous is not None and previous[0] != "running"):
            sequence = integer(self.store.get_meta(db, "runtime_sequence", 0)) + 1
            event = AlertEvent(AlertKind.SERVICE_FAULT if fault else AlertKind.SERVICE_RECOVERED,
                               canonical_sha256({"source": str(self.health.data_dir), "runtime_transition": sequence}),
                               now, None, "runtime_changed", problems=(f"runtime.{runtime}",) if fault else ())
            self.store.enqueue(db, event, now_ms=now)
            self.store.set_meta(db, "runtime_sequence", sequence)
        self.store.set_meta(db, "runtime", key)

    def reconcile_health(self) -> None:
        state, _ = self._snapshot()
        if state is None:
            raise NotificationError("no current health snapshot to reconcile")
        with self.store.connection() as db, self.store.transaction(db):
            self.store.validate(db)
            old = self.store.get_meta(db, "health_cursor", 0)
            now = self.clock.now_ms()
            audit = AlertEvent(AlertKind.SERVICE_FAULT,
                               canonical_sha256({"data_dir": str(self.health.data_dir), "old_cursor": old,
                                                 "new_cursor": state.event_sequence, "acknowledged_at_ms": now}),
                               now, None, "health_event", problems=("notification.health_history_gap_acknowledged",))
            self.store.enqueue(db, audit, now_ms=now, suppressed_reason="historical_event")
            self.store.set_meta(db, "health_reconciliation", {"previous_cursor": old, "new_cursor": state.event_sequence,
                                                           "at_ms": now, "missing_history_acknowledged": True})
            self.store.set_meta(db, "health_cursor", state.event_sequence)

    def source_unavailable(self) -> None:
        """Report an unreadable source without claiming the market or scanner failed."""
        now = self.clock.now_ms()
        with self.store.connection() as db, self.store.transaction(db):
            self.store.validate(db)
            self.store.set_meta(db, "source_ready", False)
            self._runtime(db, "source_unavailable", None, now)
            for symbol in self.store.symbols(db):
                stream = self.store.stream(db, symbol)
                if stream["active_event_id"]:
                    self._invalidate(db, symbol, stream, "source_unavailable", now)

    def observation_unavailable(self) -> bool:
        """Defer lifecycle changes for a possibly in-progress append, never sending entries.

        The existing writer marks every append invalid until its fsync completes.
        Persist a 30-second retry window so one overlapping poll does not withdraw
        a valid signal; an unresolved marker/corruption still becomes a fault.
        """
        now = self.clock.now_ms()
        with self.store.connection() as db, self.store.transaction(db):
            self.store.validate(db)
            since = self.store.get_meta(db, "log_unavailable_since_ms")
            if since is None or now < since:
                since = now
                self.store.set_meta(db, "log_unavailable_since_ms", since)
            self.store.set_meta(db, "source_ready", False)
        if since <= now < since + 30_000:
            return False
        self.source_unavailable()
        return True

    def deliver(self, transport, *, limit: int = 20) -> int:
        """Reserve UNKNOWN durably before network; serialize result/manual reconciliation.

        The second transaction spans SMTP. A killed sender rolls that transaction
        back to the already committed UNKNOWN reservation; another sender cannot
        replay it, and an operator cannot resolve an attempt still using the DB.
        """
        delivered = 0
        with self.store.connection() as db:
            with self.store.transaction(db):
                self.store.validate(db)
                target = self.store.get_meta(db, "delivery_target")
                if target is not None and target != transport.target_fingerprint:
                    raise NotificationError("SMTP destination differs from persisted delivery target")
                self.store.set_meta(db, "delivery_target", transport.target_fingerprint)
            for _ in range(integer(limit, minimum=1)):
                with self.store.transaction(db):
                    claimed = self.store.claim(db, now_ms=self.clock.now_ms())
                if claimed is None:
                    break
                with self.store.transaction(db), ExitStack() as fence:
                    current = self.store.record(db, claimed.event.event_id)
                    if current.state is not DeliveryState.UNKNOWN or current.attempts != claimed.attempts:
                        continue
                    now = self.clock.now_ms()
                    if current.event.kind is AlertKind.ENTRY_REVIEW:
                        try:
                            fence.enter_context(self.observations.publication_fence(wait=False))
                        except FileLockBusyError:
                            self.store.defer_unstarted(db, current.event.event_id, now_ms=now, reason="source_not_caught_up")
                            continue
                        if not self._prepare_entry(db, current, now):
                            continue
                    result = transport.send(current.event)
                    self.store.finish(db, current.event.event_id, state=result.state, now_ms=self.clock.now_ms(),
                                      code=result.code, retryable=result.retryable)
                    delivered += 1
        return delivered

    def _prepare_entry(self, db, current, now: int) -> bool:
        event = current.event

        def suppress(reason):
            self.store.defer_unstarted(db, event.event_id, now_ms=now, reason="entry_not_reviewable")
            self.store.suppress(db, event.event_id, reason, now)
            return False

        if current.suppressed_reason is not None:
            return suppress(current.suppressed_reason)
        if not event.occurred_at_ms <= now < event.review_until_ms:
            return suppress("review_expired")
        try:
            state, running = self._snapshot()
        except (HealthStateError, OSError):
            self.store.defer_unstarted(db, event.event_id, now_ms=now)
            return False
        view = health_view(state, running=running, now_ms=now)
        latest = state.last_cycle.scan.cycle if state and state.last_cycle else None
        # A newer or temporarily unreadable source is missing evidence, not a
        # definitive invalidation. Let collect reconcile the full transition first.
        caught_up = latest is not None and self.store.get_meta(db, "last_cycle_sha256") == canonical_sha256(latest.to_dict())
        if not view["healthy"] or not self.store.get_meta(db, "source_ready", False) or not caught_up:
            self.store.defer_unstarted(db, event.event_id, now_ms=now, reason="source_not_caught_up")
            return False
        cursor = self.store.get_meta(db, "observation_cursor", [0, 0, None])
        try:
            remaining, _ = self.observations.read_batch(offset=cursor[0], anchor_start=cursor[1], anchor_sha256=cursor[2], limit=1)
        except ObservationCorruptionError:
            remaining = True
        if remaining:
            self.store.defer_unstarted(db, event.event_id, now_ms=now, reason="source_not_caught_up")
            return False
        symbol = event.observation.symbol
        stream = self.store.stream(db, symbol)
        evidence = next((item for item in latest.observations if item.symbol == symbol), None)
        if symbol not in state.symbols or evidence is None:
            return suppress("source_unavailable")
        if self._signal_key(evidence) != stream["signal_key"] or stream["active_event_id"] != event.event_id:
            return suppress("decision_changed")
        return True
