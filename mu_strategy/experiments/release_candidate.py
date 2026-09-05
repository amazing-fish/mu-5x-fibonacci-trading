from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
# Keep the existing import boundary for release-candidate callers.
from mu_strategy.research.historical_data import (
    HistoricalGenerationError,
    HistoricalTrustedGeneration,
    HistoricalTrustedGenerationReader,
)
from mu_strategy.research.strategy_releases import (
    BacktestAssumptionsV1, ExperimentWindow, ExperimentWindowResultV1, ExperimentWindowRole,
    validate_release_experiment_assumptions,
)
from mu_strategy.strategy import StrategyConfig


_RELEASE_INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000}


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
