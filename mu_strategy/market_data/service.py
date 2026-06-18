from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mu_strategy.market_data.cache import cached_historical
from mu_strategy.market_data.symbols import ResolvedSymbol, resolve_okx_swap_symbol
from mu_strategy.models import Candle


@dataclass(frozen=True)
class CandleBundle:
    symbol: ResolvedSymbol
    candles_by_interval: dict[str, list[Candle]]
    files_by_interval: dict[str, Path]
    days: int


def refresh_candle_bundle(
    symbol: str,
    *,
    intervals: tuple[str, ...] = ("15m", "1h"),
    days: int = 28,
    data_dir: Path = Path("data"),
    refresh: bool = False,
) -> CandleBundle:
    resolved = resolve_okx_swap_symbol(symbol)
    candles_by_interval: dict[str, list[Candle]] = {}
    files_by_interval: dict[str, Path] = {}
    for interval in intervals:
        candles, path = cached_historical(
            resolved.inst_id,
            interval,
            days=days,
            data_dir=data_dir,
            refresh=refresh,
            source=resolved.source,
        )
        candles_by_interval[interval] = candles
        files_by_interval[interval] = path
    return CandleBundle(
        symbol=resolved,
        candles_by_interval=candles_by_interval,
        files_by_interval=files_by_interval,
        days=days,
    )
