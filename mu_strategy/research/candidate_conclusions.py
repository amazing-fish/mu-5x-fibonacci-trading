from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from enum import Enum
from pathlib import Path
from typing import Any


STRATEGY_LADDER_PROTOCOL_VERSION = "strategy-ladder-v1"
STRATEGY_LADDER_DEFAULT_FEE_BPS = 5
STRATEGY_LADDER_FEE_GRID_BPS = (0, 5, 10)
STRATEGY_LADDER_SLIPPAGE_GRID_TICKS = (0, 1, 2)
STRATEGY_LADDER_TOP_N = 5
STRATEGY_LADDER_FAMILY_SCHEMA = (
    (
        "overnight_seasonality",
        "issue-93 overnight seasonality hypothesis",
        ("overnight_seasonality",),
    ),
    (
        "time_series_momentum",
        "issue-93 trailing-return-sign hypothesis",
        (
            "time_series_momentum_24h",
            "time_series_momentum_96h",
            "time_series_momentum_168h",
        ),
    ),
    (
        "baseline",
        "mu_strategy.strategies.registry:baseline",
        ("baseline",),
    ),
)


class CandidateConclusionError(ValueError):
    """Raised when the candidate conclusion index violates its strict contract."""


class CandidateStatus(str, Enum):
    STRESS_FAILED = "stress_failed"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class FeeAssumption:
    default_fee_bps_per_side: int
    fee_grid_bps_per_side: tuple[int, ...]
    slippage_grid_ticks: tuple[int, ...]
    tick_size: str

    def __post_init__(self) -> None:
        if type(self.default_fee_bps_per_side) is not int or self.default_fee_bps_per_side < 0:
            raise CandidateConclusionError("default fee must be non-negative")
        if (
            not isinstance(self.fee_grid_bps_per_side, tuple)
            or not self.fee_grid_bps_per_side
            or any(type(value) is not int or value < 0 for value in self.fee_grid_bps_per_side)
        ):
            raise CandidateConclusionError("fee grid must contain non-negative values")
        if (
            not isinstance(self.slippage_grid_ticks, tuple)
            or not self.slippage_grid_ticks
            or any(type(value) is not int or value < 0 for value in self.slippage_grid_ticks)
        ):
            raise CandidateConclusionError("slippage grid must contain non-negative values")
        if self.default_fee_bps_per_side not in self.fee_grid_bps_per_side:
            raise CandidateConclusionError("default fee must be present in the fee grid")
        if 0 not in self.slippage_grid_ticks:
            raise CandidateConclusionError("zero slippage must be present in the slippage grid")
        if not isinstance(self.tick_size, str) or not self.tick_size:
            raise CandidateConclusionError("tick_size is required")
        tick_size = _canonical_decimal(self.tick_size, "tick_size")
        if tick_size <= 0:
            raise CandidateConclusionError("tick_size must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_fee_bps_per_side": self.default_fee_bps_per_side,
            "fee_grid_bps_per_side": list(self.fee_grid_bps_per_side),
            "slippage_grid_ticks": list(self.slippage_grid_ticks),
            "tick_size": self.tick_size,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "FeeAssumption":
        _require_exact_keys(
            payload,
            {
                "default_fee_bps_per_side",
                "fee_grid_bps_per_side",
                "slippage_grid_ticks",
                "tick_size",
            },
            "fee_assumption",
        )
        return cls(
            default_fee_bps_per_side=_require_int(payload, "default_fee_bps_per_side"),
            fee_grid_bps_per_side=_require_int_tuple(payload, "fee_grid_bps_per_side"),
            slippage_grid_ticks=_require_int_tuple(payload, "slippage_grid_ticks"),
            tick_size=_require_text(payload, "tick_size"),
        )


@dataclass(frozen=True)
class CandidateRobustness:
    candidate_id: str
    total_return_pct: str
    max_drawdown_pct: str
    trade_count: int
    win_rate: str
    top_n: int
    top_n_trade_concentration: str | None
    survives_stress_grid: bool

    def __post_init__(self) -> None:
        for field_name in ("candidate_id", "total_return_pct", "max_drawdown_pct", "win_rate"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise CandidateConclusionError(f"{field_name} is required")
        if (
            type(self.trade_count) is not int
            or type(self.top_n) is not int
            or self.trade_count < 0
            or self.top_n < 0
        ):
            raise CandidateConclusionError("trade_count and top_n must be non-negative")
        if self.top_n_trade_concentration is not None and (
            not isinstance(self.top_n_trade_concentration, str) or not self.top_n_trade_concentration
        ):
            raise CandidateConclusionError("top_n_trade_concentration must be text or null")
        if not isinstance(self.survives_stress_grid, bool):
            raise CandidateConclusionError("survives_stress_grid must be a boolean")
        total_return = _canonical_decimal_metric(self.total_return_pct, "total_return_pct")
        max_drawdown = _canonical_decimal_metric(self.max_drawdown_pct, "max_drawdown_pct")
        if max_drawdown > 0:
            raise CandidateConclusionError("max_drawdown_pct cannot be positive")
        win_rate = _canonical_decimal_metric(self.win_rate, "win_rate")
        if not Decimal("0") <= win_rate <= Decimal("1"):
            raise CandidateConclusionError("win_rate must be between zero and one")
        concentration = None
        if self.top_n_trade_concentration is not None:
            concentration = _canonical_decimal_metric(
                self.top_n_trade_concentration,
                "top_n_trade_concentration",
            )
            if concentration < 0:
                raise CandidateConclusionError("top_n_trade_concentration cannot be negative")
        if self.survives_stress_grid and self.trade_count == 0:
            raise CandidateConclusionError("surviving stress evidence requires at least one trade")
        if self.survives_stress_grid and total_return < 0:
            raise CandidateConclusionError("surviving stress evidence cannot have negative return")
        if self.trade_count == 0 and (
            total_return != 0
            or max_drawdown != 0
            or win_rate != 0
            or concentration is not None
        ):
            raise CandidateConclusionError("zero-trade evidence must contain only zero metrics and null concentration")
        if not _has_compatible_win_count(self.win_rate, self.trade_count):
            raise CandidateConclusionError("win_rate must correspond to an integer win count")
        if self.trade_count > 0 and total_return > 0 and not _has_compatible_win_count(
            self.win_rate,
            self.trade_count,
            minimum=1,
        ):
            raise CandidateConclusionError("positive return requires at least one winning trade")
        if self.trade_count > 0 and total_return < 0 and not _has_compatible_win_count(
            self.win_rate,
            self.trade_count,
            maximum=self.trade_count - 1,
        ):
            raise CandidateConclusionError("negative return requires at least one non-winning trade")
        if total_return < 0 and max_drawdown > total_return:
            raise CandidateConclusionError(
                "max_drawdown_pct cannot be shallower than negative total_return_pct"
            )
        if (total_return > 0) != (concentration is not None):
            raise CandidateConclusionError(
                "top_n_trade_concentration must be present exactly when total_return_pct is positive"
            )
        if concentration is not None:
            if self.top_n == 0 and concentration != 0:
                raise CandidateConclusionError("zero top_n requires zero trade concentration")
            compatible_wins = _compatible_win_count_bounds(self.win_rate, self.trade_count)
            if compatible_wins is None:
                raise CandidateConclusionError("win_rate must correspond to an integer win count")
            minimum_wins, maximum_wins = compatible_wins
            minimum_concentration = _quantized_fraction(
                min(self.top_n, maximum_wins),
                maximum_wins,
            )
            if concentration < minimum_concentration:
                raise CandidateConclusionError("trade concentration is below the top_n coverage bound")
            if minimum_wins == self.trade_count and concentration > 1:
                raise CandidateConclusionError("all-winning evidence cannot have concentration above one")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "total_return_pct": self.total_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "top_n": self.top_n,
            "top_n_trade_concentration": self.top_n_trade_concentration,
            "survives_stress_grid": self.survives_stress_grid,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "CandidateRobustness":
        _require_exact_keys(
            payload,
            {
                "candidate_id",
                "total_return_pct",
                "max_drawdown_pct",
                "trade_count",
                "win_rate",
                "top_n",
                "top_n_trade_concentration",
                "survives_stress_grid",
            },
            "robustness metric",
        )
        concentration = payload["top_n_trade_concentration"]
        if concentration is not None and not isinstance(concentration, str):
            raise CandidateConclusionError("top_n_trade_concentration must be text or null")
        survives = payload["survives_stress_grid"]
        if not isinstance(survives, bool):
            raise CandidateConclusionError("survives_stress_grid must be a boolean")
        return cls(
            candidate_id=_require_text(payload, "candidate_id"),
            total_return_pct=_require_text(payload, "total_return_pct"),
            max_drawdown_pct=_require_text(payload, "max_drawdown_pct"),
            trade_count=_require_int(payload, "trade_count"),
            win_rate=_require_text(payload, "win_rate"),
            top_n=_require_int(payload, "top_n"),
            top_n_trade_concentration=concentration,
            survives_stress_grid=survives,
        )


@dataclass(frozen=True)
class CandidateConclusion:
    family: str
    source: str
    protocol_version: str
    fee_assumption: FeeAssumption
    robustness_metrics: tuple[CandidateRobustness, ...]
    status: CandidateStatus

    def __post_init__(self) -> None:
        for field_name in ("family", "source", "protocol_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise CandidateConclusionError(f"{field_name} is required")
        if not isinstance(self.fee_assumption, FeeAssumption):
            raise CandidateConclusionError("fee_assumption must be FeeAssumption")
        if not isinstance(self.status, CandidateStatus):
            raise CandidateConclusionError("status must be stress_failed or candidate")
        if (
            not isinstance(self.robustness_metrics, tuple)
            or not self.robustness_metrics
            or any(not isinstance(metric, CandidateRobustness) for metric in self.robustness_metrics)
        ):
            raise CandidateConclusionError("robustness_metrics must not be empty")
        candidate_ids = [metric.candidate_id for metric in self.robustness_metrics]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise CandidateConclusionError("candidate ids must be unique within a family")
        expected_status = (
            CandidateStatus.CANDIDATE
            if any(metric.survives_stress_grid for metric in self.robustness_metrics)
            else CandidateStatus.STRESS_FAILED
        )
        if self.status is not expected_status:
            raise CandidateConclusionError("candidate status contradicts robustness metrics")

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "source": self.source,
            "protocol_version": self.protocol_version,
            "fee_assumption": self.fee_assumption.to_dict(),
            "robustness_metrics": [metric.to_dict() for metric in self.robustness_metrics],
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "CandidateConclusion":
        _require_exact_keys(
            payload,
            {"family", "source", "protocol_version", "fee_assumption", "robustness_metrics", "status"},
            "conclusion entry",
        )
        raw_status = _require_text(payload, "status")
        try:
            status = CandidateStatus(raw_status)
        except ValueError as exc:
            raise CandidateConclusionError(f"unsupported candidate status: {raw_status}") from exc
        metrics = payload["robustness_metrics"]
        if not isinstance(metrics, list):
            raise CandidateConclusionError("robustness_metrics must be a list")
        return cls(
            family=_require_text(payload, "family"),
            source=_require_text(payload, "source"),
            protocol_version=_require_text(payload, "protocol_version"),
            fee_assumption=FeeAssumption.from_dict(payload["fee_assumption"]),
            robustness_metrics=tuple(CandidateRobustness.from_dict(metric) for metric in metrics),
            status=status,
        )


@dataclass(frozen=True)
class CandidateConclusionIndex:
    entries: tuple[CandidateConclusion, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise CandidateConclusionError("unsupported candidate conclusion schema_version")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, CandidateConclusion) for entry in self.entries
        ):
            raise CandidateConclusionError("conclusion entries must be CandidateConclusion values")
        expected_families = tuple(schema[0] for schema in STRATEGY_LADDER_FAMILY_SCHEMA)
        families = tuple(entry.family for entry in self.entries)
        if families != expected_families:
            raise CandidateConclusionError(
                f"conclusion families must be exactly {expected_families} in canonical order"
            )
        shared_fee_assumption = self.entries[0].fee_assumption
        if (
            shared_fee_assumption.default_fee_bps_per_side != STRATEGY_LADDER_DEFAULT_FEE_BPS
            or shared_fee_assumption.fee_grid_bps_per_side != STRATEGY_LADDER_FEE_GRID_BPS
            or shared_fee_assumption.slippage_grid_ticks != STRATEGY_LADDER_SLIPPAGE_GRID_TICKS
        ):
            raise CandidateConclusionError("strategy-ladder-v1 requires its canonical cost grid")
        for entry, (family, source, candidate_ids) in zip(self.entries, STRATEGY_LADDER_FAMILY_SCHEMA):
            if entry.protocol_version != STRATEGY_LADDER_PROTOCOL_VERSION:
                raise CandidateConclusionError("unsupported strategy ladder protocol_version")
            if entry.source != source:
                raise CandidateConclusionError(f"{family} source does not match strategy-ladder-v1")
            if entry.fee_assumption != shared_fee_assumption:
                raise CandidateConclusionError("all conclusion entries must share one fee assumption")
            if tuple(metric.candidate_id for metric in entry.robustness_metrics) != candidate_ids:
                raise CandidateConclusionError(f"{family} candidate ids do not match strategy-ladder-v1")
            if any(metric.top_n != STRATEGY_LADDER_TOP_N for metric in entry.robustness_metrics):
                raise CandidateConclusionError("strategy-ladder-v1 requires top_n=5")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"

    @classmethod
    def from_dict(cls, payload: Any) -> "CandidateConclusionIndex":
        _require_exact_keys(payload, {"schema_version", "entries"}, "conclusion index")
        entries = payload["entries"]
        if not isinstance(entries, list):
            raise CandidateConclusionError("entries must be a list")
        return cls(
            schema_version=_require_int(payload, "schema_version"),
            entries=tuple(CandidateConclusion.from_dict(entry) for entry in entries),
        )

    @classmethod
    def from_json(cls, text: str) -> "CandidateConclusionIndex":
        if not text.strip():
            raise CandidateConclusionError("candidate conclusion index is empty")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CandidateConclusionError(f"candidate conclusion index is malformed: {exc.msg}") from exc
        return cls.from_dict(payload)


def validate_candidate_artifact_path(path: Path) -> Path:
    """Reject targets that could be mistaken for strategy release provenance."""

    target = Path(path).resolve()
    if any(part.lower().replace("_", "-") == "strategy-releases" for part in target.parts):
        raise CandidateConclusionError("candidate conclusions cannot write strategy release provenance")
    return target


def write_candidate_conclusion_index(path: Path, index: CandidateConclusionIndex) -> None:
    """Write only the explicit index target; directory ownership stays with the caller."""

    target = validate_candidate_artifact_path(path)
    target.write_text(index.to_json(), encoding="utf-8", newline="\n")


def read_candidate_conclusion_index(path: Path) -> CandidateConclusionIndex:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CandidateConclusionError("candidate conclusion index is missing") from exc
    except OSError as exc:
        raise CandidateConclusionError(f"candidate conclusion index cannot be read: {exc}") from exc
    return CandidateConclusionIndex.from_json(text)


def _require_exact_keys(payload: Any, expected: set[str], label: str) -> None:
    if not isinstance(payload, dict) or set(payload) != expected:
        raise CandidateConclusionError(f"{label} fields must be exactly {sorted(expected)}")


def _require_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload[field_name]
    if not isinstance(value, str) or not value:
        raise CandidateConclusionError(f"{field_name} must be non-empty text")
    return value


def _require_int(payload: dict[str, Any], field_name: str) -> int:
    value = payload[field_name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise CandidateConclusionError(f"{field_name} must be an integer")
    return value


def _require_int_tuple(payload: dict[str, Any], field_name: str) -> tuple[int, ...]:
    values = payload[field_name]
    if not isinstance(values, list):
        raise CandidateConclusionError(f"{field_name} must be a list")
    return tuple(
        value if isinstance(value, int) and not isinstance(value, bool) else _raise_integer(field_name)
        for value in values
    )


def _raise_integer(field_name: str) -> int:
    raise CandidateConclusionError(f"{field_name} values must be integers")


def _canonical_decimal_metric(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
        canonical = format(parsed.quantize(Decimal("0.00000001")), "f")
    except (InvalidOperation, ValueError) as exc:
        raise CandidateConclusionError(f"{field_name} must be a finite canonical decimal") from exc
    if not parsed.is_finite() or canonical != value:
        raise CandidateConclusionError(f"{field_name} must be a finite canonical decimal")
    return parsed


def _has_compatible_win_count(
    win_rate: str,
    trade_count: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> bool:
    return _compatible_win_count_bounds(
        win_rate,
        trade_count,
        minimum=minimum,
        maximum=maximum,
    ) is not None


def _compatible_win_count_bounds(
    win_rate: str,
    trade_count: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> tuple[int, int] | None:
    maximum = trade_count if maximum is None else maximum
    minimum = max(0, minimum)
    maximum = min(trade_count, maximum)
    if minimum > maximum:
        return None
    if trade_count == 0:
        return (0, 0) if minimum == 0 and maximum == 0 and win_rate == "0.00000000" else None
    with localcontext() as context:
        context.prec = max(28, len(str(trade_count)) + 20)
        parsed = Decimal(win_rate)
        half_unit = Decimal("0.000000005")
        lower_estimate = (parsed - half_unit) * trade_count
        upper_estimate = (parsed + half_unit) * trade_count
        candidates = {minimum, maximum}
        for estimate in (lower_estimate, upper_estimate):
            boundary = int(estimate.to_integral_value(rounding=ROUND_FLOOR))
            candidates.update(range(boundary - 2, boundary + 3))
        compatible = tuple(
            win_count
            for win_count in sorted(candidates)
            if minimum <= win_count <= maximum
            and format(
                (Decimal(win_count) / Decimal(trade_count)).quantize(Decimal("0.00000001")),
                "f",
            )
            == win_rate
        )
        return (compatible[0], compatible[-1]) if compatible else None


def _quantized_fraction(numerator: int, denominator: int) -> Decimal:
    with localcontext() as context:
        context.prec = max(28, len(str(max(numerator, denominator))) + 20)
        return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.00000001"))


def _canonical_decimal(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CandidateConclusionError(f"{field_name} must be a finite canonical decimal") from exc
    if not parsed.is_finite() or format(parsed, "f") != value:
        raise CandidateConclusionError(f"{field_name} must be a finite canonical decimal")
    return parsed
