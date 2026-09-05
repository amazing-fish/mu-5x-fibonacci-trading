from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from mu_strategy.canonical import canonical_sha256
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
from mu_strategy.market_data.utils import DAY_MS, interval_to_ms
from mu_strategy.models import Candle
from mu_strategy.research.strategy_releases import TrustedExperimentDatasetV1


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
            step = interval_to_ms(interval)
            opens = [bar.open_time_ms for bar in candles]
            if not opens or any(value % step for value in opens) or any(
                right - left != step for left, right in zip(opens, opens[1:])
            ):
                raise HistoricalGenerationError(f"historical dataset {symbol}/{interval} has invalid timing")
            if opens[-1] + step > snapshot.completed_at_ms:
                raise HistoricalGenerationError(f"historical dataset {symbol}/{interval} was not closed at publication")
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


@dataclass(frozen=True)
class HistoricalResearchWindow:
    generation: HistoricalTrustedGeneration
    candles_by_interval: Mapping[str, tuple[Candle, ...]]
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "candles_by_interval", MappingProxyType(dict(self.candles_by_interval)))

    @property
    def data_files(self) -> tuple[Path, ...]:
        # A manifest identifies both flat and segmented storage without inventing CSV paths.
        return (Path("generations") / self.generation.reference.run_id / "manifest.json",)

    def provenance(self, configuration: dict[str, object]) -> str:
        package_root = Path(__file__).resolve().parents[1]
        source_hashes = {
            path.relative_to(package_root).as_posix(): hashlib.sha256(
                path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest()
            for path in sorted(package_root.rglob("*.py"))
        }
        payload = {
            "time_contract": "historical_replay",
            "window_rule": "latest common closed hour in the selected generation; end exclusive",
            "wall_clock_freshness": "not evaluated; research only, not a live trading data decision",
            "dataset": self.generation.reference.to_dict(),
            "published_freshness_by_interval": dict(self.generation.published_freshness_by_interval),
            "published_at_ms": self.generation.completed_at_ms,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "input_content_sha256_by_interval": {
                interval: candles_content_sha256(list(bars))
                for interval, bars in self.candles_by_interval.items()
            },
            "code_identity": "SHA-256 of canonical path/content-hash map of mu_strategy Python sources (LF normalized)",
            "code_sha256": canonical_sha256(source_hashes),
            "configuration": configuration,
            "configuration_sha256": canonical_sha256(configuration),
        }
        return json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True)


def load_historical_window(
    *,
    data_dir: Path,
    generation_id: str,
    symbol: str,
    days: int,
) -> HistoricalResearchWindow:
    """Select an exact research window, independent of current.json and the wall clock."""
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise HistoricalGenerationError("historical window days must be a positive integer")
    generation = HistoricalTrustedGenerationReader(data_dir=data_dir).read(run_id=generation_id, symbol=symbol)
    required = {"15m": 900_000, "1h": 3_600_000}
    if any(not generation.candles_by_interval.get(interval) for interval in required):
        raise HistoricalGenerationError("historical generation requires 15m and 1h data")
    end_ms = min(
        bars[-1].open_time_ms + interval_to_ms(interval)
        for interval, bars in generation.candles_by_interval.items()
    )
    end_ms -= end_ms % 3_600_000
    start_ms = end_ms - days * DAY_MS
    selected: dict[str, tuple[Candle, ...]] = {}
    for interval, step in required.items():
        bars = tuple(
            bar for bar in generation.candles_by_interval[interval]
            if start_ms <= bar.open_time_ms < end_ms
        )
        if (
            len(bars) != days * DAY_MS // step
            or not bars
            or bars[0].open_time_ms != start_ms
            or bars[-1].open_time_ms + step != end_ms
        ):
            raise HistoricalGenerationError(f"historical window has insufficient_coverage:{interval}")
        selected[interval] = bars
    return HistoricalResearchWindow(generation, selected, start_ms, end_ms)


def validate_replay_outputs(data_dir: Path, *paths: Path) -> None:
    data_root = data_dir.resolve()
    if any(path.resolve() == data_root or path.resolve().is_relative_to(data_root) for path in paths):
        raise HistoricalGenerationError("historical reports cannot write inside the trusted data store")


def replay_markdown(provenance: str | None) -> str:
    if provenance is None:
        return ""
    return "\n\n## Historical replay provenance\n\n```json\n" + provenance + "\n```\n"


def replay_html(provenance: str | None) -> str:
    if provenance is None:
        return ""
    return (
        '<section><h2>Historical replay provenance</h2>'
        '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">'
        + html.escape(provenance) + '</pre></section>'
    )
