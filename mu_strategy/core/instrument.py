"""Venue-agnostic instrument specification and trading cost / slippage models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math
from typing import Any, Literal


AssetClass = Literal["perp", "spot", "equity"]
RoundingMode = Literal["down", "up", "nearest"]

VALID_ASSET_CLASSES: frozenset[str] = frozenset({"perp", "spot", "equity"})
_VALID_ROUNDING_MODES: frozenset[str] = frozenset({"down", "up", "nearest"})
_HOUR_MS = 3_600_000


def _non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty string without surrounding whitespace")
    return value


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive decimal")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite positive decimal") from exc
    if not dec.is_finite() or dec <= 0:
        raise ValueError(f"{label} must be a finite positive decimal")
    return dec


def _non_negative_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite non-negative decimal")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite non-negative decimal") from exc
    if not dec.is_finite() or dec < 0:
        raise ValueError(f"{label} must be a finite non-negative decimal")
    return Decimal("0") if dec == 0 else dec


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return 0.0 if result == 0.0 else result


def _positive_finite_float(value: Any, label: str) -> float:
    result = _finite_float(value, label)
    if result <= 0.0:
        raise ValueError(f"{label} must be positive")
    return result


def _non_negative_finite_float(value: Any, label: str) -> float:
    result = _finite_float(value, label)
    if result < 0.0:
        raise ValueError(f"{label} must be non-negative")
    return result


def _exact_round_fraction(frac: Fraction, mode: RoundingMode) -> int:
    if mode not in _VALID_ROUNDING_MODES:
        raise ValueError(f"unsupported rounding mode: {mode!r}")
    q, rem = divmod(frac.numerator, frac.denominator)
    if rem == 0 or mode == "down":
        return q
    if mode == "up":
        return q + 1
    twice_rem = rem * 2
    if twice_rem > frac.denominator:
        return q + 1
    if twice_rem < frac.denominator:
        return q
    return q if (q % 2 == 0) else q + 1


def _exact_mul_units_step(units: int, step: Decimal) -> Decimal:
    if units == 0:
        return Decimal("0")
    tup = step.as_tuple()
    assert isinstance(tup.exponent, int)
    step_coeff = 0
    for digit in tup.digits:
        step_coeff = (step_coeff * 10) + digit
    prod = abs(units) * step_coeff
    if prod == 0:
        return Decimal("0")
    sign = (1 if units < 0 else 0) ^ tup.sign
    digits = tuple(int(ch) for ch in str(prod))
    return Decimal((sign, digits, tup.exponent))


def _quantize_to_step(value: Decimal, step: Decimal, mode: RoundingMode) -> Decimal:
    frac = Fraction(value) / Fraction(step)
    units = _exact_round_fraction(frac, mode)
    return _exact_mul_units_step(units, step)


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Venue-agnostic instrument specification with exact Decimal sizing rules."""

    instrument_id: str
    venue: str
    symbol: str
    asset_class: AssetClass
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal
    contract_value: Decimal
    max_leverage: float
    calendar_id: str | None = None
    funding_interval_hours: int | None = 8

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _non_empty_str(self.instrument_id, "instrument_id"))
        object.__setattr__(self, "venue", _non_empty_str(self.venue, "venue"))
        object.__setattr__(self, "symbol", _non_empty_str(self.symbol, "symbol"))
        if self.asset_class not in VALID_ASSET_CLASSES:
            raise ValueError(
                f"asset_class must be one of {sorted(VALID_ASSET_CLASSES)}, got {self.asset_class!r}"
            )
        object.__setattr__(self, "tick_size", _positive_decimal(self.tick_size, "tick_size"))
        object.__setattr__(self, "lot_size", _positive_decimal(self.lot_size, "lot_size"))
        object.__setattr__(self, "min_size", _positive_decimal(self.min_size, "min_size"))
        object.__setattr__(self, "contract_value", _positive_decimal(self.contract_value, "contract_value"))
        object.__setattr__(self, "max_leverage", _positive_finite_float(self.max_leverage, "max_leverage"))
        if self.calendar_id is not None:
            object.__setattr__(self, "calendar_id", _non_empty_str(self.calendar_id, "calendar_id"))
        if self.asset_class in ("spot", "equity"):
            if self.funding_interval_hours is not None:
                raise ValueError(
                    f"funding_interval_hours must be None for asset_class={self.asset_class!r}"
                )
        elif self.funding_interval_hours is not None:
            if isinstance(self.funding_interval_hours, bool) or not isinstance(self.funding_interval_hours, int):
                raise ValueError("funding_interval_hours must be a positive integer or None")
            if self.funding_interval_hours <= 0:
                raise ValueError("funding_interval_hours must be positive")

    @property
    def is_perp(self) -> bool:
        return self.asset_class == "perp"

    @property
    def funding_interval_ms(self) -> int | None:
        if self.funding_interval_hours is None:
            return None
        return self.funding_interval_hours * _HOUR_MS

    @property
    def tick_size_float(self) -> float:
        return float(self.tick_size)

    @property
    def lot_size_float(self) -> float:
        return float(self.lot_size)

    @property
    def min_size_float(self) -> float:
        return float(self.min_size)

    @property
    def contract_value_float(self) -> float:
        return float(self.contract_value)

    def round_price(
        self,
        price: float | str | Decimal,
        *,
        rounding: RoundingMode = "nearest",
    ) -> Decimal:
        value = _positive_decimal(price, "price")
        rounded = _quantize_to_step(value, self.tick_size, rounding)
        if rounded <= 0:
            raise ValueError("rounded price must be positive")
        return rounded

    def round_size(
        self,
        size: float | str | Decimal,
        *,
        rounding: RoundingMode = "down",
        enforce_min: bool = False,
    ) -> Decimal:
        value = _non_negative_decimal(size, "size")
        rounded = _quantize_to_step(value, self.lot_size, rounding)
        if enforce_min and rounded < self.min_size:
            return Decimal("0")
        return rounded

    def contracts_for_notional(
        self,
        notional: float | str | Decimal,
        *,
        price: float | str | Decimal,
        enforce_min: bool = False,
    ) -> Decimal:
        notional_val = _non_negative_decimal(notional, "notional")
        price_val = _positive_decimal(price, "price")
        raw_lots = Fraction(notional_val) / (
            Fraction(price_val) * Fraction(self.contract_value) * Fraction(self.lot_size)
        )
        units = _exact_round_fraction(raw_lots, "down")
        rounded = _exact_mul_units_step(units, self.lot_size)
        if enforce_min and rounded < self.min_size:
            return Decimal("0")
        return rounded

    def base_qty_for_contracts(self, contracts: float | str | Decimal) -> Decimal:
        dec = _non_negative_decimal(contracts, "contracts")
        return dec * self.contract_value

    def notional_for_contracts(
        self,
        contracts: float | str | Decimal,
        *,
        price: float | str | Decimal,
    ) -> Decimal:
        dec = _non_negative_decimal(contracts, "contracts")
        price_val = _positive_decimal(price, "price")
        return dec * self.contract_value * price_val

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "tick_size": format(self.tick_size, "f"),
            "lot_size": format(self.lot_size, "f"),
            "min_size": format(self.min_size, "f"),
            "contract_value": format(self.contract_value, "f"),
            "max_leverage": self.max_leverage,
            "calendar_id": self.calendar_id,
            "funding_interval_hours": self.funding_interval_hours,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InstrumentSpec":
        try:
            return cls(
                instrument_id=data["instrument_id"],
                venue=data["venue"],
                symbol=data["symbol"],
                asset_class=data["asset_class"],
                tick_size=Decimal(str(data["tick_size"])),
                lot_size=Decimal(str(data["lot_size"])),
                min_size=Decimal(str(data["min_size"])),
                contract_value=Decimal(str(data["contract_value"])),
                max_leverage=data["max_leverage"],
                calendar_id=data.get("calendar_id"),
                funding_interval_hours=data.get("funding_interval_hours"),
            )
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"invalid InstrumentSpec payload: {exc}") from exc


