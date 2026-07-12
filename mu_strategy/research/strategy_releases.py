from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping

from mu_strategy.canonical import canonical_sha256
from mu_strategy.strategy import StrategyConfig


STRATEGY_CONFIG_SCHEMA_VERSION = 1
STRATEGY_CONFIG_V1_FIELD_NAMES = (
    "symbol",
    "entry_execution",
    "second_pullback_wait_bars",
    "stop_tightening",
    "leverage",
    "margin_steps",
    "initial_stop_pct",
    "add_thresholds",
    "rsi_floor",
    "rsi_add_floor",
    "fib_tolerance_pct",
    "fee_profile",
    "fee_rate",
    "fib_lookback",
    "stop_buffer_pct",
    "stop_transition_bars",
    "stop_transition_curve",
    "max_entry_above_fib_pct",
    "yellow_max_entry_above_fib_pct",
    "max_signal_range_pct",
    "max_entry_above_signal_close_pct",
    "block_reverse_fib_resistance",
    "reverse_fib_lookback",
    "reverse_fib_tolerance_pct",
    "yellow_stop_tightening",
    "green_stop_tightening",
    "green_wide_stop_buffer_pct",
    "allowed_regimes",
    "full_size_regime",
    "trading_windows_bjt",
    "trading_windows_et",
)
_DECIMAL_FIELDS = {
    "leverage",
    "initial_stop_pct",
    "rsi_floor",
    "rsi_add_floor",
    "fib_tolerance_pct",
    "fee_rate",
    "stop_buffer_pct",
    "max_entry_above_fib_pct",
    "yellow_max_entry_above_fib_pct",
    "max_signal_range_pct",
    "max_entry_above_signal_close_pct",
    "reverse_fib_tolerance_pct",
    "green_wide_stop_buffer_pct",
}
_OPTIONAL_DECIMAL_FIELDS = {
    "max_entry_above_fib_pct",
    "yellow_max_entry_above_fib_pct",
    "max_signal_range_pct",
    "max_entry_above_signal_close_pct",
}
_DECIMAL_TUPLE_FIELDS = {"margin_steps", "add_thresholds"}
_INTEGER_FIELDS = {
    "second_pullback_wait_bars",
    "fib_lookback",
    "stop_transition_bars",
    "reverse_fib_lookback",
}
_BOOLEAN_FIELDS = {"block_reverse_fib_resistance"}
_OPTIONAL_TEXT_FIELDS = {"yellow_stop_tightening", "green_stop_tightening"}
_TEXT_TUPLE_FIELDS = {"allowed_regimes"}
_WINDOW_TUPLE_FIELDS = {"trading_windows_bjt", "trading_windows_et"}


class StrategyReleaseSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyConfigPayloadV1:
    values: Mapping[str, Any]
    schema_version: int = STRATEGY_CONFIG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_CONFIG_SCHEMA_VERSION:
            raise StrategyReleaseSchemaError(f"unsupported strategy config schema_version: {self.schema_version}")
        normalized = _validate_config_values(dict(self.values))
        object.__setattr__(self, "values", MappingProxyType(normalized))

    @property
    def strategy_config_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    @classmethod
    def from_config(cls, config: StrategyConfig) -> "StrategyConfigPayloadV1":
        actual_fields = tuple(field.name for field in dataclass_fields(StrategyConfig))
        if actual_fields != STRATEGY_CONFIG_V1_FIELD_NAMES:
            raise StrategyReleaseSchemaError("StrategyConfig fields changed without a v1 schema decision")
        encoded = {
            field_name: _encode_config_value(field_name, getattr(config, field_name))
            for field_name in STRATEGY_CONFIG_V1_FIELD_NAMES
        }
        return cls(encoded)

    @classmethod
    def from_dict(cls, payload: Any) -> "StrategyConfigPayloadV1":
        if not isinstance(payload, dict):
            raise StrategyReleaseSchemaError("strategy config payload must be an object")
        actual = set(payload)
        expected = {"schema_version", "fields"}
        if unknown := actual - expected:
            raise StrategyReleaseSchemaError(f"strategy config payload has unknown fields: {sorted(unknown)}")
        if missing := expected - actual:
            raise StrategyReleaseSchemaError(f"strategy config payload has missing fields: {sorted(missing)}")
        if not isinstance(payload["fields"], dict):
            raise StrategyReleaseSchemaError("strategy config fields must be an object")
        return cls(values=payload["fields"], schema_version=payload["schema_version"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fields": {name: _wire_value(value) for name, value in self.values.items()},
        }

    def to_strategy_config(self) -> StrategyConfig:
        decoded = {
            field_name: _decode_config_value(field_name, value)
            for field_name, value in self.values.items()
        }
        return StrategyConfig(**decoded)


def _validate_config_values(values: dict[str, Any]) -> dict[str, Any]:
    actual = set(values)
    expected = set(STRATEGY_CONFIG_V1_FIELD_NAMES)
    if unknown := actual - expected:
        raise StrategyReleaseSchemaError(f"strategy config has unknown fields: {sorted(unknown)}")
    if missing := expected - actual:
        raise StrategyReleaseSchemaError(f"strategy config has missing fields: {sorted(missing)}")
    return {
        field_name: _validate_config_value(field_name, values[field_name])
        for field_name in STRATEGY_CONFIG_V1_FIELD_NAMES
    }


def _validate_config_value(field_name: str, value: Any) -> Any:
    if field_name in _DECIMAL_FIELDS:
        if value is None and field_name in _OPTIONAL_DECIMAL_FIELDS:
            return None
        return _require_canonical_decimal(value, field_name)
    if field_name in _DECIMAL_TUPLE_FIELDS:
        if not isinstance(value, (list, tuple)) or not value:
            raise StrategyReleaseSchemaError(f"strategy config {field_name} must be a non-empty list")
        return tuple(_require_canonical_decimal(item, field_name) for item in value)
    if field_name in _INTEGER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise StrategyReleaseSchemaError(f"strategy config {field_name} must be an integer")
        return value
    if field_name in _BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise StrategyReleaseSchemaError(f"strategy config {field_name} must be a boolean")
        return value
    if field_name in _OPTIONAL_TEXT_FIELDS:
        if value is not None and (not isinstance(value, str) or not value):
            raise StrategyReleaseSchemaError(f"strategy config {field_name} must be text or null")
        return value
    if field_name in _TEXT_TUPLE_FIELDS:
        if not isinstance(value, (list, tuple)) or not value or not all(isinstance(item, str) and item for item in value):
            raise StrategyReleaseSchemaError(f"strategy config {field_name} must be a non-empty text list")
        return tuple(value)
    if field_name in _WINDOW_TUPLE_FIELDS:
        if not isinstance(value, (list, tuple)) or not value:
            raise StrategyReleaseSchemaError(f"strategy config {field_name} must be a non-empty window list")
        windows: list[tuple[str, str]] = []
        for window in value:
            if not isinstance(window, (list, tuple)) or len(window) != 2 or not all(isinstance(item, str) and item for item in window):
                raise StrategyReleaseSchemaError(f"strategy config {field_name} contains an invalid window")
            windows.append((window[0], window[1]))
        return tuple(windows)
    if not isinstance(value, str) or not value:
        raise StrategyReleaseSchemaError(f"strategy config {field_name} must be non-empty text")
    return value


def _encode_config_value(field_name: str, value: Any) -> Any:
    if field_name in _DECIMAL_FIELDS:
        return None if value is None else _decimal_text(value)
    if field_name in _DECIMAL_TUPLE_FIELDS:
        return tuple(_decimal_text(item) for item in value)
    if isinstance(value, tuple):
        return tuple(tuple(item) if isinstance(item, tuple) else item for item in value)
    return value


def _decode_config_value(field_name: str, value: Any) -> Any:
    if field_name in _DECIMAL_FIELDS:
        return None if value is None else float(value)
    if field_name in _DECIMAL_TUPLE_FIELDS:
        return tuple(float(item) for item in value)
    return value


def _require_canonical_decimal(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise StrategyReleaseSchemaError(f"strategy config {field_name} must be a canonical decimal string")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        raise StrategyReleaseSchemaError(f"strategy config {field_name} must be a canonical decimal string") from exc
    if not decimal_value.is_finite() or _decimal_text(decimal_value) != value:
        raise StrategyReleaseSchemaError(f"strategy config {field_name} must be a canonical decimal string")
    return value


def _decimal_text(value: Any) -> str:
    if isinstance(value, bool):
        raise StrategyReleaseSchemaError("boolean is not a decimal")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StrategyReleaseSchemaError("invalid decimal") from exc
    if not decimal_value.is_finite():
        raise StrategyReleaseSchemaError("decimal must be finite")
    if decimal_value == 0:
        return "0"
    return format(decimal_value.normalize(), "f")


def _wire_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_wire_value(item) for item in value]
    return value
