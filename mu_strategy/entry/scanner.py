from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.cli import build_hourly_context
from mu_strategy.execution.plan import initial_stop_price
from mu_strategy.indicators import macd, rsi
from mu_strategy.models import Candle, closes
from mu_strategy.strategy import StrategyConfig, is_preferred_us_cash_window, nearest_fib_retest_level, should_enter_long


@dataclass(frozen=True)
class EntryScanResult:
    symbol: str
    action: str
    reason: str
    last_close: float | None
    regime_1h: str
    rsi14: float | None
    macd_hist: float | None
    macd_hist_prev: float | None
    fib_level: float | None = None
    fib_distance_pct: float | None = None
    trigger_price: float | None = None
    initial_stop: float | None = None
    signal_time_ms: int | None = None


def scan_entry(
    symbol: str,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    *,
    config: StrategyConfig,
    lookback_bars: int = 16,
    max_fib_distance_pct: float = 0.01,
) -> EntryScanResult:
    if not candles_15m:
        return EntryScanResult(
            symbol=symbol,
            action="wait",
            reason="no 15m candles",
            last_close=None,
            regime_1h="yellow",
            rsi14=None,
            macd_hist=None,
            macd_hist_prev=None,
        )

    close_values = closes(candles_15m)
    rsi_values = rsi(close_values, 14)
    _, _, hist_values = macd(close_values)
    hourly_context = build_hourly_context(candles_15m, candles_1h)

    last_index = len(candles_15m) - 1
    last_candle = candles_15m[last_index]
    last_regime = hourly_context.get(last_candle.open_time_ms, "yellow")
    last_rsi = _series_value(rsi_values, last_index)
    last_hist = _series_value(hist_values, last_index)
    previous_hist = _series_value(hist_values, max(0, last_index - 1))

    if config.entry_execution == "second_pullback":
        pending_signal = _latest_recent_signal(
            candles_15m,
            hourly_context,
            rsi_values,
            hist_values,
            config=config,
            lookback_bars=_effective_lookback_bars(config, lookback_bars),
        )
        if pending_signal is not None:
            if not is_preferred_us_cash_window(last_candle.open_time_ms, config):
                return EntryScanResult(
                    symbol=symbol,
                    action="wait",
                    reason="current bar is outside configured trading window",
                    last_close=last_candle.close,
                    regime_1h=last_regime,
                    rsi14=last_rsi,
                    macd_hist=last_hist,
                    macd_hist_prev=previous_hist,
                )
            return _result_for_recent_signal(
                symbol=symbol,
                last_candle=last_candle,
                regime=last_regime,
                rsi14=last_rsi,
                macd_hist=last_hist,
                macd_hist_prev=previous_hist,
                signal=pending_signal,
                max_fib_distance_pct=max_fib_distance_pct,
                config=config,
            )

    blocked = _blocked_result(
        symbol=symbol,
        last_candle=last_candle,
        regime=last_regime,
        rsi14=last_rsi,
        macd_hist=last_hist,
        macd_hist_prev=previous_hist,
        config=config,
    )
    if blocked is not None:
        return blocked

    if not is_preferred_us_cash_window(last_candle.open_time_ms, config):
        return EntryScanResult(
            symbol=symbol,
            action="wait",
            reason="current bar is outside configured trading window",
            last_close=last_candle.close,
            regime_1h=last_regime,
            rsi14=last_rsi,
            macd_hist=last_hist,
            macd_hist_prev=previous_hist,
        )

    signal = _latest_recent_signal(
        candles_15m,
        hourly_context,
        rsi_values,
        hist_values,
        config=config,
        lookback_bars=_effective_lookback_bars(config, lookback_bars),
    )
    if signal is None:
        return EntryScanResult(
            symbol=symbol,
            action="wait",
            reason="filters are not fully blocked, but no recent confirmed fib retest",
            last_close=last_candle.close,
            regime_1h=last_regime,
            rsi14=last_rsi,
            macd_hist=last_hist,
            macd_hist_prev=previous_hist,
        )

    return _result_for_recent_signal(
        symbol=symbol,
        last_candle=last_candle,
        regime=last_regime,
        rsi14=last_rsi,
        macd_hist=last_hist,
        macd_hist_prev=previous_hist,
        signal=signal,
        max_fib_distance_pct=max_fib_distance_pct,
        config=config,
    )


