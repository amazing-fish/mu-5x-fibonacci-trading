from __future__ import annotations

import hashlib
import json
import multiprocessing
import unittest
from contextlib import chdir
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.experiments.release_candidate import HistoricalTrustedGenerationReader
from mu_strategy.market_data.trusted_data.contracts import (
    HealthReason,
    ManifestSchemaError,
    utc_month_segment_id,
)
from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
from mu_strategy.market_data.trusted_data.policy import observe_only_policy, trading_strict_policy
from mu_strategy.market_data.trusted_data.refresh import (
    RefreshTrustedMarketData,
    RefreshTrustedMarketDataRequest,
)
from mu_strategy.market_data.trusted_data.store import (
    SegmentCorrectionError,
    TrustedDataStore,
    candles_content_sha256,
)
from mu_strategy.market_data.trusted_data.validation import aggregate_candles
from mu_strategy.models import Candle
from mu_strategy.market_data.utils import DAY_MS


SYMBOL = "MU-USDT-SWAP"
STEP_MS = 300_000
JAN_MONTH_START_MS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
JAN_START_MS = int(datetime(2026, 1, 22, tzinfo=timezone.utc).timestamp() * 1000)
FEB_START_MS = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
MAR_START_MS = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
RUN_A = "a" * 32
RUN_B = "b" * 32
RUN_C = "c" * 32
RUN_D = "f" * 32


class MutableHistoryProvider:
    def __init__(self, rows: list[Candle]):
        self.rows = list(rows)
        self.return_all_incremental = False
        self.history_calls = 0
        self.history_days: list[int] = []
        self.incremental_calls = 0

    def fetch_tickers(self):
        raise AssertionError("explicit symbol refresh must not fetch tickers")

    def fetch_history(self, symbol: str, interval: str, *, days: int) -> list[Candle]:
        self.history_calls += 1
        self.history_days.append(days)
        return list(self.rows)

    def fetch_incremental(self, symbol: str, interval: str, *, since_time_ms: int) -> list[Candle]:
        self.incremental_calls += 1
        if self.return_all_incremental:
            return list(self.rows)
        return [candle for candle in self.rows if candle.open_time_ms >= since_time_ms]


class WindowedHistoryProvider:
    def __init__(self, *, now_ms: int):
        self.now_ms = now_ms
        self.history_days: list[int] = []
        self.incremental_calls = 0

    def fetch_tickers(self):
        raise AssertionError("explicit symbol refresh must not fetch tickers")

    def fetch_history(self, symbol: str, interval: str, *, days: int) -> list[Candle]:
        self.history_days.append(days)
        return _window_candles(self.now_ms - days * DAY_MS, self.now_ms - STEP_MS)

    def fetch_incremental(self, symbol: str, interval: str, *, since_time_ms: int) -> list[Candle]:
        self.incremental_calls += 1
        raise AssertionError("expanded logical window must use full history")


class MutableBundleProvider:
    def __init__(self, rows_by_interval: dict[str, list[Candle]]):
        self.rows_by_interval = rows_by_interval
        self.history_calls: list[str] = []
        self.incremental_calls: list[str] = []

    def fetch_tickers(self):
        raise AssertionError("explicit symbol refresh must not fetch tickers")

    def fetch_history(self, symbol: str, interval: str, *, days: int) -> list[Candle]:
        self.history_calls.append(interval)
        return list(self.rows_by_interval[interval])

    def fetch_incremental(self, symbol: str, interval: str, *, since_time_ms: int) -> list[Candle]:
        self.incremental_calls.append(interval)
        return [
            candle
            for candle in self.rows_by_interval[interval]
            if candle.open_time_ms >= since_time_ms
        ]


class UtcMonthPartitionTests(unittest.TestCase):
    def test_partition_key_is_utc_calendar_month_at_boundaries(self):
        cases = (
            (datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc), "1969-12"),
            (datetime(1970, 1, 1, 0, 0, tzinfo=timezone.utc), "1970-01"),
            (datetime(2025, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc), "2025-12"),
            (datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), "2026-01"),
            (datetime(2026, 1, 31, 23, 59, 59, 999000, tzinfo=timezone.utc), "2026-01"),
            (datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc), "2026-02"),
            (datetime(2026, 3, 8, 6, 59, 59, 999000, tzinfo=timezone.utc), "2026-03"),
            (datetime(2026, 3, 8, 7, 0, 0, tzinfo=timezone.utc), "2026-03"),
            (datetime(2026, 11, 1, 5, 59, 59, 999000, tzinfo=timezone.utc), "2026-11"),
            (datetime(2026, 11, 1, 6, 0, 0, tzinfo=timezone.utc), "2026-11"),
        )
        for instant, expected in cases:
            with self.subTest(instant=instant.isoformat()):
                self.assertEqual(expected, utc_month_segment_id(int(instant.timestamp() * 1000)))

    def test_partition_key_rejects_non_integer_and_out_of_range_values(self):
        for value in (True, 1.5, "0", -(10**30), 10**30):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    utc_month_segment_id(value)


