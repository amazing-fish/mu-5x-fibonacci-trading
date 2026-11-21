from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from bisect import bisect_left, bisect_right

from mu_strategy.models import Candle


DAY_MS = 86_400_000
CSV_FIELDS = ["open_time_ms", "open_time_iso", "open", "high", "low", "close", "volume"]
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UNIT_MULTIPLIERS = {
    "m": 60_000,
    "h": 3_600_000,
    "d": DAY_MS,
}
_DAY_ISO_PREFIX_CACHE: dict[int, str] = {}
_DAY_MONTH_ID_CACHE: dict[int, str] = {}


@lru_cache(maxsize=64)
def interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    multiplier = _UNIT_MULTIPLIERS.get(unit)
    if multiplier is None:
        raise ValueError(f"unsupported interval: {interval}")
    return value * multiplier


def day_iso_prefix(utc_day: int) -> str:
    prefix = _DAY_ISO_PREFIX_CACHE.get(utc_day)
    if prefix is None:
        instant = _UTC_EPOCH + timedelta(days=utc_day)
        prefix = f"{instant.year:04d}-{instant.month:02d}-{instant.day:02d}T"
        _DAY_ISO_PREFIX_CACHE[utc_day] = prefix
    return prefix


def day_utc_month_id(utc_day: int) -> str:
    month_id = _DAY_MONTH_ID_CACHE.get(utc_day)
    if month_id is None:
        instant = _UTC_EPOCH + timedelta(days=utc_day)
        month_id = f"{instant.year:04d}-{instant.month:02d}"
        _DAY_MONTH_ID_CACHE[utc_day] = month_id
    return month_id


def format_open_time_iso(open_time_ms: int) -> str:
    seconds, rem_ms = divmod(open_time_ms, 1000)
    day, sec_in_day = divmod(seconds, 86_400)
    prefix = day_iso_prefix(day)
    hh, rem = divmod(sec_in_day, 3600)
    mm, ss = divmod(rem, 60)
    if rem_ms:
        return f"{prefix}{hh:02d}:{mm:02d}:{ss:02d}.{rem_ms:03d}000+00:00"
    return f"{prefix}{hh:02d}:{mm:02d}:{ss:02d}+00:00"


def dedupe_candles(candles: list[Candle]) -> list[Candle]:
    n = len(candles)
    if n <= 1:
        return list(candles)
    # Fast path: most stored/provider streams are already strictly ordered & unique.
    prev = candles[0].open_time_ms
    already_ordered = True
    for index in range(1, n):
        ts = candles[index].open_time_ms
        if ts <= prev:
            already_ordered = False
            break
        prev = ts
    if already_ordered:
        return list(candles)
    by_time = {bar.open_time_ms: bar for bar in candles}
    return [by_time[key] for key in sorted(by_time)]


def prune_candles_to_window(
    candles: list[Candle],
    *,
    days: int,
    end_time_ms: int | None = None,
) -> list[Candle]:
    if not candles:
        return []
    ordered = dedupe_candles(candles)
    resolved_end = end_time_ms if end_time_ms is not None else ordered[-1].open_time_ms
    start_time_ms = resolved_end - (days * DAY_MS)
    if ordered[0].open_time_ms >= start_time_ms and ordered[-1].open_time_ms <= resolved_end:
        return ordered
    lo = bisect_left(ordered, start_time_ms, key=_candle_open_time_ms)
    hi = bisect_right(ordered, resolved_end, key=_candle_open_time_ms)
    return ordered[lo:hi]


def _candle_open_time_ms(candle: Candle) -> int:
    return candle.open_time_ms


def infer_candle_interval_ms(candles: list[Candle]) -> int:
    """Return the smallest positive adjacent timestamp gap, or zero."""
    if len(candles) < 2:
        return 0
    min_diff = 0
    prev = candles[0].open_time_ms
    for index in range(1, len(candles)):
        ts = candles[index].open_time_ms
        diff = ts - prev
        if diff > 0 and (min_diff == 0 or diff < min_diff):
            min_diff = diff
        prev = ts
    return min_diff
