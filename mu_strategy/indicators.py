from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []

    alpha = 2.0 / (period + 1)
    one_minus_alpha = 1.0 - alpha
    output: list[float] = []
    append = output.append
    previous = float(values[0])
    for value in values:
        previous = (value * alpha) + (previous * one_minus_alpha)
        append(previous)
    return output


def rsi(values: list[float], period: int = 14) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [50.0]

    output = [50.0] * n
    gains = 0.0
    losses = 0.0
    limit = period + 1 if period + 1 < n else n
    for index in range(1, limit):
        change = values[index] - values[index - 1]
        if change >= 0.0:
            gains += change
        else:
            losses -= change

    if n <= period:
        return output

    inv_period = 1.0 / period
    prev_weight = period - 1
    avg_gain = gains * inv_period
    avg_loss = losses * inv_period
    output[period] = _rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, n):
        change = values[index] - values[index - 1]
        if change > 0.0:
            avg_gain = ((avg_gain * prev_weight) + change) * inv_period
            avg_loss = (avg_loss * prev_weight) * inv_period
        elif change < 0.0:
            avg_gain = (avg_gain * prev_weight) * inv_period
            avg_loss = ((avg_loss * prev_weight) - change) * inv_period
        else:
            avg_gain = (avg_gain * prev_weight) * inv_period
            avg_loss = (avg_loss * prev_weight) * inv_period
        output[index] = _rsi_from_averages(avg_gain, avg_loss)

    return output


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def macd(
    values: list[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[list[float], list[float], list[float]]:
    if slow_period <= fast_period:
        raise ValueError("slow_period must be greater than fast_period")
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)
    line = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    signal = ema(line, signal_period)
    histogram = [line_value - signal_value for line_value, signal_value in zip(line, signal)]
    return line, signal, histogram
