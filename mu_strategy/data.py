from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from mu_strategy.models import Candle


BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
OKX_BASE_URL = "https://www.okx.com/api/v5/market/history-candles"
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


def okx_interval(interval: str) -> str:
    if interval == "1h":
        return "1H"
    return interval


def okx_row_to_candle(row: list) -> Candle | None:
    if len(row) >= 9 and str(row[8]) != "1":
        return None
    return Candle(
        open_time_ms=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
    )


def fetch_okx_candles(
    symbol: str,
    interval: str,
    *,
    after: int | None = None,
    limit: int = 100,
) -> list[Candle]:
    params: dict[str, str | int] = {
        "instId": symbol,
        "bar": okx_interval(interval),
        "limit": min(limit, 100),
    }
    if after is not None:
        params["after"] = after
    url = f"{OKX_BASE_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX request failed: {payload.get('msg') or payload.get('code')}")
    candles = [okx_row_to_candle(row) for row in payload.get("data", [])]
    return dedupe_candles([candle for candle in candles if candle is not None])


def fetch_okx_historical(
    symbol: str,
    interval: str,
    *,
    days: int,
    end_time_ms: int | None = None,
) -> list[Candle]:
    end_time_ms = end_time_ms or int(time.time() * 1000)
    start_time_ms = end_time_ms - (days * 86_400_000)
    output: list[Candle] = []
    after: int | None = None
    while True:
        batch = fetch_okx_candles(symbol, interval, after=after)
        if not batch:
            break
        output.extend(batch)
        oldest = min(bar.open_time_ms for bar in batch)
        if oldest <= start_time_ms:
            break
        if after == oldest:
            break
        after = oldest
    return dedupe_candles([bar for bar in output if start_time_ms <= bar.open_time_ms <= end_time_ms])


def fetch_okx_incremental(
    symbol: str,
    interval: str,
    *,
    since_time_ms: int,
) -> list[Candle]:
    output: list[Candle] = []
    after: int | None = None
    while True:
        batch = fetch_okx_candles(symbol, interval, after=after)
        if not batch:
            break
        output.extend(bar for bar in batch if bar.open_time_ms >= since_time_ms)
        oldest = min(bar.open_time_ms for bar in batch)
        if oldest <= since_time_ms:
            break
        if after == oldest:
            break
        after = oldest
    return dedupe_candles(output)


def dedupe_candles(candles: list[Candle]) -> list[Candle]:
    by_time = {bar.open_time_ms: bar for bar in candles}
    return [by_time[key] for key in sorted(by_time)]


def merge_incremental_candles(existing: list[Candle], fetched: list[Candle]) -> list[Candle]:
    stable_existing = existing[:-1] if existing else []
    return dedupe_candles([*stable_existing, *fetched])


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
    source: str = "binance",
    incremental: bool | None = None,
) -> tuple[list[Candle], Path]:
    path = cache_path(symbol, interval, days=days, data_dir=data_dir, source=source)
    if source not in ("binance", "okx"):
        raise ValueError(f"unsupported data source: {source}")
    if incremental is None:
        incremental = source == "okx"
    if path.exists() and not refresh:
        candles = read_csv(path)
        if source == "okx" and incremental and candles:
            since_time_ms = candles[-2].open_time_ms if len(candles) >= 2 else candles[0].open_time_ms
            fetched = fetch_okx_incremental(symbol, interval, since_time_ms=since_time_ms)
            candles = merge_incremental_candles(candles, fetched)
            write_csv(candles, path)
        return candles, path
    if source == "okx":
        candles = fetch_okx_historical(symbol, interval, days=days)
    else:
        candles = fetch_historical(symbol, interval, days=days)
    write_csv(candles, path)
    return candles, path


def cache_path(symbol: str, interval: str, *, days: int, data_dir: Path = Path("data"), source: str = "binance") -> Path:
    if source == "okx":
        return data_dir / f"OKX_{symbol}_{interval}_{days}d.csv"
    if source == "binance":
        return data_dir / f"{symbol}_{interval}_{days}d.csv"
    raise ValueError(f"unsupported data source: {source}")
