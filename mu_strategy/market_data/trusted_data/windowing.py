from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.market_data.cache import prune_candles_to_window
from mu_strategy.models import Candle


@dataclass(frozen=True)
class CandleWindowPlan:
    days: int
    end_time_ms: int | None
    base_interval: str = "5m"


def resolve_shared_window(
    candles_by_interval: dict[str, list[Candle]],
    *,
    days: int,
    base_interval: str = "5m",
) -> CandleWindowPlan:
    base_candles = candles_by_interval.get(base_interval) or []
    if base_candles:
        end_time_ms = max(candle.open_time_ms for candle in base_candles)
    else:
        timestamps = [
            candle.open_time_ms
            for candles in candles_by_interval.values()
            for candle in candles
        ]
        end_time_ms = max(timestamps) if timestamps else None
    return CandleWindowPlan(days=days, end_time_ms=end_time_ms, base_interval=base_interval)


def prune_candle_bundle(
    candles_by_interval: dict[str, list[Candle]],
    *,
    plan: CandleWindowPlan,
) -> dict[str, list[Candle]]:
    return {
        interval: prune_candles_to_window(candles or [], days=plan.days, end_time_ms=plan.end_time_ms)
        for interval, candles in candles_by_interval.items()
    }
