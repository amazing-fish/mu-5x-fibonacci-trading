from __future__ import annotations

import json
import unittest
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
from mu_strategy.models import Candle


SYMBOL = "MU-USDT-SWAP"
STEP_MS = 300_000
JAN_START_MS = int(datetime(2026, 1, 22, tzinfo=timezone.utc).timestamp() * 1000)
FEB_START_MS = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
RUN_A = "a" * 32
RUN_B = "b" * 32
RUN_C = "c" * 32


class MutableHistoryProvider:
    def __init__(self, rows: list[Candle]):
        self.rows = list(rows)
        self.return_all_incremental = False
        self.history_calls = 0
        self.incremental_calls = 0

    def fetch_tickers(self):
        raise AssertionError("explicit symbol refresh must not fetch tickers")

    def fetch_history(self, symbol: str, interval: str, *, days: int) -> list[Candle]:
        self.history_calls += 1
        return list(self.rows)

    def fetch_incremental(self, symbol: str, interval: str, *, since_time_ms: int) -> list[Candle]:
        self.incremental_calls += 1
        if self.return_all_incremental:
            return list(self.rows)
        return [candle for candle in self.rows if candle.open_time_ms >= since_time_ms]


class UtcMonthPartitionTests(unittest.TestCase):
    def test_partition_key_is_utc_calendar_month_at_boundaries(self):
        cases = (
            (datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc), "1969-12"),
            (datetime(1970, 1, 1, 0, 0, tzinfo=timezone.utc), "1970-01"),
            (datetime(2025, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc), "2025-12"),
            (datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), "2026-01"),
            (datetime(2026, 1, 31, 23, 59, 59, 999000, tzinfo=timezone.utc), "2026-01"),
            (datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc), "2026-02"),
        )
        for instant, expected in cases:
            with self.subTest(instant=instant.isoformat()):
                self.assertEqual(expected, utc_month_segment_id(int(instant.timestamp() * 1000)))

    def test_partition_key_rejects_non_integer_and_out_of_range_values(self):
        for value in (True, 1.5, "0", 10**30):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    utc_month_segment_id(value)


class SegmentedRefreshBehaviorTests(unittest.TestCase):
    def test_multi_cycle_growth_is_new_bytes_plus_metadata_and_closed_segment_is_unchanged(self):
        initial_rows = _candles_through(FEB_START_MS)
        provider = MutableHistoryProvider(initial_rows)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _refresh(store, provider, RUN_A)
            first_tree = _file_sizes(data_dir)
            january_path = store.segment_path(SYMBOL, "5m", "2026-01")
            january_bytes = january_path.read_bytes()
            initial_segment_bytes = _segment_bytes(data_dir)

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
            self.assertEqual(january_bytes, january_path.read_bytes())
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
            self.assertEqual(january_bytes, january_path.read_bytes())

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

    def test_closed_segment_historical_correction_fails_before_publication(self):
        initial_rows = _candles_through(FEB_START_MS)
        provider = MutableHistoryProvider(initial_rows)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            _refresh(store, provider, RUN_A)
            january_path = store.segment_path(SYMBOL, "5m", "2026-01")
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
            store.write_segmented_dataset(initial_rows, symbol=SYMBOL, interval="5m")
            january_path = store.segment_path(SYMBOL, "5m", "2026-01")
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


class SegmentedFailureContractTests(unittest.TestCase):
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
                    elif case == "empty_valid_storage":
                        dataset["storage"]["segments"] = []
                    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

                    result = store.read_generation_manifest(RUN_A)

                self.assertFalse(result.ok)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, result.reason)


class FlatGenerationImportTests(unittest.TestCase):
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
    rows: list[Candle] = []
    timestamp = JAN_START_MS
    while timestamp <= end_ms:
        index = (timestamp - JAN_START_MS) // STEP_MS
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
    provider: MutableHistoryProvider,
    run_id: str,
):
    return RefreshTrustedMarketData(store, provider).execute(
        RefreshTrustedMarketDataRequest(
            requested_intervals=("5m",),
            days=20,
            symbols=(SYMBOL,),
            now_ms=provider.rows[-1].open_time_ms + STEP_MS,
            run_id=run_id,
        )
    )


def _read_exact(store: TrustedDataStore, run_id: str) -> list[Candle]:
    result = store.read_generation_manifest(run_id)
    if not result.ok or result.snapshot is None or result.generation_root is None:
        raise AssertionError(result)
    return store.read_generation_dataset(
        result.snapshot,
        symbol=SYMBOL,
        interval="5m",
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


if __name__ == "__main__":
    unittest.main()
