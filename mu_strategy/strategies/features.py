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
_INF = math.inf
_isfinite = math.isfinite
_sqrt = math.sqrt
_log = math.log
_frexp = math.frexp
_ldexp = math.ldexp


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


def _neumaier_add(total: float, comp: float, value: float) -> tuple[float, float]:
    """Accumulate `value` into `(total, comp)` using Neumaier compensated summation."""
    t = total + value
    if abs(total) >= abs(value):
        comp += (total - t) + value
    else:
        comp += (value - t) + total
    return t, comp


def _safe_window_mean(values: Sequence[float], start: int, end: int) -> float:
    """Compute overflow-safe exact mean of finite ``values[start:end]`` via power-of-2 scaling."""
    count = end - start
    max_abs = 0.0
    for j in range(start, end):
        a = abs(values[j])
        if a > max_abs:
            max_abs = a
    if max_abs == 0.0:
        return 0.0
    _, exp = _frexp(max_abs)
    scaled_mean = math.fsum(_ldexp(values[j], -exp) for j in range(start, end)) / count
    return _ldexp(scaled_mean, exp)


def compute_sma(closes: Sequence[float], period: int) -> array:
    """Compute O(N) rolling SMA with `math.nan` during warmup (`0..period-2`).

    Uses a zero-subtraction two-stack sliding window with Neumaier compensated
    sums so a dominant outgoing value (e.g. `[1e16, 1, 1]`) never masks retained
    values while remaining strictly O(N) for all periods.
    """
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(closes)
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n < period:
        return array("d", out)

    inv_period = 1.0 / period
    # Each stack stores tuples of (value_or_0, bad_flag, agg_sum, agg_comp, agg_bad).
    in_stack: list[tuple[float, int, float, float, int]] = []
    out_stack: list[tuple[float, int, float, float, int]] = []

    for i in range(n):
        v = closes[i]
        if _isfinite(v):
            val = float(v)
            bad = 0
        else:
            val = 0.0
            bad = 1

        if in_stack:
            _, _, prev_sum, prev_comp, prev_bad = in_stack[-1]
            agg_sum, agg_comp = _neumaier_add(prev_sum, prev_comp, val)
            agg_bad = prev_bad + bad
        else:
            agg_sum, agg_comp, agg_bad = val, 0.0, bad
        in_stack.append((val, bad, agg_sum, agg_comp, agg_bad))

        if i >= period:
            if not out_stack:
                s_sum = 0.0
                s_comp = 0.0
                s_bad = 0
                while in_stack:
                    e_val, e_bad, _, _, _ = in_stack.pop()
                    s_sum, s_comp = _neumaier_add(s_sum, s_comp, e_val)
                    s_bad += e_bad
                    out_stack.append((e_val, e_bad, s_sum, s_comp, s_bad))
            out_stack.pop()

        if i >= period - 1:
            total_bad = (in_stack[-1][4] if in_stack else 0) + (out_stack[-1][4] if out_stack else 0)
            if total_bad == 0:
                if in_stack and out_stack:
                    s_sum, s_comp = _neumaier_add(out_stack[-1][2], out_stack[-1][3], in_stack[-1][2])
                    s_sum, s_comp = _neumaier_add(s_sum, s_comp, in_stack[-1][3])
                    window_sum = s_sum + s_comp
                elif in_stack:
                    window_sum = in_stack[-1][2] + in_stack[-1][3]
                else:
                    window_sum = out_stack[-1][2] + out_stack[-1][3]
                if _isfinite(window_sum):
                    out[i] = window_sum * inv_period
                else:
                    out[i] = _safe_window_mean(closes, i - period + 1, i + 1)

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


