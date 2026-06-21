from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from mu_strategy.viz.entry_dashboard import DEFAULT_REFRESH_SECONDS, latest_payload_from_jsonl, write_entry_dashboard


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    args = _build_parser().parse_args(argv)
    payload = latest_payload_from_jsonl(args.log)
    output_path = write_entry_dashboard(
        payload,
        args.output,
        refresh_seconds=args.dashboard_refresh_seconds,
    )
    stdout.write(f"wrote {output_path}\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the latest OKX entry scanner JSONL payload as an HTML dashboard.")
    parser.add_argument("--log", type=Path, required=True, help="JSONL log written by okx_demo_loop.")
    parser.add_argument("--output", type=Path, required=True, help="Output HTML dashboard path.")
    parser.add_argument("--dashboard-refresh-seconds", type=int, default=DEFAULT_REFRESH_SECONDS)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
