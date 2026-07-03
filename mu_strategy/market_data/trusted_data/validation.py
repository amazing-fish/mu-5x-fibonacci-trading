from __future__ import annotations

import math

from mu_strategy.market_data.trusted_data.contracts import HealthReason, ValidationReport
from mu_strategy.market_data.utils import dedupe_candles, interval_to_ms
from mu_strategy.models import Candle


def normalize_and_validate_candles(
    candles: list[Candle],
    *,
    interval: str,
    max_gap_pct: float = 0.02,
    max_timestamp_gaps: int = 20,
) -> tuple[list[Candle], ValidationReport]:
    ordered = dedupe_candles(candles)
    if not ordered:
        return [], ValidationReport(False, HealthReason.EMPTY)
    interval_ms = interval_to_ms(interval)
    misaligned = tuple(candle.open_time_ms for candle in ordered if candle.open_time_ms % interval_ms != 0)
    if misaligned:
        return ordered, ValidationReport(False, HealthReason.TIMESTAMP_MISALIGNED, misaligned_timestamps=misaligned)
    timestamp_gaps: list[dict[str, int]] = []
    for previous, current in zip(ordered, ordered[1:]):
        delta_ms = current.open_time_ms - previous.open_time_ms
        if delta_ms == interval_ms:
            continue
        timestamp_gaps.append(
            {
                "previous_timestamp_ms": previous.open_time_ms,
                "current_timestamp_ms": current.open_time_ms,
                "expected_interval_ms": interval_ms,
                "actual_interval_ms": delta_ms,
                "missing_count": max(0, (delta_ms // interval_ms) - 1),
            }
        )
        if len(timestamp_gaps) >= max_timestamp_gaps:
            break
    if timestamp_gaps:
        return ordered, ValidationReport(False, HealthReason.TIMESTAMP_GAP, timestamp_gaps=tuple(timestamp_gaps))
    for candle in ordered:
        if not all(_is_finite_positive(value) for value in (candle.open, candle.high, candle.low, candle.close)):
            return ordered, ValidationReport(False, HealthReason.OHLCV_INVALID)
        if not _is_finite_non_negative(candle.volume):
            return ordered, ValidationReport(False, HealthReason.OHLCV_INVALID)
        if (
            candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
            or candle.volume < 0
        ):
            return ordered, ValidationReport(False, HealthReason.OHLCV_INVALID)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.close == 0:
            continue
        gap_pct = abs((current.open / previous.close) - 1)
        if gap_pct > max_gap_pct:
            return ordered, ValidationReport(False, HealthReason.CONTINUITY_GAP)
    return ordered, ValidationReport(True, HealthReason.OK)


def _is_finite_positive(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _is_finite_non_negative(value: float) -> bool:
    return math.isfinite(value) and value >= 0


def aggregate_candles(
    candles: list[Candle],
    *,
    interval: str,
    base_interval: str = "5m",
    ohlc_policy: str = "standard",
) -> list[Candle]:
    target_ms = interval_to_ms(interval)
    base_ms = interval_to_ms(base_interval)
    expected = target_ms // base_ms
    if expected <= 0 or target_ms % base_ms != 0:
        raise ValueError(f"{base_interval} cannot build {interval}")
    if ohlc_policy not in {"standard", "okx_native"}:
        raise ValueError(f"unsupported ohlc_policy: {ohlc_policy}")
    groups: dict[int, list[Candle]] = {}
    for candle in dedupe_candles(candles):
        bucket = candle.open_time_ms - (candle.open_time_ms % target_ms)
        groups.setdefault(bucket, []).append(candle)
    output: list[Candle] = []
    for timestamp, rows in sorted(groups.items()):
        rows = dedupe_candles(rows)
        if len(rows) != expected:
            continue
        output.append(_aggregate_parent(timestamp, rows, ohlc_policy=ohlc_policy))
    return output


def _aggregate_parent(timestamp: int, rows: list[Candle], *, ohlc_policy: str) -> Candle:
    volume = sum(row.volume for row in rows)
    if ohlc_policy == "standard":
        ohlc_rows = rows
    else:
        ohlc_rows = [row for row in rows if row.volume > 0]
        if not ohlc_rows:
            ohlc_values = {(row.open, row.high, row.low, row.close) for row in rows}
            if len(ohlc_values) != 1:
                raise ValueError(f"inconsistent all-zero no-trade bucket at {timestamp}")
            ohlc_rows = rows
    return Candle(
        timestamp,
        ohlc_rows[0].open,
        max(row.high for row in ohlc_rows),
        min(row.low for row in ohlc_rows),
        ohlc_rows[-1].close,
        volume,
    )


def complete_parent_window(
    base_candles: list[Candle],
    *,
    interval: str,
    base_interval: str = "5m",
) -> tuple[int, int] | None:
    ordered = dedupe_candles(base_candles)
    if not ordered:
        return None
    target_ms = interval_to_ms(interval)
    base_ms = interval_to_ms(base_interval)
    if target_ms % base_ms != 0 or target_ms < base_ms:
        raise ValueError(f"{base_interval} cannot build {interval}")
    first = _ceil_to_interval(ordered[0].open_time_ms, target_ms)
    last = _floor_to_interval(ordered[-1].open_time_ms - (target_ms - base_ms), target_ms)
    if first > last:
        return None
    return first, last


def clip_parent_candles_to_complete_base_window(
    candles: list[Candle],
    *,
    base_candles: list[Candle],
    interval: str,
    base_interval: str = "5m",
) -> list[Candle]:
    window = complete_parent_window(base_candles, interval=interval, base_interval=base_interval)
    if window is None:
        return []
    start_ms, end_ms = window
    return [candle for candle in dedupe_candles(candles) if start_ms <= candle.open_time_ms <= end_ms]


def _ceil_to_interval(timestamp_ms: int, interval_ms: int) -> int:
    return timestamp_ms if timestamp_ms % interval_ms == 0 else timestamp_ms + (interval_ms - (timestamp_ms % interval_ms))


def _floor_to_interval(timestamp_ms: int, interval_ms: int) -> int:
    return timestamp_ms - (timestamp_ms % interval_ms)


def validate_built_native_candles(
    built: list[Candle],
    native: list[Candle],
    *,
    interval: str,
    min_samples: int = 1,
    value_rel_tol: float = 1e-8,
    value_abs_tol: float = 1e-8,
    max_value_mismatches: int = 20,
) -> ValidationReport:
    if not built:
        return ValidationReport(False, HealthReason.BUILT_EMPTY)
    if not native:
        return ValidationReport(False, HealthReason.NATIVE_EMPTY)
    if len(built) < min_samples:
        return ValidationReport(False, HealthReason.BUILT_SAMPLE_COUNT_BELOW_MINIMUM)
    if len(native) < min_samples:
        return ValidationReport(False, HealthReason.NATIVE_SAMPLE_COUNT_BELOW_MINIMUM)

    interval_ms = interval_to_ms(interval)
    timestamps = sorted({bar.open_time_ms for bar in [*built, *native]})
    misaligned = tuple(timestamp for timestamp in timestamps if timestamp % interval_ms != 0)
    if misaligned:
        return ValidationReport(False, HealthReason.TIMESTAMP_MISALIGNED, misaligned_timestamps=misaligned)

    built_times = {bar.open_time_ms for bar in built}
    native_times = {bar.open_time_ms for bar in native}
    missing_in_built = tuple(sorted(native_times - built_times))
    if missing_in_built:
        return ValidationReport(False, HealthReason.MISSING_IN_BUILT, missing_in_built=missing_in_built)
    missing_in_native = tuple(sorted(built_times - native_times))
    if missing_in_native:
        return ValidationReport(False, HealthReason.MISSING_IN_NATIVE, missing_in_native=missing_in_native)

    built_by_time = {bar.open_time_ms: bar for bar in built}
    native_by_time = {bar.open_time_ms: bar for bar in native}
    value_mismatches: list[dict[str, int | float | str]] = []
    for timestamp in sorted(built_times):
        built_bar = built_by_time[timestamp]
        native_bar = native_by_time[timestamp]
        for field_name in ("open", "high", "low", "close", "volume"):
            built_value = getattr(built_bar, field_name)
            native_value = getattr(native_bar, field_name)
            if math.isclose(built_value, native_value, rel_tol=value_rel_tol, abs_tol=value_abs_tol):
                continue
            value_mismatches.append(
                {
                    "timestamp_ms": timestamp,
                    "field": field_name,
                    "built": built_value,
                    "native": native_value,
                }
            )
            if len(value_mismatches) >= max_value_mismatches:
                break
        if len(value_mismatches) >= max_value_mismatches:
            break
    if value_mismatches:
        return ValidationReport(False, HealthReason.OHLCV_MISMATCH, value_mismatches=tuple(value_mismatches))
    return ValidationReport(True, HealthReason.OK)
