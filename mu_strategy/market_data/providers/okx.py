from __future__ import annotations

import json
import time
import urllib.parse
import urllib.error
import urllib.request

from mu_strategy.market_data.utils import DAY_MS, dedupe_candles
from mu_strategy.models import Candle


OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
OKX_HISTORY_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_BASE_URL = OKX_CANDLES_URL
OKX_MAX_CANDLE_LIMIT = 300


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
    before: int | None = None,
    limit: int = 100,
    retries: int = 3,
) -> list[Candle]:
    return _fetch_okx_candles(
        OKX_CANDLES_URL,
        symbol,
        interval,
        after=after,
        before=before,
        limit=limit,
        retries=retries,
    )


def fetch_okx_history_candles(
    symbol: str,
    interval: str,
    *,
    after: int | None = None,
    before: int | None = None,
    limit: int = 100,
    retries: int = 3,
) -> list[Candle]:
    return _fetch_okx_candles(
        OKX_HISTORY_CANDLES_URL,
        symbol,
        interval,
        after=after,
        before=before,
        limit=limit,
        retries=retries,
    )


def _fetch_okx_candles(
    base_url: str,
    symbol: str,
    interval: str,
    *,
    after: int | None = None,
    before: int | None = None,
    limit: int = 100,
    retries: int = 3,
) -> list[Candle]:
    params: dict[str, str | int] = {
        "instId": symbol,
        "bar": okx_interval(interval),
        "limit": min(limit, OKX_MAX_CANDLE_LIMIT),
    }
    if after is not None:
        params["after"] = after
    if before is not None:
        params["before"] = before
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(0.5 * attempt)
    else:
        raise RuntimeError(f"OKX request failed after {retries} retries: {last_error}")
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
    after: int | None = end_time_ms
    while True:
        batch = fetch_okx_history_candles(symbol, interval, after=after)
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
        batch = fetch_okx_history_candles(symbol, interval, after=after)
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
