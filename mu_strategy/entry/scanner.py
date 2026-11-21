from __future__ import annotations

import math
from dataclasses import dataclass, replace

from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.core.risk import resolve_initial_stop
from mu_strategy.core.trading_calendar import evaluate_trading_window
from mu_strategy.indicators import macd, rsi
from mu_strategy.models import (
    Candle,
    EntryDecisionCode,
    EntryDecisionStage,
    EntryDisposition,
    closes,
    entry_decision_metadata,
    scanner_action_for,
)
from mu_strategy.strategy import (
    StrategyConfig,
    is_preferred_us_cash_window,
    nearest_fib_retest_level,
    should_enter_long,
    should_execute_entry,
)


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
    decision_code: EntryDecisionCode = EntryDecisionCode.UNKNOWN

    evaluated_candle_open_ms: int | None = None
    evaluated_candle_close_ms: int | None = None
    calendar_id: str | None = None
    calendar_sha256: str | None = None
    calendar_session: str | None = None
    trading_windows_et: tuple[tuple[str, str], ...] | None = None

    def __post_init__(self) -> None:
        if self.decision_code is not EntryDecisionCode.UNKNOWN:
            object.__setattr__(self, "action", scanner_action_for(self.disposition))

    @property
    def disposition(self) -> EntryDisposition:
        return entry_decision_metadata(self.decision_code).disposition

    @property
    def stage(self) -> EntryDecisionStage:
        return entry_decision_metadata(self.decision_code).stage


def scan_entry(symbol, candles_15m, candles_1h, *, config, lookback_bars=16, max_fib_distance_pct=0.01):
    if not candles_15m:
        return _scan_entry(symbol, candles_15m, candles_1h, config=config)
    candle = candles_15m[-1]
    calendar = evaluate_trading_window(candle.open_time_ms, config)
    if not calendar.allowed:
        result = EntryScanResult(
            symbol,
            "wait",
            (
                "reference market is closed"
                if calendar.reason == "reference_market_closed"
                else "current bar is outside configured trading window"
            ),
            candle.close,
            "unknown",
            None,
            None,
            None,
            decision_code=EntryDecisionCode(calendar.reason),
        )
    else:
        result = _scan_entry(
            symbol,
            candles_15m,
            candles_1h,
            config=config,
            lookback_bars=lookback_bars,
            max_fib_distance_pct=max_fib_distance_pct,
        )
    return replace(
        result,
        evaluated_candle_open_ms=candle.open_time_ms,
        evaluated_candle_close_ms=candle.open_time_ms + 900_000,
        calendar_id=calendar.calendar_id,
        calendar_sha256=calendar.calendar_sha256,
        calendar_session=calendar.session,
        trading_windows_et=config.trading_windows_et,
    )


