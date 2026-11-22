from __future__ import annotations

import time
import urllib.parse

from mu_strategy.market_data.http_pool import get_json
from mu_strategy.market_data.utils import DAY_MS, dedupe_candles
from mu_strategy.models import Candle


OKX_BASE_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
_OKX_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}


def fetch_okx_listing_time(symbol: str) -> int:
    """Read the exchange's start boundary, never inferring it from a short page."""
    params = urllib.parse.urlencode({"instType": "SWAP", "instId": symbol})
    payload = get_json(
        f"{OKX_INSTRUMENTS_URL}?{params}",
        headers=_OKX_HEADERS,
        timeout=20.0,
        retries=1,
    )
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise ValueError("OKX listing metadata request failed")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("OKX listing metadata must identify one instrument")
    row = rows[0]
    value = row.get("listTime")
    if (
        row.get("instId") != symbol
        or not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or int(value) <= 0
    ):
        raise ValueError("OKX listing metadata has an invalid instrument or start time")
    return int(value)


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
    retries: int = 3,
) -> list[Candle]:
    params: dict[str, str | int] = {
        "instId": symbol,
        "bar": okx_interval(interval),
        "limit": min(limit, 100),
    }
    if after is not None:
        params["after"] = after
    url = f"{OKX_BASE_URL}?{urllib.parse.urlencode(params)}"
    payload = get_json(
        url,
        headers=_OKX_HEADERS,
        timeout=20.0,
        retries=retries,
        retry_backoff_base=0.25,
        retry_backoff_cap=2.0,
    )
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX request failed: {payload.get('msg') or payload.get('code')}")
    candles = [candle for row in payload.get("data", []) if (candle := okx_row_to_candle(row)) is not None]
    return dedupe_candles(candles)


def fetch_okx_historical(
    symbol: str,
    interval: str,
    *,
    days: int,
    end_time_ms: int | None = None,
) -> list[Candle]:
    end_time_ms = end_time_ms or int(time.time() * 1000)
    output: list[Candle] = []
    after: int | None = end_time_ms
    latest_confirmed_time_ms: int | None = None
    oldest_confirmed_time_ms: int | None = None
    required_start_time_ms: int | None = None
    while True:
        batch = fetch_okx_candles(symbol, interval, after=after)
        if not batch:
            break
        batch_oldest = batch[0].open_time_ms
        for bar in batch:
            ts = bar.open_time_ms
            if ts < batch_oldest:
                batch_oldest = ts
            if ts <= end_time_ms:
                output.append(bar)
                if latest_confirmed_time_ms is None or ts > latest_confirmed_time_ms:
                    latest_confirmed_time_ms = ts
                if oldest_confirmed_time_ms is None or ts < oldest_confirmed_time_ms:
                    oldest_confirmed_time_ms = ts
        if latest_confirmed_time_ms is None or oldest_confirmed_time_ms is None:
            break
        required_start_time_ms = latest_confirmed_time_ms - (days * DAY_MS)
        if oldest_confirmed_time_ms <= required_start_time_ms:
            break
        if after == batch_oldest:
            break
        after = batch_oldest
    if latest_confirmed_time_ms is None or required_start_time_ms is None:
        return []
    return dedupe_candles([bar for bar in output if required_start_time_ms <= bar.open_time_ms <= latest_confirmed_time_ms])


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
        oldest = batch[0].open_time_ms
        for bar in batch:
            ts = bar.open_time_ms
            if ts < oldest:
                oldest = ts
            if ts >= since_time_ms:
                output.append(bar)
        if oldest <= since_time_ms:
            break
        if after == oldest:
            break
        after = oldest
    return dedupe_candles(output)
