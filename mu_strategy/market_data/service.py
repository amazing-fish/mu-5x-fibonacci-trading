from __future__ import annotations

from pathlib import Path

from mu_strategy.market_data.cache import cached_historical
from mu_strategy.market_data.symbols import ResolvedSymbol, resolve_okx_swap_symbol
from mu_strategy.market_data.trusted_data.compat import CandleBundle
from mu_strategy.market_data.trusted_data.load import (
    TRUSTED_CONSUMER_REFRESH_ERROR,
    load_trusted_candle_bundle as refresh_trusted_candle_bundle,
)


def refresh_candle_bundle(
    symbol: str,
    *,
    intervals: tuple[str, ...] = ("15m", "1h"),
    days: int = 28,
    data_dir: Path = Path("data"),
    refresh: bool = False,
    source: str = "okx",
) -> CandleBundle:
    if source == "okx":
        resolved = resolve_okx_swap_symbol(symbol)
        fetch_symbol = resolved.inst_id
    elif source == "binance":
        resolved = ResolvedSymbol(requested=symbol, inst_id=symbol, source=source)
        fetch_symbol = symbol
    else:
        raise ValueError(f"unsupported data source: {source}")
    candles_by_interval: dict[str, list[Candle]] = {}
    files_by_interval: dict[str, Path] = {}
    for interval in intervals:
        candles, path = cached_historical(
            fetch_symbol,
            interval,
            days=days,
            data_dir=data_dir,
            refresh=refresh,
            source=source,
        )
        candles_by_interval[interval] = candles
        files_by_interval[interval] = path
    return CandleBundle(
        symbol=resolved,
        candles_by_interval=candles_by_interval,
        files_by_interval=files_by_interval,
        days=days,
    )
