from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass


OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"


@dataclass(frozen=True)
class OKXSwapTicker:
    inst_id: str
    last: float
    volume_ccy_24h: float
    source: str = "top"


def fetch_okx_swap_tickers() -> list[dict]:
    url = f"{OKX_TICKERS_URL}?{urllib.parse.urlencode({'instType': 'SWAP'})}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX tickers request failed: {payload.get('msg') or payload.get('code')}")
    return list(payload.get("data") or [])


def select_top_okx_usdt_swaps(rows: list[dict], *, limit: int) -> list[OKXSwapTicker]:
    candidates = []
    for row in rows:
        inst_id = str(row.get("instId", ""))
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        volume = _float(row.get("volCcy24h"))
        last = _float(row.get("last"))
        if volume <= 0 or last <= 0:
            continue
        candidates.append(OKXSwapTicker(inst_id=inst_id, last=last, volume_ccy_24h=volume))
    return sorted(candidates, key=_usdt_turnover_24h, reverse=True)[:limit]


def top_okx_usdt_swaps(*, limit: int = 10) -> list[OKXSwapTicker]:
    return select_top_okx_usdt_swaps(fetch_okx_swap_tickers(), limit=limit)


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _usdt_turnover_24h(ticker: OKXSwapTicker) -> float:
    return ticker.last * ticker.volume_ccy_24h
