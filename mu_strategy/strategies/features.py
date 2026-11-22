"""Layer 2 shared read-only FeatureCache for strictly causal O(1) indicator access.

Pure Python 3.12 stdlib only (`array('d')`, `collections.deque`, `math`, `threading`).
Computes indicator arrays once over an underlying `BarSeries` in O(N) time and
exposes strictly causal read-only `FeatureSlice` views bounded by `BarSeriesSlice`
so future bars past `slice.end` are structurally unreachable.
"""

from __future__ import annotations

import math
import threading
from array import array
from collections import deque
from collections.abc import Iterator, Sequence
from typing import Any, overload

__all__ = [
    "MS_PER_YEAR_24_7",
    "FeatureSlice",
    "FeatureCache",
    "compute_ema",
    "compute_sma",
    "compute_rsi",
    "compute_macd",
    "compute_atr",
    "compute_donchian_high",
    "compute_donchian_low",
    "compute_realized_vol",
    "compute_zscore",
    "compute_vwap",
]

MS_PER_YEAR_24_7: float = 365.0 * 24.0 * 3600.0 * 1000.0
_NAN = math.nan
_isfinite = math.isfinite
_sqrt = math.sqrt
_log = math.log


def _validate_positive_int(value: Any, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _validate_positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    result = float(value)
    if not _isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if not (_isfinite(avg_gain) and _isfinite(avg_loss)):
        return _NAN
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def compute_ema(closes: Sequence[float], period: int) -> array:
    """Compute EMA matching `mu_strategy.indicators.ema` in O(N) into `array('d')`."""
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(closes)
    if n == 0:
        return array("d")

    alpha = 2.0 / (period + 1)
    one_minus_alpha = 1.0 - alpha
    out = [0.0] * n
    previous = float(closes[0])
    for i in range(n):
        previous = (closes[i] * alpha) + (previous * one_minus_alpha)
        out[i] = previous
    return array("d", out)


def compute_sma(closes: Sequence[float], period: int) -> array:
    """Compute O(N) rolling SMA with `math.nan` during warmup (`0..period-2`)."""
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(closes)
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n < period:
        return array("d", out)

    inv_period = 1.0 / period
    resync_every = period if period > 256 else 256
    window_sum = 0.0
    bad_count = 0

    for i in range(period):
        v = closes[i]
        if _isfinite(v):
            window_sum += v
        else:
            bad_count += 1
    if bad_count == 0:
        out[period - 1] = window_sum * inv_period

    since_resync = 0
    for i in range(period, n):
        old_v = closes[i - period]
        new_v = closes[i]
        if _isfinite(old_v):
            window_sum -= old_v
        else:
            bad_count -= 1
        if _isfinite(new_v):
            window_sum += new_v
        else:
            bad_count += 1

        since_resync += 1
        if since_resync >= resync_every and bad_count == 0:
            window_sum = math.fsum(closes[i - period + 1 : i + 1])
            since_resync = 0

        if bad_count == 0:
            out[i] = window_sum * inv_period

    return array("d", out)


def compute_rsi(closes: Sequence[float], period: int = 14) -> array:
    """Compute Wilder RSI in O(N) with `math.nan` warmup (`0..period-1`).

    For `index >= period`, matches `mu_strategy.indicators.rsi` exactly,
    preserving `_rsi_from_averages(0.0, 0.0) == 50.0`.
    """
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(closes)
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n <= period:
        return array("d", out)

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change >= 0.0:
            gains += change
        else:
            losses -= change

    inv_period = 1.0 / period
    prev_weight = period - 1
    avg_gain = gains * inv_period
    avg_loss = losses * inv_period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        if change > 0.0:
            avg_gain = ((avg_gain * prev_weight) + change) * inv_period
            avg_loss = (avg_loss * prev_weight) * inv_period
        elif change < 0.0:
            avg_gain = (avg_gain * prev_weight) * inv_period
            avg_loss = ((avg_loss * prev_weight) - change) * inv_period
        else:
            avg_gain = (avg_gain * prev_weight) * inv_period
            avg_loss = (avg_loss * prev_weight) * inv_period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)

    return array("d", out)


