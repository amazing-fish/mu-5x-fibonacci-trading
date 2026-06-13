from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from mu_strategy.market_data.utils import DAY_MS, dedupe_candles
from mu_strategy.models import Candle


OKX_BASE_URL = "https://www.okx.com/api/v5/market/history-candles"


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
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
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
    start_time_ms = end_time_ms - (days * DAY_MS)
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