def okx_swap_spec(
    symbol: str,
    *,
    tick_size: Decimal | str | float = Decimal("0.01"),
    lot_size: Decimal | str | float = Decimal("0.01"),
    min_size: Decimal | str | float | None = None,
    contract_value: Decimal | str | float = Decimal("1"),
    max_leverage: float = 5.0,
    calendar_id: str | None = None,
    funding_interval_hours: int = 8,
) -> InstrumentSpec:
    """Build an OKX USDT perpetual swap InstrumentSpec."""
    clean_symbol = _non_empty_str(symbol, "symbol")
    resolved_min = lot_size if min_size is None else min_size
    return InstrumentSpec(
        instrument_id=f"okx:{clean_symbol}",
        venue="okx",
        symbol=clean_symbol,
        asset_class="perp",
        tick_size=_positive_decimal(tick_size, "tick_size"),
        lot_size=_positive_decimal(lot_size, "lot_size"),
        min_size=_positive_decimal(resolved_min, "min_size"),
        contract_value=_positive_decimal(contract_value, "contract_value"),
        max_leverage=max_leverage,
        calendar_id=calendar_id,
        funding_interval_hours=funding_interval_hours,
    )


def binance_perp_spec(
    symbol: str,
    *,
    tick_size: Decimal | str | float = Decimal("0.01"),
    lot_size: Decimal | str | float = Decimal("0.001"),
    min_size: Decimal | str | float | None = None,
    contract_value: Decimal | str | float = Decimal("1"),
    max_leverage: float = 20.0,
    calendar_id: str | None = None,
    funding_interval_hours: int = 8,
) -> InstrumentSpec:
    """Build a Binance USD-M perpetual futures InstrumentSpec."""
    clean_symbol = _non_empty_str(symbol, "symbol")
    resolved_min = lot_size if min_size is None else min_size
    return InstrumentSpec(
        instrument_id=f"binance:{clean_symbol}",
        venue="binance",
        symbol=clean_symbol,
        asset_class="perp",
        tick_size=_positive_decimal(tick_size, "tick_size"),
        lot_size=_positive_decimal(lot_size, "lot_size"),
        min_size=_positive_decimal(resolved_min, "min_size"),
        contract_value=_positive_decimal(contract_value, "contract_value"),
        max_leverage=max_leverage,
        calendar_id=calendar_id,
        funding_interval_hours=funding_interval_hours,
    )


