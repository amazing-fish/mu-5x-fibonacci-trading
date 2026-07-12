from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields as dataclass_fields
from decimal import Decimal, InvalidOperation
from enum import Enum, unique
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from mu_strategy.canonical import canonical_sha256
from mu_strategy.strategy import FEE_PROFILE_CHOICES, StrategyConfig


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


class StrategyReleaseResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyConfigPayloadV1:
    values: Mapping[str, Any]
    schema_version: int = STRATEGY_CONFIG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != STRATEGY_CONFIG_SCHEMA_VERSION
        ):
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
        return cls(values=payload["fields"], schema_version=_required_int(payload, "schema_version"))

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
    if field_name == "fee_profile":
        if value not in FEE_PROFILE_CHOICES:
            raise StrategyReleaseSchemaError("strategy config fee_profile is unsupported")
        return value
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


STRATEGY_RELEASE_SCHEMA_VERSION = 1
STRATEGY_RELEASE_SCM_PROVIDER = "github"
STRATEGY_RELEASE_SCM_REPOSITORY = "amazing-fish/mu-5x-fibonacci-trading"
EXPERIMENT_PROTOCOL_ID = "mu.baseline.walk_forward.cold_start.v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_GITHUB_REVIEW_RECORD_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
_RELEASE_ID_PATTERN = re.compile(r"^sr1_[0-9a-f]{64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")
_RESULT_ARITHMETIC_TOLERANCE = Decimal("0.00000000001")


