from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from mu_strategy.market_data.trusted_data.contracts import SystemClock
from mu_strategy.notifications.events import DeliveryState, NotificationError
from mu_strategy.notifications.service import EmailAlerts
from mu_strategy.notifications.smtp import SmtpConfig, SmtpTransport
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.observations import ObservationCorruptionError
from mu_strategy.service_health import HealthSnapshotUnstableError, HealthStateError


def main(argv=None, *, stdout=None, environment=None, factory=EmailAlerts, transport_factory=SmtpTransport, sleeper=time.sleep) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(description="Consume committed signal observations and service health for NetEase email reminders.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Collect reminders; dry-run unless --send is explicitly supplied.")
    run.add_argument("--once", action="store_true")
    run.add_argument("--send", action="store_true")
    run.add_argument("--poll-seconds", type=int, default=30)
    run.add_argument("--review-seconds", type=int, default=300)
    status = commands.add_parser("status", help="Read delivery counts and recent records without creating state.")
    show = commands.add_parser("show", help="Read one reminder and its delivery history.")
    show.add_argument("event_id")
    resolve = commands.add_parser("resolve", help="Resolve UNKNOWN after checking SMTP acceptance evidence.")
    resolve.add_argument("event_id")
    resolve.add_argument("--outcome", choices=("confirmed", "failed"), required=True)
    resolve.add_argument("--confirm-checked", action="store_true", required=True)
    retry = commands.add_parser("retry", help="Retry a definite failure after correcting its cause, within the three-attempt limit.")
    retry.add_argument("event_id")
    retry.add_argument("--confirm-cause-fixed", action="store_true", required=True)
    reconcile = commands.add_parser("reconcile-health", help="Acknowledge a lost health-event range after inspecting status.")
    reconcile.add_argument("--confirm-history-gap", action="store_true", required=True)
    for command in (run, status, show, resolve, retry, reconcile):
        command.add_argument("--data-dir", type=Path, default=Path("data/live"))
    args = parser.parse_args(argv)

    def emit(value):
        stdout.write(json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n")
        stdout.flush()

    try:
        store = NotificationStore(args.data_dir)
        if args.command in {"status", "show"}:
            emit(store.status(event_id=args.event_id if args.command == "show" else None))
            return 0
        if args.command == "resolve":
            store.resolve(args.event_id, outcome=DeliveryState(args.outcome), now_ms=SystemClock().now_ms())
            emit(store.status(event_id=args.event_id))
            return 0
        if args.command == "retry":
            store.retry_failed(args.event_id, now_ms=SystemClock().now_ms())
            emit(store.status(event_id=args.event_id))
            return 0
        if args.command == "reconcile-health":
            factory(args.data_dir).reconcile_health()
            emit({"health_history_gap_acknowledged": True})
            return 0
        if args.poll_seconds <= 0:
            raise NotificationError("poll seconds must be positive")
        # Validate all secret-bearing inputs before opening or advancing the outbox.
        transport = transport_factory(SmtpConfig.from_environment(os.environ if environment is None else environment)) if args.send else None
        alerts = factory(args.data_dir, review_seconds=args.review_seconds)
        alerts.initialize()
        while True:
            try:
                result = alerts.collect()
                result.update(dry_run=transport is None, delivery_attempts=alerts.deliver(transport) if transport else 0)
                result["delivery"] = alerts.store.status()
                emit(result)
                counts = result["delivery"].get("counts", {})
                code = 2 if transport and (counts.get("failed", 0) or counts.get("unknown", 0)) else 0
            except HealthSnapshotUnstableError:
                emit({"error_code": "health_snapshot_unstable", "retryable": True})
                code = 2
            except (HealthStateError, ObservationCorruptionError):
                alerts.source_unavailable()
                if transport:
                    alerts.deliver(transport)
                emit({"error_code": "notification_source_unavailable_or_cursor_gap", "retryable": True,
                      "action": "inspect source status; reconcile-health requires explicit acknowledgement of lost health history"})
                code = 2
            except (NotificationError, OSError, sqlite3.Error):
                emit({"error_code": "notification_state_or_delivery_error", "retryable": False})
                code = 2
            if args.once:
                return code
            sleeper(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    except NotificationError as exc:
        emit({"error_code": "notification_configuration_or_state_error", "message": str(exc)})
        return 2
    except (OSError, sqlite3.Error, HealthStateError):
        emit({"error_code": "notification_storage_or_health_error"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