def compute_macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[array, array, array]:
    """Compute MACD `(line, signal, histogram)` matching `mu_strategy.indicators.macd`."""
    fast = _validate_positive_int(fast, "fast", minimum=1)
    slow = _validate_positive_int(slow, "slow", minimum=1)
    signal = _validate_positive_int(signal, "signal", minimum=1)
    if slow <= fast:
        raise ValueError("slow must be greater than fast")

    n = len(closes)
    if n == 0:
        return array("d"), array("d"), array("d")

    fast_arr = compute_ema(closes, fast)
    slow_arr = compute_ema(closes, slow)
    line_list = [fast_arr[i] - slow_arr[i] for i in range(n)]
    line_arr = array("d", line_list)
    signal_arr = compute_ema(line_arr, signal)
    hist_arr = array("d", (line_list[i] - signal_arr[i] for i in range(n)))
    return line_arr, signal_arr, hist_arr


def compute_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> array:
    """Compute Wilder ATR in O(N) with `math.nan` warmup (`0..period-2`)."""
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(closes)
    if len(highs) != n or len(lows) != n:
        raise ValueError("highs, lows, and closes must have equal length")
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n < period:
        return array("d", out)

    inv_period = 1.0 / period
    prev_weight = period - 1

    tr_sum = highs[0] - lows[0]
    for i in range(1, period):
        prev_close = closes[i - 1]
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - prev_close)
        lc = abs(lows[i] - prev_close)
        tr = hl if hl >= hc else hc
        if lc > tr:
            tr = lc
        tr_sum += tr

    atr_val = tr_sum * inv_period
    out[period - 1] = atr_val

    for i in range(period, n):
        prev_close = closes[i - 1]
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - prev_close)
        lc = abs(lows[i] - prev_close)
        tr = hl if hl >= hc else hc
        if lc > tr:
            tr = lc
        atr_val = ((atr_val * prev_weight) + tr) * inv_period
        out[i] = atr_val

    return array("d", out)


def compute_donchian_high(highs: Sequence[float], period: int) -> array:
    """Compute O(N) rolling highest high using a monotonic deque (`math.nan` warmup)."""
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(highs)
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n < period:
        return array("d", out)

    dq: deque[int] = deque()
    bad_count = 0

    for i in range(n):
        if i >= period and not _isfinite(highs[i - period]):
            bad_count -= 1

        val = highs[i]
        if _isfinite(val):
            while dq and highs[dq[-1]] <= val:
                dq.pop()
            dq.append(i)
        else:
            bad_count += 1

        window_start = i - period + 1
        while dq and dq[0] < window_start:
            dq.popleft()

        if i >= period - 1 and bad_count == 0 and dq:
            out[i] = highs[dq[0]]

    return array("d", out)


def compute_donchian_low(lows: Sequence[float], period: int) -> array:
    """Compute O(N) rolling lowest low using a monotonic deque (`math.nan` warmup)."""
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(lows)
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n < period:
        return array("d", out)

    dq: deque[int] = deque()
    bad_count = 0

    for i in range(n):
        if i >= period and not _isfinite(lows[i - period]):
            bad_count -= 1

        val = lows[i]
        if _isfinite(val):
            while dq and lows[dq[-1]] >= val:
                dq.pop()
            dq.append(i)
        else:
            bad_count += 1

        window_start = i - period + 1
        while dq and dq[0] < window_start:
            dq.popleft()

        if i >= period - 1 and bad_count == 0 and dq:
            out[i] = lows[dq[0]]

    return array("d", out)


def _exact_window_stats(values: Sequence[float], start: int, end: int) -> tuple[float, float]:
    """Compute exact (mean, sum_sq_dev) over values[start:end] via two-pass summation."""
    count = end - start
    mean = math.fsum(values[start:end]) / count
    ssd = math.fsum((values[j] - mean) * (values[j] - mean) for j in range(start, end))
    return mean, ssd


