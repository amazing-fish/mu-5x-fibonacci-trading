"""Layer 2 single-source Strategy protocol, execution intents, and multi-family registry.

Pure Python 3.12 stdlib only. Defines immutable data contracts shared by
Layer 2 strategies and the Layer 3 composable backtest/runtime engine.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, is_dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from mu_strategy.core.trading_calendar import calendar_sha256, is_trading_window_allowed

if TYPE_CHECKING:
    from mu_strategy.core.instrument import InstrumentSpec
    from mu_strategy.market_data.bar_series import MarketView

__all__ = [
    "EMPTY_MAPPING",
    "EMPTY_OUTPUT",
    "MAX_PARAM_GRID_SIZE",
    "OrderSide",
    "OrderType",
    "QtyMode",
    "TimeInForce",
    "PriceSource",
    "SideMode",
    "SUPPORTED_ORDER_SIDES",
    "SUPPORTED_ORDER_TYPES",
    "SUPPORTED_QTY_MODES",
    "SUPPORTED_TIME_IN_FORCE",
    "SUPPORTED_PRICE_SOURCES",
    "SUPPORTED_SIDE_MODES",
    "PriceExclusionBand",
    "SessionGate",
    "PositionView",
    "OrderIntent",
    "PortfolioView",
    "BarContext",
    "TargetPosition",
    "StrategyOutput",
    "ParamKind",
    "ParamSpec",
    "ParamSpace",
    "Strategy",
    "FastCursor",
    "FastCursorMarket",
    "StrategyFamilyDescriptor",
    "StrategyFamilyRegistry",
]

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop_market", "stop_limit"]
QtyMode = Literal["target_weight", "equity_fraction", "contracts", "close_all"]
TimeInForce = Literal["gtc", "open_only"]
PriceSource = Literal["fill", "open"]
SideMode = Literal["buy", "sell", "long_short"]
ParamKind = Literal["int", "float", "bool", "categorical"]

SUPPORTED_ORDER_SIDES: frozenset[str] = frozenset({"buy", "sell"})
SUPPORTED_ORDER_TYPES: frozenset[str] = frozenset({"market", "limit", "stop_market", "stop_limit"})
SUPPORTED_QTY_MODES: frozenset[str] = frozenset({"target_weight", "equity_fraction", "contracts", "close_all"})
SUPPORTED_TIME_IN_FORCE: frozenset[str] = frozenset({"gtc", "open_only"})
SUPPORTED_PRICE_SOURCES: frozenset[str] = frozenset({"fill", "open"})
SUPPORTED_SIDE_MODES: frozenset[str] = frozenset({"buy", "sell", "long_short"})
_SUPPORTED_PARAM_KINDS: frozenset[str] = frozenset({"int", "float", "bool", "categorical"})
MAX_PARAM_GRID_SIZE: int = 100_000

EMPTY_MAPPING: Mapping[str, Any] = MappingProxyType({})
_isfinite = math.isfinite
_copysign = math.copysign
_INF = math.inf
_MappingProxyType = MappingProxyType


def _needs_float_set(orig: Any, normalized: float) -> bool:
    return type(orig) is not float or normalized != orig or (normalized == 0.0 and _copysign(1.0, orig) < 0.0)


def _normalize_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not _isfinite(result):
        raise ValueError(f"{name} must be finite")
    if result == 0.0:
        return 0.0
    return result


def _normalize_positive_float(value: Any, name: str) -> float:
    result = _normalize_float(value, name)
    if result <= 0.0:
        raise ValueError(f"{name} must be positive")
    return result


def _normalize_non_negative_float(value: Any, name: str) -> float:
    result = _normalize_float(value, name)
    if result < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _validate_non_empty_str(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class PositionView:
    """Read-only view of a single symbol position passed to strategies."""

    symbol: str
    qty: float
    avg_entry_price: float
    unrealized_pnl: float
    notional: float
    leverage: float
    liquidation_price: float | None = None
    stage: int = 0

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.symbol, "symbol")
        qty = _normalize_float(self.qty, "qty")
        avg_entry = _normalize_non_negative_float(self.avg_entry_price, "avg_entry_price")
        unrealized = _normalize_float(self.unrealized_pnl, "unrealized_pnl")
        notional = _normalize_non_negative_float(self.notional, "notional")
        leverage = _normalize_non_negative_float(self.leverage, "leverage")

        if _needs_float_set(self.qty, qty):
            object.__setattr__(self, "qty", qty)
        if _needs_float_set(self.avg_entry_price, avg_entry):
            object.__setattr__(self, "avg_entry_price", avg_entry)
        if _needs_float_set(self.unrealized_pnl, unrealized):
            object.__setattr__(self, "unrealized_pnl", unrealized)
        if _needs_float_set(self.notional, notional):
            object.__setattr__(self, "notional", notional)
        if _needs_float_set(self.leverage, leverage):
            object.__setattr__(self, "leverage", leverage)

        if self.liquidation_price is not None:
            liq = _normalize_positive_float(self.liquidation_price, "liquidation_price")
            if _needs_float_set(self.liquidation_price, liq):
                object.__setattr__(self, "liquidation_price", liq)

        if isinstance(self.stage, bool) or not isinstance(self.stage, int) or self.stage < 0:
            raise ValueError("stage must be a non-negative integer")

    @property
    def is_flat(self) -> bool:
        return self.qty == 0.0

    @property
    def is_long(self) -> bool:
        return self.qty > 0.0

    @property
    def is_short(self) -> bool:
        return self.qty < 0.0

    @property
    def side(self) -> Literal["long", "short", "flat"]:
        if self.qty > 0.0:
            return "long"
        if self.qty < 0.0:
            return "short"
        return "flat"


def _parse_hhmm_minute(value: Any) -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError(f"invalid trading window endpoint {value!r}; expected HH:MM")
    hh = value[:2]
    mm = value[3:]
    if not (hh.isdigit() and mm.isdigit()):
        raise ValueError(f"invalid trading window endpoint {value!r}; expected HH:MM")
    hour = int(hh)
    minute = int(mm)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"trading window endpoint out of range: {value!r}")
    return hour * 60 + minute


@dataclass(frozen=True, slots=True)
class PriceExclusionBand:
    """Fill-time price exclusion band around ``center`` within ``tolerance_pct``.

    Excludes candidate price ``price`` when
    ``abs(price - center) <= price * tolerance_pct``.

    ``price_source`` selects which price the execution engine tests against this band:
      - ``"fill"`` (default): final execution price after spread/slippage
        (``quote.exec_price``), preserving generic order-fill protection.
      - ``"open"``: raw fill-bar opening price (``bar_open``) before spread/slippage,
        for strategies (such as legacy-parity ``direct_next_open``) whose entry gate
        is defined on the candle open.
    """

    center: float
    tolerance_pct: float
    price_source: PriceSource = "fill"

    def __post_init__(self) -> None:
        center = _normalize_positive_float(self.center, "center")
        if _needs_float_set(self.center, center):
            object.__setattr__(self, "center", center)
        tol = _normalize_non_negative_float(self.tolerance_pct, "tolerance_pct")
        if tol >= 1.0:
            raise ValueError("tolerance_pct must be in [0, 1)")
        if _needs_float_set(self.tolerance_pct, tol):
            object.__setattr__(self, "tolerance_pct", tol)
        if self.price_source not in SUPPORTED_PRICE_SOURCES:
            raise ValueError(f"unsupported price_source: {self.price_source!r}")

    def excludes(self, price: float) -> bool:
        return abs(price - self.center) <= price * self.tolerance_pct


@dataclass(frozen=True, slots=True)
class SessionGate:
    """Fill-time session eligibility gate evaluated against actual bar open timestamps."""

    trading_calendar_id: str
    trading_windows_et: tuple[tuple[str, str], ...]
    trading_calendar_sha256: str = ""

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.trading_calendar_id, "trading_calendar_id")
        if isinstance(self.trading_windows_et, (str, bytes)):
            raise ValueError("trading_windows_et must be a sequence of (HH:MM, HH:MM) pairs")
        parsed_windows: list[tuple[str, str]] = []
        try:
            for window in self.trading_windows_et:
                if isinstance(window, (str, bytes)):
                    raise ValueError("each trading window must be a pair of HH:MM strings")
                start, end = window
                start_min = _parse_hhmm_minute(start)
                end_min = _parse_hhmm_minute(end)
                if start_min > end_min:
                    raise ValueError(f"trading window must not cross ET date boundary: {(start, end)!r}")
                parsed_windows.append((start, end))
        except TypeError as exc:
            raise ValueError("trading_windows_et must be a sequence of (HH:MM, HH:MM) pairs") from exc
        if not parsed_windows:
            raise ValueError("trading_windows_et must be non-empty")
        object.__setattr__(self, "trading_windows_et", tuple(parsed_windows))
        digest = calendar_sha256(self.trading_calendar_id)
        if not self.trading_calendar_sha256:
            object.__setattr__(self, "trading_calendar_sha256", digest)
        elif self.trading_calendar_sha256 != digest:
            raise ValueError("trading_calendar_sha256 does not match trading_calendar_id")

    def is_allowed(self, ts_ms: int) -> bool:
        return is_trading_window_allowed(ts_ms, self)


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Explicit executable order intent emitted by a Strategy."""

    intent_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty_mode: QtyMode
    qty_value: float
    limit_price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    expires_after_bars: int | None = None
    tag: str = ""
    time_in_force: TimeInForce = "gtc"
    session_gate: SessionGate | None = None
    excluded_price_bands: tuple[PriceExclusionBand, ...] = ()

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.intent_id, "intent_id")
        _validate_non_empty_str(self.symbol, "symbol")
        if self.side not in SUPPORTED_ORDER_SIDES:
            raise ValueError(f"unsupported side: {self.side!r}")
        if self.order_type not in SUPPORTED_ORDER_TYPES:
            raise ValueError(f"unsupported order_type: {self.order_type!r}")
        if self.qty_mode not in SUPPORTED_QTY_MODES:
            raise ValueError(f"unsupported qty_mode: {self.qty_mode!r}")
        if self.time_in_force not in SUPPORTED_TIME_IN_FORCE:
            raise ValueError(f"unsupported time_in_force: {self.time_in_force!r}")
        if self.time_in_force == "open_only" and self.order_type not in ("market", "limit"):
            raise ValueError("time_in_force='open_only' is only valid for market and limit orders")

        qty_val = _normalize_float(self.qty_value, "qty_value")
        if self.qty_mode in ("contracts", "equity_fraction"):
            if qty_val <= 0.0:
                raise ValueError(f"qty_value must be positive for qty_mode={self.qty_mode!r}")
        elif self.qty_mode == "close_all":
            if qty_val < 0.0:
                raise ValueError("qty_value must be non-negative for qty_mode='close_all'")
        if _needs_float_set(self.qty_value, qty_val):
            object.__setattr__(self, "qty_value", qty_val)

        if self.order_type in ("limit", "stop_limit"):
            if self.limit_price is None:
                raise ValueError(f"limit_price is required for order_type={self.order_type!r}")
            limit_px = _normalize_positive_float(self.limit_price, "limit_price")
            if _needs_float_set(self.limit_price, limit_px):
                object.__setattr__(self, "limit_price", limit_px)
        elif self.limit_price is not None:
            raise ValueError(f"limit_price is not allowed for order_type={self.order_type!r}")

        if self.order_type in ("stop_market", "stop_limit"):
            if self.stop_price is None:
                raise ValueError(f"stop_price is required for order_type={self.order_type!r}")
            stop_px = _normalize_positive_float(self.stop_price, "stop_price")
            if _needs_float_set(self.stop_price, stop_px):
                object.__setattr__(self, "stop_price", stop_px)
        elif self.stop_price is not None:
            raise ValueError(f"stop_price is not allowed for order_type={self.order_type!r}")

        if not isinstance(self.reduce_only, bool):
            raise ValueError("reduce_only must be a bool")

        if self.session_gate is not None:
            if not isinstance(self.session_gate, SessionGate):
                raise ValueError("session_gate must be a SessionGate or None")
            if self.reduce_only:
                raise ValueError("session_gate is not allowed on reduce_only protective exits")

        bands = self.excluded_price_bands
        if type(bands) is not tuple:
            if isinstance(bands, (str, bytes)):
                raise ValueError("excluded_price_bands must be an iterable of PriceExclusionBand instances")
            try:
                bands = tuple(bands)
            except TypeError as exc:
                raise ValueError("excluded_price_bands must be an iterable of PriceExclusionBand instances") from exc
            object.__setattr__(self, "excluded_price_bands", bands)
        if bands:
            for band in bands:
                if not isinstance(band, PriceExclusionBand):
                    raise ValueError("excluded_price_bands entries must be PriceExclusionBand instances")
            if self.reduce_only:
                raise ValueError("excluded_price_bands is not allowed on reduce_only protective exits")

        if self.expires_after_bars is not None:
            if (
                isinstance(self.expires_after_bars, bool)
                or not isinstance(self.expires_after_bars, int)
                or self.expires_after_bars <= 0
            ):
                raise ValueError("expires_after_bars must be a positive integer")

        if not isinstance(self.tag, str):
            raise ValueError("tag must be a string")


