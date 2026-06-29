from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.market_data.cache import prune_candles_to_window
from mu_strategy.market_data.utils import DAY_MS, interval_to_ms
from mu_strategy.models import Candle


@dataclass(frozen=True)
class CandleWindowPlan:
    days: int
    end_time_ms: int | None
    base_interval: str = "5m"


@dataclass(frozen=True)
class CoverageAssessment:
    covered: bool
    expected_start_ms: int | None
    actual_start_ms: int | None
    actual_end_ms: int | None
    requested_days: int
    effective_days: float | None
    coverage_state: str
    message: str | None


def resolve_shared_window(
    candles_by_interval: dict[str, list[Candle]],
    *,
    days: int,
    base_interval: str = "5m",
) -> CandleWindowPlan:
    base_candles = candles_by_interval.get(base_interval) or []
    if base_candles:
        end_time_ms = max(candle.open_time_ms for candle in base_candles)
    else:
        timestamps = [
            candle.open_time_ms
            for candles in candles_by_interval.values()
            for candle in candles
        ]
        end_time_ms = max(timestamps) if timestamps else None
    return CandleWindowPlan(days=days, end_time_ms=end_time_ms, base_interval=base_interval)


def prune_candle_bundle(
    candles_by_interval: dict[str, list[Candle]],
    *,
    plan: CandleWindowPlan,
) -> dict[str, list[Candle]]:
    return {
        interval: prune_candles_to_window(candles or [], days=plan.days, end_time_ms=plan.end_time_ms)
        for interval, candles in candles_by_interval.items()
    }


def assess_requested_coverage(
    candles: list[Candle],
    *,
    interval: str,
    requested_days: int,
    window_end_time_ms: int | None,
) -> CoverageAssessment:
    if window_end_time_ms is None or not candles:
        return CoverageAssessment(
            covered=True,
            expected_start_ms=None,
            actual_start_ms=candles[0].open_time_ms if candles else None,
            actual_end_ms=candles[-1].open_time_ms if candles else None,
            requested_days=requested_days,
            effective_days=None,
            coverage_state="complete",
            message=None,
        )
    expected_start_ms = window_end_time_ms - (requested_days * DAY_MS)
    actual_start_ms = candles[0].open_time_ms
    actual_end_ms = candles[-1].open_time_ms
    covered = actual_start_ms <= expected_start_ms + interval_to_ms(interval)
    effective_days = max(0.0, (actual_end_ms - actual_start_ms) / DAY_MS)
    coverage_state = "complete" if covered else "partial_available_history"
    message = None
    if not covered:
        message = (
            "partial_available_history:"
            f"requested_days={requested_days} "
            f"effective_days={effective_days:.2f} "
            f"expected_start_ms={expected_start_ms} "
            f"actual_start_ms={actual_start_ms} "
            f"actual_end_ms={actual_end_ms}"
        )
    return CoverageAssessment(
        covered=covered,
        expected_start_ms=expected_start_ms,
        actual_start_ms=actual_start_ms,
        actual_end_ms=actual_end_ms,
        requested_days=requested_days,
        effective_days=effective_days,
        coverage_state=coverage_state,
        message=message,
    )
