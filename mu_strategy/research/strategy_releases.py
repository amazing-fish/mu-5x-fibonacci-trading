"""Lightweight canonical StrategyConfig payload codecs (V1 legacy & V2 dynamic)."""
from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields
from decimal import Decimal, InvalidOperation
from types import MappingProxyType, NoneType, UnionType
from typing import Any, ClassVar, Mapping, Union, get_args, get_origin, get_type_hints

from mu_strategy.canonical import canonical_sha256
from mu_strategy.core.trading_calendar import LEGACY_CALENDAR
from mu_strategy.research.historical_data import TrustedExperimentDatasetV1
from mu_strategy.strategy import FEE_PROFILE_CHOICES, StrategyConfig

__all__ = (
    "STRATEGY_CONFIG_SCHEMA_VERSION",
    "STRATEGY_CONFIG_V1_FIELD_NAMES",
    "StrategyConfigPayloadV1",
    "StrategyConfigPayloadV2",
    "StrategyReleaseSchemaError",
    "TrustedExperimentDatasetV1",
    "parse_strategy_config_payload",
)

STRATEGY_CONFIG_SCHEMA_VERSION = 2
_NON_NULL_FIELDS = frozenset({"fee_rate", "trading_calendar_sha256"})
_V2_OPTIONAL_DEFAULT_NONE_FIELDS = frozenset({"max_account_drawdown_pct", "daily_loss_limit_pct"})
_LEGACY_V1_DEFAULTS: dict[str, Any] = {"trading_windows_bjt": (("21:45", "23:30"), ("02:30", "03:45"))}
_LEGACY_V1_KINDS: dict[str, str] = {"trading_windows_bjt": "window_tuple"}
STRATEGY_CONFIG_V1_FIELD_NAMES = (
    "symbol", "entry_execution", "second_pullback_wait_bars", "stop_tightening",
    "leverage", "margin_steps", "initial_stop_pct", "add_thresholds", "rsi_floor",
    "rsi_add_floor", "fib_tolerance_pct", "fee_profile", "fee_rate", "fib_lookback",
    "stop_buffer_pct", "stop_transition_bars", "stop_transition_curve",
    "max_entry_above_fib_pct", "yellow_max_entry_above_fib_pct", "max_signal_range_pct",
    "max_entry_above_signal_close_pct", "block_reverse_fib_resistance",
    "reverse_fib_lookback", "reverse_fib_tolerance_pct", "yellow_stop_tightening",
    "green_stop_tightening", "green_wide_stop_buffer_pct", "allowed_regimes",
    "full_size_regime", "trading_windows_bjt", "trading_windows_et",
)


class StrategyReleaseSchemaError(ValueError):
    pass


def _v2_field_specs() -> dict[str, str]:
    hints = get_type_hints(StrategyConfig)
    return {f.name: _classify_hint(f.name, hints[f.name]) for f in dataclass_fields(StrategyConfig)}


def _v1_field_specs() -> dict[str, str]:
    v2 = _v2_field_specs()
    return {name: (v2[name] if name in v2 else _LEGACY_V1_KINDS[name]) for name in STRATEGY_CONFIG_V1_FIELD_NAMES}


def _classify_hint(field_name: str, hint: Any) -> str:
    optional = False
    origin = get_origin(hint)
    if origin in (Union, UnionType):
        args = [a for a in get_args(hint) if a is not NoneType]
        optional = len(args) != len(get_args(hint))
        if len(args) != 1:
            raise StrategyReleaseSchemaError(f"unsupported union for {field_name}: {hint}")
        hint, origin = args[0], get_origin(args[0])
    if field_name in _NON_NULL_FIELDS:
        optional = False
    if hint is bool:
        return "bool"
    if hint is int:
        return "optional_int" if optional else "int"
    if hint is float:
        return "optional_decimal" if optional else "decimal"
    if hint is str:
        return "optional_text" if optional else "text"
    if origin is tuple:
        args = get_args(hint)
        if args == (float, Ellipsis):
            return "decimal_tuple"
        if args == (str, Ellipsis):
            return "text_tuple"
        if len(args) == 2 and args[1] is Ellipsis and get_origin(args[0]) is tuple and get_args(args[0]) == (str, str):
            return "window_tuple"
    raise StrategyReleaseSchemaError(f"unsupported StrategyConfig type for {field_name}: {hint}")


