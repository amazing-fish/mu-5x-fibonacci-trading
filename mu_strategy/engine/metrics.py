"""Unified performance metrics, cost attribution, and benchmark utilities for Layer 3."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from heapq import heappop, heappush
from math import expm1, fsum, isfinite, log, sqrt
from types import MappingProxyType
from typing import Any, Literal

from mu_strategy.core.instrument import AtrBps, CostModel, InstrumentSpec
from mu_strategy.engine.fills import ExecutionModel, compute_position_liquidation_price
from mu_strategy.market_data.funding import MissingFundingHistoryError
from mu_strategy.strategies.features import compute_atr

__all__ = [
    "MS_PER_YEAR",
    "CostAttribution",
    "SimulationResult",
    "TradeRecord",
    "compute_buy_and_hold_benchmark",
    "compute_simulation_metrics",
]

MS_PER_YEAR: float = 365.0 * 86_400_000.0
_DEFAULT_INTERVAL_MS: int = 3_600_000
_ATTRIBUTION_TOLERANCE: float = 1e-6


def _clean_zero(value: float) -> float:
    return 0.0 if value == 0.0 else float(value)


def _require_finite(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return _clean_zero(float(value))


@dataclass(frozen=True)
class CostAttribution:
    """Exact PnL decomposition obeying ``net_pnl == gross_pnl - fees - slippage + funding_pnl``."""

    gross_pnl: float
    fees: float
    slippage: float
    funding_pnl: float
    net_pnl: float

    def __post_init__(self) -> None:
        gross = _require_finite("gross_pnl", self.gross_pnl)
        fees = _require_finite("fees", self.fees)
        slippage = _require_finite("slippage", self.slippage)
        funding = _require_finite("funding_pnl", self.funding_pnl)
        net = _require_finite("net_pnl", self.net_pnl)

        expected = fsum((gross, -fees, -slippage, funding))
        if abs(net - expected) > _ATTRIBUTION_TOLERANCE:
            raise ValueError(
                f"CostAttribution identity violated: net_pnl={net} != "
                f"gross_pnl({gross}) - fees({fees}) - slippage({slippage}) + funding_pnl({funding}) = {expected}"
            )

        object.__setattr__(self, "gross_pnl", gross)
        object.__setattr__(self, "fees", fees)
        object.__setattr__(self, "slippage", slippage)
        object.__setattr__(self, "funding_pnl", funding)
        object.__setattr__(self, "net_pnl", net)

    @classmethod
    def zero(cls) -> CostAttribution:
        return cls(0.0, 0.0, 0.0, 0.0, 0.0)

    @classmethod
    def from_components(
        cls,
        *,
        gross_pnl: float,
        fees: float,
        slippage: float,
        funding_pnl: float,
    ) -> CostAttribution:
        net_pnl = fsum((gross_pnl, -fees, -slippage, funding_pnl))
        return cls(
            gross_pnl=gross_pnl,
            fees=fees,
            slippage=slippage,
            funding_pnl=funding_pnl,
            net_pnl=net_pnl,
        )


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Closed trade record with full cost attribution."""

    entry_time_ms: int
    exit_time_ms: int
    side: Literal["long", "short"]
    entry_price: float
    exit_price: float
    qty: float
    gross_pnl: float
    fees: float
    slippage: float
    funding_pnl: float
    net_pnl: float
    return_pct: float
    reason: str
    symbol: str = ""
    max_stage: int = 1
    ref_entry_price: float | None = None
    ref_exit_price: float | None = None

    def __post_init__(self) -> None:
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {self.side!r}")
        entry_price = _require_finite("entry_price", self.entry_price)
        exit_price = _require_finite("exit_price", self.exit_price)
        qty = _require_finite("qty", self.qty)
        if entry_price <= 0.0:
            raise ValueError(f"entry_price must be positive, got {entry_price}")
        if exit_price <= 0.0:
            raise ValueError(f"exit_price must be positive, got {exit_price}")
        if qty <= 0.0:
            raise ValueError(f"qty must be positive, got {qty}")

        gross = _require_finite("gross_pnl", self.gross_pnl)
        fees = _require_finite("fees", self.fees)
        slippage = _require_finite("slippage", self.slippage)
        funding = _require_finite("funding_pnl", self.funding_pnl)
        net = _require_finite("net_pnl", self.net_pnl)
        ret = _require_finite("return_pct", self.return_pct)

        expected = fsum((gross, -fees, -slippage, funding))
        if abs(net - expected) > _ATTRIBUTION_TOLERANCE:
            raise ValueError(
                f"TradeRecord attribution identity violated: net_pnl={net} != {expected}"
            )

        object.__setattr__(self, "entry_price", entry_price)
        object.__setattr__(self, "exit_price", exit_price)
        object.__setattr__(self, "qty", qty)
        object.__setattr__(self, "gross_pnl", gross)
        object.__setattr__(self, "fees", fees)
        object.__setattr__(self, "slippage", slippage)
        object.__setattr__(self, "funding_pnl", funding)
        object.__setattr__(self, "net_pnl", net)
        object.__setattr__(self, "return_pct", ret)

    @classmethod
    def _from_validated(
        cls,
        *,
        entry_time_ms: int,
        exit_time_ms: int,
        side: Literal["long", "short"],
        entry_price: float,
        exit_price: float,
        qty: float,
        gross_pnl: float,
        fees: float,
        slippage: float,
        funding_pnl: float,
        net_pnl: float,
        return_pct: float,
        reason: str,
        symbol: str,
        max_stage: int,
        ref_entry_price: float | None,
        ref_exit_price: float | None,
    ) -> TradeRecord:
        obj = cls.__new__(cls)
        _tr_set_et(obj, entry_time_ms)
        _tr_set_xt(obj, exit_time_ms)
        _tr_set_side(obj, side)
        _tr_set_ep(obj, entry_price)
        _tr_set_xp(obj, exit_price)
        _tr_set_qty(obj, qty)
        _tr_set_gross(obj, 0.0 if gross_pnl == 0.0 else gross_pnl)
        _tr_set_fees(obj, 0.0 if fees == 0.0 else fees)
        _tr_set_slip(obj, 0.0 if slippage == 0.0 else slippage)
        _tr_set_fund(obj, 0.0 if funding_pnl == 0.0 else funding_pnl)
        _tr_set_net(obj, 0.0 if net_pnl == 0.0 else net_pnl)
        _tr_set_ret(obj, 0.0 if return_pct == 0.0 else return_pct)
        _tr_set_rsn(obj, reason)
        _tr_set_sym(obj, symbol)
        _tr_set_stg(obj, max_stage)
        _tr_set_rep(obj, ref_entry_price)
        _tr_set_rxp(obj, ref_exit_price)
        return obj


_tr_set_et = TradeRecord.entry_time_ms.__set__
_tr_set_xt = TradeRecord.exit_time_ms.__set__
_tr_set_side = TradeRecord.side.__set__
_tr_set_ep = TradeRecord.entry_price.__set__
_tr_set_xp = TradeRecord.exit_price.__set__
_tr_set_qty = TradeRecord.qty.__set__
_tr_set_gross = TradeRecord.gross_pnl.__set__
_tr_set_fees = TradeRecord.fees.__set__
_tr_set_slip = TradeRecord.slippage.__set__
_tr_set_fund = TradeRecord.funding_pnl.__set__
_tr_set_net = TradeRecord.net_pnl.__set__
_tr_set_ret = TradeRecord.return_pct.__set__
_tr_set_rsn = TradeRecord.reason.__set__
_tr_set_sym = TradeRecord.symbol.__set__
_tr_set_stg = TradeRecord.max_stage.__set__
_tr_set_rep = TradeRecord.ref_entry_price.__set__
_tr_set_rxp = TradeRecord.ref_exit_price.__set__


