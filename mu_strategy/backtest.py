from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from mu_strategy.core.risk import (
    equity_floor_exit_price,
    evaluate_risk_exit,
    has_non_session_liquidation_risk,
    liquidation_risk_price,
    resolve_initial_stop,
    sell_stop_fill_price,
)
from mu_strategy.core.trading_calendar import eastern_date
from mu_strategy.indicators import macd, rsi
from mu_strategy.models import BacktestResult, Candle, Fill, Trade
from mu_strategy.strategies.position_rules import (
    PositionFillSnapshot,
    PositionStateSnapshot,
    PyramidAddDecision,
    decide_pyramid_add,
    tighten_stop,
)
from mu_strategy.strategy import (
    StrategyConfig,
    is_preferred_us_cash_window,
    nearest_fib_retest_level,
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
    stop_transition_fill_count: int = 0
    stop_transition_start: float = 0.0
    pending_add: PendingPyramidAdd | None = None
    _cache_len: int = field(default=-1, init=False, repr=False, compare=False)
    _cached_units: float = field(default=0.0, init=False, repr=False, compare=False)
    _cached_cost_basis: float = field(default=0.0, init=False, repr=False, compare=False)
    _cached_fees: float = field(default=0.0, init=False, repr=False, compare=False)
    _cached_fill_snapshots: tuple[PositionFillSnapshot, ...] = field(default=(), init=False, repr=False, compare=False)

    def _sync_fill_cache(self) -> None:
        n = len(self.fills)
        if n == self._cache_len:
            return
        total_units = 0.0
        total_cost = 0.0
        total_fees = 0.0
        snapshots: list[PositionFillSnapshot] = []
        for fill in self.fills:
            total_units += fill.units
            total_cost += fill.price * fill.units
            total_fees += fill.fee
            snapshots.append(PositionFillSnapshot(time_ms=fill.time_ms, price=fill.price, units=fill.units))
        self._cached_units = total_units
        self._cached_cost_basis = total_cost
        self._cached_fees = total_fees
        self._cached_fill_snapshots = tuple(snapshots)
        self._cache_len = n

    @property
    def units(self) -> float:
        self._sync_fill_cache()
        return self._cached_units

    @property
    def entry_price(self) -> float:
        self._sync_fill_cache()
        if not self._cached_units:
            return 0.0
        return self._cached_cost_basis / self._cached_units

    @property
    def fees(self) -> float:
        self._sync_fill_cache()
        return self._cached_fees

    @property
    def fill_snapshots(self) -> tuple[PositionFillSnapshot, ...]:
        self._sync_fill_cache()
        return self._cached_fill_snapshots


@dataclass
class PendingEntry:
    fib_level: float
    expires_index: int
    stop_price: float | None = None


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
    if len(candles_15m) < 4 or starting_equity <= 0:
        return BacktestResult(starting_equity, starting_equity, [], [])

    closes = [bar.close for bar in candles_15m]
    rsi_values = rsi(closes, 14)
    _, _, hist_values = macd(closes)

    # Settled equity is also the existing sizing budget. Open-position fees
    # affect measurements only; debiting this budget would change later fills
    # and charge the same entry fees again at settlement. It is not free margin.
    equity = starting_equity
    peak_equity = starting_equity
    track_drawdown = config.max_account_drawdown_pct is not None
    track_daily = config.daily_loss_limit_pct is not None
    account_drawdown_halted = False
    current_day: date | None = None
    day_start_equity = starting_equity
    daily_loss_halted = False

    equity_curve: list[tuple[int, float]] = [(candles_15m[0].open_time_ms, equity)]
    trades: list[Trade] = []
    position: OpenPosition | None = None
    pending_entry: PendingEntry | None = None

    def _sync_trading_day(candle: Candle) -> None:
        nonlocal current_day, day_start_equity, daily_loss_halted
        if not track_daily:
            return
        bar_day = eastern_date(candle.open_time_ms)
        if bar_day != current_day:
            current_day = bar_day
            day_start_equity = _marked_equity(equity, position, candle.open)
            daily_loss_halted = False

    def _can_trade() -> bool:
        return equity > 0.0 and not account_drawdown_halted and not daily_loss_halted

    def _record_equity(time_ms: int, value: float) -> None:
        nonlocal peak_equity
        if track_drawdown and value > peak_equity:
            peak_equity = value
        equity_curve.append((time_ms, value))

    def record_close(candle: Candle) -> None:
        # All timestamps label the 15m bar, not an exact intrabar instant.
        # Preserve list order: executed events, observed close, final settlement.
        _record_equity(candle.open_time_ms, _marked_equity(equity, position, candle.close))

    def _exit_on_risk(candle: Candle, *, stop_reason: str) -> bool:
        # Called only with an open position. Own risk settlement and its event
        # sample here; callers retain their distinct close/continue boundaries.
        nonlocal equity, position, account_drawdown_halted, daily_loss_halted
        dd_stop_price: float | None = None
        if track_drawdown and peak_equity > 0.0:
            dd_floor = peak_equity * (1.0 - config.max_account_drawdown_pct)
            dd_stop_price = equity_floor_exit_price(
                equity_floor=dd_floor,
                settled_equity=equity,
                entry_price=position.entry_price,
                units=position.units,
                entry_fees=position.fees,
                fee_rate=config.fee_rate,
            )
        daily_stop_price: float | None = None
        if track_daily and day_start_equity > 0.0:
            daily_floor = day_start_equity * (1.0 - config.daily_loss_limit_pct)
            daily_stop_price = equity_floor_exit_price(
                equity_floor=daily_floor,
                settled_equity=equity,
                entry_price=position.entry_price,
                units=position.units,
                entry_fees=position.fees,
                fee_rate=config.fee_rate,
            )
        decision = _risk_exit(
            candle,
            position,
            config,
            stop_reason=stop_reason,
            drawdown_stop_price=dd_stop_price,
            daily_loss_stop_price=daily_stop_price,
        )
        if decision is None:
            return False
        exit_price, reason = decision
        equity, trade = _close_position(position, candle, exit_price, equity, reason, config)
        trades.append(trade)
        position = None
        _record_equity(candle.open_time_ms, equity)
        if track_drawdown and (
            reason == "account_drawdown_stop"
            or (peak_equity > 0.0 and equity <= peak_equity * (1.0 - config.max_account_drawdown_pct) + 1e-9)
        ):
            account_drawdown_halted = True
        if track_daily and (
            reason == "daily_loss_stop"
            or (day_start_equity > 0.0 and equity <= day_start_equity * (1.0 - config.daily_loss_limit_pct) + 1e-9)
        ):
            daily_loss_halted = True
        return True

    index = 1
    while index < len(candles_15m):
        candle = candles_15m[index]
        _sync_trading_day(candle)

        if position is None:
            if not _can_trade():
                pending_entry = None
                record_close(candle)
                index += 1
                continue

            if config.entry_execution == "second_pullback" and pending_entry is not None:
                if index > pending_entry.expires_index:
                    pending_entry = None
                elif candle.low <= pending_entry.fib_level:
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
                    stop_price = resolve_initial_stop(entry_price, pending_entry.stop_price, config)
                    position = OpenPosition([fill], stop_price, entry_price, stop_price)
                    pending_entry = None
                    _record_equity(candle.open_time_ms, _marked_equity(equity, position, entry_price))
                    _exit_on_risk(candle, stop_reason="initial_stop")
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
                    stop_price=signal.stop_price,
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
            _sync_trading_day(next_candle)
            entry_price = execution.entry_price
            fill = _make_fill(next_candle.open_time_ms, entry_price, config.margin_steps[0], equity, config)
            stop_price = resolve_initial_stop(entry_price, signal.stop_price, config)
            position = OpenPosition([fill], stop_price, entry_price, stop_price)
            _record_equity(next_candle.open_time_ms, _marked_equity(equity, position, entry_price))
            _exit_on_risk(next_candle, stop_reason="initial_stop")
            index += 1
            continue

        # Next-bar execution above has already checked this fill bar's initial
        # risk. Its surviving position still receives normal add/stop updates.
        entry_bar_risk_checked = position.fills[0].time_ms == candle.open_time_ms
        if not entry_bar_risk_checked and _exit_on_risk(candle, stop_reason="stop"):
            record_close(candle)
            index += 1
            continue

        fill_count = len(position.fills)
        if _can_trade():
            _execute_pyramid_add(
                position,
                candle,
                index,
                candles_15m,
                hourly_context,
                hist_values,
                rsi_values,
                equity,
                config,
            )
            if len(position.fills) > fill_count:
                _record_equity(candle.open_time_ms, _marked_equity(equity, position, position.fills[-1].price))
        else:
            position.pending_add = None
        _tighten_stop(position, candle, index, candles_15m, hourly_context.get(candle.open_time_ms, "yellow"), config)
        if _can_trade():
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
        _record_equity(final.open_time_ms, equity)

    return BacktestResult(starting_equity, equity, trades, equity_curve)


def _make_fill(time_ms: int, price: float, margin_fraction: float, equity: float, config: StrategyConfig) -> Fill:
    if equity <= 0.0:
        raise ValueError("equity must be positive to size a fill")
    if price <= 0.0:
        raise ValueError("fill price must be positive")
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


_sell_stop_fill_price = sell_stop_fill_price


def _risk_exit(
    candle: Candle,
    position: OpenPosition,
    config: StrategyConfig,
    *,
    stop_reason: str,
    drawdown_stop_price: float | None = None,
    daily_loss_stop_price: float | None = None,
) -> tuple[float, str] | None:
    """Resolve stop, equity-floor, and non-session liquidation exits via shared core risk helper."""
    decision = evaluate_risk_exit(
        candle,
        entry_price=position.entry_price,
        stop_price=position.stop_price,
        config=config,
        stop_reason=stop_reason,
        drawdown_stop_price=drawdown_stop_price,
        daily_loss_stop_price=daily_loss_stop_price,
    )
    if decision is None:
        return None
    return decision.exit_price, decision.reason


def _has_non_session_liquidation_risk(candle: Candle, position: OpenPosition, config: StrategyConfig) -> bool:
    return has_non_session_liquidation_risk(candle, position.entry_price, position.stop_price, config)


def _liquidation_risk_price(position: OpenPosition, config: StrategyConfig) -> float:
    return liquidation_risk_price(position.entry_price, config.leverage)


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
    if plan is None or equity <= 0.0:
        return
    candidate = plan.candidate
    if (
        index != plan.signal_index + 1
        or candle.open_time_ms != candidate.available_at_ms
        or position.max_stage + 1 != candidate.stage
    ):
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
        _position_snapshot(position),
        candle,
        rsi_value=rsi_values[index],
        macd_hist=hist_values[index],
        previous_macd_hist=hist_values[index - 1],
        regime=hourly_context.get(candle.open_time_ms, "yellow"),
        config=config,
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
        fills=position.fill_snapshots,
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
    position._sync_fill_cache()
    unrealized = (mark_price * position._cached_units) - position._cached_cost_basis
    return equity + unrealized - position._cached_fees


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