def spot_spec(
    symbol: str,
    *,
    venue: str = "binance",
    tick_size: Decimal | str | float = Decimal("0.01"),
    lot_size: Decimal | str | float = Decimal("0.0001"),
    min_size: Decimal | str | float | None = None,
    contract_value: Decimal | str | float = Decimal("1"),
    max_leverage: float = 1.0,
    calendar_id: str | None = None,
    asset_class: Literal["spot", "equity"] = "spot",
) -> InstrumentSpec:
    """Build a spot or cash-equity InstrumentSpec (no perpetual funding)."""
    clean_venue = _non_empty_str(venue, "venue")
    clean_symbol = _non_empty_str(symbol, "symbol")
    if asset_class not in ("spot", "equity"):
        raise ValueError("spot_spec asset_class must be 'spot' or 'equity'")
    resolved_min = lot_size if min_size is None else min_size
    return InstrumentSpec(
        instrument_id=f"{clean_venue}:{clean_symbol}",
        venue=clean_venue,
        symbol=clean_symbol,
        asset_class=asset_class,
        tick_size=_positive_decimal(tick_size, "tick_size"),
        lot_size=_positive_decimal(lot_size, "lot_size"),
        min_size=_positive_decimal(resolved_min, "min_size"),
        contract_value=_positive_decimal(contract_value, "contract_value"),
        max_leverage=max_leverage,
        calendar_id=calendar_id,
        funding_interval_hours=None,
    )