def _exact_window_stats_scaled(values: Sequence[float], start: int, end: int) -> tuple[float, float, int]:
    """Compute overflow/underflow-safe ``(scaled_mean, scaled_ssd, scale_exp)`` over ``values[start:end]``.

    Values are scaled by ``2**(-scale_exp)`` (exact IEEE-754 exponent shift with zero
    mantissa rounding loss). For ordinary magnitudes (``1e-100 <= max_abs <= 1e100``),
    ``scale_exp == 0`` so callers pay zero scaling overhead on normal market prices.
    """
    count = end - start
    max_abs = 0.0
    for j in range(start, end):
        a = abs(values[j])
        if a > max_abs:
            max_abs = a
    if max_abs == 0.0:
        return 0.0, 0.0, 0
    if 1e-100 <= max_abs <= 1e100:
        mean = math.fsum(values[start:end]) / count
        ssd = math.fsum((values[j] - mean) * (values[j] - mean) for j in range(start, end))
        return mean, ssd, 0
    _, exp = _frexp(max_abs)
    scaled_vals = [_ldexp(values[j], -exp) for j in range(start, end)]
    mean = math.fsum(scaled_vals) / count
    ssd = math.fsum((v - mean) * (v - mean) for v in scaled_vals)
    return mean, ssd, exp


