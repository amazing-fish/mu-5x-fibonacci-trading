from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

from mu_strategy.market_data.trusted_data.contracts import HealthReason, ValidationReport
from mu_strategy.market_data.utils import dedupe_candles, interval_to_ms
from mu_strategy.models import Candle


def _candle_open_time_ms(candle: Candle) -> int:
    return candle.open_time_ms


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
    isfinite = math.isfinite

    misaligned: list[int] = []
    timestamp_gaps: list[dict[str, int]] = []
    ohlcv_invalid = False
    continuity_gap = False

    first = ordered[0]
    first_ts = first.open_time_ms
    if first_ts % interval_ms != 0:
        misaligned.append(first_ts)
    o = first.open
    h = first.high
    l = first.low
    c = first.close
    v = first.volume
    if not (
        isfinite(o) and o > 0
        and isfinite(h) and h > 0
        and isfinite(l) and l > 0
        and isfinite(c) and c > 0
        and isfinite(v) and v >= 0
        and h >= o and h >= c
        and l <= o and l <= c
    ):
        ohlcv_invalid = True

    prev_ts = first_ts
    prev_close = c
    for index in range(1, len(ordered)):
        candle = ordered[index]
        ts = candle.open_time_ms
        if ts % interval_ms != 0:
            misaligned.append(ts)
        delta_ms = ts - prev_ts
        if delta_ms != interval_ms and len(timestamp_gaps) < max_timestamp_gaps:
            timestamp_gaps.append(
                {
                    "previous_timestamp_ms": prev_ts,
                    "current_timestamp_ms": ts,
                    "expected_interval_ms": interval_ms,
                    "actual_interval_ms": delta_ms,
                    "missing_count": max(0, (delta_ms // interval_ms) - 1),
                }
            )
        if not ohlcv_invalid:
            o = candle.open
            h = candle.high
            l = candle.low
            c = candle.close
            v = candle.volume
            if not (
                isfinite(o) and o > 0
                and isfinite(h) and h > 0
                and isfinite(l) and l > 0
                and isfinite(c) and c > 0
                and isfinite(v) and v >= 0
                and h >= o and h >= c
                and l <= o and l <= c
            ):
                ohlcv_invalid = True
        if not continuity_gap and prev_close != 0:
            if abs((candle.open / prev_close) - 1.0) > max_gap_pct:
                continuity_gap = True
        prev_ts = ts
        prev_close = candle.close

    if misaligned:
        return ordered, ValidationReport(False, HealthReason.TIMESTAMP_MISALIGNED, misaligned_timestamps=tuple(misaligned))
    if timestamp_gaps:
        return ordered, ValidationReport(False, HealthReason.TIMESTAMP_GAP, timestamp_gaps=tuple(timestamp_gaps))
    if ohlcv_invalid:
        return ordered, ValidationReport(False, HealthReason.OHLCV_INVALID)
    if continuity_gap:
        return ordered, ValidationReport(False, HealthReason.CONTINUITY_GAP)
    return ordered, ValidationReport(True, HealthReason.OK)


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

    ordered = dedupe_candles(candles)
    if not ordered:
        return []

    output: list[Candle] = []
    append = output.append
    candle_cls = Candle

    if ohlc_policy == "standard":
        first = ordered[0]
        cur_bucket = first.open_time_ms - (first.open_time_ms % target_ms)
        count = 1
        b_open = first.open
        b_high = first.high
        b_low = first.low
        b_close = first.close
        b_vol = first.volume
        for index in range(1, len(ordered)):
            row = ordered[index]
            ts = row.open_time_ms
            bucket = ts - (ts % target_ms)
            if bucket == cur_bucket:
                count += 1
                if row.high > b_high:
                    b_high = row.high
                if row.low < b_low:
                    b_low = row.low
                b_close = row.close
                b_vol += row.volume
            else:
                if count == expected:
                    append(candle_cls(cur_bucket, b_open, b_high, b_low, b_close, b_vol))
                cur_bucket = bucket
                count = 1
                b_open = row.open
                b_high = row.high
                b_low = row.low
                b_close = row.close
                b_vol = row.volume
        if count == expected:
            append(candle_cls(cur_bucket, b_open, b_high, b_low, b_close, b_vol))
        return output

    # okx_native policy: single O(N) pass tracking nonzero-volume OHLC & all-zero consistency.
    first = ordered[0]
    cur_bucket = first.open_time_ms - (first.open_time_ms % target_ms)
    count = 1
    b_vol = first.volume
    has_active = first.volume > 0
    a_open = first.open
    a_high = first.high
    a_low = first.low
    a_close = first.close
    z_open = first.open
    z_high = first.high
    z_low = first.low
    z_close = first.close
    z_consistent = True

    for index in range(1, len(ordered)):
        row = ordered[index]
        ts = row.open_time_ms
        bucket = ts - (ts % target_ms)
        if bucket == cur_bucket:
            count += 1
            b_vol += row.volume
            if row.volume > 0:
                if not has_active:
                    has_active = True
                    a_open = row.open
                    a_high = row.high
                    a_low = row.low
                    a_close = row.close
                else:
                    if row.high > a_high:
                        a_high = row.high
                    if row.low < a_low:
                        a_low = row.low
                    a_close = row.close
            elif not has_active and z_consistent:
                if row.open != z_open or row.high != z_high or row.low != z_low or row.close != z_close:
                    z_consistent = False
        else:
            if count == expected:
                if has_active:
                    append(candle_cls(cur_bucket, a_open, a_high, a_low, a_close, b_vol))
                elif not z_consistent:
                    raise ValueError(f"inconsistent all-zero no-trade bucket at {cur_bucket}")
                else:
                    append(candle_cls(cur_bucket, z_open, z_high, z_low, z_close, b_vol))
            cur_bucket = bucket
            count = 1
            b_vol = row.volume
            has_active = row.volume > 0
            a_open = row.open
            a_high = row.high
            a_low = row.low
            a_close = row.close
            z_open = row.open
            z_high = row.high
            z_low = row.low
            z_close = row.close
            z_consistent = True

    if count == expected:
        if has_active:
            append(candle_cls(cur_bucket, a_open, a_high, a_low, a_close, b_vol))
        elif not z_consistent:
            raise ValueError(f"inconsistent all-zero no-trade bucket at {cur_bucket}")
        else:
            append(candle_cls(cur_bucket, z_open, z_high, z_low, z_close, b_vol))
    return output


def complete_parent_window_from_bounds(
    first_base_ms: int,
    last_base_ms: int,
    *,
    interval: str,
    base_interval: str = "5m",
) -> tuple[int, int] | None:
    target_ms = interval_to_ms(interval)
    base_ms = interval_to_ms(base_interval)
    if target_ms % base_ms != 0 or target_ms < base_ms:
        raise ValueError(f"{base_interval} cannot build {interval}")
    first = _ceil_to_interval(first_base_ms, target_ms)
    last = _floor_to_interval(last_base_ms - (target_ms - base_ms), target_ms)
    if first > last:
        return None
    return first, last


def complete_parent_window(
    base_candles: list[Candle],
    *,
    interval: str,
    base_interval: str = "5m",
) -> tuple[int, int] | None:
    ordered = dedupe_candles(base_candles)
    if not ordered:
        return None
    return complete_parent_window_from_bounds(
        ordered[0].open_time_ms,
        ordered[-1].open_time_ms,
        interval=interval,
        base_interval=base_interval,
    )


def clip_parent_candles_to_bounds(
    candles: list[Candle],
    *,
    start_ms: int,
    end_ms: int,
) -> list[Candle]:
    if not candles:
        return []
    ordered = dedupe_candles(candles)
    if ordered[0].open_time_ms >= start_ms and ordered[-1].open_time_ms <= end_ms:
        return ordered
    lo = bisect_left(ordered, start_ms, key=_candle_open_time_ms)
    hi = bisect_right(ordered, end_ms, key=_candle_open_time_ms)
    return ordered[lo:hi]


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
    return clip_parent_candles_to_bounds(candles, start_ms=start_ms, end_ms=end_ms)


def _ceil_to_interval(timestamp_ms: int, interval_ms: int) -> int:
    rem = timestamp_ms % interval_ms
    return timestamp_ms if rem == 0 else timestamp_ms + (interval_ms - rem)


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
    isclose = math.isclose

    # Fast lockstep O(N) path when both sequences are strictly sorted, aligned, and timestamp-matched.
    if len(built) == len(native):
        fast_aligned = True
        prev_ts: int | None = None
        value_mismatches: list[dict[str, int | float | str]] = []
        for index in range(len(built)):
            b = built[index]
            n = native[index]
            ts = b.open_time_ms
            if ts != n.open_time_ms or ts % interval_ms != 0 or (prev_ts is not None and ts <= prev_ts):
                fast_aligned = False
                break
            prev_ts = ts
            if len(value_mismatches) < max_value_mismatches:
                if (
                    b.open != n.open
                    or b.high != n.high
                    or b.low != n.low
                    or b.close != n.close
                    or b.volume != n.volume
                ):
                    for field_name, bv, nv in (
                        ("open", b.open, n.open),
                        ("high", b.high, n.high),
                        ("low", b.low, n.low),
                        ("close", b.close, n.close),
                        ("volume", b.volume, n.volume),
                    ):
                        if not isclose(bv, nv, rel_tol=value_rel_tol, abs_tol=value_abs_tol):
                            value_mismatches.append(
                                {
                                    "timestamp_ms": ts,
                                    "field": field_name,
                                    "built": bv,
                                    "native": nv,
                                }
                            )
                            if len(value_mismatches) >= max_value_mismatches:
                                break
        if fast_aligned:
            if value_mismatches:
                return ValidationReport(False, HealthReason.OHLCV_MISMATCH, value_mismatches=tuple(value_mismatches))
            return ValidationReport(True, HealthReason.OK)

    # Full diagnostic path for mismatched/misaligned/unsorted inputs.
    built_by_time = {bar.open_time_ms: bar for bar in built}
    native_by_time = {bar.open_time_ms: bar for bar in native}
    timestamps = sorted(built_by_time.keys() | native_by_time.keys())
    misaligned = tuple(timestamp for timestamp in timestamps if timestamp % interval_ms != 0)
    if misaligned:
        return ValidationReport(False, HealthReason.TIMESTAMP_MISALIGNED, misaligned_timestamps=misaligned)

    missing_in_built = tuple(sorted(native_by_time.keys() - built_by_time.keys()))
    if missing_in_built:
        return ValidationReport(False, HealthReason.MISSING_IN_BUILT, missing_in_built=missing_in_built)
    missing_in_native = tuple(sorted(built_by_time.keys() - native_by_time.keys()))
    if missing_in_native:
        return ValidationReport(False, HealthReason.MISSING_IN_NATIVE, missing_in_native=missing_in_native)

    value_mismatches = []
    for timestamp in timestamps:
        b = built_by_time[timestamp]
        n = native_by_time[timestamp]
        for field_name, bv, nv in (
            ("open", b.open, n.open),
            ("high", b.high, n.high),
            ("low", b.low, n.low),
            ("close", b.close, n.close),
            ("volume", b.volume, n.volume),
        ):
            if isclose(bv, nv, rel_tol=value_rel_tol, abs_tol=value_abs_tol):
                continue
            value_mismatches.append(
                {
                    "timestamp_ms": timestamp,
                    "field": field_name,
                    "built": bv,
                    "native": nv,
                }
            )
            if len(value_mismatches) >= max_value_mismatches:
                break
        if len(value_mismatches) >= max_value_mismatches:
            break
    if value_mismatches:
        return ValidationReport(False, HealthReason.OHLCV_MISMATCH, value_mismatches=tuple(value_mismatches))
    return ValidationReport(True, HealthReason.OK)