def compute_realized_vol(
    closes: Sequence[float],
    period: int = 20,
    periods_per_year: float = 8760.0,
) -> array:
    """Compute O(N) annualized rolling sample std (`ddof=1`) of log returns.

    Indices `0..period-1` are `math.nan` (requires `period` log returns, i.e., `period + 1` closes).
    """
    period = _validate_positive_int(period, "period", minimum=2)
    periods_per_year = _validate_positive_float(periods_per_year, "periods_per_year")
    n = len(closes)
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n <= period:
        return array("d", out)

    returns = [_NAN] * n
    for i in range(1, n):
        c0 = closes[i - 1]
        c1 = closes[i]
        if c0 > 0.0 and c1 > 0.0 and _isfinite(c0) and _isfinite(c1):
            returns[i] = _log(c1 / c0)

    ann_factor = _sqrt(periods_per_year)
    inv_period = 1.0 / period
    inv_dof = 1.0 / (period - 1)
    resync_every = period if period > 256 else 256

    anchor = 0.0
    sum_d = 0.0
    sum_d2 = 0.0
    bad_count = 0

    for i in range(1, period + 1):
        r = returns[i]
        if _isfinite(r):
            sum_d += r
            sum_d2 += r * r
        else:
            bad_count += 1

    if bad_count == 0:
        anchor = sum_d * inv_period
        _, ssd = _exact_window_stats(returns, 1, period + 1)
        sum_d = 0.0
        sum_d2 = ssd
        vol = _sqrt(ssd * inv_dof) * ann_factor if ssd > 0.0 else 0.0
        out[period] = 0.0 if vol == 0.0 else vol

    since_resync = 0
    for i in range(period + 1, n):
        old_r = returns[i - period]
        new_r = returns[i]
        if _isfinite(old_r):
            d_old = old_r - anchor
            sum_d -= d_old
            sum_d2 -= d_old * d_old
        else:
            bad_count -= 1

        if _isfinite(new_r):
            d_new = new_r - anchor
            sum_d += d_new
            sum_d2 += d_new * d_new
        else:
            bad_count += 1

        since_resync += 1
        if bad_count != 0:
            continue

        ssd = sum_d2 - (sum_d * sum_d) * inv_period
        if since_resync >= resync_every or ssd <= 1e-12 * sum_d2 or ssd < 0.0:
            mean, ssd = _exact_window_stats(returns, i - period + 1, i + 1)
            anchor = mean
            sum_d = 0.0
            sum_d2 = ssd
            since_resync = 0

        vol = _sqrt(ssd * inv_dof) * ann_factor if ssd > 0.0 else 0.0
        out[i] = 0.0 if vol == 0.0 else vol

    return array("d", out)


def compute_zscore(closes: Sequence[float], period: int = 20) -> array:
    """Compute O(N) rolling z-score (`(close - mean) / sample_std`, `ddof=1`).

    Indices `0..period-2` are `math.nan`. Flat windows (`std == 0`) yield `0.0`.
    Uses anchor-shifted rolling sums with amortized exact resync for full float64 precision.
    """
    period = _validate_positive_int(period, "period", minimum=2)
    n = len(closes)
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n < period:
        return array("d", out)

    inv_period = 1.0 / period
    inv_dof = 1.0 / (period - 1)
    resync_every = period if period > 256 else 256

    bad_count = 0
    anchor = 0.0
    for i in range(period):
        if not _isfinite(closes[i]):
            bad_count += 1

    sum_d = 0.0
    sum_d2 = 0.0
    if bad_count == 0:
        mean, ssd = _exact_window_stats(closes, 0, period)
        anchor = mean
        sum_d = 0.0
        sum_d2 = ssd
        std = _sqrt(ssd * inv_dof) if ssd > 0.0 else 0.0
        eps = 1e-12 * max(1.0, abs(mean))
        if std <= eps:
            out[period - 1] = 0.0
        else:
            z = (closes[period - 1] - mean) / std
            out[period - 1] = 0.0 if z == 0.0 else z
    else:
        for i in range(period):
            v = closes[i]
            if _isfinite(v):
                sum_d += v
                sum_d2 += v * v

    since_resync = 0
    for i in range(period, n):
        old_v = closes[i - period]
        new_v = closes[i]
        if _isfinite(old_v):
            d_old = old_v - anchor
            sum_d -= d_old
            sum_d2 -= d_old * d_old
        else:
            bad_count -= 1

        if _isfinite(new_v):
            d_new = new_v - anchor
            sum_d += d_new
            sum_d2 += d_new * d_new
        else:
            bad_count += 1

        since_resync += 1
        if bad_count != 0:
            continue

        ssd = sum_d2 - (sum_d * sum_d) * inv_period
        if since_resync >= resync_every or ssd <= 1e-12 * sum_d2 or ssd < 0.0:
            mean, ssd = _exact_window_stats(closes, i - period + 1, i + 1)
            anchor = mean
            sum_d = 0.0
            sum_d2 = ssd
            since_resync = 0
        else:
            mean = anchor + (sum_d * inv_period)

        std = _sqrt(ssd * inv_dof) if ssd > 0.0 else 0.0
        eps = 1e-12 * max(1.0, abs(mean))
        if std <= eps:
            out[i] = 0.0
        else:
            z = (new_v - mean) / std
            out[i] = 0.0 if z == 0.0 else z

    return array("d", out)


