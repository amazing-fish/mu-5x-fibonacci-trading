"""Read-only Phase 2 Strategy Discovery Workbench CLI (strictly dry-run)."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from mu_strategy.core.instrument import CostModel, FixedTicks, InstrumentSpec, okx_swap_spec, spot_spec
from mu_strategy.data import read_csv
from mu_strategy.market_data.bar_series import BarSeries
from mu_strategy.market_data.funding import FundingSeries
from mu_strategy.market_data.trusted_data.contracts import AvailabilityState, IntegrityState, ManifestSchemaError
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.market_data.utils import DAY_MS
from mu_strategy.strategy import FEE_PROFILE_CHOICES
from mu_strategy.lab.workbench_html import write_workbench_html


def _parse_csv_tokens(raw: str) -> tuple[str, ...]:
    tokens = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not tokens:
        raise ValueError("expected at least one comma-separated value")
    return tokens


def _guard_output_not_in_data_dir(output_path: Path | None, data_dir: Path | None) -> None:
    if output_path is None or data_dir is None:
        return
    resolved_out = output_path.resolve(strict=False)
    resolved_data = data_dir.resolve(strict=False)
    if resolved_out == resolved_data or resolved_data in resolved_out.parents:
        raise ValueError(f"refusing to write output inside read-only data directory: {output_path}")


def _build_instrument(symbol: str) -> InstrumentSpec:
    upper = symbol.upper()
    if upper.endswith("-SWAP") or upper.endswith("PERP"):
        return okx_swap_spec(symbol)
    return spot_spec(symbol, venue="okx")


def _build_cost_model(fee_profile: str) -> CostModel:
    if fee_profile == "limit":
        return CostModel(
            maker_fee_bps=2.0,
            taker_fee_bps=2.0,
            half_spread_bps=0.5,
            slippage_model=FixedTicks(0.0),
            stop_slippage_multiplier=1.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
        )
    return CostModel.okx_default()


def _load_bar_series_readonly(
    *,
    symbol: str,
    interval: str,
    data_dir: Path,
    candles_file: Path | None,
    generation_id: str | None,
    days: int | None,
) -> BarSeries:
    """Load candles strictly in memory with ZERO disk writes, ZERO .bar_cache, and ZERO network calls."""
    if candles_file is not None:
        candles = read_csv(candles_file)
    else:
        store = TrustedDataStore(data_dir=data_dir)
        manifest_result = (
            store.read_generation_manifest(generation_id)
            if generation_id is not None
            else store.read_manifest()
        )
        if (
            not manifest_result.ok
            or manifest_result.snapshot is None
            or manifest_result.generation_id is None
        ):
            detail = manifest_result.message or (
                manifest_result.reason.value if manifest_result.reason is not None else "manifest unreadable"
            )
            raise ValueError(f"cannot load trusted market data from {data_dir}: {detail}")
        snapshot = manifest_result.snapshot
        if snapshot.attempt_status == "failed" or snapshot.snapshot_usability == "invalid":
            raise ValueError(f"trusted generation {manifest_result.generation_id} is unusable")
        health = snapshot.datasets.get((symbol, interval))
        if health is None:
            raise ValueError(f"dataset ({symbol}, {interval}) not found in trusted manifest")
        if health.availability is not AvailabilityState.AVAILABLE or health.integrity is not IntegrityState.VALID:
            raise ValueError(
                f"trusted dataset ({symbol}, {interval}) is unavailable or invalid "
                f"(availability={health.availability.value}, integrity={health.integrity.value})"
            )
        candles, _ = store.read_generation_dataset_with_hash(
            snapshot,
            symbol=symbol,
            interval=interval,
            generation_root=manifest_result.generation_root or store.data_dir,
            generation_id=manifest_result.generation_id,
            verify_logical=True,
            compute_hash=True,
        )
    if not candles:
        raise ValueError(f"no candles loaded for {symbol} ({interval})")
    if days is not None:
        if days <= 0:
            raise ValueError("--days must be positive")
        cutoff_ms = candles[-1].open_time_ms - (days * DAY_MS)
        candles = [c for c in candles if c.open_time_ms >= cutoff_ms]
        if not candles:
            raise ValueError(f"no candles within requested {days}-day window")
    return BarSeries.from_candles(symbol, interval, candles)


def _format_summary_table(report_dict: Mapping[str, Any]) -> str:
    symbol = report_dict.get("symbol", "UNKNOWN")
    interval = report_dict.get("interval", "1h")
    oos_days = float(report_dict.get("oos_days") or 0.0)
    n_folds = int(report_dict.get("n_folds") or 0)
    n_trials = int(report_dict.get("n_trials") or 0)
    runtime_ms = float(report_dict.get("runtime_ms") or 0.0)
    candidates = report_dict.get("candidates") or []

    verdict_counts = {"GO": 0, "CONDITIONAL": 0, "NO_GO": 0}
    for cand in candidates:
        gates = cand.get("gates") if isinstance(cand, Mapping) else None
        verdict = str((gates or {}).get("verdict") or "NO_GO")
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    lines = [
        f"Phase 2 Strategy Discovery Summary | {symbol} ({interval}) | DRY-RUN ONLY",
        (
            f"OOS={oos_days:.1f}d | Folds={n_folds} | Trials={n_trials} | "
            f"GO={verdict_counts['GO']} CONDITIONAL={verdict_counts['CONDITIONAL']} "
            f"NO_GO={verdict_counts['NO_GO']} | Runtime={runtime_ms:.0f}ms"
        ),
    ]
    benchmarks = report_dict.get("benchmarks")
    if isinstance(benchmarks, Mapping):
        bench_parts: list[str] = []
        for key, label in (("spot_1x", "Spot1x"), ("perp_1x", "Perp1x")):
            b = benchmarks.get(key)
            if isinstance(b, Mapping):
                bm = b.get("metrics") if isinstance(b.get("metrics"), Mapping) else b
                ret = float(bm.get("total_return") or 0.0) * 100.0
                dd = float(bm.get("max_drawdown") or 0.0) * 100.0
                sh = float(bm.get("sharpe") or 0.0)
                bench_parts.append(f"{label}: ret={ret:+.2f}% dd={dd:.2f}% sharpe={sh:.2f}")
        if bench_parts:
            lines.append("Benchmarks | " + " | ".join(bench_parts))

    header = (
        f"{'Rank':>4} {'Family':<20} {'Overlay':<12} {'OOS Ret':>9} "
        f"{'MaxDD':>8} {'Sharpe':>7} {'DSR':>6} {'Calmar':>7} {'Trd':>4} {'Liq':>3} {'Verdict':<11}"
    )
    lines.append("-" * len(header))
    lines.append(header)
    lines.append("-" * len(header))
    for idx, cand in enumerate(candidates[:15], start=1):
        m = cand.get("metrics") if isinstance(cand.get("metrics"), Mapping) else cand
        g = cand.get("gates") if isinstance(cand.get("gates"), Mapping) else {}
        rank = int(cand.get("rank") or idx)
        family = str(cand.get("family_id") or "unknown")[:20]
        overlay = str(cand.get("overlay_preset") or "none")[:12]
        ret = float(m.get("oos_total_return") if m.get("oos_total_return") is not None else (m.get("total_return") or 0.0)) * 100.0
        dd = float(m.get("oos_max_drawdown") if m.get("oos_max_drawdown") is not None else (m.get("max_drawdown") or 0.0)) * 100.0
        sharpe = float(m.get("oos_sharpe") if m.get("oos_sharpe") is not None else (m.get("sharpe") or 0.0))
        dsr = float(g.get("dsr") if g.get("dsr") is not None else (m.get("dsr") or 0.0))
        calmar = float(m.get("oos_calmar") if m.get("oos_calmar") is not None else (m.get("calmar") or 0.0))
        trades = int(m.get("oos_trades") if m.get("oos_trades") is not None else (m.get("trades") or m.get("trade_count") or 0))
        liqs = int(m.get("liquidations") if m.get("liquidations") is not None else (m.get("liquidation_count") or 0))
        verdict = str(g.get("verdict") or "NO_GO")
        lines.append(
            f"{rank:>4d} {family:<20} {overlay:<12} {ret:>+8.2f}% "
            f"{dd:>7.2f}% {sharpe:>7.2f} {dsr:>6.3f} {calmar:>7.2f} {trades:>4d} {liqs:>3d} {verdict:<11}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the Phase 2 Strategy Discovery CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="mu_strategy.lab.cli",
        description=(
            "Phase 2 Strategy Discovery Workbench CLI (strictly read-only dry-run; "
            "fee profiles: market/taker=0.0500%%, limit/maker=0.0200%%)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")
    discover = subparsers.add_parser(
        "discover",
        help="Run walk-forward 6-gate strategy discovery and optionally render offline HTML/JSON artifacts.",
        description="Run read-only walk-forward 6-gate strategy discovery (zero live orders, zero data writes).",
    )
    discover.add_argument("--data-dir", type=Path, default=Path("data/live"), help="Read-only trusted market data directory.")
    discover.add_argument("--candles-file", type=Path, default=None, help="Optional CSV candles file (overrides --data-dir).")
    discover.add_argument("--generation-id", default=None, help="Explicit trusted historical generation ID (deterministic replay).")
    discover.add_argument("--days", type=int, default=None, help="Optional trailing day window to evaluate.")
    discover.add_argument("--funding-file", type=Path, default=None, help="Optional historical funding JSON archive.")
    discover.add_argument("--symbol", default="MU-USDT-SWAP", help="Instrument symbol (default: MU-USDT-SWAP).")
    discover.add_argument("--interval", default="1h", help="Bar interval (default: 1h).")
    discover.add_argument("--families", default="all", help="Comma-separated strategy family IDs or 'all'.")
    discover.add_argument("--overlays", default="none,dd20_vol25", help="Comma-separated risk overlay presets.")
    discover.add_argument("--folds", type=int, default=3, help="Number of purged walk-forward folds (default: 3).")
    discover.add_argument("--purge-bars", type=int, default=24, help="Purge gap bars between IS and OOS (default: 24).")
    discover.add_argument(
        "--fee-profile",
        choices=FEE_PROFILE_CHOICES,
        default="market",
        help="Backtest cost assumption: market/taker=0.0500%%, limit/maker=0.0200%%.",
    )
    discover.add_argument("--output-html", type=Path, default=None, help="Write standalone zero-CDN HTML workbench.")
    discover.add_argument("--output-json", type=Path, default=None, help="Write full DiscoveryReport JSON.")
    discover.add_argument("--recipe-out", type=Path, default=None, help="Write dry-run winner recipe JSON.")
    discover.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Strict read-only research mode (default: True; live orders are forbidden).",
    )
    return parser


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _run_discover(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from mu_strategy.lab.discovery import (
        DiscoveryConfig,
        default_strategy_catalog,
        discover_strategies,
        export_winner_recipe,
        overlay_presets,
    )

    if not args.dry_run:
        parser.error("live order execution is forbidden in mu_strategy.lab.cli; --dry-run must remain enabled")

    guard_dir = None if args.candles_file is not None else args.data_dir
    try:
        for out_path in (args.output_html, args.output_json, args.recipe_out):
            _guard_output_not_in_data_dir(out_path, guard_dir)
        selected_overlays = _parse_csv_tokens(args.overlays)
        available_overlays = overlay_presets()
        unknown_overlays = [name for name in selected_overlays if name not in available_overlays]
        if unknown_overlays:
            raise ValueError(
                f"unknown overlay preset(s): {', '.join(unknown_overlays)} "
                f"(available: {', '.join(sorted(available_overlays))})"
            )

        instrument = _build_instrument(args.symbol)
        catalog = default_strategy_catalog(
            symbol=args.symbol,
            interval=args.interval,
            allow_short=instrument.is_perp,
        )
        if args.families.strip().lower() != "all":
            requested = _parse_csv_tokens(args.families)
            by_id = {entry.family_id: entry for entry in catalog}
            unknown_families = [name for name in requested if name not in by_id]
            if unknown_families:
                raise ValueError(
                    f"unknown strategy family(ies): {', '.join(unknown_families)} "
                    f"(available: {', '.join(sorted(by_id))})"
                )
            catalog = tuple(by_id[name] for name in requested)

        series = _load_bar_series_readonly(
            symbol=args.symbol,
            interval=args.interval,
            data_dir=args.data_dir,
            candles_file=args.candles_file,
            generation_id=args.generation_id,
            days=args.days,
        )
        cost_model = _build_cost_model(args.fee_profile)
        funding = (
            FundingSeries.from_json_file(args.funding_file, instrument_id=instrument.instrument_id)
            if args.funding_file is not None
            else None
        )
        config = DiscoveryConfig(
            n_folds=args.folds,
            purge_bars=args.purge_bars,
            overlay_names=selected_overlays,
        )
        report = discover_strategies(
            series,
            instrument,
            cost_model,
            funding=funding,
            catalog=catalog,
            config=config,
        )
    except (ValueError, KeyError, FileNotFoundError, ManifestSchemaError) as exc:
        parser.error(str(exc))

    report_dict = report.to_dict()
    recipe = export_winner_recipe(report)
    if isinstance(recipe, Mapping):
        recipe_payload = dict(recipe)
        recipe_payload["dry_run_only"] = True
        recipe_payload["live_orders_allowed"] = False
    else:
        recipe_payload = {"dry_run_only": True, "live_orders_allowed": False}

    if args.output_json is not None:
        _write_json(args.output_json, report_dict)
    if args.recipe_out is not None:
        _write_json(args.recipe_out, recipe_payload)
    if args.output_html is not None:
        write_workbench_html(report_dict, args.output_html, recipe=recipe_payload)

    print(_format_summary_table(report_dict))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m mu_strategy.lab.cli``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "discover":
        return _run_discover(args, parser)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
