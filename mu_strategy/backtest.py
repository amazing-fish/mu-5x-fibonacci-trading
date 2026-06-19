from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.indicators import macd, rsi
from mu_strategy.models import BacktestResult, Candle, Fill, Trade
from mu_strategy.strategy import (
    StrategyConfig,
    is_preferred_us_cash_window,
    nearest_fib_retest_level,
    recent_higher_low,
    should_enter_long,
    should_execute_entry,
)


@dataclass
class OpenPosition:
    fills: list[Fill]
    stop_price: float
    entry_anchor: float
    initial_stop_price: float = 0.0
    max_stage: int = 1

    @property
    def units(self) -> float:
        return sum(fill.units for fill in self.fills)

    @property
    def entry_price(self) -> float:
        if not self.units:
            return 0.0
        return sum(fill.price * fill.units for fill in self.fills) / self.units

    @property
    def fees(self) -> float:
        return sum(fill.fee for fill in self.fills)


@dataclass
class PendingEntry:
    fib_level: float
    expires_index: int


def run_backtest(
    candles_15m: list[Candle],
    hourly_context: dict[int, str],
    *,
    config: StrategyConfig | None = None,
    starting_equity: float = 10_000.0,
) -> BacktestResult:
    config = config or StrategyConfig()
    if len(candles_15m) < 4:
        return BacktestResult(starting_equity, starting_equity, [], [])

    closes = [bar.close for bar in candles_15m]
    rsi_values = rsi(closes, 14)
    _, _, hist_values = macd(closes)

    equity = starting_equity
    equity_curve: list[tuple[int, float]] = [(candles_15m[0].open_time_ms, equity)]
    trades: list[Trade] = []
    position: OpenPosition | None = None
    pending_entry: PendingEntry | None = None

    index = 1
    while index < len(candles_15m) - 1:
        candle = candles_15m[index]
        next_candle = candles_15m[index + 1]

        if position is None:
            if config.entry_execution == "second_pullback" and pending_entry is not None:
                if index > pending_entry.expires_index:
                    pending_entry = None
                elif candle.low <= pending_entry.fib_level * (1 + config.fib_tolerance_pct):
                    if not is_preferred_us_cash_window(candle.open_time_ms, config):
                        index += 1
                        continue
                    entry_price = _buy_limit_fill_price(candle, pending_entry.fib_level)
                    if entry_price is None:
                        index += 1
                        continue
                    fill = _make_fill(candle.open_time_ms, entry_price, config.margin_steps[0], equity, config)
                    stop_price = entry_price * (1 - config.initial_stop_pct)
                    position = OpenPosition([fill], stop_price, entry_price, stop_price)
                    pending_entry = None
                    if _has_non_session_liquidation_risk(candle, position, config):
                        exit_price = _sell_stop_fill_price(candle, _liquidation_risk_price(position, config))
                        equity, trade = _close_position(
                            position,
                            candle,
                            exit_price if exit_price is not None else _liquidation_risk_price(position, config),
                            equity,
                            starting_equity,
                            "non_session_liquidation_risk",
                            config,
                        )
                        trades.append(trade)
                        position = None
                        equity_curve.append((candle.open_time_ms, equity))
                    elif candle.low <= position.stop_price:
                        exit_price = _sell_stop_fill_price(candle, position.stop_price)
                        equity, trade = _close_position(
                            position,
                            candle,
                            exit_price if exit_price is not None else position.stop_price,
                            equity,
                            starting_equity,
                            "initial_stop",
                            config,
                        )
                        trades.append(trade)
                        position = None
                        equity_curve.append((candle.open_time_ms, equity))
                    index += 1
                    continue
                else:
                    index += 1
                    continue

            if not is_preferred_us_cash_window(candle.open_time_ms, config):
                index += 1
                continue
            fib_level = nearest_fib_retest_level(candles_15m, index, config)
            if fib_level is None:
                index += 1
                continue

            regime = hourly_context.get(candle.open_time_ms, "yellow")
            signal = should_enter_long(
                candle,
                fib_level,
                regime,
                rsi_values[index],
                hist_values[index],
                hist_values[index - 1],
                config,
            )
            if not signal.allowed or signal.stop_price is None:
                index += 1
                continue

            if config.entry_execution == "second_pullback":
                pending_entry = PendingEntry(
                    fib_level=fib_level,
                    expires_index=min(len(candles_15m) - 2, index + config.second_pullback_wait_bars),
                )
                index += 1
                continue

            execution = should_execute_entry(candles_15m, index, next_candle, fib_level, regime, config)
            if not execution.allowed or execution.entry_price is None:
                index += 1
                continue
            if not is_preferred_us_cash_window(next_candle.open_time_ms, config):
                index += 1
                continue

            entry_price = execution.entry_price
            fill = _make_fill(next_candle.open_time_ms, entry_price, config.margin_steps[0], equity, config)
            stop_price = entry_price * (1 - config.initial_stop_pct)
            position = OpenPosition([fill], stop_price, entry_price, stop_price)
            if _has_non_session_liquidation_risk(next_candle, position, config):
                exit_price = _sell_stop_fill_price(next_candle, _liquidation_risk_price(position, config))
                equity, trade = _close_position(
                    position,
                    next_candle,
                    exit_price if exit_price is not None else _liquidation_risk_price(position, config),
                    equity,
                    starting_equity,
                    "non_session_liquidation_risk",
                    config,
                )
                trades.append(trade)
                position = None
                equity_curve.append((next_candle.open_time_ms, equity))
            elif next_candle.low <= position.stop_price:
                exit_price = _sell_stop_fill_price(next_candle, position.stop_price)
                equity, trade = _close_position(
                    position,
                    next_candle,
                    exit_price if exit_price is not None else position.stop_price,
                    equity,
                    starting_equity,
                    "initial_stop",
                    config,
                )
                trades.append(trade)
                position = None
                equity_curve.append((next_candle.open_time_ms, equity))
            index += 1
            continue

        if _has_non_session_liquidation_risk(candle, position, config):
            exit_price = _sell_stop_fill_price(candle, _liquidation_risk_price(position, config))
            equity, trade = _close_position(
                position,
                candle,
                exit_price if exit_price is not None else _liquidation_risk_price(position, config),
                equity,
                starting_equity,
                "non_session_liquidation_risk",
                config,
            )
            trades.append(trade)
            position = None
            equity_curve.append((candle.open_time_ms, equity))
            index += 1
            continue

        if candle.low <= position.stop_price:
            exit_price = _sell_stop_fill_price(candle, position.stop_price)
            equity, trade = _close_position(
                position,
                candle,
                exit_price if exit_price is not None else position.stop_price,
                equity,
                starting_equity,
                "stop",
                config,
            )
            trades.append(trade)
            position = None
            equity_curve.append((candle.open_time_ms, equity))
            index += 1
            continue

        _maybe_add(position, candle, index, candles_15m, hourly_context, hist_values, rsi_values, equity, config)
        _tighten_stop(position, candle, index, candles_15m, hourly_context.get(candle.open_time_ms, "yellow"), config)
        equity_curve.append((candle.open_time_ms, _marked_equity(equity, position, candle.close)))
        index += 1

    if position is not None:
        final = candles_15m[-1]
        equity, trade = _close_position(
            position,
            final,
            final.close,
            equity,
            starting_equity,
            "end_of_data",
            config,
        )
        trades.append(trade)
        equity_curve.append((final.open_time_ms, equity))

    return BacktestResult(starting_equity, equity, trades, equity_curve)