def _result_for_recent_signal(
    *,
    symbol: str,
    last_candle: Candle,
    regime: str,
    rsi14: float,
    macd_hist: float,
    macd_hist_prev: float,
    signal: tuple[Candle, float],
    max_fib_distance_pct: float,
    config: StrategyConfig,
) -> EntryScanResult:
    signal_candle, fib_level = signal
    distance_pct = (last_candle.close / fib_level) - 1 if fib_level else None
    close_is_near_fib = distance_pct is not None and abs(distance_pct) <= max_fib_distance_pct
    if distance_pct is not None and (close_is_near_fib or config.entry_execution == "second_pullback"):
        return EntryScanResult(
            symbol=symbol,
            action="enter",
            reason=(
                "recent retest confirmed and price is near fib zone"
                if close_is_near_fib
                else "recent retest confirmed; resting second-pullback fib limit"
            ),
            last_close=last_candle.close,
            regime_1h=regime,
            rsi14=rsi14,
            macd_hist=macd_hist,
            macd_hist_prev=macd_hist_prev,
            fib_level=fib_level,
            fib_distance_pct=distance_pct,
            trigger_price=fib_level,
            initial_stop=initial_stop_price(fib_level, config),
            signal_time_ms=signal_candle.open_time_ms,
        )

    return EntryScanResult(
        symbol=symbol,
        action="wait",
        reason="recent retest confirmed but price has moved away from fib zone",
        last_close=last_candle.close,
        regime_1h=regime,
        rsi14=rsi14,
        macd_hist=macd_hist,
        macd_hist_prev=macd_hist_prev,
        fib_level=fib_level,
        fib_distance_pct=distance_pct,
        signal_time_ms=signal_candle.open_time_ms,
    )


def _blocked_result(
    *,
    symbol: str,
    last_candle: Candle,
    regime: str,
    rsi14: float,
    macd_hist: float,
    macd_hist_prev: float,
    config: StrategyConfig,
) -> EntryScanResult | None:
    if regime == "red":
        reason = "1h regime is red"
    elif rsi14 < config.rsi_floor:
        reason = "15m RSI is below floor"
    elif macd_hist < macd_hist_prev and macd_hist < 0:
        reason = "15m MACD histogram still weakening"
    else:
        return None

    return EntryScanResult(
        symbol=symbol,
        action="skip",
        reason=reason,
        last_close=last_candle.close,
        regime_1h=regime,
        rsi14=rsi14,
        macd_hist=macd_hist,
        macd_hist_prev=macd_hist_prev,
    )


def _latest_recent_signal(
    candles: list[Candle],
    hourly_context: dict[int, str],
    rsi_values: list[float],
    hist_values: list[float],
    *,
    config: StrategyConfig,
    lookback_bars: int,
) -> tuple[Candle, float] | None:
    start = max(1, len(candles) - max(1, lookback_bars))
    latest: tuple[Candle, float] | None = None
    for index in range(start, len(candles)):
        if not is_preferred_us_cash_window(candles[index].open_time_ms, config):
            continue
        fib_level = nearest_fib_retest_level(candles, index, config)
        if fib_level is None:
            continue
        previous_hist = _series_value(hist_values, max(0, index - 1))
        entry_signal = should_enter_long(
            candles[index],
            fib_level,
            hourly_context.get(candles[index].open_time_ms, "yellow"),
            _series_value(rsi_values, index),
            _series_value(hist_values, index),
            previous_hist,
            config,
        )
        if entry_signal.allowed:
            if config.entry_execution == "second_pullback":
                if _second_pullback_signal_already_filled(candles, index, fib_level, config):
                    continue
                return (candles[index], fib_level)
            latest = (candles[index], fib_level)
    return latest


def _second_pullback_signal_already_filled(
    candles: list[Candle],
    signal_index: int,
    fib_level: float,
    config: StrategyConfig,
) -> bool:
    expires_index = min(len(candles) - 1, signal_index + config.second_pullback_wait_bars)
    for index in range(signal_index + 1, expires_index + 1):
        candle = candles[index]
        if not is_preferred_us_cash_window(candle.open_time_ms, config):
            continue
        if candle.low <= fib_level:
            return True
    return False


def _effective_lookback_bars(config: StrategyConfig, requested_lookback_bars: int) -> int:
    if config.entry_execution != "second_pullback":
        return requested_lookback_bars
    return min(requested_lookback_bars, config.second_pullback_wait_bars)


def _series_value(values: list[float], index: int) -> float:
    if not values:
        return 0.0
    return values[min(index, len(values) - 1)]
