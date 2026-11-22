"""Deterministic two-phase Layer 3 backtest simulator with funding, fills, liquidation, and overlays."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import fsum, inf as _INF, isfinite
from types import MappingProxyType
from typing import Any, Literal

from mu_strategy.core.instrument import AtrBps, CostModel, InstrumentSpec
from mu_strategy.engine.fills import ExecutionModel, FillQuote
from mu_strategy.engine.metrics import (
    CostAttribution,
    SimulationResult,
    TradeRecord,
    _finalize_curve_stats,
    compute_simulation_metrics,
)
from mu_strategy.engine.risk_overlay import RiskOverlay, apply_risk_overlays
from mu_strategy.market_data.bar_series import BarSeries, BarSeriesSlice, MarketView
from mu_strategy.market_data.funding import (
    FundingSeries,
    MissingFundingHistoryError,
    format_apr_scenario_key,
    require_or_sensitivity_series,
)
from mu_strategy.strategies.features import FeatureCache
from mu_strategy.strategies.protocol import (
    EMPTY_OUTPUT,
    BarContext,
    OrderIntent,
    PortfolioView,
    PositionView,
    Strategy,
    StrategyOutput,
    TargetPosition,
)

__all__ = [
    "FundingSensitivityResult",
    "Simulator",
    "SimulatorConfig",
]

_EMPTY_POSITIONS: Mapping[str, PositionView] = MappingProxyType({})
_EMPTY_FUNDING_PROXY: Mapping[str, FundingSeries] = MappingProxyType({})
_EMPTY_ORDERS: tuple[OrderIntent, ...] = ()
_MS_PER_DAY: int = 86_400_000
_NEG_INF: float = -_INF

# C-level slot descriptors for fast allocation of validated immutable Layer 1/2 views.
_new_slice = BarSeriesSlice.__new__
_set_slice_series = BarSeriesSlice._series.__set__
_set_slice_start = BarSeriesSlice._start.__set__
_set_slice_end = BarSeriesSlice._end.__set__

_new_pos_view = PositionView.__new__
_set_pv_symbol = PositionView.symbol.__set__
_set_pv_qty = PositionView.qty.__set__
_set_pv_avg = PositionView.avg_entry_price.__set__
_set_pv_upnl = PositionView.unrealized_pnl.__set__
_set_pv_notional = PositionView.notional.__set__
_set_pv_lev = PositionView.leverage.__set__
_set_pv_liq = PositionView.liquidation_price.__set__
_set_pv_stage = PositionView.stage.__set__

_new_pf_view = PortfolioView.__new__
_set_pf_eq = PortfolioView.equity.__set__
_set_pf_cash = PortfolioView.cash.__set__
_set_pf_pos = PortfolioView.positions.__set__
_set_pf_ord = PortfolioView.open_orders.__set__
_set_pf_peak = PortfolioView.peak_equity.__set__
_set_pf_daily = PortfolioView.daily_start_equity.__set__

_new_ctx = BarContext.__new__
_set_ctx_now = BarContext.now_ms.__set__
_set_ctx_sym = BarContext.symbol.__set__
_set_ctx_mkt = BarContext.market.__set__
_set_ctx_pf = BarContext.portfolio.__set__
_set_ctx_ins = BarContext.instrument.__set__
_set_ctx_rtt = BarContext.require_touch_through.__set__


def _clean_zero(value: float) -> float:
    return 0.0 if value == 0.0 else float(value)


def _fast_slice(series: BarSeries, end: int) -> BarSeriesSlice:
    sl = _new_slice(BarSeriesSlice)
    _set_slice_series(sl, series)
    _set_slice_start(sl, 0)
    _set_slice_end(sl, end)
    return sl


def _fast_position_view(
    symbol: str,
    qty: float,
    avg_entry_price: float,
    unrealized_pnl: float,
    notional: float,
    leverage: float,
    liquidation_price: float | None,
    stage: int,
) -> PositionView:
    pv = _new_pos_view(PositionView)
    _set_pv_symbol(pv, symbol)
    _set_pv_qty(pv, 0.0 if qty == 0.0 else qty)
    _set_pv_avg(pv, 0.0 if avg_entry_price == 0.0 else avg_entry_price)
    _set_pv_upnl(pv, 0.0 if unrealized_pnl == 0.0 else unrealized_pnl)
    _set_pv_notional(pv, 0.0 if notional == 0.0 else notional)
    _set_pv_lev(pv, 0.0 if leverage == 0.0 else leverage)
    _set_pv_liq(pv, liquidation_price)
    _set_pv_stage(pv, stage)
    return pv


def _fast_portfolio_view(
    equity: float,
    cash: float,
    positions: Mapping[str, PositionView],
    open_orders: tuple[OrderIntent, ...],
    peak_equity: float,
    daily_start_equity: float,
) -> PortfolioView:
    pf = _new_pf_view(PortfolioView)
    _set_pf_eq(pf, 0.0 if equity == 0.0 else equity)
    _set_pf_cash(pf, 0.0 if cash == 0.0 else cash)
    _set_pf_pos(pf, positions)
    _set_pf_ord(pf, open_orders)
    _set_pf_peak(pf, 0.0 if peak_equity == 0.0 else peak_equity)
    _set_pf_daily(pf, 0.0 if daily_start_equity == 0.0 else daily_start_equity)
    return pf


def _fast_bar_context(
    now_ms: int,
    symbol: str,
    market: MarketView,
    portfolio: PortfolioView,
    instrument: InstrumentSpec,
    require_touch_through: bool = False,
) -> BarContext:
    ctx = _new_ctx(BarContext)
    _set_ctx_now(ctx, now_ms)
    _set_ctx_sym(ctx, symbol)
    _set_ctx_mkt(ctx, market)
    _set_ctx_pf(ctx, portfolio)
    _set_ctx_ins(ctx, instrument)
    _set_ctx_rtt(ctx, require_touch_through)
    return ctx


class _LivePositionView(PositionView):
    """Callback-scoped PositionView reading live state from ``_SimAccount``."""

    __slots__ = ("_acct",)

    def __init__(self, acct: Any, symbol: str) -> None:
        _set_pv_symbol(self, symbol)
        object.__setattr__(self, "_acct", acct)

    def _require_active(self) -> Any:
        acct = self._acct
        if not acct.callback_active:
            raise RuntimeError(
                "Ephemeral PositionView is only valid during Strategy.on_bar(ctx); "
                "do not retain ephemeral views across bars."
            )
        return acct

    @property
    def qty(self) -> float:
        q = self._require_active().pos_qty
        return 0.0 if q == 0.0 else q

    @property
    def avg_entry_price(self) -> float:
        return self._require_active().avg_exec_price

    @property
    def liquidation_price(self) -> float | None:
        return self._require_active().cached_liq_price

    @property
    def stage(self) -> int:
        return self._require_active().pos_stage

    @property
    def unrealized_pnl(self) -> float:
        acct = self._require_active()
        q = acct.pos_qty
        if q == 0.0:
            return 0.0
        u = (acct.cash_minus_cost + (q * acct.closes[acct.bar_idx])) - acct.cash
        return 0.0 if u == 0.0 else u

    @property
    def notional(self) -> float:
        acct = self._require_active()
        q = acct.pos_qty
        return (q if q > 0.0 else -q) * acct.closes[acct.bar_idx]

    @property
    def leverage(self) -> float:
        acct = self._require_active()
        q = acct.pos_qty
        if q == 0.0:
            return 0.0
        c = acct.closes[acct.bar_idx]
        eq = acct.cash_minus_cost + (q * c)
        if eq <= 0.0:
            return 0.0
        return ((q if q > 0.0 else -q) * c) / eq


class _LivePortfolioView(PortfolioView):
    """Callback-scoped PortfolioView reading live state from ``_SimAccount``."""

    __slots__ = ("_acct", "_sym", "_pos_view", "_pos_map")

    def __init__(self, acct: Any, symbol: str, pos_view: _LivePositionView) -> None:
        pos_map = MappingProxyType({symbol: pos_view})
        object.__setattr__(self, "_acct", acct)
        object.__setattr__(self, "_sym", symbol)
        object.__setattr__(self, "_pos_view", pos_view)
        object.__setattr__(self, "_pos_map", pos_map)

    def _require_active(self) -> Any:
        acct = self._acct
        if not acct.callback_active:
            raise RuntimeError(
                "Ephemeral PortfolioView is only valid during Strategy.on_bar(ctx); "
                "do not retain ephemeral views across bars."
            )
        return acct

    @property
    def equity(self) -> float:
        acct = self._require_active()
        q = acct.pos_qty
        if q == 0.0:
            e = acct.cash
        else:
            e = acct.cash_minus_cost + (q * acct.closes[acct.bar_idx])
        return 0.0 if e == 0.0 else e

    @property
    def cash(self) -> float:
        c = self._require_active().cash
        return 0.0 if c == 0.0 else c

    @property
    def positions(self) -> Mapping[str, PositionView]:
        return self._pos_map if self._require_active().pos_qty != 0.0 else _EMPTY_POSITIONS

    @property
    def open_orders(self) -> tuple[OrderIntent, ...]:
        acct = self._require_active()
        pe = acct.pending_entries
        pr = acct.pending_reduce_orders
        if not pe and not pr:
            return _EMPTY_ORDERS
        return tuple(qo.intent for qo in pe) + tuple(qo.intent for qo in pr)

    @property
    def peak_equity(self) -> float:
        p = self._require_active().peak_equity
        return 0.0 if p == 0.0 else p

    @property
    def daily_start_equity(self) -> float:
        acct = self._require_active()
        if acct.lazy_daily:
            idx = acct.bar_idx
            if idx < acct.start_idx:
                d = acct.starting_equity
            else:
                curr_ms = int(acct.close_times[idx])
                day_start_ms = (curr_ms // _MS_PER_DAY) * _MS_PER_DAY
                day_bar = bisect_left(acct.close_times, day_start_ms, acct.start_idx, idx + 1)
                if day_bar <= acct.start_idx:
                    d = acct.starting_equity
                else:
                    # Equity at the day boundary BEFORE the opening bar's PnL (CR1-30).
                    d = acct.eq_values[day_bar - 1 - acct.start_idx]
        else:
            d = acct.daily_start_equity
        return 0.0 if d == 0.0 else d

    def position(self, symbol: str) -> PositionView | None:
        if symbol == self._sym and self._require_active().pos_qty != 0.0:
            return self._pos_view
        return None

    def position_qty(self, symbol: str) -> float:
        acct = self._require_active()
        if symbol == self._sym:
            return acct.pos_qty
        return 0.0


class _LiveMarketView(MarketView):
    """Callback-scoped strictly causal MarketView returning immutable fixed-bound slices."""

    __slots__ = ("_acct", "_p_sym", "_p_iv", "_primary_series")

    def __init__(
        self,
        acct: Any,
        bars_proxy: Mapping[tuple[str, str], BarSeries],
        funding_proxy: Mapping[str, FundingSeries],
        features: Any,
        primary_key: tuple[str, str],
        primary_series: BarSeries,
    ) -> None:
        object.__setattr__(self, "_bars", bars_proxy)
        object.__setattr__(self, "_funding", funding_proxy)
        object.__setattr__(self, "_features", features)
        object.__setattr__(self, "_now_ms", 0)
        object.__setattr__(self, "_acct", acct)
        object.__setattr__(self, "_p_sym", primary_key[0])
        object.__setattr__(self, "_p_iv", primary_key[1])
        object.__setattr__(self, "_primary_series", primary_series)

    def _require_active(self) -> Any:
        acct = self._acct
        if not acct.callback_active:
            raise RuntimeError(
                "Ephemeral MarketView is only valid during Strategy.on_bar(ctx); "
                "do not retain ephemeral views across bars."
            )
        return acct

    @property
    def now_ms(self) -> int:
        acct = self._require_active()
        return acct.close_times[acct.bar_idx]

    def closed_bars(self, symbol: str, interval: str) -> BarSeriesSlice:
        acct = self._require_active()
        if symbol == self._p_sym and interval == self._p_iv:
            # Always allocate a fresh immutable BarSeriesSlice tied permanently to bar_idx (CR1-49).
            return _fast_slice(self._primary_series, acct.bar_idx + 1)
        series = self._bars[(symbol, interval)]
        return _fast_slice(series, series.closed_count_at(acct.close_times[acct.bar_idx]))

    def latest_closed_bar(self, symbol: str, interval: str):
        acct = self._require_active()
        if symbol == self._p_sym and interval == self._p_iv:
            idx = acct.bar_idx
            if idx < 0:
                return None
            return self._primary_series.bar_at(idx)
        series = self._bars[(symbol, interval)]
        end = series.closed_count_at(acct.close_times[acct.bar_idx])
        if end <= 0:
            return None
        return series.bar_at(end - 1)

    def funding_series(self, symbol: str) -> FundingSeries | None:
        now = self.now_ms
        series = self._funding.get(symbol)
        if series is None:
            return None
        hi = bisect_right(series.timestamps_ms, now)
        if hi == len(series):
            return series
        return FundingSeries(
            instrument_id=series.instrument_id,
            timestamps_ms=series.timestamps_ms[:hi],
            rates=series.rates[:hi],
            settlement_interval_hours=series.settlement_interval_hours,
        )

    def funding_between(self, symbol: str, start_ms: int, end_ms: int):
        now = self.now_ms
        series = self._funding.get(symbol)
        if series is None:
            return ()
        causal_end = end_ms if end_ms <= now else now
        return series.between(start_ms, causal_end)

    def __repr__(self) -> str:
        acct = self._acct
        idx = acct.bar_idx
        now = acct.close_times[idx] if (acct.callback_active and 0 <= idx < len(acct.close_times)) else 0
        return f"MarketView(now_ms={now}, series={len(self._bars)})"


class _LiveBarContext(BarContext):
    """Callback-scoped BarContext subclass for explicit opt-in ephemeral strategies."""

    __slots__ = ("_acct",)

    def __init__(
        self,
        acct: Any,
        symbol: str,
        market: MarketView,
        portfolio: PortfolioView,
        instrument: InstrumentSpec,
    ) -> None:
        _set_ctx_now(self, 0)
        _set_ctx_sym(self, symbol)
        _set_ctx_mkt(self, market)
        _set_ctx_pf(self, portfolio)
        _set_ctx_ins(self, instrument)
        _set_ctx_rtt(self, acct.execution.require_touch_through)
        object.__setattr__(self, "_acct", acct)

    @property
    def now_ms(self) -> int:
        acct = self._acct
        if not acct.callback_active:
            raise RuntimeError(
                "Ephemeral BarContext is only valid during Strategy.on_bar(ctx); "
                "do not retain ephemeral contexts across bars."
            )
        return acct.close_times[acct.bar_idx]


class _SimMarketView(MarketView):
    """Immutable per-bar O(1) MarketView used when ``reuse_bar_context=False``."""

    __slots__ = ("_primary_key", "_primary_series", "_primary_end")

    def closed_bars(self, symbol: str, interval: str) -> BarSeriesSlice:
        if (symbol, interval) == self._primary_key:
            return _fast_slice(self._primary_series, self._primary_end)
        series = self._bars[(symbol, interval)]
        return _fast_slice(series, series.closed_count_at(self._now_ms))

    def latest_closed_bar(self, symbol: str, interval: str):
        if (symbol, interval) == self._primary_key:
            end = self._primary_end
            if end <= 0:
                return None
            return self._primary_series.bar_at(end - 1)
        return MarketView.latest_closed_bar(self, symbol, interval)


_new_sim_mv = _SimMarketView.__new__
_set_mv_bars = MarketView._bars.__set__
_set_mv_funding = MarketView._funding.__set__
_set_mv_features = MarketView._features.__set__
_set_mv_now = MarketView._now_ms.__set__
_set_mv_pkey = _SimMarketView._primary_key.__set__
_set_mv_pseries = _SimMarketView._primary_series.__set__
_set_mv_pend = _SimMarketView._primary_end.__set__


@dataclass(frozen=True, slots=True)
class SimulatorConfig:
    """Configuration governing deterministic two-phase simulation behavior.

    ``reuse_bar_context`` controls the ``BarContext`` lifetime contract:
      - ``None`` (default): pass stable immutable snapshots to standard strategies;
        use callback-scoped ephemeral context reuse only when
        ``getattr(strategy, "supports_ephemeral_context", False)`` is ``True``.
      - ``False``: always pass stable immutable per-bar snapshots.
      - ``True``: requires ``strategy.supports_ephemeral_context = True`` and
        raises ``ValueError`` for unmarked strategies so the default snapshot
        retention guarantee is never silently broken.
    """

    starting_equity: float = 10_000.0
    primary_interval: str | None = None
    warmup_bars: int | None = None
    close_on_end: bool = True
    require_touch_through: bool = False
    sizing_basis: Literal["marked_equity", "settled_equity"] = "marked_equity"
    target_rebalance_mode: Literal["on_change", "continuous"] = "on_change"
    allow_missing_funding_for_perp: bool = False
    funding_gap_policy: Literal["fail_closed", "allow_partial"] = "fail_closed"
    funding_gap_tolerance: float = 1.5
    enforce_instrument_max_leverage: bool = True
    rebalance_deadband_weight: float = 1e-6
    atr_period: int = 14
    reuse_bar_context: bool | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.starting_equity, bool)
            or not isinstance(self.starting_equity, (int, float))
            or not isfinite(self.starting_equity)
            or self.starting_equity <= 0.0
        ):
            raise ValueError(f"starting_equity must be positive finite, got {self.starting_equity!r}")
        if self.warmup_bars is not None:
            if isinstance(self.warmup_bars, bool) or not isinstance(self.warmup_bars, int) or self.warmup_bars < 0:
                raise ValueError(f"warmup_bars must be a non-negative int or None, got {self.warmup_bars!r}")
        if self.sizing_basis not in ("marked_equity", "settled_equity"):
            raise ValueError(f"unsupported sizing_basis: {self.sizing_basis!r}")
        if self.target_rebalance_mode not in ("on_change", "continuous"):
            raise ValueError(f"unsupported target_rebalance_mode: {self.target_rebalance_mode!r}")
        if self.funding_gap_policy not in ("fail_closed", "allow_partial"):
            raise ValueError(f"unsupported funding_gap_policy: {self.funding_gap_policy!r}")
        if (
            isinstance(self.funding_gap_tolerance, bool)
            or not isinstance(self.funding_gap_tolerance, (int, float))
            or not isfinite(self.funding_gap_tolerance)
            or self.funding_gap_tolerance < 1.0
        ):
            raise ValueError("funding_gap_tolerance must be finite and >= 1.0")
        if self.rebalance_deadband_weight < 0.0 or not isfinite(self.rebalance_deadband_weight):
            raise ValueError("rebalance_deadband_weight must be non-negative finite")
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int) or self.atr_period < 1:
            raise ValueError("atr_period must be an integer >= 1")
        if self.reuse_bar_context is not None and not isinstance(self.reuse_bar_context, bool):
            raise ValueError(f"reuse_bar_context must be bool or None, got {self.reuse_bar_context!r}")


@dataclass(frozen=True, slots=True)
class FundingSensitivityResult:
    """Result bundle from ``Simulator.run_with_funding_sensitivity``."""

    mode: Literal["historical", "sensitivity_band", "historical_gap_filled"]
    results: Mapping[str, SimulationResult]
    apr_band: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.results, MappingProxyType):
            object.__setattr__(self, "results", MappingProxyType(dict(self.results)))

    @property
    def base_result(self) -> SimulationResult:
        return next(iter(self.results.values()))

    @property
    def worst_net_pnl_result(self) -> SimulationResult:
        return min(self.results.values(), key=lambda r: r.attribution.net_pnl)

    @property
    def best_net_pnl_result(self) -> SimulationResult:
        return max(self.results.values(), key=lambda r: r.attribution.net_pnl)


@dataclass(slots=True)
class _QueuedOrder:
    intent: OrderIntent
    bars_left: int | None
    created_while_open: bool
    activated: bool = False
    activated_bar: int = -1
    activated_at_open: bool = False


class _SimAccount:
    """Module-level cold-path account/order engine keeping Simulator.run free of closures."""

    __slots__ = (
        "symbol",
        "instrument",
        "execution",
        "cost_model",
        "contract_value",
        "max_leverage",
        "enforce_max_lev",
        "sizing_settled",
        "rebalance_on_change",
        "deadband_w",
        "is_spot_or_eq",
        "one_minus_mmr",
        "one_plus_mmr",
        "bar_idx",
        "callback_active",
        "overlays",
        "session_overlays",
        "starting_equity",
        "start_idx",
        "eq_values",
        "lazy_daily",
        "peak_equity",
        "daily_start_equity",
        "bars_proxy",
        "funding_proxy",
        "cache",
        "primary_key",
        "primary",
        "close_times",
        "closes",
        "cached_flat_pv",
        "cached_flat_key",
        "cash",
        "settled_equity",
        "cash_minus_cost",
        "pos_qty",
        "avg_exec_price",
        "avg_ref_price",
        "open_entry_fees",
        "open_entry_slippage",
        "open_funding_pnl",
        "pos_entry_time_ms",
        "pos_entry_notional",
        "pos_stage",
        "pos_open_bar",
        "cached_liq_price",
        "last_applied_target_weight",
        "active_stop_price",
        "active_take_profit",
        "active_stop_reason",
        "active_tp_reason",
        "pending_target",
        "pending_entries",
        "pending_reduce_orders",
        "realized_gross_pnl",
        "realized_fees",
        "realized_slippage",
        "realized_funding_pnl",
        "total_traded_notional",
        "liquidations",
        "bars_in_market",
        "trades",
    )

    def __init__(
        self,
        *,
        symbol: str,
        instrument: InstrumentSpec,
        execution: ExecutionModel,
        cost_model: CostModel,
        contract_value: float,
        max_leverage: float,
        enforce_max_lev: bool,
        sizing_settled: bool,
        rebalance_on_change: bool,
        deadband_w: float,
        is_spot_or_eq: bool,
        one_minus_mmr: float,
        one_plus_mmr: float,
        starting_equity: float,
        bars_proxy: Mapping[tuple[str, str], BarSeries],
        funding_proxy: Mapping[str, FundingSeries],
        cache: FeatureCache,
        primary_key: tuple[str, str],
        primary: BarSeries,
        close_times: Any,
        closes: Any,
        overlays: Sequence[RiskOverlay] = (),
    ) -> None:
        self.symbol = symbol
        self.instrument = instrument
        self.execution = execution
        self.cost_model = cost_model
        self.contract_value = contract_value
        self.max_leverage = max_leverage
        self.enforce_max_lev = enforce_max_lev
        self.sizing_settled = sizing_settled
        self.rebalance_on_change = rebalance_on_change
        self.deadband_w = deadband_w
        self.is_spot_or_eq = is_spot_or_eq
        self.one_minus_mmr = one_minus_mmr
        self.one_plus_mmr = one_plus_mmr
        self.bar_idx = -1
        self.callback_active = False
        self.overlays: tuple[RiskOverlay, ...] = tuple(overlays)
        self.session_overlays: tuple[Any, ...] = tuple(
            ov for ov in self.overlays if hasattr(ov, "is_allowed")
        )
        self.starting_equity = starting_equity
        self.start_idx = 0
        self.eq_values: list[float] = []
        self.lazy_daily = False
        self.peak_equity = starting_equity
        self.daily_start_equity = starting_equity
        self.bars_proxy = bars_proxy
        self.funding_proxy = funding_proxy
        self.cache = cache
        self.primary_key = primary_key
        self.primary = primary
        self.close_times = close_times
        self.closes = closes
        self.cached_flat_pv: PortfolioView | None = None
        self.cached_flat_key: tuple[float, float, float, float] | None = None

        self.cash = starting_equity
        self.settled_equity = starting_equity
        self.cash_minus_cost = starting_equity
        self.pos_qty = 0.0
        self.avg_exec_price = 0.0
        self.avg_ref_price = 0.0
        self.open_entry_fees = 0.0
        self.open_entry_slippage = 0.0
        self.open_funding_pnl = 0.0
        self.pos_entry_time_ms = 0
        self.pos_entry_notional = 0.0
        self.pos_stage = 0
        self.pos_open_bar = -1
        self.cached_liq_price: float | None = None
        self.last_applied_target_weight: float | None = None
        self.active_stop_price: float | None = None
        self.active_take_profit: float | None = None
        self.active_stop_reason = "stop_loss"
        self.active_tp_reason = "take_profit"
        self.pending_target: TargetPosition | None = None
        self.pending_entries: list[_QueuedOrder] = []
        self.pending_reduce_orders: list[_QueuedOrder] = []

        self.realized_gross_pnl = 0.0
        self.realized_fees = 0.0
        self.realized_slippage = 0.0
        self.realized_funding_pnl = 0.0
        self.total_traded_notional = 0.0
        self.liquidations = 0
        self.bars_in_market = 0
        self.trades: list[TradeRecord] = []

    def recompute_liq_price(self) -> float | None:
        q = self.pos_qty
        avg = self.avg_exec_price
        if q == 0.0 or avg <= 0.0:
            return None
        if q > 0.0:
            if self.is_spot_or_eq:
                return None
            num = (q * avg) - self.cash
            if num <= 0.0:
                return None
            px = num / (q * self.one_minus_mmr)
            return px if px > 0.0 else None
        q_abs = -q
        num = (q_abs * avg) + self.cash
        if num <= 0.0:
            return 1e-12
        return num / (q_abs * self.one_plus_mmr)

    def sync_live_position(self) -> None:
        self.cash_minus_cost = self.cash - (self.pos_qty * self.avg_exec_price)

    def compute_triggers(self) -> tuple[float, float, bool]:
        q = self.pos_qty
        if q == 0.0:
            return _NEG_INF, _INF, False
        lt = _NEG_INF
        ht = _INF
        liq = self.cached_liq_price
        stp = self.active_stop_price
        tp = self.active_take_profit
        if q > 0.0:
            if liq is not None and liq > lt:
                lt = liq
            if stp is not None and stp > lt:
                lt = stp
            if tp is not None and tp < ht:
                ht = tp
        else:
            if liq is not None and liq < ht:
                ht = liq
            if stp is not None and stp < ht:
                ht = stp
            if tp is not None and tp > lt:
                lt = tp
        return lt, ht, (lt > _NEG_INF) or (ht < _INF)

    def apply_fill(
        self,
        quote: FillQuote,
        fill_time_ms: int,
        bar_idx: int = -1,
        *,
        reason_override: str | None = None,
    ) -> bool:
        """Apply FillQuote to position/cash/attribution; return True if fresh entry opened."""
        qty_abs = quote.qty
        if qty_abs <= 0.0:
            return False

        signed_delta = qty_abs if quote.side == "buy" else -qty_abs
        self.total_traded_notional += quote.notional
        reason = reason_override if reason_override is not None else quote.reason
        q = self.pos_qty

        # Case 1: Fresh open from flat.
        if q == 0.0:
            self.pos_qty = signed_delta
            self.avg_exec_price = quote.exec_price
            self.avg_ref_price = quote.ref_price
            self.open_entry_fees = quote.fee
            self.open_entry_slippage = quote.slippage_cost
            self.open_funding_pnl = 0.0
            self.pos_entry_time_ms = fill_time_ms
            self.pos_entry_notional = quote.notional
            self.pos_stage = 1
            self.pos_open_bar = bar_idx
            self.cash -= quote.fee
            self.cached_liq_price = self.recompute_liq_price()
            self.sync_live_position()
            return True

        # Case 2: Same-side add / pyramid.
        if (q > 0.0 and signed_delta > 0.0) or (q < 0.0 and signed_delta < 0.0):
            old_abs = q if q > 0.0 else -q
            new_abs = old_abs + qty_abs
            self.avg_exec_price = ((old_abs * self.avg_exec_price) + (qty_abs * quote.exec_price)) / new_abs
            self.avg_ref_price = ((old_abs * self.avg_ref_price) + (qty_abs * quote.ref_price)) / new_abs
            self.pos_qty = q + signed_delta
            self.open_entry_fees += quote.fee
            self.open_entry_slippage += quote.slippage_cost
            self.pos_entry_notional += quote.notional
            self.pos_stage += 1
            self.cash -= quote.fee
            self.cached_liq_price = self.recompute_liq_price()
            self.sync_live_position()
            return False

        # Case 3: Opposite-side reduce, full close, or flip.
        old_abs = q if q > 0.0 else -q
        pos_sign = 1.0 if q > 0.0 else -1.0
        pos_side: Literal["long", "short"] = "long" if q > 0.0 else "short"

        if qty_abs <= old_abs + 1e-12:
            is_full = qty_abs >= old_abs - 1e-12
            close_abs = old_abs if is_full else qty_abs
            frac = 1.0 if is_full else (close_abs / old_abs)

            slice_entry_fees = self.open_entry_fees if is_full else (self.open_entry_fees * frac)
            slice_entry_slip = self.open_entry_slippage if is_full else (self.open_entry_slippage * frac)
            slice_funding = self.open_funding_pnl if is_full else (self.open_funding_pnl * frac)
            slice_entry_notional = self.pos_entry_notional if is_full else (self.pos_entry_notional * frac)

            realized_exec_pnl = pos_sign * close_abs * (quote.exec_price - self.avg_exec_price)
            self.cash += realized_exec_pnl - quote.fee

            trade_gross = pos_sign * close_abs * (quote.ref_price - self.avg_ref_price)
            trade_fees = slice_entry_fees + quote.fee
            trade_slip = slice_entry_slip + quote.slippage_cost
            trade_funding = slice_funding
            trade_net = fsum((trade_gross, -trade_fees, -trade_slip, trade_funding))

            self.realized_gross_pnl += trade_gross
            self.realized_fees += trade_fees
            self.realized_slippage += trade_slip
            self.realized_funding_pnl += trade_funding
            self.settled_equity += trade_net

            ret_pct = (trade_net / slice_entry_notional) if slice_entry_notional > 0.0 else 0.0
            self.trades.append(
                TradeRecord._from_validated(
                    entry_time_ms=self.pos_entry_time_ms,
                    exit_time_ms=fill_time_ms,
                    side=pos_side,
                    entry_price=self.avg_exec_price,
                    exit_price=quote.exec_price,
                    qty=close_abs,
                    gross_pnl=trade_gross,
                    fees=trade_fees,
                    slippage=trade_slip,
                    funding_pnl=trade_funding,
                    net_pnl=trade_net,
                    return_pct=ret_pct,
                    reason=reason,
                    symbol=self.symbol,
                    max_stage=self.pos_stage,
                    ref_entry_price=self.avg_ref_price,
                    ref_exit_price=quote.ref_price,
                )
            )

            if is_full:
                if bar_idx >= 0 and self.pos_open_bar >= 0:
                    self.bars_in_market += bar_idx - self.pos_open_bar
                self.pos_open_bar = -1
                self.pos_qty = 0.0
                self.avg_exec_price = 0.0
                self.avg_ref_price = 0.0
                self.open_entry_fees = 0.0
                self.open_entry_slippage = 0.0
                self.open_funding_pnl = 0.0
                self.pos_entry_time_ms = 0
                self.pos_entry_notional = 0.0
                self.pos_stage = 0
                self.cached_liq_price = None
                self.last_applied_target_weight = None
                self.active_stop_price = None
                self.active_take_profit = None
                self.pending_reduce_orders.clear()
            else:
                self.pos_qty = pos_sign * (old_abs - close_abs)
                self.open_entry_fees -= slice_entry_fees
                self.open_entry_slippage -= slice_entry_slip
                self.open_funding_pnl -= slice_funding
                self.pos_entry_notional -= slice_entry_notional
                self.cached_liq_price = self.recompute_liq_price()
            self.sync_live_position()
            return False

        # Flip: split cleanly into close leg (old_abs) + fresh open leg (qty_abs - old_abs).
        if bar_idx >= 0 and self.pos_open_bar >= 0:
            self.bars_in_market += bar_idx - self.pos_open_bar
        self.pos_open_bar = bar_idx

        close_frac = old_abs / qty_abs
        close_fee = quote.fee * close_frac
        open_fee = quote.fee - close_fee
        close_slip = quote.slippage_cost * close_frac
        open_slip = quote.slippage_cost - close_slip

        realized_exec_pnl = pos_sign * old_abs * (quote.exec_price - self.avg_exec_price)
        self.cash += realized_exec_pnl - close_fee

        trade_gross = pos_sign * old_abs * (quote.ref_price - self.avg_ref_price)
        trade_fees = self.open_entry_fees + close_fee
        trade_slip = self.open_entry_slippage + close_slip
        trade_funding = self.open_funding_pnl
        trade_net = fsum((trade_gross, -trade_fees, -trade_slip, trade_funding))

        self.realized_gross_pnl += trade_gross
        self.realized_fees += trade_fees
        self.realized_slippage += trade_slip
        self.realized_funding_pnl += trade_funding
        self.settled_equity += trade_net

        ret_pct = (trade_net / self.pos_entry_notional) if self.pos_entry_notional > 0.0 else 0.0
        self.trades.append(
            TradeRecord._from_validated(
                entry_time_ms=self.pos_entry_time_ms,
                exit_time_ms=fill_time_ms,
                side=pos_side,
                entry_price=self.avg_exec_price,
                exit_price=quote.exec_price,
                qty=old_abs,
                gross_pnl=trade_gross,
                fees=trade_fees,
                slippage=trade_slip,
                funding_pnl=trade_funding,
                net_pnl=trade_net,
                return_pct=ret_pct,
                reason=reason or "flip",
                symbol=self.symbol,
                max_stage=self.pos_stage,
                ref_entry_price=self.avg_ref_price,
                ref_exit_price=quote.ref_price,
            )
        )
        self.pending_reduce_orders.clear()

        rem_abs = qty_abs - old_abs
        self.pos_qty = rem_abs if quote.side == "buy" else -rem_abs
        self.avg_exec_price = quote.exec_price
        self.avg_ref_price = quote.ref_price
        self.open_entry_fees = open_fee
        self.open_entry_slippage = open_slip
        self.open_funding_pnl = 0.0
        self.pos_entry_time_ms = fill_time_ms
        self.pos_entry_notional = rem_abs * quote.exec_price
        self.pos_stage = 1
        self.active_stop_price = None
        self.active_take_profit = None
        self.cash -= open_fee
        self.cached_liq_price = self.recompute_liq_price()
        self.sync_live_position()
        return True

    def is_halted_by_overlay(self) -> bool:
        for ov in self.overlays:
            if getattr(ov, "is_halting_entries", False):
                return True
        return False

    def is_entry_session_allowed(self, bar_open_ms: int, intent: OrderIntent | None = None) -> bool:
        if intent is not None:
            gate = getattr(intent, "session_gate", None)
            if gate is not None and not gate.is_allowed(bar_open_ms):
                return False
        for ov in self.session_overlays:
            if not ov.is_allowed(bar_open_ms):
                return False
        return True

    def should_cancel_entry_outside_session(self, bar_open_ms: int) -> bool:
        for ov in self.session_overlays:
            if getattr(ov, "flatten_outside_session", False) and not ov.is_allowed(bar_open_ms):
                return True
        return False

    def collect_protective_levels(
        self,
        for_qty: float,
        *,
        include_active_bracket: bool,
    ) -> tuple[float | None, str, float | None, str]:
        is_long = for_qty > 0.0
        close_side = "sell" if is_long else "buy"
        best_stop: float | None = self.active_stop_price if include_active_bracket else None
        best_stop_reason = self.active_stop_reason
        best_tp: float | None = self.active_take_profit if include_active_bracket else None
        best_tp_reason = self.active_tp_reason

        if self.pending_reduce_orders:
            for qo in self.pending_reduce_orders:
                intent = qo.intent
                if intent.side != close_side:
                    continue
                if intent.order_type == "stop_limit" and qo.activated and intent.limit_price is not None:
                    lp = intent.limit_price
                    if best_tp is None or (is_long and lp < best_tp) or ((not is_long) and lp > best_tp):
                        best_tp = lp
                        best_tp_reason = intent.tag or "stop_limit"
                elif intent.order_type in ("stop_market", "stop_limit") and intent.stop_price is not None:
                    sp = intent.stop_price
                    if best_stop is None or (is_long and sp > best_stop) or ((not is_long) and sp < best_stop):
                        best_stop = sp
                        best_stop_reason = intent.tag or "stop_loss"
                elif intent.order_type == "limit" and intent.limit_price is not None:
                    lp = intent.limit_price
                    if best_tp is None or (is_long and lp < best_tp) or ((not is_long) and lp > best_tp):
                        best_tp = lp
                        best_tp_reason = intent.tag or "take_profit"
        return best_stop, best_stop_reason, best_tp, best_tp_reason

    def size_intent_qty(
        self,
        intent: OrderIntent,
        ref_px: float,
        exec_px: float,
        eq_basis: float,
    ) -> float:
        is_reduce = intent.reduce_only or intent.qty_mode == "close_all"
        if is_reduce:
            pos_q = self.pos_qty
            if pos_q == 0.0:
                return 0.0
            if (pos_q > 0.0 and intent.side != "sell") or (pos_q < 0.0 and intent.side != "buy"):
                return 0.0

        mode = intent.qty_mode
        if mode == "contracts":
            qty_abs = intent.qty_value * self.contract_value
        elif mode == "close_all":
            qty_abs = abs(self.pos_qty)
        elif eq_basis <= 0.0 or exec_px <= 0.0:
            qty_abs = 0.0
        elif mode == "equity_fraction":
            notional = eq_basis * intent.qty_value
            if self.enforce_max_lev:
                cap = eq_basis * self.max_leverage
                if notional > cap:
                    notional = cap
            qty_abs = notional / exec_px if notional > 0.0 else 0.0
        else:
            w = intent.qty_value
            if self.enforce_max_lev:
                ml = self.max_leverage
                if w > ml:
                    w = ml
                elif w < -ml:
                    w = -ml
            desired_notional = w * eq_basis
            current_notional = self.pos_qty * ref_px
            delta = desired_notional - current_notional
            if (intent.side == "buy" and delta <= 0.0) or (intent.side == "sell" and delta >= 0.0):
                qty_abs = 0.0
            else:
                qty_abs = abs(delta) / exec_px
                is_same_side_reduce = (
                    (self.pos_qty > 0.0 and 0.0 < desired_notional < current_notional)
                    or (self.pos_qty < 0.0 and current_notional < desired_notional < 0.0)
                )
                if is_same_side_reduce:
                    pos_abs_s = abs(self.pos_qty)
                    if qty_abs >= pos_abs_s - 1e-12:
                        keep_qty = max(abs(desired_notional) / ref_px, 2e-12)
                        qty_abs = max(0.0, pos_abs_s - keep_qty)

        # Enforce reduce_only cap immediately against live position before every fill (CR1-47).
        if is_reduce and qty_abs > 0.0:
            pos_abs = self.pos_qty if self.pos_qty > 0.0 else -self.pos_qty
            if qty_abs > pos_abs:
                qty_abs = pos_abs
        return qty_abs

    def _try_execute_queued_order(
        self,
        intent: OrderIntent,
        *,
        qo: _QueuedOrder | None = None,
        bar_idx: int = -1,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None,
        pre_high: float,
        pre_low: float,
        pre_close: float,
        pre_volume: float,
        pre_atr: float | None,
        eq_basis: float,
    ) -> tuple[FillQuote | None, bool]:
        """Match an OrderIntent and return ``(FillQuote | None, filled_at_open)``."""
        execution = self.execution
        otype = intent.order_type
        side = intent.side
        reason = intent.tag or otype
        is_reduce = intent.reduce_only or intent.qty_mode == "close_all"
        excluded_bands = () if is_reduce else intent.excluded_price_bands

        if is_reduce:
            pos_q = self.pos_qty
            if pos_q == 0.0 or (pos_q > 0.0 and side != "sell") or (pos_q < 0.0 and side != "buy"):
                return None, False

        if otype == "market":
            if excluded_bands and any(b.excludes(bar_open) for b in excluded_bands):
                return None, False
            # Open fill: price strictly from pre-open data (CR1-40).
            est_notional = (
                (eq_basis * intent.qty_value)
                if intent.qty_mode == "equity_fraction"
                else (abs(intent.qty_value * eq_basis - self.pos_qty * bar_open))
                if intent.qty_mode == "target_weight"
                else (abs(self.pos_qty) * bar_open)
                if intent.qty_mode == "close_all"
                else (intent.qty_value * self.contract_value * bar_open)
            )
            if is_reduce:
                max_red_notional = abs(self.pos_qty) * bar_open
                if est_notional > max_red_notional:
                    est_notional = max_red_notional
            ref_px, exec_px = execution.market_exec_price(
                side,
                order_notional=max(0.0, est_notional),
                bar_open=bar_open,
                bar_high=pre_high,
                bar_low=pre_low,
                bar_close=pre_close,
                bar_volume=pre_volume,
                bar_atr=pre_atr,
            )
            qty_abs = self.size_intent_qty(intent, ref_px, exec_px, eq_basis)
            if qty_abs <= 1e-12:
                return None, False
            return (
                execution.fill_market(
                    side,
                    qty_abs,
                    bar_open=bar_open,
                    bar_high=pre_high,
                    bar_low=pre_low,
                    bar_close=pre_close,
                    bar_volume=pre_volume,
                    bar_atr=pre_atr,
                    reason=reason,
                ),
                True,
            )

        if otype == "limit":
            assert intent.limit_price is not None
            open_only = getattr(intent, "time_in_force", "gtc") == "open_only"
            fill_px = execution.limit_fill_price(
                side,
                intent.limit_price,
                bar_open=bar_open,
                bar_high=bar_high,
                bar_low=bar_low,
                open_only=open_only,
            )
            if fill_px is None:
                return None, False
            if excluded_bands and any(b.excludes(fill_px) for b in excluded_bands):
                return None, False
            qty_abs = self.size_intent_qty(intent, fill_px, fill_px, eq_basis)
            if qty_abs <= 1e-12:
                return None, False
            if open_only:
                at_open = True
            elif side == "buy":
                at_open = (bar_open < intent.limit_price) if execution.require_touch_through else (bar_open <= intent.limit_price)
            else:
                at_open = (bar_open > intent.limit_price) if execution.require_touch_through else (bar_open >= intent.limit_price)
            return (
                execution.try_fill_limit(
                    side,
                    qty_abs,
                    intent.limit_price,
                    bar_open=bar_open,
                    bar_high=bar_high,
                    bar_low=bar_low,
                    reason=reason,
                    open_only=open_only,
                ),
                at_open,
            )

        if otype == "stop_market":
            assert intent.stop_price is not None
            ref_px = execution.stop_trigger_ref_price(
                side,
                intent.stop_price,
                bar_open=bar_open,
                bar_high=bar_high,
                bar_low=bar_low,
            )
            if ref_px is None:
                return None, False
            if excluded_bands and any(b.excludes(ref_px) for b in excluded_bands):
                return None, False
            triggered_at_open = (bar_open >= intent.stop_price) if side == "buy" else (bar_open <= intent.stop_price)
            p_high = pre_high if triggered_at_open else bar_high
            p_low = pre_low if triggered_at_open else bar_low
            p_close = pre_close if triggered_at_open else bar_close
            p_vol = pre_volume if triggered_at_open else bar_volume
            p_atr = pre_atr if triggered_at_open else bar_atr
            est_notional = (
                (eq_basis * intent.qty_value)
                if intent.qty_mode == "equity_fraction"
                else (abs(self.pos_qty) * ref_px)
                if intent.qty_mode == "close_all"
                else (intent.qty_value * self.contract_value * ref_px)
            )
            if is_reduce:
                max_red_notional = abs(self.pos_qty) * ref_px
                if est_notional > max_red_notional:
                    est_notional = max_red_notional
            slip_px = execution.compute_slippage_price(
                ref_px,
                order_notional=max(0.0, est_notional),
                bar_close=p_close,
                bar_volume=p_vol,
                bar_atr=p_atr,
                bar_high=p_high,
                bar_low=p_low,
                is_stop=True,
            )
            adverse = (ref_px * execution._half_spread_rate) + slip_px
            est_exec = (ref_px + adverse) if side == "buy" else max(execution.min_price, ref_px - adverse)
            qty_abs = self.size_intent_qty(intent, ref_px, est_exec, eq_basis)
            if qty_abs <= 1e-12:
                return None, False
            if triggered_at_open:
                slip_exact = execution.compute_slippage_price(
                    ref_px,
                    order_notional=ref_px * qty_abs,
                    bar_close=pre_close,
                    bar_volume=pre_volume,
                    bar_atr=pre_atr,
                    bar_high=pre_high,
                    bar_low=pre_low,
                    is_stop=True,
                )
                adverse_exact = (ref_px * execution._half_spread_rate) + slip_exact
                exec_exact = (ref_px + adverse_exact) if side == "buy" else max(execution.min_price, ref_px - adverse_exact)
                return (
                    execution._make_quote(
                        side=side,
                        order_type="stop_market",
                        ref_price=ref_px,
                        exec_price=exec_exact,
                        qty=qty_abs,
                        fee_rate=execution._taker_fee_rate,
                        liquidity="taker",
                        reason=reason,
                    ),
                    True,
                )
            return (
                execution.try_fill_stop_market(
                    side,
                    qty_abs,
                    intent.stop_price,
                    bar_open=bar_open,
                    bar_high=bar_high,
                    bar_low=bar_low,
                    bar_close=bar_close,
                    bar_volume=bar_volume,
                    bar_atr=bar_atr,
                    reason=reason,
                ),
                False,
            )

        # order_type == "stop_limit" (CR1-42 / CR2-15 / CR2-18 / CR2-22).
        assert intent.stop_price is not None and intent.limit_price is not None
        is_activated = qo.activated if qo is not None else False
        if is_activated:
            fill_px = execution.limit_fill_price(
                side,
                intent.limit_price,
                bar_open=bar_open,
                bar_high=bar_high,
                bar_low=bar_low,
            )
            if fill_px is None:
                return None, False
            if excluded_bands and any(b.excludes(fill_px) for b in excluded_bands):
                return None, False
            qty_abs = self.size_intent_qty(intent, fill_px, fill_px, eq_basis)
            if qty_abs <= 1e-12:
                return None, False
            if side == "buy":
                at_open = (bar_open < intent.limit_price) if execution.require_touch_through else (bar_open <= intent.limit_price)
            else:
                at_open = (bar_open > intent.limit_price) if execution.require_touch_through else (bar_open >= intent.limit_price)
            return (
                execution.try_fill_limit(
                    side,
                    qty_abs,
                    intent.limit_price,
                    bar_open=bar_open,
                    bar_high=bar_high,
                    bar_low=bar_low,
                    reason=reason,
                ),
                at_open,
            )

        ref_px = execution.stop_trigger_ref_price(
            side,
            intent.stop_price,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
        )
        if ref_px is None:
            return None, False
        triggered_at_open = (bar_open >= intent.stop_price) if side == "buy" else (bar_open <= intent.stop_price)
        if triggered_at_open:
            within_limit = (bar_open <= intent.limit_price) if side == "buy" else (bar_open >= intent.limit_price)
            if within_limit:
                if excluded_bands and any(b.excludes(bar_open) for b in excluded_bands):
                    return None, False
                if qo is not None:
                    qo.activated = True
                    qo.activated_bar = bar_idx
                    qo.activated_at_open = True
                qty_abs = self.size_intent_qty(intent, bar_open, bar_open, eq_basis)
                if qty_abs <= 1e-12:
                    return None, False
                # CR2-22: Keep ATR range fallback strictly causal using prior-bar OHLCV/ATR for opening fills.
                raw_exec = execution._adverse_exec_price(
                    side,
                    bar_open,
                    order_notional=bar_open * qty_abs,
                    bar_high=pre_high,
                    bar_low=pre_low,
                    bar_close=pre_close,
                    bar_volume=pre_volume,
                    bar_atr=pre_atr,
                    is_stop=True,
                )
                if side == "buy":
                    exec_px = raw_exec if raw_exec <= intent.limit_price else intent.limit_price
                else:
                    exec_px = raw_exec if raw_exec >= intent.limit_price else intent.limit_price
                return (
                    execution._make_quote(
                        side=side,
                        order_type="stop_limit",
                        ref_price=bar_open,
                        exec_price=exec_px,
                        qty=qty_abs,
                        fee_rate=execution._taker_fee_rate,
                        liquidity="taker",
                        reason=reason,
                    ),
                    True,
                )
            # Triggered at open past limit_price: activates as a passive resting limit for this bar.
            if qo is not None:
                qo.activated = True
                qo.activated_bar = bar_idx
                qo.activated_at_open = True
            if side == "buy":
                reached = (bar_low < intent.limit_price) if execution.require_touch_through else (bar_low <= intent.limit_price)
            else:
                reached = (bar_high > intent.limit_price) if execution.require_touch_through else (bar_high >= intent.limit_price)
            if not reached:
                return None, False
            if excluded_bands and any(b.excludes(intent.limit_price) for b in excluded_bands):
                return None, False
            qty_abs = self.size_intent_qty(intent, intent.limit_price, intent.limit_price, eq_basis)
            if qty_abs <= 1e-12:
                return None, False
            return (
                execution._make_quote(
                    side=side,
                    order_type="limit",
                    ref_price=intent.limit_price,
                    exec_price=intent.limit_price,
                    qty=qty_abs,
                    fee_rate=execution._maker_fee_rate,
                    liquidity="maker",
                    reason=reason,
                ),
                False,
            )

        # Triggered strictly intrabar at ref_px == intent.stop_price.
        within_limit = (ref_px <= intent.limit_price) if side == "buy" else (ref_px >= intent.limit_price)
        if within_limit and excluded_bands and any(b.excludes(ref_px) for b in excluded_bands):
            return None, False
        if qo is not None:
            qo.activated = True
            qo.activated_bar = bar_idx
            qo.activated_at_open = False
        if not within_limit:
            return None, False
        qty_abs = self.size_intent_qty(intent, ref_px, ref_px, eq_basis)
        if qty_abs <= 1e-12:
            return None, False
        quote, _ = execution.evaluate_stop_limit(
            side,
            qty_abs,
            intent.stop_price,
            intent.limit_price,
            activated=False,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_atr=bar_atr,
            reason=reason,
        )
        return quote, False

    def try_execute_intent(
        self,
        intent: OrderIntent,
        *,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None,
        eq_basis: float,
        qo: _QueuedOrder | None = None,
        pre_high: float | None = None,
        pre_low: float | None = None,
        pre_close: float | None = None,
        pre_volume: float | None = None,
        pre_atr: float | None = None,
    ) -> FillQuote | None:
        quote, _ = self._try_execute_queued_order(
            intent,
            qo=qo,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_atr=bar_atr,
            pre_high=bar_high if pre_high is None else pre_high,
            pre_low=bar_low if pre_low is None else pre_low,
            pre_close=bar_close if pre_close is None else pre_close,
            pre_volume=bar_volume if pre_volume is None else pre_volume,
            pre_atr=bar_atr if pre_atr is None else pre_atr,
            eq_basis=eq_basis,
        )
        return quote

    def process_protective_exits_with_orders(
        self,
        i: int,
        bar_open_ms: int,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        bar_atr: float | None,
        pre_high: float,
        pre_low: float,
        pre_close: float,
        pre_volume: float,
        pre_atr: float | None,
        *,
        include_active_bracket: bool = True,
        allow_liq: bool = True,
        allow_tp: bool = True,
        liq_bar_open: float | None = None,
        liq_bar_high: float | None = None,
        liq_bar_low: float | None = None,
        skip_stops_at_or_beyond_entry: bool = False,
    ) -> bool:
        """Resolve liquidation, bracket stops/TPs, and resting reduce-only orders accurately (CR1-44/45/53)."""
        if self.pos_qty == 0.0:
            return False

        execution = self.execution
        exited_any = False
        adverse_stop_filled = False
        liq_o = bar_open if liq_bar_open is None else liq_bar_open
        liq_h = bar_high if liq_bar_high is None else liq_bar_high
        liq_l = bar_low if liq_bar_low is None else liq_bar_low

        def _eq_basis() -> float:
            return (
                self.settled_equity
                if self.sizing_settled
                else (self.cash + self.pos_qty * (bar_open - self.avg_exec_price))
            )

        def _is_stop_valid_for_fresh(sp: float) -> bool:
            if not skip_stops_at_or_beyond_entry:
                return True
            if self.pos_qty > 0.0:
                return sp < self.avg_exec_price
            return sp > self.avg_exec_price

        # ---- Step A: Events marketable AT THE BAR OPEN (priority: liquidation > stop > TP) ----
        is_long = self.pos_qty > 0.0
        close_side: Literal["buy", "sell"] = "sell" if is_long else "buy"

        # A.1 Gap-at-open liquidation (pre-open pricing, CR1-40).
        if allow_liq and self.cached_liq_price is not None:
            liq_px = self.cached_liq_price
            if (is_long and liq_o <= liq_px) or ((not is_long) and liq_o >= liq_px):
                qty_l = abs(self.pos_qty)
                slip_l = execution.compute_slippage_price(
                    liq_o,
                    order_notional=liq_o * qty_l,
                    bar_close=pre_close,
                    bar_volume=pre_volume,
                    bar_atr=pre_atr,
                    bar_high=pre_high,
                    bar_low=pre_low,
                    is_stop=True,
                )
                adv_l = (liq_o * execution._half_spread_rate) + slip_l
                exec_l = (liq_o + adv_l) if close_side == "buy" else max(execution.min_price, liq_o - adv_l)
                lq = execution._make_quote(
                    side=close_side,
                    order_type="liquidation",
                    ref_price=liq_o,
                    exec_price=exec_l,
                    qty=qty_l,
                    fee_rate=execution._liq_total_fee_rate,
                    liquidity="taker",
                    reason="liquidation",
                )
                self.apply_fill(lq, bar_open_ms, i, reason_override="liquidation")
                self.liquidations += 1
                return True

        # A.2 Active bracket stop triggered AT OPEN (pre-open pricing, CR1-40).
        if include_active_bracket and self.active_stop_price is not None and _is_stop_valid_for_fresh(self.active_stop_price):
            sp = self.active_stop_price
            if (is_long and bar_open <= sp) or ((not is_long) and bar_open >= sp):
                qty_s = abs(self.pos_qty)
                slip_s = execution.compute_slippage_price(
                    bar_open,
                    order_notional=bar_open * qty_s,
                    bar_close=pre_close,
                    bar_volume=pre_volume,
                    bar_atr=pre_atr,
                    bar_high=pre_high,
                    bar_low=pre_low,
                    is_stop=True,
                )
                adv_s = (bar_open * execution._half_spread_rate) + slip_s
                exec_s = (bar_open + adv_s) if close_side == "buy" else max(execution.min_price, bar_open - adv_s)
                sq = execution._make_quote(
                    side=close_side,
                    order_type="stop_market",
                    ref_price=bar_open,
                    exec_price=exec_s,
                    qty=qty_s,
                    fee_rate=execution._taker_fee_rate,
                    liquidity="taker",
                    reason=self.active_stop_reason,
                )
                self.apply_fill(sq, bar_open_ms, i)
                return True

        # A.3 Resting reduce-only stop_market & un-activated stop_limit triggered AT OPEN.
        if self.pos_qty != 0.0 and self.pending_reduce_orders:
            open_stops: list[_QueuedOrder] = []
            for qo in self.pending_reduce_orders:
                it = qo.intent
                if it.side != close_side:
                    continue
                if it.order_type == "stop_market" and it.stop_price is not None and _is_stop_valid_for_fresh(it.stop_price):
                    if (is_long and bar_open <= it.stop_price) or ((not is_long) and bar_open >= it.stop_price):
                        open_stops.append(qo)
                elif it.order_type == "stop_limit" and not qo.activated and it.stop_price is not None and _is_stop_valid_for_fresh(it.stop_price):
                    if (is_long and bar_open <= it.stop_price) or ((not is_long) and bar_open >= it.stop_price):
                        open_stops.append(qo)
            if open_stops:
                open_stops.sort(key=lambda q: q.intent.stop_price or 0.0, reverse=is_long)
                consumed_ids: set[int] = set()
                for qo in open_stops:
                    if self.pos_qty == 0.0:
                        break
                    it = qo.intent
                    if it.order_type == "stop_market":
                        quote, _ = self._try_execute_queued_order(
                            it,
                            qo=qo,
                            bar_idx=i,
                            bar_open=bar_open,
                            bar_high=bar_open,
                            bar_low=bar_open,
                            bar_close=pre_close,
                            bar_volume=pre_volume,
                            bar_atr=pre_atr,
                            pre_high=pre_high,
                            pre_low=pre_low,
                            pre_close=pre_close,
                            pre_volume=pre_volume,
                            pre_atr=pre_atr,
                            eq_basis=_eq_basis(),
                        )
                        if quote is not None:
                            self.apply_fill(quote, bar_open_ms, i)
                            consumed_ids.add(id(qo))
                            exited_any = True
                            adverse_stop_filled = True
                    else:
                        # Protective stop_limit triggered AT OPEN (CR1-45 / CR2-18 / CR2-22):
                        # activate with at-open provenance and price strictly from pre-open data.
                        assert it.limit_price is not None and it.stop_price is not None
                        qo.activated = True
                        qo.activated_bar = i
                        qo.activated_at_open = True
                        if (close_side == "sell" and bar_open >= it.limit_price) or (
                            close_side == "buy" and bar_open <= it.limit_price
                        ):
                            qty_abs = self.size_intent_qty(it, bar_open, bar_open, _eq_basis())
                            if qty_abs > 1e-12:
                                raw_exec = execution._adverse_exec_price(
                                    close_side,
                                    bar_open,
                                    order_notional=bar_open * qty_abs,
                                    bar_high=pre_high,
                                    bar_low=pre_low,
                                    bar_close=pre_close,
                                    bar_volume=pre_volume,
                                    bar_atr=pre_atr,
                                    is_stop=True,
                                )
                                if close_side == "buy":
                                    exec_px = raw_exec if raw_exec <= it.limit_price else it.limit_price
                                else:
                                    exec_px = raw_exec if raw_exec >= it.limit_price else it.limit_price
                                quote = execution._make_quote(
                                    side=close_side,
                                    order_type="stop_limit",
                                    ref_price=bar_open,
                                    exec_price=exec_px,
                                    qty=qty_abs,
                                    fee_rate=execution._taker_fee_rate,
                                    liquidity="taker",
                                    reason=it.tag or "stop_limit",
                                )
                                self.apply_fill(quote, bar_open_ms, i)
                                consumed_ids.add(id(qo))
                                exited_any = True
                                adverse_stop_filled = True
                        # Else: bar_open gapped past limit_price; qo stays activated=True (at_open) for intrabar reach!
                if consumed_ids and self.pos_qty != 0.0:
                    self.pending_reduce_orders = [q for q in self.pending_reduce_orders if id(q) not in consumed_ids]

        if self.pos_qty == 0.0:
            return True

        # A.4 Marketable take-profit / resting limit / pre-open activated stop_limit AT OPEN (CR1-53 / CR2-18).
        if allow_tp and not adverse_stop_filled:
            if include_active_bracket and self.active_take_profit is not None:
                tp = self.active_take_profit
                tp_at_open = (
                    (bar_open > tp if execution.require_touch_through else bar_open >= tp)
                    if is_long
                    else (bar_open < tp if execution.require_touch_through else bar_open <= tp)
                )
                if tp_at_open:
                    tq = execution._make_quote(
                        side=close_side,
                        order_type="limit",
                        ref_price=bar_open,
                        exec_price=bar_open,
                        qty=abs(self.pos_qty),
                        fee_rate=execution._maker_fee_rate,
                        liquidity="maker",
                        reason=self.active_tp_reason,
                    )
                    self.apply_fill(tq, bar_open_ms, i)
                    return True

            if self.pending_reduce_orders:
                open_limits: list[_QueuedOrder] = []
                for qo in self.pending_reduce_orders:
                    it = qo.intent
                    if it.side != close_side or it.limit_price is None:
                        continue
                    is_resting_lim = (it.order_type == "limit") or (
                        it.order_type == "stop_limit" and qo.activated and qo.activated_bar < i
                    )
                    if not is_resting_lim:
                        continue
                    open_only = it.order_type == "limit" and getattr(it, "time_in_force", "gtc") == "open_only"
                    lp = it.limit_price
                    if open_only:
                        mkt = (bar_open >= lp) if close_side == "sell" else (bar_open <= lp)
                    elif close_side == "sell":
                        mkt = (bar_open > lp) if execution.require_touch_through else (bar_open >= lp)
                    else:
                        mkt = (bar_open < lp) if execution.require_touch_through else (bar_open <= lp)
                    if mkt:
                        open_limits.append(qo)
                if open_limits:
                    open_limits.sort(key=lambda q: q.intent.limit_price or 0.0, reverse=(not is_long))
                    consumed_lim: set[int] = set()
                    for qo in open_limits:
                        if self.pos_qty == 0.0:
                            break
                        it = qo.intent
                        qty_abs = self.size_intent_qty(it, bar_open, bar_open, _eq_basis())
                        if qty_abs > 1e-12:
                            lq = execution._make_quote(
                                side=close_side,
                                order_type="limit",
                                ref_price=bar_open,
                                exec_price=bar_open,
                                qty=qty_abs,
                                fee_rate=execution._maker_fee_rate,
                                liquidity="maker",
                                reason=it.tag or it.order_type,
                            )
                            self.apply_fill(lq, bar_open_ms, i)
                            consumed_lim.add(id(qo))
                            exited_any = True
                    if consumed_lim and self.pos_qty != 0.0:
                        self.pending_reduce_orders = [q for q in self.pending_reduce_orders if id(q) not in consumed_lim]

        if self.pos_qty == 0.0:
            return True

        # ---- Step B: Intrabar protective stops -> liquidation -> intrabar TPs (CONSERVATIVE) ----
        is_long = self.pos_qty > 0.0
        close_side = "sell" if is_long else "buy"

        # Gather intrabar stop candidates (bracket stop + resting stop_market + un-activated stop_limit).
        stop_candidates: list[tuple[float, str, _QueuedOrder | None]] = []
        if include_active_bracket and self.active_stop_price is not None and _is_stop_valid_for_fresh(self.active_stop_price):
            sp = self.active_stop_price
            if (is_long and bar_low <= sp) or ((not is_long) and bar_high >= sp):
                stop_candidates.append((sp, "bracket", None))

        for qo in self.pending_reduce_orders:
            it = qo.intent
            if it.side != close_side or it.stop_price is None or not _is_stop_valid_for_fresh(it.stop_price):
                continue
            if it.order_type == "stop_market" or (it.order_type == "stop_limit" and not qo.activated):
                sp = it.stop_price
                if (is_long and bar_low <= sp) or ((not is_long) and bar_high >= sp):
                    stop_candidates.append((sp, it.order_type, qo))

        if stop_candidates:
            # Physical encounter order moving adversely from bar_open.
            stop_candidates.sort(key=lambda item: item[0], reverse=is_long)
            consumed_stops: set[int] = set()
            for sp, kind, qo in stop_candidates:
                if self.pos_qty == 0.0:
                    break
                # Stop beats liquidation ONLY if strictly better than current liq_price (unless liq not breached).
                liq_px = self.cached_liq_price if allow_liq else None
                if liq_px is not None:
                    liq_hit = (liq_l <= liq_px) if is_long else (liq_h >= liq_px)
                    if liq_hit and ((is_long and sp <= liq_px) or ((not is_long) and sp >= liq_px)):
                        break
                if kind == "bracket":
                    sq = execution.try_fill_stop_market(
                        close_side,
                        abs(self.pos_qty),
                        sp,
                        bar_open=bar_open,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        bar_close=bar_close,
                        bar_volume=bar_volume,
                        bar_atr=bar_atr,
                        reason=self.active_stop_reason,
                    )
                    if sq is not None:
                        self.apply_fill(sq, bar_open_ms, i)
                        return True
                elif kind == "stop_market" and qo is not None:
                    quote, _ = self._try_execute_queued_order(
                        qo.intent,
                        qo=qo,
                        bar_idx=i,
                        bar_open=bar_open,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        bar_close=bar_close,
                        bar_volume=bar_volume,
                        bar_atr=bar_atr,
                        pre_high=pre_high,
                        pre_low=pre_low,
                        pre_close=pre_close,
                        pre_volume=pre_volume,
                        pre_atr=pre_atr,
                        eq_basis=_eq_basis(),
                    )
                    if quote is not None:
                        self.apply_fill(quote, bar_open_ms, i)
                        consumed_stops.add(id(qo))
                        exited_any = True
                        adverse_stop_filled = True
                elif kind == "stop_limit" and qo is not None:
                    quote, _ = self._try_execute_queued_order(
                        qo.intent,
                        qo=qo,
                        bar_idx=i,
                        bar_open=bar_open,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        bar_close=bar_close,
                        bar_volume=bar_volume,
                        bar_atr=bar_atr,
                        pre_high=pre_high,
                        pre_low=pre_low,
                        pre_close=pre_close,
                        pre_volume=pre_volume,
                        pre_atr=pre_atr,
                        eq_basis=_eq_basis(),
                    )
                    if quote is not None:
                        self.apply_fill(quote, bar_open_ms, i)
                        consumed_stops.add(id(qo))
                        exited_any = True
                        adverse_stop_filled = True
            if consumed_stops and self.pos_qty != 0.0:
                self.pending_reduce_orders = [q for q in self.pending_reduce_orders if id(q) not in consumed_stops]

        if self.pos_qty == 0.0:
            return True

        # B.2 Intrabar MMR liquidation check on remaining open position.
        if allow_liq and self.cached_liq_price is not None:
            liq_px = self.cached_liq_price
            liq_hit = (liq_l <= liq_px) if is_long else (liq_h >= liq_px)
            if liq_hit:
                lq = execution.try_fill_liquidation(
                    self.pos_qty,
                    liq_px,
                    bar_open=liq_o,
                    bar_high=liq_h,
                    bar_low=liq_l,
                    bar_close=bar_close,
                    bar_volume=bar_volume,
                    bar_atr=bar_atr,
                )
                if lq is not None:
                    self.apply_fill(lq, bar_open_ms, i, reason_override="liquidation")
                    self.liquidations += 1
                    return True

        # B.3 Intrabar TPs / resting limits / eligible activated stop-limits (CONSERVATIVE + CR2-18).
        if allow_tp and not adverse_stop_filled and self.pos_qty != 0.0:
            tp_candidates: list[tuple[float, str, _QueuedOrder | None]] = []
            if include_active_bracket and self.active_take_profit is not None:
                tp_candidates.append((self.active_take_profit, "bracket", None))
            for qo in self.pending_reduce_orders:
                it = qo.intent
                if it.side != close_side or it.limit_price is None:
                    continue
                if it.order_type == "limit":
                    if getattr(it, "time_in_force", "gtc") == "open_only":
                        continue  # open_only never fills intrabar
                    tp_candidates.append((it.limit_price, "limit", qo))
                elif it.order_type == "stop_limit" and qo.activated:
                    # CR2-18: Distinguish pre-open, at-open, and intrabar activation:
                    # intrabar-activated stop-limits MUST NOT match against earlier bar_open or OHLC range.
                    if qo.activated_bar < i:
                        tp_candidates.append((it.limit_price, "stop_limit_pre", qo))
                    elif qo.activated_bar == i and qo.activated_at_open:
                        tp_candidates.append((it.limit_price, "stop_limit_open", qo))
            if tp_candidates:
                # Encounter order moving favorably from bar_open: lowest limit first for long sell, highest for short buy.
                tp_candidates.sort(key=lambda item: item[0], reverse=(not is_long))
                consumed_tps: set[int] = set()
                for lp, kind, qo in tp_candidates:
                    if self.pos_qty == 0.0:
                        break
                    if kind == "bracket":
                        tq = execution.try_fill_limit(
                            close_side,
                            abs(self.pos_qty),
                            lp,
                            bar_open=bar_open,
                            bar_high=bar_high,
                            bar_low=bar_low,
                            reason=self.active_tp_reason,
                        )
                        if tq is not None:
                            self.apply_fill(tq, bar_open_ms, i)
                            return True
                    elif qo is not None:
                        it = qo.intent
                        if kind == "stop_limit_open":
                            # Activated at bar_open past limit_price: fills ONLY on intrabar return to limit_price (never at bar_open).
                            if close_side == "sell":
                                reached = (bar_high > lp) if execution.require_touch_through else (bar_high >= lp)
                            else:
                                reached = (bar_low < lp) if execution.require_touch_through else (bar_low <= lp)
                            if reached:
                                qty_abs = self.size_intent_qty(it, lp, lp, _eq_basis())
                                if qty_abs > 1e-12:
                                    tq = execution._make_quote(
                                        side=close_side,
                                        order_type="limit",
                                        ref_price=lp,
                                        exec_price=lp,
                                        qty=qty_abs,
                                        fee_rate=execution._maker_fee_rate,
                                        liquidity="maker",
                                        reason=it.tag or it.order_type,
                                    )
                                    self.apply_fill(tq, bar_open_ms, i)
                                    consumed_tps.add(id(qo))
                                    exited_any = True
                            continue
                        fill_px = execution.limit_fill_price(
                            close_side,
                            lp,
                            bar_open=bar_open,
                            bar_high=bar_high,
                            bar_low=bar_low,
                        )
                        if fill_px is not None:
                            qty_abs = self.size_intent_qty(it, fill_px, fill_px, _eq_basis())
                            if qty_abs > 1e-12:
                                tq = execution.try_fill_limit(
                                    close_side,
                                    qty_abs,
                                    lp,
                                    bar_open=bar_open,
                                    bar_high=bar_high,
                                    bar_low=bar_low,
                                    reason=it.tag or it.order_type,
                                )
                                if tq is not None:
                                    self.apply_fill(tq, bar_open_ms, i)
                                    consumed_tps.add(id(qo))
                                    exited_any = True
                if consumed_tps and self.pos_qty != 0.0:
                    self.pending_reduce_orders = [q for q in self.pending_reduce_orders if id(q) not in consumed_tps]

        return exited_any

    def process_phase1_pending(
        self,
        i: int,
        open_times: Any,
        opens: Any,
        highs: Any,
        lows: Any,
        closes: Any,
        volumes: Any,
        atr_arr: Any,
    ) -> bool:
        """Execute Phase 1.2..1.7 when pending targets/orders exist. Returns True if orders remain pending."""
        execution = self.execution
        bar_open_ms = int(open_times[i])
        bar_open = opens[i]
        bar_high = highs[i]
        bar_low = lows[i]
        bar_close_p1 = closes[i]
        bar_volume = volumes[i]
        bar_atr = atr_arr[i] if atr_arr is not None else None

        # Pre-open estimates known causally at bar_open (CR1-40).
        if i > 0:
            pre_idx = i - 1
            pre_high = highs[pre_idx]
            pre_low = lows[pre_idx]
            pre_close = closes[pre_idx]
            pre_volume = volumes[pre_idx]
            pre_atr = atr_arr[pre_idx] if atr_arr is not None else None
        else:
            pre_high = bar_open
            pre_low = bar_open
            pre_close = bar_open
            pre_volume = 0.0
            pre_atr = None

        was_open_at_start = self.pos_qty != 0.0
        exited_existing_this_bar = False
        fresh_entry_this_bar = False
        risk_increased_in_p1_6 = False
        p1_6_liq_open = bar_open
        p1_6_liq_high = bar_high
        p1_6_liq_low = bar_low
        new_target_bracket_set = False

        # 1.2 Gap-at-open liquidation check for existing open position (CR1-40 pre-open inputs).
        if self.pos_qty != 0.0 and self.cached_liq_price is not None:
            liq_px = self.cached_liq_price
            gapped_liq = (bar_open <= liq_px) if self.pos_qty > 0.0 else (bar_open >= liq_px)
            if gapped_liq:
                close_side_l: Literal["buy", "sell"] = "sell" if self.pos_qty > 0.0 else "buy"
                qty_l = abs(self.pos_qty)
                slip_l = execution.compute_slippage_price(
                    bar_open,
                    order_notional=bar_open * qty_l,
                    bar_close=pre_close,
                    bar_volume=pre_volume,
                    bar_atr=pre_atr,
                    bar_high=pre_high,
                    bar_low=pre_low,
                    is_stop=True,
                )
                adv_l = (bar_open * execution._half_spread_rate) + slip_l
                exec_l = (bar_open + adv_l) if close_side_l == "buy" else max(execution.min_price, bar_open - adv_l)
                liq_quote = execution._make_quote(
                    side=close_side_l,
                    order_type="liquidation",
                    ref_price=bar_open,
                    exec_price=exec_l,
                    qty=qty_l,
                    fee_rate=execution._liq_total_fee_rate,
                    liquidity="taker",
                    reason="liquidation",
                )
                self.apply_fill(liq_quote, bar_open_ms, i, reason_override="liquidation")
                self.liquidations += 1
                exited_existing_this_bar = True
                self.pending_target = None

        # 1.3 Queued reduce_only market / close_all orders at bar open (CR1-40 pre-open + CR1-47 reduce_only).
        if self.pos_qty != 0.0 and self.pending_reduce_orders:
            kept_reduce: list[_QueuedOrder] = []
            for qo in self.pending_reduce_orders:
                if self.pos_qty == 0.0:
                    break
                if qo.intent.order_type == "market":
                    eq_basis = (
                        self.settled_equity
                        if self.sizing_settled
                        else (self.cash + self.pos_qty * (bar_open - self.avg_exec_price))
                    )
                    quote, _ = self._try_execute_queued_order(
                        qo.intent,
                        qo=qo,
                        bar_idx=i,
                        bar_open=bar_open,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        bar_close=bar_close_p1,
                        bar_volume=bar_volume,
                        bar_atr=bar_atr,
                        pre_high=pre_high,
                        pre_low=pre_low,
                        pre_close=pre_close,
                        pre_volume=pre_volume,
                        pre_atr=pre_atr,
                        eq_basis=eq_basis,
                    )
                    if quote is not None:
                        self.apply_fill(quote, bar_open_ms, i)
                        if self.pos_qty == 0.0:
                            exited_existing_this_bar = True
                        continue
                    # Drop unfillable same-side market reduce_only rather than keeping it forever.
                    continue
                kept_reduce.append(qo)
            if self.pos_qty != 0.0:
                self.pending_reduce_orders = kept_reduce

        # 1.4 Queued TargetPosition market rebalance at bar open (CR1-38/39/40).
        if self.pending_target is not None:
            target = self.pending_target
            self.pending_target = None
            t_weight = target.target_weight
            if self.enforce_max_lev:
                ml = self.max_leverage
                if t_weight > ml:
                    t_weight = ml
                elif t_weight < -ml:
                    t_weight = -ml

            if t_weight == 0.0:
                if self.pos_qty != 0.0:
                    close_side: Literal["buy", "sell"] = "sell" if self.pos_qty > 0.0 else "buy"
                    quote = execution.fill_market(
                        close_side,
                        abs(self.pos_qty),
                        bar_open=bar_open,
                        bar_high=pre_high,
                        bar_low=pre_low,
                        bar_close=pre_close,
                        bar_volume=pre_volume,
                        bar_atr=pre_atr,
                        reason=target.reason or "target_flat",
                    )
                    self.apply_fill(quote, bar_open_ms, i)
                    exited_existing_this_bar = True
                self.active_stop_price = None
                self.active_take_profit = None
                self.last_applied_target_weight = None
            else:
                q_now = self.pos_qty
                same_side_hold = (
                    self.rebalance_on_change
                    and q_now != 0.0
                    and self.last_applied_target_weight is not None
                    and ((q_now > 0.0 and t_weight > 0.0) or (q_now < 0.0 and t_weight < 0.0))
                    and abs(t_weight - self.last_applied_target_weight) <= self.deadband_w
                )
                target_blocked = False
                if not same_side_hold:
                    eq_basis = (
                        self.settled_equity
                        if self.sizing_settled
                        else (self.cash + q_now * (bar_open - self.avg_exec_price))
                    )
                    if eq_basis > 0.0:
                        current_notional = q_now * bar_open
                        current_weight = current_notional / eq_basis
                        # Determine whether this target increases risk (entry, reversal, or same-side scale-up).
                        is_reducing_target = (
                            q_now != 0.0
                            and ((q_now > 0.0 and 0.0 < t_weight <= current_weight + 1e-12)
                                 or (q_now < 0.0 and current_weight - 1e-12 <= t_weight < 0.0))
                        )
                        target_blocked = (not is_reducing_target) and (
                            self.is_halted_by_overlay() or not self.is_entry_session_allowed(bar_open_ms)
                        )
                        if (not target_blocked) and abs(t_weight - current_weight) > self.deadband_w:
                            desired_notional = t_weight * eq_basis
                            delta_notional = desired_notional - current_notional
                            trade_side: Literal["buy", "sell"] = "buy" if delta_notional > 0.0 else "sell"
                            ref_px_t, est_exec = execution.market_exec_price(
                                trade_side,
                                order_notional=abs(delta_notional),
                                bar_open=bar_open,
                                bar_high=pre_high,
                                bar_low=pre_low,
                                bar_close=pre_close,
                                bar_volume=pre_volume,
                                bar_atr=pre_atr,
                            )
                            if (q_now > 0.0 and t_weight < 0.0) or (q_now < 0.0 and t_weight > 0.0):
                                delta_qty_abs = abs(q_now) + (abs(desired_notional) / est_exec)
                            else:
                                delta_qty_abs = abs(delta_notional) / est_exec
                                # CR2-24: A same-side reducing target with non-zero weight must never
                                # cross zero or collapse into a full close due to adverse execution slippage.
                                if is_reducing_target:
                                    q_abs_r = abs(q_now)
                                    if delta_qty_abs >= q_abs_r - 1e-12:
                                        keep_qty = max(abs(desired_notional) / bar_open, 2e-12)
                                        delta_qty_abs = max(0.0, q_abs_r - keep_qty)
                            if delta_qty_abs > 1e-12:
                                quote = execution._make_quote(
                                    side=trade_side,
                                    order_type="market",
                                    ref_price=ref_px_t,
                                    exec_price=est_exec,
                                    qty=delta_qty_abs,
                                    fee_rate=execution._taker_fee_rate,
                                    liquidity="taker",
                                    reason=target.reason or "target_rebalance",
                                )
                                if self.apply_fill(quote, bar_open_ms, i):
                                    fresh_entry_this_bar = True
                        if not target_blocked:
                            self.last_applied_target_weight = t_weight
                # CR2-16: Never install a bracket from a blocked or opposite-side target.
                target_same_side = (
                    (self.pos_qty > 0.0 and t_weight > 0.0)
                    or (self.pos_qty < 0.0 and t_weight < 0.0)
                )
                if (not target_blocked) and target_same_side:
                    self.active_stop_price = target.stop_price
                    self.active_take_profit = target.take_profit
                    self.active_stop_reason = "stop_loss"
                    self.active_tp_reason = "take_profit"
                    new_target_bracket_set = True

        # 1.5 Intrabar protective exits (MMR liquidation / stop-loss / take-profit under CONSERVATIVE).
        if self.pos_qty != 0.0:
            if self.pending_reduce_orders:
                if self.process_protective_exits_with_orders(
                    i,
                    bar_open_ms,
                    bar_open,
                    bar_high,
                    bar_low,
                    bar_close_p1,
                    bar_volume,
                    bar_atr,
                    pre_high,
                    pre_low,
                    pre_close,
                    pre_volume,
                    pre_atr,
                    include_active_bracket=True,
                ):
                    if self.pos_qty == 0.0:
                        exited_existing_this_bar = True
            else:
                liq_px = self.cached_liq_price
                stop_lvl = self.active_stop_price
                tp_lvl = self.active_take_profit
                if liq_px is not None or stop_lvl is not None or tp_lvl is not None:
                    if self.pos_qty > 0.0:
                        might_hit = (
                            (liq_px is not None and bar_low <= liq_px)
                            or (stop_lvl is not None and bar_low <= stop_lvl)
                            or (tp_lvl is not None and bar_high >= tp_lvl)
                        )
                    else:
                        might_hit = (
                            (liq_px is not None and bar_high >= liq_px)
                            or (stop_lvl is not None and bar_high >= stop_lvl)
                            or (tp_lvl is not None and bar_low <= tp_lvl)
                        )
                    if might_hit:
                        # CR2-23: Use the causal pre-open pricing path when triggered at bar_open.
                        exit_quote = self.evaluate_bracket_protective_exit(
                            i, opens, highs, lows, closes, volumes, atr_arr
                        )
                        if exit_quote is not None:
                            self.apply_fill(exit_quote, bar_open_ms, i)
                            if exit_quote.order_type == "liquidation":
                                self.liquidations += 1
                            exited_existing_this_bar = True

        # 1.6 Resting entry / scale-in OrderIntents (CR1-38/39/40/42/46 + CR2-19/20).
        if self.pending_entries:
            if self.is_halted_by_overlay():
                # Loss overlay halted trading: cancel all queued risk-increasing entries (CR1-38).
                self.pending_entries.clear()
            elif was_open_at_start and exited_existing_this_bar:
                self.pending_entries = [
                    qo for qo in self.pending_entries if not qo.created_while_open
                ]

            if self.pending_entries:
                suspended_entries: list[_QueuedOrder] = []
                eligible_entries: list[tuple[int, float, int, _QueuedOrder]] = []
                rtt = execution.require_touch_through
                for orig_idx, qo in enumerate(self.pending_entries):
                    # Check fill-bar session gate for risk-increasing entries (CR1-39 + CR1-20).
                    if not self.is_entry_session_allowed(bar_open_ms, qo.intent):
                        if self.should_cancel_entry_outside_session(bar_open_ms):
                            continue
                        # Suspend outside session while aging TTL cleanly.
                        if qo.bars_left is not None:
                            rem = qo.bars_left - 1
                            if rem <= 0:
                                continue
                            qo.bars_left = rem
                        suspended_entries.append(qo)
                        continue
                    it_e = qo.intent
                    ot_e = it_e.order_type
                    sd_e = it_e.side
                    if ot_e == "market":
                        e_phase, e_dist = 0, 0.0
                    elif ot_e == "limit":
                        assert it_e.limit_price is not None
                        lp_e = it_e.limit_price
                        if getattr(it_e, "time_in_force", "gtc") == "open_only":
                            e_phase, e_dist = 0, 0.0
                        elif (
                            ((bar_open < lp_e) if rtt else (bar_open <= lp_e))
                            if sd_e == "buy"
                            else ((bar_open > lp_e) if rtt else (bar_open >= lp_e))
                        ):
                            e_phase, e_dist = 0, 0.0
                        else:
                            e_phase, e_dist = 1, abs(lp_e - bar_open)
                    elif ot_e == "stop_market":
                        assert it_e.stop_price is not None
                        sp_e = it_e.stop_price
                        if (bar_open >= sp_e) if sd_e == "buy" else (bar_open <= sp_e):
                            e_phase, e_dist = 0, 0.0
                        else:
                            e_phase, e_dist = 1, abs(sp_e - bar_open)
                    else:
                        # stop_limit
                        assert it_e.stop_price is not None and it_e.limit_price is not None
                        sp_e = it_e.stop_price
                        lp_e = it_e.limit_price
                        if qo.activated:
                            if (
                                ((bar_open < lp_e) if rtt else (bar_open <= lp_e))
                                if sd_e == "buy"
                                else ((bar_open > lp_e) if rtt else (bar_open >= lp_e))
                            ):
                                e_phase, e_dist = 0, 0.0
                            else:
                                e_phase, e_dist = 1, abs(lp_e - bar_open)
                        elif (bar_open >= sp_e) if sd_e == "buy" else (bar_open <= sp_e):
                            if (bar_open <= lp_e) if sd_e == "buy" else (bar_open >= lp_e):
                                e_phase, e_dist = 0, 0.0
                            else:
                                e_phase, e_dist = 1, abs(lp_e - bar_open)
                        else:
                            e_phase, e_dist = 1, abs(sp_e - bar_open)
                    eligible_entries.append((e_phase, e_dist, orig_idx, qo))

                if len(eligible_entries) > 1:
                    # CR2-20: Execute open fills before intrabar fills, and intrabar fills in encounter order.
                    eligible_entries.sort(key=lambda item: (item[0], item[1], item[2]))

                next_entries: list[_QueuedOrder] = []
                pos_anchor: float | None = bar_open if self.pos_qty != 0.0 else None
                pos_has_open_exposure: bool = self.pos_qty != 0.0
                liquidated_in_p1_6 = False

                for _, _, _, qo in eligible_entries:
                    eq_basis = (
                        self.settled_equity
                        if self.sizing_settled
                        else (self.cash + self.pos_qty * (bar_open - self.avg_exec_price))
                    )
                    if eq_basis <= 0.0:
                        continue
                    prev_qty = self.pos_qty
                    prev_abs = prev_qty if prev_qty > 0.0 else -prev_qty
                    was_act = qo.activated
                    was_act_bar = qo.activated_bar
                    was_act_open = qo.activated_at_open
                    quote, filled_at_open = self._try_execute_queued_order(
                        qo.intent,
                        qo=qo,
                        bar_idx=i,
                        bar_open=bar_open,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        bar_close=bar_close_p1,
                        bar_volume=bar_volume,
                        bar_atr=bar_atr,
                        pre_high=pre_high,
                        pre_low=pre_low,
                        pre_close=pre_close,
                        pre_volume=pre_volume,
                        pre_atr=pre_atr,
                        eq_basis=eq_basis,
                    )
                    if quote is not None:
                        # CR2-20: Before applying a later intrabar fill, check whether the live position
                        # would already have liquidated (or hit a protective stop) along the path to it.
                        if (not filled_at_open) and self.pos_qty != 0.0 and pos_anchor is not None:
                            target_ref = quote.ref_price
                            path_lo = pos_anchor if pos_anchor < target_ref else target_ref
                            path_hi = pos_anchor if pos_anchor > target_ref else target_ref
                            is_long_cur = self.pos_qty > 0.0
                            if pos_has_open_exposure:
                                if is_long_cur and target_ref >= pos_anchor:
                                    check_lo = bar_low if bar_low < path_lo else path_lo
                                    check_hi = path_hi
                                elif (not is_long_cur) and target_ref <= pos_anchor:
                                    check_lo = path_lo
                                    check_hi = bar_high if bar_high > path_hi else path_hi
                                else:
                                    check_lo = path_lo
                                    check_hi = path_hi
                            else:
                                check_lo = path_lo
                                check_hi = path_hi

                            liq_cur = self.cached_liq_price
                            if liq_cur is not None and (
                                (is_long_cur and check_lo <= liq_cur)
                                or ((not is_long_cur) and check_hi >= liq_cur)
                            ):
                                liqs_before = self.liquidations
                                if self.pending_reduce_orders:
                                    self.process_protective_exits_with_orders(
                                        i,
                                        bar_open_ms,
                                        pos_anchor,
                                        check_hi,
                                        check_lo,
                                        bar_close_p1,
                                        bar_volume,
                                        bar_atr,
                                        pre_high,
                                        pre_low,
                                        pre_close,
                                        pre_volume,
                                        pre_atr,
                                        include_active_bracket=True,
                                        allow_liq=True,
                                        allow_tp=False,
                                        liq_bar_open=pos_anchor,
                                        liq_bar_high=check_hi,
                                        liq_bar_low=check_lo,
                                        skip_stops_at_or_beyond_entry=fresh_entry_this_bar,
                                    )
                                else:
                                    stop_lvl_p = self.active_stop_price
                                    if fresh_entry_this_bar and stop_lvl_p is not None:
                                        if (is_long_cur and stop_lvl_p >= self.avg_exec_price) or (
                                            (not is_long_cur) and stop_lvl_p <= self.avg_exec_price
                                        ):
                                            stop_lvl_p = None
                                    pre_exit = execution.evaluate_protective_exits(
                                        self.pos_qty,
                                        liq_price=liq_cur,
                                        stop_price=stop_lvl_p,
                                        take_profit=None,
                                        bar_open=pos_anchor,
                                        bar_high=check_hi,
                                        bar_low=check_lo,
                                        bar_close=bar_close_p1,
                                        bar_volume=bar_volume,
                                        bar_atr=bar_atr,
                                        stop_reason=self.active_stop_reason,
                                        tp_reason=self.active_tp_reason,
                                    )
                                    if pre_exit is not None:
                                        self.apply_fill(pre_exit, bar_open_ms, i)
                                        if pre_exit.order_type == "liquidation":
                                            self.liquidations += 1
                                if self.liquidations > liqs_before:
                                    liquidated_in_p1_6 = True
                                    exited_existing_this_bar = True
                                    pos_anchor = None
                                    pos_has_open_exposure = False
                                    break
                                if self.pos_qty == 0.0:
                                    exited_existing_this_bar = True
                                    pos_anchor = None
                                    pos_has_open_exposure = False
                                    if qo.created_while_open:
                                        continue
                                    eq_basis = (
                                        self.settled_equity
                                        if self.sizing_settled
                                        else (self.cash + self.pos_qty * (bar_open - self.avg_exec_price))
                                    )
                                    if eq_basis <= 0.0:
                                        continue
                                    prev_qty = 0.0
                                    prev_abs = 0.0
                                    qo.activated = was_act
                                    qo.activated_bar = was_act_bar
                                    qo.activated_at_open = was_act_open
                                    quote, filled_at_open = self._try_execute_queued_order(
                                        qo.intent,
                                        qo=qo,
                                        bar_idx=i,
                                        bar_open=bar_open,
                                        bar_high=bar_high,
                                        bar_low=bar_low,
                                        bar_close=bar_close_p1,
                                        bar_volume=bar_volume,
                                        bar_atr=bar_atr,
                                        pre_high=pre_high,
                                        pre_low=pre_low,
                                        pre_close=pre_close,
                                        pre_volume=pre_volume,
                                        pre_atr=pre_atr,
                                        eq_basis=eq_basis,
                                    )
                                    if quote is None:
                                        if getattr(qo.intent, "time_in_force", "gtc") == "open_only":
                                            continue
                                        if qo.bars_left is not None:
                                            rem = qo.bars_left - 1
                                            if rem <= 0:
                                                continue
                                            qo.bars_left = rem
                                        next_entries.append(qo)
                                        continue

                        self.last_applied_target_weight = None
                        opened_or_flipped = self.apply_fill(quote, bar_open_ms, i)
                        new_abs = self.pos_qty if self.pos_qty > 0.0 else -self.pos_qty
                        if self.pos_qty == 0.0:
                            pos_anchor = None
                            pos_has_open_exposure = False
                        else:
                            pos_anchor = bar_open if filled_at_open else quote.ref_price
                            pos_has_open_exposure = filled_at_open
                        if opened_or_flipped:
                            fresh_entry_this_bar = True
                        if opened_or_flipped or new_abs > prev_abs + 1e-12:
                            risk_increased_in_p1_6 = True
                            # Compute conservative post-fill price envelope for CR1-46.
                            if filled_at_open:
                                p1_6_liq_open = bar_open
                                p1_6_liq_high = bar_high
                                p1_6_liq_low = bar_low
                            else:
                                ref_f = quote.ref_price
                                p1_6_liq_open = ref_f
                                if quote.order_type == "limit":
                                    if quote.side == "buy":
                                        # Price fell from bar_open > ref_f through ref_f to bar_low (IVT).
                                        p1_6_liq_low = bar_low if bar_low < ref_f else ref_f
                                        p1_6_liq_high = bar_close_p1 if bar_close_p1 > ref_f else ref_f
                                    else:
                                        # Price rose from bar_open < ref_f through ref_f to bar_high (IVT).
                                        p1_6_liq_high = bar_high if bar_high > ref_f else ref_f
                                        p1_6_liq_low = bar_close_p1 if bar_close_p1 < ref_f else ref_f
                                else:
                                    # Intrabar stop fill: pre-fill opposite extreme cannot trigger post-fill liquidation.
                                    if quote.side == "buy":
                                        p1_6_liq_low = bar_close_p1 if bar_close_p1 < ref_f else ref_f
                                        p1_6_liq_high = bar_high if bar_high > ref_f else ref_f
                                    else:
                                        p1_6_liq_high = bar_close_p1 if bar_close_p1 > ref_f else ref_f
                                        p1_6_liq_low = bar_low if bar_low < ref_f else ref_f
                        continue
                    # CR2-19: Expire open_only orders immediately after a missed eligible open.
                    if getattr(qo.intent, "time_in_force", "gtc") == "open_only":
                        continue
                    if qo.bars_left is not None:
                        rem = qo.bars_left - 1
                        if rem <= 0:
                            continue
                        qo.bars_left = rem
                    next_entries.append(qo)
                if liquidated_in_p1_6:
                    self.pending_entries = []
                else:
                    self.pending_entries = suspended_entries + next_entries

        # 1.7 Same-bar conservative protective stop & post-fill liquidation recheck (CR1-46).
        if risk_increased_in_p1_6 and self.pos_qty != 0.0:
            if fresh_entry_this_bar and (not new_target_bracket_set):
                if self.pending_reduce_orders:
                    self.process_protective_exits_with_orders(
                        i,
                        bar_open_ms,
                        bar_open,
                        bar_high,
                        bar_low,
                        bar_close_p1,
                        bar_volume,
                        bar_atr,
                        pre_high,
                        pre_low,
                        pre_close,
                        pre_volume,
                        pre_atr,
                        include_active_bracket=True,
                        allow_liq=True,
                        liq_bar_open=p1_6_liq_open,
                        liq_bar_high=p1_6_liq_high,
                        liq_bar_low=p1_6_liq_low,
                        skip_stops_at_or_beyond_entry=True,
                    )
                else:
                    liq_px = self.cached_liq_price
                    liq_hit = (
                        liq_px is not None
                        and ((p1_6_liq_low <= liq_px) if self.pos_qty > 0.0 else (p1_6_liq_high >= liq_px))
                    )
                    stop_lvl = self.active_stop_price
                    if stop_lvl is not None:
                        if (self.pos_qty > 0.0 and stop_lvl >= self.avg_exec_price) or (
                            self.pos_qty < 0.0 and stop_lvl <= self.avg_exec_price
                        ):
                            stop_lvl = None
                    init_exit = execution.evaluate_protective_exits(
                        self.pos_qty,
                        liq_price=liq_px if liq_hit else None,
                        stop_price=stop_lvl,
                        take_profit=self.active_take_profit,
                        bar_open=bar_open,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        bar_close=bar_close_p1,
                        bar_volume=bar_volume,
                        bar_atr=bar_atr,
                        stop_reason=self.active_stop_reason,
                        tp_reason=self.active_tp_reason,
                    )
                    if init_exit is not None:
                        self.apply_fill(init_exit, bar_open_ms, i)
                        if init_exit.order_type == "liquidation":
                            self.liquidations += 1
            else:
                # Same-side scale-in (CR1-46): recheck updated liquidation (and protective stops) on post-fill path!
                liq_px = self.cached_liq_price
                if liq_px is not None:
                    liq_hit = (p1_6_liq_low <= liq_px) if self.pos_qty > 0.0 else (p1_6_liq_high >= liq_px)
                    if liq_hit:
                        if self.pending_reduce_orders:
                            self.process_protective_exits_with_orders(
                                i,
                                bar_open_ms,
                                p1_6_liq_open,
                                p1_6_liq_high,
                                p1_6_liq_low,
                                bar_close_p1,
                                bar_volume,
                                bar_atr,
                                pre_high,
                                pre_low,
                                pre_close,
                                pre_volume,
                                pre_atr,
                                include_active_bracket=True,
                                allow_liq=True,
                                liq_bar_open=p1_6_liq_open,
                                liq_bar_high=p1_6_liq_high,
                                liq_bar_low=p1_6_liq_low,
                            )
                        else:
                            scale_exit = execution.evaluate_protective_exits(
                                self.pos_qty,
                                liq_price=liq_px,
                                stop_price=self.active_stop_price,
                                take_profit=None,
                                bar_open=p1_6_liq_open,
                                bar_high=p1_6_liq_high,
                                bar_low=p1_6_liq_low,
                                bar_close=bar_close_p1,
                                bar_volume=bar_volume,
                                bar_atr=bar_atr,
                                stop_reason=self.active_stop_reason,
                                tp_reason=self.active_tp_reason,
                            )
                            if scale_exit is not None:
                                self.apply_fill(scale_exit, bar_open_ms, i)
                                if scale_exit.order_type == "liquidation":
                                    self.liquidations += 1

        # Decrement TTL and expire missed open_only orders on surviving resting reduce_only orders (CR2-19).
        if self.pending_reduce_orders:
            surviving_reduce: list[_QueuedOrder] = []
            for qo in self.pending_reduce_orders:
                if getattr(qo.intent, "time_in_force", "gtc") == "open_only":
                    continue
                if qo.bars_left is not None:
                    rem = qo.bars_left - 1
                    if rem <= 0:
                        continue
                    qo.bars_left = rem
                surviving_reduce.append(qo)
            self.pending_reduce_orders = surviving_reduce

        return bool(self.pending_entries or self.pending_reduce_orders)

    def process_phase1_target_fast(
        self,
        i: int,
        open_times: Any,
        opens: Any,
        highs: Any,
        lows: Any,
        closes: Any,
        volumes: Any,
        atr_arr: Any,
    ) -> tuple[float, float, bool]:
        """Zero-allocation fast path when only a single TargetPosition is queued."""
        target = self.pending_target
        self.pending_target = None
        assert target is not None

        execution = self.execution
        bar_open = opens[i]
        q_now = self.pos_qty

        # 1.2 Gap-at-open liquidation check for existing open position (CR1-40 pre-open pricing).
        liq_cur = self.cached_liq_price
        if q_now != 0.0 and liq_cur is not None:
            if (q_now > 0.0 and bar_open <= liq_cur) or (q_now < 0.0 and bar_open >= liq_cur):
                pre_idx_l = i - 1 if i > 0 else 0
                close_side_f: Literal["buy", "sell"] = "sell" if q_now > 0.0 else "buy"
                qty_f = abs(q_now)
                slip_f = execution.compute_slippage_price(
                    bar_open,
                    order_notional=bar_open * qty_f,
                    bar_close=closes[pre_idx_l] if i > 0 else bar_open,
                    bar_volume=volumes[pre_idx_l] if i > 0 else 0.0,
                    bar_atr=(atr_arr[pre_idx_l] if (atr_arr is not None and i > 0) else None),
                    bar_high=highs[pre_idx_l] if i > 0 else bar_open,
                    bar_low=lows[pre_idx_l] if i > 0 else bar_open,
                    is_stop=True,
                )
                adv_f = (bar_open * execution._half_spread_rate) + slip_f
                exec_f = (bar_open + adv_f) if close_side_f == "buy" else max(execution.min_price, bar_open - adv_f)
                liq_quote = execution._make_quote(
                    side=close_side_f,
                    order_type="liquidation",
                    ref_price=bar_open,
                    exec_price=exec_f,
                    qty=qty_f,
                    fee_rate=execution._liq_total_fee_rate,
                    liquidity="taker",
                    reason="liquidation",
                )
                self.apply_fill(liq_quote, int(open_times[i]), i, reason_override="liquidation")
                self.liquidations += 1
                return _NEG_INF, _INF, False

        # 1.4 TargetPosition market rebalance at bar open (inlined, zero FillQuote allocation).
        t_weight = target.target_weight
        if self.enforce_max_lev:
            ml = self.max_leverage
            if t_weight > ml:
                t_weight = ml
            elif t_weight < -ml:
                t_weight = -ml

        slip_kind = execution._slip_kind
        half_spr = execution._half_spread_rate
        taker_r = execution._taker_fee_rate

        if t_weight == 0.0:
            if q_now != 0.0:
                is_long_close = q_now > 0.0
                old_abs = q_now if is_long_close else -q_now
                if slip_kind == 0:
                    slip = 0.0
                elif slip_kind == 1:
                    slip = execution._slip_p1
                else:
                    # Pre-open pricing (CR1-40).
                    pre_idx = i - 1 if i > 0 else 0
                    slip = execution.compute_slippage_price(
                        bar_open,
                        order_notional=bar_open * old_abs,
                        bar_close=closes[pre_idx] if i > 0 else bar_open,
                        bar_volume=volumes[pre_idx] if i > 0 else 0.0,
                        bar_atr=(atr_arr[pre_idx] if (atr_arr is not None and i > 0) else None),
                        bar_high=highs[pre_idx] if i > 0 else bar_open,
                        bar_low=lows[pre_idx] if i > 0 else bar_open,
                        is_stop=False,
                    )
                adverse = (bar_open * half_spr) + slip
                if is_long_close:
                    exec_px = bar_open - adverse
                    if exec_px < execution.min_price:
                        exec_px = execution.min_price
                    exit_slip = (bar_open - exec_px) * old_abs
                    pos_sign = 1.0
                    pos_side: Literal["long", "short"] = "long"
                else:
                    exec_px = bar_open + adverse
                    exit_slip = (exec_px - bar_open) * old_abs
                    pos_sign = -1.0
                    pos_side = "short"

                notional = exec_px * old_abs
                exit_fee = notional * taker_r
                self.total_traded_notional += notional
                realized_exec_pnl = pos_sign * old_abs * (exec_px - self.avg_exec_price)
                self.cash += realized_exec_pnl - exit_fee

                trade_gross = pos_sign * old_abs * (bar_open - self.avg_ref_price)
                trade_fees = self.open_entry_fees + exit_fee
                trade_slip = self.open_entry_slippage + exit_slip
                trade_funding = self.open_funding_pnl
                trade_net = fsum((trade_gross, -trade_fees, -trade_slip, trade_funding))

                self.realized_gross_pnl += trade_gross
                self.realized_fees += trade_fees
                self.realized_slippage += trade_slip
                self.realized_funding_pnl += trade_funding
                self.settled_equity += trade_net

                ret_pct = (trade_net / self.pos_entry_notional) if self.pos_entry_notional > 0.0 else 0.0
                self.trades.append(
                    TradeRecord._from_validated(
                        entry_time_ms=self.pos_entry_time_ms,
                        exit_time_ms=int(open_times[i]),
                        side=pos_side,
                        entry_price=self.avg_exec_price,
                        exit_price=exec_px,
                        qty=old_abs,
                        gross_pnl=trade_gross,
                        fees=trade_fees,
                        slippage=trade_slip,
                        funding_pnl=trade_funding,
                        net_pnl=trade_net,
                        return_pct=ret_pct,
                        reason=target.reason or "target_flat",
                        symbol=self.symbol,
                        max_stage=self.pos_stage,
                        ref_entry_price=self.avg_ref_price,
                        ref_exit_price=bar_open,
                    )
                )
                if self.pos_open_bar >= 0:
                    self.bars_in_market += i - self.pos_open_bar
                self.pos_open_bar = -1
                self.pos_qty = 0.0
                self.avg_exec_price = 0.0
                self.avg_ref_price = 0.0
                self.cash_minus_cost = self.cash
                self.open_entry_fees = 0.0
                self.open_entry_slippage = 0.0
                self.open_funding_pnl = 0.0
                self.pos_entry_time_ms = 0
                self.pos_entry_notional = 0.0
                self.pos_stage = 0
                self.cached_liq_price = None
            self.active_stop_price = None
            self.active_take_profit = None
            self.last_applied_target_weight = None
            return _NEG_INF, _INF, False

        same_side_hold = (
            self.rebalance_on_change
            and q_now != 0.0
            and self.last_applied_target_weight is not None
            and ((q_now > 0.0 and t_weight > 0.0) or (q_now < 0.0 and t_weight < 0.0))
            and abs(t_weight - self.last_applied_target_weight) <= self.deadband_w
        )
        if not same_side_hold:
            eq_basis = (
                self.settled_equity
                if self.sizing_settled
                else (self.cash + q_now * (bar_open - self.avg_exec_price))
            )
            if eq_basis > 0.0:
                current_notional = q_now * bar_open
                current_weight = current_notional / eq_basis
                if abs(t_weight - current_weight) > self.deadband_w:
                    desired_notional = t_weight * eq_basis
                    delta_notional = desired_notional - current_notional
                    is_buy = delta_notional > 0.0
                    abs_delta_notional = delta_notional if is_buy else -delta_notional
                    if slip_kind == 0:
                        slip = 0.0
                    elif slip_kind == 1:
                        slip = execution._slip_p1
                    else:
                        # Pre-open pricing (CR1-40).
                        pre_idx2 = i - 1 if i > 0 else 0
                        slip = execution.compute_slippage_price(
                            bar_open,
                            order_notional=abs_delta_notional,
                            bar_close=closes[pre_idx2] if i > 0 else bar_open,
                            bar_volume=volumes[pre_idx2] if i > 0 else 0.0,
                            bar_atr=(atr_arr[pre_idx2] if (atr_arr is not None and i > 0) else None),
                            bar_high=highs[pre_idx2] if i > 0 else bar_open,
                            bar_low=lows[pre_idx2] if i > 0 else bar_open,
                            is_stop=False,
                        )
                    adverse = (bar_open * half_spr) + slip
                    if is_buy:
                        exec_px = bar_open + adverse
                    else:
                        exec_px = bar_open - adverse
                        if exec_px < execution.min_price:
                            exec_px = execution.min_price

                    is_flip = (q_now > 0.0 and t_weight < 0.0) or (q_now < 0.0 and t_weight > 0.0)
                    if is_flip:
                        old_abs = q_now if q_now > 0.0 else -q_now
                        rem_abs = (desired_notional if desired_notional > 0.0 else -desired_notional) / exec_px
                        delta_qty_abs = old_abs + rem_abs
                    else:
                        delta_qty_abs = abs_delta_notional / exec_px
                        # CR2-24: Bound same-side non-zero reducing target strictly below abs(q_now).
                        is_reducing_target = (
                            q_now != 0.0
                            and (
                                (q_now > 0.0 and 0.0 < t_weight <= current_weight + 1e-12)
                                or (q_now < 0.0 and current_weight - 1e-12 <= t_weight < 0.0)
                            )
                        )
                        if is_reducing_target:
                            q_abs_f = q_now if q_now > 0.0 else -q_now
                            if delta_qty_abs >= q_abs_f - 1e-12:
                                keep_qty = max(
                                    (desired_notional if desired_notional > 0.0 else -desired_notional) / bar_open,
                                    2e-12,
                                )
                                delta_qty_abs = max(0.0, q_abs_f - keep_qty)

                    if delta_qty_abs > 1e-12:
                        fill_ms = int(open_times[i])
                        notional = exec_px * delta_qty_abs
                        fee = notional * taker_r
                        slip_cost = (exec_px - bar_open) * (delta_qty_abs if is_buy else -delta_qty_abs)
                        self.total_traded_notional += notional

                        if q_now == 0.0:
                            # Fresh open from flat.
                            self.pos_qty = delta_qty_abs if is_buy else -delta_qty_abs
                            self.avg_exec_price = exec_px
                            self.avg_ref_price = bar_open
                            self.open_entry_fees = fee
                            self.open_entry_slippage = slip_cost
                            self.open_funding_pnl = 0.0
                            self.pos_entry_time_ms = fill_ms
                            self.pos_entry_notional = notional
                            self.pos_stage = 1
                            self.pos_open_bar = i
                            self.cash -= fee
                            self.cash_minus_cost = self.cash - (self.pos_qty * exec_px)
                            self.cached_liq_price = self.recompute_liq_price()
                        elif is_flip:
                            # Flip: close existing leg + open opposite leg.
                            if self.pos_open_bar >= 0:
                                self.bars_in_market += i - self.pos_open_bar
                            self.pos_open_bar = i
                            pos_sign = 1.0 if q_now > 0.0 else -1.0
                            pos_side = "long" if q_now > 0.0 else "short"
                            close_frac = old_abs / delta_qty_abs
                            close_fee = fee * close_frac
                            open_fee = fee - close_fee
                            close_slip = slip_cost * close_frac
                            open_slip = slip_cost - close_slip

                            realized_exec_pnl = pos_sign * old_abs * (exec_px - self.avg_exec_price)
                            self.cash += realized_exec_pnl - close_fee

                            trade_gross = pos_sign * old_abs * (bar_open - self.avg_ref_price)
                            trade_fees = self.open_entry_fees + close_fee
                            trade_slip = self.open_entry_slippage + close_slip
                            trade_funding = self.open_funding_pnl
                            trade_net = fsum((trade_gross, -trade_fees, -trade_slip, trade_funding))

                            self.realized_gross_pnl += trade_gross
                            self.realized_fees += trade_fees
                            self.realized_slippage += trade_slip
                            self.realized_funding_pnl += trade_funding
                            self.settled_equity += trade_net

                            ret_pct = (trade_net / self.pos_entry_notional) if self.pos_entry_notional > 0.0 else 0.0
                            self.trades.append(
                                TradeRecord._from_validated(
                                    entry_time_ms=self.pos_entry_time_ms,
                                    exit_time_ms=fill_ms,
                                    side=pos_side,
                                    entry_price=self.avg_exec_price,
                                    exit_price=exec_px,
                                    qty=old_abs,
                                    gross_pnl=trade_gross,
                                    fees=trade_fees,
                                    slippage=trade_slip,
                                    funding_pnl=trade_funding,
                                    net_pnl=trade_net,
                                    return_pct=ret_pct,
                                    reason=target.reason or "target_rebalance",
                                    symbol=self.symbol,
                                    max_stage=self.pos_stage,
                                    ref_entry_price=self.avg_ref_price,
                                    ref_exit_price=bar_open,
                                )
                            )
                            self.pos_qty = rem_abs if is_buy else -rem_abs
                            self.avg_exec_price = exec_px
                            self.avg_ref_price = bar_open
                            self.open_entry_fees = open_fee
                            self.open_entry_slippage = open_slip
                            self.open_funding_pnl = 0.0
                            self.pos_entry_time_ms = fill_ms
                            self.pos_entry_notional = rem_abs * exec_px
                            self.pos_stage = 1
                            self.cash -= open_fee
                            self.cash_minus_cost = self.cash - (self.pos_qty * exec_px)
                            self.cached_liq_price = self.recompute_liq_price()
                        else:
                            # Same-side resize (add or partial reduce): delegate cleanly to apply_fill.
                            self.total_traded_notional -= notional
                            quote = execution._make_quote(
                                side="buy" if is_buy else "sell",
                                order_type="market",
                                ref_price=bar_open,
                                exec_price=exec_px,
                                qty=delta_qty_abs,
                                fee_rate=taker_r,
                                liquidity="taker",
                                reason=target.reason or "target_rebalance",
                            )
                            self.apply_fill(quote, fill_ms, i)
                self.last_applied_target_weight = t_weight

        if self.pos_qty != 0.0:
            self.active_stop_price = target.stop_price
            self.active_take_profit = target.take_profit
            self.active_stop_reason = "stop_loss"
            self.active_tp_reason = "take_profit"
            lt, ht, has_t = self.compute_triggers()
            # 1.5 Intrabar protective exits if breached on the fill bar (CR2-23 causal pre-open pricing).
            if has_t and (lows[i] <= lt or highs[i] >= ht):
                exit_quote = self.evaluate_bracket_protective_exit(
                    i, opens, highs, lows, closes, volumes, atr_arr
                )
                if exit_quote is not None:
                    self.apply_fill(exit_quote, int(open_times[i]), i)
                    if exit_quote.order_type == "liquidation":
                        self.liquidations += 1
                    return self.compute_triggers()
            return lt, ht, has_t
        return _NEG_INF, _INF, False

    def evaluate_bracket_protective_exit(
        self,
        i: int,
        opens: Any,
        highs: Any,
        lows: Any,
        closes: Any,
        volumes: Any,
        atr_arr: Any,
    ) -> FillQuote | None:
        """Evaluate bracket liquidation/SL/TP using pre-open inputs if triggered at bar_open (CR1-40)."""
        bar_open = opens[i]
        liq_px = self.cached_liq_price
        stp = self.active_stop_price
        is_long = self.pos_qty > 0.0
        at_open = (
            (liq_px is not None and ((bar_open <= liq_px) if is_long else (bar_open >= liq_px)))
            or (stp is not None and ((bar_open <= stp) if is_long else (bar_open >= stp)))
        )
        if at_open:
            pre_idx = i - 1 if i > 0 else 0
            p_close = closes[pre_idx] if i > 0 else bar_open
            p_vol = volumes[pre_idx] if i > 0 else 0.0
            p_atr = atr_arr[pre_idx] if (atr_arr is not None and i > 0) else None
            p_high = highs[pre_idx] if i > 0 else bar_open
            p_low = lows[pre_idx] if i > 0 else bar_open
            execution = self.execution
            close_side: Literal["buy", "sell"] = "sell" if is_long else "buy"
            qty_abs = abs(self.pos_qty)
            is_liq = liq_px is not None and ((bar_open <= liq_px) if is_long else (bar_open >= liq_px))
            slip = execution.compute_slippage_price(
                bar_open,
                order_notional=bar_open * qty_abs,
                bar_close=p_close,
                bar_volume=p_vol,
                bar_atr=p_atr,
                bar_high=p_high,
                bar_low=p_low,
                is_stop=True,
            )
            adv = (bar_open * execution._half_spread_rate) + slip
            exec_px = (bar_open + adv) if close_side == "buy" else max(execution.min_price, bar_open - adv)
            return execution._make_quote(
                side=close_side,
                order_type="liquidation" if is_liq else "stop_market",
                ref_price=bar_open,
                exec_price=exec_px,
                qty=qty_abs,
                fee_rate=execution._liq_total_fee_rate if is_liq else execution._taker_fee_rate,
                liquidity="taker",
                reason="liquidation" if is_liq else self.active_stop_reason,
            )
        return self.execution.evaluate_protective_exits(
            self.pos_qty,
            liq_price=liq_px,
            stop_price=stp,
            take_profit=self.active_take_profit,
            bar_open=bar_open,
            bar_high=highs[i],
            bar_low=lows[i],
            bar_close=closes[i],
            bar_volume=volumes[i],
            bar_atr=atr_arr[i] if atr_arr is not None else None,
            stop_reason=self.active_stop_reason,
            tp_reason=self.active_tp_reason,
        )

    def build_immutable_ctx(
        self,
        bar_idx: int,
        close_eq: float,
        peak_equity: float,
        daily_start_equity: float,
    ) -> BarContext:
        bar_close_ms = int(self.close_times[bar_idx])
        causal_view = _new_sim_mv(_SimMarketView)
        _set_mv_bars(causal_view, self.bars_proxy)
        _set_mv_funding(causal_view, self.funding_proxy)
        _set_mv_features(causal_view, self.cache)
        _set_mv_now(causal_view, bar_close_ms)
        _set_mv_pkey(causal_view, self.primary_key)
        _set_mv_pseries(causal_view, self.primary)
        _set_mv_pend(causal_view, bar_idx + 1)

        q = self.pos_qty
        if q == 0.0 and not self.pending_entries and not self.pending_reduce_orders:
            flat_key = (close_eq, self.cash, peak_equity, daily_start_equity)
            if self.cached_flat_pv is not None and self.cached_flat_key == flat_key:
                portfolio_view = self.cached_flat_pv
            else:
                portfolio_view = _fast_portfolio_view(
                    close_eq,
                    self.cash,
                    _EMPTY_POSITIONS,
                    _EMPTY_ORDERS,
                    peak_equity,
                    daily_start_equity,
                )
                self.cached_flat_pv = portfolio_view
                self.cached_flat_key = flat_key
        else:
            if q != 0.0:
                unrealized = close_eq - self.cash
                abs_notional = (q if q > 0.0 else -q) * self.closes[bar_idx]
                eff_lev = (abs_notional / close_eq) if close_eq > 0.0 else 0.0
                pos_view = _fast_position_view(
                    self.symbol,
                    q,
                    self.avg_exec_price,
                    unrealized,
                    abs_notional,
                    eff_lev,
                    self.cached_liq_price,
                    self.pos_stage,
                )
                positions_map: Mapping[str, PositionView] = MappingProxyType({self.symbol: pos_view})
            else:
                positions_map = _EMPTY_POSITIONS

            if self.pending_entries or self.pending_reduce_orders:
                open_orders = tuple(qo.intent for qo in self.pending_entries) + tuple(
                    qo.intent for qo in self.pending_reduce_orders
                )
            else:
                open_orders = _EMPTY_ORDERS

            portfolio_view = _fast_portfolio_view(
                close_eq,
                self.cash,
                positions_map,
                open_orders,
                peak_equity,
                daily_start_equity,
            )

        return _fast_bar_context(
            bar_close_ms,
            self.symbol,
            causal_view,
            portfolio_view,
            self.instrument,
            self.execution.require_touch_through,
        )


def _normalize_bars_map(
    bars: BarSeries | Mapping[str, BarSeries] | Mapping[tuple[str, str], BarSeries],
    symbol: str,
) -> dict[tuple[str, str], BarSeries]:
    if isinstance(bars, BarSeries):
        return {(bars.symbol, bars.interval): bars}
    if not isinstance(bars, Mapping) or not bars:
        raise ValueError("bars must be a BarSeries or non-empty mapping of BarSeries")

    out: dict[tuple[str, str], BarSeries] = {}
    for key, series in bars.items():
        if not isinstance(series, BarSeries):
            raise TypeError(f"bars values must be BarSeries, got {type(series).__name__}")
        if isinstance(key, tuple) and len(key) == 2:
            key_sym, key_iv = str(key[0]), str(key[1])
            if key_sym != series.symbol or key_iv != series.interval:
                raise ValueError(
                    f"bars key {(key_sym, key_iv)!r} contradicts BarSeries identity "
                    f"{(series.symbol, series.interval)!r}"
                )
            out[(key_sym, key_iv)] = series
        elif isinstance(key, str):
            if symbol != series.symbol or key != series.interval:
                raise ValueError(
                    f"bars key {key!r} for simulator symbol {symbol!r} contradicts BarSeries identity "
                    f"{(series.symbol, series.interval)!r}"
                )
            out[(symbol, key)] = series
        else:
            raise ValueError(f"unsupported bars key: {key!r}")
    return out


def _normalize_funding_map(
    funding: FundingSeries | Mapping[str, FundingSeries] | None,
    symbol: str,
) -> dict[str, FundingSeries]:
    if funding is None:
        return {}
    if isinstance(funding, FundingSeries):
        return {symbol: funding}
    if isinstance(funding, Mapping):
        out: dict[str, FundingSeries] = {}
        for k, v in funding.items():
            if not isinstance(v, FundingSeries):
                raise TypeError(f"funding values must be FundingSeries, got {type(v).__name__}")
            out[str(k)] = v
        return out
    raise TypeError(f"funding must be FundingSeries, Mapping, or None, got {type(funding).__name__}")


def _resolve_primary_interval(
    bars_map: Mapping[tuple[str, str], BarSeries],
    symbol: str,
    strategy: Strategy,
    configured: str | None,
) -> str:
    if configured is not None:
        if (symbol, configured) not in bars_map:
            raise KeyError(f"configured primary_interval {configured!r} missing for {symbol!r}")
        return configured
    for iv in strategy.required_intervals:
        if (symbol, iv) in bars_map:
            return iv
    for sym, iv in bars_map:
        if sym == symbol:
            return iv
    raise KeyError(f"no BarSeries found for symbol {symbol!r}")


def _resolve_warmup_index(
    primary: BarSeries,
    bars_map: Mapping[tuple[str, str], BarSeries],
    symbol: str,
    strategy: Strategy,
    configured_warmup: int | None,
) -> int:
    # Fail closed if any interval with a positive warmup requirement is missing (CR1-37).
    for iv, req_bars in strategy.warmup_bars.items():
        if iv == primary.interval or req_bars <= 0:
            continue
        if (symbol, iv) not in bars_map:
            raise KeyError(
                f"required warmup interval {iv!r} (warmup_bars={req_bars}) missing from bars for {symbol!r}"
            )

    if configured_warmup is not None:
        return configured_warmup

    start_idx = int(strategy.warmup_bars.get(primary.interval, 0))
    if start_idx < 0:
        start_idx = 0

    for iv, req_bars in strategy.warmup_bars.items():
        if iv == primary.interval or req_bars <= 0:
            continue
        other = bars_map[(symbol, iv)]
        if len(other) < req_bars:
            return len(primary)
        needed_close_ms = int(other.close_times[req_bars - 1])
        if primary.is_uniform_close_times:
            idx = bisect_left(primary.open_times, needed_close_ms - primary.interval_ms)
        else:
            idx = bisect_left(primary.close_times, needed_close_ms)
        if idx > start_idx:
            start_idx = idx
    return start_idx


class Simulator:
    """Deterministic two-phase Layer 3 backtest simulator.

    Phase 1 (Bar ``B_t`` execution of orders queued at ``<= t-1``):
      1. Settle exact signed funding events in ``(prev_close_ms, B_t.close_time_ms]``.
      2. Check gap-at-open liquidation, queued market/target orders, intrabar
         protective exits (liquidation / stop-loss / take-profit under CONSERVATIVE
         policy), and resting limit/stop entry orders via ``ExecutionModel``.
      3. Enforce bankruptcy guard (``equity <= 0`` flattens & halts cleanly).

    Phase 2 (Bar ``B_t`` close valuation & decision):
      1. Record canonical bar-close ``equity_curve`` sample ``(B_t.close_time_ms, net_equity)``.
      2. Construct strictly causal ``MarketView`` at ``now_ms = B_t.close_time_ms`` and
         invoke ``strategy.on_bar(ctx)``.
      3. Apply decoupled ``RiskOverlay`` stack in order and queue orders for ``B_{t+1}``
         (strictly zero look-ahead).
    """

    def __init__(
        self,
        instrument: InstrumentSpec,
        cost_model: CostModel | None = None,
        config: SimulatorConfig | None = None,
    ) -> None:
        self.instrument = instrument
        self.cost_model = cost_model if cost_model is not None else CostModel.zero_cost()
        self.config = config if config is not None else SimulatorConfig()
        self.execution = ExecutionModel(
            instrument=self.instrument,
            cost_model=self.cost_model,
            intrabar_policy="CONSERVATIVE",
            require_touch_through=self.config.require_touch_through,
        )
        self._contract_value_float = self.instrument.contract_value_float
        self._is_spot_or_eq = self.instrument.asset_class in ("spot", "equity")
        eff_mmr = self.cost_model.maintenance_margin_rate + self.cost_model.liquidation_fee_rate
        if eff_mmr >= 1.0:
            eff_mmr = 0.999999
        self._one_minus_mmr = 1.0 - eff_mmr
        self._one_plus_mmr = 1.0 + eff_mmr
        self._uses_atr_slip = isinstance(self.cost_model.slippage_model, AtrBps)

    def run(
        self,
        strategy: Strategy,
        bars: BarSeries | Mapping[str, BarSeries] | Mapping[tuple[str, str], BarSeries],
        *,
        funding: FundingSeries | Mapping[str, FundingSeries] | None = None,
        overlays: Sequence[RiskOverlay] = (),
        feature_cache: FeatureCache | None = None,
        reset_components: bool = True,
        funding_mode_label: str | None = None,
    ) -> SimulationResult:
        """Execute a deterministic single-strategy simulation over ``bars``."""
        instrument = self.instrument
        symbol = instrument.symbol
        bars_map = _normalize_bars_map(bars, symbol)
        primary_interval = _resolve_primary_interval(
            bars_map, symbol, strategy, self.config.primary_interval
        )
        primary = bars_map[(symbol, primary_interval)]
        funding_map = _normalize_funding_map(funding, symbol)
        primary_funding = funding_map.get(symbol)
        if primary_funding is not None and primary_funding.instrument_id != instrument.instrument_id:
            raise ValueError(
                f"FundingSeries instrument_id {primary_funding.instrument_id!r} does not match "
                f"simulator instrument {instrument.instrument_id!r}"
            )

        if instrument.is_perp and instrument.funding_interval_hours is not None:
            if primary_funding is None or len(primary_funding) == 0:
                if not self.config.allow_missing_funding_for_perp:
                    raise MissingFundingHistoryError(
                        f"missing historical funding series for perp {instrument.instrument_id}; "
                        "pass a FundingSeries, call Simulator.run_with_funding_sensitivity(...), "
                        "or explicitly opt in via SimulatorConfig(allow_missing_funding_for_perp=True)"
                    )
                resolved_funding_mode = funding_mode_label or "explicit_zero_override"
            else:
                resolved_funding_mode = funding_mode_label or "historical"
        else:
            resolved_funding_mode = funding_mode_label or "spot_no_funding"

        n_bars = len(primary)
        starting_equity = float(self.config.starting_equity)
        start_idx = _resolve_warmup_index(
            primary, bars_map, symbol, strategy, self.config.warmup_bars
        )

        # Fail closed on PARTIAL funding history over the scored span (never silently count gaps as 0).
        run_diagnostics: dict[str, Any] = {}
        if (
            resolved_funding_mode == (funding_mode_label or "historical")
            and primary_funding is not None
            and len(primary_funding) > 0
            and instrument.is_perp
            and instrument.funding_interval_hours is not None
            and 0 < n_bars
            and start_idx < n_bars
        ):
            span_start = int(primary.open_times[start_idx])
            span_end = int(primary.close_times[-1])
            gaps = primary_funding.coverage_gaps(
                span_start,
                span_end,
                instrument.funding_interval_hours,
                self.config.funding_gap_tolerance,
            )
            if gaps:
                coverage = primary_funding.coverage_ratio(
                    span_start,
                    span_end,
                    instrument.funding_interval_hours,
                    self.config.funding_gap_tolerance,
                )
                if self.config.funding_gap_policy != "allow_partial" and not self.config.allow_missing_funding_for_perp:
                    raise MissingFundingHistoryError(
                        f"partial funding history for perp {instrument.instrument_id}: {len(gaps)} gap(s) "
                        f"covering {(1.0 - coverage):.1%} of scored span ({span_start}, {span_end}] "
                        f"(first gap {gaps[0][0]}..{gaps[0][1]}); call Simulator.run_with_funding_sensitivity(...) "
                        "to fill gaps with an explicit APR band, or opt in via "
                        "SimulatorConfig(funding_gap_policy='allow_partial')"
                    )
                if funding_mode_label is None:
                    resolved_funding_mode = "historical_partial"
                run_diagnostics["funding_coverage_ratio"] = coverage
                run_diagnostics["funding_gap_count"] = len(gaps)

        if reset_components:
            strategy.reset()
            for overlay in overlays:
                overlay.reset()

        if n_bars == 0 or start_idx >= n_bars:
            return compute_simulation_metrics(
                starting_equity=starting_equity,
                ending_equity=starting_equity,
                equity_curve=(),
                trades=(),
                attribution=CostAttribution.zero(),
                bars_processed=n_bars,
                bars_scored=0,
                interval_ms=primary.interval_ms,
                symbol=symbol,
                interval=primary_interval,
                strategy_id=strategy.strategy_id,
                funding_mode=resolved_funding_mode,
            )

        cache = feature_cache if feature_cache is not None else FeatureCache()
        bars_proxy = MappingProxyType(bars_map)
        funding_proxy = MappingProxyType(funding_map) if funding_map else _EMPTY_FUNDING_PROXY
        primary_key = (symbol, primary_interval)

        # Pre-compute ATR array once ONLY when AtrBps slippage model is active.
        atr_arr = cache._atr_array(primary, self.config.atr_period) if self._uses_atr_slip else None

        # Materialize columns ONCE before the loop (100% pure LOAD_FAST locals!).
        open_times = primary.open_times
        close_times = primary.close_times
        interval_ms = primary.interval_ms
        opens = primary.opens
        highs = primary.highs
        lows = primary.lows
        closes = cache._closes_tuple(primary) if hasattr(cache, "_closes_tuple") else primary.closes
        volumes = primary.volumes

        execution = self.execution
        is_spot_or_eq = self._is_spot_or_eq
        one_minus_mmr = self._one_minus_mmr
        one_plus_mmr = self._one_plus_mmr
        max_leverage = instrument.max_leverage
        enforce_max_lev = self.config.enforce_instrument_max_leverage
        deadband_w = self.config.rebalance_deadband_weight
        rebalance_on_change = self.config.target_rebalance_mode == "on_change"
        supports_ephemeral = bool(getattr(strategy, "supports_ephemeral_context", False))
        if self.config.reuse_bar_context is None:
            reuse_ctx = supports_ephemeral
        elif self.config.reuse_bar_context:
            if not supports_ephemeral:
                raise ValueError(
                    "SimulatorConfig(reuse_bar_context=True) requires strategy.supports_ephemeral_context = True "
                    "so the default immutable BarContext snapshot contract is never silently broken"
                )
            reuse_ctx = True
        else:
            reuse_ctx = False
        has_overlays = bool(overlays)
        fast_ctx_mode = reuse_ctx and (not has_overlays)
        on_bar_fn = strategy.on_bar

        # Monotonic funding bar-index cursor: O(1) integer check in hot loop (CR2-17: perps only).
        supports_funding = instrument.is_perp and instrument.funding_interval_hours is not None
        f_step_bars = 0
        if supports_funding and primary_funding is not None and len(primary_funding) > 0:
            f_ts = primary_funding.timestamps_ms
            f_rates = primary_funding.rates
            f_len = len(f_ts)
            f_idx = bisect_right(f_ts, open_times[start_idx])
            next_funding_bar = bisect_left(close_times, f_ts[f_idx], start_idx, n_bars) if f_idx < f_len else n_bars
            if primary_funding.settlement_interval_hours and interval_ms > 0:
                step_ms = primary_funding.settlement_interval_hours * 3_600_000
                if step_ms % interval_ms == 0:
                    f_step_bars = step_ms // interval_ms
        else:
            f_ts = None
            f_rates = None
            f_len = 0
            f_idx = 0
            next_funding_bar = n_bars

        # Monotonic UTC day-roll bar-index cursor: runs once per day instead of every bar.
        day_step_bars = (_MS_PER_DAY // interval_ms) if (_MS_PER_DAY % interval_ms == 0) else 0
        next_day_ms = ((int(close_times[start_idx]) // _MS_PER_DAY) + 1) * _MS_PER_DAY
        next_day_bar = bisect_left(close_times, next_day_ms, start_idx, n_bars)

        acct = _SimAccount(
            symbol=symbol,
            instrument=instrument,
            execution=execution,
            cost_model=self.cost_model,
            contract_value=self._contract_value_float,
            max_leverage=max_leverage,
            enforce_max_lev=enforce_max_lev,
            sizing_settled=(self.config.sizing_basis == "settled_equity"),
            rebalance_on_change=rebalance_on_change,
            deadband_w=deadband_w,
            is_spot_or_eq=is_spot_or_eq,
            one_minus_mmr=one_minus_mmr,
            one_plus_mmr=one_plus_mmr,
            starting_equity=starting_equity,
            bars_proxy=bars_proxy,
            funding_proxy=funding_proxy,
            cache=cache,
            primary_key=primary_key,
            primary=primary,
            close_times=close_times,
            closes=closes,
            overlays=overlays,
        )
        live_pos_view = _LivePositionView(acct, symbol)
        live_pf_view = _LivePortfolioView(acct, symbol, live_pos_view)
        live_mv = _LiveMarketView(
            acct, bars_proxy, funding_proxy, cache, primary_key, primary
        )
        live_ctx = _LiveBarContext(acct, symbol, live_mv, live_pf_view, instrument)

        # Hot-path pure LOAD_FAST locals (zero closures in Simulator.run!).
        cash = starting_equity
        cash_minus_cost = starting_equity
        pos_qty = 0.0
        has_position = False
        has_any_pending = False
        p1_mode = 0  # 0=none, 1=low_only, 2=high_only, 3=both, 4=pending
        low_trig = _NEG_INF
        high_trig = _INF
        skip_output: StrategyOutput = EMPTY_OUTPUT
        went_bankrupt = False

        # Online bar-close equity curve statistics (eliminates second pass in metrics.py!).
        peak_equity = starting_equity
        prev_peak = starting_equity
        peak_bar = -1
        min_ratio = 1.0
        prev_min_ratio = 1.0
        min_ratio_bar = -1
        dd_thresh = starting_equity
        prev_eq = starting_equity
        sum_r = 0.0
        sum_r2 = 0.0
        downside_sq = 0.0
        daily_start_equity = starting_equity

        n_scored = n_bars - start_idx
        eq_values: list[float] = []
        eq_append = eq_values.append
        acct.start_idx = start_idx
        acct.eq_values = eq_values
        acct.lazy_daily = fast_ctx_mode

        # ---- Main deterministic bar loop over scored bars [start_idx, n_bars) ----
        if fast_ctx_mode:
            # Handle bar start_idx cleanly so the main loop has zero prev_eq/day_roll/overlay branches!
            acct.callback_active = True
            try:
                i = start_idx
                if i == next_funding_bar:
                    bar_close_ms_f = close_times[i]
                    f_idx += 1
                    while f_idx < f_len and f_ts[f_idx] <= bar_close_ms_f:  # type: ignore[index]
                        f_idx += 1
                    if f_idx < f_len:
                        next_f_ts = f_ts[f_idx]  # type: ignore[index]
                        hint = i + f_step_bars
                        if f_step_bars > 0 and hint < n_bars and close_times[hint] == next_f_ts:
                            next_funding_bar = hint
                        else:
                            next_funding_bar = bisect_left(close_times, next_f_ts, i + 1, n_bars)
                    else:
                        next_funding_bar = n_bars
                eq_append(cash)
                acct.bar_idx = i
                out0: StrategyOutput = on_bar_fn(live_ctx)
                if out0 is not EMPTY_OUTPUT:
                    if out0.targets:
                        for tgt in out0.targets:
                            if tgt.symbol == symbol:
                                acct.pending_target = tgt
                                has_any_pending = True
                                p1_mode = 4
                    if out0.intents:
                        has_any_pending = True
                        p1_mode = 4
                        for intent in out0.intents:
                            if intent.symbol != symbol:
                                continue
                            qo = _QueuedOrder(
                                intent=intent,
                                bars_left=intent.expires_after_bars,
                                created_while_open=False,
                            )
                            if intent.reduce_only or intent.qty_mode == "close_all":
                                acct.pending_reduce_orders.append(qo)
                            else:
                                acct.pending_entries.append(qo)

                for i in range(start_idx + 1, n_bars):
                    # 1.1 Gap funding settlement in (prev_close_ms, open_t] BEFORE bar open fills (CR1-41).
                    if i == next_funding_bar and f_ts[f_idx] <= open_times[i]:  # type: ignore[index]
                        bar_open_ms_g = open_times[i]
                        gap_rate_sum = f_rates[f_idx]  # type: ignore[index]
                        f_idx += 1
                        while f_idx < f_len and f_ts[f_idx] <= bar_open_ms_g:  # type: ignore[index]
                            gap_rate_sum += f_rates[f_idx]  # type: ignore[index]
                            f_idx += 1
                        if f_idx < f_len:
                            next_f_ts = f_ts[f_idx]  # type: ignore[index]
                            if next_f_ts <= close_times[i]:
                                next_funding_bar = i
                            else:
                                hint = i + f_step_bars
                                if f_step_bars > 0 and hint < n_bars and close_times[hint] == next_f_ts:
                                    next_funding_bar = hint
                                else:
                                    next_funding_bar = bisect_left(close_times, next_f_ts, i + 1, n_bars)
                        else:
                            next_funding_bar = n_bars
                        if has_position and gap_rate_sum != 0.0:
                            gap_mark = closes[i - 1] if (i > 0 and closes[i - 1] > 0.0) else opens[i]
                            funding_delta = -pos_qty * gap_mark * gap_rate_sum
                            cash += funding_delta
                            cash_minus_cost += funding_delta
                            acct.cash = cash
                            acct.cash_minus_cost = cash_minus_cost
                            acct.open_funding_pnl += funding_delta
                            acct.cached_liq_price = acct.recompute_liq_price()
                            low_trig, high_trig, _ = acct.compute_triggers()
                            p1_mode = 4 if has_any_pending else (
                                (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                            )

                    if p1_mode:
                        if p1_mode == 1:
                            if lows[i] <= low_trig:
                                exit_quote = acct.evaluate_bracket_protective_exit(
                                    i, opens, highs, lows, closes, volumes, atr_arr
                                )
                                if exit_quote is not None:
                                    acct.apply_fill(exit_quote, int(open_times[i]), i)
                                    if exit_quote.order_type == "liquidation":
                                        acct.liquidations += 1
                                    cash = acct.cash
                                    cash_minus_cost = acct.cash_minus_cost
                                    pos_qty = acct.pos_qty
                                    has_position = pos_qty != 0.0
                                    skip_output = EMPTY_OUTPUT
                                    low_trig, high_trig, _ = acct.compute_triggers()
                                    p1_mode = (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                        elif p1_mode == 2:
                            if highs[i] >= high_trig:
                                exit_quote = acct.evaluate_bracket_protective_exit(
                                    i, opens, highs, lows, closes, volumes, atr_arr
                                )
                                if exit_quote is not None:
                                    acct.apply_fill(exit_quote, int(open_times[i]), i)
                                    if exit_quote.order_type == "liquidation":
                                        acct.liquidations += 1
                                    cash = acct.cash
                                    cash_minus_cost = acct.cash_minus_cost
                                    pos_qty = acct.pos_qty
                                    has_position = pos_qty != 0.0
                                    skip_output = EMPTY_OUTPUT
                                    low_trig, high_trig, _ = acct.compute_triggers()
                                    p1_mode = (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                        elif p1_mode == 4:
                            if acct.pending_target is not None and not acct.pending_entries and not acct.pending_reduce_orders:
                                low_trig, high_trig, _ = acct.process_phase1_target_fast(
                                    i, open_times, opens, highs, lows, closes, volumes, atr_arr
                                )
                                has_any_pending = False
                                cash = acct.cash
                                cash_minus_cost = acct.cash_minus_cost
                                pos_qty = acct.pos_qty
                                has_position = pos_qty != 0.0
                                skip_output = EMPTY_OUTPUT
                                p1_mode = (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                            else:
                                has_any_pending = acct.process_phase1_pending(
                                    i, open_times, opens, highs, lows, closes, volumes, atr_arr
                                )
                                cash = acct.cash
                                cash_minus_cost = acct.cash_minus_cost
                                pos_qty = acct.pos_qty
                                has_position = pos_qty != 0.0
                                skip_output = EMPTY_OUTPUT
                                low_trig, high_trig, _ = acct.compute_triggers()
                                p1_mode = 4 if has_any_pending else (
                                    (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                                )
                        elif lows[i] <= low_trig or highs[i] >= high_trig:
                            exit_quote = acct.evaluate_bracket_protective_exit(
                                i, opens, highs, lows, closes, volumes, atr_arr
                            )
                            if exit_quote is not None:
                                acct.apply_fill(exit_quote, int(open_times[i]), i)
                                if exit_quote.order_type == "liquidation":
                                    acct.liquidations += 1
                                cash = acct.cash
                                cash_minus_cost = acct.cash_minus_cost
                                pos_qty = acct.pos_qty
                                has_position = pos_qty != 0.0
                                skip_output = EMPTY_OUTPUT
                                low_trig, high_trig, _ = acct.compute_triggers()
                                p1_mode = (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)

                    # 1.6 Exact signed funding settlement in (open_t, close_t] on the position held AFTER Bar B_t fills (mark = close_t).
                    if i == next_funding_bar:
                        bar_close_ms_f = close_times[i]
                        rate_sum = f_rates[f_idx]  # type: ignore[index]
                        f_idx += 1
                        if f_idx < f_len and f_ts[f_idx] <= bar_close_ms_f:  # type: ignore[index]
                            while f_idx < f_len and f_ts[f_idx] <= bar_close_ms_f:  # type: ignore[index]
                                rate_sum += f_rates[f_idx]  # type: ignore[index]
                                f_idx += 1
                        if f_idx < f_len:
                            next_f_ts = f_ts[f_idx]  # type: ignore[index]
                            hint = i + f_step_bars
                            if f_step_bars > 0 and hint < n_bars and close_times[hint] == next_f_ts:
                                next_funding_bar = hint
                            else:
                                next_funding_bar = bisect_left(close_times, next_f_ts, i + 1, n_bars)
                        else:
                            next_funding_bar = n_bars
                        if has_position and rate_sum != 0.0:
                            funding_delta = -pos_qty * closes[i] * rate_sum
                            cash += funding_delta
                            cash_minus_cost += funding_delta
                            acct.cash = cash
                            acct.cash_minus_cost = cash_minus_cost
                            acct.open_funding_pnl += funding_delta
                            avg_px_f = acct.avg_exec_price
                            if pos_qty > 0.0:
                                if is_spot_or_eq:
                                    liq_f = None
                                else:
                                    num = (pos_qty * avg_px_f) - cash
                                    liq_f = (num / (pos_qty * one_minus_mmr)) if num > 0.0 else None
                                acct.cached_liq_price = liq_f
                                lt = liq_f if liq_f is not None else _NEG_INF
                                stp_f = acct.active_stop_price
                                if stp_f is not None and stp_f > lt:
                                    lt = stp_f
                                low_trig = lt
                            else:
                                q_abs_f = -pos_qty
                                num = (q_abs_f * avg_px_f) + cash
                                liq_f = (num / (q_abs_f * one_plus_mmr)) if num > 0.0 else 1e-12
                                acct.cached_liq_price = liq_f
                                ht = liq_f
                                stp_f = acct.active_stop_price
                                if stp_f is not None and stp_f < ht:
                                    ht = stp_f
                                high_trig = ht
                            if has_any_pending:
                                p1_mode = 4
                            else:
                                p1_mode = (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)

                    # Close valuation + bankruptcy + online stats (zero branches on flat bars!).
                    if has_position:
                        close_equity = cash_minus_cost + (pos_qty * closes[i])
                        if close_equity <= 0.0:
                            bar_close_b = closes[i]
                            close_side_b: Literal["buy", "sell"] = "sell" if pos_qty > 0.0 else "buy"
                            b_quote = execution.fill_market(
                                close_side_b,
                                abs(pos_qty),
                                bar_open=bar_close_b,
                                bar_high=bar_close_b,
                                bar_low=bar_close_b,
                                bar_close=bar_close_b,
                                bar_volume=volumes[i],
                                bar_atr=atr_arr[i] if atr_arr is not None else None,
                                reason="bankrupt",
                            )
                            acct.apply_fill(b_quote, int(close_times[i]), i, reason_override="bankrupt")
                            acct.liquidations += 1
                            cash = acct.cash
                            pos_qty = acct.pos_qty
                            has_position = False
                            went_bankrupt = True
                            c_eq = 0.0 if cash == 0.0 else cash
                            eq_values.extend([c_eq] * (n_bars - i))
                            break
                        if close_equity > peak_equity:
                            prev_peak = peak_equity
                            peak_equity = close_equity
                            peak_bar = i
                            dd_thresh = peak_equity * min_ratio
                            acct.peak_equity = peak_equity
                        elif close_equity < dd_thresh:
                            prev_min_ratio = min_ratio
                            min_ratio = close_equity / peak_equity
                            min_ratio_bar = i
                            dd_thresh = close_equity
                        r = (close_equity - prev_eq) / prev_eq
                        sum_r += r
                        r2 = r * r
                        sum_r2 += r2
                        if r < 0.0:
                            downside_sq += r2
                        prev_eq = close_equity
                        eq_append(close_equity)
                    else:
                        if cash != prev_eq:
                            if cash <= 0.0:
                                went_bankrupt = True
                                c_eq = 0.0 if cash == 0.0 else cash
                                eq_values.extend([c_eq] * (n_bars - i))
                                break
                            if cash > peak_equity:
                                prev_peak = peak_equity
                                peak_equity = cash
                                peak_bar = i
                                dd_thresh = peak_equity * min_ratio
                                acct.peak_equity = peak_equity
                            elif cash < dd_thresh:
                                prev_min_ratio = min_ratio
                                min_ratio = cash / peak_equity
                                min_ratio_bar = i
                                dd_thresh = cash
                            r = (cash - prev_eq) / prev_eq
                            sum_r += r
                            r2 = r * r
                            sum_r2 += r2
                            if r < 0.0:
                                downside_sq += r2
                            prev_eq = cash
                        eq_append(cash)

                    acct.bar_idx = i
                    output: StrategyOutput = on_bar_fn(live_ctx)

                    # Queue StrategyOutput for Bar B_{t+1} (zero look-ahead).
                    if output is not skip_output and output is not EMPTY_OUTPUT:
                        out_targets = output.targets
                        out_intents = output.intents

                        if (
                            len(out_targets) == 1
                            and not out_intents
                            and has_position
                            and rebalance_on_change
                            and acct.last_applied_target_weight is not None
                        ):
                            tgt = out_targets[0]
                            if tgt.symbol == symbol:
                                t_w = tgt.target_weight
                                eff_w = (
                                    max_leverage
                                    if (enforce_max_lev and t_w > max_leverage)
                                    else (-max_leverage if (enforce_max_lev and t_w < -max_leverage) else t_w)
                                )
                                if (
                                    ((pos_qty > 0.0 and eff_w > 0.0) or (pos_qty < 0.0 and eff_w < 0.0))
                                    and abs(eff_w - acct.last_applied_target_weight) <= deadband_w
                                ):
                                    stp_new = tgt.stop_price
                                    tp_new = tgt.take_profit
                                    acct.active_stop_price = stp_new
                                    acct.active_take_profit = tp_new
                                    acct.active_stop_reason = "stop_loss"
                                    acct.active_tp_reason = "take_profit"
                                    liq_cur = acct.cached_liq_price
                                    if pos_qty > 0.0:
                                        lt = liq_cur if liq_cur is not None else _NEG_INF
                                        if stp_new is not None and stp_new > lt:
                                            lt = stp_new
                                        low_trig = lt
                                        high_trig = tp_new if tp_new is not None else _INF
                                    else:
                                        ht = liq_cur if liq_cur is not None else _INF
                                        if stp_new is not None and stp_new < ht:
                                            ht = stp_new
                                        high_trig = ht
                                        low_trig = tp_new if tp_new is not None else _NEG_INF
                                    p1_mode = 4 if has_any_pending else (
                                        (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                                    )
                                    skip_output = output
                                    continue

                        if out_targets:
                            all_steady_hold = not out_intents
                            for tgt in out_targets:
                                if tgt.symbol != symbol:
                                    continue
                                t_w = tgt.target_weight
                                if has_position and rebalance_on_change and acct.last_applied_target_weight is not None:
                                    eff_w = (
                                        max_leverage
                                        if (enforce_max_lev and t_w > max_leverage)
                                        else (-max_leverage if (enforce_max_lev and t_w < -max_leverage) else t_w)
                                    )
                                    if (
                                        ((pos_qty > 0.0 and eff_w > 0.0) or (pos_qty < 0.0 and eff_w < 0.0))
                                        and abs(eff_w - acct.last_applied_target_weight) <= deadband_w
                                    ):
                                        acct.active_stop_price = tgt.stop_price
                                        acct.active_take_profit = tgt.take_profit
                                        acct.active_stop_reason = "stop_loss"
                                        acct.active_tp_reason = "take_profit"
                                        low_trig, high_trig, _ = acct.compute_triggers()
                                        p1_mode = 4 if has_any_pending else (
                                            (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                                        )
                                        continue
                                all_steady_hold = False
                                skip_output = EMPTY_OUTPUT
                                acct.pending_target = tgt
                                has_any_pending = True
                                p1_mode = 4
                            if all_steady_hold:
                                skip_output = output

                        if out_intents:
                            skip_output = EMPTY_OUTPUT
                            has_any_pending = True
                            p1_mode = 4
                            # CR2-21: Replace protective orders strictly by intent_id, preserving independent
                            # protective orders and any already-activated stop_limit state.
                            if acct.pending_reduce_orders:
                                replaced_reduce_ids = {
                                    it.intent_id
                                    for it in out_intents
                                    if it.symbol == symbol
                                    and (it.reduce_only or it.qty_mode == "close_all")
                                }
                                if replaced_reduce_ids:
                                    acct.pending_reduce_orders = [
                                        existing
                                        for existing in acct.pending_reduce_orders
                                        if existing.intent.intent_id not in replaced_reduce_ids
                                    ]
                            for intent in out_intents:
                                if intent.symbol != symbol:
                                    continue
                                qo = _QueuedOrder(
                                    intent=intent,
                                    bars_left=intent.expires_after_bars,
                                    created_while_open=has_position,
                                )
                                if intent.reduce_only or intent.qty_mode == "close_all":
                                    acct.pending_reduce_orders.append(qo)
                                else:
                                    acct.pending_entries.append(qo)
            finally:
                acct.callback_active = False
        else:
            prev_eq = -1.0
            for i in range(start_idx, n_bars):
                # 1.1 Gap funding settlement in (prev_close_ms, open_t] BEFORE bar open fills (CR1-41).
                if i == next_funding_bar and f_ts[f_idx] <= open_times[i]:  # type: ignore[index]
                    bar_open_ms_g = open_times[i]
                    gap_rate_sum = f_rates[f_idx]  # type: ignore[index]
                    f_idx += 1
                    while f_idx < f_len and f_ts[f_idx] <= bar_open_ms_g:  # type: ignore[index]
                        gap_rate_sum += f_rates[f_idx]  # type: ignore[index]
                        f_idx += 1
                    if f_idx < f_len:
                        next_f_ts = f_ts[f_idx]  # type: ignore[index]
                        if next_f_ts <= close_times[i]:
                            next_funding_bar = i
                        else:
                            hint = i + f_step_bars
                            if f_step_bars > 0 and hint < n_bars and close_times[hint] == next_f_ts:
                                next_funding_bar = hint
                            else:
                                next_funding_bar = bisect_left(close_times, next_f_ts, i + 1, n_bars)
                    else:
                        next_funding_bar = n_bars
                    if has_position and gap_rate_sum != 0.0:
                        gap_mark = closes[i - 1] if (i > 0 and closes[i - 1] > 0.0) else opens[i]
                        funding_delta = -pos_qty * gap_mark * gap_rate_sum
                        cash += funding_delta
                        cash_minus_cost += funding_delta
                        acct.cash = cash
                        acct.cash_minus_cost = cash_minus_cost
                        acct.open_funding_pnl += funding_delta
                        acct.cached_liq_price = acct.recompute_liq_price()
                        low_trig, high_trig, _ = acct.compute_triggers()
                        p1_mode = 4 if has_any_pending else (
                            (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                        )

                if p1_mode:
                    if p1_mode < 4:
                        hit_p1 = (
                            (lows[i] <= low_trig)
                            if p1_mode == 1
                            else (highs[i] >= high_trig)
                            if p1_mode == 2
                            else (lows[i] <= low_trig or highs[i] >= high_trig)
                        )
                        if hit_p1:
                            exit_quote = acct.evaluate_bracket_protective_exit(
                                i, opens, highs, lows, closes, volumes, atr_arr
                            )
                            if exit_quote is not None:
                                acct.apply_fill(exit_quote, int(open_times[i]), i)
                                if exit_quote.order_type == "liquidation":
                                    acct.liquidations += 1
                                cash = acct.cash
                                cash_minus_cost = acct.cash_minus_cost
                                pos_qty = acct.pos_qty
                                has_position = pos_qty != 0.0
                                skip_output = EMPTY_OUTPUT
                                low_trig, high_trig, _ = acct.compute_triggers()
                                p1_mode = (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                    elif (
                        not has_overlays
                        and acct.pending_target is not None
                        and not acct.pending_entries
                        and not acct.pending_reduce_orders
                    ):
                        low_trig, high_trig, _ = acct.process_phase1_target_fast(
                            i, open_times, opens, highs, lows, closes, volumes, atr_arr
                        )
                        has_any_pending = False
                        cash = acct.cash
                        cash_minus_cost = acct.cash_minus_cost
                        pos_qty = acct.pos_qty
                        has_position = pos_qty != 0.0
                        skip_output = EMPTY_OUTPUT
                        p1_mode = (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                    else:
                        has_any_pending = acct.process_phase1_pending(
                            i, open_times, opens, highs, lows, closes, volumes, atr_arr
                        )
                        cash = acct.cash
                        cash_minus_cost = acct.cash_minus_cost
                        pos_qty = acct.pos_qty
                        has_position = pos_qty != 0.0
                        skip_output = EMPTY_OUTPUT
                        low_trig, high_trig, _ = acct.compute_triggers()
                        p1_mode = 4 if has_any_pending else (
                            (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                        )

                if i == next_funding_bar:
                    bar_close_ms_f = close_times[i]
                    rate_sum = f_rates[f_idx]  # type: ignore[index]
                    f_idx += 1
                    while f_idx < f_len and f_ts[f_idx] <= bar_close_ms_f:  # type: ignore[index]
                        rate_sum += f_rates[f_idx]  # type: ignore[index]
                        f_idx += 1
                    if f_idx < f_len:
                        next_f_ts = f_ts[f_idx]  # type: ignore[index]
                        hint = i + f_step_bars
                        if f_step_bars > 0 and hint < n_bars and close_times[hint] == next_f_ts:
                            next_funding_bar = hint
                        else:
                            next_funding_bar = bisect_left(close_times, next_f_ts, i + 1, n_bars)
                    else:
                        next_funding_bar = n_bars
                    if has_position and rate_sum != 0.0:
                        funding_delta = -pos_qty * closes[i] * rate_sum
                        cash += funding_delta
                        cash_minus_cost += funding_delta
                        acct.cash = cash
                        acct.cash_minus_cost = cash_minus_cost
                        acct.open_funding_pnl += funding_delta
                        acct.cached_liq_price = acct.recompute_liq_price()
                        low_trig, high_trig, _ = acct.compute_triggers()
                        p1_mode = 4 if has_any_pending else (
                            (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                        )

                if has_position:
                    close_equity = cash_minus_cost + (pos_qty * closes[i])
                    if close_equity <= 0.0:
                        bar_close_b = closes[i]
                        close_side_b = "sell" if pos_qty > 0.0 else "buy"
                        b_quote = execution.fill_market(
                            close_side_b,
                            abs(pos_qty),
                            bar_open=bar_close_b,
                            bar_high=bar_close_b,
                            bar_low=bar_close_b,
                            bar_close=bar_close_b,
                            bar_volume=volumes[i],
                            bar_atr=atr_arr[i] if atr_arr is not None else None,
                            reason="bankrupt",
                        )
                        acct.apply_fill(b_quote, int(close_times[i]), i, reason_override="bankrupt")
                        acct.liquidations += 1
                        cash = acct.cash
                        pos_qty = acct.pos_qty
                        has_position = False
                        went_bankrupt = True
                        c_eq = 0.0 if cash == 0.0 else cash
                        eq_values.extend([c_eq] * (n_bars - i))
                        break
                else:
                    close_equity = cash
                    if close_equity <= 0.0:
                        went_bankrupt = True
                        c_eq = 0.0 if close_equity == 0.0 else close_equity
                        eq_values.extend([c_eq] * (n_bars - i))
                        break

                if i == next_day_bar:
                    # Equity at the day boundary BEFORE this opening bar's PnL (CR1-30).
                    day_base_eq = prev_eq if prev_eq > 0.0 else starting_equity
                    daily_start_equity = day_base_eq
                    acct.daily_start_equity = day_base_eq
                    next_day_ms = ((int(close_times[i]) // _MS_PER_DAY) + 1) * _MS_PER_DAY
                    day_hint = i + day_step_bars
                    if day_step_bars > 0 and day_hint < n_bars and close_times[day_hint] == next_day_ms:
                        next_day_bar = day_hint
                    else:
                        next_day_bar = bisect_left(close_times, next_day_ms, i + 1, n_bars)

                if close_equity != prev_eq:
                    if close_equity > peak_equity:
                        prev_peak = peak_equity
                        peak_equity = close_equity
                        peak_bar = i
                        dd_thresh = peak_equity * min_ratio
                        acct.peak_equity = peak_equity
                    elif close_equity < dd_thresh:
                        prev_min_ratio = min_ratio
                        min_ratio = close_equity / peak_equity
                        min_ratio_bar = i
                        dd_thresh = close_equity
                    if prev_eq > 0.0:
                        r = (close_equity - prev_eq) / prev_eq
                        sum_r += r
                        r2 = r * r
                        sum_r2 += r2
                        if r < 0.0:
                            downside_sq += r2
                    prev_eq = close_equity

                eq_append(close_equity)

                if reuse_ctx:
                    acct.bar_idx = i
                    acct.callback_active = True
                    try:
                        output = apply_risk_overlays(overlays, live_ctx, on_bar_fn(live_ctx))
                    finally:
                        acct.callback_active = False
                else:
                    ctx = acct.build_immutable_ctx(i, close_equity, peak_equity, daily_start_equity)
                    output = on_bar_fn(ctx)
                    if has_overlays:
                        output = apply_risk_overlays(overlays, ctx, output)

                # If a loss overlay halted trading, cancel all queued risk-increasing entries while preserving exits (CR1-38).
                if has_overlays and acct.is_halted_by_overlay():
                    if acct.pending_entries:
                        acct.pending_entries.clear()
                    if acct.pending_target is not None and acct.pending_target.target_weight != 0.0:
                        acct.pending_target = None
                    has_any_pending = bool(
                        acct.pending_target is not None or acct.pending_entries or acct.pending_reduce_orders
                    )
                    if not has_any_pending:
                        p1_mode = (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)

                if output is not skip_output and output is not EMPTY_OUTPUT:
                    out_targets = output.targets
                    out_intents = output.intents
                    if out_targets:
                        all_steady_hold = not out_intents
                        for tgt in out_targets:
                            if tgt.symbol != symbol:
                                continue
                            t_w = tgt.target_weight
                            if has_position and rebalance_on_change and acct.last_applied_target_weight is not None:
                                eff_w = (
                                    max_leverage
                                    if (enforce_max_lev and t_w > max_leverage)
                                    else (-max_leverage if (enforce_max_lev and t_w < -max_leverage) else t_w)
                                )
                                if (
                                    ((pos_qty > 0.0 and eff_w > 0.0) or (pos_qty < 0.0 and eff_w < 0.0))
                                    and abs(eff_w - acct.last_applied_target_weight) <= deadband_w
                                ):
                                    acct.active_stop_price = tgt.stop_price
                                    acct.active_take_profit = tgt.take_profit
                                    acct.active_stop_reason = "stop_loss"
                                    acct.active_tp_reason = "take_profit"
                                    low_trig, high_trig, _ = acct.compute_triggers()
                                    p1_mode = 4 if has_any_pending else (
                                        (1 if low_trig > _NEG_INF else 0) | (2 if high_trig < _INF else 0)
                                    )
                                    continue
                            all_steady_hold = False
                            skip_output = EMPTY_OUTPUT
                            acct.pending_target = tgt
                            has_any_pending = True
                            p1_mode = 4
                        if all_steady_hold and not has_overlays:
                            skip_output = output
                    if out_intents:
                        skip_output = EMPTY_OUTPUT
                        has_any_pending = True
                        p1_mode = 4
                        # CR2-21: Replace protective orders strictly by intent_id, preserving independent
                        # protective orders and any already-activated stop_limit state.
                        if acct.pending_reduce_orders:
                            replaced_reduce_ids = {
                                it.intent_id
                                for it in out_intents
                                if it.symbol == symbol
                                and (it.reduce_only or it.qty_mode == "close_all")
                            }
                            if replaced_reduce_ids:
                                acct.pending_reduce_orders = [
                                    existing
                                    for existing in acct.pending_reduce_orders
                                    if existing.intent.intent_id not in replaced_reduce_ids
                                ]
                        for intent in out_intents:
                            if intent.symbol != symbol:
                                continue
                            qo = _QueuedOrder(
                                intent=intent,
                                bars_left=intent.expires_after_bars,
                                created_while_open=has_position,
                            )
                            if intent.reduce_only or intent.qty_mode == "close_all":
                                acct.pending_reduce_orders.append(qo)
                            else:
                                acct.pending_entries.append(qo)

        # Close open time-in-market interval at end of scored bars.
        if has_position and acct.pos_open_bar >= 0:
            acct.bars_in_market += n_bars - acct.pos_open_bar
            acct.pos_open_bar = -1

        # ---- End-of-data settlement & exact CostAttribution reconciliation ----
        final_close = closes[-1]
        final_close_ms = int(close_times[-1])
        precomputed_stats: tuple[float, float, float, float, float] | None = None

        if has_position and self.config.close_on_end:
            old_last_eq = eq_values[-1] if eq_values else starting_equity
            close_side_end: Literal["buy", "sell"] = "sell" if pos_qty > 0.0 else "buy"
            qty_abs_end = abs(pos_qty)
            # Apply modeled market spread, slippage, and taker fee to end-of-data close (CR1-43).
            end_quote = execution.fill_market(
                close_side_end,
                qty_abs_end,
                bar_open=final_close,
                bar_high=highs[-1],
                bar_low=lows[-1],
                bar_close=final_close,
                bar_volume=volumes[-1],
                bar_atr=atr_arr[-1] if atr_arr is not None else None,
                reason="end_of_data",
            )
            acct.apply_fill(end_quote, final_close_ms, -1, reason_override="end_of_data")
            ending_equity = acct.cash
            new_last_eq = _clean_zero(ending_equity)
            if eq_values:
                eq_values[-1] = new_last_eq
            attribution = CostAttribution.from_components(
                gross_pnl=acct.realized_gross_pnl,
                fees=acct.realized_fees,
                slippage=acct.realized_slippage,
                funding_pnl=acct.realized_funding_pnl,
            )
            if (not went_bankrupt) and n_scored >= 2 and new_last_eq > 0.0:
                last_bar = n_bars - 1
                peak_eff = prev_peak if peak_bar == last_bar else peak_equity
                min_r_eff = prev_min_ratio if min_ratio_bar == last_bar else min_ratio
                if new_last_eq > peak_eff:
                    peak_eff = new_last_eq
                elif peak_eff > 0.0 and new_last_eq < peak_eff * min_r_eff:
                    min_r_eff = new_last_eq / peak_eff
                penult = eq_values[-2]
                if new_last_eq != old_last_eq:
                    old_r = (old_last_eq - penult) / penult
                    new_r = (new_last_eq - penult) / penult
                    sum_r += new_r - old_r
                    sum_r2 += (new_r * new_r) - (old_r * old_r)
                    if old_r < 0.0:
                        downside_sq -= old_r * old_r
                    if new_r < 0.0:
                        downside_sq += new_r * new_r
                    if downside_sq < 0.0:
                        downside_sq = 0.0
                precomputed_stats = _finalize_curve_stats(
                    starting_equity=starting_equity,
                    ending_equity=ending_equity,
                    first_ts=int(close_times[start_idx]),
                    last_ts=final_close_ms,
                    n_points=n_scored,
                    max_dd=min(0.0, min_r_eff - 1.0),
                    avg_equity=sum(eq_values) / n_scored,
                    sum_r=sum_r,
                    sum_r2=sum_r2,
                    downside_sq=downside_sq,
                    interval_ms=interval_ms,
                )
        elif has_position:
            unrealized_gross = pos_qty * (final_close - acct.avg_ref_price)
            ending_equity = cash + pos_qty * (final_close - acct.avg_exec_price)
            attribution = CostAttribution.from_components(
                gross_pnl=fsum((acct.realized_gross_pnl, unrealized_gross)),
                fees=fsum((acct.realized_fees, acct.open_entry_fees)),
                slippage=fsum((acct.realized_slippage, acct.open_entry_slippage)),
                funding_pnl=fsum((acct.realized_funding_pnl, acct.open_funding_pnl)),
            )
            if (not went_bankrupt) and n_scored >= 1:
                precomputed_stats = _finalize_curve_stats(
                    starting_equity=starting_equity,
                    ending_equity=ending_equity,
                    first_ts=int(close_times[start_idx]),
                    last_ts=final_close_ms,
                    n_points=n_scored,
                    max_dd=min(0.0, min_ratio - 1.0),
                    avg_equity=sum(eq_values) / n_scored,
                    sum_r=sum_r,
                    sum_r2=sum_r2,
                    downside_sq=downside_sq,
                    interval_ms=interval_ms,
                )
        else:
            ending_equity = cash
            attribution = CostAttribution.from_components(
                gross_pnl=acct.realized_gross_pnl,
                fees=acct.realized_fees,
                slippage=acct.realized_slippage,
                funding_pnl=acct.realized_funding_pnl,
            )
            if (not went_bankrupt) and n_scored >= 1:
                precomputed_stats = _finalize_curve_stats(
                    starting_equity=starting_equity,
                    ending_equity=ending_equity,
                    first_ts=int(close_times[start_idx]),
                    last_ts=final_close_ms,
                    n_points=n_scored,
                    max_dd=min(0.0, min_ratio - 1.0),
                    avg_equity=sum(eq_values) / n_scored,
                    sum_r=sum_r,
                    sum_r2=sum_r2,
                    downside_sq=downside_sq,
                    interval_ms=interval_ms,
                )

        equity_curve = tuple(zip(close_times[start_idx:], eq_values))
        return compute_simulation_metrics(
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            equity_curve=equity_curve,
            trades=acct.trades,
            attribution=attribution,
            total_traded_notional=acct.total_traded_notional,
            bars_in_market=acct.bars_in_market,
            bars_scored=n_scored,
            bars_processed=n_bars,
            liquidations=acct.liquidations,
            interval_ms=interval_ms,
            symbol=symbol,
            interval=primary_interval,
            strategy_id=strategy.strategy_id,
            funding_mode=resolved_funding_mode,
            curve_normalized=True,
            equity_values=eq_values,
            precomputed_curve_stats=precomputed_stats,
            diagnostics=run_diagnostics or None,
        )

    def run_with_funding_sensitivity(
        self,
        strategy: Strategy,
        bars: BarSeries | Mapping[str, BarSeries] | Mapping[tuple[str, str], BarSeries],
        *,
        funding: FundingSeries | Mapping[str, FundingSeries] | None = None,
        overlays: Sequence[RiskOverlay] = (),
        feature_cache: FeatureCache | None = None,
        apr_band: Sequence[float] | None = None,
    ) -> FundingSensitivityResult:
        """Run with historical funding if available, or an explicit APR sensitivity sweep.

        Missing funding history on a perpetual contract NEVER silently collapses to ``0.0``:
        it runs every scenario in ``apr_band`` (defaulting to ``cost_model.missing_funding_apr_band``,
        i.e. ``0% / +15% / +30% APR``) with independent strategy and overlay resets.
        """
        symbol = self.instrument.symbol
        bars_map = _normalize_bars_map(bars, symbol)
        primary_interval = _resolve_primary_interval(
            bars_map, symbol, strategy, self.config.primary_interval
        )
        primary = bars_map[(symbol, primary_interval)]
        funding_map = _normalize_funding_map(funding, symbol)
        primary_funding = funding_map.get(symbol)
        if primary_funding is not None and primary_funding.instrument_id != self.instrument.instrument_id:
            raise ValueError(
                f"FundingSeries instrument_id {primary_funding.instrument_id!r} does not match "
                f"simulator instrument {self.instrument.instrument_id!r}"
            )
        cache = feature_cache if feature_cache is not None else FeatureCache()

        if not self.instrument.is_perp or self.instrument.funding_interval_hours is None:
            res = self.run(
                strategy,
                bars_map,
                funding=funding_map if funding_map else None,
                overlays=overlays,
                feature_cache=cache,
                reset_components=True,
                funding_mode_label="spot_no_funding",
            )
            return FundingSensitivityResult(mode="historical", results={"spot_no_funding": res}, apr_band=())

        start_ms = int(primary.open_times[0]) if len(primary) > 0 else 0
        end_ms = int(primary.close_times[-1]) if len(primary) > 0 else 0
        band = apr_band if apr_band is not None else self.cost_model.missing_funding_apr_band

        # Partial historical archive: keep every real event and fill ONLY the gaps with each APR scenario.
        if primary_funding is not None and len(primary_funding) > 0 and end_ms > start_ms:
            hours = self.instrument.funding_interval_hours
            tol = self.config.funding_gap_tolerance
            if primary_funding.coverage_gaps(start_ms, end_ms, hours, tol):
                norm_band = tuple(float(v) for v in band)
                if len(set(norm_band)) != len(norm_band) or len(norm_band) < 2 or all(v == 0.0 for v in norm_band):
                    raise ValueError(
                        "partial funding history requires an explicit multi-point apr_band "
                        "with at least one non-zero APR (e.g. (0.0, 0.15, 0.30))"
                    )
                gap_results: dict[str, SimulationResult] = {}
                for apr in norm_band:
                    label = format_apr_scenario_key(apr, prefix="historical+apr_")
                    filled = primary_funding.fill_gaps_with_apr(
                        start_ms=start_ms,
                        end_ms=end_ms,
                        interval_hours=hours,
                        apr=apr,
                        tolerance=tol,
                    )
                    scenario_funding = dict(funding_map)
                    scenario_funding[symbol] = filled
                    gap_results[label] = self.run(
                        strategy,
                        bars_map,
                        funding=scenario_funding,
                        overlays=overlays,
                        feature_cache=cache,
                        reset_components=True,
                        funding_mode_label=f"historical_gap_filled_{label}",
                    )
                return FundingSensitivityResult(
                    mode="historical_gap_filled",
                    results=gap_results,
                    apr_band=norm_band,
                )

        resolution = require_or_sensitivity_series(
            primary_funding,
            instrument_id=self.instrument.instrument_id,
            start_ms=start_ms,
            end_ms=end_ms,
            interval_hours=self.instrument.funding_interval_hours,
            apr_band=band,
        )

        scenario_results: dict[str, SimulationResult] = {}
        for label, fseries in resolution.scenarios:
            mode_label = "historical" if resolution.is_historical else f"sensitivity_{label}"
            scenario_funding = dict(funding_map)
            scenario_funding[symbol] = fseries
            scenario_results[label] = self.run(
                strategy,
                bars_map,
                funding=scenario_funding,
                overlays=overlays,
                feature_cache=cache,
                reset_components=True,
                funding_mode_label=mode_label,
            )

        return FundingSensitivityResult(
            mode=resolution.mode,
            results=scenario_results,
            apr_band=resolution.apr_band,
        )
