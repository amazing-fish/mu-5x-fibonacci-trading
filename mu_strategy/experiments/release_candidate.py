from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.market_data.trusted_data.contracts import (
    AvailabilityState,
    IntegrityState,
    ManifestSchemaError,
    RefreshAttemptStatus,
    SnapshotUsability,
    trusted_manifest_snapshot_from_dict,
)
from mu_strategy.market_data.trusted_data.store import (
    TrustedDataStore,
    candles_content_sha256,
    validate_storage_segment,
)
from mu_strategy.models import Candle
from mu_strategy.research.strategy_releases import TrustedExperimentDatasetV1
from mu_strategy.research.strategy_releases import (
    BacktestAssumptionsV1,
    ExperimentWindow,
    ExperimentWindowResultV1,
    ExperimentWindowRole,
    FillModel,
    PartialFillModel,
)
from mu_strategy.strategy import StrategyConfig


class HistoricalGenerationError(ValueError):
    pass


@dataclass(frozen=True)
class HistoricalTrustedGeneration:
    reference: TrustedExperimentDatasetV1
    candles_by_interval: Mapping[str, tuple[Candle, ...]]
    published_freshness_by_interval: Mapping[str, str]
    completed_at_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candles_by_interval",
            MappingProxyType({key: tuple(value) for key, value in self.candles_by_interval.items()}),
        )
        object.__setattr__(
            self,
            "published_freshness_by_interval",
            MappingProxyType(dict(self.published_freshness_by_interval)),
        )


