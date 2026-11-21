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

from mu_strategy.research.historical_data import HistoricalTrustedGenerationReader
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
    def test_complete_window_starting_in_listing_month_uses_canonical_segment(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus

        now_ms = FEB_START_MS + 10 * DAY_MS
        listing_ms = FEB_START_MS + 2 * DAY_MS + (7 * 60 + 15) * 60_000

        class ListedProvider(MutableHistoryProvider):
            def fetch_listing_time(self, symbol):
                self.asserted_symbol = symbol
                return listing_ms

        provider = ListedProvider(_window_candles(listing_ms, now_ms - STEP_MS))
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            run = _refresh(store, provider, RUN_A, days=1, now_ms=now_ms)

            self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
            health = run.datasets[(SYMBOL, "5m")]
            self.assertEqual("complete", health.coverage_state)
            self.assertEqual(1, health.requested_days)
            storage = run.storage_by_dataset[(SYMBOL, "5m")]
            self.assertEqual(store.segment_source_file(SYMBOL, "5m", "2026-02"), storage.segments[0].source_file)
            self.assertTrue(store.segment_path(SYMBOL, "5m", "2026-02").exists())
            self.assertEqual(SYMBOL, provider.asserted_symbol)
            self.assertIn(f"verified_listing_start:listing_time_ms={listing_ms}", health.warnings)

    def test_listing_boundary_supports_all_intervals_and_incremental_restart(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus

        # A non-hour boundary leaves an incomplete trailing native hour.
        now_ms = FEB_START_MS + 10 * DAY_MS + 15 * 60_000
        listing_ms = FEB_START_MS + 2 * DAY_MS + (7 * 60 + 15) * 60_000
        five = _window_candles(listing_ms, now_ms - STEP_MS)

        class ListedProvider(MutableBundleProvider):
            listing_calls = 0

            def fetch_listing_time(self, symbol):
                self.listing_calls += 1
                return listing_ms

        provider = ListedProvider({"5m": five, "15m": aggregate_candles(five, interval="15m"),
                                   "1h": aggregate_candles(five, interval="1h")})
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            first = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(days=1, symbols=(SYMBOL,), now_ms=now_ms, run_id=RUN_A)
            )
            self.assertEqual(RefreshAttemptStatus.SUCCESS, first.attempt_status)
            self.assertEqual(1, provider.listing_calls)
            paths = {interval: store.data_dir / first.storage_by_dataset[(SYMBOL, interval)].segments[0].source_file
                     for interval in ("5m", "15m", "1h")}
            previous_bytes = {interval: path.read_bytes() for interval, path in paths.items()}
            for interval, path in paths.items():
                self.assertEqual("complete", first.datasets[(SYMBOL, interval)].coverage_state)
                self.assertEqual(store.segment_path(SYMBOL, interval, "2026-02"), path)

            five = _window_candles(listing_ms, now_ms + 3_600_000 - STEP_MS)
            provider.rows_by_interval = {"5m": five, "15m": aggregate_candles(five, interval="15m"),
                                         "1h": aggregate_candles(five, interval="1h")}
            second = RefreshTrustedMarketData(TrustedDataStore(data_dir=store.data_dir), provider).execute(
                RefreshTrustedMarketDataRequest(days=1, symbols=(SYMBOL,), now_ms=now_ms + 3_600_000, run_id=RUN_B)
            )
            self.assertEqual(RefreshAttemptStatus.SUCCESS, second.attempt_status)
            self.assertEqual(1, provider.listing_calls)
            self.assertEqual(3, len(provider.incremental_calls))
            for interval, path in paths.items():
                self.assertTrue(path.read_bytes().startswith(previous_bytes[interval]))
                self.assertEqual(path, store.data_dir / second.storage_by_dataset[(SYMBOL, interval)].segments[0].source_file)

    def test_listing_time_does_not_excuse_truncated_or_wrong_month_history(self):
        now_ms = FEB_START_MS + 10 * DAY_MS
        listing_ms = FEB_START_MS + 2 * DAY_MS + (7 * 60 + 15) * 60_000

        class ListedProvider(MutableHistoryProvider):
            def fetch_listing_time(self, symbol):
                return self.listing

        cases = [(listing_ms, listing_ms + STEP_MS), (JAN_START_MS, listing_ms),
                 (listing_ms + STEP_MS, listing_ms), (listing_ms + 60_000, listing_ms + STEP_MS)]
        cases.extend((value, listing_ms) for value in (None, 0, -1, True, str(listing_ms), now_ms + 1))
        for boundary, first_candle in cases:
            with self.subTest(boundary=boundary, first_candle=first_candle), TemporaryDirectory() as tmp:
                provider = ListedProvider(_window_candles(first_candle, now_ms - STEP_MS))
                provider.listing = boundary
                store = TrustedDataStore(data_dir=Path(tmp))
                with self.assertRaises(SegmentCorrectionError):
                    _refresh(store, provider, RUN_A, days=1, now_ms=now_ms)
                self.assertFalse(store.current_path.exists())
                self.assertFalse(any(store.data_dir.rglob("*.csv")))

    def test_listing_metadata_failure_cannot_publish_a_complete_start_month(self):
        now_ms = FEB_START_MS + 10 * DAY_MS

        class ListedProvider(MutableHistoryProvider):
            def fetch_listing_time(self, symbol):
                raise TimeoutError("listing metadata unavailable")

        provider = ListedProvider(_window_candles(FEB_START_MS + 2 * DAY_MS, now_ms - STEP_MS))
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            with self.assertRaisesRegex(SegmentCorrectionError, "listing metadata unavailable"):
                _refresh(store, provider, RUN_A, days=1, now_ms=now_ms)
            self.assertFalse(store.current_path.exists())
            self.assertFalse(any(store.data_dir.rglob("*.csv")))

    def test_default_180_day_window_is_capped_at_available_listing_history(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus

        now_ms = int(datetime(2026, 9, 6, tzinfo=timezone.utc).timestamp() * 1000)
        listing_ms = int(datetime(2026, 3, 4, 7, 15, tzinfo=timezone.utc).timestamp() * 1000)

        class ListedProvider(MutableHistoryProvider):
            def fetch_listing_time(self, symbol):
                return listing_ms

        provider = ListedProvider(_window_candles(listing_ms, now_ms - STEP_MS))
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            request = RefreshTrustedMarketDataRequest(requested_intervals=("5m",), symbols=(SYMBOL,),
                                                       now_ms=now_ms, run_id=RUN_A)
            run = RefreshTrustedMarketData(store, provider).execute(request)
            self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
            health = run.datasets[(SYMBOL, "5m")]
            self.assertEqual(180, health.requested_days)
            self.assertEqual("complete", health.coverage_state)
            self.assertEqual(180 * 288 + 1, health.rows)
            self.assertEqual([212], provider.history_days)
            self.assertEqual(
                store.segment_source_file(SYMBOL, "5m", "2026-03"),
                run.storage_by_dataset[(SYMBOL, "5m")].segments[0].source_file,
            )

        # When fewer than 180 days exist, retain the existing truthful coverage
        # state instead of inventing pre-listing rows or shortening the request.
        short_now = listing_ms + 10 * DAY_MS
        provider = ListedProvider(_window_candles(listing_ms, short_now - STEP_MS))
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            run = _refresh(store, provider, RUN_A, days=180, now_ms=short_now)
            health = run.datasets[(SYMBOL, "5m")]
            self.assertEqual(180, health.requested_days)
            self.assertEqual("partial_available_history", health.coverage_state)
            self.assertEqual(10 * 288, health.rows)

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


class FlatGenerationCompatTests(unittest.TestCase):
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

    def test_retained_v4_partial_and_import_first_segment_paths_remain_exact_readable(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store, provider = _published_store(data_dir)
            manifest_path = store.generation_manifest_path(RUN_A)
            original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            segments = original_manifest["symbols"][SYMBOL]["intervals"]["5m"]["storage"]["segments"]
            first = segments[0]
            canonical_path = data_dir / first["source_file"]
            expected_rows = provider.rows

            # 1. Retained first-segment .partial-<physical_first_timestamp_ms>.csv replays identically.
            partial_start_ms = first["first_timestamp_ms"] - (first["start_row"] * STEP_MS)
            partial_rel = f"segments/okx/{SYMBOL}/5m/{first['segment_id']}.partial-{partial_start_ms}.csv"
            (data_dir / partial_rel).write_bytes(canonical_path.read_bytes())
            partial_manifest = json.loads(json.dumps(original_manifest))
            partial_manifest["symbols"][SYMBOL]["intervals"]["5m"]["storage"]["segments"][0]["source_file"] = partial_rel
            manifest_path.write_text(json.dumps(partial_manifest, sort_keys=True), encoding="utf-8")

            manifest_result = store.read_generation_manifest(RUN_A)
            self.assertTrue(manifest_result.ok, manifest_result.message)
            self.assertEqual(expected_rows, _read_exact(store, RUN_A))
            self.assertEqual(
                manifest_result.snapshot.datasets[(SYMBOL, "5m")].content_sha256,
                store.verify_generation_dataset_bytes(
                    manifest_result.snapshot,
                    symbol=SYMBOL,
                    interval="5m",
                    generation_root=store.generation_root(RUN_A),
                    generation_id=RUN_A,
                ),
            )

            # 2. Retained first-segment .import-<run_id>.csv with imported_from_run_id replays identically.
            import_rel = f"segments/okx/{SYMBOL}/5m/{first['segment_id']}.import-{RUN_A}.csv"
            (data_dir / import_rel).write_bytes(canonical_path.read_bytes())
            import_manifest = json.loads(json.dumps(original_manifest))
            import_manifest["imported_from_run_id"] = "v3-source-run"
            import_manifest["symbols"][SYMBOL]["intervals"]["5m"]["storage"]["segments"][0]["source_file"] = import_rel
            manifest_path.write_text(json.dumps(import_manifest, sort_keys=True), encoding="utf-8")

            imported_result = store.read_generation_manifest(RUN_A)
            self.assertTrue(imported_result.ok, imported_result.message)
            self.assertEqual("v3-source-run", imported_result.snapshot.imported_from_run_id)
            self.assertEqual(expected_rows, _read_exact(store, RUN_A))

            # 3. .import-<run_id>.csv without imported_from_run_id or on non-first ref still fails closed.
            import_manifest.pop("imported_from_run_id")
            manifest_path.write_text(json.dumps(import_manifest, sort_keys=True), encoding="utf-8")
            self.assertFalse(store.read_generation_manifest(RUN_A).ok)

            bad_second = json.loads(json.dumps(original_manifest))
            bad_second["imported_from_run_id"] = "v3-source-run"
            second = bad_second["symbols"][SYMBOL]["intervals"]["5m"]["storage"]["segments"][1]
            second["source_file"] = f"segments/okx/{SYMBOL}/5m/{second['segment_id']}.import-{RUN_A}.csv"
            manifest_path.write_text(json.dumps(bad_second, sort_keys=True), encoding="utf-8")
            self.assertFalse(store.read_generation_manifest(RUN_A).ok)

    def test_store_csv_bare_cr_bounded_open_read_and_signed_zero_overlap_guard(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            rows = _window_candles(JAN_START_MS, JAN_START_MS + 2 * STEP_MS)

            # 1. Bare-CR CSV falls back cleanly to csv.reader instead of rejecting header.
            bare_cr_path = data_dir / "bare_cr.csv"
            store.write_csv(rows, bare_cr_path)
            lf_text = bare_cr_path.read_text(encoding="utf-8").replace("\r\n", "\n")
            bare_cr_path.write_bytes(lf_text.replace("\n", "\r").encode("utf-8"))
            self.assertEqual(rows, store.read_csv(bare_cr_path))

            # 2. Open-segment bounded read (require_eof=False) does not decode unreferenced trailing bytes.
            open_seg_path = data_dir / "open_seg.csv"
            store.write_csv(rows[:2], open_seg_path)
            with open_seg_path.open("ab") as handle:
                handle.write(b"bad,\xff\xfe,trailing,unreferenced,row,0,0\n")
            bounded_candles, bounded_hash = store._read_csv_slice_with_hash(
                open_seg_path,
                start_row=0,
                rows=2,
                require_eof=False,
                expected_sha256=candles_content_sha256(rows[:2]),
                need_hash=True,
            )
            self.assertEqual(rows[:2], bounded_candles)
            self.assertEqual(candles_content_sha256(rows[:2]), bounded_hash)

            # 3. Signed zero (-0.0 vs +0.0) in overlap is rejected before mutating the shared segment.
            zero_rows = [
                Candle(JAN_START_MS, 100.0, 101.0, 99.0, 100.5, 0.0),
                Candle(JAN_START_MS + STEP_MS, 100.5, 101.5, 99.5, 101.0, 10.0),
            ]
            store.write_segmented_dataset(zero_rows, symbol=SYMBOL, interval="5m")
            seg_path = store.segment_path(SYMBOL, "5m", "2026-01")
            before_bytes = seg_path.read_bytes()
            neg_zero_candidate = [
                Candle(JAN_START_MS, 100.0, 101.0, 99.0, 100.5, -0.0),
                Candle(JAN_START_MS + STEP_MS, 100.5, 101.5, 99.5, 101.0, 10.0),
                Candle(JAN_START_MS + 2 * STEP_MS, 101.0, 102.0, 100.0, 101.5, 11.0),
            ]
            with self.assertRaises(SegmentCorrectionError):
                store.write_segmented_dataset(neg_zero_candidate, symbol=SYMBOL, interval="5m")
            self.assertEqual(before_bytes, seg_path.read_bytes())

    def test_canonical_creation_checks_retained_partial_overlap_and_partial_first_month_expands(self):
        # 1. Retained .partial- file overlap is checked before creating a missing canonical month file.
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            full_jan = _window_candles(JAN_START_MS, JAN_START_MS + 10 * STEP_MS)
            partial_start = full_jan[5].open_time_ms
            partial_path = data_dir / f"segments/okx/{SYMBOL}/5m/2026-01.partial-{partial_start}.csv"
            store.write_csv(full_jan[5:], partial_path)

            conflicting = list(full_jan)
            conflicting[6] = Candle(
                conflicting[6].open_time_ms,
                conflicting[6].open + 5.0,
                conflicting[6].high + 5.0,
                conflicting[6].low,
                conflicting[6].close + 5.0,
                conflicting[6].volume,
            )
            with self.assertRaises(SegmentCorrectionError):
                store.write_segmented_dataset(conflicting, symbol=SYMBOL, interval="5m")
            self.assertFalse(store.segment_path(SYMBOL, "5m", "2026-01").exists())

            storage = store.write_segmented_dataset(full_jan, symbol=SYMBOL, interval="5m")
            self.assertEqual(store.segment_source_file(SYMBOL, "5m", "2026-01"), storage.segments[0].source_file)
            self.assertEqual(full_jan, store.read_csv(store.segment_path(SYMBOL, "5m", "2026-01")))

        # 2. A mid-month 2026-01.csv remains expandable across the Feb 1 month boundary without shifting old offsets.
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            short_rows = _window_candles(JAN_START_MS, JAN_START_MS + DAY_MS)
            full_through_feb1 = _window_candles(JAN_MONTH_START_MS, FEB_START_MS + 3 * STEP_MS)
            extended_feb1 = _window_candles(JAN_MONTH_START_MS, FEB_START_MS + 6 * STEP_MS)
            later_feb_rows = _window_candles(JAN_MONTH_START_MS, FEB_START_MS + 12 * DAY_MS)

            run_short = _refresh(
                store,
                MutableHistoryProvider(short_rows),
                RUN_A,
                days=10,
                now_ms=short_rows[-1].open_time_ms + STEP_MS,
            )
            self.assertEqual("success", run_short.attempt_status.value)
            self.assertEqual("partial_available_history", run_short.datasets[(SYMBOL, "5m")].coverage_state)
            canonical_jan = store.segment_path(SYMBOL, "5m", "2026-01")
            canonical_bytes_before = canonical_jan.read_bytes()

            # Continuous Jan 1 -> Feb 1 creates expanded Jan partial AND creates Feb 2026-02.csv seamlessly.
            run_full = _refresh(
                store,
                MutableHistoryProvider(full_through_feb1),
                RUN_B,
                days=32,
                now_ms=full_through_feb1[-1].open_time_ms + STEP_MS,
            )
            self.assertEqual("success", run_full.attempt_status.value)
            self.assertTrue(store.segment_path(SYMBOL, "5m", "2026-02").exists())
            jan_partial_path = data_dir / f"segments/okx/{SYMBOL}/5m/2026-01.partial-{JAN_MONTH_START_MS}.csv"
            self.assertTrue(jan_partial_path.exists())
            jan_partial_bytes_before = jan_partial_path.read_bytes()
            # Original 2026-01.csv stays byte-identical so RUN_A remains exact-readable.
            self.assertEqual(canonical_bytes_before, canonical_jan.read_bytes())
            self.assertEqual(short_rows, _read_exact(store, RUN_A))
            self.assertEqual(full_through_feb1, _read_exact(store, RUN_B))

            # Incremental refresh extending Feb 1 with logical start mid-Jan (< Jan 22) reuses expanded partial cleanly.
            run_ext = _refresh(
                store,
                MutableHistoryProvider(extended_feb1),
                RUN_C,
                days=20,
                now_ms=extended_feb1[-1].open_time_ms + STEP_MS,
            )
            self.assertEqual("success", run_ext.attempt_status.value)
            self.assertEqual(canonical_bytes_before, canonical_jan.read_bytes())
            self.assertEqual(jan_partial_bytes_before, jan_partial_path.read_bytes())
            # No redundant partial file should be spawned when existing expanded partial already covers the window.
            self.assertEqual(
                [jan_partial_path.name],
                sorted(p.name for p in canonical_jan.parent.glob("2026-01.partial-*.csv")),
            )
            self.assertEqual(extended_feb1[-run_ext.datasets[(SYMBOL, "5m")].rows :], _read_exact(store, RUN_C))
            self.assertEqual(full_through_feb1, _read_exact(store, RUN_B))

            # Later incremental refresh (Feb 13, days=20 -> logical start Jan 24 > Jan 22) still reuses expanded partial
            # instead of failing closed-segment growth against the frozen Jan 22-23 canonical file.
            run_later = _refresh(
                store,
                MutableHistoryProvider(later_feb_rows),
                RUN_D,
                days=20,
                now_ms=later_feb_rows[-1].open_time_ms + STEP_MS,
            )
            self.assertEqual("success", run_later.attempt_status.value)
            self.assertEqual(canonical_bytes_before, canonical_jan.read_bytes())
            self.assertEqual(later_feb_rows[-run_later.datasets[(SYMBOL, "5m")].rows :], _read_exact(store, RUN_D))
            self.assertEqual(short_rows, _read_exact(store, RUN_A))
            self.assertEqual(full_through_feb1, _read_exact(store, RUN_B))

        # 3. Two-step expansion: Jan 1..Jan 31 23:55 expanded first, then a standalone Feb 1 write validates adjacency
        # against the expanded Jan partial (while gapped Feb 1 00:05 still fails closed).
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            short_rows = _window_candles(JAN_START_MS, JAN_START_MS + DAY_MS)
            full_jan_only = _window_candles(JAN_MONTH_START_MS, FEB_START_MS - STEP_MS)
            feb_rows = _window_candles(FEB_START_MS, FEB_START_MS + 2 * STEP_MS)
            gapped_feb_rows = _window_candles(FEB_START_MS + STEP_MS, FEB_START_MS + 3 * STEP_MS)

            store.write_segmented_dataset(short_rows, symbol=SYMBOL, interval="5m")
            canonical_jan = store.segment_path(SYMBOL, "5m", "2026-01")
            canonical_bytes_before = canonical_jan.read_bytes()
            store.write_segmented_dataset(full_jan_only, symbol=SYMBOL, interval="5m")
            self.assertEqual(canonical_bytes_before, canonical_jan.read_bytes())

            with self.assertRaisesRegex(SegmentCorrectionError, "not adjacent"):
                store.write_segmented_dataset(gapped_feb_rows, symbol=SYMBOL, interval="5m")
            self.assertFalse(store.segment_path(SYMBOL, "5m", "2026-02").exists())

            feb_storage = store.write_segmented_dataset(feb_rows, symbol=SYMBOL, interval="5m")
            self.assertEqual(("2026-02",), tuple(ref.segment_id for ref in feb_storage.segments))
            self.assertEqual(canonical_bytes_before, canonical_jan.read_bytes())
            self.assertEqual(feb_rows, store.read_csv(store.segment_path(SYMBOL, "5m", "2026-02")))


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