def compute_vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    period: int = 20,
) -> array:
    """Compute O(N) rolling typical-price VWAP (`math.nan` during warmup or zero volume)."""
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(closes)
    if len(highs) != n or len(lows) != n or len(volumes) != n:
        raise ValueError("highs, lows, closes, and volumes must have equal length")
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n < period:
        return array("d", out)

    resync_every = period if period > 256 else 256
    pv_vals = [_NAN] * n
    vol_vals = [_NAN] * n
    for i in range(n):
        h = highs[i]
        l = lows[i]
        c = closes[i]
        v = volumes[i]
        if _isfinite(h) and _isfinite(l) and _isfinite(c) and _isfinite(v) and v >= 0.0:
            pv_vals[i] = ((h + l + c) / 3.0) * v
            vol_vals[i] = v

    sum_pv = 0.0
    sum_v = 0.0
    bad_count = 0
    for i in range(period):
        pv = pv_vals[i]
        v = vol_vals[i]
        if _isfinite(pv):
            sum_pv += pv
            sum_v += v
        else:
            bad_count += 1
    if bad_count == 0 and sum_v > 0.0:
        out[period - 1] = sum_pv / sum_v

    since_resync = 0
    for i in range(period, n):
        old_pv = pv_vals[i - period]
        new_pv = pv_vals[i]
        if _isfinite(old_pv):
            sum_pv -= old_pv
            sum_v -= vol_vals[i - period]
        else:
            bad_count -= 1

        if _isfinite(new_pv):
            sum_pv += new_pv
            sum_v += vol_vals[i]
        else:
            bad_count += 1

        since_resync += 1
        if since_resync >= resync_every and bad_count == 0:
            sum_pv = math.fsum(pv_vals[i - period + 1 : i + 1])
            sum_v = math.fsum(vol_vals[i - period + 1 : i + 1])
            since_resync = 0

        if bad_count == 0 and sum_v > 0.0:
            out[i] = sum_pv / sum_v

    return array("d", out)


class FeatureSlice(Sequence[float]):
    """Strictly causal, zero-copy read-only view into a cached `array('d')`.

    Indexing is bounded to `[start, end)` so elements at or beyond `end`
    (future bars) can never be read. Mutation is impossible.
    """

    __slots__ = ("_data", "_start", "_end")

    def __init__(self, data: array, start: int, end: int) -> None:
        if start < 0 or end < start or end > len(data):
            raise ValueError(f"invalid slice bounds [{start}, {end}) for length {len(data)}")
        self._data = data
        self._start = start
        self._end = end

    def __len__(self) -> int:
        return self._end - self._start

    @property
    def start(self) -> int:
        return self._start

    @property
    def end(self) -> int:
        return self._end

    @property
    def latest(self) -> float:
        if self._end <= self._start:
            return _NAN
        return self._data[self._end - 1]

    @overload
    def __getitem__(self, index: int) -> float: ...

    @overload
    def __getitem__(self, index: slice) -> FeatureSlice: ...

    def __getitem__(self, index: int | slice) -> float | FeatureSlice:
        length = self._end - self._start
        if isinstance(index, slice):
            start_off, stop_off, step = index.indices(length)
            if step == 1:
                if stop_off < start_off:
                    stop_off = start_off
                return FeatureSlice(self._data, self._start + start_off, self._start + stop_off)
            sub = array("d", (self._data[self._start + i] for i in range(start_off, stop_off, step)))
            return FeatureSlice(sub, 0, len(sub))
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("FeatureSlice indices must be integers or slices")
        if index < 0:
            index += length
        if index < 0 or index >= length:
            raise IndexError("FeatureSlice index out of range")
        return self._data[self._start + index]

    def __iter__(self) -> Iterator[float]:
        data = self._data
        for idx in range(self._start, self._end):
            yield data[idx]

    def to_tuple(self) -> tuple[float, ...]:
        return tuple(self._data[self._start : self._end])

    def __repr__(self) -> str:
        return f"FeatureSlice(start={self._start}, end={self._end}, len={self._end - self._start})"


def _unwrap_series_bounds(series: Any) -> tuple[Any, int, int]:
    """Extract `(underlying_bar_series, start, end)` from a `BarSeries` or `BarSeriesSlice`."""
    base = getattr(series, "_series", None)
    if base is not None and base is not series:
        return base, int(series.start), int(series.end)
    base = getattr(series, "series", None)
    if base is None:
        base = getattr(series, "parent", None)
    if base is not None and base is not series:
        start = getattr(series, "start", getattr(series, "_start", getattr(series, "start_idx", 0)))
        end = getattr(series, "end", getattr(series, "_end", getattr(series, "end_idx", start + len(series))))
        return base, int(start), int(end)
    return series, 0, len(series)


