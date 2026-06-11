from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from mu_strategy.models import Candle


BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
CSV_FIELDS = ["open_time_ms", "open_time_iso", "open", "high", "low", "close", "volume"]


def interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    multipliers = {
        "m": 60_000,
        "h": 3_600_000,
        "d": 86_400_000,
    }
    if unit not in multipliers:
        raise ValueError(f"unsupported interval: {interval}")
    return value * multipliers[unit]


def fetch_klines(
    symbol: str,
    interval: str,
    *,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 1500,
) -> list[Candle]:
    params: dict[str, str | int] = {
        "symbol": symbol,
        "interval": interval,
        "limit": min(limit, 1500),
    }
    if start_time_ms is not None:
        params["startTime"] = start_time_ms
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [Candle.from_binance_row(row) for row in payload]


def fetch_historical(
    symbol: str,
    interval: str,
    *,
    days: int,
    end_time_ms: int | None = None,
) -> list[Candle]:
    interval_ms = interval_to_ms(interval)
    end_time_ms = end_time_ms or int(time.time() * 1000)
    start_time_ms = end_time_ms - (days * 86_400_000)
    output: list[Candle] = []
    cursor = start_time_ms
    while cursor < end_time_ms:
        batch = fetch_klines(symbol, interval, start_time_ms=cursor, end_time_ms=end_time_ms, limit=1500)
        if not batch:
            break
        output.extend(batch)
        next_cursor = batch[-1].open_time_ms + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1500:
            break
    return dedupe_candles(output)


def dedupe_candles(candles: list[Candle]) -> list[Candle]:
    by_time = {bar.open_time_ms: bar for bar in candles}
    return [by_time[key] for key in sorted(by_time)]


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


def cached_historical(
    symbol: str,
    interval: str,
    *,
    days: int,
    data_dir: Path = Path("data"),
    refresh: bool = False,
) -> tuple[list[Candle], Path]:
    path = data_dir / f"{symbol}_{interval}_{days}d.csv"
    if path.exists() and not refresh:
        return read_csv(path), path
    candles = fetch_historical(symbol, interval, days=days)
    write_csv(candles, path)
    return candles, path