def _make_fill(time_ms: int, price: float, margin_fraction: float, equity: float, config: StrategyConfig) -> Fill:
    notional = equity * margin_fraction * config.leverage
    units = notional / price
    fee = notional * config.fee_rate
    return Fill(time_ms, price, margin_fraction, notional, units, fee)


def _buy_limit_fill_price(candle: Candle, limit_price: float) -> float | None:
    if candle.low > limit_price:
        return None
    if candle.open < limit_price:
        return candle.open
    return limit_price


def _buy_stop_fill_price(candle: Candle, trigger_price: float) -> float | None:
    if candle.high < trigger_price:
        return None
    if candle.open > trigger_price:
        return candle.open
    return trigger_price


def _sell_stop_fill_price(candle: Candle, stop_price: float) -> float | None:
    if candle.low > stop_price:
        return None
    if candle.open < stop_price:
        return candle.open
    return stop_price


def _has_non_session_liquidation_risk(candle: Candle, position: OpenPosition, config: StrategyConfig) -> bool:
    if is_preferred_us_cash_window(candle.open_time_ms, config):
        return False
    return candle.low <= _liquidation_risk_price(position, config)


def _liquidation_risk_price(position: OpenPosition, config: StrategyConfig) -> float:
    if config.leverage <= 0:
        raise ValueError("leverage must be positive")
    return position.entry_price * max(0.0, 1 - (1 / config.leverage))


