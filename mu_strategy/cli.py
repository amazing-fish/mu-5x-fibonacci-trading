from __future__ import annotations

import argparse
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.market_data.service import refresh_trusted_candle_bundle
from mu_strategy.market_data.trusted_data.compat import trusted_bundle_error
from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.research.historical_data import (
    HistoricalGenerationError,
    load_historical_window,
    replay_markdown,
    validate_replay_outputs,
)
from mu_strategy.research.strategy_releases import StrategyConfigPayloadV2
from mu_strategy.reporting import candle_sample_summary, render_markdown_report
from mu_strategy.strategies.registry import selected_strategy_groups, strategy_group_registrations
from mu_strategy.strategy import FEE_PROFILE_CHOICES, with_fee_profile

TRUSTED_REQUESTED_INTERVALS = ("15m", "1h")


def format_strategy_list() -> str:
    lines: list[str] = []
    for registration in strategy_group_registrations():
        if not registration.selectable:
            continue
        tags: list[str] = []
        if registration.is_default:
            tags.append("default")
        if registration.aliases:
            tags.append(f"aliases: {', '.join(registration.aliases)}")
        suffix = f" ({'; '.join(tags)})" if tags else ""
        lines.append(f"{registration.name}{suffix} - {registration.label}")
    return "\n".join(lines)


def _available_strategy_names() -> str:
    return ", ".join(
        registration.name
        for registration in strategy_group_registrations()
        if registration.selectable
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the MU 5x Fibonacci strategy from trusted cached market data.")
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--generation-id", help="Explicit trusted historical generation; research replay only.")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument(
        "--strict-coverage",
        action="store_true",
        help="Fail closed when --generation-id has fewer complete days than --days instead of replaying partial available history.",
    )
    parser.add_argument("--data-dir", type=Path, help="Trusted data store directory. Defaults to data/live.")
    parser.add_argument("--report", type=Path, default=Path("reports/live/mu_okx_backtest.md"))
    parser.add_argument(
        "--strategy",
        default="baseline",
        help="Single strategy group name to backtest (use --list-strategies to inspect registered names).",
    )
    parser.add_argument(
        "--list-strategies",
        action="store_true",
        help="List all registered selectable strategy names and exit.",
    )
    parser.add_argument(
        "--fee-profile",
        choices=FEE_PROFILE_CHOICES,
        default="market",
        help="Backtest cost assumption: market/taker=0.0500%%, limit/maker=0.0200%%.",
    )
    args = parser.parse_args()

    if args.list_strategies:
        print(format_strategy_list())
        return

    try:
        groups = selected_strategy_groups(args.symbol, [args.strategy])
    except ValueError as exc:
        parser.error(f"{exc}. Available strategies: {_available_strategy_names()} (use --list-strategies for details)")
    if len(groups) != 1:
        parser.error("--strategy must resolve to exactly one strategy group")
    config = with_fee_profile(groups[0].config, args.fee_profile)
    data_dir = args.data_dir or Path("data/live")
    provenance = None
    if args.generation_id is not None:
        try:
            validate_replay_outputs(data_dir, args.report)
            window = load_historical_window(
                data_dir=data_dir,
                generation_id=args.generation_id,
                symbol=resolve_okx_swap_symbol(args.symbol).inst_id,
                days=args.days,
                allow_partial=not args.strict_coverage,
            )
            provenance = window.provenance({
                "strategy": args.strategy,
                "strategy_config": StrategyConfigPayloadV2.from_config(config).to_dict(),
                "days": args.days,
                "starting_equity": 10000,
                "slippage": "not modeled",
                "partial_fills": "not modeled",
            })
        except HistoricalGenerationError as exc:
            parser.error(str(exc))
        candles_15m = list(window.candles_by_interval["15m"])
        candles_1h = list(window.candles_by_interval["1h"])
        data_files = list(window.data_files)
    else:
        bundle = refresh_trusted_candle_bundle(
            args.symbol,
            intervals=TRUSTED_REQUESTED_INTERVALS,
            days=args.days,
            data_dir=data_dir,
            refresh=False,
        )
        status_error = trusted_bundle_error(bundle, requested_intervals=TRUSTED_REQUESTED_INTERVALS)
        if status_error:
            parser.error(status_error)
        candles_15m = bundle.candles_by_interval["15m"]
        candles_1h = bundle.candles_by_interval["1h"]
        data_files = [bundle.files_by_interval["15m"], bundle.files_by_interval["1h"]]
    context = build_hourly_context(candles_15m, candles_1h)
    result = run_backtest(candles_15m, context, config=config)
    report = render_markdown_report(
        result,
        config=config,
        symbol=args.symbol,
        data_files=data_files,
        sample_summary=candle_sample_summary({"15m": candles_15m, "1h": candles_1h}),
        candles=candles_15m,
    )
    report += replay_markdown(provenance)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report)

if __name__ == "__main__":
    main()
