from __future__ import annotations

import argparse
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.indicators import ema, macd, rsi
from mu_strategy.market_data.service import refresh_candle_bundle, refresh_trusted_candle_bundle
from mu_strategy.market_data.trusted_data.compat import trusted_bundle_error
from mu_strategy.models import Candle
from mu_strategy.reporting import candle_sample_summary, render_markdown_report
from mu_strategy.strategy import FEE_PROFILE_CHOICES, one_hour_regime, selected_strategy_groups, with_fee_profile

TRUSTED_REQUESTED_INTERVALS = ("15m", "1h")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the MU 5x Fibonacci strategy.")
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--source", choices=("binance", "okx"), default="okx")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh legacy cached_historical data. Not supported with --trusted-data; run python -m mu_strategy.commands.refresh_market_data first.",
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--trusted-data",
        action="store_true",
        help="Use the trusted OKX cache-only data layer instead of legacy cached_historical.",
    )
    parser.add_argument("--report", type=Path, default=Path("reports/mu_okx_backtest.md"))
    parser.add_argument("--strategy", default="baseline", help="Single strategy group name to backtest.")
    parser.add_argument(
        "--fee-profile",
        choices=FEE_PROFILE_CHOICES,
        default="market",
        help="Backtest cost assumption: market/taker=0.0500%%, limit/maker=0.0200%%.",
    )
    args = parser.parse_args()
    if args.trusted_data and args.refresh:
        parser.error(
            "--trusted-data --refresh is not supported; run "
            "python -m mu_strategy.commands.refresh_market_data before loading trusted data"
        )

    try:
        groups = selected_strategy_groups(args.symbol, [args.strategy])
    except ValueError as exc:
        parser.error(str(exc))
    if len(groups) != 1:
        parser.error("--strategy must resolve to exactly one strategy group")
    config = with_fee_profile(groups[0].config, args.fee_profile)
    data_dir = args.data_dir
    if data_dir is None:
        data_dir = Path("data/live") if args.trusted_data else Path("data")
    if args.trusted_data:
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
    else:
        bundle = refresh_candle_bundle(
            args.symbol,
            intervals=("15m", "1h"),
            days=args.days,
            data_dir=data_dir,
            refresh=args.refresh,
            source=args.source,
        )
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


def build_hourly_context(candles_15m: list[Candle], candles_1h: list[Candle]) -> dict[int, str]:
    if not candles_1h:
        return {bar.open_time_ms: "yellow" for bar in candles_15m}

    closes = [bar.close for bar in candles_1h]
    ema21_values = ema(closes, 21)
    rsi_values = rsi(closes, 14)
    _, _, hist_values = macd(closes)

    hourly_states: list[tuple[int, str]] = []
    for index, candle in enumerate(candles_1h):
        previous_hist = hist_values[index - 1] if index > 0 else hist_values[index]
        state = one_hour_regime(candle.close, ema21_values[index], rsi_values[index], hist_values[index], previous_hist)
        hourly_states.append((candle.open_time_ms, state))

    context: dict[int, str] = {}
    cursor = 0
    current_state = "yellow"
    for bar in candles_15m:
        while cursor < len(hourly_states) and hourly_states[cursor][0] <= bar.open_time_ms:
            current_state = hourly_states[cursor][1]
            cursor += 1
        context[bar.open_time_ms] = current_state
    return context


if __name__ == "__main__":
    main()