@dataclass(frozen=True)
class StrategyConfigPayloadV1:
    values: Mapping[str, Any]
    schema_version: int = 1
    VERSION: ClassVar[int] = 1
    _OPTIONAL_OMITTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def _field_specs(cls) -> dict[str, str]:
        return _v1_field_specs()

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != self.VERSION:
            raise StrategyReleaseSchemaError(f"unsupported strategy config schema_version: {self.schema_version}")
        specs = self._field_specs()
        actual, expected = set(self.values), set(specs)
        if unknown := actual - expected:
            raise StrategyReleaseSchemaError(f"strategy config has unknown fields: {sorted(unknown)}")
        if missing := (expected - actual) - self._OPTIONAL_OMITTABLE_FIELDS:
            raise StrategyReleaseSchemaError(f"strategy config has missing fields: {sorted(missing)}")
        normalized = {
            name: _validate_value(name, kind, self.values[name])
            for name, kind in specs.items()
            if name in self.values
        }
        if self.VERSION == 1 and normalized["trading_windows_bjt"] != _LEGACY_V1_DEFAULTS["trading_windows_bjt"]:
            raise StrategyReleaseSchemaError("V1 only supports the legacy default trading_windows_bjt; use V2")
        object.__setattr__(self, "values", MappingProxyType(normalized))

    @property
    def strategy_config_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    @classmethod
    def from_config(cls, config: StrategyConfig) -> "StrategyConfigPayloadV1":
        if cls.VERSION == 1:
            if config.trading_calendar_id != LEGACY_CALENDAR:
                raise StrategyReleaseSchemaError("V1 cannot encode a reference calendar; use V2")
            if config.max_account_drawdown_pct is not None or config.daily_loss_limit_pct is not None:
                raise StrategyReleaseSchemaError("V1 cannot encode account risk limits; use V2")
        encoded: dict[str, Any] = {}
        for name, kind in cls._field_specs().items():
            raw = getattr(config, name) if hasattr(config, name) else _LEGACY_V1_DEFAULTS[name]
            if raw is None and name in cls._OPTIONAL_OMITTABLE_FIELDS:
                continue
            encoded[name] = _encode_value(kind, raw)
        return cls(encoded)

    @classmethod
    def from_dict(cls, payload: Any) -> "StrategyConfigPayloadV1":
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "fields"}:
            raise StrategyReleaseSchemaError("strategy config payload must have schema_version and fields")
        if not isinstance(payload["fields"], dict) or type(payload["schema_version"]) is not int:
            raise StrategyReleaseSchemaError("invalid strategy config payload structure")
        return cls(values=payload["fields"], schema_version=payload["schema_version"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fields": {name: _wire_value(value) for name, value in self.values.items()},
        }

    def to_strategy_config(self) -> StrategyConfig:
        specs = self._field_specs()
        live_fields = {f.name for f in dataclass_fields(StrategyConfig)}
        return StrategyConfig(**{
            name: _decode_value(specs[name], value)
            for name, value in self.values.items()
            if name in live_fields
        })


@dataclass(frozen=True)
class StrategyConfigPayloadV2(StrategyConfigPayloadV1):
    schema_version: int = 2
    VERSION: ClassVar[int] = 2
    _OPTIONAL_OMITTABLE_FIELDS: ClassVar[frozenset[str]] = _V2_OPTIONAL_DEFAULT_NONE_FIELDS

    @classmethod
    def _field_specs(cls) -> dict[str, str]:
        return _v2_field_specs()

    def __post_init__(self) -> None:
        super().__post_init__()
        try:
            self.to_strategy_config()
        except ValueError as exc:
            raise StrategyReleaseSchemaError(str(exc)) from exc


def parse_strategy_config_payload(payload: Any) -> StrategyConfigPayloadV1:
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
        raise StrategyReleaseSchemaError("invalid strategy config schema_version")
    codec = {1: StrategyConfigPayloadV1, 2: StrategyConfigPayloadV2}.get(payload["schema_version"])
    if codec is None:
        raise StrategyReleaseSchemaError("unsupported strategy config schema_version")
    return codec.from_dict(payload)


def _validate_value(name: str, kind: str, value: Any) -> Any:
    if name == "fee_profile":
        if value not in FEE_PROFILE_CHOICES:
            raise StrategyReleaseSchemaError("strategy config fee_profile is unsupported")
        return value
    if kind == "decimal":
        return _require_canonical_decimal(value, name)
    if kind == "optional_decimal":
        return None if value is None else _require_canonical_decimal(value, name)
    if kind == "decimal_tuple":
        if not isinstance(value, (list, tuple)) or not value:
            raise StrategyReleaseSchemaError(f"strategy config {name} must be a non-empty list")
        return tuple(_require_canonical_decimal(item, name) for item in value)
    if kind in ("int", "optional_int"):
        if (value is None and kind == "optional_int") or type(value) is int:
            return value
        raise StrategyReleaseSchemaError(f"strategy config {name} must be an integer")
    if kind == "bool":
        if not isinstance(value, bool):
            raise StrategyReleaseSchemaError(f"strategy config {name} must be a boolean")
        return value
    if kind == "optional_text":
        if value is not None and (not isinstance(value, str) or not value):
            raise StrategyReleaseSchemaError(f"strategy config {name} must be text or null")
        return value
    if kind == "text_tuple":
        if not isinstance(value, (list, tuple)) or not value or not all(isinstance(x, str) and x for x in value):
            raise StrategyReleaseSchemaError(f"strategy config {name} must be a non-empty text list")
        return tuple(value)
    if kind == "window_tuple":
        if not isinstance(value, (list, tuple)) or not value:
            raise StrategyReleaseSchemaError(f"strategy config {name} must be a non-empty window list")
        windows: list[tuple[str, str]] = []
        for win in value:
            if not isinstance(win, (list, tuple)) or len(win) != 2 or not all(isinstance(x, str) and x for x in win):
                raise StrategyReleaseSchemaError(f"strategy config {name} contains an invalid window")
            windows.append((win[0], win[1]))
        return tuple(windows)
    if not isinstance(value, str) or not value:
        raise StrategyReleaseSchemaError(f"strategy config {name} must be non-empty text")
    return value


def _encode_value(kind: str, value: Any) -> Any:
    if kind in ("decimal", "optional_decimal"):
        return None if value is None else _decimal_text(value)
    if kind == "decimal_tuple":
        return tuple(_decimal_text(item) for item in value)
    if isinstance(value, tuple):
        return tuple(tuple(item) if isinstance(item, tuple) else item for item in value)
    return value


def _decode_value(kind: str, value: Any) -> Any:
    if kind in ("decimal", "optional_decimal"):
        return None if value is None else float(value)
    if kind == "decimal_tuple":
        return tuple(float(item) for item in value)
    return value


def _require_canonical_decimal(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise StrategyReleaseSchemaError(f"strategy config {name} must be a canonical decimal string")
    try:
        dec = Decimal(value)
    except InvalidOperation as exc:
        raise StrategyReleaseSchemaError(f"strategy config {name} must be a canonical decimal string") from exc
    if not dec.is_finite() or _decimal_text(dec) != value:
        raise StrategyReleaseSchemaError(f"strategy config {name} must be a canonical decimal string")
    return value


def _decimal_text(value: Any) -> str:
    if isinstance(value, bool):
        raise StrategyReleaseSchemaError("boolean is not a decimal")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StrategyReleaseSchemaError("invalid decimal") from exc
    if not dec.is_finite():
        raise StrategyReleaseSchemaError("decimal must be finite")
    return "0" if dec == 0 else format(dec.normalize(), "f")


def _wire_value(value: Any) -> Any:
    return [_wire_value(item) for item in value] if isinstance(value, tuple) else value
