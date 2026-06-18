from __future__ import annotations

import csv
from pathlib import Path

from mu_strategy.market_data.providers.binance import fetch_historical
from mu_strategy.market_data.providers.okx import fetch_okx_historical, fetch_okx_incremental
from mu_strategy.market_data.utils import DAY_MS, dedupe_candles
from mu_strategy.models import Candle


CSV_FIELDS = ["open_time_ms", "open_time_iso", "open", "high", "low", "close", "volume"]
DEFAULT_MAX_CLOSE_TO_NEXT_OPEN_GAP_PCT = 0.02


class DataQualityError(ValueError):
    pass


def cache_path(symbol: str, interval: str, *, days: int, data_dir: Path = Path("data"), source: str = "binance") -> Path:
    if source == "okx":
        return data_dir / f"OKX_{symbol}_{interval}_{days}d.csv"
    if source == "binance":
        return data_dir / f"{symbol}_{interval}_{days}d.csv"
    raise ValueError(f"unsupported data source: {source}")


def write_csv(candles: list[Candle], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for candle in candles:
            writer.writerow(candle.to_csv_row())


def read_csv(path: Path) -> list[Candle]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [Candle.from_csv_row(row) for row in reader]


def merge_incremental_candles(existing: list[Candle], fetched: list[Candle]) -> list[Candle]:
    if not fetched:
        return list(existing)
    stable_existing = existing[:-1] if existing else []
    return dedupe_candles([*stable_existing, *fetched])


def prune_candles_to_window(candles: list[Candle], *, days: int, end_time_ms: int | None = None) -> list[Candle]:
    if not candles:
        return []
    end_time_ms = end_time_ms if end_time_ms is not None else max(bar.open_time_ms for bar in candles)
    start_time_ms = end_time_ms - (days * DAY_MS)
    return [bar for bar in dedupe_candles(candles) if start_time_ms <= bar.open_time_ms <= end_time_ms]


def validate_close_to_next_open_gaps(
    candles: list[Candle],
    *,
    max_gap_pct: float = DEFAULT_MAX_CLOSE_TO_NEXT_OPEN_GAP_PCT,
) -> None:
    if max_gap_pct < 0:
        raise ValueError("max_gap_pct must be non-negative")
    for previous, current in zip(candles, candles[1:]):
        if previous.close == 0:
            continue
        gap_pct = abs((current.open / previous.close) - 1)
        if gap_pct > max_gap_pct:
            raise DataQualityError(
                "close_to_next_open_gap "
                f"previous_time={previous.open_time_iso} "
                f"current_time={current.open_time_iso} "
                f"previous_close={previous.close:.8f} "
                f"current_open={current.open:.8f} "
                f"gap_pct={gap_pct:.6f} "
                f"max_gap_pct={max_gap_pct:.6f}"
            )


def cached_historical(
    symbol: str,
    interval: str,
    *,
    days: int,
    data_dir: Path = Path("data"),
    refresh: bool = False,
    source: str = "binance",
    incremental: bool | None = None,
) -> tuple[list[Candle], Path]:
    if source not in ("binance", "okx"):
        raise ValueError(f"unsupported data source: {source}")
    if incremental is None:
        incremental = source == "okx"

    path = cache_path(symbol, interval, days=days, data_dir=data_dir, source=source)
    if path.exists() and not refresh:
        candles = read_csv(path)
        validate_close_to_next_open_gaps(candles)
        if source == "okx" and incremental and candles:
            since_time_ms = candles[-2].open_time_ms if len(candles) >= 2 else candles[0].open_time_ms
            try:
                fetched = fetch_okx_incremental(symbol, interval, since_time_ms=since_time_ms)
            except Exception:
                candles = prune_candles_to_window(candles, days=days)
                validate_close_to_next_open_gaps(candles)
                write_csv(candles, path)
                return candles, path
            candles = merge_incremental_candles(candles, fetched)
            candles = prune_candles_to_window(candles, days=days)
            validate_close_to_next_open_gaps(candles)
            write_csv(candles, path)
        return candles, path

    if source == "okx":
        candles = fetch_okx_historical(symbol, interval, days=days)
    else:
        candles = fetch_historical(symbol, interval, days=days)
    validate_close_to_next_open_gaps(candles)
    write_csv(candles, path)
    return candles, path
