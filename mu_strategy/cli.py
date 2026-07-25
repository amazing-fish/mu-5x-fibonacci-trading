from __future__ import annotations

import argparse
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.market_data.service import refresh_trusted_candle_bundle
from mu_strategy.market_data.trusted_data.compat import trusted_bundle_error
from mu_strategy.reporting import candle_sample_summary, render_markdown_report
from mu_strategy.strategies.registry import selected_strategy_groups
from mu_strategy.strategy import FEE_PROFILE_CHOICES, with_fee_profile

TRUSTED_REQUESTED_INTERVALS = ("15m", "1h")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the MU 5x Fibonacci strategy from trusted cached market data.")
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--data-dir", type=Path, help="Trusted data store directory. Defaults to data/live.")
    parser.add_argument("--report", type=Path, default=Path("reports/live/mu_okx_backtest.md"))
    parser.add_argument("--strategy", default="baseline", help="Single strategy group name to backtest.")
    parser.add_argument(
        "--fee-profile",
        choices=FEE_PROFILE_CHOICES,
        default="market",
        help="Backtest cost assumption: market/taker=0.0500%%, limit/maker=0.0200%%.",
    )
    args = parser.parse_args()

    try:
        groups = selected_strategy_groups(args.symbol, [args.strategy])
    except ValueError as exc:
        parser.error(str(exc))
    if len(groups) != 1:
        parser.error("--strategy must resolve to exactly one strategy group")
    config = with_fee_profile(groups[0].config, args.fee_profile)
    data_dir = args.data_dir or Path("data/live")
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
    file_15m = bundle.files_by_interval["15m"]
    file_1h = bundle.files_by_interval["1h"]
    context = build_hourly_context(candles_15m, candles_1h)
    result = run_backtest(candles_15m, context, config=config)
    report = render_markdown_report(
        result,
        config=config,
        symbol=args.symbol,
        data_files=[file_15m, file_1h],
        sample_summary=candle_sample_summary({"15m": candles_15m, "1h": candles_1h}),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report)

if __name__ == "__main__":
    main()
