from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from mu_strategy.demo_trading import DEFAULT_WATCHLIST_SYMBOLS, DemoTradingConfig, run_once
from mu_strategy.live.okx import OKXCredentials, OKXRestClient
from mu_strategy.live.okx_cli import CREDENTIAL_SOURCE_CHOICES
from mu_strategy.viz.entry_dashboard import DEFAULT_REFRESH_SECONDS, write_entry_dashboard


Runner = Callable[[DemoTradingConfig, Any], dict[str, Any]]
DashboardWriter = Callable[..., Path]


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    runner: Runner = run_once,
    dashboard_writer: DashboardWriter = write_entry_dashboard,
) -> int:
    stdout = stdout or sys.stdout
    args = _build_parser().parse_args(argv)
    dry_run = args.dry_run or not args.confirm_demo_orders
    config = DemoTradingConfig(
        universe_limit=args.limit,
        days=args.days,
        data_dir=args.data_dir,
        refresh=args.refresh,
        notional_usdt=args.notional_usdt,
        max_open_positions=args.max_open_positions,
        leverage=args.leverage,
        dry_run=dry_run,
        watchlist_symbols=_watchlist_symbols(args),
    )
    broker = _build_broker(dry_run=dry_run, credential_source=args.credential_source)

    if args.once:
        payload = runner(config, broker=broker)
        stdout.write(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        stdout.write("\n")
        _write_dashboard_safely(
            payload,
            dashboard_output=args.dashboard_output,
            dashboard_refresh_seconds=args.dashboard_refresh_seconds,
            dashboard_writer=dashboard_writer,
            stderr=sys.stderr,
        )
        return 0

    _run_forever(
        config,
        broker=broker,
        interval_seconds=args.interval_seconds,
        runner=runner,
        stdout=stdout,
        stderr=sys.stderr,
        dashboard_output=args.dashboard_output,
        dashboard_refresh_seconds=args.dashboard_refresh_seconds,
        dashboard_writer=dashboard_writer,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan trusted manifest universe symbols and place guarded demo limit orders.")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Plan orders without private account calls or placement.")
    parser.add_argument("--confirm-demo-orders", action="store_true", help="Allow real OKX demo trading order placement.")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--notional-usdt", type=float, default=10.0)
    parser.add_argument("--max-open-positions", type=int, default=3)
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--credential-source", choices=CREDENTIAL_SOURCE_CHOICES)
    parser.add_argument(
        "--watchlist-symbol",
        action="append",
        default=[],
        help="Always scan this OKX swap symbol in addition to the Top universe. Can be repeated.",
    )
    parser.add_argument(
        "--no-default-watchlist",
        action="store_true",
        help="Disable the default fixed watchlist, including MU-USDT-SWAP.",
    )
    parser.add_argument("--dashboard-output", type=Path, help="Write an auto-refreshing HTML dashboard after each scan cycle.")
    parser.add_argument("--dashboard-refresh-seconds", type=int, default=DEFAULT_REFRESH_SECONDS)
    return parser


def _watchlist_symbols(args: argparse.Namespace) -> tuple[str, ...]:
    symbols: list[str] = []
    if not args.no_default_watchlist:
        symbols.extend(DEFAULT_WATCHLIST_SYMBOLS)
    symbols.extend(args.watchlist_symbol or [])
    return tuple(symbols)


def _build_broker(*, dry_run: bool, credential_source: str | None) -> OKXRestClient:
    credentials = None if dry_run else OKXCredentials.from_env(source=credential_source)
    return OKXRestClient(credentials=credentials, demo=True)


def _run_forever(
    config: DemoTradingConfig,
    *,
    broker: OKXRestClient,
    interval_seconds: int,
    runner: Runner,
    stdout: TextIO,
    stderr: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    dashboard_output: Path | None = None,
    dashboard_refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    dashboard_writer: DashboardWriter = write_entry_dashboard,
) -> None:
    stderr = stderr or sys.stderr
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    next_run_at = clock()
    while True:
        sleep_seconds = next_run_at - clock()
        if sleep_seconds > 0:
            sleeper(sleep_seconds)
        try:
            payload = runner(config, broker=broker)
        except Exception as exc:
            payload = _runner_failure_payload(exc)
        stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        stdout.write("\n")
        stdout.flush()
        _write_dashboard_safely(
            payload,
            dashboard_output=dashboard_output,
            dashboard_refresh_seconds=dashboard_refresh_seconds,
            dashboard_writer=dashboard_writer,
            stderr=stderr,
        )
        next_run_at += interval_seconds
        now = clock()
        while next_run_at <= now:
            next_run_at += interval_seconds


def _runner_failure_payload(exc: Exception) -> dict[str, str]:
    return {
        "mode": "cycle_failed",
        "reason": "runner_failed",
        "error_type": type(exc).__name__,
        "message": str(exc),
    }


def _write_dashboard_safely(
    payload: dict[str, Any],
    *,
    dashboard_output: Path | None,
    dashboard_refresh_seconds: int,
    dashboard_writer: DashboardWriter,
    stderr: TextIO,
) -> None:
    if dashboard_output is None:
        return
    try:
        dashboard_writer(payload, dashboard_output, refresh_seconds=dashboard_refresh_seconds)
    except Exception as exc:
        stderr.write(
            json.dumps(
                {
                    "mode": "dashboard_render_failed",
                    "reason": "dashboard_render_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "dashboard_output": str(dashboard_output),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        stderr.write("\n")
        stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