def _exact_window_stats(values: Sequence[float], start: int, end: int) -> tuple[float, float]:
    """Compute exact (mean, sum_sq_dev) over values[start:end] via two-pass summation."""
    mean, ssd, exp = _exact_window_stats_scaled(values, start, end)
    if exp == 0:
        return mean, ssd
    return _ldexp(mean, exp), _ldexp(ssd, 2 * exp)


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
            ratio = c1 / c0
            if 0.0 < ratio < _INF:
                returns[i] = _log(ratio)
            else:
                returns[i] = _log(c1) - _log(c0)

    ann_factor = _sqrt(periods_per_year)
    inv_period = 1.0 / period
    inv_dof = 1.0 / (period - 1)
    resync_every = period if period > 256 else 256

    anchor = 0.0
    sum_d = 0.0
    sum_d2 = 0.0
    bad_count = 0
    run_len = 0
    last_val = _NAN

    for i in range(1, period + 1):
        r = returns[i]
        if _isfinite(r):
            sum_d += r
            sum_d2 += r * r
            if run_len > 0 and r == last_val:
                run_len += 1
            else:
                run_len = 1
                last_val = r
        else:
            bad_count += 1
            run_len = 0
            last_val = _NAN

    if bad_count == 0:
        if run_len >= period:
            anchor = last_val
            sum_d = 0.0
            sum_d2 = 0.0
            out[period] = 0.0
        else:
            anchor = sum_d * inv_period
            mean, ssd = _exact_window_stats(returns, 1, period + 1)
            anchor = mean
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
            if run_len > 0 and new_r == last_val:
                run_len += 1
            else:
                run_len = 1
                last_val = new_r
        else:
            bad_count += 1
            run_len = 0
            last_val = _NAN

        since_resync += 1
        if bad_count != 0:
            continue

        # Provably constant window: every element in [i - period + 1, i] equals last_val.
        if run_len >= period:
            anchor = last_val
            sum_d = 0.0
            sum_d2 = 0.0
            since_resync = 0
            out[i] = 0.0
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
    Uses anchor-shifted rolling sums with constant-run fast path and amortized
    exact resync for full float64 precision.
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
    run_len = 0
    last_val = _NAN
    for i in range(period):
        v = closes[i]
        if _isfinite(v):
            if run_len > 0 and v == last_val:
                run_len += 1
            else:
                run_len = 1
                last_val = v
        else:
            bad_count += 1
            run_len = 0
            last_val = _NAN

    sum_d = 0.0
    sum_d2 = 0.0
    scale_exp = 0
    if bad_count == 0:
        if run_len >= period:
            anchor = last_val
            sum_d = 0.0
            sum_d2 = 0.0
            out[period - 1] = 0.0
        else:
            s_mean, s_ssd, scale_exp = _exact_window_stats_scaled(closes, 0, period)
            anchor = s_mean
            sum_d = 0.0
            sum_d2 = s_ssd
            std = _sqrt(s_ssd * inv_dof) if s_ssd > 0.0 else 0.0
            eps = 1e-12 * max(1.0, abs(s_mean))
            if std <= eps:
                out[period - 1] = 0.0
            else:
                last_scaled = closes[period - 1] if scale_exp == 0 else _ldexp(closes[period - 1], -scale_exp)
                z = (last_scaled - s_mean) / std
                out[period - 1] = 0.0 if z == 0.0 else z
    else:
        # Defer sum initialization until the first full finite window triggers scaled resync.
        sum_d = _NAN
        sum_d2 = _NAN

    since_resync = 0
    for i in range(period, n):
        old_v = closes[i - period]
        new_v = closes[i]
        if _isfinite(old_v):
            old_s = old_v if scale_exp == 0 else _ldexp(old_v, -scale_exp)
            d_old = old_s - anchor
            sum_d -= d_old
            sum_d2 -= d_old * d_old
        else:
            bad_count -= 1

        if _isfinite(new_v):
            new_s = new_v if scale_exp == 0 else _ldexp(new_v, -scale_exp)
            d_new = new_s - anchor
            sum_d += d_new
            sum_d2 += d_new * d_new
            if run_len > 0 and new_v == last_val:
                run_len += 1
            else:
                run_len = 1
                last_val = new_v
        else:
            bad_count += 1
            run_len = 0
            last_val = _NAN

        since_resync += 1
        if bad_count != 0:
            continue

        # Provably constant window: every close in [i - period + 1, i] equals last_val.
        if run_len >= period:
            scale_exp = 0
            anchor = last_val
            sum_d = 0.0
            sum_d2 = 0.0
            since_resync = 0
            out[i] = 0.0
            continue

        ssd = sum_d2 - (sum_d * sum_d) * inv_period
        if (
            since_resync >= resync_every
            or not (_isfinite(sum_d) and _isfinite(sum_d2) and _isfinite(ssd))
            or ssd <= 1e-12 * sum_d2
            or ssd < 0.0
            or (scale_exp == 0 and abs(new_v) > 1e100)
            or (scale_exp != 0 and (abs(new_s) > 1e100 or (new_v != 0.0 and abs(new_s) < 1e-100)))
        ):
            s_mean, s_ssd, scale_exp = _exact_window_stats_scaled(closes, i - period + 1, i + 1)
            anchor = s_mean
            sum_d = 0.0
            sum_d2 = s_ssd
            ssd = s_ssd
            since_resync = 0
            mean = s_mean
        else:
            mean = anchor + (sum_d * inv_period)

        std = _sqrt(ssd * inv_dof) if ssd > 0.0 else 0.0
        eps = 1e-12 * max(1.0, abs(mean))
        if std <= eps:
            out[i] = 0.0
        else:
            last_scaled = new_v if scale_exp == 0 else _ldexp(new_v, -scale_exp)
            z = (last_scaled - mean) / std
            out[i] = 0.0 if z == 0.0 else z

    return array("d", out)


