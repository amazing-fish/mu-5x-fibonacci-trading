from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any


@dataclass(frozen=True)
class OKXInstrumentSpec:
    """Public OKX sizing metadata with deterministic floor-to-step semantics."""

    inst_id: str
    tick_size: Decimal
    lot_size: Decimal
    contract_value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.inst_id, str) or not self.inst_id:
            raise ValueError("inst_id must be non-empty")
        for field_name in ("tick_size", "lot_size", "contract_value"):
            value = _positive_decimal(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "OKXInstrumentSpec":
        try:
            return cls(
                inst_id=str(row["instId"]),
                tick_size=Decimal(str(row["tickSz"])),
                lot_size=Decimal(str(row["lotSz"])),
                contract_value=Decimal(str(row["ctVal"])),
            )
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid public instrument metadata: {exc}") from exc

    def price_to_string(self, price: float | str | Decimal) -> str:
        value = _positive_decimal(price, "price")
        rounded = _floor_to_step(value, self.tick_size)
        if rounded <= 0:
            raise ValueError("rounded price must be positive")
        return _decimal_to_string(rounded)

    def size_to_string(self, size: float | str | Decimal) -> str:
        value = _positive_decimal(size, "size")
        rounded = _floor_to_step(value, self.lot_size)
        if rounded <= 0:
            raise ValueError("rounded size must be positive")
        return _decimal_to_string(rounded)

    def size_for_notional(
        self,
        notional_usdt: float | str | Decimal,
        *,
        price: float | str | Decimal,
    ) -> str:
        notional_value = _positive_decimal(notional_usdt, "notional_usdt")
        price_value = _positive_decimal(price, "price")
        raw_size = notional_value / (price_value * self.contract_value)
        return self.size_to_string(raw_size)


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive decimal")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a finite positive decimal") from exc
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise ValueError(f"{label} must be a finite positive decimal")
    return decimal_value


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _decimal_to_string(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")
