"""Shared stop, liquidation, and equity-floor risk exit evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.models import Candle
from mu_strategy.strategy import StrategyConfig, is_preferred_us_cash_window

__all__ = [
    "RiskExitDecision",
    "equity_floor_exit_price",
    "evaluate_risk_exit",
    "has_non_session_liquidation_risk",
    "liquidation_risk_price",
    "resolve_initial_stop",
    "sell_stop_fill_price",
]


@dataclass(frozen=True)
class RiskExitDecision:
    exit_price: float
    reason: str
    trigger_basis: str


def liquidation_risk_price(entry_price: float, leverage: float) -> float:
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    return entry_price * max(0.0, 1.0 - (1.0 / leverage))


def sell_stop_fill_price(candle: Candle, stop_price: float) -> float | None:
    if candle.low > stop_price:
        return None
    if candle.open < stop_price:
        return candle.open
    return stop_price


def resolve_initial_stop(
    entry_price: float,
    signal_stop: float | None,
    config: StrategyConfig,
) -> float:
    """Honor signal technical stop while capping risk at initial_stop_pct and staying below entry."""
    hard_stop = entry_price * (1.0 - config.initial_stop_pct)
    if signal_stop is not None and signal_stop < entry_price:
        return max(hard_stop, signal_stop)
    return hard_stop


def equity_floor_exit_price(
    *,
    equity_floor: float,
    settled_equity: float,
    entry_price: float,
    units: float,
    entry_fees: float,
    fee_rate: float,
) -> float | None:
    """Price at which closing the position nets exactly ``equity_floor`` after entry and exit fees."""
    if units <= 0.0 or fee_rate >= 1.0:
        return None
    cost_basis = entry_price * units
    price = (equity_floor - settled_equity + cost_basis + entry_fees) / (units * (1.0 - fee_rate))
    return price if price > 0.0 else None


def has_non_session_liquidation_risk(
    candle: Candle,
    entry_price: float,
    stop_price: float,
    config: StrategyConfig,
    *,
    in_session: bool | None = None,
) -> bool:
    """True only when outside session and liquidation is hit without an active higher stop filling first."""
    liq_price = liquidation_risk_price(entry_price, config.leverage)
    if candle.low > liq_price:
        return False
    stop_fill = sell_stop_fill_price(candle, stop_price)
    if stop_fill is not None and stop_fill > liq_price:
        return False
    session_open = (
        is_preferred_us_cash_window(candle.open_time_ms, config)
        if in_session is None
        else in_session
    )
    return not session_open


def evaluate_risk_exit(
    candle: Candle,
    *,
    entry_price: float,
    stop_price: float,
    config: StrategyConfig,
    stop_reason: str = "stop",
    in_session: bool | None = None,
    drawdown_stop_price: float | None = None,
    daily_loss_stop_price: float | None = None,
) -> RiskExitDecision | None:
    """Resolve active stops vs non-session liquidation using physical price-path ordering.

    An active stop above liquidation fills first unless price gapped straight to or
    below the liquidation threshold at bar open (or the stop itself is at/below liquidation).
    """
    liq_price = liquidation_risk_price(entry_price, config.leverage)

    best_threshold: float | None = None
    best_fill: float | None = None
    best_reason = ""
    best_basis = ""

    for threshold, reason, basis in (
        (stop_price, stop_reason, "candle_low_at_or_below_stop_before_candle"),
        (drawdown_stop_price, "account_drawdown_stop", "candle_low_at_or_below_account_drawdown_stop"),
        (daily_loss_stop_price, "daily_loss_stop", "candle_low_at_or_below_daily_loss_stop"),
    ):
        if threshold is None or threshold <= 0.0:
            continue
        fill = sell_stop_fill_price(candle, threshold)
        if fill is not None and (best_threshold is None or threshold > best_threshold):
            best_threshold = threshold
            best_fill = fill
            best_reason = reason
            best_basis = basis

    liq_fill = sell_stop_fill_price(candle, liq_price)
    if liq_fill is not None and (best_fill is None or best_fill <= liq_price):
        session_open = (
            is_preferred_us_cash_window(candle.open_time_ms, config)
            if in_session is None
            else in_session
        )
        if not session_open:
            return RiskExitDecision(
                exit_price=liq_fill,
                reason="non_session_liquidation_risk",
                trigger_basis="candle_low_at_or_below_non_session_liquidation_risk_price",
            )

    if best_fill is not None:
        return RiskExitDecision(
            exit_price=best_fill,
            reason=best_reason,
            trigger_basis=best_basis,
        )
    return None