def _infer_periods_per_year(base: Any, periods_per_year: float | None) -> float:
    if periods_per_year is not None:
        return _validate_positive_float(periods_per_year, "periods_per_year")
    interval_ms = getattr(base, "interval_ms", None)
    if isinstance(interval_ms, int) and not isinstance(interval_ms, bool) and interval_ms > 0:
        return MS_PER_YEAR_24_7 / float(interval_ms)
    return 8760.0


class FeatureCache:
    """Thread-safe shared read-only indicator cache keyed by underlying `BarSeries`.

    Computes each indicator once over the full underlying `BarSeries` as `array('d')`
    and serves strictly causal `FeatureSlice` views (or O(1) bounded scalar values)
    capped to the caller's `BarSeriesSlice` `[start, end)`.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Maps id(base_series) -> (base_series_ref, {feature_key: array | tuple[array, ...]})
        # Holding base_series_ref prevents id() reuse while cached.
        self._store: dict[int, tuple[Any, dict[tuple[Any, ...], Any]]] = {}
        self._generation: int = 0
        self._hits = 0
        self._misses = 0

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._generation += 1
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            entries = sum(len(bucket) for _, bucket in self._store.values())
            return {
                "series_count": len(self._store),
                "feature_count": entries,
                "hits": self._hits,
                "misses": self._misses,
            }

    def _get_or_compute(self, base: Any, key: tuple[Any, ...], compute_fn: Any) -> Any:
        series_id = id(base)
        entry = self._store.get(series_id)
        if entry is not None and entry[0] is base:
            cached = entry[1].get(key)
            if cached is not None:
                self._hits += 1
                return cached

        with self._lock:
            entry = self._store.get(series_id)
            if entry is not None and entry[0] is base:
                cached = entry[1].get(key)
                if cached is not None:
                    self._hits += 1
                    return cached
            value = compute_fn(base)
            if entry is None or entry[0] is not base:
                bucket: dict[tuple[Any, ...], Any] = {key: value}
                self._store[series_id] = (base, bucket)
            else:
                entry[1][key] = value
            self._misses += 1
            return value

    def _scalar_from_array(self, arr: array, start: int, end: int, offset: int) -> float:
        length = end - start
        idx = (length + offset) if offset < 0 else offset
        if idx < 0 or idx >= length:
            raise IndexError("feature index out of bounds for causal BarSeriesSlice")
        return arr[start + idx]

    # ---- EMA ----
    def _ema_array(self, base: Any, period: int) -> array:
        period = _validate_positive_int(period, "period", minimum=1)
        return self._get_or_compute(base, ("ema", period), lambda s: compute_ema(s.closes, period))

    def ema(self, series: Any, period: int) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._ema_array(base, period), start, end)

    def ema_value(self, series: Any, period: int, offset: int = -1) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(self._ema_array(base, period), start, end, offset)

    # ---- SMA ----
    def _sma_array(self, base: Any, period: int) -> array:
        period = _validate_positive_int(period, "period", minimum=1)
        return self._get_or_compute(base, ("sma", period), lambda s: compute_sma(s.closes, period))

    def sma(self, series: Any, period: int) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._sma_array(base, period), start, end)

    def sma_value(self, series: Any, period: int, offset: int = -1) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(self._sma_array(base, period), start, end, offset)

    # ---- RSI ----
    def _rsi_array(self, base: Any, period: int) -> array:
        period = _validate_positive_int(period, "period", minimum=1)
        return self._get_or_compute(base, ("rsi", period), lambda s: compute_rsi(s.closes, period))

    def rsi(self, series: Any, period: int = 14) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._rsi_array(base, period), start, end)

    def rsi_value(self, series: Any, period: int = 14, offset: int = -1) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(self._rsi_array(base, period), start, end, offset)

    # ---- MACD ----
    def _macd_arrays(self, base: Any, fast: int, slow: int, signal: int) -> tuple[array, array, array]:
        fast = _validate_positive_int(fast, "fast", minimum=1)
        slow = _validate_positive_int(slow, "slow", minimum=1)
        signal = _validate_positive_int(signal, "signal", minimum=1)
        if slow <= fast:
            raise ValueError("slow must be greater than fast")
        return self._get_or_compute(
            base,
            ("macd", fast, slow, signal),
            lambda s: compute_macd(s.closes, fast, slow, signal),
        )

    def macd(
        self,
        series: Any,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> tuple[FeatureSlice, FeatureSlice, FeatureSlice]:
        base, start, end = _unwrap_series_bounds(series)
        line, sig, hist = self._macd_arrays(base, fast, slow, signal)
        return (
            FeatureSlice(line, start, end),
            FeatureSlice(sig, start, end),
            FeatureSlice(hist, start, end),
        )

    def macd_values(
        self,
        series: Any,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        offset: int = -1,
    ) -> tuple[float, float, float]:
        base, start, end = _unwrap_series_bounds(series)
        line, sig, hist = self._macd_arrays(base, fast, slow, signal)
        return (
            self._scalar_from_array(line, start, end, offset),
            self._scalar_from_array(sig, start, end, offset),
            self._scalar_from_array(hist, start, end, offset),
        )

    # ---- ATR ----
    def _atr_array(self, base: Any, period: int) -> array:
        period = _validate_positive_int(period, "period", minimum=1)
        return self._get_or_compute(
            base,
            ("atr", period),
            lambda s: compute_atr(s.highs, s.lows, s.closes, period),
        )

    def atr(self, series: Any, period: int = 14) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._atr_array(base, period), start, end)

    def atr_value(self, series: Any, period: int = 14, offset: int = -1) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(self._atr_array(base, period), start, end, offset)

    # ---- Donchian High / Low ----
    def _donchian_high_array(self, base: Any, period: int) -> array:
        period = _validate_positive_int(period, "period", minimum=1)
        return self._get_or_compute(
            base,
            ("donchian_high", period),
            lambda s: compute_donchian_high(s.highs, period),
        )

    def donchian_high(self, series: Any, period: int) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._donchian_high_array(base, period), start, end)

    def donchian_high_value(self, series: Any, period: int, offset: int = -1) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(self._donchian_high_array(base, period), start, end, offset)

    def _donchian_low_array(self, base: Any, period: int) -> array:
        period = _validate_positive_int(period, "period", minimum=1)
        return self._get_or_compute(
            base,
            ("donchian_low", period),
            lambda s: compute_donchian_low(s.lows, period),
        )

    def donchian_low(self, series: Any, period: int) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._donchian_low_array(base, period), start, end)

    def donchian_low_value(self, series: Any, period: int, offset: int = -1) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(self._donchian_low_array(base, period), start, end, offset)

    # ---- Realized Volatility ----
    def _realized_vol_array(self, base: Any, period: int, periods_per_year: float | None) -> array:
        period = _validate_positive_int(period, "period", minimum=2)
        ppy = _infer_periods_per_year(base, periods_per_year)
        return self._get_or_compute(
            base,
            ("realized_vol", period, ppy),
            lambda s: compute_realized_vol(s.closes, period, ppy),
        )

    def realized_vol(
        self,
        series: Any,
        period: int = 20,
        periods_per_year: float | None = None,
    ) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._realized_vol_array(base, period, periods_per_year), start, end)

    def realized_vol_value(
        self,
        series: Any,
        period: int = 20,
        periods_per_year: float | None = None,
        offset: int = -1,
    ) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(
            self._realized_vol_array(base, period, periods_per_year),
            start,
            end,
            offset,
        )

    # ---- Z-Score ----
    def _zscore_array(self, base: Any, period: int) -> array:
        period = _validate_positive_int(period, "period", minimum=2)
        return self._get_or_compute(base, ("zscore", period), lambda s: compute_zscore(s.closes, period))

    def zscore(self, series: Any, period: int = 20) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._zscore_array(base, period), start, end)

    def zscore_value(self, series: Any, period: int = 20, offset: int = -1) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(self._zscore_array(base, period), start, end, offset)

    # ---- VWAP ----
    def _vwap_array(self, base: Any, period: int) -> array:
        period = _validate_positive_int(period, "period", minimum=1)
        return self._get_or_compute(
            base,
            ("vwap", period),
            lambda s: compute_vwap(s.highs, s.lows, s.closes, s.volumes, period),
        )

    def vwap(self, series: Any, period: int = 20) -> FeatureSlice:
        base, start, end = _unwrap_series_bounds(series)
        return FeatureSlice(self._vwap_array(base, period), start, end)

    def vwap_value(self, series: Any, period: int = 20, offset: int = -1) -> float:
        base, start, end = _unwrap_series_bounds(series)
        return self._scalar_from_array(self._vwap_array(base, period), start, end, offset)