@dataclass(frozen=True)
class SimulationResult:
    """Unified simulation output with canonical bar-close equity curve and <= 0.0 drawdown."""

    starting_equity: float
    ending_equity: float
    equity_curve: tuple[tuple[int, float], ...]
    trades: tuple[TradeRecord, ...]
    attribution: CostAttribution
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    profit_factor: float
    win_rate: float
    expectancy: float
    turnover: float
    time_in_market_pct: float
    ex_top3_net_pnl: float
    ex_top5_net_pnl: float
    top3_pnl_share: float | None
    liquidations: int
    symbol: str = ""
    interval: str = ""
    strategy_id: str = ""
    bars_processed: int = 0
    bars_scored: int = 0
    funding_mode: str = "historical"
    diagnostics: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        max_dd = _require_finite("max_drawdown_pct", self.max_drawdown_pct)
        if max_dd > 0.0:
            raise ValueError(f"max_drawdown_pct must be <= 0.0, got {max_dd}")
        object.__setattr__(self, "starting_equity", _require_finite("starting_equity", self.starting_equity))
        object.__setattr__(self, "ending_equity", _require_finite("ending_equity", self.ending_equity))
        object.__setattr__(self, "total_return_pct", _require_finite("total_return_pct", self.total_return_pct))
        object.__setattr__(self, "max_drawdown_pct", max_dd)
        object.__setattr__(self, "sharpe_ratio", _require_finite("sharpe_ratio", self.sharpe_ratio))
        object.__setattr__(self, "sortino_ratio", _require_finite("sortino_ratio", self.sortino_ratio))
        object.__setattr__(self, "calmar_ratio", _require_finite("calmar_ratio", self.calmar_ratio))
        object.__setattr__(self, "win_rate", _require_finite("win_rate", self.win_rate))
        object.__setattr__(self, "expectancy", _require_finite("expectancy", self.expectancy))
        object.__setattr__(self, "turnover", _require_finite("turnover", self.turnover))
        object.__setattr__(self, "time_in_market_pct", _require_finite("time_in_market_pct", self.time_in_market_pct))
        object.__setattr__(self, "ex_top3_net_pnl", _require_finite("ex_top3_net_pnl", self.ex_top3_net_pnl))
        object.__setattr__(self, "ex_top5_net_pnl", _require_finite("ex_top5_net_pnl", self.ex_top5_net_pnl))
        if self.top3_pnl_share is not None:
            object.__setattr__(self, "top3_pnl_share", _require_finite("top3_pnl_share", self.top3_pnl_share))
        if not isinstance(self.diagnostics, MappingProxyType):
            object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))

    @property
    def trade_count(self) -> int:
        return len(self.trades)


