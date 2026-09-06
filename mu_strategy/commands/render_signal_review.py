from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from mu_strategy.market_data.trusted_data.contracts import SystemClock
from mu_strategy.signal_review import read_signal_review, review_window, validate_review_output
from mu_strategy.viz.signal_review import render_signal_review


def main(argv=None, *, clock=None, stdout=None) -> int:
    import sys
    stdout = stdout or sys.stdout
    clock = clock or SystemClock()
    parser = argparse.ArgumentParser(description="Render a read-only Chinese signal review from existing local evidence.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--output", type=Path, default=Path("reports/live/signal-review.html"))
    parser.add_argument("--days", type=int, default=7, help="Calendar days ending on --to-date, default 7.")
    parser.add_argument("--from-date", help="First included Beijing date, YYYY-MM-DD.")
    parser.add_argument("--to-date", help="Last included Beijing date, YYYY-MM-DD; default today.")
    parser.add_argument("--serve", action="store_true", help="Open a loopback-only live viewer instead of writing a static report.")
    parser.add_argument("--port", type=int, default=8769, help="Loopback port for --serve, default 8769.")
    args = parser.parse_args(argv)
    try:
        window = review_window(now_ms=clock.now_ms(), days=args.days, from_date=args.from_date, to_date=args.to_date)
        if args.serve:
            if not 1 <= args.port <= 65535:
                raise ValueError("invalid local port")
        else:
            validate_review_output(args.data_dir, args.output)
    except (ValueError, OverflowError, OSError):
        parser.error("invalid report path, Beijing date window or port; use an .html output outside data/service state, 1–366 days and port 1–65535")
    if args.serve:
        from mu_strategy.signal_review_server import serve_signal_review
        return serve_signal_review(args.data_dir, port=args.port, days=args.days,
                                   from_date=args.from_date, to_date=args.to_date, clock=clock, stdout=stdout)
    report = read_signal_review(args.data_dir, window, clock=clock)
    content = render_signal_review(report)
    temporary = None
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent,
                                         prefix="signal-review-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, args.output)
    except OSError:
        stdout.write(json.dumps({"error_code": "report_write_failed"}) + "\n")
        return 2
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    stdout.write(json.dumps({
        "output": str(args.output.resolve()), "sources_readable": report["sources_readable"],
        "source_states": {key: source["state"] for key, source in report["sources"].items()},
        "window": window,
    }, ensure_ascii=True) + "\n")
    return 0 if report["sources_readable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
