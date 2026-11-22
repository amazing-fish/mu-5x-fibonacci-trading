"""Layer 2 single-source Strategy protocol, execution intents, and multi-family registry.

Pure Python 3.12 stdlib only. Defines immutable data contracts shared by
Layer 2 strategies and the Layer 3 composable backtest/runtime engine.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, is_dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mu_strategy.core.instrument import InstrumentSpec
    from mu_strategy.market_data.bar_series import MarketView

__all__ = [
    "EMPTY_MAPPING",
    "EMPTY_OUTPUT",
    "OrderSide",
    "OrderType",
    "QtyMode",
    "SideMode",
    "SUPPORTED_ORDER_SIDES",
    "SUPPORTED_ORDER_TYPES",
    "SUPPORTED_QTY_MODES",
    "SUPPORTED_SIDE_MODES",
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
SideMode = Literal["buy", "sell", "long_short"]
ParamKind = Literal["int", "float", "bool", "categorical"]

SUPPORTED_ORDER_SIDES: frozenset[str] = frozenset({"buy", "sell"})
SUPPORTED_ORDER_TYPES: frozenset[str] = frozenset({"market", "limit", "stop_market", "stop_limit"})
SUPPORTED_QTY_MODES: frozenset[str] = frozenset({"target_weight", "equity_fraction", "contracts", "close_all"})
SUPPORTED_SIDE_MODES: frozenset[str] = frozenset({"buy", "sell", "long_short"})
_SUPPORTED_PARAM_KINDS: frozenset[str] = frozenset({"int", "float", "bool", "categorical"})

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

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.intent_id, "intent_id")
        _validate_non_empty_str(self.symbol, "symbol")
        if self.side not in SUPPORTED_ORDER_SIDES:
            raise ValueError(f"unsupported side: {self.side!r}")
        if self.order_type not in SUPPORTED_ORDER_TYPES:
            raise ValueError(f"unsupported order_type: {self.order_type!r}")
        if self.qty_mode not in SUPPORTED_QTY_MODES:
            raise ValueError(f"unsupported qty_mode: {self.qty_mode!r}")

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

        if type(self.positions) is not _MappingProxyType:
            if not isinstance(self.positions, Mapping):
                raise ValueError("positions must be a mapping")
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

    def __post_init__(self) -> None:
        if isinstance(self.now_ms, bool) or not isinstance(self.now_ms, int) or self.now_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")
        _validate_non_empty_str(self.symbol, "symbol")
        if not isinstance(self.portfolio, PortfolioView):
            raise ValueError("portfolio must be a PortfolioView")


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


@dataclass(frozen=True, slots=True)
class StrategyOutput:
    """Combined order intents, target weights, and diagnostics from ``on_bar``."""

    intents: tuple[OrderIntent, ...] = ()
    targets: tuple[TargetPosition, ...] = ()
    diagnostics: Mapping[str, Any] = EMPTY_MAPPING

    def __post_init__(self) -> None:
        if type(self.intents) is not tuple:
            object.__setattr__(self, "intents", tuple(self.intents))
        if type(self.targets) is not tuple:
            object.__setattr__(self, "targets", tuple(self.targets))
        diag = self.diagnostics
        if diag is not EMPTY_MAPPING and type(diag) is not _MappingProxyType:
            if not isinstance(diag, Mapping):
                raise ValueError("diagnostics must be a mapping")
            object.__setattr__(self, "diagnostics", _MappingProxyType(dict(diag)))

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

        if self.kind in ("int", "float"):
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

        self.validate(self.default)

    def validate(self, value: Any) -> None:
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

        if self.choices is not None and value not in self.choices:
            raise ValueError(f"parameter {self.name!r} must be one of {self.choices!r}")

    def grid_values(self) -> tuple[Any, ...]:
        if self.choices is not None:
            return self.choices
        if self.kind == "bool":
            return (False, True)
        if self.kind == "int" and self.min_value is not None and self.max_value is not None:
            step = int(self.step) if self.step is not None else 1
            return tuple(range(int(self.min_value), int(self.max_value) + 1, step))
        if self.kind == "float" and self.min_value is not None and self.max_value is not None and self.step is not None:
            values: list[float] = []
            lo = float(self.min_value)
            hi = float(self.max_value)
            step = float(self.step)
            idx = 0
            while True:
                val = lo + idx * step
                if val > hi + (step * 1e-9):
                    break
                values.append(round(min(val, hi), 12))
                idx += 1
            return tuple(values)
        return (self.default,)


@dataclass(frozen=True, slots=True)
class ParamSpace:
    """Immutable collection of parameter specifications for a strategy family."""

    specs: tuple[ParamSpec, ...] = ()

    def __post_init__(self) -> None:
        if type(self.specs) is not tuple:
            object.__setattr__(self, "specs", tuple(self.specs))
        seen: set[str] = set()
        for spec in self.specs:
            if not isinstance(spec, ParamSpec):
                raise ValueError("ParamSpace specs must be ParamSpec instances")
            if spec.name in seen:
                raise ValueError(f"duplicate parameter name in ParamSpace: {spec.name!r}")
            seen.add(spec.name)

    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)

    def by_name(self) -> Mapping[str, ParamSpec]:
        return _MappingProxyType({spec.name: spec for spec in self.specs})

    def defaults(self) -> dict[str, Any]:
        return {spec.name: spec.default for spec in self.specs}

    def validate_params(self, params: Any) -> None:
        if isinstance(params, Mapping):
            for spec in self.specs:
                if spec.name in params:
                    spec.validate(params[spec.name])
            return
        if is_dataclass(params) or hasattr(params, "__dict__") or hasattr(params, "__slots__"):
            for spec in self.specs:
                if hasattr(params, spec.name):
                    spec.validate(getattr(params, spec.name))
            return
        raise ValueError("params must be a mapping or parameter object")


@runtime_checkable
class FastCursor(Protocol):
    """Documented strictly-causal O(1) cursor contract exposed optionally by fast engines.

    Implementers MUST maintain these invariants on every ``Strategy.on_bar(ctx)`` call:
      - ``bar_idx`` equals ``ctx.market.closed_bars(symbol, interval).end - 1`` with
        slice ``start == 0`` (never exposing bars past ``ctx.now_ms``).
      - ``pos_qty`` equals ``ctx.portfolio.position_qty(symbol)``.
    """

    bar_idx: int
    pos_qty: float


@runtime_checkable
class FastCursorMarket(Protocol):
    """Optional fast-path MarketView extension bound to a single primary ``(symbol, interval)``."""

    _acct: FastCursor
    _p_sym: str
    _p_iv: str


@runtime_checkable
class Strategy(Protocol):
    """Single-source protocol driving backtest, walk-forward, shadow, and live."""

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
