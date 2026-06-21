from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mu_strategy.market_data.cache import cached_historical, read_csv
from mu_strategy.market_data.symbols import ResolvedSymbol, resolve_okx_swap_symbol
from mu_strategy.market_data.trusted import DataStatus, refresh_trusted_symbol_statuses
from mu_strategy.models import Candle


@dataclass(frozen=True)
class CandleBundle:
    symbol: ResolvedSymbol
    candles_by_interval: dict[str, list[Candle]]
    files_by_interval: dict[str, Path]
    days: int
    statuses_by_interval: dict[str, DataStatus] = field(default_factory=dict)


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


def refresh_trusted_candle_bundle(
    symbol: str,
    *,
    intervals: tuple[str, ...] = ("15m", "1h"),
    days: int = 28,
    data_dir: Path = Path("data/live"),
    refresh: bool = False,
    fetcher: Callable[..., list[Candle]] | None = None,
) -> CandleBundle:
    resolved = resolve_okx_swap_symbol(symbol)
    candles_by_interval: dict[str, list[Candle]] = {}
    files_by_interval: dict[str, Path] = {}
    requested_intervals = tuple(dict.fromkeys(intervals))
    validation_intervals = _validation_intervals(requested_intervals)
    statuses_by_interval = refresh_trusted_symbol_statuses(
        resolved.inst_id,
        intervals=validation_intervals,
        days=days,
        data_dir=data_dir,
        fetcher=fetcher,
    )

    for interval in requested_intervals:
        status = statuses_by_interval[interval]
        files_by_interval[interval] = status.source_file
        if status.source_file.exists():
            candles_by_interval[interval] = read_csv(status.source_file)
        else:
            candles_by_interval[interval] = []
    return CandleBundle(
        symbol=resolved,
        candles_by_interval=candles_by_interval,
        files_by_interval=files_by_interval,
        days=days,
        statuses_by_interval=statuses_by_interval,
    )


def _validation_intervals(intervals: tuple[str, ...]) -> tuple[str, ...]:
    if any(interval in {"15m", "1h"} for interval in intervals):
        return tuple(dict.fromkeys(("5m", *intervals)))
    return intervals
