from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

from mu_strategy.market_data.trusted_data.store import TrustedDataStore


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(
        description="Explicitly import one immutable flat schema-v3 trusted generation into schema v4 segments."
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--target-run-id", required=True)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Atomically replace current.json after the imported generation is fully verified.",
    )
    args = parser.parse_args(argv)

    try:
        result = TrustedDataStore(data_dir=args.data_dir).import_flat_generation(
            args.source_run_id,
            args.target_run_id,
            publish=args.publish,
        )
    except Exception as exc:
        payload = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        exit_code = 1
    else:
        snapshot = result.snapshot
        payload = {
            "status": "ok",
            "source_run_id": args.source_run_id,
            "target_run_id": args.target_run_id,
            "schema_version": snapshot.schema_version if snapshot is not None else None,
            "storage_layout": "segmented_csv_v1",
            "published": bool(args.publish),
            "datasets": len(snapshot.datasets) if snapshot is not None else 0,
        }
        exit_code = 0
    stdout.write(json.dumps(payload, sort_keys=True))
    stdout.write("\n")
    stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