@unique
class ExperimentWindowRole(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    OUT_OF_SAMPLE = "out_of_sample"


@unique
class ReleaseDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@unique
class SelectionReasonCode(str, Enum):
    BASELINE_CONTINUITY = "baseline_continuity"
    REVALIDATED_BASELINE = "revalidated_baseline"


@unique
class FillModel(str, Enum):
    DETERMINISTIC_OHLC = "deterministic_ohlc"


@unique
class PartialFillModel(str, Enum):
    NONE = "none"


@dataclass(frozen=True)
class ExperimentWindow:
    role: ExperimentWindowRole
    input_start_ms: int
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        for field_name in ("input_start_ms", "start_ms", "end_ms"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"experiment window {field_name} must be a non-negative integer")
        if self.input_start_ms != self.start_ms:
            raise ValueError("cold-start experiment window requires input_start_ms == start_ms")
        if self.start_ms >= self.end_ms:
            raise ValueError("experiment window must be non-empty and end-exclusive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "input_start_ms": self.input_start_ms,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ExperimentWindow":
        _require_exact_mapping(payload, {"role", "input_start_ms", "start_ms", "end_ms"}, "window")
        try:
            return cls(
                role=ExperimentWindowRole(payload["role"]),
                input_start_ms=_required_int(payload, "input_start_ms"),
                start_ms=_required_int(payload, "start_ms"),
                end_ms=_required_int(payload, "end_ms"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StrategyReleaseSchemaError):
                raise
            raise StrategyReleaseSchemaError(f"invalid experiment window: {exc}") from exc


@dataclass(frozen=True)
class BacktestAssumptionsV1:
    starting_equity: str
    fee_profile: str
    fee_rate: str
    fill_model: FillModel
    slippage_bps: str
    partial_fill_model: PartialFillModel
    schema_version: int = STRATEGY_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_RELEASE_SCHEMA_VERSION:
            raise ValueError("unsupported assumptions schema_version")
        _require_decimal_range(self.starting_equity, "starting_equity", minimum=Decimal("0"), exclusive_minimum=True)
        _require_decimal_range(self.fee_rate, "fee_rate", minimum=Decimal("0"))
        _require_decimal_range(self.slippage_bps, "slippage_bps", minimum=Decimal("0"))
        if self.fee_profile not in FEE_PROFILE_CHOICES:
            raise ValueError("fee_profile is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "starting_equity": self.starting_equity,
            "fee_profile": self.fee_profile,
            "fee_rate": self.fee_rate,
            "fill_model": self.fill_model.value,
            "slippage_bps": self.slippage_bps,
            "partial_fill_model": self.partial_fill_model.value,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "BacktestAssumptionsV1":
        expected = {
            "schema_version",
            "starting_equity",
            "fee_profile",
            "fee_rate",
            "fill_model",
            "slippage_bps",
            "partial_fill_model",
        }
        _require_exact_mapping(payload, expected, "assumptions")
        try:
            return cls(
                schema_version=_required_int(payload, "schema_version"),
                starting_equity=payload["starting_equity"],
                fee_profile=_required_text(payload, "fee_profile"),
                fee_rate=payload["fee_rate"],
                fill_model=FillModel(payload["fill_model"]),
                slippage_bps=payload["slippage_bps"],
                partial_fill_model=PartialFillModel(payload["partial_fill_model"]),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StrategyReleaseSchemaError):
                raise
            raise StrategyReleaseSchemaError(f"invalid assumptions: {exc}") from exc


def validate_release_experiment_assumptions(
    *,
    config_fee_profile: str,
    config_fee_rate: Any,
    assumptions: BacktestAssumptionsV1,
) -> None:
    if (
        assumptions.fee_profile != config_fee_profile
        or Decimal(assumptions.fee_rate) != Decimal(str(config_fee_rate))
    ):
        raise ValueError("experiment fee assumptions must match the strategy config")
    if assumptions.fill_model is not FillModel.DETERMINISTIC_OHLC:
        raise ValueError("unsupported experiment fill model")
    if Decimal(assumptions.slippage_bps) != 0:
        raise ValueError("v1 experiment requires zero explicit slippage")
    if assumptions.partial_fill_model is not PartialFillModel.NONE:
        raise ValueError("v1 experiment does not model partial fills")


@dataclass(frozen=True)
class ExperimentWindowResultV1:
    role: ExperimentWindowRole
    trade_count: int
    starting_equity: str
    ending_equity: str
    gross_profit: str
    gross_loss: str
    total_return_pct: str
    max_drawdown_pct: str
    result_fingerprint: str
    schema_version: int = STRATEGY_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_RELEASE_SCHEMA_VERSION:
            raise ValueError("unsupported result schema_version")
        if isinstance(self.trade_count, bool) or not isinstance(self.trade_count, int) or self.trade_count < 0:
            raise ValueError("trade_count must be a non-negative integer")
        starting_equity = _require_decimal_range(
            self.starting_equity,
            "starting_equity",
            minimum=Decimal("0"),
            exclusive_minimum=True,
        )
        ending_equity = _require_decimal_range(self.ending_equity, "ending_equity", minimum=Decimal("0"))
        gross_profit = _require_decimal_range(self.gross_profit, "gross_profit", minimum=Decimal("0"))
        gross_loss = _require_decimal_range(self.gross_loss, "gross_loss", minimum=Decimal("0"))
        total_return_pct = _require_decimal_range(self.total_return_pct, "total_return_pct")
        max_drawdown_pct = _require_decimal_range(
            self.max_drawdown_pct,
            "max_drawdown_pct",
            minimum=Decimal("-1"),
            maximum=Decimal("0"),
        )
        expected_return = (ending_equity / starting_equity) - 1
        if abs(total_return_pct - expected_return) > _RESULT_ARITHMETIC_TOLERANCE:
            raise ValueError("result total return does not match starting and ending equity")
        net_equity_change = ending_equity - starting_equity
        if abs(net_equity_change - (gross_profit - gross_loss)) > _RESULT_ARITHMETIC_TOLERANCE:
            raise ValueError("result gross profit and loss do not match the equity change")
        if self.trade_count == 0 and any(
            abs(value) > _RESULT_ARITHMETIC_TOLERANCE
            for value in (
                net_equity_change,
                gross_profit,
                gross_loss,
                total_return_pct,
                max_drawdown_pct,
            )
        ):
            raise ValueError("zero-trade result must not report P&L, return, equity change, or drawdown")
        directional_trade_minimum = int(gross_profit > 0) + int(gross_loss > 0)
        if self.trade_count < directional_trade_minimum:
            raise ValueError("result trade count cannot produce the reported directional P&L")
        expected = canonical_sha256(self._fingerprint_payload())
        if self.result_fingerprint != expected:
            raise ValueError("result fingerprint does not match canonical summary")

    @classmethod
    def create(cls, **kwargs: Any) -> "ExperimentWindowResultV1":
        payload = {
            "schema_version": STRATEGY_RELEASE_SCHEMA_VERSION,
            "role": kwargs["role"].value,
            "trade_count": kwargs["trade_count"],
            "starting_equity": kwargs["starting_equity"],
            "ending_equity": kwargs["ending_equity"],
            "gross_profit": kwargs["gross_profit"],
            "gross_loss": kwargs["gross_loss"],
            "total_return_pct": kwargs["total_return_pct"],
            "max_drawdown_pct": kwargs["max_drawdown_pct"],
        }
        return cls(result_fingerprint=canonical_sha256(payload), **kwargs)

    def _fingerprint_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role": self.role.value,
            "trade_count": self.trade_count,
            "starting_equity": self.starting_equity,
            "ending_equity": self.ending_equity,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            "total_return_pct": self.total_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._fingerprint_payload(), "result_fingerprint": self.result_fingerprint}

    @classmethod
    def from_dict(cls, payload: Any) -> "ExperimentWindowResultV1":
        expected = {
            "schema_version",
            "role",
            "trade_count",
            "starting_equity",
            "ending_equity",
            "gross_profit",
            "gross_loss",
            "total_return_pct",
            "max_drawdown_pct",
            "result_fingerprint",
        }
        _require_exact_mapping(payload, expected, "window result")
        try:
            return cls(
                schema_version=_required_int(payload, "schema_version"),
                role=ExperimentWindowRole(payload["role"]),
                trade_count=_required_int(payload, "trade_count"),
                starting_equity=payload["starting_equity"],
                ending_equity=payload["ending_equity"],
                gross_profit=payload["gross_profit"],
                gross_loss=payload["gross_loss"],
                total_return_pct=payload["total_return_pct"],
                max_drawdown_pct=payload["max_drawdown_pct"],
                result_fingerprint=_required_text(payload, "result_fingerprint"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StrategyReleaseSchemaError):
                raise
            raise StrategyReleaseSchemaError(f"invalid window result: {exc}") from exc


@dataclass(frozen=True)
class TrustedExperimentDatasetV1:
    run_id: str
    symbol: str
    manifest_schema_version: int
    requested_intervals: tuple[str, ...]
    effective_intervals: tuple[str, ...]
    content_sha256_by_interval: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not _RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ValueError("dataset run_id must be lowercase 32-character hex")
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("dataset symbol is invalid")
        if self.manifest_schema_version != 3:
            raise ValueError("historical dataset requires manifest schema v3")
        if not self.requested_intervals or not self.effective_intervals:
            raise ValueError("dataset intervals must not be empty")
        if len(set(self.requested_intervals)) != len(self.requested_intervals) or len(set(self.effective_intervals)) != len(
            self.effective_intervals
        ):
            raise ValueError("dataset intervals must be unique")
        if not set(self.requested_intervals).issubset(self.effective_intervals):
            raise ValueError("requested intervals must be a subset of effective intervals")
        effective = set(self.effective_intervals)
        if any(interval in effective for interval in ("15m", "1h")) and "5m" not in effective:
            raise ValueError("dataset effective_intervals must include 5m when native intervals are present")
        if tuple(sorted(self.content_sha256_by_interval)) != self.content_sha256_by_interval:
            raise ValueError("dataset content hashes must be sorted by interval")
        hashes = dict(self.content_sha256_by_interval)
        if len(hashes) != len(self.content_sha256_by_interval) or set(hashes) != set(self.effective_intervals):
            raise ValueError("dataset requires exactly one content hash per effective interval")
        if any(not interval or not _SHA256_PATTERN.fullmatch(digest) for interval, digest in hashes.items()):
            raise ValueError("dataset content hashes must be lowercase SHA-256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "symbol": self.symbol,
            "manifest_schema_version": self.manifest_schema_version,
            "requested_intervals": list(self.requested_intervals),
            "effective_intervals": list(self.effective_intervals),
            "content_sha256_by_interval": {key: value for key, value in self.content_sha256_by_interval},
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "TrustedExperimentDatasetV1":
        expected = {
            "run_id",
            "symbol",
            "manifest_schema_version",
            "requested_intervals",
            "effective_intervals",
            "content_sha256_by_interval",
        }
        _require_exact_mapping(payload, expected, "trusted dataset")
        hashes = payload["content_sha256_by_interval"]
        if not isinstance(hashes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in hashes.items()):
            raise StrategyReleaseSchemaError("dataset content hashes must be an object of strings")
        try:
            return cls(
                run_id=_required_text(payload, "run_id"),
                symbol=_required_text(payload, "symbol"),
                manifest_schema_version=_required_int(payload, "manifest_schema_version"),
                requested_intervals=_required_text_tuple(payload, "requested_intervals"),
                effective_intervals=_required_text_tuple(payload, "effective_intervals"),
                content_sha256_by_interval=tuple(sorted(hashes.items())),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StrategyReleaseSchemaError):
                raise
            raise StrategyReleaseSchemaError(f"invalid trusted dataset: {exc}") from exc


@dataclass(frozen=True)
class StrategyReleaseCandidateV1:
    strategy_rule_id: str
    strategy_name: str
    supported_symbols: tuple[str, ...]
    strategy_config: StrategyConfigPayloadV1
    evaluated_code_commit_sha: str
    dataset: TrustedExperimentDatasetV1
    windows: tuple[ExperimentWindow, ...]
    experiment_protocol_id: str
    assumptions: BacktestAssumptionsV1
    results: tuple[ExperimentWindowResultV1, ...]
    selection_reason: SelectionReasonCode
    result_fingerprint: str
    candidate_fingerprint: str
    schema_version: int = STRATEGY_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_RELEASE_SCHEMA_VERSION:
            raise ValueError("unsupported candidate schema_version")
        if not self.strategy_rule_id or not self.strategy_name:
            raise ValueError("candidate strategy identity is required")
        if not self.supported_symbols or tuple(sorted(set(self.supported_symbols))) != self.supported_symbols:
            raise ValueError("candidate supported_symbols must be sorted and unique")
        if self.dataset.symbol not in self.supported_symbols:
            raise ValueError("candidate dataset symbol must be supported")
        if self.strategy_config.values["symbol"] != self.dataset.symbol:
            raise ValueError("candidate config symbol must match the dataset symbol")
        if not _GIT_SHA_PATTERN.fullmatch(self.evaluated_code_commit_sha):
            raise ValueError("evaluated_code_commit_sha must be lowercase full SHA-1")
        if self.experiment_protocol_id != EXPERIMENT_PROTOCOL_ID:
            raise ValueError("unsupported experiment protocol")
        validate_release_experiment_assumptions(
            config_fee_profile=self.strategy_config.values["fee_profile"],
            config_fee_rate=self.strategy_config.values["fee_rate"],
            assumptions=self.assumptions,
        )
        required_intervals = {"15m", "1h"}
        if not required_intervals.issubset(self.dataset.effective_intervals):
            raise ValueError("candidate dataset is missing required intervals")
        _validate_windows(self.windows)
        expected_roles = tuple(ExperimentWindowRole)
        if tuple(result.role for result in self.results) != expected_roles:
            raise ValueError("candidate requires one ordered result per experiment window")
        if any(result.starting_equity != self.assumptions.starting_equity for result in self.results):
            raise ValueError("candidate result starting equity must match the assumptions")
        expected_result_fingerprint = canonical_sha256(self._result_payload())
        if self.result_fingerprint != expected_result_fingerprint:
            raise ValueError("candidate result fingerprint does not match canonical results")
        expected_candidate_fingerprint = canonical_sha256(self._control_payload())
        if self.candidate_fingerprint != expected_candidate_fingerprint:
            raise ValueError("candidate fingerprint does not match canonical content")

    @classmethod
    def create(cls, **kwargs: Any) -> "StrategyReleaseCandidateV1":
        result_payload = _candidate_result_payload(
            kwargs["experiment_protocol_id"], kwargs["windows"], kwargs["assumptions"], kwargs["results"]
        )
        result_fingerprint = canonical_sha256(result_payload)
        control_payload = _candidate_control_payload(
            schema_version=STRATEGY_RELEASE_SCHEMA_VERSION,
            result_fingerprint=result_fingerprint,
            **kwargs,
        )
        return cls(
            result_fingerprint=result_fingerprint,
            candidate_fingerprint=canonical_sha256(control_payload),
            **kwargs,
        )

    @property
    def strategy_config_sha256(self) -> str:
        return self.strategy_config.strategy_config_sha256

    def _result_payload(self) -> dict[str, Any]:
        return _candidate_result_payload(self.experiment_protocol_id, self.windows, self.assumptions, self.results)

    def _control_payload(self) -> dict[str, Any]:
        return _candidate_control_payload(
            schema_version=self.schema_version,
            strategy_rule_id=self.strategy_rule_id,
            strategy_name=self.strategy_name,
            supported_symbols=self.supported_symbols,
            strategy_config=self.strategy_config,
            evaluated_code_commit_sha=self.evaluated_code_commit_sha,
            dataset=self.dataset,
            windows=self.windows,
            experiment_protocol_id=self.experiment_protocol_id,
            assumptions=self.assumptions,
            results=self.results,
            selection_reason=self.selection_reason,
            result_fingerprint=self.result_fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self._control_payload(), "candidate_fingerprint": self.candidate_fingerprint}

    @classmethod
    def from_dict(cls, payload: Any) -> "StrategyReleaseCandidateV1":
        expected = {
            "schema_version",
            "strategy_rule_id",
            "strategy_name",
            "supported_symbols",
            "strategy_config",
            "strategy_config_sha256",
            "evaluated_code_commit_sha",
            "dataset",
            "windows",
            "experiment_protocol_id",
            "assumptions",
            "results",
            "selection_reason",
            "result_fingerprint",
            "candidate_fingerprint",
        }
        _require_exact_mapping(payload, expected, "candidate")
        try:
            config = StrategyConfigPayloadV1.from_dict(payload["strategy_config"])
            if payload["strategy_config_sha256"] != config.strategy_config_sha256:
                raise StrategyReleaseSchemaError("candidate strategy config fingerprint does not match payload")
            return cls(
                schema_version=_required_int(payload, "schema_version"),
                strategy_rule_id=_required_text(payload, "strategy_rule_id"),
                strategy_name=_required_text(payload, "strategy_name"),
                supported_symbols=_required_text_tuple(payload, "supported_symbols"),
                strategy_config=config,
                evaluated_code_commit_sha=_required_text(payload, "evaluated_code_commit_sha"),
                dataset=TrustedExperimentDatasetV1.from_dict(payload["dataset"]),
                windows=tuple(ExperimentWindow.from_dict(item) for item in _required_list(payload, "windows")),
                experiment_protocol_id=_required_text(payload, "experiment_protocol_id"),
                assumptions=BacktestAssumptionsV1.from_dict(payload["assumptions"]),
                results=tuple(
                    ExperimentWindowResultV1.from_dict(item) for item in _required_list(payload, "results")
                ),
                selection_reason=SelectionReasonCode(payload["selection_reason"]),
                result_fingerprint=_required_text(payload, "result_fingerprint"),
                candidate_fingerprint=_required_text(payload, "candidate_fingerprint"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StrategyReleaseSchemaError):
                raise
            raise StrategyReleaseSchemaError(f"invalid candidate: {exc}") from exc


@dataclass(frozen=True)
class ScmReviewSnapshotV1:
    scm_provider: str
    repository: str
    pull_request_number: int
    review_record_id: str
    reviewer_id: str
    author_id: str
    reviewed_at_ms: int
    decision: ReleaseDecision
    candidate_fingerprint: str
    evaluated_code_commit_sha: str
    statement: str
    review_url: str
    snapshot_sha256: str
    schema_version: int = STRATEGY_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_RELEASE_SCHEMA_VERSION:
            raise ValueError("unsupported SCM review snapshot schema_version")
        for field_name in ("scm_provider", "repository", "review_record_id", "reviewer_id", "author_id", "statement"):
            if not getattr(self, field_name):
                raise ValueError(f"SCM review {field_name} is required")
        if self.scm_provider != STRATEGY_RELEASE_SCM_PROVIDER:
            raise ValueError("SCM review provider is not trusted")
        if self.repository != STRATEGY_RELEASE_SCM_REPOSITORY:
            raise ValueError("SCM review repository is not trusted")
        if not _GITHUB_REVIEW_RECORD_ID_PATTERN.fullmatch(self.review_record_id):
            raise ValueError("SCM review record ID must be a positive GitHub database ID")
        if isinstance(self.pull_request_number, bool) or not isinstance(self.pull_request_number, int) or self.pull_request_number <= 0:
            raise ValueError("pull_request_number must be positive")
        if isinstance(self.reviewed_at_ms, bool) or not isinstance(self.reviewed_at_ms, int) or self.reviewed_at_ms < 0:
            raise ValueError("reviewed_at_ms must be non-negative")
        if not _SHA256_PATTERN.fullmatch(self.candidate_fingerprint):
            raise ValueError("review candidate fingerprint must be SHA-256")
        if not _GIT_SHA_PATTERN.fullmatch(self.evaluated_code_commit_sha):
            raise ValueError("review implementation commit must be full SHA-1")
        if self.reviewer_id.casefold() == self.author_id.casefold():
            raise ValueError("SCM reviewer must be independent from captured author")
        expected_statement = strategy_release_approval_statement(
            self.candidate_fingerprint,
            self.evaluated_code_commit_sha,
        )
        if self.statement != expected_statement:
            raise ValueError("SCM review statement is not canonical")
        expected_url = (
            f"https://github.com/{self.repository}/pull/{self.pull_request_number}"
            f"#pullrequestreview-{self.review_record_id}"
        )
        if self.review_url != expected_url:
            raise ValueError("SCM review URL does not match trusted review coordinates")
        if self.snapshot_sha256 != canonical_sha256(self._snapshot_payload()):
            raise ValueError("SCM review snapshot hash does not match canonical evidence")

    @classmethod
    def create(cls, **kwargs: Any) -> "ScmReviewSnapshotV1":
        payload = _scm_snapshot_payload(schema_version=STRATEGY_RELEASE_SCHEMA_VERSION, **kwargs)
        return cls(snapshot_sha256=canonical_sha256(payload), **kwargs)

    def _snapshot_payload(self) -> dict[str, Any]:
        return _scm_snapshot_payload(
            schema_version=self.schema_version,
            scm_provider=self.scm_provider,
            repository=self.repository,
            pull_request_number=self.pull_request_number,
            review_record_id=self.review_record_id,
            reviewer_id=self.reviewer_id,
            author_id=self.author_id,
            reviewed_at_ms=self.reviewed_at_ms,
            decision=self.decision,
            candidate_fingerprint=self.candidate_fingerprint,
            evaluated_code_commit_sha=self.evaluated_code_commit_sha,
            statement=self.statement,
            review_url=self.review_url,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self._snapshot_payload(), "snapshot_sha256": self.snapshot_sha256}

    @classmethod
    def from_dict(cls, payload: Any) -> "ScmReviewSnapshotV1":
        expected = {
            "schema_version",
            "scm_provider",
            "repository",
            "pull_request_number",
            "review_record_id",
            "reviewer_id",
            "author_id",
            "reviewed_at_ms",
            "decision",
            "candidate_fingerprint",
            "evaluated_code_commit_sha",
            "statement",
            "review_url",
            "snapshot_sha256",
        }
        _require_exact_mapping(payload, expected, "SCM review snapshot")
        try:
            return cls(
                schema_version=_required_int(payload, "schema_version"),
                scm_provider=_required_text(payload, "scm_provider"),
                repository=_required_text(payload, "repository"),
                pull_request_number=_required_int(payload, "pull_request_number"),
                review_record_id=_required_text(payload, "review_record_id"),
                reviewer_id=_required_text(payload, "reviewer_id"),
                author_id=_required_text(payload, "author_id"),
                reviewed_at_ms=_required_int(payload, "reviewed_at_ms"),
                decision=ReleaseDecision(payload["decision"]),
                candidate_fingerprint=_required_text(payload, "candidate_fingerprint"),
                evaluated_code_commit_sha=_required_text(payload, "evaluated_code_commit_sha"),
                statement=_required_text(payload, "statement"),
                review_url=_required_text(payload, "review_url"),
                snapshot_sha256=_required_text(payload, "snapshot_sha256"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StrategyReleaseSchemaError):
                raise
            raise StrategyReleaseSchemaError(f"invalid SCM review snapshot: {exc}") from exc


@dataclass(frozen=True)
class StrategyReleaseApprovalV1:
    decision: ReleaseDecision
    candidate_fingerprint: str
    evaluated_code_commit_sha: str
    review_snapshot: ScmReviewSnapshotV1
    approval_snapshot_sha256: str
    schema_version: int = STRATEGY_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_RELEASE_SCHEMA_VERSION:
            raise ValueError("unsupported release approval schema_version")
        if self.decision is not self.review_snapshot.decision:
            raise ValueError("approval decision must match review snapshot")
        if self.candidate_fingerprint != self.review_snapshot.candidate_fingerprint:
            raise ValueError("approval candidate must match review snapshot")
        if self.evaluated_code_commit_sha != self.review_snapshot.evaluated_code_commit_sha:
            raise ValueError("approval implementation commit must match review snapshot")
        if self.approval_snapshot_sha256 != self.review_snapshot.snapshot_sha256:
            raise ValueError("approval snapshot hash must match review snapshot")

    @classmethod
    def create(cls, *, review_snapshot: ScmReviewSnapshotV1) -> "StrategyReleaseApprovalV1":
        return cls(
            decision=review_snapshot.decision,
            candidate_fingerprint=review_snapshot.candidate_fingerprint,
            evaluated_code_commit_sha=review_snapshot.evaluated_code_commit_sha,
            review_snapshot=review_snapshot,
            approval_snapshot_sha256=review_snapshot.snapshot_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision": self.decision.value,
            "candidate_fingerprint": self.candidate_fingerprint,
            "evaluated_code_commit_sha": self.evaluated_code_commit_sha,
            "review_snapshot": self.review_snapshot.to_dict(),
            "approval_snapshot_sha256": self.approval_snapshot_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "StrategyReleaseApprovalV1":
        expected = {
            "schema_version",
            "decision",
            "candidate_fingerprint",
            "evaluated_code_commit_sha",
            "review_snapshot",
            "approval_snapshot_sha256",
        }
        _require_exact_mapping(payload, expected, "release approval")
        try:
            return cls(
                schema_version=_required_int(payload, "schema_version"),
                decision=ReleaseDecision(payload["decision"]),
                candidate_fingerprint=_required_text(payload, "candidate_fingerprint"),
                evaluated_code_commit_sha=_required_text(payload, "evaluated_code_commit_sha"),
                review_snapshot=ScmReviewSnapshotV1.from_dict(payload["review_snapshot"]),
                approval_snapshot_sha256=_required_text(payload, "approval_snapshot_sha256"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StrategyReleaseSchemaError):
                raise
            raise StrategyReleaseSchemaError(f"invalid release approval: {exc}") from exc


@dataclass(frozen=True)
class StrategyReleaseV1:
    candidate: StrategyReleaseCandidateV1
    approval: StrategyReleaseApprovalV1
    strategy_release_id: str
    schema_version: int = STRATEGY_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_RELEASE_SCHEMA_VERSION:
            raise ValueError("unsupported strategy release schema_version")
        if self.approval.decision is not ReleaseDecision.APPROVED:
            raise ValueError("strategy release requires an APPROVED review")
        if self.approval.candidate_fingerprint != self.candidate.candidate_fingerprint:
            raise ValueError("strategy release candidate does not match approval")
        if self.approval.evaluated_code_commit_sha != self.candidate.evaluated_code_commit_sha:
            raise ValueError("strategy release implementation does not match approval")
        if not _RELEASE_ID_PATTERN.fullmatch(self.strategy_release_id):
            raise ValueError("strategy_release_id has invalid format")
        expected = _strategy_release_id(self.candidate, self.approval)
        if self.strategy_release_id != expected:
            raise ValueError("strategy_release_id does not match canonical release content")

    @classmethod
    def create(
        cls,
        *,
        candidate: StrategyReleaseCandidateV1,
        approval: StrategyReleaseApprovalV1,
    ) -> "StrategyReleaseV1":
        if approval.decision is not ReleaseDecision.APPROVED:
            raise ValueError("strategy release requires an APPROVED review")
        if approval.candidate_fingerprint != candidate.candidate_fingerprint:
            raise ValueError("strategy release candidate does not match approval")
        return cls(candidate=candidate, approval=approval, strategy_release_id=_strategy_release_id(candidate, approval))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate": self.candidate.to_dict(),
            "approval": self.approval.to_dict(),
            "strategy_release_id": self.strategy_release_id,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "StrategyReleaseV1":
        _require_exact_mapping(payload, {"schema_version", "candidate", "approval", "strategy_release_id"}, "release")
        try:
            return cls(
                schema_version=_required_int(payload, "schema_version"),
                candidate=StrategyReleaseCandidateV1.from_dict(payload["candidate"]),
                approval=StrategyReleaseApprovalV1.from_dict(payload["approval"]),
                strategy_release_id=_required_text(payload, "strategy_release_id"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StrategyReleaseSchemaError):
                raise
            raise StrategyReleaseSchemaError(f"invalid release: {exc}") from exc


class StrictStrategyReleaseResolver:
    def __init__(self, release_dir: Path) -> None:
        self._release_dir = release_dir

    def resolve(
        self,
        strategy_release_id: str,
        *,
        expected_rule_id: str,
        expected_symbol: str,
    ) -> StrategyReleaseV1:
        if not isinstance(strategy_release_id, str) or not _RELEASE_ID_PATTERN.fullmatch(strategy_release_id):
            raise StrategyReleaseResolutionError("strategy_release_id has invalid format")
        if not expected_rule_id:
            raise StrategyReleaseResolutionError("expected rule ID is required")
        if not expected_symbol:
            raise StrategyReleaseResolutionError("expected symbol is required")

        path = self._release_dir / f"{strategy_release_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            release = StrategyReleaseV1.from_dict(payload)
        except FileNotFoundError as exc:
            raise StrategyReleaseResolutionError(f"strategy release not found: {strategy_release_id}") from exc
        except (OSError, json.JSONDecodeError, StrategyReleaseSchemaError, ValueError) as exc:
            raise StrategyReleaseResolutionError(f"invalid strategy release {strategy_release_id}: {exc}") from exc

        if release.strategy_release_id != strategy_release_id:
            raise StrategyReleaseResolutionError("strategy release path does not match content identity")
        if release.candidate.strategy_rule_id != expected_rule_id:
            raise StrategyReleaseResolutionError("strategy release rule does not match expected rule")
        if (
            expected_symbol not in release.candidate.supported_symbols
            or release.candidate.dataset.symbol != expected_symbol
        ):
            raise StrategyReleaseResolutionError("strategy release symbol does not match expected symbol")
        return release


def _candidate_result_payload(
    protocol_id: str,
    windows: tuple[ExperimentWindow, ...],
    assumptions: BacktestAssumptionsV1,
    results: tuple[ExperimentWindowResultV1, ...],
) -> dict[str, Any]:
    return {
        "experiment_protocol_id": protocol_id,
        "windows": [window.to_dict() for window in windows],
        "assumptions": assumptions.to_dict(),
        "results": [result.to_dict() for result in results],
    }


def _candidate_control_payload(*, schema_version: int, result_fingerprint: str, **kwargs: Any) -> dict[str, Any]:
    config = kwargs["strategy_config"]
    return {
        "schema_version": schema_version,
        "strategy_rule_id": kwargs["strategy_rule_id"],
        "strategy_name": kwargs["strategy_name"],
        "supported_symbols": list(kwargs["supported_symbols"]),
        "strategy_config": config.to_dict(),
        "strategy_config_sha256": config.strategy_config_sha256,
        "evaluated_code_commit_sha": kwargs["evaluated_code_commit_sha"],
        "dataset": kwargs["dataset"].to_dict(),
        "windows": [window.to_dict() for window in kwargs["windows"]],
        "experiment_protocol_id": kwargs["experiment_protocol_id"],
        "assumptions": kwargs["assumptions"].to_dict(),
        "results": [result.to_dict() for result in kwargs["results"]],
        "selection_reason": kwargs["selection_reason"].value,
        "result_fingerprint": result_fingerprint,
    }


def _scm_snapshot_payload(*, schema_version: int, decision: ReleaseDecision, **kwargs: Any) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "scm_provider": kwargs["scm_provider"],
        "repository": kwargs["repository"],
        "pull_request_number": kwargs["pull_request_number"],
        "review_record_id": kwargs["review_record_id"],
        "reviewer_id": kwargs["reviewer_id"],
        "author_id": kwargs["author_id"],
        "reviewed_at_ms": kwargs["reviewed_at_ms"],
        "decision": decision.value,
        "candidate_fingerprint": kwargs["candidate_fingerprint"],
        "evaluated_code_commit_sha": kwargs["evaluated_code_commit_sha"],
        "statement": kwargs["statement"],
        "review_url": kwargs["review_url"],
    }


def strategy_release_approval_statement(
    candidate_fingerprint: str,
    evaluated_code_commit_sha: str,
) -> str:
    return "\n".join(
        (
            "APPROVED_STRATEGY_RELEASE_V1",
            f"candidate_fingerprint={candidate_fingerprint}",
            f"evaluated_code_commit_sha={evaluated_code_commit_sha}",
        )
    )


def _strategy_release_id(candidate: StrategyReleaseCandidateV1, approval: StrategyReleaseApprovalV1) -> str:
    digest = canonical_sha256({"candidate": candidate.to_dict(), "approval": approval.to_dict()})
    return f"sr1_{digest}"


def _validate_windows(windows: tuple[ExperimentWindow, ...]) -> None:
    expected_roles = tuple(ExperimentWindowRole)
    if tuple(window.role for window in windows) != expected_roles:
        raise ValueError("candidate requires TRAIN, VALIDATION, and OUT_OF_SAMPLE windows in order")
    for previous, current in zip(windows, windows[1:]):
        if previous.end_ms != current.start_ms:
            raise ValueError("candidate experiment windows must be contiguous")


def _require_decimal_range(
    value: Any,
    label: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    exclusive_minimum: bool = False,
) -> Decimal:
    text = _require_canonical_decimal(value, label)
    decimal_value = Decimal(text)
    if minimum is not None and (decimal_value < minimum or (exclusive_minimum and decimal_value == minimum)):
        raise ValueError(f"{label} is below its allowed minimum")
    if maximum is not None and decimal_value > maximum:
        raise ValueError(f"{label} is above its allowed maximum")
    return decimal_value


def _require_exact_mapping(payload: Any, expected: set[str], label: str) -> None:
    if not isinstance(payload, dict):
        raise StrategyReleaseSchemaError(f"{label} must be an object")
    actual = set(payload)
    if unknown := actual - expected:
        raise StrategyReleaseSchemaError(f"{label} has unknown fields: {sorted(unknown)}")
    if missing := expected - actual:
        raise StrategyReleaseSchemaError(f"{label} has missing fields: {sorted(missing)}")


def _required_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload[field_name]
    if not isinstance(value, str) or not value:
        raise StrategyReleaseSchemaError(f"{field_name} must be non-empty text")
    return value


def _required_int(payload: Mapping[str, Any], field_name: str) -> int:
    value = payload[field_name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise StrategyReleaseSchemaError(f"{field_name} must be an integer")
    return value


def _required_list(payload: Mapping[str, Any], field_name: str) -> list[Any]:
    value = payload[field_name]
    if not isinstance(value, list):
        raise StrategyReleaseSchemaError(f"{field_name} must be a list")
    return value


def _required_text_tuple(payload: Mapping[str, Any], field_name: str) -> tuple[str, ...]:
    value = _required_list(payload, field_name)
    if not value or not all(isinstance(item, str) and item for item in value):
        raise StrategyReleaseSchemaError(f"{field_name} must be a non-empty text list")
    return tuple(value)
