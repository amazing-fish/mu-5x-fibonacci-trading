from __future__ import annotations

from mu_strategy.indicators import ema, macd, rsi
from mu_strategy.models import Candle
from mu_strategy.strategy import one_hour_regime


def build_hourly_context(candles_15m: list[Candle], candles_1h: list[Candle]) -> dict[int, str]:
    if not candles_1h:
        return {bar.open_time_ms: "yellow" for bar in candles_15m}

    closes = [bar.close for bar in candles_1h]
    ema21_values = ema(closes, 21)
    rsi_values = rsi(closes, 14)
    _, _, hist_values = macd(closes)

    hourly_states: list[tuple[int, str]] = []
    for index, candle in enumerate(candles_1h):
        previous_hist = hist_values[index - 1] if index > 0 else hist_values[index]
        state = one_hour_regime(candle.close, ema21_values[index], rsi_values[index], hist_values[index], previous_hist)
        hourly_states.append((candle.open_time_ms, state))

    context: dict[int, str] = {}
    cursor = 0
    current_state = "yellow"
    for bar in candles_15m:
        while cursor < len(hourly_states) and hourly_states[cursor][0] <= bar.open_time_ms:
            current_state = hourly_states[cursor][1]
            cursor += 1
        context[bar.open_time_ms] = current_state
    return context