def _infer_interval_ms(equity_curve: Sequence[tuple[int, float]], interval_ms: int | None) -> int:
    if interval_ms is not None and interval_ms > 0:
        return int(interval_ms)
    if len(equity_curve) < 2:
        return _DEFAULT_INTERVAL_MS
    diffs = [
        equity_curve[i][0] - equity_curve[i - 1][0]
        for i in range(1, len(equity_curve))
        if equity_curve[i][0] > equity_curve[i - 1][0]
    ]
    if not diffs:
        return _DEFAULT_INTERVAL_MS
    diffs.sort()
    return diffs[len(diffs) // 2]


def _compute_max_drawdown(starting_equity: float, equity_curve: Sequence[tuple[int, float]]) -> float:
    if not equity_curve:
        return 0.0
    peak = starting_equity if starting_equity > 0.0 else equity_curve[0][1]
    max_dd = 0.0
    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        if peak > 0.0:
            dd = (equity / peak) - 1.0
            if dd < max_dd:
                max_dd = dd
    return _clean_zero(min(0.0, max_dd))


def _compute_return_ratios(
    starting_equity: float,
    ending_equity: float,
    equity_curve: Sequence[tuple[int, float]],
    max_drawdown_pct: float,
    interval_ms: int,
    periods_per_year: float | None = None,
) -> tuple[float, float, float]:
    n_points = len(equity_curve)
    if n_points < 2:
        return 0.0, 0.0, 0.0

    ppy = periods_per_year if (periods_per_year is not None and periods_per_year > 0.0) else (MS_PER_YEAR / interval_ms)
    returns: list[float] = []
    prev_eq = equity_curve[0][1]
    for idx in range(1, n_points):
        curr_eq = equity_curve[idx][1]
        if prev_eq > 0.0:
            returns.append((curr_eq - prev_eq) / prev_eq)
        else:
            returns.append(0.0)
        prev_eq = curr_eq

    n_ret = len(returns)
    if n_ret == 0:
        return 0.0, 0.0, 0.0

    mean_r = fsum(returns) / n_ret
    scale = sqrt(ppy)

    sharpe = 0.0
    if n_ret >= 2:
        var = fsum((r - mean_r) * (r - mean_r) for r in returns) / (n_ret - 1)
        std_r = sqrt(var) if var > 0.0 else 0.0
        if std_r > 1e-15:
            sharpe = (mean_r / std_r) * scale

    downside_sq = fsum(r * r for r in returns if r < 0.0)
    downside_dev = sqrt(downside_sq / n_ret) if downside_sq > 0.0 else 0.0
    sortino = (mean_r / downside_dev) * scale if downside_dev > 1e-15 else 0.0

    calmar = 0.0
    if max_drawdown_pct < -1e-15 and starting_equity > 0.0:
        elapsed_ms = equity_curve[-1][0] - equity_curve[0][0]
        elapsed_years = (elapsed_ms / MS_PER_YEAR) if elapsed_ms > 0 else (n_ret / ppy)
        if elapsed_years > 0.0:
            if ending_equity <= 0.0:
                ann_return = -1.0
            else:
                ratio = ending_equity / starting_equity
                log_arg = log(ratio) / elapsed_years
                if log_arg > 50.0:
                    ann_return = ((ending_equity / starting_equity) - 1.0) / elapsed_years
                else:
                    ann_return = expm1(log_arg)
            calmar = ann_return / abs(max_drawdown_pct)

    return _clean_zero(sharpe), _clean_zero(sortino), _clean_zero(calmar)


def _finalize_curve_stats(
    *,
    starting_equity: float,
    ending_equity: float,
    first_ts: int,
    last_ts: int,
    n_points: int,
    max_dd: float,
    avg_equity: float,
    sum_r: float,
    sum_r2: float,
    downside_sq: float,
    interval_ms: int,
    periods_per_year: float | None = None,
) -> tuple[float, float, float, float, float]:
    """Finalize (max_drawdown_pct, avg_equity, sharpe, sortino, calmar) from online accumulators."""
    max_dd_clean = _clean_zero(min(0.0, max_dd))
    if n_points <= 1:
        return max_dd_clean, avg_equity, 0.0, 0.0, 0.0
    n_ret = n_points - 1
    ppy = periods_per_year if (periods_per_year is not None and periods_per_year > 0.0) else (MS_PER_YEAR / interval_ms)
    mean_r = sum_r / n_ret
    scale = sqrt(ppy)

    sharpe = 0.0
    if n_ret >= 2:
        ssd = sum_r2 - (sum_r * mean_r)
        if ssd > 0.0:
            std_r = sqrt(ssd / (n_ret - 1))
            if std_r > 1e-15:
                sharpe = (mean_r / std_r) * scale

    sortino = 0.0
    if downside_sq > 0.0:
        downside_dev = sqrt(downside_sq / n_ret)
        if downside_dev > 1e-15:
            sortino = (mean_r / downside_dev) * scale

    calmar = 0.0
    if max_dd_clean < -1e-15 and starting_equity > 0.0:
        elapsed_ms = last_ts - first_ts
        elapsed_years = (elapsed_ms / MS_PER_YEAR) if elapsed_ms > 0 else (n_ret / ppy)
        if elapsed_years > 0.0:
            if ending_equity <= 0.0:
                ann_return = -1.0
            else:
                ratio = ending_equity / starting_equity
                log_arg = log(ratio) / elapsed_years
                if log_arg > 50.0:
                    ann_return = ((ending_equity / starting_equity) - 1.0) / elapsed_years
                else:
                    ann_return = expm1(log_arg)
            calmar = ann_return / abs(max_dd_clean)

    return max_dd_clean, avg_equity, _clean_zero(sharpe), _clean_zero(sortino), _clean_zero(calmar)


def _compute_curve_stats_from_values(
    starting_equity: float,
    ending_equity: float,
    first_ts: int,
    last_ts: int,
    eq_values: Sequence[float],
    interval_ms: int,
    periods_per_year: float | None = None,
) -> tuple[float, float, float, float, float]:
    """Single-pass stats directly over a flat equity sequence (zero tuple unpacking)."""
    n_points = len(eq_values)
    if n_points == 0:
        return 0.0, starting_equity, 0.0, 0.0, 0.0

    prev_eq = eq_values[0]
    peak = starting_equity if starting_equity > 0.0 else prev_eq
    if prev_eq > peak:
        peak = prev_eq
    min_ratio = (prev_eq / peak) if (peak > 0.0 and prev_eq < peak) else 1.0

    if n_points == 1:
        return _clean_zero(min(0.0, min_ratio - 1.0)), prev_eq, 0.0, 0.0, 0.0

    avg_equity = sum(eq_values) / n_points
    sum_r = 0.0
    sum_r2 = 0.0
    downside_sq = 0.0

    all_positive = min(eq_values) > 0.0 and peak > 0.0
    it = iter(eq_values)
    next(it)

    if all_positive:
        dd_thresh = peak * min_ratio
        for curr_eq in it:
            if curr_eq == prev_eq:
                continue
            if curr_eq > peak:
                peak = curr_eq
                dd_thresh = peak * min_ratio
            elif curr_eq < dd_thresh:
                min_ratio = curr_eq / peak
                dd_thresh = curr_eq
            r = (curr_eq - prev_eq) / prev_eq
            sum_r += r
            r2 = r * r
            sum_r2 += r2
            if r < 0.0:
                downside_sq += r2
            prev_eq = curr_eq
        max_dd = min(0.0, min_ratio - 1.0)
    else:
        max_dd = min(0.0, min_ratio - 1.0)
        for curr_eq in it:
            if curr_eq == prev_eq:
                continue
            if curr_eq > peak:
                peak = curr_eq
            elif peak > 0.0:
                dd = (curr_eq / peak) - 1.0
                if dd < max_dd:
                    max_dd = dd
            if prev_eq > 0.0:
                r = (curr_eq - prev_eq) / prev_eq
                sum_r += r
                r2 = r * r
                sum_r2 += r2
                if r < 0.0:
                    downside_sq += r2
            prev_eq = curr_eq

    return _finalize_curve_stats(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        first_ts=first_ts,
        last_ts=last_ts,
        n_points=n_points,
        max_dd=max_dd,
        avg_equity=avg_equity,
        sum_r=sum_r,
        sum_r2=sum_r2,
        downside_sq=downside_sq,
        interval_ms=interval_ms,
        periods_per_year=periods_per_year,
    )


def _compute_curve_stats(
    starting_equity: float,
    ending_equity: float,
    equity_curve: Sequence[tuple[int, float]],
    interval_ms: int,
    periods_per_year: float | None = None,
) -> tuple[float, float, float, float, float]:
    """Single-pass computation of (max_drawdown_pct, avg_equity, sharpe, sortino, calmar)."""
    if not equity_curve:
        return 0.0, starting_equity, 0.0, 0.0, 0.0
    eq_values = [pt[1] for pt in equity_curve]
    return _compute_curve_stats_from_values(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        first_ts=equity_curve[0][0],
        last_ts=equity_curve[-1][0],
        eq_values=eq_values,
        interval_ms=interval_ms,
        periods_per_year=periods_per_year,
    )


def compute_simulation_metrics(
    *,
    starting_equity: float,
    ending_equity: float,
    equity_curve: Sequence[tuple[int, float]],
    trades: Sequence[TradeRecord],
    attribution: CostAttribution,
    total_traded_notional: float = 0.0,
    bars_in_market: int = 0,
    bars_scored: int = 0,
    bars_processed: int = 0,
    liquidations: int = 0,
    interval_ms: int | None = None,
    periods_per_year: float | None = None,
    symbol: str = "",
    interval: str = "",
    strategy_id: str = "",
    funding_mode: str = "historical",
    diagnostics: Mapping[str, Any] | None = None,
    curve_normalized: bool = False,
    equity_values: Sequence[float] | None = None,
    precomputed_curve_stats: tuple[float, float, float, float, float] | None = None,
) -> SimulationResult:
    """Compute unified Layer 3 metrics from canonical bar-close equity curve and trades."""
    if curve_normalized:
        curve_tuple = equity_curve if type(equity_curve) is tuple else tuple(equity_curve)
    else:
        curve_tuple = tuple((int(ts), _clean_zero(float(eq))) for ts, eq in equity_curve)
    trades_tuple = trades if type(trades) is tuple else tuple(trades)

    total_return_pct = (
        ((ending_equity / starting_equity) - 1.0) if starting_equity > 0.0 else 0.0
    )
    resolved_interval_ms = _infer_interval_ms(curve_tuple, interval_ms)
    if precomputed_curve_stats is not None:
        max_drawdown_pct, avg_equity, sharpe, sortino, calmar = precomputed_curve_stats
    elif equity_values is not None and curve_tuple:
        max_drawdown_pct, avg_equity, sharpe, sortino, calmar = _compute_curve_stats_from_values(
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            first_ts=curve_tuple[0][0],
            last_ts=curve_tuple[-1][0],
            eq_values=equity_values,
            interval_ms=resolved_interval_ms,
            periods_per_year=periods_per_year,
        )
    else:
        max_drawdown_pct, avg_equity, sharpe, sortino, calmar = _compute_curve_stats(
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            equity_curve=curve_tuple,
            interval_ms=resolved_interval_ms,
            periods_per_year=periods_per_year,
        )

    n_trades = len(trades_tuple)
    if n_trades > 0:
        wins = 0
        gross_profit = 0.0
        gross_loss = 0.0
        total_trade_net_pnl = 0.0
        indexed_winners: list[tuple[int, float]] = []
        for idx, t in enumerate(trades_tuple):
            pnl = t.net_pnl
            total_trade_net_pnl += pnl
            if pnl > 0.0:
                wins += 1
                gross_profit += pnl
                indexed_winners.append((idx, pnl))
            elif pnl < 0.0:
                gross_loss -= pnl

        win_rate = wins / n_trades
        expectancy = total_trade_net_pnl / n_trades
        if gross_loss == 0.0:
            profit_factor = float("inf") if gross_profit > 0.0 else 0.0
        else:
            profit_factor = gross_profit / gross_loss

        ranked_winners = sorted(indexed_winners, key=lambda item: (-item[1], item[0]))
        top3_pnl = fsum(pnl for _, pnl in ranked_winners[:3])
        top5_pnl = fsum(pnl for _, pnl in ranked_winners[:5])
        ex_top3_net_pnl = fsum((total_trade_net_pnl, -top3_pnl))
        ex_top5_net_pnl = fsum((total_trade_net_pnl, -top5_pnl))
        top3_pnl_share = (top3_pnl / total_trade_net_pnl) if total_trade_net_pnl > 0.0 else None
    else:
        win_rate = 0.0
        expectancy = 0.0
        profit_factor = 0.0
        ex_top3_net_pnl = 0.0
        ex_top5_net_pnl = 0.0
        top3_pnl_share = None

    denom_equity = avg_equity if avg_equity > 0.0 else starting_equity
    turnover = (total_traded_notional / denom_equity) if denom_equity > 0.0 else 0.0

    scored_count = bars_scored if bars_scored > 0 else len(curve_tuple)
    time_in_market_pct = (bars_in_market / scored_count) if scored_count > 0 else 0.0
    time_in_market_pct = min(1.0, max(0.0, time_in_market_pct))

    return SimulationResult(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        equity_curve=curve_tuple,
        trades=trades_tuple,
        attribution=attribution,
        total_return_pct=_clean_zero(total_return_pct),
        max_drawdown_pct=max_drawdown_pct,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        profit_factor=profit_factor,
        win_rate=_clean_zero(win_rate),
        expectancy=_clean_zero(expectancy),
        turnover=_clean_zero(turnover),
        time_in_market_pct=_clean_zero(time_in_market_pct),
        ex_top3_net_pnl=_clean_zero(ex_top3_net_pnl),
        ex_top5_net_pnl=_clean_zero(ex_top5_net_pnl),
        top3_pnl_share=_clean_zero(top3_pnl_share) if top3_pnl_share is not None else None,
        liquidations=liquidations,
        symbol=symbol,
        interval=interval,
        strategy_id=strategy_id,
        bars_processed=bars_processed if bars_processed > 0 else len(curve_tuple),
        bars_scored=scored_count,
        funding_mode=funding_mode,
        diagnostics=MappingProxyType(dict(diagnostics or {})),
    )


def _extract_bar_arrays(
    bars: Any,
    interval_ms: int | None,
) -> tuple[
    Sequence[int],
    Sequence[int],
    Sequence[float],
    Sequence[float],
    Sequence[float],
    Sequence[float],
    Sequence[float],
    int,
    str,
    str,
]:
    """Support BarSeries, BarSeriesSlice, or Sequence[Candle] without hard Layer 1 import cycles."""
    if (
        hasattr(bars, "open_times")
        and hasattr(bars, "close_times")
        and hasattr(bars, "opens")
        and hasattr(bars, "highs")
        and hasattr(bars, "lows")
        and hasattr(bars, "closes")
    ):
        # BarSeries or BarSeriesSlice (BarSeriesSlice columns are already bounded views!)
        base = getattr(bars, "_series", None)
        if base is None:
            base = getattr(bars, "series", None)
        resolved_iv_ms = int(interval_ms or getattr(bars, "interval_ms", getattr(base, "interval_ms", _DEFAULT_INTERVAL_MS)))
        vols = bars.volumes if hasattr(bars, "volumes") else [0.0] * len(bars.closes)
        return (
            bars.open_times,
            bars.close_times,
            bars.opens,
            bars.highs,
            bars.lows,
            bars.closes,
            vols,
            resolved_iv_ms,
            getattr(bars, "symbol", getattr(base, "symbol", "")),
            getattr(bars, "interval", getattr(base, "interval", "")),
        )

    # Fallback: sequence of Candle-like objects
    seq = list(bars)
    if not seq:
        return (), (), (), (), (), (), (), int(interval_ms or _DEFAULT_INTERVAL_MS), "", ""
    inferred_ms = interval_ms
    if inferred_ms is None:
        if len(seq) >= 2 and seq[1].open_time_ms > seq[0].open_time_ms:
            inferred_ms = seq[1].open_time_ms - seq[0].open_time_ms
        else:
            inferred_ms = _DEFAULT_INTERVAL_MS
    open_times = [int(c.open_time_ms) for c in seq]
    close_times = [int(c.open_time_ms) + inferred_ms for c in seq]
    opens = [float(c.open) for c in seq]
    highs = [float(c.high) for c in seq]
    lows = [float(c.low) for c in seq]
    closes = [float(c.close) for c in seq]
    volumes = [float(getattr(c, "volume", 0.0)) for c in seq]
    return open_times, close_times, opens, highs, lows, closes, volumes, int(inferred_ms), "", ""


def compute_buy_and_hold_benchmark(
    bars: Any,
    *,
    starting_equity: float = 10_000.0,
    leverage: float = 1.0,
    side: Literal["long", "short"] = "long",
    instrument: InstrumentSpec | None = None,
    cost_model: CostModel | None = None,
    funding: Any | None = None,
    allow_missing_funding_for_perp: bool = False,
    funding_gap_policy: Literal["fail_closed", "allow_partial"] = "fail_closed",
    funding_gap_tolerance: float = 1.5,
    taker_fee_bps: float = 0.0,
    half_spread_bps: float = 0.0,
    maintenance_margin_rate: float | None = None,
    liquidation_fee_bps: float | None = None,
    atr_period: int = 14,
    warmup_bars: int = 0,
    close_on_end: bool = True,
    interval_ms: int | None = None,
    symbol: str = "",
    interval: str = "",
) -> SimulationResult:
    """Compute a realistic buy-and-hold (or short-and-hold) benchmark on the same bars, funding, and liquidation rules."""
    if starting_equity <= 0.0 or not isfinite(starting_equity):
        raise ValueError(f"starting_equity must be finite and positive, got {starting_equity}")
    if leverage <= 0.0 or not isfinite(leverage):
        raise ValueError(f"leverage must be finite and positive, got {leverage}")
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    if isinstance(atr_period, bool) or not isinstance(atr_period, int) or atr_period < 1:
        raise ValueError(f"atr_period must be an integer >= 1, got {atr_period!r}")
    if warmup_bars < 0:
        raise ValueError(f"warmup_bars must be >= 0, got {warmup_bars}")
    if funding_gap_policy not in ("fail_closed", "allow_partial"):
        raise ValueError(f"funding_gap_policy must be 'fail_closed' or 'allow_partial', got {funding_gap_policy!r}")
    if (
        isinstance(funding_gap_tolerance, bool)
        or not isinstance(funding_gap_tolerance, (int, float))
        or not isfinite(funding_gap_tolerance)
        or funding_gap_tolerance <= 0.0
    ):
        raise ValueError(f"funding_gap_tolerance must be positive finite, got {funding_gap_tolerance!r}")

    (
        open_times,
        close_times,
        opens,
        highs,
        lows,
        closes,
        volumes,
        resolved_interval_ms,
        inferred_symbol,
        inferred_interval,
    ) = _extract_bar_arrays(bars, interval_ms)

    # Validate explicit symbol and instrument against bar symbol metadata (CR2-32, CR3-02).
    if symbol and inferred_symbol and symbol != inferred_symbol:
        raise ValueError(
            f"benchmark symbol {symbol!r} does not match bars symbol {inferred_symbol!r}"
        )
    if instrument is not None:
        if inferred_symbol and inferred_symbol != instrument.symbol:
            raise ValueError(
                f"benchmark instrument symbol {instrument.symbol!r} does not match "
                f"bars symbol {inferred_symbol!r}"
            )
        if symbol and symbol != instrument.symbol:
            raise ValueError(
                f"benchmark symbol {symbol!r} does not match instrument symbol {instrument.symbol!r}"
            )

    resolved_symbol = symbol or inferred_symbol or (instrument.symbol if instrument is not None else "")
    resolved_interval = interval or inferred_interval
    n = len(closes)

    # Validate funding instrument alignment and fail-closed coverage before scoring (CR1-50, CR2-47).
    bench_diagnostics: dict[str, Any] = {}
    is_perp = instrument is not None and instrument.funding_interval_hours is not None
    if instrument is not None and funding is not None and hasattr(funding, "instrument_id"):
        if funding.instrument_id != instrument.instrument_id:
            raise ValueError(
                f"FundingSeries instrument_id {funding.instrument_id!r} does not match "
                f"benchmark instrument {instrument.instrument_id!r}"
            )

    effective_funding = funding
    funding_is_empty = effective_funding is None or (
        hasattr(effective_funding, "__len__") and len(effective_funding) == 0
    )
    if instrument is not None and not is_perp:
        # Non-perpetual instruments (spot / equity) do not settle funding.
        effective_funding = None
        funding_mode = "none"
    elif is_perp:
        if funding_is_empty:
            if not allow_missing_funding_for_perp:
                raise MissingFundingHistoryError(
                    f"Perpetual benchmark for {instrument.instrument_id!r} requires non-empty funding history. "
                    "Supply a complete FundingSeries or explicitly set allow_missing_funding_for_perp=True."
                )
            effective_funding = None
            funding_mode = "missing_explicit_zero"
        elif n > warmup_bars and hasattr(effective_funding, "coverage_gaps"):
            span_start = int(open_times[warmup_bars])
            span_end = int(close_times[-1])
            gaps = effective_funding.coverage_gaps(
                span_start,
                span_end,
                instrument.funding_interval_hours,  # type: ignore[arg-type]
                tolerance=funding_gap_tolerance,
            )
            if gaps:
                cov_ratio = effective_funding.coverage_ratio(
                    span_start,
                    span_end,
                    instrument.funding_interval_hours,  # type: ignore[arg-type]
                    tolerance=funding_gap_tolerance,
                )
                if funding_gap_policy == "fail_closed" and not allow_missing_funding_for_perp:
                    raise MissingFundingHistoryError(
                        f"Incomplete funding history for perpetual benchmark {instrument.instrument_id!r}: "
                        f"{len(gaps)} gap(s) covering {(1.0 - cov_ratio) * 100.0:.1f}% of scored span. "
                        "Provide complete funding history or set funding_gap_policy='allow_partial'."
                    )
                funding_mode = "historical_partial"
                bench_diagnostics["funding_coverage_ratio"] = cov_ratio
                bench_diagnostics["funding_gap_count"] = len(gaps)
            else:
                funding_mode = "historical"
        else:
            funding_mode = "historical"
    else:
        # instrument is None: preserve no-funding path when funding is omitted,
        # but strictly validate non-empty coverage whenever a FundingSeries IS supplied.
        if effective_funding is None:
            funding_mode = "none"
        elif funding_is_empty:
            if not allow_missing_funding_for_perp:
                raise MissingFundingHistoryError(
                    f"Empty funding history supplied for benchmark ({getattr(effective_funding, 'instrument_id', resolved_symbol)!r}). "
                    "Supply a complete FundingSeries or explicitly set allow_missing_funding_for_perp=True."
                )
            effective_funding = None
            funding_mode = "missing_explicit_zero"
        elif n > warmup_bars and hasattr(effective_funding, "coverage_gaps"):
            span_start = int(open_times[warmup_bars])
            span_end = int(close_times[-1])
            f_iv_h = getattr(effective_funding, "settlement_interval_hours", None) or 8
            gaps = effective_funding.coverage_gaps(
                span_start,
                span_end,
                f_iv_h,
                tolerance=funding_gap_tolerance,
            )
            if gaps:
                cov_ratio = effective_funding.coverage_ratio(
                    span_start,
                    span_end,
                    f_iv_h,
                    tolerance=funding_gap_tolerance,
                )
                if funding_gap_policy == "fail_closed" and not allow_missing_funding_for_perp:
                    raise MissingFundingHistoryError(
                        f"Incomplete funding history for benchmark ({getattr(effective_funding, 'instrument_id', resolved_symbol)!r}): "
                        f"{len(gaps)} gap(s) covering {(1.0 - cov_ratio) * 100.0:.1f}% of scored span. "
                        "Provide complete funding history or set funding_gap_policy='allow_partial'."
                    )
                funding_mode = "historical_partial"
                bench_diagnostics["funding_coverage_ratio"] = cov_ratio
                bench_diagnostics["funding_gap_count"] = len(gaps)
            else:
                funding_mode = "historical"
        else:
            funding_mode = "historical"

    if n == 0 or warmup_bars >= n:
        return compute_simulation_metrics(
            starting_equity=starting_equity,
            ending_equity=starting_equity,
            equity_curve=(),
            trades=(),
            attribution=CostAttribution.zero(),
            bars_processed=n,
            bars_scored=0,
            interval_ms=resolved_interval_ms,
            symbol=resolved_symbol,
            interval=resolved_interval,
            strategy_id=f"buy_and_hold_{side}",
            funding_mode=funding_mode,
            diagnostics=bench_diagnostics,
        )

    # Resolve execution & liquidation parameters using the simulator's rules (CR1-52, CR2-33, CR2-34).
    execution: ExecutionModel | None = None
    atr_arr = None
    if instrument is not None and cost_model is not None:
        overrides: dict[str, Any] = {}
        if maintenance_margin_rate is not None:
            overrides["maintenance_margin_rate"] = float(maintenance_margin_rate)
        if liquidation_fee_bps is not None:
            overrides["liquidation_fee_bps"] = float(liquidation_fee_bps)
        effective_cost = replace(cost_model, **overrides) if overrides else cost_model
        execution = ExecutionModel(instrument, effective_cost)
        if isinstance(effective_cost.slippage_model, AtrBps):
            atr_arr = compute_atr(highs, lows, closes, atr_period)
        asset_class = instrument.asset_class
        mmr = float(effective_cost.maintenance_margin_rate)
        liq_fee_bps_eff = float(effective_cost.liquidation_fee_bps)
        fee_rate = float(effective_cost.taker_fee_bps) / 10_000.0
        spread_rate = float(effective_cost.half_spread_bps) / 10_000.0
    else:
        if cost_model is not None:
            fee_rate = float(cost_model.taker_fee_bps) / 10_000.0
            spread_rate = float(cost_model.half_spread_bps) / 10_000.0
            mmr = float(cost_model.maintenance_margin_rate if maintenance_margin_rate is None else maintenance_margin_rate)
            liq_fee_bps_eff = float(cost_model.liquidation_fee_bps if liquidation_fee_bps is None else liquidation_fee_bps)
        else:
            fee_rate = max(0.0, float(taker_fee_bps)) / 10_000.0
            spread_rate = max(0.0, float(half_spread_bps)) / 10_000.0
            mmr = float(maintenance_margin_rate) if maintenance_margin_rate is not None else 0.0
            liq_fee_bps_eff = float(liquidation_fee_bps) if liquidation_fee_bps is not None else 0.0
        asset_class = (
            instrument.asset_class
            if instrument is not None
            else ("perp" if (leverage > 1.0 or side == "short" or effective_funding is not None or mmr > 0.0) else "spot")
        )
    if mmr < 0.0 or mmr >= 1.0 or not isfinite(mmr):
        raise ValueError(f"maintenance_margin_rate must be in [0, 1), got {mmr!r}")
    if liq_fee_bps_eff < 0.0 or not isfinite(liq_fee_bps_eff):
        raise ValueError(f"liquidation_fee_bps must be non-negative finite, got {liq_fee_bps_eff!r}")
    liq_total_fee_rate = fee_rate + (liq_fee_bps_eff / 10_000.0)

    side_sign = 1.0 if side == "long" else -1.0
    entry_side: Literal["buy", "sell"] = "buy" if side == "long" else "sell"
    close_side: Literal["buy", "sell"] = "sell" if side == "long" else "buy"

    start_idx = warmup_bars
    ref_entry = float(opens[start_idx])
    if ref_entry <= 0.0:
        raise ValueError(f"benchmark entry open price must be positive, got {ref_entry}")

    def _prior_closed_index(idx: int, fill_open_ms: int) -> int | None:
        """Select the latest bar strictly closed at or before ``fill_open_ms`` (CR3-03)."""
        if idx <= 0:
            return None
        if int(close_times[idx - 1]) <= fill_open_ms:
            return idx - 1
        j = idx - 2
        while j >= 0 and int(close_times[j]) > fill_open_ms:
            j -= 1
        return j if j >= 0 else None

    entry_open_ms = int(open_times[start_idx])
    entry_prior_idx = _prior_closed_index(start_idx, entry_open_ms)

    if execution is not None:
        # Strictly causal pre-open estimates selected by close time <= fill open (CR2-30, CR3-03).
        pre_close = float(closes[entry_prior_idx]) if entry_prior_idx is not None else ref_entry
        pre_vol = float(volumes[entry_prior_idx]) if entry_prior_idx is not None else 0.0
        pre_high = float(highs[entry_prior_idx]) if entry_prior_idx is not None else ref_entry
        pre_low = float(lows[entry_prior_idx]) if entry_prior_idx is not None else ref_entry
        pre_atr = float(atr_arr[entry_prior_idx]) if (atr_arr is not None and entry_prior_idx is not None) else None
        qty_abs = (starting_equity * leverage) / ref_entry
        entry_quote = execution.fill_market(
            entry_side,
            qty_abs,
            bar_open=ref_entry,
            bar_high=pre_high,
            bar_low=pre_low,
            bar_close=pre_close,
            bar_volume=pre_vol,
            bar_atr=pre_atr,
            reason="benchmark_entry",
        )
        exec_entry = entry_quote.exec_price
        entry_notional = entry_quote.notional
        entry_fee = entry_quote.fee
        entry_slippage = entry_quote.slippage_cost
    else:
        exec_entry = ref_entry * (1.0 + side_sign * spread_rate)
        qty_abs = (starting_equity * leverage) / exec_entry
        entry_notional = exec_entry * qty_abs
        entry_fee = entry_notional * fee_rate
        entry_slippage = (exec_entry - ref_entry) * (side_sign * qty_abs)

    signed_qty = side_sign * qty_abs
    cash = starting_equity - entry_fee
    accrued_funding = 0.0
    total_traded_notional = entry_notional
    equity_curve_slots: list[tuple[int, float] | None] = [None] * (n - start_idx)
    pending_closes: list[tuple[int, int, float | None]] = []
    trades: list[TradeRecord] = []
    entry_bar_idx = start_idx
    bars_in_market = 0
    liquidations = 0
    is_open = True
    settled_until_ms = entry_open_ms

    def _compute_bench_liq_price(curr_cash: float) -> float | None:
        if asset_class in ("spot", "equity") and side == "long":
            return None
        return compute_position_liquidation_price(
            side=side,
            entry_price=exec_entry,
            qty=qty_abs,
            margin_equity=curr_cash,
            maintenance_margin_rate=mmr,
            liquidation_fee_bps=liq_fee_bps_eff,
        )

    def _causal_event_mark(ev_ts_ms: int) -> float:
        """Select the unique causally observable mark at or before ``ev_ts_ms`` (CR5-01 / CR6-01).

        Because ``open_times`` is strictly increasing and ``open_times[j] <= close_times[j]``,
        only bars with ``j <= open_pos`` can have an open or close timestamp ``<= ev_ts_ms``.
        Prefers the latest bar close with ``close_time_ms <= ev_ts_ms`` (breaking exact ties in
        favor of close), or a strictly newer bar open with ``open_time_ms <= ev_ts_ms``, falling
        back to ``ref_entry``. Applied uniformly across non-overlapping and overlapping bars.
        """
        open_pos = bisect_right(open_times, ev_ts_ms) - 1
        if open_pos < 0:
            return ref_entry
        best_close_ts: int | None = None
        best_close_px = 0.0
        for j in range(open_pos, -1, -1):
            c_ts = int(close_times[j])
            if c_ts <= ev_ts_ms:
                if best_close_ts is None or c_ts > best_close_ts:
                    c_px = float(closes[j])
                    if c_px > 0.0:
                        best_close_ts = c_ts
                        best_close_px = c_px
                elif c_ts < best_close_ts:
                    break
        best_open_ts: int | None = None
        best_open_px = 0.0
        k = open_pos
        while k >= 0:
            o_px = float(opens[k])
            if o_px > 0.0:
                best_open_ts = int(open_times[k])
                best_open_px = o_px
                break
            k -= 1
        if best_close_ts is not None and best_close_ts == ev_ts_ms:
            return best_close_px
        if best_open_ts is not None and (best_close_ts is None or best_open_ts >= best_close_ts):
            return best_open_px
        if best_close_ts is not None:
            return best_close_px
        return ref_entry

    def _settle_funding_up_to(end_ms: int) -> None:
        nonlocal cash, accrued_funding, liq_price, settled_until_ms
        if is_open and effective_funding is not None and end_ms > settled_until_ms:
            events = effective_funding.between(settled_until_ms, end_ms)
            if events:
                for ev in events:
                    ev_ts = int(ev.ts_ms) if hasattr(ev, "ts_ms") else int(ev[0])
                    rate = ev.rate if hasattr(ev, "rate") else ev[1]
                    ev_mark = _causal_event_mark(ev_ts)
                    delta = -signed_qty * ev_mark * float(rate)
                    cash += delta
                    accrued_funding += delta
                liq_price = _compute_bench_liq_price(cash)
            settled_until_ms = end_ms

    def _liquidate_bench(
        *,
        exit_bar_idx: int,
        exit_time_ms: int,
        liq_threshold: float,
        gapped_open: bool,
        use_prior_open_slip: bool,
        b_open: float,
        b_high: float,
        b_low: float,
        b_close: float,
        b_vol: float,
        b_atr: float | None,
    ) -> None:
        nonlocal cash, total_traded_notional, bars_in_market, is_open, liq_price, liquidations
        if execution is not None:
            if use_prior_open_slip:
                gap_prior_idx = _prior_closed_index(exit_bar_idx, exit_time_ms)
                p_high_l = float(highs[gap_prior_idx]) if gap_prior_idx is not None else b_open
                p_low_l = float(lows[gap_prior_idx]) if gap_prior_idx is not None else b_open
                p_close_l = float(closes[gap_prior_idx]) if gap_prior_idx is not None else b_open
                p_vol_l = float(volumes[gap_prior_idx]) if gap_prior_idx is not None else 0.0
                p_atr_l = (
                    float(atr_arr[gap_prior_idx])
                    if (atr_arr is not None and gap_prior_idx is not None)
                    else None
                )
                slip_l = execution.compute_slippage_price(
                    b_open,
                    order_notional=b_open * qty_abs,
                    bar_close=p_close_l,
                    bar_volume=p_vol_l,
                    bar_atr=p_atr_l,
                    bar_high=p_high_l,
                    bar_low=p_low_l,
                    is_stop=True,
                )
                adv_l = (b_open * execution._half_spread_rate) + slip_l
                exec_l = (
                    (b_open + adv_l)
                    if close_side == "buy"
                    else max(execution.min_price, b_open - adv_l)
                )
                liq_quote = execution._make_quote(
                    side=close_side,
                    order_type="liquidation",
                    ref_price=b_open,
                    exec_price=exec_l,
                    qty=qty_abs,
                    fee_rate=execution._liq_total_fee_rate,
                    liquidity="taker",
                    reason="liquidation",
                )
            else:
                liq_quote = execution.try_fill_liquidation(
                    signed_qty,
                    liq_threshold,
                    bar_open=b_open,
                    bar_high=b_high,
                    bar_low=b_low,
                    bar_close=b_close,
                    bar_volume=b_vol,
                    bar_atr=b_atr,
                )
                assert liq_quote is not None
            ref_exit = liq_quote.ref_price
            exec_exit = liq_quote.exec_price
            exit_notional = liq_quote.notional
            exit_fee = liq_quote.fee
            exit_slippage = liq_quote.slippage_cost
        else:
            ref_exit = b_open if gapped_open else liq_threshold
            exec_exit = ref_exit * (1.0 - side_sign * spread_rate)
            if exec_exit <= 0.0:
                exec_exit = ref_exit
            exit_notional = exec_exit * qty_abs
            exit_fee = exit_notional * liq_total_fee_rate
            exit_slippage = (exec_exit - ref_exit) * (-signed_qty)

        gross_pnl = signed_qty * (ref_exit - ref_entry)
        total_fees = entry_fee + exit_fee
        total_slip = entry_slippage + exit_slippage
        net_pnl = fsum((gross_pnl, -total_fees, -total_slip, accrued_funding))
        cash = starting_equity + net_pnl
        total_traded_notional += exit_notional
        bars_in_market += max(0, exit_bar_idx - entry_bar_idx)
        trades.append(
            TradeRecord(
                entry_time_ms=int(open_times[start_idx]),
                exit_time_ms=exit_time_ms,
                side=side,
                entry_price=exec_entry,
                exit_price=exec_exit,
                qty=qty_abs,
                gross_pnl=gross_pnl,
                fees=total_fees,
                slippage=total_slip,
                funding_pnl=accrued_funding,
                net_pnl=net_pnl,
                return_pct=net_pnl / entry_notional if entry_notional > 0.0 else 0.0,
                reason="liquidation",
                symbol=resolved_symbol,
                ref_entry_price=ref_entry,
                ref_exit_price=ref_exit,
            )
        )
        is_open = False
        liq_price = None
        liquidations += 1

    def _flush_pending_close(p_close_ms: int, p_idx: int, open_liq_price: float | None) -> None:
        nonlocal cash, total_traded_notional, bars_in_market, is_open, liq_price, liquidations
        p_open = float(opens[p_idx])
        p_high = float(highs[p_idx])
        p_low = float(lows[p_idx])
        p_close = float(closes[p_idx])
        p_vol = float(volumes[p_idx])
        p_atr = float(atr_arr[p_idx]) if atr_arr is not None else None

        # 1. Non-gap intrabar OHLC MMR liquidation check deferred to bar close so a later overlapping
        #    bar's intrabar wick cannot preempt an earlier bar's close or funding (CR7-03), while
        #    preserving pre-funding intrabar ordering on non-overlapping bars and avoiding false
        #    retroactive liquidation when subsequent funding raises liq_price past an earlier open (CR4-07).
        if is_open and open_liq_price is not None and liq_price is not None:
            eff_intrabar_liq = (
                min(open_liq_price, liq_price) if side == "long" else max(open_liq_price, liq_price)
            )
            intrabar_hit = (p_low <= eff_intrabar_liq) if side == "long" else (p_high >= eff_intrabar_liq)
            if intrabar_hit:
                _liquidate_bench(
                    exit_bar_idx=p_idx,
                    exit_time_ms=p_close_ms,
                    liq_threshold=liq_price,
                    gapped_open=((p_open <= liq_price) if side == "long" else (p_open >= liq_price)),
                    use_prior_open_slip=False,
                    b_open=p_open,
                    b_high=p_high,
                    b_low=p_low,
                    b_close=p_close,
                    b_vol=p_vol,
                    b_atr=p_atr,
                )

        # 2. Settle all funding up to this bar's close timestamp strictly causally (CR6-01 / CR6-02).
        _settle_funding_up_to(p_close_ms)

        if is_open:
            # 3. Post-close-funding maintenance-margin liquidation check before recording the close (CR7-02).
            if liq_price is not None and (
                (p_close <= liq_price) if side == "long" else (p_close >= liq_price)
            ):
                _liquidate_bench(
                    exit_bar_idx=p_idx,
                    exit_time_ms=p_close_ms,
                    liq_threshold=liq_price,
                    gapped_open=True,
                    use_prior_open_slip=False,
                    b_open=p_close,
                    b_high=p_close,
                    b_low=p_close,
                    b_close=p_close,
                    b_vol=p_vol,
                    b_atr=p_atr,
                )
                equity_curve_slots[p_idx - start_idx] = (p_close_ms, _clean_zero(cash))
                return

            unrealized = signed_qty * (p_close - exec_entry)
            marked_equity = cash + unrealized
            if marked_equity <= 0.0:
                if execution is not None:
                    b_quote = execution.fill_market(
                        close_side,
                        qty_abs,
                        bar_open=p_close,
                        bar_high=p_close,
                        bar_low=p_close,
                        bar_close=p_close,
                        bar_volume=p_vol,
                        bar_atr=p_atr,
                        reason="bankrupt",
                    )
                    ref_exit = b_quote.ref_price
                    exec_exit = b_quote.exec_price
                    exit_notional = b_quote.notional
                    exit_fee = b_quote.fee
                    exit_slippage = b_quote.slippage_cost
                else:
                    ref_exit = p_close
                    exec_exit = p_close * (1.0 - side_sign * spread_rate)
                    if exec_exit <= 0.0:
                        exec_exit = p_close
                    exit_notional = exec_exit * qty_abs
                    exit_fee = exit_notional * fee_rate
                    exit_slippage = (exec_exit - ref_exit) * (-signed_qty)
                gross_pnl = signed_qty * (ref_exit - ref_entry)
                total_fees = entry_fee + exit_fee
                total_slip = entry_slippage + exit_slippage
                net_pnl = fsum((gross_pnl, -total_fees, -total_slip, accrued_funding))
                cash = starting_equity + net_pnl
                total_traded_notional += exit_notional
                bars_in_market += max(0, p_idx - entry_bar_idx)
                trades.append(
                    TradeRecord(
                        entry_time_ms=int(open_times[start_idx]),
                        exit_time_ms=p_close_ms,
                        side=side,
                        entry_price=exec_entry,
                        exit_price=exec_exit,
                        qty=qty_abs,
                        gross_pnl=gross_pnl,
                        fees=total_fees,
                        slippage=total_slip,
                        funding_pnl=accrued_funding,
                        net_pnl=net_pnl,
                        return_pct=net_pnl / entry_notional if entry_notional > 0.0 else 0.0,
                        reason="bankrupt",
                        symbol=resolved_symbol,
                        ref_entry_price=ref_entry,
                        ref_exit_price=ref_exit,
                    )
                )
                is_open = False
                liq_price = None
                liquidations += 1
                equity_curve_slots[p_idx - start_idx] = (p_close_ms, _clean_zero(cash))
            else:
                equity_curve_slots[p_idx - start_idx] = (p_close_ms, _clean_zero(marked_equity))
        else:
            equity_curve_slots[p_idx - start_idx] = (p_close_ms, _clean_zero(cash))

    liq_price = _compute_bench_liq_price(cash)

    # Monotonic timestamp-ordered timeline over bar opens and closes (CR4-07 / CR5-01 / CR5-02 / CR6-01 / CR6-02 / CR7-01..03).
    # On contiguous/gapped bars, pending_closes flushes bar i-1 immediately before bar i opens (zero drift).
    # On overlapping bars, gap-at-open liquidations execute at bar_open_ms while non-gap intrabar breaches
    # and close-time funding/MMR liquidation checks flush in ascending close_time_ms order on one live cash state.
    for i in range(start_idx, n):
        bar_open_ms = int(open_times[i])
        bar_close_ms = int(close_times[i])

        # 1a. Flush every prior bar close completing at or before this bar's open in ascending close_time_ms order.
        while pending_closes and pending_closes[0][0] <= bar_open_ms:
            p_close_ms, p_idx, p_open_liq = heappop(pending_closes)
            _flush_pending_close(p_close_ms, p_idx, p_open_liq)

        # 1b. Settle all funding events in (settled_until_ms, bar_open_ms] BEFORE bar i's open/gap-liquidation check.
        _settle_funding_up_to(bar_open_ms)

        bar_open = float(opens[i])
        bar_high = float(highs[i])
        bar_low = float(lows[i])
        bar_close = float(closes[i])
        bar_vol = float(volumes[i])
        bar_atr = float(atr_arr[i]) if atr_arr is not None else None

        # 2. Gap-at-open MMR liquidation check ONLY (CR1-52 / CR2-31 / CR7-03); non-gap intrabar
        #    liquidations are evaluated when bar i's close flushes so overlapping opens cannot preempt
        #    earlier bar closes or funding events.
        if is_open and liq_price is not None:
            gapped_liq = (bar_open <= liq_price) if side == "long" else (bar_open >= liq_price)
            if gapped_liq:
                _liquidate_bench(
                    exit_bar_idx=i,
                    exit_time_ms=bar_open_ms,
                    liq_threshold=liq_price,
                    gapped_open=True,
                    use_prior_open_slip=True,
                    b_open=bar_open,
                    b_high=bar_high,
                    b_low=bar_low,
                    b_close=bar_close,
                    b_vol=bar_vol,
                    b_atr=bar_atr,
                )

        # 3. Enqueue bar i's close timestamp & open-time liq threshold for chronological close processing.
        heappush(pending_closes, (bar_close_ms, i, liq_price))

    # 4. Flush all remaining bar closes in ascending close_time_ms order.
    while pending_closes:
        p_close_ms, p_idx, p_open_liq = heappop(pending_closes)
        _flush_pending_close(p_close_ms, p_idx, p_open_liq)

    equity_curve: list[tuple[int, float]] = [pt for pt in equity_curve_slots if pt is not None]

    if is_open:
        bars_in_market += max(0, n - entry_bar_idx)
        final_close = float(closes[-1])
        final_close_ms = int(close_times[-1])
        if close_on_end:
            if execution is not None:
                eod_quote = execution.fill_market(
                    close_side,
                    qty_abs,
                    bar_open=final_close,
                    bar_high=float(highs[-1]),
                    bar_low=float(lows[-1]),
                    bar_close=final_close,
                    bar_volume=float(volumes[-1]),
                    bar_atr=(float(atr_arr[-1]) if atr_arr is not None else None),
                    reason="end_of_data",
                )
                ref_exit = eod_quote.ref_price
                exec_exit = eod_quote.exec_price
                exit_notional = eod_quote.notional
                exit_fee = eod_quote.fee
                exit_slippage = eod_quote.slippage_cost
            else:
                ref_exit = final_close
                exec_exit = final_close * (1.0 - side_sign * spread_rate)
                if exec_exit <= 0.0:
                    exec_exit = final_close
                exit_notional = exec_exit * qty_abs
                exit_fee = exit_notional * fee_rate
                exit_slippage = (exec_exit - ref_exit) * (-signed_qty)
            gross_pnl = signed_qty * (ref_exit - ref_entry)
            total_fees = entry_fee + exit_fee
            total_slip = entry_slippage + exit_slippage
            net_pnl = fsum((gross_pnl, -total_fees, -total_slip, accrued_funding))
            ending_equity = starting_equity + net_pnl
            total_traded_notional += exit_notional
            trades.append(
                TradeRecord(
                    entry_time_ms=int(open_times[start_idx]),
                    exit_time_ms=final_close_ms,
                    side=side,
                    entry_price=exec_entry,
                    exit_price=exec_exit,
                    qty=qty_abs,
                    gross_pnl=gross_pnl,
                    fees=total_fees,
                    slippage=total_slip,
                    funding_pnl=accrued_funding,
                    net_pnl=net_pnl,
                    return_pct=net_pnl / entry_notional if entry_notional > 0.0 else 0.0,
                    reason="end_of_data",
                    symbol=resolved_symbol,
                    ref_entry_price=ref_entry,
                    ref_exit_price=ref_exit,
                )
            )
            equity_curve[-1] = (final_close_ms, _clean_zero(ending_equity))
            attribution = CostAttribution.from_components(
                gross_pnl=gross_pnl,
                fees=total_fees,
                slippage=total_slip,
                funding_pnl=accrued_funding,
            )
        else:
            gross_pnl = signed_qty * (final_close - ref_entry)
            ending_equity = equity_curve[-1][1]
            attribution = CostAttribution.from_components(
                gross_pnl=gross_pnl,
                fees=entry_fee,
                slippage=entry_slippage,
                funding_pnl=accrued_funding,
            )
    else:
        ending_equity = cash
        attribution = CostAttribution.from_components(
            gross_pnl=fsum(t.gross_pnl for t in trades),
            fees=fsum(t.fees for t in trades),
            slippage=fsum(t.slippage for t in trades),
            funding_pnl=fsum(t.funding_pnl for t in trades),
        )

    return compute_simulation_metrics(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        equity_curve=equity_curve,
        trades=trades,
        attribution=attribution,
        total_traded_notional=total_traded_notional,
        bars_in_market=bars_in_market,
        bars_scored=len(equity_curve),
        bars_processed=n,
        liquidations=liquidations,
        interval_ms=resolved_interval_ms,
        symbol=resolved_symbol,
        interval=resolved_interval,
        strategy_id=f"buy_and_hold_{side}",
        funding_mode=funding_mode,
        diagnostics=bench_diagnostics,
    )