def _maybe_add(
    position: OpenPosition,
    candle: Candle,
    index: int,
    candles: list[Candle],
    hourly_context: dict[int, str],
    hist_values: list[float],
    rsi_values: list[float],
    equity: float,
    config: StrategyConfig,
) -> None:
    next_stage = position.max_stage + 1
    if next_stage > len(config.margin_steps):
        return
    if not is_preferred_us_cash_window(candle.open_time_ms, config):
        return
    threshold = config.add_thresholds[next_stage - 2]
    trigger_price = position.entry_anchor * (1 + threshold)
    fill_price = _buy_stop_fill_price(candle, trigger_price)
    if fill_price is None:
        return
    if rsi_values[index] < config.rsi_add_floor or hist_values[index] < hist_values[index - 1]:
        return
    regime = hourly_context.get(candle.open_time_ms, "yellow")
    if next_stage >= 3 and regime != config.full_size_regime:
        return

    fill = _make_fill(candle.open_time_ms, fill_price, config.margin_steps[next_stage - 1], equity, config)
    position.fills.append(fill)
    position.max_stage = next_stage


def _tighten_stop(
    position: OpenPosition,
    candle: Candle,
    index: int,
    candles: list[Candle],
    regime: str,
    config: StrategyConfig,
) -> None:
    initial_stop = position.initial_stop_price or position.stop_price
    first_entry = position.fills[0].price
    mode = _resolved_stop_tightening(regime, config)

    if mode == "baseline":
        _tighten_stop_baseline(position, index, candles, config)
        return

    if mode == "half_protect":
        _tighten_stop_half_protect(position, index, candles, config, initial_stop, first_entry)
        return

    if mode == "wide":
        _tighten_stop_green_wide(position, index, candles, config, initial_stop, first_entry)
        return

    if mode == "delayed_baseline":
        _tighten_stop_delayed_baseline(position, index, candles, config)
        return

    raise ValueError(f"unsupported stop_tightening: {mode}")


def _resolved_stop_tightening(regime: str, config: StrategyConfig) -> str:
    if regime == "yellow" and config.yellow_stop_tightening is not None:
        return config.yellow_stop_tightening
    if regime == "green" and config.green_stop_tightening is not None:
        return config.green_stop_tightening

    if config.stop_tightening == "green_wide":
        return "wide" if regime == "green" else "baseline"
    if config.stop_tightening == "half_protect_green_wide":
        return "wide" if regime == "green" else "half_protect"
    return config.stop_tightening


def _tighten_stop_baseline(
    position: OpenPosition,
    index: int,
    candles: list[Candle],
    config: StrategyConfig,
) -> None:
    if position.max_stage >= 2:
        position.stop_price = max(position.stop_price, position.fills[0].price)
    if position.max_stage >= 3:
        position.stop_price = max(position.stop_price, position.entry_price)
    if position.max_stage >= 4:
        higher_low = recent_higher_low(candles, index)
        if higher_low:
            position.stop_price = max(position.stop_price, higher_low * (1 - config.stop_buffer_pct))


def _tighten_stop_half_protect(
    position: OpenPosition,
    index: int,
    candles: list[Candle],
    config: StrategyConfig,
    initial_stop: float,
    first_entry: float,
) -> None:
    if position.max_stage >= 2:
        position.stop_price = max(position.stop_price, (initial_stop + first_entry) / 2)
    if position.max_stage >= 3:
        position.stop_price = max(position.stop_price, first_entry)
    if position.max_stage >= 4:
        higher_low = recent_higher_low(candles, index)
        if higher_low:
            position.stop_price = max(position.stop_price, position.entry_price, higher_low * (1 - config.stop_buffer_pct))


