from __future__ import annotations

from dataclasses import dataclass

from mu_strategy.market_data.utils import dedupe_candles, interval_to_ms
from mu_strategy.models import Candle


@dataclass(frozen=True)
class CandleValidationMismatch:
    open_time_ms: int
    field: str
    built: float | None
    native: float | None


class CandleValidationError(ValueError):
    def __init__(self, mismatches: list[CandleValidationMismatch]):
        self.mismatches = mismatches
        details = "; ".join(
            f"{item.open_time_ms} {item.field}: built={item.built} native={item.native}"
            for item in mismatches[:5]
        )
        suffix = f"; +{len(mismatches) - 5} more" if len(mismatches) > 5 else ""
        super().__init__(f"built candle validation failed: {details}{suffix}")


def aggregate_from_base_interval(
    candles: list[Candle],
    *,
    base_interval: str,
    target_interval: str,
) -> list[Candle]:
    base_ms = interval_to_ms(base_interval)
    target_ms = interval_to_ms(target_interval)
    if target_ms <= base_ms or target_ms % base_ms != 0:
        raise ValueError(f"{target_interval} must be a larger multiple of {base_interval}")

    required_count = target_ms // base_ms
    groups: dict[int, list[Candle]] = {}
    for candle in dedupe_candles(candles):
        bucket_start = (candle.open_time_ms // target_ms) * target_ms
        groups.setdefault(bucket_start, []).append(candle)

    output: list[Candle] = []
    for bucket_start in sorted(groups):
        group = sorted(groups[bucket_start], key=lambda bar: bar.open_time_ms)
        expected_times = [bucket_start + (index * base_ms) for index in range(required_count)]
        actual_times = [bar.open_time_ms for bar in group]
        if actual_times != expected_times:
            continue
        output.append(
            Candle(
                open_time_ms=bucket_start,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
                volume=sum(bar.volume for bar in group),
            )
        )
    return output


def validate_built_candles(
    built: list[Candle],
    native: list[Candle],
    *,
    tolerance: float = 0.000001,
) -> None:
    native_by_time = {bar.open_time_ms: bar for bar in native}
    mismatches: list[CandleValidationMismatch] = []
    for built_bar in built:
        native_bar = native_by_time.get(built_bar.open_time_ms)
        if native_bar is None:
            mismatches.append(CandleValidationMismatch(built_bar.open_time_ms, "missing_native", None, None))
            continue
        for field in ("open", "high", "low", "close", "volume"):
            built_value = getattr(built_bar, field)
            native_value = getattr(native_bar, field)
            if abs(built_value - native_value) > tolerance:
                mismatches.append(
                    CandleValidationMismatch(
                        open_time_ms=built_bar.open_time_ms,
                        field=field,
                        built=built_value,
                        native=native_value,
                    )
                )
    if mismatches:
        raise CandleValidationError(mismatches)