class HistoricalTrustedGenerationReader:
    def __init__(self, *, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.store = TrustedDataStore(data_dir=self.data_dir)

    def read(self, *, run_id: str, symbol: str) -> HistoricalTrustedGeneration:
        try:
            run_id = validate_storage_segment(run_id, field="run_id")
            symbol = validate_storage_segment(symbol, field="symbol")
        except ValueError as exc:
            raise HistoricalGenerationError(str(exc)) from exc

        generation_root = self.store.generation_root(run_id)
        manifest_path = generation_root / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HistoricalGenerationError(f"historical manifest read failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise HistoricalGenerationError("historical manifest root must be an object")
        try:
            snapshot = trusted_manifest_snapshot_from_dict(payload)
        except (ManifestSchemaError, TypeError, ValueError) as exc:
            raise HistoricalGenerationError(f"historical manifest schema_version/contract invalid: {exc}") from exc

        if snapshot.run_id != run_id:
            raise HistoricalGenerationError("historical manifest run_id must match generation directory")
        if snapshot.attempt_status is RefreshAttemptStatus.FAILED:
            raise HistoricalGenerationError("historical generation attempt_status is failed")
        if snapshot.snapshot_usability is not SnapshotUsability.USABLE:
            raise HistoricalGenerationError("historical generation snapshot_usability must be usable")

        candles_by_interval: dict[str, tuple[Candle, ...]] = {}
        freshness_by_interval: dict[str, str] = {}
        content_hashes: list[tuple[str, str]] = []
        for interval in snapshot.effective_intervals:
            health = snapshot.datasets.get((symbol, interval))
            if health is None:
                raise HistoricalGenerationError(f"historical manifest is missing {symbol}/{interval}")
            if health.availability is not AvailabilityState.AVAILABLE:
                raise HistoricalGenerationError(f"historical dataset {symbol}/{interval} is unavailable")
            if health.integrity is not IntegrityState.VALID:
                raise HistoricalGenerationError(f"historical dataset {symbol}/{interval} integrity is not valid")
            if not health.content_sha256:
                raise HistoricalGenerationError(f"historical dataset {symbol}/{interval} has no content SHA-256")

            expected_source = self.store.generation_source_file(symbol, interval)
            if health.source_file.as_posix() != expected_source.as_posix():
                raise HistoricalGenerationError(
                    f"historical dataset source_file must equal {expected_source.as_posix()}"
                )
            csv_path = generation_root / expected_source
            try:
                candles = self.store.read_csv(csv_path)
            except Exception as exc:
                raise HistoricalGenerationError(
                    f"historical dataset read failed for {symbol}/{interval}: {type(exc).__name__}"
                ) from exc
            if len(candles) != health.rows:
                raise HistoricalGenerationError(f"historical dataset row count mismatch for {symbol}/{interval}")
            actual_hash = candles_content_sha256(candles)
            if actual_hash != health.content_sha256:
                raise HistoricalGenerationError(f"historical dataset content SHA-256 mismatch for {symbol}/{interval}")

            candles_by_interval[interval] = tuple(candles)
            freshness_by_interval[interval] = health.freshness.value
            content_hashes.append((interval, actual_hash))

        reference = TrustedExperimentDatasetV1(
            run_id=run_id,
            symbol=symbol,
            manifest_schema_version=snapshot.schema_version,
            requested_intervals=snapshot.requested_intervals,
            effective_intervals=snapshot.effective_intervals,
            content_sha256_by_interval=tuple(sorted(content_hashes)),
        )
        return HistoricalTrustedGeneration(
            reference=reference,
            candles_by_interval=candles_by_interval,
            published_freshness_by_interval=freshness_by_interval,
            completed_at_ms=snapshot.completed_at_ms,
        )


def run_release_experiment(
    generation: HistoricalTrustedGeneration,
    *,
    config: StrategyConfig,
    windows: tuple[ExperimentWindow, ...],
    assumptions: BacktestAssumptionsV1,
) -> tuple[ExperimentWindowResultV1, ...]:
    _validate_runner_assumptions(config, assumptions)
    _validate_runner_windows(generation, windows)
    candles_15m = tuple(generation.candles_by_interval.get("15m", ()))
    candles_1h = tuple(generation.candles_by_interval.get("1h", ()))
    hourly_interval_ms = _infer_interval_ms(candles_1h)
    summaries: list[ExperimentWindowResultV1] = []

    for window in windows:
        segment_15m = [
            candle for candle in candles_15m if window.start_ms <= candle.open_time_ms < window.end_ms
        ]
        segment_1h = [
            candle
            for candle in candles_1h
            if candle.open_time_ms < window.end_ms
            and candle.open_time_ms + hourly_interval_ms > window.start_ms
        ]
        if not segment_15m or not segment_1h:
            raise ValueError(f"experiment window {window.role.value} has no required candles")
        context = build_hourly_context(segment_15m, segment_1h)
        result = run_backtest(
            segment_15m,
            context,
            config=config,
            starting_equity=float(assumptions.starting_equity),
        )
        gross_profit = sum(trade.pnl for trade in result.trades if trade.pnl > 0)
        gross_loss = -sum(trade.pnl for trade in result.trades if trade.pnl < 0)
        summaries.append(
            ExperimentWindowResultV1.create(
                role=window.role,
                trade_count=result.trade_count,
                starting_equity=_quantized_decimal(result.starting_equity),
                ending_equity=_quantized_decimal(result.ending_equity),
                gross_profit=_quantized_decimal(gross_profit),
                gross_loss=_quantized_decimal(gross_loss),
                total_return_pct=_quantized_decimal(result.total_return_pct),
                max_drawdown_pct=_quantized_decimal(result.max_drawdown_pct),
            )
        )
    return tuple(summaries)


def _validate_runner_assumptions(config: StrategyConfig, assumptions: BacktestAssumptionsV1) -> None:
    if assumptions.fee_profile != config.fee_profile or Decimal(assumptions.fee_rate) != Decimal(str(config.fee_rate)):
        raise ValueError("experiment fee assumptions must match the strategy config")
    if assumptions.fill_model is not FillModel.DETERMINISTIC_OHLC:
        raise ValueError("unsupported experiment fill model")
    if Decimal(assumptions.slippage_bps) != 0:
        raise ValueError("v1 experiment requires zero explicit slippage")
    if assumptions.partial_fill_model is not PartialFillModel.NONE:
        raise ValueError("v1 experiment does not model partial fills")


def _validate_runner_windows(
    generation: HistoricalTrustedGeneration,
    windows: tuple[ExperimentWindow, ...],
) -> None:
    if tuple(window.role for window in windows) != tuple(ExperimentWindowRole):
        raise ValueError("experiment windows must be TRAIN, VALIDATION, and OUT_OF_SAMPLE")
    for previous, current in zip(windows, windows[1:]):
        if previous.end_ms != current.start_ms:
            raise ValueError("experiment windows must be contiguous")
    candles = tuple(generation.candles_by_interval.get("15m", ()))
    if not candles:
        raise ValueError("historical generation has no 15m candles")
    interval_ms = _infer_interval_ms(candles)
    data_start = min(candle.open_time_ms for candle in candles)
    data_end = max(candle.open_time_ms for candle in candles) + interval_ms
    if windows[0].start_ms < data_start or windows[-1].end_ms > data_end:
        raise ValueError("experiment window is outside pinned data")


def _infer_interval_ms(candles: tuple[Candle, ...]) -> int:
    ordered = sorted({candle.open_time_ms for candle in candles})
    deltas = [later - earlier for earlier, later in zip(ordered, ordered[1:]) if later > earlier]
    if not deltas:
        raise ValueError("cannot infer candle interval")
    return min(deltas)


def _quantized_decimal(value: int | float | Decimal) -> str:
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("experiment metrics must be finite")
    quantized = decimal_value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        return "0"
    return format(quantized.normalize(), "f")