@dataclass(frozen=True, slots=True)
class PortfolioView:
    """Read-only snapshot of account equity, positions, and open orders."""

    equity: float
    cash: float
    positions: Mapping[str, PositionView]
    open_orders: tuple[OrderIntent, ...]
    peak_equity: float
    daily_start_equity: float

    def __post_init__(self) -> None:
        equity = _normalize_float(self.equity, "equity")
        cash = _normalize_float(self.cash, "cash")
        peak = _normalize_float(self.peak_equity, "peak_equity")
        daily = _normalize_float(self.daily_start_equity, "daily_start_equity")

        if _needs_float_set(self.equity, equity):
            object.__setattr__(self, "equity", equity)
        if _needs_float_set(self.cash, cash):
            object.__setattr__(self, "cash", cash)
        if _needs_float_set(self.peak_equity, peak):
            object.__setattr__(self, "peak_equity", peak)
        if _needs_float_set(self.daily_start_equity, daily):
            object.__setattr__(self, "daily_start_equity", daily)

        if self.positions is not EMPTY_MAPPING:
            if not isinstance(self.positions, Mapping):
                raise ValueError("positions must be a mapping")
            # Always copy caller-supplied mappings (including MappingProxyType over a caller dict)
            # so later external mutations cannot alter this snapshot.
            object.__setattr__(self, "positions", _MappingProxyType(dict(self.positions)))

        if type(self.open_orders) is not tuple:
            object.__setattr__(self, "open_orders", tuple(self.open_orders))

    def position(self, symbol: str) -> PositionView | None:
        return self.positions.get(symbol)

    def position_qty(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.qty if pos is not None else 0.0

    def gross_notional(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            total += pos.notional
        return total

    def net_notional(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            if pos.qty > 0.0:
                total += pos.notional
            elif pos.qty < 0.0:
                total -= pos.notional
        return 0.0 if total == 0.0 else total


@dataclass(frozen=True, slots=True)
class BarContext:
    """Strictly causal decision context passed to ``Strategy.on_bar``."""

    now_ms: int
    symbol: str
    market: MarketView
    portfolio: PortfolioView
    instrument: InstrumentSpec
    require_touch_through: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.now_ms, bool) or not isinstance(self.now_ms, int) or self.now_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")
        _validate_non_empty_str(self.symbol, "symbol")
        if not isinstance(self.portfolio, PortfolioView):
            raise ValueError("portfolio must be a PortfolioView")
        if not isinstance(self.require_touch_through, bool):
            raise ValueError("require_touch_through must be a bool")
        market_now = getattr(self.market, "now_ms", None)
        if market_now is not None:
            if isinstance(market_now, bool) or not isinstance(market_now, int):
                raise ValueError("market.now_ms must be an integer")
            if market_now != self.now_ms:
                raise ValueError(
                    f"market.now_ms ({market_now}) must match BarContext.now_ms ({self.now_ms})"
                )


@dataclass(frozen=True, slots=True)
class TargetPosition:
    """Ergonomic directional / weight target emitted by a Strategy.

    ``target_weight`` is signed notional / equity (negative = short,
    0.0 = flat, positive = long).
    """

    symbol: str
    target_weight: float
    stop_price: float | None = None
    take_profit: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        sym = self.symbol
        if type(sym) is not str or not sym or sym.isspace():
            _validate_non_empty_str(sym, "symbol")

        w_raw = self.target_weight
        if type(w_raw) is float and -_INF < w_raw < _INF:
            if w_raw == 0.0 and _copysign(1.0, w_raw) < 0.0:
                object.__setattr__(self, "target_weight", 0.0)
                weight = 0.0
            else:
                weight = w_raw
        else:
            weight = _normalize_float(w_raw, "target_weight")
            object.__setattr__(self, "target_weight", weight)

        stop = self.stop_price
        if stop is not None:
            if type(stop) is float and 0.0 < stop < _INF:
                pass
            else:
                stop = _normalize_positive_float(stop, "stop_price")
                object.__setattr__(self, "stop_price", stop)

        tp = self.take_profit
        if tp is not None:
            if type(tp) is float and 0.0 < tp < _INF:
                pass
            else:
                tp = _normalize_positive_float(tp, "take_profit")
                object.__setattr__(self, "take_profit", tp)

        if stop is not None and tp is not None:
            if weight > 0.0 and stop >= tp:
                raise ValueError("long TargetPosition requires stop_price < take_profit")
            if weight < 0.0 and stop <= tp:
                raise ValueError("short TargetPosition requires stop_price > take_profit")

        if type(self.reason) is not str and not isinstance(self.reason, str):
            raise ValueError("reason must be a string")


def _freeze_diagnostic_value(value: Any, active: set[int]) -> Any:
    """Recursively freeze standard mutable containers in ``StrategyOutput.diagnostics``.

    Tracks active container identities on the current recursion path and raises
    ``ValueError`` on reference cycles while preserving shared acyclic containers.
    """
    if isinstance(value, Mapping):
        obj_id = id(value)
        if obj_id in active:
            raise ValueError("diagnostics cannot contain cyclic references")
        active.add(obj_id)
        try:
            return _MappingProxyType({k: _freeze_diagnostic_value(v, active) for k, v in value.items()})
        finally:
            active.discard(obj_id)
    if isinstance(value, (list, tuple)):
        obj_id = id(value)
        if obj_id in active:
            raise ValueError("diagnostics cannot contain cyclic references")
        active.add(obj_id)
        try:
            return tuple(_freeze_diagnostic_value(v, active) for v in value)
        finally:
            active.discard(obj_id)
    if isinstance(value, set):
        obj_id = id(value)
        if obj_id in active:
            raise ValueError("diagnostics cannot contain cyclic references")
        active.add(obj_id)
        try:
            return frozenset(_freeze_diagnostic_value(v, active) for v in value)
        finally:
            active.discard(obj_id)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


@dataclass(frozen=True, slots=True)
class StrategyOutput:
    """Combined order intents, target weights, and diagnostics from ``on_bar``.

    ``diagnostics`` copies caller-owned mappings and recursively freezes standard
    nested containers (``Mapping``, ``list``, ``tuple``, ``set``, ``bytearray``)
    so later caller mutations to those containers cannot alter earlier outputs.
    Cyclic container references are rejected with ``ValueError``; shared acyclic
    container references remain valid.
    """

    intents: tuple[OrderIntent, ...] = ()
    targets: tuple[TargetPosition, ...] = ()
    diagnostics: Mapping[str, Any] = EMPTY_MAPPING

    def __post_init__(self) -> None:
        if type(self.intents) is not tuple:
            object.__setattr__(self, "intents", tuple(self.intents))
        if type(self.targets) is not tuple:
            object.__setattr__(self, "targets", tuple(self.targets))
        diag = self.diagnostics
        if diag is not EMPTY_MAPPING:
            if not isinstance(diag, Mapping):
                raise ValueError("diagnostics must be a mapping")
            object.__setattr__(self, "diagnostics", _freeze_diagnostic_value(diag, set()))

    @classmethod
    def empty(cls, diagnostics: Mapping[str, Any] | None = None) -> StrategyOutput:
        if not diagnostics:
            return EMPTY_OUTPUT
        return cls(diagnostics=diagnostics)


EMPTY_OUTPUT = StrategyOutput()

_new_target_pos = TargetPosition.__new__
_set_tp_sym = TargetPosition.symbol.__set__
_set_tp_w = TargetPosition.target_weight.__set__
_set_tp_stop = TargetPosition.stop_price.__set__
_set_tp_tp = TargetPosition.take_profit.__set__
_set_tp_reason = TargetPosition.reason.__set__

_new_strat_out = StrategyOutput.__new__
_set_so_intents = StrategyOutput.intents.__set__
_set_so_targets = StrategyOutput.targets.__set__
_set_so_diag = StrategyOutput.diagnostics.__set__


def _fast_target_output(
    symbol: str,
    target_weight: float,
    stop_price: float | None,
    reason: str,
) -> StrategyOutput:
    """Fast internal constructor for already-validated single-target `StrategyOutput`."""
    tp = _new_target_pos(TargetPosition)
    _set_tp_sym(tp, symbol)
    _set_tp_w(tp, 0.0 if target_weight == 0.0 else target_weight)
    _set_tp_stop(tp, stop_price)
    _set_tp_tp(tp, None)
    _set_tp_reason(tp, reason)

    out = _new_strat_out(StrategyOutput)
    _set_so_intents(out, ())
    _set_so_targets(out, (tp,))
    _set_so_diag(out, EMPTY_MAPPING)
    return out


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Declarative parameter specification for sweeps and validation."""

    name: str
    kind: ParamKind
    default: Any
    min_value: int | float | None = None
    max_value: int | float | None = None
    step: int | float | None = None
    choices: tuple[Any, ...] | None = None
    description: str = ""

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "name")
        if self.kind not in _SUPPORTED_PARAM_KINDS:
            raise ValueError(f"unsupported param kind: {self.kind!r}")
        if not isinstance(self.description, str):
            raise ValueError("description must be a string")

        if self.choices is not None:
            choices_tuple = tuple(self.choices)
            if not choices_tuple:
                raise ValueError("choices must be non-empty when provided")
            object.__setattr__(self, "choices", choices_tuple)

        if self.kind == "int":
            if self.min_value is not None:
                if isinstance(self.min_value, bool) or not isinstance(self.min_value, int):
                    raise ValueError("int ParamSpec min_value must be an int")
            if self.max_value is not None:
                if isinstance(self.max_value, bool) or not isinstance(self.max_value, int):
                    raise ValueError("int ParamSpec max_value must be an int")
            if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
                raise ValueError("min_value must be <= max_value")
            if self.step is not None:
                if isinstance(self.step, bool) or not isinstance(self.step, int) or self.step <= 0:
                    raise ValueError("int ParamSpec step must be a positive int")
        elif self.kind == "float":
            if self.min_value is not None:
                _normalize_float(self.min_value, "min_value")
            if self.max_value is not None:
                _normalize_float(self.max_value, "max_value")
            if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
                raise ValueError("min_value must be <= max_value")
            if self.step is not None:
                _normalize_positive_float(self.step, "step")
        elif self.kind == "categorical":
            if self.choices is None:
                raise ValueError("categorical ParamSpec requires choices")
            if self.min_value is not None or self.max_value is not None or self.step is not None:
                raise ValueError("categorical ParamSpec cannot set min_value, max_value, or step")
        elif self.kind == "bool":
            if self.min_value is not None or self.max_value is not None or self.step is not None:
                raise ValueError("bool ParamSpec cannot set min_value, max_value, or step")

        if self.choices is not None:
            for choice in self.choices:
                self._validate_type_and_bounds(choice)

        self.validate(self.default)

    def _validate_type_and_bounds(self, value: Any) -> None:
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"parameter {self.name!r} must be a bool")
        elif self.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"parameter {self.name!r} must be an int")
            if self.min_value is not None and value < self.min_value:
                raise ValueError(f"parameter {self.name!r} must be >= {self.min_value}")
            if self.max_value is not None and value > self.max_value:
                raise ValueError(f"parameter {self.name!r} must be <= {self.max_value}")
        elif self.kind == "float":
            val = _normalize_float(value, f"parameter {self.name!r}")
            if self.min_value is not None and val < float(self.min_value):
                raise ValueError(f"parameter {self.name!r} must be >= {self.min_value}")
            if self.max_value is not None and val > float(self.max_value):
                raise ValueError(f"parameter {self.name!r} must be <= {self.max_value}")
        elif self.kind == "categorical":
            assert self.choices is not None
            if value not in self.choices:
                raise ValueError(f"parameter {self.name!r} must be one of {self.choices!r}")

    def validate(self, value: Any) -> None:
        self._validate_type_and_bounds(value)
        if self.choices is not None and value not in self.choices:
            raise ValueError(f"parameter {self.name!r} must be one of {self.choices!r}")

    def grid_values(self) -> tuple[Any, ...]:
        if self.choices is not None:
            return self.choices
        if self.kind == "bool":
            return (False, True)
        if self.kind == "int" and self.min_value is not None and self.max_value is not None:
            lo_i = int(self.min_value)
            hi_i = int(self.max_value)
            step_i = int(self.step) if self.step is not None else 1
            count = ((hi_i - lo_i) // step_i) + 1
            if count > MAX_PARAM_GRID_SIZE:
                raise ValueError(
                    f"parameter {self.name!r} grid size {count} exceeds limit {MAX_PARAM_GRID_SIZE}"
                )
            return tuple(range(lo_i, hi_i + 1, step_i))
        if self.kind == "float" and self.min_value is not None and self.max_value is not None and self.step is not None:
            lo = float(self.min_value)
            hi = float(self.max_value)
            step = float(self.step)
            with localcontext() as ctx:
                ctx.prec = 50
                ctx.Emax = 10_000
                ctx.Emin = -10_000
                d_lo = Decimal(str(lo))
                d_hi = Decimal(str(hi))
                d_step = Decimal(str(step))
                span = d_hi - d_lo
                steps_exact = span / d_step
                if not steps_exact.is_finite() or steps_exact < 0:
                    raise ValueError(f"parameter {self.name!r} float grid is unrepresentable")
                if steps_exact >= Decimal(MAX_PARAM_GRID_SIZE):
                    raise ValueError(
                        f"parameter {self.name!r} float grid exceeds limit {MAX_PARAM_GRID_SIZE}"
                    )
                count = int(steps_exact.to_integral_value(rounding=ROUND_FLOOR)) + 1
                values: list[float] = []
                for idx in range(count):
                    val = float(d_lo + (d_step * idx))
                    if not _isfinite(val):
                        raise ValueError(f"parameter {self.name!r} grid point overflows float")
                    if val < lo:
                        val = lo
                    elif val > hi:
                        val = hi
                    values.append(0.0 if val == 0.0 else val)
                return tuple(values)
        return (self.default,)


@dataclass(frozen=True, slots=True)
class ParamSpace:
    """Immutable collection of parameter specifications and cross-parameter constraints."""

    specs: tuple[ParamSpec, ...] = ()
    constraints: tuple[Callable[[Mapping[str, Any]], None], ...] = ()

    def __post_init__(self) -> None:
        if type(self.specs) is not tuple:
            object.__setattr__(self, "specs", tuple(self.specs))
        if type(self.constraints) is not tuple:
            object.__setattr__(self, "constraints", tuple(self.constraints))
        seen: set[str] = set()
        for spec in self.specs:
            if not isinstance(spec, ParamSpec):
                raise ValueError("ParamSpace specs must be ParamSpec instances")
            if spec.name in seen:
                raise ValueError(f"duplicate parameter name in ParamSpace: {spec.name!r}")
            seen.add(spec.name)
        for constraint in self.constraints:
            if not callable(constraint):
                raise ValueError("ParamSpace constraints must be callable")

    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)

    def by_name(self) -> Mapping[str, ParamSpec]:
        return _MappingProxyType({spec.name: spec for spec in self.specs})

    def defaults(self) -> dict[str, Any]:
        return {spec.name: spec.default for spec in self.specs}

    def validate_params(self, params: Any) -> None:
        effective: dict[str, Any] = {}
        if isinstance(params, Mapping):
            for spec in self.specs:
                if spec.name in params:
                    val = params[spec.name]
                    spec.validate(val)
                    effective[spec.name] = val
                else:
                    effective[spec.name] = spec.default
        elif is_dataclass(params) or hasattr(params, "__dict__") or hasattr(params, "__slots__"):
            for spec in self.specs:
                if hasattr(params, spec.name):
                    val = getattr(params, spec.name)
                    spec.validate(val)
                    effective[spec.name] = val
                else:
                    effective[spec.name] = spec.default
        else:
            raise ValueError("params must be a mapping or parameter object")

        for constraint in self.constraints:
            constraint(effective)

    def is_valid(self, params: Any) -> bool:
        try:
            self.validate_params(params)
            return True
        except ValueError:
            return False

    def iter_valid_grid(self) -> Iterator[dict[str, Any]]:
        """Yield parameter dicts from the Cartesian grid satisfying all constraints."""
        names = tuple(spec.name for spec in self.specs)
        grids = tuple(spec.grid_values() for spec in self.specs)
        for combo in itertools.product(*grids):
            candidate = dict(zip(names, combo))
            if not self.constraints:
                yield candidate
                continue
            try:
                for constraint in self.constraints:
                    constraint(candidate)
            except ValueError:
                continue
            yield candidate


@runtime_checkable
class FastCursor(Protocol):
    """Documented strictly-causal O(1) cursor contract exposed optionally by fast engines.

    Implementers MUST maintain these invariants during every ``Strategy.on_bar(ctx)`` call:
      - ``bar_idx`` equals ``ctx.market.closed_bars(symbol, interval).end - 1`` with
        slice ``start == 0`` (never exposing bars past ``ctx.now_ms``).
      - ``pos_qty`` equals ``ctx.portfolio.position_qty(symbol)``.
      - The cursor is callback-scoped; engines enforce that retained live views
        cannot be read outside an active ``on_bar(ctx)`` invocation.
    """

    bar_idx: int
    pos_qty: float


@runtime_checkable
class FastCursorMarket(Protocol):
    """Optional fast-path MarketView extension bound to a single primary ``(symbol, interval)``.

    Calls to ``closed_bars()`` MUST still return fixed-bound immutable ``BarSeriesSlice``
    snapshots whose bounds do not mutate across subsequent bars.
    """

    _acct: FastCursor
    _p_sym: str
    _p_iv: str


@runtime_checkable
class Strategy(Protocol):
    """Single-source protocol driving backtest, walk-forward, shadow, and live.

    Callback lifetime contract:
      - Standard strategies (where ``getattr(strategy, "supports_ephemeral_context", False) is not True``)
        always receive point-in-time immutable ``BarContext``, ``PortfolioView``, and
        ``BarSeriesSlice`` snapshots that remain valid even if retained across bars.
      - Strategies that explicitly set Boolean ``supports_ephemeral_context = True``
        (strict ``is True``, not arbitrary truthy values) opt in to callback-scoped live
        context / ``FastCursor`` reuse and MUST NOT retain ``ctx``, ``ctx.market``, or
        ``ctx.portfolio`` across ``on_bar`` invocations. Returned ``BarSeriesSlice`` objects
        from ``closed_bars()`` always retain fixed bounds.
      - ``RiskOverlay`` instances opt in independently via ``supports_ephemeral_context = True``;
        a strategy's opt-in never grants permission to pass a mutable context to an unmarked overlay.
      - Engine ``reuse_bar_context`` contract:
        * ``reuse_bar_context=None`` (default): pass stable immutable snapshots to standard
          strategies; use callback-scoped live context only when ``supports_ephemeral_context is True``.
        * ``reuse_bar_context=False``: always pass stable immutable snapshots.
        * ``reuse_bar_context=True``: requires ``strategy.supports_ephemeral_context is True``
          and is validated before resetting strategy/overlay state or returning early on empty
          series, raising ``ValueError`` for unmarked strategies while allowing valid empty
          runs to return metrics.
    """

    strategy_id: str
    version: str
    supported_sides: tuple[str, ...]
    required_intervals: tuple[str, ...]
    warmup_bars: Mapping[str, int]
    params: Any

    def param_space(self) -> ParamSpace: ...

    def reset(self) -> None: ...

    def on_bar(self, ctx: BarContext) -> StrategyOutput: ...

    def snapshot_state(self) -> dict[str, Any]: ...

    def restore_state(self, state: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class StrategyFamilyDescriptor:
    """Registration metadata for a strategy family supporting buy, sell, or long_short."""

    family_id: str
    version: str
    display_name: str
    supported_sides: tuple[SideMode, ...]
    default_intervals: tuple[str, ...]
    factory: Callable[..., Strategy]
    param_space: ParamSpace
    description: str = ""
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.family_id, "family_id")
        _validate_non_empty_str(self.version, "version")
        _validate_non_empty_str(self.display_name, "display_name")

        sides = tuple(self.supported_sides)
        if not sides:
            raise ValueError("supported_sides must be non-empty")
        if len(sides) != len(set(sides)):
            raise ValueError("supported_sides must be unique")
        for side in sides:
            if side not in SUPPORTED_SIDE_MODES:
                raise ValueError(f"unsupported side mode: {side!r}")
        if type(self.supported_sides) is not tuple:
            object.__setattr__(self, "supported_sides", sides)

        intervals = tuple(self.default_intervals)
        if not intervals or any(not isinstance(iv, str) or not iv.strip() for iv in intervals):
            raise ValueError("default_intervals must be non-empty strings")
        if type(self.default_intervals) is not tuple:
            object.__setattr__(self, "default_intervals", intervals)

        if not callable(self.factory):
            raise ValueError("factory must be callable")
        if not isinstance(self.param_space, ParamSpace):
            raise ValueError("param_space must be a ParamSpace")
        if not isinstance(self.description, str):
            raise ValueError("description must be a string")

        aliases = tuple(self.aliases)
        if any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
            raise ValueError("aliases must be non-empty strings")
        if len(aliases) != len(set(aliases)):
            raise ValueError("aliases must be unique")
        if self.family_id in aliases:
            raise ValueError("aliases cannot contain family_id")
        if type(self.aliases) is not tuple:
            object.__setattr__(self, "aliases", aliases)


class StrategyFamilyRegistry:
    """Multi-family strategy registry with native buy, sell, and long_short support."""

    def __init__(self, descriptors: tuple[StrategyFamilyDescriptor, ...] = ()) -> None:
        self._by_id: dict[str, StrategyFamilyDescriptor] = {}
        self._by_key: dict[str, StrategyFamilyDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: StrategyFamilyDescriptor) -> None:
        if not isinstance(descriptor, StrategyFamilyDescriptor):
            raise ValueError("descriptor must be a StrategyFamilyDescriptor")
        keys = (descriptor.family_id, *descriptor.aliases)
        for key in keys:
            if key in self._by_key:
                raise ValueError(f"strategy family or alias already registered: {key!r}")
        self._by_id[descriptor.family_id] = descriptor
        for key in keys:
            self._by_key[key] = descriptor

    def get(self, name: str) -> StrategyFamilyDescriptor:
        try:
            return self._by_key[name]
        except KeyError as exc:
            raise ValueError(f"unknown strategy family: {name!r}") from exc

    def create(self, name: str, **kwargs: Any) -> Strategy:
        descriptor = self.get(name)
        return descriptor.factory(**kwargs)

    def list_families(self, *, side: SideMode | None = None) -> tuple[StrategyFamilyDescriptor, ...]:
        if side is not None:
            if side not in SUPPORTED_SIDE_MODES:
                raise ValueError(f"unsupported side filter: {side!r}")
            return tuple(d for d in self._by_id.values() if side in d.supported_sides)
        return tuple(self._by_id.values())

    def family_ids(self) -> tuple[str, ...]:
        return tuple(self._by_id.keys())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_key

    def __len__(self) -> int:
        return len(self._by_id)