class SegmentedRefreshBehaviorTests(unittest.TestCase):
    def test_refresh_writes_only_to_configured_data_dir_not_repository_default(self):
        provider = MutableHistoryProvider(_window_candles(FEB_START_MS, FEB_START_MS + STEP_MS))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            configured_data_dir = root / "configured" / "live"
            store = TrustedDataStore(data_dir=configured_data_dir)

            with chdir(repository):
                run = _refresh(store, provider, RUN_A, days=1)

            self.assertTrue(run.datasets[(SYMBOL, "5m")].is_usable)
            self.assertTrue((configured_data_dir / "current.json").is_file())
            self.assertTrue(store.generation_manifest_path(RUN_A).is_file())
            self.assertTrue(any((configured_data_dir / "segments").rglob("*.csv")))
            self.assertFalse((repository / "data" / "live").exists())

    def test_incremental_partial_history_that_becomes_complete_stays_out_of_canonical_month(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability

        partial_end_ms = JAN_START_MS + (12 * 60 * 60_000)
        complete_end_ms = JAN_START_MS + DAY_MS + (12 * 60 * 60_000)
        provider = MutableHistoryProvider(_window_candles(JAN_START_MS, partial_end_ms))
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            first_run = _refresh(
                store,
                provider,
                RUN_A,
                days=1,
                now_ms=partial_end_ms + STEP_MS,
            )
            first_snapshot = store.read_generation_manifest(RUN_A).snapshot
            first_reference = first_snapshot.storage_by_dataset[(SYMBOL, "5m")].segments[0]
            partial_path = data_dir / first_reference.source_file

            provider.rows = _window_candles(JAN_START_MS, complete_end_ms)
            second_run = _refresh(
                store,
                provider,
                RUN_B,
                days=1,
                now_ms=complete_end_ms + STEP_MS,
            )
            second_result = store.read_generation_manifest(RUN_B)
            second_reference = second_result.snapshot.storage_by_dataset[(SYMBOL, "5m")].segments[0]

            self.assertEqual("partial_available_history", first_run.datasets[(SYMBOL, "5m")].coverage_state)
            self.assertEqual(RefreshAttemptStatus.SUCCESS, second_run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, second_run.snapshot_usability)
            self.assertEqual("complete", second_run.datasets[(SYMBOL, "5m")].coverage_state)
            self.assertTrue(second_result.ok)
            self.assertEqual(first_reference.source_file, second_reference.source_file)
            self.assertGreater(second_reference.start_row, 0)
            self.assertFalse(store.segment_path(SYMBOL, "5m", first_reference.segment_id).exists())
            self.assertEqual(second_run.datasets[(SYMBOL, "5m")].rows, len(_read_exact(store, RUN_B)))

            complete_end_ms += STEP_MS
            provider.rows = _window_candles(JAN_START_MS, complete_end_ms)
            third_run = _refresh(
                store,
                provider,
                RUN_C,
                days=1,
                now_ms=complete_end_ms + STEP_MS,
            )
            third_reference = store.read_generation_manifest(RUN_C).snapshot.storage_by_dataset[
                (SYMBOL, "5m")
            ].segments[0]

            self.assertEqual(RefreshAttemptStatus.SUCCESS, third_run.attempt_status)
            self.assertEqual("complete", third_run.datasets[(SYMBOL, "5m")].coverage_state)
            self.assertEqual(first_reference.source_file, third_reference.source_file)
            self.assertFalse(store.segment_path(SYMBOL, "5m", first_reference.segment_id).exists())

            def failed_incremental(symbol: str, interval: str, *, since_time_ms: int) -> list[Candle]:
                raise TimeoutError("incremental unavailable")

            provider.fetch_incremental = failed_incremental
            failed_run_id = "d" * 32
            failed_run = _refresh(
                store,
                provider,
                failed_run_id,
                days=1,
                now_ms=complete_end_ms + STEP_MS,
            )
            failed_reference = store.read_generation_manifest(failed_run_id).snapshot.storage_by_dataset[
                (SYMBOL, "5m")
            ].segments[0]
            partial_bytes = partial_path.read_bytes()

            self.assertEqual(RefreshAttemptStatus.DEGRADED, failed_run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, failed_run.snapshot_usability)
            self.assertEqual("complete", failed_run.datasets[(SYMBOL, "5m")].coverage_state)
            self.assertEqual(first_reference.source_file, failed_reference.source_file)
            self.assertFalse(store.segment_path(SYMBOL, "5m", first_reference.segment_id).exists())

            january_open_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            provider.rows = _window_candles(january_open_ms, complete_end_ms)
            expanded_run_id = "e" * 32
            expanded_run = _refresh(
                store,
                provider,
                expanded_run_id,
                days=3,
                now_ms=complete_end_ms + STEP_MS,
            )
            expanded_reference = store.read_generation_manifest(expanded_run_id).snapshot.storage_by_dataset[
                (SYMBOL, "5m")
            ].segments[0]

            self.assertEqual(RefreshAttemptStatus.SUCCESS, expanded_run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, expanded_run.snapshot_usability)
            self.assertEqual(store.segment_source_file(SYMBOL, "5m", "2026-01"), expanded_reference.source_file)
            self.assertTrue(store.segment_path(SYMBOL, "5m", "2026-01").exists())
            self.assertEqual(partial_bytes, partial_path.read_bytes())

    def test_canonical_month_rejects_corrections_against_all_compatibility_sources(self):
        compatibility_rows = _window_candles(JAN_START_MS, JAN_START_MS + STEP_MS)
        for compatibility_kind in ("partial", "import"):
            with self.subTest(compatibility_kind=compatibility_kind), TemporaryDirectory() as tmp:
                store = TrustedDataStore(data_dir=Path(tmp))
                kwargs = (
                    {"isolate_partial_start_month": True}
                    if compatibility_kind == "partial"
                    else {"import_generation_id": RUN_A}
                )
                storage = store.write_segmented_dataset(
                    compatibility_rows,
                    symbol=SYMBOL,
                    interval="5m",
                    **kwargs,
                )
                compatibility_path = Path(tmp) / storage.segments[0].source_file
                compatibility_bytes = compatibility_path.read_bytes()
                canonical_rows = _window_candles(
                    JAN_MONTH_START_MS,
                    JAN_START_MS + STEP_MS,
                )
                changed_index = (JAN_START_MS - JAN_MONTH_START_MS) // STEP_MS
                changed = canonical_rows[changed_index]
                canonical_rows[changed_index] = Candle(
                    changed.open_time_ms,
                    changed.open,
                    changed.high + 1.0,
                    changed.low,
                    changed.close + 0.5,
                    changed.volume,
                )

                with self.assertRaisesRegex(
                    SegmentCorrectionError,
                    "historical correction.*segment representation",
                ):
                    store.write_segmented_dataset(
                        canonical_rows,
                        symbol=SYMBOL,
                        interval="5m",
                        require_complete_start_month=True,
                    )

                self.assertFalse(store.segment_path(SYMBOL, "5m", "2026-01").exists())
                self.assertEqual(compatibility_bytes, compatibility_path.read_bytes())

    def test_partial_start_reuses_existing_canonical_month_instead_of_wedging_extension(self):
        january_rows = _window_candles(JAN_MONTH_START_MS, FEB_START_MS - STEP_MS)
        february_rows = _window_candles(FEB_START_MS, FEB_START_MS + STEP_MS)
        canonical_rows = [*january_rows, *february_rows]
        partial_rows = [*january_rows[-2:], *february_rows]

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            store.write_segmented_dataset(canonical_rows, symbol=SYMBOL, interval="5m")
            partial_path = Path(tmp) / store.partial_segment_source_file(
                SYMBOL,
                "5m",
                "2026-01",
                partial_rows[0].open_time_ms,
            )

            storage = store.write_segmented_dataset(
                partial_rows,
                symbol=SYMBOL,
                interval="5m",
                isolate_partial_start_month=True,
            )

            self.assertEqual(
                store.segment_source_file(SYMBOL, "5m", "2026-01"),
                storage.segments[0].source_file,
            )
            self.assertFalse(partial_path.exists())

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            prior_partial = store.write_segmented_dataset(
                partial_rows[:1],
                symbol=SYMBOL,
                interval="5m",
                isolate_partial_start_month=True,
            )
            prior_partial_source = prior_partial.segments[0].source_file
            prior_partial_path = Path(tmp) / prior_partial_source
            prior_partial_bytes = prior_partial_path.read_bytes()
            store.write_segmented_dataset(canonical_rows, symbol=SYMBOL, interval="5m")

            storage = store.write_segmented_dataset(
                partial_rows,
                symbol=SYMBOL,
                interval="5m",
                reuse_partial_start_source_file=prior_partial_source,
            )

            self.assertEqual(
                store.segment_source_file(SYMBOL, "5m", "2026-01"),
                storage.segments[0].source_file,
            )
            self.assertEqual(prior_partial_bytes, prior_partial_path.read_bytes())

    def test_new_compatibility_source_rejects_corrections_against_existing_representations(self):
        canonical_rows = _window_candles(
            JAN_MONTH_START_MS,
            JAN_START_MS + STEP_MS,
        )
        compatibility_rows = _window_candles(JAN_START_MS, JAN_START_MS + STEP_MS)
        changed_rows = list(compatibility_rows)
        changed = changed_rows[0]
        changed_rows[0] = Candle(
            changed.open_time_ms,
            changed.open,
            changed.high + 1.0,
            changed.low,
            changed.close + 0.5,
            changed.volume,
        )
        cases = (
            ("canonical", "partial"),
            ("canonical", "import"),
            ("partial", "import"),
            ("import", "partial"),
        )
        for existing_kind, new_kind in cases:
            with self.subTest(existing_kind=existing_kind, new_kind=new_kind), TemporaryDirectory() as tmp:
                store = TrustedDataStore(data_dir=Path(tmp))
                if existing_kind == "canonical":
                    existing_storage = store.write_segmented_dataset(
                        canonical_rows,
                        symbol=SYMBOL,
                        interval="5m",
                    )
                elif existing_kind == "partial":
                    existing_storage = store.write_segmented_dataset(
                        compatibility_rows,
                        symbol=SYMBOL,
                        interval="5m",
                        isolate_partial_start_month=True,
                    )
                else:
                    existing_storage = store.write_segmented_dataset(
                        compatibility_rows,
                        symbol=SYMBOL,
                        interval="5m",
                        import_generation_id=RUN_A,
                    )
                existing_path = Path(tmp) / existing_storage.segments[0].source_file
                existing_bytes = existing_path.read_bytes()
                if new_kind == "partial":
                    new_path = Path(tmp) / store.partial_segment_source_file(
                        SYMBOL,
                        "5m",
                        "2026-01",
                        JAN_START_MS,
                    )
                    kwargs = {"isolate_partial_start_month": True}
                else:
                    new_path = Path(tmp) / store.imported_segment_source_file(
                        SYMBOL,
                        "5m",
                        "2026-01",
                        RUN_B,
                    )
                    kwargs = {"import_generation_id": RUN_B}

                with self.assertRaisesRegex(
                    SegmentCorrectionError,
                    "historical correction.*trusted segment",
                ):
                    store.write_segmented_dataset(
                        changed_rows,
                        symbol=SYMBOL,
                        interval="5m",
                        **kwargs,
                    )

                self.assertFalse(new_path.exists())
                self.assertEqual(existing_bytes, existing_path.read_bytes())

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            partial_storage = store.write_segmented_dataset(
                compatibility_rows[:1],
                symbol=SYMBOL,
                interval="5m",
                isolate_partial_start_month=True,
            )
            partial_path = Path(tmp) / partial_storage.segments[0].source_file
            partial_bytes = partial_path.read_bytes()
            store.write_segmented_dataset(
                canonical_rows,
                symbol=SYMBOL,
                interval="5m",
            )
            changed_extension = list(compatibility_rows)
            changed = changed_extension[-1]
            changed_extension[-1] = Candle(
                changed.open_time_ms,
                changed.open,
                changed.high + 1.0,
                changed.low,
                changed.close + 0.5,
                changed.volume,
            )

            with self.assertRaisesRegex(
                SegmentCorrectionError,
                "historical correction.*trusted segment",
            ):
                store.write_segmented_dataset(
                    changed_extension,
                    symbol=SYMBOL,
                    interval="5m",
                    isolate_partial_start_month=True,
                )

            self.assertEqual(partial_bytes, partial_path.read_bytes())

    def test_invalid_physical_lookbehind_is_not_written_and_corrected_retry_succeeds(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability

        complete = _window_candles(0, 2 * DAY_MS - STEP_MS)
        missing_lookbehind = complete[:10] + complete[11:]
        complete_fifteen = aggregate_candles(complete, interval="15m", ohlc_policy="okx_native")
        provider = MutableBundleProvider({"5m": missing_lookbehind, "15m": complete_fifteen})
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            first_run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=2 * DAY_MS,
                    run_id=RUN_A,
                )
            )

            self.assertEqual(RefreshAttemptStatus.FAILED, first_run.attempt_status)
            self.assertEqual(SnapshotUsability.INVALID, first_run.snapshot_usability)
            self.assertEqual(HealthReason.TIMESTAMP_GAP, first_run.datasets[(SYMBOL, "5m")].primary_reason)
            self.assertEqual(HealthReason.TIMESTAMP_GAP, first_run.datasets[(SYMBOL, "15m")].primary_reason)
            self.assertFalse(store.segment_path(SYMBOL, "5m", "1970-01").exists())
            self.assertFalse(store.segment_path(SYMBOL, "15m", "1970-01").exists())
            first_manifest = store.read_generation_manifest(RUN_A).snapshot
            self.assertEqual((), first_manifest.storage_by_dataset[(SYMBOL, "5m")].segments)
            self.assertEqual((), first_manifest.storage_by_dataset[(SYMBOL, "15m")].segments)

            provider.rows_by_interval["5m"] = complete
            second_run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=2 * DAY_MS,
                    run_id=RUN_B,
                )
            )

            self.assertEqual(RefreshAttemptStatus.SUCCESS, second_run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, second_run.snapshot_usability)
            self.assertEqual(2, provider.history_calls.count("5m"))
            self.assertTrue(store.segment_path(SYMBOL, "5m", "1970-01").exists())
            self.assertTrue(store.segment_path(SYMBOL, "15m", "1970-01").exists())
            second_rows = _read_exact(store, RUN_B)
            self.assertEqual(complete[-len(second_rows) :], second_rows)
            second_fifteen = _read_exact_interval(store, RUN_B, "15m")
            self.assertEqual(complete_fifteen[-len(second_fifteen) :], second_fifteen)

    def test_built_native_mismatch_does_not_write_segment_and_corrected_retry_succeeds(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability

        five = _window_candles(0, DAY_MS - STEP_MS)
        correct_fifteen = aggregate_candles(five, interval="15m", ohlc_policy="okx_native")
        mismatched_fifteen = list(correct_fifteen)
        first = mismatched_fifteen[0]
        mismatched_fifteen[0] = Candle(
            first.open_time_ms,
            first.open,
            first.high + 1.0,
            first.low,
            first.close,
            first.volume,
        )
        provider = MutableBundleProvider({"5m": five, "15m": mismatched_fifteen})
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            first_run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=DAY_MS,
                    run_id=RUN_A,
                )
            )

            self.assertEqual(RefreshAttemptStatus.DEGRADED, first_run.attempt_status)
            self.assertEqual(SnapshotUsability.INVALID, first_run.snapshot_usability)
            self.assertEqual(HealthReason.OHLCV_MISMATCH, first_run.datasets[(SYMBOL, "15m")].primary_reason)
            self.assertFalse(store.segment_path(SYMBOL, "15m", "1970-01").exists())
            first_manifest = store.read_generation_manifest(RUN_A).snapshot
            self.assertEqual((), first_manifest.storage_by_dataset[(SYMBOL, "15m")].segments)

            provider.rows_by_interval["15m"] = correct_fifteen
            second_run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=DAY_MS,
                    run_id=RUN_B,
                )
            )

            self.assertEqual(RefreshAttemptStatus.SUCCESS, second_run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, second_run.snapshot_usability)
            self.assertIn("5m", provider.incremental_calls)
            self.assertEqual(2, provider.history_calls.count("15m"))
            self.assertTrue(store.segment_path(SYMBOL, "15m", "1970-01").exists())
            self.assertEqual(correct_fifteen, _read_exact_interval(store, RUN_B, "15m"))

    def test_uncovered_native_edge_is_not_persisted_or_wedged_on_later_window_expansion(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability

        five = _window_candles(0, 2 * DAY_MS - STEP_MS)
        correct_fifteen = aggregate_candles(five, interval="15m", ohlc_policy="okx_native")
        edge_open = correct_fifteen[-1].close
        bad_edge = Candle(2 * DAY_MS, edge_open, edge_open + 1.0, edge_open - 1.0, edge_open + 0.25, 1.0)
        provider = MutableBundleProvider({"5m": five, "15m": [*correct_fifteen, bad_edge]})
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            first_run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=2 * DAY_MS,
                    run_id=RUN_A,
                )
            )

            self.assertEqual(RefreshAttemptStatus.SUCCESS, first_run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, first_run.snapshot_usability)
            physical_fifteen = store.read_csv(store.segment_path(SYMBOL, "15m", "1970-01"))
            self.assertNotIn(bad_edge.open_time_ms, {candle.open_time_ms for candle in physical_fifteen})

            expanded_five = _window_candles(0, 2 * DAY_MS + 2 * STEP_MS)
            expanded_fifteen = aggregate_candles(expanded_five, interval="15m", ohlc_policy="okx_native")
            provider.rows_by_interval["5m"] = expanded_five
            provider.rows_by_interval["15m"] = expanded_fifteen
            second_run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=2 * DAY_MS + 3 * STEP_MS,
                    run_id=RUN_B,
                )
            )

            self.assertEqual(RefreshAttemptStatus.SUCCESS, second_run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, second_run.snapshot_usability)
            second_fifteen = _read_exact_interval(store, RUN_B, "15m")
            self.assertEqual(expanded_fifteen[-len(second_fifteen) :], second_fifteen)

    def test_multi_cycle_growth_is_new_bytes_plus_metadata_and_closed_segment_is_unchanged(self):
        initial_rows = _candles_through(FEB_START_MS)
        provider = MutableHistoryProvider(initial_rows)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _refresh(store, provider, RUN_A)
            first_tree = _file_sizes(data_dir)
            first_storage = store.read_generation_manifest(RUN_A).snapshot.storage_by_dataset[(SYMBOL, "5m")]
            january_path = data_dir / first_storage.segments[0].source_file
            january_bytes = january_path.read_bytes()
            january_sha256 = hashlib.sha256(january_bytes).hexdigest()
            january_mtime_ns = january_path.stat().st_mtime_ns
            initial_segment_bytes = _segment_bytes(data_dir)
            average_initial_row_bytes = initial_segment_bytes / len(initial_rows)
            self.assertLess(first_tree[f"generations/{RUN_A}/manifest.json"], 10 * 1024)

            provider.rows = _candles_through(FEB_START_MS + 6 * STEP_MS)
            _refresh(store, provider, RUN_B)
            second_tree = _file_sizes(data_dir)
            second_segment_bytes = _segment_bytes(data_dir)
            second_delta = sum(second_tree.values()) - sum(first_tree.values())
            second_segment_delta = second_segment_bytes - initial_segment_bytes
            second_manifest_bytes = second_tree[f"generations/{RUN_B}/manifest.json"]
            second_log_delta = second_tree["refresh_runs.jsonl"] - first_tree["refresh_runs.jsonl"]
            second_pointer_delta = second_tree["current.json"] - first_tree["current.json"]

            self.assertEqual(
                second_segment_delta + second_manifest_bytes + second_log_delta + second_pointer_delta,
                second_delta,
            )
            self.assertLess(second_delta, initial_segment_bytes // 4)
            self.assertLess(second_manifest_bytes, 10 * 1024)
            self.assertLessEqual(second_segment_delta, 2 * 6 * average_initial_row_bytes)
            self.assertEqual(january_bytes, january_path.read_bytes())
            self.assertEqual(january_sha256, hashlib.sha256(january_path.read_bytes()).hexdigest())
            self.assertEqual(january_mtime_ns, january_path.stat().st_mtime_ns)
            self.assertEqual(1, provider.history_calls)
            self.assertEqual(1, provider.incremental_calls)
            self.assertTrue(_refresh_segment(store, RUN_B)["reused_prior_generation"])
            self.assertEqual("incremental_reuse", _refresh_segment(store, RUN_B)["fetch_mode"])
            self.assertFalse(any((data_dir / "generations").rglob("*.csv")))

            provider.rows = _candles_through(FEB_START_MS + 12 * STEP_MS)
            before_third = _file_sizes(data_dir)
            before_third_segment_bytes = _segment_bytes(data_dir)
            _refresh(store, provider, RUN_C)
            after_third = _file_sizes(data_dir)
            third_segment_delta = _segment_bytes(data_dir) - before_third_segment_bytes
            third_delta = sum(after_third.values()) - sum(before_third.values())
            third_metadata = (
                after_third[f"generations/{RUN_C}/manifest.json"]
                + after_third["refresh_runs.jsonl"]
                - before_third["refresh_runs.jsonl"]
                + after_third["current.json"]
                - before_third["current.json"]
            )

            self.assertEqual(third_segment_delta + third_metadata, third_delta)
            self.assertLess(third_delta, initial_segment_bytes // 4)
            self.assertLessEqual(third_segment_delta, 2 * 6 * average_initial_row_bytes)
            self.assertEqual(january_bytes, january_path.read_bytes())
            self.assertEqual(january_sha256, hashlib.sha256(january_path.read_bytes()).hexdigest())
            self.assertEqual(january_mtime_ns, january_path.stat().st_mtime_ns)

            provider.rows = _candles_through(FEB_START_MS + 18 * STEP_MS)
            before_fourth = _file_sizes(data_dir)
            before_fourth_segment_bytes = _segment_bytes(data_dir)
            _refresh(store, provider, RUN_D)
            after_fourth = _file_sizes(data_dir)
            fourth_segment_delta = _segment_bytes(data_dir) - before_fourth_segment_bytes
            fourth_delta = sum(after_fourth.values()) - sum(before_fourth.values())
            fourth_metadata = (
                after_fourth[f"generations/{RUN_D}/manifest.json"]
                + after_fourth["refresh_runs.jsonl"]
                - before_fourth["refresh_runs.jsonl"]
                + after_fourth["current.json"]
                - before_fourth["current.json"]
            )

            self.assertEqual(fourth_segment_delta + fourth_metadata, fourth_delta)
            self.assertLessEqual(fourth_segment_delta, 2 * 6 * average_initial_row_bytes)
            self.assertEqual(january_bytes, january_path.read_bytes())
            self.assertEqual(january_sha256, hashlib.sha256(january_path.read_bytes()).hexdigest())
            self.assertEqual(january_mtime_ns, january_path.stat().st_mtime_ns)
            self.assertEqual(1, provider.history_calls)
            self.assertEqual(3, provider.incremental_calls)

    def test_trailing_growth_preserves_old_exact_generation_and_current_matches_exact_reader(self):
        first_rows = _candles_through(FEB_START_MS)
        provider = MutableHistoryProvider(first_rows)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _refresh(store, provider, RUN_A)
            old_manifest = store.read_generation_manifest(RUN_A)
            old_hash = old_manifest.snapshot.datasets[(SYMBOL, "5m")].content_sha256

            provider.rows = _candles_through(FEB_START_MS + 8 * STEP_MS)
            _refresh(store, provider, RUN_B)

            old_rows = _read_exact(store, RUN_A)
            current_rows = _read_exact(store, RUN_B)
            old_historical = HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                run_id=RUN_A,
                symbol=SYMBOL,
            )
            current_historical = HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                run_id=RUN_B,
                symbol=SYMBOL,
            )
            current_bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(
                    SYMBOL,
                    intervals=("5m",),
                    days=20,
                    now_ms=provider.rows[-1].open_time_ms + STEP_MS,
                ),
                observe_only_policy(),
            )

        self.assertEqual(first_rows, old_rows)
        self.assertEqual(old_hash, candles_content_sha256(old_rows))
        self.assertEqual(tuple(first_rows), old_historical.candles_by_interval["5m"])
        self.assertEqual(tuple(current_rows), current_historical.candles_by_interval["5m"])
        self.assertEqual(current_rows, current_bundle.candles_by_interval["5m"])
        self.assertEqual(RUN_B, current_bundle.run_id)

    def test_gapped_suffix_cannot_extend_trailing_month(self):
        initial_rows = _window_candles(0, STEP_MS)
        gapped_suffix = _window_candles(3 * STEP_MS, 4 * STEP_MS)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            store.write_segmented_dataset(initial_rows, symbol=SYMBOL, interval="5m")
            segment_path = store.segment_path(SYMBOL, "5m", "1970-01")
            original_bytes = segment_path.read_bytes()

            with self.assertRaisesRegex(SegmentCorrectionError, "timestamp gap"):
                store.write_segmented_dataset(gapped_suffix, symbol=SYMBOL, interval="5m")

            self.assertEqual(original_bytes, segment_path.read_bytes())

    def test_new_month_requires_adjacency_to_existing_predecessor(self):
        initial_rows = _candles_through(FEB_START_MS - 2 * STEP_MS)
        next_month_rows = _window_candles(FEB_START_MS, FEB_START_MS + STEP_MS)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            store.write_segmented_dataset(initial_rows, symbol=SYMBOL, interval="5m")
            january_path = store.segment_path(SYMBOL, "5m", "2026-01")
            february_path = store.segment_path(SYMBOL, "5m", "2026-02")
            january_bytes = january_path.read_bytes()

            with self.assertRaisesRegex(SegmentCorrectionError, "not adjacent"):
                store.write_segmented_dataset(next_month_rows, symbol=SYMBOL, interval="5m")

            self.assertEqual(january_bytes, january_path.read_bytes())
            self.assertFalse(february_path.exists())

            missing_january_tail = _window_candles(FEB_START_MS - STEP_MS, FEB_START_MS - STEP_MS)
            storage = store.write_segmented_dataset(
                [*missing_january_tail, *next_month_rows],
                symbol=SYMBOL,
                interval="5m",
            )

            self.assertTrue(february_path.exists())
            self.assertEqual(("2026-01", "2026-02"), tuple(ref.segment_id for ref in storage.segments))

    def test_physical_lookbehind_extends_existing_predecessor_outside_logical_window(self):
        initial_rows = _candles_through(FEB_START_MS - 2 * STEP_MS)
        missing_january_tail = _window_candles(FEB_START_MS - STEP_MS, FEB_START_MS - STEP_MS)
        february_rows = _window_candles(FEB_START_MS, FEB_START_MS + STEP_MS)
        physical_rows = [*missing_january_tail, *february_rows]

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            store.write_segmented_dataset(initial_rows, symbol=SYMBOL, interval="5m")
            january_path = store.segment_path(SYMBOL, "5m", "2026-01")
            previous_size = january_path.stat().st_size

            storage = store.write_segmented_dataset(
                february_rows,
                symbol=SYMBOL,
                interval="5m",
                physical_candles=physical_rows,
            )

            self.assertGreater(january_path.stat().st_size, previous_size)
            self.assertEqual(
                FEB_START_MS - STEP_MS,
                store.read_csv(january_path)[-1].open_time_ms,
            )
            self.assertEqual(("2026-02",), tuple(ref.segment_id for ref in storage.segments))
            self.assertEqual(
                store.segment_source_file(SYMBOL, "5m", "2026-02"),
                storage.segments[0].source_file,
            )

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))

            storage = store.write_segmented_dataset(
                february_rows,
                symbol=SYMBOL,
                interval="5m",
                physical_candles=physical_rows,
            )

            self.assertFalse(store.segment_path(SYMBOL, "5m", "2026-01").exists())
            self.assertEqual(("2026-02",), tuple(ref.segment_id for ref in storage.segments))

    def test_physical_lookbehind_retains_chain_from_existing_predecessor(self):
        december_start_ms = int(datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)
        initial_rows = _window_candles(
            december_start_ms,
            JAN_MONTH_START_MS - 2 * STEP_MS,
        )
        missing_december_tail = _window_candles(
            JAN_MONTH_START_MS - STEP_MS,
            JAN_MONTH_START_MS - STEP_MS,
        )
        january_rows = _window_candles(JAN_MONTH_START_MS, FEB_START_MS - STEP_MS)
        february_rows = _window_candles(FEB_START_MS, FEB_START_MS + STEP_MS)
        physical_rows = [*missing_december_tail, *january_rows, *february_rows]

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            store.write_segmented_dataset(initial_rows, symbol=SYMBOL, interval="5m")
            december_path = store.segment_path(SYMBOL, "5m", "2025-12")
            january_path = store.segment_path(SYMBOL, "5m", "2026-01")
            previous_size = december_path.stat().st_size

            storage = store.write_segmented_dataset(
                february_rows,
                symbol=SYMBOL,
                interval="5m",
                physical_candles=physical_rows,
            )

            self.assertGreater(december_path.stat().st_size, previous_size)
            self.assertEqual(
                JAN_MONTH_START_MS - STEP_MS,
                store.read_csv(december_path)[-1].open_time_ms,
            )
            self.assertTrue(january_path.exists())
            self.assertEqual(
                FEB_START_MS - STEP_MS,
                store.read_csv(january_path)[-1].open_time_ms,
            )
            self.assertEqual(("2026-02",), tuple(ref.segment_id for ref in storage.segments))

    def test_backfilled_month_requires_adjacency_to_existing_successor(self):
        february_rows = _window_candles(FEB_START_MS, FEB_START_MS + STEP_MS)
        incomplete_january_rows = _window_candles(
            JAN_MONTH_START_MS,
            JAN_MONTH_START_MS + STEP_MS,
        )
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            store.write_segmented_dataset(february_rows, symbol=SYMBOL, interval="5m")
            january_path = store.segment_path(SYMBOL, "5m", "2026-01")
            february_path = store.segment_path(SYMBOL, "5m", "2026-02")
            february_bytes = february_path.read_bytes()

            with self.assertRaisesRegex(SegmentCorrectionError, "not adjacent.*successor"):
                store.write_segmented_dataset(
                    incomplete_january_rows,
                    symbol=SYMBOL,
                    interval="5m",
                )

            self.assertFalse(january_path.exists())
            self.assertEqual(february_bytes, february_path.read_bytes())

            complete_january_rows = _window_candles(
                JAN_MONTH_START_MS,
                FEB_START_MS - STEP_MS,
            )
            storage = store.write_segmented_dataset(
                [*complete_january_rows, *february_rows],
                symbol=SYMBOL,
                interval="5m",
            )

            self.assertTrue(january_path.exists())
            self.assertEqual(february_bytes, february_path.read_bytes())
            self.assertEqual(("2026-01", "2026-02"), tuple(ref.segment_id for ref in storage.segments))

    def test_multi_month_backfill_validates_final_candidate_against_successor(self):
        march_rows = _window_candles(MAR_START_MS, MAR_START_MS + STEP_MS)
        backfill_rows = _window_candles(FEB_START_MS - STEP_MS, MAR_START_MS - STEP_MS)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            store.write_segmented_dataset(march_rows, symbol=SYMBOL, interval="5m")
            march_path = store.segment_path(SYMBOL, "5m", "2026-03")
            march_bytes = march_path.read_bytes()

            storage = store.write_segmented_dataset(
                [*backfill_rows, *march_rows],
                symbol=SYMBOL,
                interval="5m",
            )

            self.assertEqual(
                ("2026-01", "2026-02", "2026-03"),
                tuple(ref.segment_id for ref in storage.segments),
            )
            self.assertTrue(store.segment_path(SYMBOL, "5m", "2026-01").exists())
            self.assertTrue(store.segment_path(SYMBOL, "5m", "2026-02").exists())
            self.assertEqual(march_bytes, march_path.read_bytes())

    def test_shortened_full_history_does_not_create_partial_canonical_start_month(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability

        now_ms = FEB_START_MS + 10 * DAY_MS
        provider = MutableHistoryProvider(
            _window_candles(now_ms - 2 * DAY_MS, now_ms - STEP_MS)
        )
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))

            with self.assertRaisesRegex(SegmentCorrectionError, "complete month lookbehind"):
                RefreshTrustedMarketData(store, provider).execute(
                    RefreshTrustedMarketDataRequest(
                        requested_intervals=("5m",),
                        days=1,
                        symbols=(SYMBOL,),
                        now_ms=now_ms,
                        run_id=RUN_A,
                    )
                )

            self.assertFalse(store.segment_path(SYMBOL, "5m", "2026-02").exists())
            self.assertFalse(store.current_path.exists())

            provider.rows = _window_candles(FEB_START_MS, now_ms - STEP_MS)
            corrected = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=now_ms,
                    run_id=RUN_B,
                )
            )

            self.assertEqual(RefreshAttemptStatus.SUCCESS, corrected.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, corrected.snapshot_usability)
            self.assertTrue(store.segment_path(SYMBOL, "5m", "2026-02").exists())

    def test_closed_segment_historical_correction_fails_before_publication(self):
        initial_rows = _candles_through(FEB_START_MS)
        provider = MutableHistoryProvider(initial_rows)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _refresh(store, provider, RUN_A)
            first_storage = store.read_generation_manifest(RUN_A).snapshot.storage_by_dataset[(SYMBOL, "5m")]
            january_path = data_dir / first_storage.segments[0].source_file
            january_bytes = january_path.read_bytes()
            old_pointer = store.current_path.read_bytes()

            corrected = list(_candles_through(FEB_START_MS + STEP_MS))
            changed = corrected[10]
            corrected[10] = Candle(
                changed.open_time_ms,
                changed.open,
                changed.high + 1.0,
                changed.low,
                changed.close + 0.5,
                changed.volume,
            )
            provider.rows = corrected
            provider.return_all_incremental = True

            with self.assertRaisesRegex(SegmentCorrectionError, "historical correction"):
                _refresh(store, provider, RUN_B)

            self.assertEqual(old_pointer, store.current_path.read_bytes())
            self.assertEqual(january_bytes, january_path.read_bytes())
            self.assertEqual(initial_rows, _read_exact(store, RUN_A))

    def test_closed_segment_cannot_grow_after_a_later_month_exists(self):
        source_rows = _candles_through(FEB_START_MS)
        initial_rows = [source_rows[0], source_rows[-1]]
        extended_rows = [source_rows[0], source_rows[1], source_rows[-1]]
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            january_path = store.segment_path(SYMBOL, "5m", "2026-01")
            february_path = store.segment_path(SYMBOL, "5m", "2026-02")
            store.write_csv([initial_rows[0]], january_path)
            store.write_csv([initial_rows[-1]], february_path)
            january_bytes = january_path.read_bytes()

            with self.assertRaisesRegex(SegmentCorrectionError, "closed trusted segment cannot grow"):
                store.write_segmented_dataset(extended_rows, symbol=SYMBOL, interval="5m")

            self.assertEqual(january_bytes, january_path.read_bytes())

    def test_failure_before_and_after_current_commit_point_has_explicit_visibility(self):
        initial_rows = _candles_through(FEB_START_MS)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            provider = MutableHistoryProvider(initial_rows)
            _refresh(store, provider, RUN_A)
            old_pointer = store.current_path.read_bytes()
            provider.rows = _candles_through(FEB_START_MS + STEP_MS)

            with patch.object(store, "replace_current", side_effect=OSError("pointer offline")):
                with self.assertRaisesRegex(OSError, "pointer offline"):
                    _refresh(store, provider, RUN_B)

            self.assertEqual(old_pointer, store.current_path.read_bytes())
            self.assertEqual(initial_rows, _read_exact(store, RUN_A))

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            provider = MutableHistoryProvider(initial_rows)
            _refresh(store, provider, RUN_A)
            provider.rows = _candles_through(FEB_START_MS + STEP_MS)

            with patch.object(store, "append_run_log", side_effect=OSError("audit offline")):
                run = _refresh(store, provider, RUN_B)

            self.assertEqual(RUN_B, json.loads(store.current_path.read_text(encoding="utf-8"))["generation_id"])
            self.assertIn("audit_log_append_failed: audit offline", run.warnings)
            self.assertEqual(provider.rows, _read_exact(store, RUN_B))

    def test_promotion_revalidates_claimed_segment_content_before_current_replace(self):
        initial_rows = _candles_through(FEB_START_MS)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            provider = MutableHistoryProvider(initial_rows)
            _refresh(store, provider, RUN_A)
            old_pointer = store.current_path.read_bytes()
            provider.rows = _candles_through(FEB_START_MS + STEP_MS)
            original_write_manifest = store.write_generation_manifest

            def write_manifest_then_corrupt(*args, **kwargs):
                result = original_write_manifest(*args, **kwargs)
                segment_path = store.segment_path(SYMBOL, "5m", "2026-02")
                payload = bytearray(segment_path.read_bytes())
                last_line = bytes(payload).rstrip(b"\r\n").rfind(b"\n") + 1
                first_comma = payload.find(b",", last_line)
                second_comma = payload.find(b",", first_comma + 1)
                value_byte = next(
                    index
                    for index in range(second_comma + 1, len(payload))
                    if payload[index] in b"0123456789"
                )
                payload[value_byte] = ord("8") if payload[value_byte] != ord("8") else ord("7")
                segment_path.write_bytes(payload)
                return result

            with patch.object(store, "write_generation_manifest", side_effect=write_manifest_then_corrupt):
                with self.assertRaisesRegex(ManifestSchemaError, "content SHA-256 mismatch"):
                    _refresh(store, provider, RUN_B)

            self.assertEqual(old_pointer, store.current_path.read_bytes())
            self.assertEqual(initial_rows, _read_exact(store, RUN_A))

    def test_uncommitted_partial_tail_is_outside_old_prefix_but_blocks_reuse(self):
        initial_rows = _candles_through(FEB_START_MS)
        provider = MutableHistoryProvider(initial_rows)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _refresh(store, provider, RUN_A)
            tail_path = store.segment_path(SYMBOL, "5m", "2026-02")
            with tail_path.open("ab") as handle:
                handle.write(b"partial")

            current_bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(
                    SYMBOL,
                    intervals=("5m",),
                    days=20,
                    now_ms=initial_rows[-1].open_time_ms + STEP_MS,
                ),
                observe_only_policy(),
            )
            self.assertEqual(initial_rows, current_bundle.candles_by_interval["5m"])

            provider.rows = _candles_through(FEB_START_MS + STEP_MS)
            with self.assertRaises(ManifestSchemaError):
                _refresh(store, provider, RUN_B)

            self.assertEqual(RUN_A, json.loads(store.current_path.read_text(encoding="utf-8"))["generation_id"])

    def test_cross_process_refresh_serializes_tail_and_current_always_names_exact_generation(self):
        initial = _window_candles(FEB_START_MS, FEB_START_MS + 2 * STEP_MS)
        longer = _window_candles(FEB_START_MS, FEB_START_MS + 10 * STEP_MS)
        shorter = _window_candles(FEB_START_MS, FEB_START_MS + 6 * STEP_MS)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _refresh(store, MutableHistoryProvider(initial), RUN_A)
            tail_path = store.segment_path(SYMBOL, "5m", "2026-02")
            context = multiprocessing.get_context("spawn")
            shorter_entered = context.Event()
            release_shorter = context.Event()
            shorter_result = context.Queue()
            longer_result = context.Queue()
            shorter_process = context.Process(
                target=_refresh_in_process,
                args=(str(data_dir), shorter, RUN_C, len(shorter), shorter_entered, release_shorter, shorter_result),
            )
            longer_process = context.Process(
                target=_refresh_in_process,
                args=(str(data_dir), longer, RUN_B, None, None, None, longer_result),
            )
            shorter_process.start()
            self.assertTrue(shorter_entered.wait(timeout=10))
            longer_process.start()
            longer_process.join(timeout=0.3)
            self.assertTrue(longer_process.is_alive())
            release_shorter.set()
            shorter_process.join(timeout=20)
            longer_process.join(timeout=20)

            self.assertEqual(0, shorter_process.exitcode)
            self.assertEqual(0, longer_process.exitcode)
            self.assertEqual(("ok", RUN_C), shorter_result.get(timeout=2))
            self.assertEqual(("ok", RUN_B), longer_result.get(timeout=2))
            self.assertEqual(longer, store.read_csv(tail_path))
            self.assertEqual(shorter, _read_exact(store, RUN_C))
            self.assertEqual(longer, _read_exact(store, RUN_B))
            current_id = json.loads(store.current_path.read_text(encoding="utf-8"))["generation_id"]
            self.assertIn(current_id, {RUN_B, RUN_C})
            self.assertEqual({RUN_B: longer, RUN_C: shorter}[current_id], _read_exact(store, current_id))

    def test_new_segment_directory_ancestors_are_fsynced(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            store = TrustedDataStore(data_dir=data_dir)
            synced: list[Path] = []
            with patch(
                "mu_strategy.market_data.trusted_data.store._fsync_directory",
                side_effect=lambda path: synced.append(Path(path).absolute()),
            ):
                store.write_segmented_dataset(
                    [_window_candles(FEB_START_MS, FEB_START_MS)[0]],
                    symbol=SYMBOL,
                    interval="5m",
                )

            segment_directory = data_dir / "segments" / "okx" / SYMBOL / "5m"
            expected = {
                data_dir.parent.absolute(),
                data_dir.absolute(),
                (data_dir / "segments").absolute(),
                (data_dir / "segments" / "okx").absolute(),
                (data_dir / "segments" / "okx" / SYMBOL).absolute(),
                segment_directory.absolute(),
            }
            self.assertTrue(expected.issubset(set(synced)), (expected, synced))

    def test_retention_window_can_expand_earlier_within_stored_month(self):
        now_ms = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
        provider = WindowedHistoryProvider(now_ms=now_ms)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _refresh(store, provider, RUN_A, days=1, now_ms=now_ms)
            first_manifest = store.read_generation_manifest(RUN_A).snapshot
            first_rows = _read_exact(store, RUN_A)
            first_reference = first_manifest.storage_by_dataset[(SYMBOL, "5m")].segments[0]

            _refresh(store, provider, RUN_B, days=14, now_ms=now_ms)
            second_manifest = store.read_generation_manifest(RUN_B).snapshot
            second_rows = _read_exact(store, RUN_B)
            second_reference = second_manifest.storage_by_dataset[(SYMBOL, "5m")].segments[0]

            self.assertEqual([33, 46], provider.history_days)
            self.assertEqual(0, provider.incremental_calls)
            self.assertEqual(1 * 288 + 1, len(first_rows))
            self.assertEqual(14 * 288 + 1, len(second_rows))
            self.assertEqual(first_rows, second_rows[-len(first_rows) :])
            self.assertGreater(first_reference.start_row, second_reference.start_row)
            self.assertEqual(first_rows, _read_exact(store, RUN_A))

    def test_torn_write_after_segment_replace_before_manifest_keeps_old_current_readable(self):
        initial_rows = _candles_through(FEB_START_MS)
        provider = MutableHistoryProvider(initial_rows)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _refresh(store, provider, RUN_A)
            old_pointer = store.current_path.read_bytes()
            provider.rows = _candles_through(FEB_START_MS + 4 * STEP_MS)

            with patch.object(
                store,
                "write_generation_manifest",
                side_effect=OSError("manifest offline"),
            ):
                with self.assertRaisesRegex(OSError, "manifest offline"):
                    _refresh(store, provider, RUN_B)

            self.assertEqual(old_pointer, store.current_path.read_bytes())
            self.assertEqual(initial_rows, _read_exact(store, RUN_A))


class SegmentedFailureContractTests(unittest.TestCase):
    def test_segment_paths_reject_traversal_without_touching_outside_sentinel(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            data_dir = workspace / "live"
            store, _ = _published_store(data_dir)
            sentinel = workspace / "sentinel.csv"
            sentinel.write_bytes(b"outside-must-remain-unchanged")

            for value in ("../2026-01", "..\\2026-01", "C:2026-01", "2026/01"):
                with self.subTest(segment_id=value):
                    with self.assertRaises(ValueError):
                        store.segment_source_file(SYMBOL, "5m", value)

            manifest_path = store.generation_manifest_path(RUN_A)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["symbols"][SYMBOL]["intervals"]["5m"]["storage"]["segments"][0][
                "source_file"
            ] = "../sentinel.csv"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            result = store.read_generation_manifest(RUN_A)

            self.assertFalse(result.ok)
            self.assertEqual(HealthReason.MALFORMED_MANIFEST, result.reason)
            self.assertEqual(b"outside-must-remain-unchanged", sentinel.read_bytes())

    def test_claimed_storage_corruption_fails_closed(self):
        cases = (
            "missing",
            "empty",
            "malformed",
            "partial",
            "row_count",
            "segment_hash",
            "logical_hash",
        )
        for case in cases:
            with self.subTest(case=case):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    store, provider = _published_store(data_dir)
                    manifest_path = store.generation_manifest_path(RUN_A)
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    dataset = manifest["symbols"][SYMBOL]["intervals"]["5m"]
                    segment = dataset["storage"]["segments"][-1]
                    segment_path = data_dir / segment["source_file"]

                    if case == "missing":
                        segment_path.unlink()
                    elif case == "empty":
                        segment_path.write_bytes(b"")
                    elif case == "malformed":
                        segment_path.write_bytes(b"wrong,header\r\n1,2\r\n")
                    elif case == "partial":
                        segment_path.write_bytes(segment_path.read_bytes()[:-50])
                    elif case == "row_count":
                        segment["rows"] += 1
                        dataset["rows"] += 1
                        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
                    elif case == "segment_hash":
                        segment["content_sha256"] = "0" * 64
                        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
                    elif case == "logical_hash":
                        dataset["content_sha256"] = "0" * 64
                        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

                    bundle = LoadTrustedBundle(store).execute(
                        LoadTrustedBundleQuery(
                            SYMBOL,
                            intervals=("5m",),
                            days=20,
                            now_ms=provider.rows[-1].open_time_ms + STEP_MS,
                        ),
                        trading_strict_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertIn(
                    bundle.trust_decision.reason,
                    {
                        HealthReason.CACHE_CONTENT_MISMATCH,
                        HealthReason.CACHE_READ_FAILED,
                        HealthReason.MALFORMED_MANIFEST,
                    },
                )

    def test_unknown_or_inconsistent_manifest_layout_and_references_fail_closed(self):
        cases = (
            "unknown_version",
            "unknown_layout",
            "dataset_layout_mismatch",
            "duplicate_reference",
            "out_of_order",
            "source_path_mismatch",
            "source_root_backslashes",
            "source_file_backslashes",
            "partial_path_not_first_reference",
            "partial_path_mismatches_physical_start",
            "empty_valid_storage",
        )
        for case in cases:
            with self.subTest(case=case):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    store, _ = _published_store(data_dir)
                    manifest_path = store.generation_manifest_path(RUN_A)
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    dataset = manifest["symbols"][SYMBOL]["intervals"]["5m"]
                    segments = dataset["storage"]["segments"]
                    if case == "unknown_version":
                        manifest["schema_version"] = 5
                    elif case == "unknown_layout":
                        manifest["storage_layout"] = "mystery"
                    elif case == "dataset_layout_mismatch":
                        dataset["storage"]["layout"] = "flat_csv_v1"
                    elif case == "duplicate_reference":
                        segments.append(dict(segments[-1]))
                        dataset["rows"] += segments[-1]["rows"]
                    elif case == "out_of_order":
                        segments.reverse()
                    elif case == "source_path_mismatch":
                        segments[0]["source_file"] = "segments/okx/OTHER/5m/2026-01.csv"
                    elif case == "source_root_backslashes":
                        dataset["storage"]["source_root"] = dataset["storage"]["source_root"].replace(
                            "/", "\\"
                        )
                    elif case == "source_file_backslashes":
                        segments[0]["source_file"] = segments[0]["source_file"].replace("/", "\\")
                    elif case == "partial_path_not_first_reference":
                        second = segments[1]
                        partial_start_ms = second["first_timestamp_ms"] - (second["start_row"] * STEP_MS)
                        second["source_file"] = (
                            f"segments/okx/{SYMBOL}/5m/{second['segment_id']}.partial-{partial_start_ms}.csv"
                        )
                    elif case == "partial_path_mismatches_physical_start":
                        first = segments[0]
                        first["source_file"] = (
                            f"segments/okx/{SYMBOL}/5m/{first['segment_id']}.partial-"
                            f"{first['first_timestamp_ms'] + STEP_MS}.csv"
                        )
                    elif case == "empty_valid_storage":
                        dataset["storage"]["segments"] = []
                    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

                    result = store.read_generation_manifest(RUN_A)

                self.assertFalse(result.ok)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, result.reason)


class FlatGenerationImportTests(unittest.TestCase):
    def test_refresh_from_v3_current_forces_month_lookbehind_before_incremental_reuse(self):
        now_ms = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
        source_rows = _window_candles(now_ms - DAY_MS, now_ms - STEP_MS)
        provider = WindowedHistoryProvider(now_ms=now_ms)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _write_flat_v3_generation(store, RUN_A, source_rows)
            store.replace_current(RUN_A)

            _refresh(store, provider, RUN_B, days=1, now_ms=now_ms)
            first_v4 = store.read_generation_manifest(RUN_B).snapshot
            first_reference = first_v4.storage_by_dataset[(SYMBOL, "5m")].segments[0]
            _refresh(store, provider, RUN_C, days=14, now_ms=now_ms)

            self.assertEqual([33, 46], provider.history_days)
            self.assertEqual(0, provider.incremental_calls)
            self.assertGreater(first_reference.start_row, 0)
            self.assertEqual(source_rows, _read_exact(store, RUN_A))
            self.assertEqual(source_rows, _read_exact(store, RUN_B)[-len(source_rows) :])
            self.assertEqual(14 * 288 + 1, len(_read_exact(store, RUN_C)))

    def test_partial_first_month_import_isolated_from_later_canonical_lookbehind(self):
        source_run = "5" * 32
        imported_run = "6" * 32
        refreshed_run = "7" * 32
        expanded_run = "8" * 32
        now_ms = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
        source_rows = _window_candles(now_ms - DAY_MS, now_ms - STEP_MS)
        provider = WindowedHistoryProvider(now_ms=now_ms)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _write_flat_v3_generation(store, source_run, source_rows)
            imported = store.import_flat_generation(source_run, imported_run, publish=True)
            import_reference = imported.snapshot.storage_by_dataset[(SYMBOL, "5m")].segments[0]
            import_path = Path(tmp) / import_reference.source_file
            import_bytes = import_path.read_bytes()

            _refresh(store, provider, refreshed_run, days=1, now_ms=now_ms)
            _refresh(store, provider, expanded_run, days=14, now_ms=now_ms)

            self.assertEqual(
                f"2026-01.import-{imported_run}.csv",
                import_reference.source_file.name,
            )
            self.assertEqual([33, 46], provider.history_days)
            self.assertEqual(0, provider.incremental_calls)
            self.assertTrue(store.segment_path(SYMBOL, "5m", "2026-01").exists())
            self.assertEqual(import_bytes, import_path.read_bytes())
            self.assertEqual(source_rows, _read_exact(store, source_run))
            self.assertEqual(source_rows, _read_exact(store, imported_run))
            self.assertEqual(source_rows, _read_exact(store, refreshed_run)[-len(source_rows) :])
            self.assertEqual(14 * 288 + 1, len(_read_exact(store, expanded_run)))

            imported_manifest_path = store.generation_manifest_path(imported_run)
            imported_manifest = json.loads(imported_manifest_path.read_text(encoding="utf-8"))
            imported_manifest["symbols"][SYMBOL]["intervals"]["5m"]["storage"]["segments"][0][
                "source_file"
            ] = f"segments/okx/{SYMBOL}/5m/2026-01.import-{'9' * 32}.csv"
            imported_manifest_path.write_text(json.dumps(imported_manifest, sort_keys=True), encoding="utf-8")
            malformed = store.read_generation_manifest(imported_run)
            self.assertFalse(malformed.ok)
            self.assertEqual(HealthReason.MALFORMED_MANIFEST, malformed.reason)
            self.assertEqual(import_bytes, import_path.read_bytes())

    def test_explicit_v3_import_round_trips_candles_hashes_and_can_publish(self):
        source_run = "d" * 32
        target_run = "e" * 32
        rows = _candles_through(FEB_START_MS)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            source_path = _write_flat_v3_generation(store, source_run, rows)
            source_bytes = source_path.read_bytes()

            imported = store.import_flat_generation(source_run, target_run, publish=True)
            source_result = store.read_generation_manifest(source_run)
            source_rows = store.read_generation_dataset(
                source_result.snapshot,
                symbol=SYMBOL,
                interval="5m",
                generation_root=source_result.generation_root,
                generation_id=source_run,
            )
            imported_rows = store.read_generation_dataset(
                imported.snapshot,
                symbol=SYMBOL,
                interval="5m",
                generation_root=imported.generation_root,
                generation_id=target_run,
            )

            self.assertEqual(source_bytes, source_path.read_bytes())
            self.assertEqual(4, imported.snapshot.schema_version)
            self.assertEqual(source_run, imported.snapshot.imported_from_run_id)
            self.assertEqual(source_rows, imported_rows)
            self.assertEqual(candles_content_sha256(source_rows), candles_content_sha256(imported_rows))
            self.assertEqual(target_run, json.loads(store.current_path.read_text(encoding="utf-8"))["generation_id"])

    def test_v3_import_rejects_unusable_dataset_before_shared_or_target_writes(self):
        rows = _candles_through(FEB_START_MS)
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _write_flat_v3_generation(store, RUN_A, rows)
            manifest_path = store.generation_manifest_path(RUN_A)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            status = manifest["symbols"][SYMBOL]["intervals"]["5m"]
            manifest["attempt_status"] = "failed"
            manifest["snapshot_usability"] = "invalid"
            status["integrity"] = "invalid"
            status["freshness"] = "stale"
            status["reasons"] = ["ohlcv_invalid"]
            status["validation"] = {"ok": False, "reason": "ohlcv_invalid"}
            store.write_generation_manifest(RUN_A, manifest)

            with self.assertRaisesRegex(ManifestSchemaError, "source dataset is unusable"):
                store.import_flat_generation(RUN_A, RUN_B)

            self.assertFalse(store.generation_root(RUN_B).exists())
            self.assertFalse(store.segments_dir.exists())

    def test_import_command_is_explicit_and_does_not_publish_without_flag(self):
        from mu_strategy.commands.import_trusted_generation import main

        source_run = "1" * 32
        target_run = "2" * 32
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _write_flat_v3_generation(store, source_run, _candles_through(FEB_START_MS))
            stdout = StringIO()

            exit_code = main(
                [
                    "--data-dir",
                    str(data_dir),
                    "--source-run-id",
                    source_run,
                    "--target-run-id",
                    target_run,
                ],
                stdout=stdout,
            )
            output = json.loads(stdout.getvalue())

            self.assertEqual(0, exit_code)
            self.assertEqual("ok", output["status"])
            self.assertFalse(output["published"])
            self.assertFalse(store.current_path.exists())
            self.assertTrue(store.generation_manifest_path(target_run).exists())

    def test_current_and_historical_readers_are_cache_only(self):
        provider = MutableHistoryProvider(_candles_through(FEB_START_MS))
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _refresh(store, provider, RUN_A)

            with patch(
                "mu_strategy.market_data.providers.okx.fetch_okx_historical",
                side_effect=AssertionError("network history"),
            ) as history:
                with patch(
                    "mu_strategy.market_data.providers.okx.fetch_okx_incremental",
                    side_effect=AssertionError("network incremental"),
                ) as incremental:
                    with patch(
                        "mu_strategy.market_data.universe.fetch_okx_swap_tickers",
                        side_effect=AssertionError("network tickers"),
                    ) as tickers:
                        bundle = LoadTrustedBundle(store).execute(
                            LoadTrustedBundleQuery(
                                SYMBOL,
                                intervals=("5m",),
                                days=20,
                                now_ms=provider.rows[-1].open_time_ms + STEP_MS,
                            ),
                            observe_only_policy(),
                        )
                        historical = HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                            run_id=RUN_A,
                            symbol=SYMBOL,
                        )

            self.assertEqual(provider.rows, bundle.candles_by_interval["5m"])
            self.assertEqual(tuple(provider.rows), historical.candles_by_interval["5m"])
            history.assert_not_called()
            incremental.assert_not_called()
            tickers.assert_not_called()


def _candles_through(end_ms: int) -> list[Candle]:
    return _window_candles(JAN_START_MS, end_ms)


def _window_candles(start_ms: int, end_ms: int) -> list[Candle]:
    rows: list[Candle] = []
    timestamp = start_ms
    while timestamp <= end_ms:
        index = timestamp // STEP_MS
        price = round(100.0 + index / 100.0, 8)
        rows.append(
            Candle(
                timestamp,
                price,
                round(price + 1.0, 8),
                round(price - 1.0, 8),
                round(price + 0.25, 8),
                float(10 + index),
            )
        )
        timestamp += STEP_MS
    return rows


def _refresh(
    store: TrustedDataStore,
    provider,
    run_id: str,
    *,
    days: int = 20,
    now_ms: int | None = None,
):
    return RefreshTrustedMarketData(store, provider).execute(
        RefreshTrustedMarketDataRequest(
            requested_intervals=("5m",),
            days=days,
            symbols=(SYMBOL,),
            now_ms=now_ms if now_ms is not None else provider.rows[-1].open_time_ms + STEP_MS,
            run_id=run_id,
        )
    )


def _refresh_segment(store: TrustedDataStore, run_id: str) -> dict:
    manifest = json.loads(store.generation_manifest_path(run_id).read_text(encoding="utf-8"))
    return manifest["diagnostics"]["refresh_segments"][0]


def _read_exact(store: TrustedDataStore, run_id: str) -> list[Candle]:
    return _read_exact_interval(store, run_id, "5m")


def _read_exact_interval(store: TrustedDataStore, run_id: str, interval: str) -> list[Candle]:
    result = store.read_generation_manifest(run_id)
    if not result.ok or result.snapshot is None or result.generation_root is None:
        raise AssertionError(result)
    return store.read_generation_dataset(
        result.snapshot,
        symbol=SYMBOL,
        interval=interval,
        generation_root=result.generation_root,
        generation_id=run_id,
    )


def _published_store(data_dir: Path) -> tuple[TrustedDataStore, MutableHistoryProvider]:
    store = TrustedDataStore(data_dir=data_dir)
    provider = MutableHistoryProvider(_candles_through(FEB_START_MS))
    _refresh(store, provider, RUN_A)
    return store, provider


def _file_sizes(root: Path) -> dict[str, int]:
    return {
        path.relative_to(root).as_posix(): path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    }


def _segment_bytes(data_dir: Path) -> int:
    return sum(path.stat().st_size for path in (data_dir / "segments").rglob("*.csv"))


def _write_flat_v3_generation(store: TrustedDataStore, run_id: str, rows: list[Candle]) -> Path:
    store.prepare_generation(run_id)
    path = store.generation_cache_path(run_id, SYMBOL, "5m")
    store.write_csv(rows, path)
    completed_at_ms = rows[-1].open_time_ms + STEP_MS
    manifest = {
        "schema_version": 3,
        "run_id": run_id,
        "attempt_status": "success",
        "snapshot_usability": "usable",
        "started_at_ms": completed_at_ms,
        "completed_at_ms": completed_at_ms,
        "updated_at_ms": completed_at_ms,
        "requested_intervals": ["5m"],
        "effective_intervals": ["5m"],
        "intervals": ["5m"],
        "universes": {
            "crypto_top": [
                {
                    "inst_id": SYMBOL,
                    "last": 100.0,
                    "volume_ccy_24h": 10.0,
                    "source": "explicit",
                }
            ],
            "stock_token_top": [],
        },
        "symbols": {
            SYMBOL: {
                "intervals": {
                    "5m": {
                        "symbol": SYMBOL,
                        "interval": "5m",
                        "availability": "available",
                        "integrity": "valid",
                        "freshness": "fresh",
                        "reasons": ["ok"],
                        "rows": len(rows),
                        "first_timestamp_ms": rows[0].open_time_ms,
                        "last_timestamp_ms": rows[-1].open_time_ms,
                        "updated_at_ms": completed_at_ms,
                        "source_file": store.generation_source_file(SYMBOL, "5m").as_posix(),
                        "content_sha256": candles_content_sha256(rows),
                        "validation": {"ok": True, "reason": "ok"},
                    }
                }
            }
        },
        "provider_failures": [],
        "warnings": [],
        "cycle_error": None,
    }
    store.write_generation_manifest(run_id, manifest)
    return path


def _refresh_in_process(
    data_dir: str,
    rows: list[Candle],
    run_id: str,
    pause_segment_rows: int | None,
    entered,
    release,
    result_queue,
) -> None:
    store = TrustedDataStore(data_dir=Path(data_dir))
    original_write_csv = store.write_csv

    def controlled_write(candles, path, *, required_prefix=None):
        if pause_segment_rows is not None and len(candles) == pause_segment_rows:
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("timed out waiting to release paused segment writer")
        return original_write_csv(candles, path, required_prefix=required_prefix)

    try:
        if pause_segment_rows is None:
            _refresh(store, MutableHistoryProvider(rows), run_id)
        else:
            with patch.object(store, "write_csv", side_effect=controlled_write):
                _refresh(store, MutableHistoryProvider(rows), run_id)
        result_queue.put(("ok", run_id))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
        raise


if __name__ == "__main__":
    unittest.main()