@dataclass(frozen=True, slots=True)
class FixedTicks:
    """Fixed adverse slippage measured in instrument ticks."""

    ticks: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticks", _non_negative_finite_float(self.ticks, "ticks"))

    def slippage_price(
        self,
        price: float,
        *,
        tick_size: float,
        atr: float | None = None,
        order_notional: float = 0.0,
        bar_usd_volume: float = 0.0,
    ) -> float:
        del price, atr, order_notional, bar_usd_volume
        if self.ticks == 0.0:
            return 0.0
        step = _positive_finite_float(tick_size, "tick_size")
        return self.ticks * step

    def scaled(self, multiplier: float) -> "FixedTicks":
        mult = _non_negative_finite_float(multiplier, "multiplier")
        return FixedTicks(ticks=self.ticks * mult)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "fixed_ticks", "ticks": self.ticks}


@dataclass(frozen=True, slots=True)
class AtrBps:
    """Volatility-proportional slippage: ``atr * (bps_per_atr / 10_000)``.

    Equivalently ``price * ((atr / price) * bps_per_atr / 10_000)``, so
    ``bps_per_atr=500.0`` represents 5% of one ATR.
    """

    bps_per_atr: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "bps_per_atr", _non_negative_finite_float(self.bps_per_atr, "bps_per_atr"))

    def slippage_price(
        self,
        price: float,
        *,
        tick_size: float,
        atr: float | None = None,
        order_notional: float = 0.0,
        bar_usd_volume: float = 0.0,
    ) -> float:
        del price, tick_size, order_notional, bar_usd_volume
        if self.bps_per_atr == 0.0 or atr is None or not math.isfinite(atr) or atr <= 0.0:
            return 0.0
        return atr * (self.bps_per_atr / 10_000.0)

    def scaled(self, multiplier: float) -> "AtrBps":
        mult = _non_negative_finite_float(multiplier, "multiplier")
        return AtrBps(bps_per_atr=self.bps_per_atr * mult)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "atr_bps", "bps_per_atr": self.bps_per_atr}


@dataclass(frozen=True, slots=True)
class SqrtVolumeImpact:
    """Square-root volume participation impact model.

    ``impact_bps = min(max_bps, impact_coeff * sqrt(order_notional / bar_usd_volume))``
    where ``impact_coeff`` is in basis points at 100% bar-volume participation.
    Zero or non-positive ``bar_usd_volume`` with positive ``order_notional``
    conservatively saturates at ``max_bps``.
    """

    impact_coeff: float
    max_bps: float = 100.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "impact_coeff", _non_negative_finite_float(self.impact_coeff, "impact_coeff"))
        object.__setattr__(self, "max_bps", _non_negative_finite_float(self.max_bps, "max_bps"))

    def slippage_price(
        self,
        price: float,
        *,
        tick_size: float,
        atr: float | None = None,
        order_notional: float = 0.0,
        bar_usd_volume: float = 0.0,
    ) -> float:
        del tick_size, atr
        px = _positive_finite_float(price, "price")
        if self.impact_coeff == 0.0 or self.max_bps == 0.0:
            return 0.0
        if not math.isfinite(order_notional) or order_notional <= 0.0:
            return 0.0
        if not math.isfinite(bar_usd_volume) or bar_usd_volume <= 0.0:
            impact_bps = self.max_bps
        else:
            participation = order_notional / bar_usd_volume
            impact_bps = min(self.max_bps, self.impact_coeff * math.sqrt(participation))
        return px * (impact_bps / 10_000.0)

    def scaled(self, multiplier: float) -> "SqrtVolumeImpact":
        mult = _non_negative_finite_float(multiplier, "multiplier")
        return SqrtVolumeImpact(
            impact_coeff=self.impact_coeff * mult,
            max_bps=self.max_bps * mult,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "sqrt_volume_impact",
            "impact_coeff": self.impact_coeff,
            "max_bps": self.max_bps,
        }


