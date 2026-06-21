from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from mu_strategy.market_data.cache import write_csv
from mu_strategy.market_data.providers.binance import fetch_klines
from mu_strategy.market_data.providers.okx import fetch_okx_candles
from mu_strategy.market_data.refresh import (
    CandleValidationError,
    aggregate_from_base_interval,
    validate_built_candles,
)
from mu_strategy.market_data.utils import dedupe_candles, interval_to_ms
from mu_strategy.models import Candle


DEFAULT_WINDOW_MINUTES = 360
DEFAULT_INTERVAL_MINUTES = 240


@dataclass(frozen=True)
class DataRefreshResult:
    source: str
    symbol: str
    fetched_5m: int
    native_1h: int
    built_15m: int
    built_1h: int
    output_paths: list[Path]


def fetch_latest_window(
    symbol: str,
    interval: str,
    *,
    source: str,
    window_minutes: int,
    end_time_ms: int | None = None,
    align_start_interval: str | None = "1h",
) -> list[Candle]:
    interval_ms = interval_to_ms(interval)
    if source == "okx":
        limit = _fetch_limit_for_aligned_window(window_minutes, interval_ms, align_start_interval)
        candles = fetch_okx_candles(symbol, interval, limit=limit)
    elif source == "binance":
        if end_time_ms is None:
            candles = fetch_klines(symbol, interval, limit=100)
        else:
            start_time_ms = _aligned_window_start(end_time_ms, window_minutes, align_start_interval)
            limit = math.ceil((end_time_ms - start_time_ms) / interval_ms) + 5
            candles = fetch_klines(
                symbol,
                interval,
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
                limit=limit,
            )
    else:
        raise ValueError(f"unsupported data source: {source}")
    if end_time_ms is None:
        if not candles:
            return []
        end_time_ms = max(bar.open_time_ms for bar in candles) + interval_ms
    start_time_ms = _aligned_window_start(end_time_ms, window_minutes, align_start_interval)
    return [bar for bar in dedupe_candles(candles) if start_time_ms <= bar.open_time_ms <= end_time_ms]


def run_once(
    *,
    source: str,
    symbol: str,
    window_minutes: int,
    data_dir: Path,
    end_time_ms: int | None = None,
) -> DataRefreshResult:
    candles_5m = fetch_latest_window(
        symbol,
        "5m",
        source=source,
        window_minutes=window_minutes,
        end_time_ms=end_time_ms,
    )
    native_1h = fetch_latest_window(
        symbol,
        "1h",
        source=source,
        window_minutes=window_minutes,
        end_time_ms=end_time_ms,
    )
    built_15m = aggregate_from_base_interval(candles_5m, base_interval="5m", target_interval="15m")
    built_1h = aggregate_from_base_interval(candles_5m, base_interval="5m", target_interval="1h")
    validate_built_candles(built_1h, native_1h)

    output_paths = [
        _live_path(data_dir, source, symbol, "5m", "native"),
        _live_path(data_dir, source, symbol, "1h", "native"),
        _live_path(data_dir, source, symbol, "15m", "built_from_5m"),
        _live_path(data_dir, source, symbol, "1h", "built_from_5m"),
    ]
    for candles, path in (
        (candles_5m, output_paths[0]),
        (native_1h, output_paths[1]),
        (built_15m, output_paths[2]),
        (built_1h, output_paths[3]),
    ):
        write_csv(candles, path)

    return DataRefreshResult(
        source=source,
        symbol=symbol,
        fetched_5m=len(candles_5m),
        native_1h=len(native_1h),
        built_15m=len(built_15m),
        built_1h=len(built_1h),
        output_paths=output_paths,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh latest MU candles and validate 5m-built 1h data.")
    parser.add_argument("--source", choices=("okx", "binance"), default="okx")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--once", action="store_true", help="Run one refresh cycle and exit.")
    parser.add_argument("--loop", action="store_true", help="Run refresh repeatedly.")
    parser.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    args = parser.parse_args()

    symbol = args.symbol or ("MU-USDT-SWAP" if args.source == "okx" else "MUUSDT")
    if args.loop:
        while True:
            _run_and_print(args.source, symbol, args.window_minutes, args.data_dir)
            time.sleep(args.interval_minutes * 60)
    else:
        _run_and_print(args.source, symbol, args.window_minutes, args.data_dir)


def _run_and_print(source: str, symbol: str, window_minutes: int, data_dir: Path) -> None:
    try:
        result = run_once(
            source=source,
            symbol=symbol,
            window_minutes=window_minutes,
            data_dir=data_dir,
        )
    except CandleValidationError as exc:
        print(f"validation=failed source={source} symbol={symbol} error={exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        "validation=ok "
        f"source={result.source} symbol={result.symbol} "
        f"fetched_5m={result.fetched_5m} native_1h={result.native_1h} "
        f"built_15m={result.built_15m} built_1h={result.built_1h} "
        f"output_dir={data_dir.resolve()}"
    )


def _live_path(data_dir: Path, source: str, symbol: str, interval: str, flavor: str) -> Path:
    return data_dir / f"{source.upper()}_{symbol}_{interval}_{flavor}_latest.csv"


def _aligned_window_start(end_time_ms: int, window_minutes: int, align_interval: str | None) -> int:
    start_time_ms = end_time_ms - (window_minutes * 60_000)
    if align_interval is None:
        return start_time_ms
    align_ms = interval_to_ms(align_interval)
    return (start_time_ms // align_ms) * align_ms


def _fetch_limit_for_aligned_window(window_minutes: int, interval_ms: int, align_interval: str | None) -> int:
    window_ms = window_minutes * 60_000
    align_padding_ms = interval_to_ms(align_interval) if align_interval is not None else 0
    return math.ceil((window_ms + align_padding_ms) / interval_ms)


if __name__ == "__main__":
    main()