def compute_vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    period: int = 20,
) -> array:
    """Compute O(N) rolling typical-price VWAP (`math.nan` during warmup or zero volume).

    Tracks positive-volume observations separately and uses a zero-subtraction
    two-stack sliding window with compensated sums so a large outgoing volume
    (e.g. `[1e12, 1e-5, 0]`) cannot erase smaller retained positive volume.
    """
    period = _validate_positive_int(period, "period", minimum=1)
    n = len(closes)
    if len(highs) != n or len(lows) != n or len(volumes) != n:
        raise ValueError("highs, lows, closes, and volumes must have equal length")
    if n == 0:
        return array("d")
    out = [_NAN] * n
    if n < period:
        return array("d", out)

    # Entries: (pv, v, bad_flag, pos_flag, agg_pv, agg_pv_c, agg_v, agg_v_c, agg_bad, agg_pos)
    in_stack: list[tuple[float, float, int, int, float, float, float, float, int, int]] = []
    out_stack: list[tuple[float, float, int, int, float, float, float, float, int, int]] = []

    for i in range(n):
        h = highs[i]
        l = lows[i]
        c = closes[i]
        v = volumes[i]
        if _isfinite(h) and _isfinite(l) and _isfinite(c) and _isfinite(v) and v >= 0.0:
            vf = float(v)
            if vf == 0.0:
                pv = 0.0
                bad = 0
                pos = 0
            else:
                tp_sum = h + l + c
                tp = (tp_sum / 3.0) if _isfinite(tp_sum) else ((h / 3.0) + (l / 3.0) + (c / 3.0))
                pv = tp * vf
                if _isfinite(pv):
                    bad = 0
                    pos = 1
                else:
                    pv = 0.0
                    vf = 0.0
                    bad = 1
                    pos = 0
        else:
            pv = 0.0
            vf = 0.0
            bad = 1
            pos = 0

        if in_stack:
            _, _, _, _, p_pv, p_pvc, p_v, p_vc, p_bad, p_pos = in_stack[-1]
            agg_pv, agg_pvc = _neumaier_add(p_pv, p_pvc, pv)
            agg_v, agg_vc = _neumaier_add(p_v, p_vc, vf)
            agg_bad = p_bad + bad
            agg_pos = p_pos + pos
        else:
            agg_pv, agg_pvc, agg_v, agg_vc, agg_bad, agg_pos = pv, 0.0, vf, 0.0, bad, pos
        in_stack.append((pv, vf, bad, pos, agg_pv, agg_pvc, agg_v, agg_vc, agg_bad, agg_pos))

        if i >= period:
            if not out_stack:
                s_pv = 0.0
                s_pvc = 0.0
                s_v = 0.0
                s_vc = 0.0
                s_bad = 0
                s_pos = 0
                while in_stack:
                    e_pv, e_v, e_bad, e_pos, _, _, _, _, _, _ = in_stack.pop()
                    s_pv, s_pvc = _neumaier_add(s_pv, s_pvc, e_pv)
                    s_v, s_vc = _neumaier_add(s_v, s_vc, e_v)
                    s_bad += e_bad
                    s_pos += e_pos
                    out_stack.append((e_pv, e_v, e_bad, e_pos, s_pv, s_pvc, s_v, s_vc, s_bad, s_pos))
            out_stack.pop()

        if i >= period - 1:
            total_bad = (in_stack[-1][8] if in_stack else 0) + (out_stack[-1][8] if out_stack else 0)
            total_pos = (in_stack[-1][9] if in_stack else 0) + (out_stack[-1][9] if out_stack else 0)
            if total_bad == 0 and total_pos > 0:
                if in_stack and out_stack:
                    s_pv, s_pvc = _neumaier_add(out_stack[-1][4], out_stack[-1][5], in_stack[-1][4])
                    s_pv, s_pvc = _neumaier_add(s_pv, s_pvc, in_stack[-1][5])
                    sum_pv = s_pv + s_pvc
                    s_v, s_vc = _neumaier_add(out_stack[-1][6], out_stack[-1][7], in_stack[-1][6])
                    s_v, s_vc = _neumaier_add(s_v, s_vc, in_stack[-1][7])
                    sum_v = s_v + s_vc
                elif in_stack:
                    sum_pv = in_stack[-1][4] + in_stack[-1][5]
                    sum_v = in_stack[-1][6] + in_stack[-1][7]
                else:
                    sum_pv = out_stack[-1][4] + out_stack[-1][5]
                    sum_v = out_stack[-1][6] + out_stack[-1][7]
                if sum_v > 0.0:
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
    def _closes_tuple(self, base: Any) -> tuple[float, ...]:
        """Return a cached immutable `tuple[float, ...]` view of `base.closes` (zero per-bar unboxing)."""
        return self._get_or_compute(base, ("_closes_tuple",), lambda s: tuple(s.closes))  # type: ignore[return-value]

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
