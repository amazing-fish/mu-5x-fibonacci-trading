from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

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
