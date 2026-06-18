from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from mu_strategy.demo_trading import DemoTradingConfig, run_once
from mu_strategy.live.okx import OKXCredentials, OKXRestClient
from mu_strategy.live.okx_cli import CREDENTIAL_SOURCE_CHOICES


Runner = Callable[[DemoTradingConfig, Any], dict[str, Any]]


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    runner: Runner = run_once,
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
    )
    broker = _build_broker(dry_run=dry_run, credential_source=args.credential_source)

    if args.once:
        payload = runner(config, broker=broker)
        stdout.write(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        stdout.write("\n")
        return 0

    _run_forever(config, broker=broker, interval_seconds=args.interval_seconds, runner=runner, stdout=stdout)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan OKX Top USDT-SWAP symbols and place guarded demo limit orders.")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Plan orders without private account calls or placement.")
    parser.add_argument("--confirm-demo-orders", action="store_true", help="Allow real OKX demo trading order placement.")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--notional-usdt", type=float, default=10.0)
    parser.add_argument("--max-open-positions", type=int, default=3)
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--credential-source", choices=CREDENTIAL_SOURCE_CHOICES)
    return parser


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
) -> None:
    while True:
        payload = runner(config, broker=broker)
        stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        stdout.write("\n")
        stdout.flush()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
