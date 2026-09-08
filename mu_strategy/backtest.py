from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.indicators import macd, rsi
from mu_strategy.models import BacktestResult, Candle, Fill, Trade
from mu_strategy.strategy import (
    StrategyConfig,
    is_preferred_us_cash_window,
    nearest_fib_retest_level,
    should_enter_long,
    should_execute_entry,
)
from mu_strategy.strategies.position_rules import (
    PositionFillSnapshot,
    PositionStateSnapshot,
    PyramidAddDecision,
    decide_pyramid_add,
    tighten_stop,
)


@dataclass
class OpenPosition:
    fills: list[Fill]
    stop_price: float
    entry_anchor: float
    initial_stop_price: float = 0.0
    max_stage: int = 1
    stop_transition_fill_count: int = 0
    stop_transition_start: float = 0.0
    pending_add: PendingPyramidAdd | None = None

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


@dataclass(frozen=True)
class PendingPyramidAdd:
    """A close-confirmed candidate valid only on the adjacent execution bar."""

    candidate: PyramidAddDecision
    signal_index: int


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

    # Settled equity is also the existing sizing budget. Open-position fees
    # affect measurements only; debiting this budget would change later fills
    # and charge the same entry fees again at settlement. It is not free margin.
    equity = starting_equity
    equity_curve: list[tuple[int, float]] = [(candles_15m[0].open_time_ms, equity)]
    trades: list[Trade] = []
    position: OpenPosition | None = None
    pending_entry: PendingEntry | None = None

    def record_close(candle: Candle) -> None:
        # All timestamps label the 15m bar, not an exact intrabar instant.
        # Preserve list order: executed events, observed close, final settlement.
        equity_curve.append((candle.open_time_ms, _marked_equity(equity, position, candle.close)))

    index = 1
    while index < len(candles_15m):
        candle = candles_15m[index]

        if position is None:
            if config.entry_execution == "second_pullback" and pending_entry is not None:
                if index > pending_entry.expires_index:
                    pending_entry = None
                elif candle.low <= pending_entry.fib_level * (1 + config.fib_tolerance_pct):
                    if not is_preferred_us_cash_window(candle.open_time_ms, config):
                        record_close(candle)
                        index += 1
                        continue
                    entry_price = _buy_limit_fill_price(candle, pending_entry.fib_level)
                    if entry_price is None:
                        record_close(candle)
                        index += 1
                        continue
                    fill = _make_fill(candle.open_time_ms, entry_price, config.margin_steps[0], equity, config)
                    stop_price = entry_price * (1 - config.initial_stop_pct)
                    position = OpenPosition([fill], stop_price, entry_price, stop_price)
                    pending_entry = None
                    equity_curve.append((candle.open_time_ms, _marked_equity(equity, position, entry_price)))
                    if _has_non_session_liquidation_risk(candle, position, config):
                        exit_price = _sell_stop_fill_price(candle, _liquidation_risk_price(position, config))
                        equity, trade = _close_position(
                            position,
                            candle,
                            exit_price if exit_price is not None else _liquidation_risk_price(position, config),
                            equity,
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
                            "initial_stop",
                            config,
                        )
                        trades.append(trade)
                        position = None
                        equity_curve.append((candle.open_time_ms, equity))
                    record_close(candle)
                    index += 1
                    continue
                else:
                    record_close(candle)
                    index += 1
                    continue

            # Pending orders consume the current bar, including the final one.
            # A new close-confirmed signal needs a real later execution bar.
            if index + 1 >= len(candles_15m):
                record_close(candle)
                break
            if not is_preferred_us_cash_window(candle.open_time_ms, config):
                record_close(candle)
                index += 1
                continue
            fib_level = nearest_fib_retest_level(candles_15m, index, config)
            if fib_level is None:
                record_close(candle)
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
                record_close(candle)
                index += 1
                continue

            if config.entry_execution == "second_pullback":
                pending_entry = PendingEntry(
                    fib_level=fib_level,
                    expires_index=index + config.second_pullback_wait_bars,
                )
                record_close(candle)
                index += 1
                continue

            next_candle = candles_15m[index + 1]
            execution = should_execute_entry(candles_15m, index, next_candle, fib_level, regime, config)
            if not execution.allowed or execution.entry_price is None:
                record_close(candle)
                index += 1
                continue
            if not is_preferred_us_cash_window(next_candle.open_time_ms, config):
                record_close(candle)
                index += 1
                continue

            # This branch creates a future fill early in control flow. Sample
            # the signal close before that fill (or its exit) changes state.
            record_close(candle)
            entry_price = execution.entry_price
            fill = _make_fill(next_candle.open_time_ms, entry_price, config.margin_steps[0], equity, config)
            stop_price = entry_price * (1 - config.initial_stop_pct)
            position = OpenPosition([fill], stop_price, entry_price, stop_price)
            equity_curve.append((next_candle.open_time_ms, _marked_equity(equity, position, entry_price)))
            if _has_non_session_liquidation_risk(next_candle, position, config):
                exit_price = _sell_stop_fill_price(next_candle, _liquidation_risk_price(position, config))
                equity, trade = _close_position(
                    position,
                    next_candle,
                    exit_price if exit_price is not None else _liquidation_risk_price(position, config),
                    equity,
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
                    "initial_stop",
                    config,
                )
                trades.append(trade)
                position = None
                equity_curve.append((next_candle.open_time_ms, equity))
            index += 1
            continue

        # Next-bar execution above has already checked this fill bar's initial
        # risk. Its surviving position still receives normal add/stop updates.
        entry_bar_risk_checked = position.fills[0].time_ms == candle.open_time_ms
        if not entry_bar_risk_checked and _has_non_session_liquidation_risk(candle, position, config):
            exit_price = _sell_stop_fill_price(candle, _liquidation_risk_price(position, config))
            equity, trade = _close_position(
                position,
                candle,
                exit_price if exit_price is not None else _liquidation_risk_price(position, config),
                equity,
                "non_session_liquidation_risk",
                config,
            )
            trades.append(trade)
            position = None
            equity_curve.append((candle.open_time_ms, equity))
            record_close(candle)
            index += 1
            continue

        if not entry_bar_risk_checked and candle.low <= position.stop_price:
            exit_price = _sell_stop_fill_price(candle, position.stop_price)
            equity, trade = _close_position(
                position,
                candle,
                exit_price if exit_price is not None else position.stop_price,
                equity,
                "stop",
                config,
            )
            trades.append(trade)
            position = None
            equity_curve.append((candle.open_time_ms, equity))
            record_close(candle)
            index += 1
            continue

        fill_count = len(position.fills)
        _execute_pyramid_add(position, candle, index, candles_15m, hourly_context, hist_values, rsi_values, equity, config)
        if len(position.fills) > fill_count:
            equity_curve.append((candle.open_time_ms, _marked_equity(equity, position, position.fills[-1].price)))
        _tighten_stop(position, candle, index, candles_15m, hourly_context.get(candle.open_time_ms, "yellow"), config)
        _plan_pyramid_add(position, candle, index, hourly_context, hist_values, rsi_values, config)
        record_close(candle)
        index += 1

    if position is not None:
        final = candles_15m[-1]
        equity, trade = _close_position(
            position,
            final,
            final.close,
            equity,
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


def _return_on_margin(net_pnl: float, fills: list[Fill], leverage: float) -> float:
    if not fills or leverage == 0:
        return 0.0
    committed_margin = sum(fill.notional for fill in fills) / leverage
    if committed_margin == 0:
        return 0.0
    return net_pnl / committed_margin


def _buy_limit_fill_price(candle: Candle, limit_price: float) -> float | None:
    if candle.low > limit_price:
        return None
    if candle.open < limit_price:
        return candle.open
    return limit_price


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


def _execute_pyramid_add(
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
    # Consume once, including misses/invalidations. Closing the owning position
    # discards its plan too. Never use this execution bar's close indicators.
    plan = position.pending_add
    position.pending_add = None
    if plan is None:
        return
    candidate = plan.candidate
    if (index != plan.signal_index + 1
            or candle.open_time_ms != candidate.available_at_ms
            or position.max_stage + 1 != candidate.stage):
        return
    if not is_preferred_us_cash_window(candle.open_time_ms, config):
        return
    regime = hourly_context.get(candle.open_time_ms, "yellow")
    decision = decide_pyramid_add(
        _position_snapshot(position),
        candles[plan.signal_index],
        rsi_value=rsi_values[plan.signal_index],
        macd_hist=hist_values[plan.signal_index],
        previous_macd_hist=hist_values[plan.signal_index - 1],
        regime=regime,
        config=config,
    )
    if not decision.should_add:
        return

    fill_price = _buy_stop_fill_price(candle, candidate.trigger_price)
    if fill_price is None:
        return

    fill = _make_fill(
        candle.open_time_ms,
        fill_price,
        candidate.margin_fraction,
        equity,
        config,
    )
    position.fills.append(fill)
    position.max_stage = candidate.stage


def _plan_pyramid_add(
    position: OpenPosition,
    candle: Candle,
    index: int,
    hourly_context: dict[int, str],
    hist_values: list[float],
    rsi_values: list[float],
    config: StrategyConfig,
) -> None:
    candidate = decide_pyramid_add(
        _position_snapshot(position), candle,
        rsi_value=rsi_values[index], macd_hist=hist_values[index],
        previous_macd_hist=hist_values[index - 1],
        regime=hourly_context.get(candle.open_time_ms, "yellow"), config=config,
    )
    position.pending_add = PendingPyramidAdd(candidate, index) if candidate.should_add else None


def _buy_stop_fill_price(candle: Candle, trigger_price: float) -> float | None:
    if candle.high < trigger_price:
        return None
    return max(candle.open, trigger_price)


def _tighten_stop(
    position: OpenPosition,
    candle: Candle,
    index: int,
    candles: list[Candle],
    regime: str,
    config: StrategyConfig,
) -> None:
    outcome = tighten_stop(
        _position_snapshot(position),
        candle,
        index=index,
        candles=candles,
        regime=regime,
        config=config,
    )
    position.stop_price = outcome.stop_price
    position.stop_transition_fill_count = outcome.transition_fill_count
    position.stop_transition_start = outcome.transition_start


def _position_snapshot(position: OpenPosition) -> PositionStateSnapshot:
    return PositionStateSnapshot(
        fills=tuple(
            PositionFillSnapshot(
                time_ms=fill.time_ms,
                price=fill.price,
                units=fill.units,
            )
            for fill in position.fills
        ),
        stop_price=position.stop_price,
        entry_anchor=position.entry_anchor,
        initial_stop_price=position.initial_stop_price,
        max_stage=position.max_stage,
        stop_transition_fill_count=position.stop_transition_fill_count,
        stop_transition_start=position.stop_transition_start,
    )


def _marked_equity(equity: float, position: OpenPosition | None, mark_price: float) -> float:
    """Net equity: settled budget + unrealized PnL - incurred entry fees.

    No margin deduction or reserve for a hypothetical future exit fee. Closed
    trades already contributed their net PnL to equity via _close_position.
    """
    if position is None:
        return equity
    unrealized = sum((mark_price - fill.price) * fill.units for fill in position.fills)
    return equity + unrealized - position.fees


def _close_position(
    position: OpenPosition,
    candle: Candle,
    exit_price: float,
    equity: float,
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
        return_pct=_return_on_margin(net_pnl, position.fills, config.leverage),
        max_stage=position.max_stage,
        exit_reason=reason,
    )
    return ending_equity, trade
