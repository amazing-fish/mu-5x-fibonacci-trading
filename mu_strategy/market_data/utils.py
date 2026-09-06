from __future__ import annotations

from mu_strategy.models import Candle


DAY_MS = 86_400_000


def interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    multipliers = {
        "m": 60_000,
        "h": 3_600_000,
        "d": DAY_MS,
    }
    if unit not in multipliers:
        raise ValueError(f"unsupported interval: {interval}")
    return value * multipliers[unit]


def dedupe_candles(candles: list[Candle]) -> list[Candle]:
    by_time = {bar.open_time_ms: bar for bar in candles}
    return [by_time[key] for key in sorted(by_time)]


def infer_candle_interval_ms(candles: list[Candle]) -> int:
    """Return the smallest positive adjacent timestamp gap, or zero."""
    if len(candles) < 2:
        return 0
    diffs = [
        candles[index].open_time_ms - candles[index - 1].open_time_ms
        for index in range(1, len(candles))
        if candles[index].open_time_ms > candles[index - 1].open_time_ms
    ]
    return min(diffs) if diffs else 0
