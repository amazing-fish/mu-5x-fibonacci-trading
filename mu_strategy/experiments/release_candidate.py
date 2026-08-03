from __future__ import annotations

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
    RefreshAttemptStatus,
    SnapshotUsability,
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
    validate_release_experiment_assumptions,
)
from mu_strategy.strategy import StrategyConfig


_RELEASE_INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000}


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
        manifest_result = self.store.read_generation_manifest(run_id)
        if not manifest_result.ok or manifest_result.snapshot is None:
            detail = manifest_result.message or (
                manifest_result.reason.value if manifest_result.reason is not None else "unknown manifest error"
            )
            raise HistoricalGenerationError(f"historical manifest schema_version/contract invalid: {detail}")
        snapshot = manifest_result.snapshot
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

            try:
                candles = self.store.read_generation_dataset(
                    snapshot,
                    symbol=symbol,
                    interval=interval,
                    generation_root=generation_root,
                    generation_id=run_id,
                )
            except Exception as exc:
                raise HistoricalGenerationError(
                    f"historical dataset read failed for {symbol}/{interval}: {type(exc).__name__}: {exc}"
                ) from exc
            actual_hash = candles_content_sha256(candles)

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
    hourly_interval_ms = _RELEASE_INTERVAL_MS["1h"]
    summaries: list[ExperimentWindowResultV1] = []

    for window in windows:
        segment_15m = [
            candle for candle in candles_15m if window.start_ms <= candle.open_time_ms < window.end_ms
        ]
        segment_1h = [
            candle
            for candle in candles_1h
            if window.start_ms <= candle.open_time_ms
            and candle.open_time_ms + hourly_interval_ms <= window.end_ms
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
    validate_release_experiment_assumptions(
        config_fee_profile=config.fee_profile,
        config_fee_rate=config.fee_rate,
        assumptions=assumptions,
    )


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
    interval_ms = _RELEASE_INTERVAL_MS["15m"]
    data_start = min(candle.open_time_ms for candle in candles)
    data_end = max(candle.open_time_ms for candle in candles) + interval_ms
    if windows[0].start_ms < data_start or windows[-1].end_ms > data_end:
        raise ValueError("experiment window is outside pinned data")
    for required_interval, required_interval_ms in _RELEASE_INTERVAL_MS.items():
        interval_candles = tuple(generation.candles_by_interval.get(required_interval, ()))
        for window in windows:
            if (window.end_ms - window.start_ms) % required_interval_ms:
                raise ValueError(f"experiment {required_interval} coverage requires aligned window boundaries")
            expected_opens = tuple(range(window.start_ms, window.end_ms, required_interval_ms))
            actual_opens = tuple(
                sorted(
                    candle.open_time_ms
                    for candle in interval_candles
                    if window.start_ms <= candle.open_time_ms < window.end_ms
                )
            )
            if actual_opens != expected_opens:
                raise ValueError(
                    f"experiment {required_interval} coverage must be complete and gap-free for every window"
                )


def _quantized_decimal(value: int | float | Decimal) -> str:
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("experiment metrics must be finite")
    quantized = decimal_value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        return "0"
    return format(quantized.normalize(), "f")
