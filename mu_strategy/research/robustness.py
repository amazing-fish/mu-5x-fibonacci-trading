"""Pure robustness metrics shared by backtests and strategy experiments.

Trade-based metrics define net PnL as ``Trade.pnl``.  The backtest stores PnL
after entry and exit fees in that field; ``Trade.fees`` records those same fees
separately for reporting.  Subtracting ``fees`` again would therefore double
count them.  A trade with zero PnL is neutral: it is neither a winner nor a
loser.

``top_n_share_of_net_pnl`` is ``None`` when aggregate net PnL is zero or
negative, because the ratio would be undefined or sign-flipped.  Positive
ratios are not clamped; values above 1.0 are an important concentration signal.

Returns expressed as ``*_pct`` are decimal ratios (for example, ``0.21`` means
21%).  The buy-and-hold benchmark is price-only and does not model fees,
funding, liquidation, or path-dependent leverage effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import fsum

from mu_strategy.models import Candle, Trade

__all__ = [
    "StageDistribution",
    "StageMetrics",
    "TradeConcentration",
    "buy_and_hold_return_pct",
    "stage_distribution",
    "trade_concentration",
]


@dataclass(frozen=True)
class TradeConcentration:
    """Aggregate dependence of net PnL on the largest winning trades."""

    net_pnl: float
    winner_count: int
    loser_count: int
    top_n_share_of_net_pnl: float | None
    net_pnl_excluding_top_n: float


@dataclass(frozen=True)
class StageMetrics:
    """Observed trade outcomes for one ``Trade.max_stage`` value."""

    max_stage: int
    trade_count: int
    win_count: int
    net_pnl: float
    win_rate: float


@dataclass(frozen=True)
class StageDistribution:
    """Metrics for observed stages, ordered by ``max_stage``."""

    stages: tuple[StageMetrics, ...]


def trade_concentration(trades: Sequence[Trade], *, top_n: int = 5) -> TradeConcentration:
    """Measure how much positive aggregate PnL depends on the top winners.

    Tied winners retain their input order, making the top-N selection stable.
    ``top_n`` may be zero or exceed the number of winners; negative values are
    rejected because they do not describe a meaningful selection.  Empty input
    returns zero PnL and counts, with a ``None`` concentration ratio.
    """

    if top_n < 0:
        raise ValueError("top_n must be non-negative")

    net_pnl = fsum(trade.pnl for trade in trades)
    winner_count = sum(1 for trade in trades if trade.pnl > 0)
    loser_count = sum(1 for trade in trades if trade.pnl < 0)
    indexed_winners = [(index, trade.pnl) for index, trade in enumerate(trades) if trade.pnl > 0]
    ranked_winners = sorted(indexed_winners, key=lambda item: (-item[1], item[0]))
    top_n_pnl = fsum(pnl for _, pnl in ranked_winners[:top_n])

    return TradeConcentration(
        net_pnl=net_pnl,
        winner_count=winner_count,
        loser_count=loser_count,
        top_n_share_of_net_pnl=top_n_pnl / net_pnl if net_pnl > 0 else None,
        net_pnl_excluding_top_n=fsum((net_pnl, -top_n_pnl)),
    )


def stage_distribution(trades: Sequence[Trade]) -> StageDistribution:
    """Group trade count, wins, net PnL, and win rate by observed stage.

    Empty input returns ``StageDistribution(stages=())``.  Stages that do not
    occur in the input are omitted rather than represented as zero rows.
    """

    pnl_by_stage: dict[int, list[float]] = {}
    for trade in trades:
        pnl_by_stage.setdefault(trade.max_stage, []).append(trade.pnl)

    stages = tuple(
        StageMetrics(
            max_stage=max_stage,
            trade_count=len(pnls),
            win_count=sum(1 for pnl in pnls if pnl > 0),
            net_pnl=fsum(pnls),
            win_rate=sum(1 for pnl in pnls if pnl > 0) / len(pnls),
        )
        for max_stage, pnls in sorted(pnl_by_stage.items())
    )
    return StageDistribution(stages=stages)


def buy_and_hold_return_pct(candles: Sequence[Candle], *, leverage: float = 1.0) -> float:
    """Return first-open-to-last-close benchmark performance as a decimal ratio.

    Fewer than two candles return ``0.0`` because no comparison window exists.
    A zero first open also returns ``0.0`` rather than raising a division error.
    The default ``leverage=1.0`` is unlevered.  Other values linearly scale the
    price return only: the result ignores liquidation, fees, funding, and path
    dependence, so it is a diagnostic reference bound rather than an achievable
    levered alternative.
    """

    if len(candles) < 2:
        return 0.0
    first_open = candles[0].open
    if first_open == 0:
        return 0.0
    return ((candles[-1].close / first_open) - 1.0) * leverage