SlippageModel = FixedTicks | AtrBps | SqrtVolumeImpact
_SLIPPAGE_TYPES = (FixedTicks, AtrBps, SqrtVolumeImpact)


def slippage_model_from_dict(data: Mapping[str, Any]) -> SlippageModel:
    kind = data.get("type")
    if kind == "fixed_ticks":
        return FixedTicks(ticks=data["ticks"])
    if kind == "atr_bps":
        return AtrBps(bps_per_atr=data["bps_per_atr"])
    if kind == "sqrt_volume_impact":
        return SqrtVolumeImpact(
            impact_coeff=data["impact_coeff"],
            max_bps=data.get("max_bps", 100.0),
        )
    raise ValueError(f"unknown slippage model type: {kind!r}")


@dataclass(frozen=True, slots=True)
class CostModel:
    """Explicit trading friction, margin, liquidation, and funding-sensitivity model."""

    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0
    half_spread_bps: float = 1.0
    slippage_model: SlippageModel = FixedTicks(0.0)
    stop_slippage_multiplier: float = 1.5
    maintenance_margin_rate: float = 0.01
    liquidation_fee_bps: float = 5.0
    missing_funding_apr_band: tuple[float, ...] = (0.0, 0.15, 0.30)

    def __post_init__(self) -> None:
        object.__setattr__(self, "maker_fee_bps", _non_negative_finite_float(self.maker_fee_bps, "maker_fee_bps"))
        object.__setattr__(self, "taker_fee_bps", _non_negative_finite_float(self.taker_fee_bps, "taker_fee_bps"))
        object.__setattr__(self, "half_spread_bps", _non_negative_finite_float(self.half_spread_bps, "half_spread_bps"))
        if not isinstance(self.slippage_model, _SLIPPAGE_TYPES):
            raise TypeError(
                "slippage_model must be FixedTicks, AtrBps, or SqrtVolumeImpact, "
                f"got {type(self.slippage_model).__name__}"
            )
        object.__setattr__(
            self,
            "stop_slippage_multiplier",
            _non_negative_finite_float(self.stop_slippage_multiplier, "stop_slippage_multiplier"),
        )
        mmr = _non_negative_finite_float(self.maintenance_margin_rate, "maintenance_margin_rate")
        if mmr >= 1.0:
            raise ValueError("maintenance_margin_rate must be in [0.0, 1.0)")
        object.__setattr__(self, "maintenance_margin_rate", mmr)
        object.__setattr__(
            self,
            "liquidation_fee_bps",
            _non_negative_finite_float(self.liquidation_fee_bps, "liquidation_fee_bps"),
        )
        if isinstance(self.missing_funding_apr_band, (str, bytes)) or not isinstance(
            self.missing_funding_apr_band, Sequence
        ):
            raise ValueError("missing_funding_apr_band must be a non-empty sequence of finite floats")
        band = tuple(_finite_float(v, "missing_funding_apr_band entry") for v in self.missing_funding_apr_band)
        if not band:
            raise ValueError("missing_funding_apr_band must not be empty")
        object.__setattr__(self, "missing_funding_apr_band", band)

    @property
    def maker_fee_rate(self) -> float:
        return self.maker_fee_bps / 10_000.0

    @property
    def taker_fee_rate(self) -> float:
        return self.taker_fee_bps / 10_000.0

    @property
    def half_spread_rate(self) -> float:
        return self.half_spread_bps / 10_000.0

    @property
    def liquidation_fee_rate(self) -> float:
        return self.liquidation_fee_bps / 10_000.0

    def half_spread_price(self, price: float) -> float:
        px = _positive_finite_float(price, "price")
        return px * self.half_spread_rate

    def estimate_slippage_price(
        self,
        price: float,
        *,
        tick_size: float,
        atr: float | None = None,
        order_notional: float = 0.0,
        bar_usd_volume: float = 0.0,
        is_stop: bool = False,
    ) -> float:
        base = self.slippage_model.slippage_price(
            price,
            tick_size=tick_size,
            atr=atr,
            order_notional=order_notional,
            bar_usd_volume=bar_usd_volume,
        )
        if is_stop:
            return base * self.stop_slippage_multiplier
        return base

    def scaled(self, multiplier: float) -> "CostModel":
        """Return a cost-stressed copy scaling fees, spread, and slippage by ``multiplier``."""
        mult = _non_negative_finite_float(multiplier, "multiplier")
        return CostModel(
            maker_fee_bps=self.maker_fee_bps * mult,
            taker_fee_bps=self.taker_fee_bps * mult,
            half_spread_bps=self.half_spread_bps * mult,
            slippage_model=self.slippage_model.scaled(mult),
            stop_slippage_multiplier=self.stop_slippage_multiplier,
            maintenance_margin_rate=self.maintenance_margin_rate,
            liquidation_fee_bps=self.liquidation_fee_bps,
            missing_funding_apr_band=self.missing_funding_apr_band,
        )

    @classmethod
    def zero_cost(cls) -> "CostModel":
        return cls(
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            half_spread_bps=0.0,
            slippage_model=FixedTicks(0.0),
            stop_slippage_multiplier=1.0,
            maintenance_margin_rate=0.0,
            liquidation_fee_bps=0.0,
        )

    @classmethod
    def okx_default(cls) -> "CostModel":
        return cls(
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            half_spread_bps=1.0,
            slippage_model=FixedTicks(1.0),
            stop_slippage_multiplier=1.5,
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
        )

    @classmethod
    def binance_default(cls) -> "CostModel":
        return cls(
            maker_fee_bps=2.0,
            taker_fee_bps=4.0,
            half_spread_bps=0.5,
            slippage_model=FixedTicks(1.0),
            stop_slippage_multiplier=1.5,
            maintenance_margin_rate=0.005,
            liquidation_fee_bps=5.0,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "maker_fee_bps": self.maker_fee_bps,
            "taker_fee_bps": self.taker_fee_bps,
            "half_spread_bps": self.half_spread_bps,
            "slippage_model": self.slippage_model.to_dict(),
            "stop_slippage_multiplier": self.stop_slippage_multiplier,
            "maintenance_margin_rate": self.maintenance_margin_rate,
            "liquidation_fee_bps": self.liquidation_fee_bps,
            "missing_funding_apr_band": list(self.missing_funding_apr_band),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CostModel":
        try:
            return cls(
                maker_fee_bps=data["maker_fee_bps"],
                taker_fee_bps=data["taker_fee_bps"],
                half_spread_bps=data["half_spread_bps"],
                slippage_model=slippage_model_from_dict(data["slippage_model"]),
                stop_slippage_multiplier=data.get("stop_slippage_multiplier", 1.5),
                maintenance_margin_rate=data.get("maintenance_margin_rate", 0.01),
                liquidation_fee_bps=data.get("liquidation_fee_bps", 5.0),
                missing_funding_apr_band=tuple(data.get("missing_funding_apr_band", (0.0, 0.15, 0.30))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid CostModel payload: {exc}") from exc


__all__ = [
    "AssetClass",
    "AtrBps",
    "CostModel",
    "FixedTicks",
    "InstrumentSpec",
    "RoundingMode",
    "SlippageModel",
    "SqrtVolumeImpact",
    "VALID_ASSET_CLASSES",
    "binance_perp_spec",
    "okx_swap_spec",
    "slippage_model_from_dict",
    "spot_spec",
]
