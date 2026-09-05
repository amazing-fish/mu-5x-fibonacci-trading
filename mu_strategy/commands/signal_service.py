from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from mu_strategy.market_data.trusted_data.contracts import SystemClock
from mu_strategy.service_health import HealthSnapshotUnstableError, HealthStateError, HealthStore, ServiceBusyError, StepStatus, health_view
from mu_strategy.signal_service import InterruptedCycleError, ServiceConfig, SignalService, recover_interrupted, scan_once


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None, service_factory=SignalService, scanner=scan_once) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(description="Run trusted refresh and observation-only scans; query service health.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Refresh then scan continuously, or run one bounded cycle.")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval-seconds", type=int, default=300)
    run.add_argument("--refresh-timeout-seconds", type=int, default=240)
    run.add_argument("--scan-timeout-seconds", type=int, default=60)
    for command in (run, subparsers.add_parser("scan-once", help="Run a cache-only scan worker without broker calls.")):
        command.add_argument("--refresh-days", type=int, default=180)
        command.add_argument("--symbol", action="append")
        command.add_argument("--scan-days", type=int, default=28)
        command.add_argument("--data-dir", type=Path, default=Path("data/live"))
    status = subparsers.add_parser("status", help="Read health and detect stopped or overdue service processes.")
    status.add_argument("--data-dir", type=Path, default=Path("data/live"))
    status.add_argument("--after-event", type=int)
    recover = subparsers.add_parser("recover", help="Acknowledge an interrupted cycle after checking that workers stopped.")
    recover.add_argument("--data-dir", type=Path, default=Path("data/live"))
    recover.add_argument("--confirm-workers-stopped", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        store = HealthStore(args.data_dir)
        if args.command == "status":
            state, running = store.snapshot()
            payload = health_view(state, running=running, now_ms=SystemClock().now_ms())
            if args.after_event is not None:
                if state is None and args.after_event != 0:
                    raise HealthStateError("no event history available")
                payload["events"] = [event.to_dict() for event in state.events_since(args.after_event)] if state else []
            _emit(stdout, payload)
            return 0 if payload["healthy"] else 2
        if args.command == "recover":
            state = recover_interrupted(store)
            _emit(stdout, health_view(state, running=False, now_ms=SystemClock().now_ms()))
            return 0
        kwargs = {"data_dir": args.data_dir, "symbols": tuple(args.symbol or ("MU-USDT-SWAP",)),
                  "scan_days": args.scan_days, "refresh_days": args.refresh_days}
        if args.command == "run":
            kwargs.update(interval_seconds=args.interval_seconds, refresh_timeout_seconds=args.refresh_timeout_seconds,
                          scan_timeout_seconds=args.scan_timeout_seconds)
        config = ServiceConfig(**kwargs)
        if args.command == "scan-once":
            scan = scanner(config)
            _emit(stdout, {"schema_version": 1, "scan": scan.to_dict()})
            return 0 if scan.status is StepStatus.SUCCEEDED and scan.persistence is StepStatus.SUCCEEDED else 1
        store.prepare()
        handler = RotatingFileHandler(store.root / "service.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        logger = logging.Logger("mu-signal-service")
        logger.addHandler(handler)
        last_healthy = False

        def report(payload: dict) -> None:
            nonlocal last_healthy
            last_healthy = payload["healthy"]
            logger.info(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            _emit(stdout, payload)

        try:
            service_factory(config).run(cycles=1 if args.once else None, on_cycle=report)
        finally:
            handler.close()
        return 0 if last_healthy else 2
    except KeyboardInterrupt:
        return 0
    except ServiceBusyError:
        _emit(stdout, {"healthy": False, "error_code": "service_busy"})
        return 3
    except InterruptedCycleError:
        _emit(stdout, {"healthy": False, "error_code": "interrupted_cycle_requires_recovery"})
        return 2
    except HealthSnapshotUnstableError:
        _emit(stdout, {"runtime": "unavailable", "healthy": False, "error_code": "health_snapshot_unstable"})
        return 2
    except (HealthStateError, OSError, ValueError):
        _emit(stdout, {"healthy": False, "error_code": "health_or_configuration_error"})
        return 2


def _emit(stdout: TextIO, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