def _scan_entry(
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
            decision_code=EntryDecisionCode.NO_CANDLES,
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
            return _result_for_recent_signal(
                symbol=symbol,
                candles=candles_15m,
                hourly_context=hourly_context,
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
        return EntryScanResult(
            symbol=symbol,
            action="wait",
            reason="filters are not fully blocked, but no recent confirmed fib retest",
            last_close=last_candle.close,
            regime_1h=last_regime,
            rsi14=last_rsi,
            macd_hist=last_hist,
            macd_hist_prev=previous_hist,
            decision_code=EntryDecisionCode.NO_RECENT_CONFIRMED_FIB_RETEST,
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
            decision_code=EntryDecisionCode.NO_RECENT_CONFIRMED_FIB_RETEST,
        )

    return _result_for_recent_signal(
        symbol=symbol,
        candles=candles_15m,
        hourly_context=hourly_context,
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
    candles: list[Candle],
    hourly_context: dict[int, str],
    last_candle: Candle,
    regime: str,
    rsi14: float,
    macd_hist: float,
    macd_hist_prev: float,
    signal: tuple[int, Candle, float, float | None],
    max_fib_distance_pct: float,
    config: StrategyConfig,
) -> EntryScanResult:
    signal_index, signal_candle, fib_level, signal_stop = signal
    distance_pct = (last_candle.close / fib_level) - 1 if fib_level else None
    close_is_near_fib = distance_pct is not None and abs(distance_pct) <= max_fib_distance_pct

    if config.entry_execution == "second_pullback":
        if distance_pct is not None:
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
                initial_stop=resolve_initial_stop(fib_level, signal_stop, config),
                signal_time_ms=signal_candle.open_time_ms,
                decision_code=EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY,
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
            decision_code=EntryDecisionCode.PRICE_AWAY_FROM_FIB,
        )

    # Apply shared execution rules (break_high, direct_next_open, optimized_v2 filters).
    signal_regime = hourly_context.get(signal_candle.open_time_ms, "yellow")
    has_next_candle = signal_index + 1 < len(candles)
    if has_next_candle:
        next_candle = candles[signal_index + 1]
        execution = should_execute_entry(candles, signal_index, next_candle, fib_level, signal_regime, config)
        if not execution.allowed or execution.entry_price is None:
            return EntryScanResult(
                symbol=symbol,
                action="wait",
                reason=execution.reason,
                last_close=last_candle.close,
                regime_1h=regime,
                rsi14=rsi14,
                macd_hist=macd_hist,
                macd_hist_prev=macd_hist_prev,
                fib_level=fib_level,
                fib_distance_pct=distance_pct,
                signal_time_ms=signal_candle.open_time_ms,
                decision_code=execution.decision_code,
            )
        if not is_preferred_us_cash_window(next_candle.open_time_ms, config):
            return EntryScanResult(
                symbol=symbol,
                action="wait",
                reason="next execution candle is outside configured trading window",
                last_close=last_candle.close,
                regime_1h=regime,
                rsi14=rsi14,
                macd_hist=macd_hist,
                macd_hist_prev=macd_hist_prev,
                fib_level=fib_level,
                fib_distance_pct=distance_pct,
                signal_time_ms=signal_candle.open_time_ms,
                decision_code=EntryDecisionCode.NEXT_FILL_OUTSIDE_TRADING_WINDOW,
            )
        trigger_price = execution.entry_price
    elif config.entry_execution == "direct_next_open":
        prospective = Candle(
            open_time_ms=last_candle.open_time_ms + 900_000,
            open=last_candle.close,
            high=last_candle.close,
            low=last_candle.close,
            close=last_candle.close,
            volume=0.0,
        )
        execution = should_execute_entry(candles, signal_index, prospective, fib_level, signal_regime, config)
        if not execution.allowed or execution.entry_price is None:
            return EntryScanResult(
                symbol=symbol,
                action="wait",
                reason=execution.reason,
                last_close=last_candle.close,
                regime_1h=regime,
                rsi14=rsi14,
                macd_hist=macd_hist,
                macd_hist_prev=macd_hist_prev,
                fib_level=fib_level,
                fib_distance_pct=distance_pct,
                signal_time_ms=signal_candle.open_time_ms,
                decision_code=execution.decision_code,
            )
        if not is_preferred_us_cash_window(prospective.open_time_ms, config):
            return EntryScanResult(
                symbol=symbol,
                action="wait",
                reason="next execution candle is outside configured trading window",
                last_close=last_candle.close,
                regime_1h=regime,
                rsi14=rsi14,
                macd_hist=macd_hist,
                macd_hist_prev=macd_hist_prev,
                fib_level=fib_level,
                fib_distance_pct=distance_pct,
                signal_time_ms=signal_candle.open_time_ms,
                decision_code=EntryDecisionCode.NEXT_FILL_OUTSIDE_TRADING_WINDOW,
            )
        trigger_price = execution.entry_price
    else:
        # break_high on the latest bar: validate signal/price filters at the planned breakout
        # trigger first, then wait for the next bar to actually break signal_candle.high.
        prospective_high = math.nextafter(signal_candle.high, math.inf)
        prospective = Candle(
            open_time_ms=last_candle.open_time_ms + 900_000,
            open=signal_candle.high,
            high=prospective_high,
            low=signal_candle.high,
            close=signal_candle.high,
            volume=0.0,
        )
        execution = should_execute_entry(candles, signal_index, prospective, fib_level, signal_regime, config)
        if not execution.allowed:
            return EntryScanResult(
                symbol=symbol,
                action="wait",
                reason=execution.reason,
                last_close=last_candle.close,
                regime_1h=regime,
                rsi14=rsi14,
                macd_hist=macd_hist,
                macd_hist_prev=macd_hist_prev,
                fib_level=fib_level,
                fib_distance_pct=distance_pct,
                signal_time_ms=signal_candle.open_time_ms,
                decision_code=execution.decision_code,
            )
        return EntryScanResult(
            symbol=symbol,
            action="wait",
            reason="next candle required to break signal high",
            last_close=last_candle.close,
            regime_1h=regime,
            rsi14=rsi14,
            macd_hist=macd_hist,
            macd_hist_prev=macd_hist_prev,
            fib_level=fib_level,
            fib_distance_pct=distance_pct,
            trigger_price=signal_candle.high,
            initial_stop=resolve_initial_stop(signal_candle.high, signal_stop, config),
            signal_time_ms=signal_candle.open_time_ms,
            decision_code=EntryDecisionCode.NEXT_CANDLE_REQUIRED,
        )

    if close_is_near_fib:
        return EntryScanResult(
            symbol=symbol,
            action="enter",
            reason="recent retest confirmed and price is near fib zone",
            last_close=last_candle.close,
            regime_1h=regime,
            rsi14=rsi14,
            macd_hist=macd_hist,
            macd_hist_prev=macd_hist_prev,
            fib_level=fib_level,
            fib_distance_pct=distance_pct,
            trigger_price=trigger_price,
            initial_stop=resolve_initial_stop(trigger_price, signal_stop, config),
            signal_time_ms=signal_candle.open_time_ms,
            decision_code=EntryDecisionCode.SIGNAL_CONFIRMED,
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
        decision_code=EntryDecisionCode.PRICE_AWAY_FROM_FIB,
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
    if regime not in config.allowed_regimes:
        reason = "1h regime is red" if regime == "red" else f"1h regime ({regime}) is not allowed"
        decision_code = EntryDecisionCode.REGIME_BLOCKED
    elif rsi14 < config.rsi_floor:
        reason = "15m RSI is below floor"
        decision_code = EntryDecisionCode.RSI_BELOW_FLOOR
    elif macd_hist < macd_hist_prev and macd_hist < 0:
        reason = "15m MACD histogram still weakening"
        decision_code = EntryDecisionCode.MACD_WEAKENING
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
        decision_code=decision_code,
    )


def _latest_recent_signal(
    candles: list[Candle],
    hourly_context: dict[int, str],
    rsi_values: list[float],
    hist_values: list[float],
    *,
    config: StrategyConfig,
    lookback_bars: int,
) -> tuple[int, Candle, float, float | None] | None:
    start = max(1, len(candles) - max(1, lookback_bars))
    indices = (
        range(start, len(candles))
        if config.entry_execution == "second_pullback"
        else range(len(candles) - 1, start - 1, -1)
    )
    for index in indices:
        candle = candles[index]
        if not is_preferred_us_cash_window(candle.open_time_ms, config):
            continue
        fib_level = nearest_fib_retest_level(candles, index, config)
        if fib_level is None:
            continue
        previous_hist = _series_value(hist_values, max(0, index - 1))
        entry_signal = should_enter_long(
            candle,
            fib_level,
            hourly_context.get(candle.open_time_ms, "yellow"),
            _series_value(rsi_values, index),
            _series_value(hist_values, index),
            previous_hist,
            config,
        )
        if not entry_signal.allowed:
            continue
        if config.entry_execution == "second_pullback":
            if _second_pullback_signal_already_filled(candles, index, fib_level, config):
                continue
        return (index, candle, fib_level, entry_signal.stop_price)
    return None


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
