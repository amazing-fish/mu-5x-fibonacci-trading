from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, RefreshRun, SnapshotUsability
from mu_strategy.market_data.trusted_data.refresh import (
    DEFAULT_INTERVALS,
    RefreshTrustedMarketData,
    RefreshTrustedMarketDataRequest,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.viz.data_health import write_data_health_dashboard


REFRESH_COMMAND_UNUSABLE_EXIT_CODE = 1
REFRESH_COMMAND_DEGRADED_USABLE_EXIT_CODE = 2


@dataclass(frozen=True)
class RefreshCommandResult:
    run_id: str
    attempt_status: str
    snapshot_usability: str
    usable: bool
    exit_code: int
    symbol_count: int
    fetch_mode: str
    requested_symbols: tuple[str, ...] = ()
    provider_failures: tuple[dict[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    cycle_error: dict[str, str] | None = None
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "attempt_status": self.attempt_status,
            "snapshot_usability": self.snapshot_usability,
            "usable": self.usable,
            "symbols": self.symbol_count,
            "fetch_mode": self.fetch_mode,
        }
        if self.requested_symbols:
            payload["requested_symbols"] = list(self.requested_symbols)
        if self.provider_failures:
            payload["provider_failures"] = [dict(value) for value in self.provider_failures]
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
        except Exception as exc:
            output = {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "symbols": 0,
            }
            exit_code = REFRESH_COMMAND_UNUSABLE_EXIT_CODE
        else:
            command_result = classify_refresh_run(run)
            dashboard_warnings = _write_dashboard_or_warning(run.to_manifest(), args.html_output)
            if dashboard_warnings:
                command_result = replace(
                    command_result,
                    warnings=(*command_result.warnings, *dashboard_warnings),
                )
            output = command_result.to_dict()
            exit_code = command_result.exit_code
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
    parser.add_argument(
        "--symbol",
        dest="symbols",
        action="append",
        default=[],
        help=(
            "Repeatable explicit OKX swap symbol subset such as MU or MU-USDT-SWAP. "
            "When provided, refresh only these symbols, skip the Top universe ticker list, "
            "and keep trusted consumers cache-only."
        ),
    )
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
            symbols=tuple(args.symbols or ()),
            stock_token_config=args.stock_token_config,
        )
    )


def _write_dashboard_or_warning(manifest: dict[str, Any], html_output: Path | None) -> tuple[str, ...]:
    if html_output is None:
        return ()
    try:
        write_data_health_dashboard(manifest, html_output)
    except Exception as exc:
        return (f"dashboard_write_failed: {exc}",)
    return ()


def classify_refresh_run(run: RefreshRun) -> RefreshCommandResult:
    usable = run.snapshot_usability == SnapshotUsability.USABLE
    if run.attempt_status == RefreshAttemptStatus.SUCCESS and usable:
        exit_code = 0
    elif run.attempt_status == RefreshAttemptStatus.DEGRADED and usable:
        exit_code = REFRESH_COMMAND_DEGRADED_USABLE_EXIT_CODE
    else:
        exit_code = REFRESH_COMMAND_UNUSABLE_EXIT_CODE
    reason = None if exit_code == 0 else "publication_not_fully_healthy"
    message = None
    if exit_code != 0:
        message = (
            "trusted publication is not fully healthy: "
            f"attempt_status={run.attempt_status.value}, snapshot_usability={run.snapshot_usability.value}"
        )
    return RefreshCommandResult(
        run_id=run.run_id,
        attempt_status=run.attempt_status.value,
        snapshot_usability=run.snapshot_usability.value,
        usable=usable,
        exit_code=exit_code,
        symbol_count=len({symbol for symbol, _ in run.datasets}),
        fetch_mode=_fetch_mode(run),
        requested_symbols=_explicit_symbols_from_run(run),
        provider_failures=run.provider_failures,
        warnings=tuple(run.warnings),
        cycle_error=run.cycle_error,
        reason=reason,
        message=message,
    )


def _fetch_mode(run: RefreshRun) -> str:
    return "explicit_symbols" if _explicit_symbols_from_run(run) else "top_universe"


def _explicit_symbols_from_run(run: RefreshRun) -> tuple[str, ...]:
    symbols: list[str] = []
    seen: set[str] = set()
    for row in [*run.universe_snapshot.crypto_top, *run.universe_snapshot.stock_token_top]:
        if row.get("source") != "explicit":
            continue
        symbol = str(row.get("inst_id") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return tuple(symbols)


if __name__ == "__main__":
    raise SystemExit(main())
