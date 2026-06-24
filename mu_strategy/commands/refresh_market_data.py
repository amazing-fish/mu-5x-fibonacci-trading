from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from mu_strategy.market_data.trusted_data.contracts import ManifestStatus, RefreshRun, RefreshRunOutcome
from mu_strategy.market_data.trusted_data.refresh import (
    DEFAULT_INTERVALS,
    RefreshTrustedMarketData,
    RefreshTrustedMarketDataRequest,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.viz.data_health import write_data_health_dashboard


REFRESH_COMMAND_UNUSABLE_EXIT_CODE = 1


@dataclass(frozen=True)
class RefreshCommandResult:
    run_id: str
    outcome: str
    manifest_status: str
    usable: bool
    exit_code: int
    symbol_count: int
    warnings: tuple[str, ...] = ()
    cycle_error: dict[str, str] | None = None
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "status": self.manifest_status,
            "usable": self.usable,
            "symbols": self.symbol_count,
        }
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        if self.cycle_error is not None:
            payload["cycle_error"] = dict(self.cycle_error)
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.message is not None:
            payload["message"] = self.message
        return payload


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.loop and args.interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    intervals = tuple(args.interval or DEFAULT_INTERVALS)

    while True:
        exit_code = 0
        try:
            run = _refresh_once(args, intervals=intervals)
            manifest = run.to_manifest()
            if args.html_output is not None:
                write_data_health_dashboard(manifest, args.html_output)
            command_result = classify_refresh_run(run)
            output = command_result.to_dict()
            exit_code = command_result.exit_code
        except Exception as exc:
            output = {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "symbols": 0,
            }
            exit_code = REFRESH_COMMAND_UNUSABLE_EXIT_CODE
        stdout.write(json.dumps(output, sort_keys=True))
        stdout.write("\n")
        stdout.flush()
        if not args.loop:
            return exit_code
        time.sleep(args.interval_seconds)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh trusted OKX market data and write data-health artifacts.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--stock-token-config", type=Path, default=Path("config/okx_stock_tokens.json"))
    parser.add_argument("--limit", type=int, default=10, help="Canonical universe limit per Top bucket. Must be non-negative.")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--interval", action="append", choices=DEFAULT_INTERVALS)
    parser.add_argument("--html-output", type=Path, default=Path("reports/live/data_health.html"))
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=300)
    return parser


def _refresh_once(args: argparse.Namespace, *, intervals: tuple[str, ...]) -> RefreshRun:
    return RefreshTrustedMarketData(TrustedDataStore(data_dir=args.data_dir)).execute(
        RefreshTrustedMarketDataRequest(
            requested_intervals=intervals,
            days=args.days,
            limit=args.limit,
            stock_token_config=args.stock_token_config,
        )
    )


def classify_refresh_run(run: RefreshRun) -> RefreshCommandResult:
    status = run.manifest_status()
    usable = run.outcome == RefreshRunOutcome.SUCCESS and status == ManifestStatus.OK.value
    reason = None if usable else "publication_not_usable"
    message = None
    if not usable:
        message = f"trusted publication is not usable: outcome={run.outcome.value}, status={status}"
    return RefreshCommandResult(
        run_id=run.run_id,
        outcome=run.outcome.value,
        manifest_status=status,
        usable=usable,
        exit_code=0 if usable else REFRESH_COMMAND_UNUSABLE_EXIT_CODE,
        symbol_count=len({symbol for symbol, _ in run.datasets}),
        warnings=tuple(run.warnings),
        cycle_error=run.cycle_error,
        reason=reason,
        message=message,
    )


if __name__ == "__main__":
    raise SystemExit(main())
