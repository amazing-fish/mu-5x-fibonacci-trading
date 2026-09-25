"""Offline timing-edge gate for a registered strategy group."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path

from mu_strategy.market_data.trusted_data.compat import trusted_bundle_error
from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, load_trusted_candle_bundle
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.research.edge_gate import assess_symbol, render_json, render_markdown, stock_symbols, summarize_gate
from mu_strategy.strategies.registry import selected_strategy_groups
from mu_strategy.strategy import with_fee_profile


def _commit() -> str:
    root = Path(__file__).resolve().parents[2]
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed offline timing-edge assessment.")
    parser.add_argument("--strategy", default="baseline", help="One registered strategy group")
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--simulations", type=int, default=200)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--funding-annual", type=float, default=0.08)
    parser.add_argument("--z-threshold", type=float, default=2.0)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.days < 1 or args.simulations < 1:
        parser.error("days and simulations must be positive")
    if not math.isfinite(args.funding_annual) or args.funding_annual < 0:
        parser.error("funding-annual must be finite and non-negative")
    if not math.isfinite(args.z_threshold) or args.z_threshold < 0:
        parser.error("z-threshold must be finite and non-negative")
    try:
        # This reader pins one manifest generation for the entire universe.
        context = LoadTrustedBundle(TrustedDataStore(data_dir=args.data_dir)).open_context()
        symbols = stock_symbols(context)
        bundles = []
        for symbol in symbols:
            bundle = load_trusted_candle_bundle(
                symbol, intervals=("15m", "1h"), days=args.days,
                data_dir=args.data_dir, refresh=False, context=context,
            )
            error = trusted_bundle_error(bundle, requested_intervals=("15m", "1h"))
            if error:
                raise ValueError(f"{symbol}: {error}")
            bundles.append((symbol, bundle))
        rows = []
        for symbol, bundle in bundles:
            groups = selected_strategy_groups(symbol, [args.strategy])
            if len(groups) != 1:
                raise ValueError("strategy must select exactly one registered group")
            config = with_fee_profile(groups[0].config, "market")
            symbol_seed = int.from_bytes(hashlib.sha256(f"{args.seed}:{symbol}".encode()).digest()[:8], "big")
            rows.append(assess_symbol(
                symbol, bundle.candles_by_interval["15m"], config,
                simulations=args.simulations, seed=symbol_seed,
                funding_annual=args.funding_annual,
                hourly_candles=bundle.candles_by_interval["1h"],
            ))
        verdict = summarize_gate(rows, z_threshold=args.z_threshold)
        parameters = {
            "strategy": args.strategy, "days": args.days,
            "simulations_per_symbol": args.simulations, "fee_profile": "market",
            "taker_fee_per_side": 0.0005, "funding_annual": args.funding_annual,
            "z_threshold": args.z_threshold,
            "null_method": "UTC trading-day block bootstrap, centered log returns",
        }
        parameter_digest = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
        run_name = f"{context.generation_id}-{args.strategy}-seed{args.seed}-n{args.simulations}-{parameter_digest}"
        output = args.output_dir or Path("reports/live/edge_gate") / run_name
        if output.resolve().is_relative_to(args.data_dir.resolve()):
            raise ValueError("output-dir must not be within trusted data-dir")
        payload = {
            "commit": _commit(), "generation_id": context.generation_id,
            "seed": args.seed,
            "parameters": parameters,
            **verdict,
        }
        output.mkdir(parents=True, exist_ok=True)
        json_path = output / "edge_gate.json"
        markdown_path = output / "edge_gate.md"
        json_path.write_text(render_json(payload), encoding="utf-8", newline="\n")
        markdown_path.write_text(render_markdown(payload), encoding="utf-8", newline="\n")
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json_path.resolve())
    print(markdown_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
