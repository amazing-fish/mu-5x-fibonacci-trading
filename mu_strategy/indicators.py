from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []

    alpha = 2 / (period + 1)
    output: list[float] = []
    previous = float(values[0])
    for value in values:
        previous = (float(value) * alpha) + (previous * (1 - alpha))
        output.append(previous)
    return output


def rsi(values: list[float], period: int = 14) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    if len(values) == 1:
        return [50.0]

    output = [50.0] * len(values)
    gains = 0.0
    losses = 0.0
    for index in range(1, min(period + 1, len(values))):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change

    if len(values) <= period:
        return output

    avg_gain = gains / period
    avg_loss = losses / period
    output[period] = _rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        output[index] = _rsi_from_averages(avg_gain, avg_loss)

    return output


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


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
