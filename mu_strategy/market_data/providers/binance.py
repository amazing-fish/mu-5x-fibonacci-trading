from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from mu_strategy.market_data.utils import DAY_MS, dedupe_candles, interval_to_ms
from mu_strategy.models import Candle


BASE_URL = "https://fapi.binance.com/fapi/v1/klines"


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
    start_time_ms = end_time_ms - (days * DAY_MS)
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