def _tighten_stop_green_wide(
    position: OpenPosition,
    index: int,
    candles: list[Candle],
    config: StrategyConfig,
    initial_stop: float,
    first_entry: float,
) -> None:
    if position.max_stage >= 2:
        position.stop_price = max(position.stop_price, (initial_stop + first_entry) / 2)
    if position.max_stage >= 3:
        position.stop_price = max(position.stop_price, first_entry)
    if position.max_stage >= 4:
        higher_low = recent_higher_low(candles, index)
        if higher_low:
            position.stop_price = max(position.stop_price, first_entry, higher_low * (1 - config.green_wide_stop_buffer_pct))


def _tighten_stop_delayed_baseline(
    position: OpenPosition,
    index: int,
    candles: list[Candle],
    config: StrategyConfig,
) -> None:
    progress = _apply_stop_transition_curve(
        _stop_transition_progress(position, index, candles, config),
        config.stop_transition_curve,
    )
    start = _previous_baseline_stop_target(position)
    target = _baseline_stop_target(position, index, candles, config)
    transitioned = _interpolate(start, target, progress)
    position.stop_price = max(position.stop_price, transitioned)


def _stop_transition_progress(
    position: OpenPosition,
    index: int,
    candles: list[Candle],
    config: StrategyConfig,
) -> float:
    if config.stop_transition_bars <= 0:
        return 1.0
    fill_index = _fill_index(candles, position.fills[-1].time_ms)
    elapsed = max(0, index - fill_index)
    return min(1.0, elapsed / config.stop_transition_bars)


def _apply_stop_transition_curve(progress: float, curve: str) -> float:
    if curve == "linear":
        return progress
    if curve == "slow_start":
        return progress * progress
    if curve == "fast_start":
        return 1 - ((1 - progress) * (1 - progress))
    if curve == "smooth":
        return (3 * progress * progress) - (2 * progress * progress * progress)
    raise ValueError(f"unsupported stop_transition_curve: {curve}")


def _fill_index(candles: list[Candle], fill_time_ms: int) -> int:
    for index, candle in enumerate(candles):
        if candle.open_time_ms == fill_time_ms:
            return index
    return 0


def _baseline_stop_target(
    position: OpenPosition,
    index: int,
    candles: list[Candle],
    config: StrategyConfig,
) -> float:
    target = position.initial_stop_price or position.stop_price
    if position.max_stage >= 2:
        target = max(target, position.fills[0].price)
    if position.max_stage >= 3:
        target = max(target, position.entry_price)
    if position.max_stage >= 4:
        higher_low = recent_higher_low(candles, index)
        if higher_low:
            target = max(target, higher_low * (1 - config.stop_buffer_pct))
    return target


def _previous_baseline_stop_target(position: OpenPosition) -> float:
    initial_stop = position.initial_stop_price or position.stop_price
    if position.max_stage <= 2:
        return initial_stop
    if position.max_stage == 3:
        return position.fills[0].price
    return _fills_entry_price(position.fills[:-1])


def _fills_entry_price(fills: list[Fill]) -> float:
    units = sum(fill.units for fill in fills)
    if not units:
        return 0.0
    return sum(fill.price * fill.units for fill in fills) / units


def _interpolate(start: float, target: float, progress: float) -> float:
    return start + ((target - start) * progress)


def _marked_equity(equity: float, position: OpenPosition | None, mark_price: float) -> float:
    if position is None:
        return equity
    unrealized = sum((mark_price - fill.price) * fill.units for fill in position.fills)
    return equity + unrealized


def _close_position(
    position: OpenPosition,
    candle: Candle,
    exit_price: float,
    equity: float,
    starting_equity: float,
    reason: str,
    config: StrategyConfig,
) -> tuple[float, Trade]:
    gross_pnl = sum((exit_price - fill.price) * fill.units for fill in position.fills)
    exit_notional = exit_price * position.units
    exit_fee = exit_notional * config.fee_rate
    fees = position.fees + exit_fee
    net_pnl = gross_pnl - position.fees - exit_fee
    ending_equity = equity + net_pnl
    trade = Trade(
        entry_time_ms=position.fills[0].time_ms,
        exit_time_ms=candle.open_time_ms,
        entry_price=position.entry_price,
        exit_price=exit_price,
        fills=position.fills.copy(),
        pnl=net_pnl,
        fees=fees,
        return_pct=net_pnl / starting_equity,
        max_stage=position.max_stage,
        exit_reason=reason,
    )
    return ending_equity, trade
