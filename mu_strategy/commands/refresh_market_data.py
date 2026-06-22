from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TextIO

from mu_strategy.market_data.trusted_data.refresh import (
    DEFAULT_INTERVALS,
    RefreshTrustedMarketData,
    RefreshTrustedMarketDataRequest,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.viz.data_health import write_data_health_dashboard


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    args = _build_parser().parse_args(argv)
    if args.loop and args.interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    intervals = tuple(args.interval or DEFAULT_INTERVALS)

    while True:
        try:
            manifest = _refresh_once(args, intervals=intervals)
            if args.html_output is not None:
                write_data_health_dashboard(manifest, args.html_output)
            output = {"status": manifest["status"], "symbols": len(manifest["symbols"])}
        except Exception as exc:
            if not args.loop:
                raise
            output = {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "symbols": 0,
            }
        stdout.write(json.dumps(output, sort_keys=True))
        stdout.write("\n")
        stdout.flush()
        if not args.loop:
            return 0
        time.sleep(args.interval_seconds)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh trusted OKX market data and write data-health artifacts.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--stock-token-config", type=Path, default=Path("config/okx_stock_tokens.json"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--interval", action="append", choices=DEFAULT_INTERVALS)
    parser.add_argument("--html-output", type=Path, default=Path("reports/live/data_health.html"))
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=300)
    return parser


def _refresh_once(args: argparse.Namespace, *, intervals: tuple[str, ...]) -> dict:
    run = RefreshTrustedMarketData(TrustedDataStore(data_dir=args.data_dir)).execute(
        RefreshTrustedMarketDataRequest(
            requested_intervals=intervals,
            days=args.days,
            limit=args.limit,
            stock_token_config=args.stock_token_config,
        )
    )
    return run.to_manifest()


if __name__ == "__main__":
    raise SystemExit(main())
