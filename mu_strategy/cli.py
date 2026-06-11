from __future__ import annotations

import argparse
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.data import cached_historical
from mu_strategy.indicators import ema, macd, rsi
from mu_strategy.models import Candle
from mu_strategy.reporting import render_markdown_report
from mu_strategy.strategy import StrategyConfig, one_hour_regime


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the MU 5x Fibonacci strategy.")
    parser.add_argument("--symbol", default="MUUSDT")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--report", type=Path, default=Path("reports/mu_backtest.md"))
    args = parser.parse_args()

    config = StrategyConfig(symbol=args.symbol)
    candles_15m, file_15m = cached_historical(
        args.symbol,
        "15m",
        days=args.days,
        data_dir=args.data_dir,
        refresh=args.refresh,
    )
    candles_1h, file_1h = cached_historical(
        args.symbol,
        "1h",
        days=args.days,
        data_dir=args.data_dir,
        refresh=args.refresh,
    )
    context = build_hourly_context(candles_15m, candles_1h)
    result = run_backtest(candles_15m, context, config=config)
    report = render_markdown_report(result, config=config, symbol=args.symbol, data_files=[file_15m, file_1h])
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
