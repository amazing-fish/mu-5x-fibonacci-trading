import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle
from tests.factories.trusted_publication import (
    SYMBOL,
    HoleyProvider,
    StaticProvider,
    UniverseFailureProvider,
    candles_by_interval,
    current_generation_dir,
    generation_manifest,
    read_current,
    write_flat_v3_publication,
    write_generation_pointer,
)


class TrustedAtomicPublicationTests(unittest.TestCase):
    def test_cold_refresh_publishes_generation_and_loader_reads_same_run(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            run = RefreshTrustedMarketData(store, StaticProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                    run_id="run-cold",
                )
            )
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("15m", "1h"), days=1, now_ms=3_600_000),
                trading_strict_policy(),
            )
            pointer = read_current(data_dir)
            manifest = generation_manifest(data_dir, "run-cold")

        self.assertEqual("run-cold", run.run_id)
        self.assertEqual({"schema_version": 1, "generation_id": "run-cold", "manifest": "generations/run-cold/manifest.json"}, pointer)
        self.assertFalse((data_dir / "manifest.json").exists())
        self.assertFalse((data_dir / "okx").exists())
        self.assertEqual("run-cold", manifest["run_id"])
        self.assertEqual("okx/BTC-USDT-SWAP/15m.csv", manifest["symbols"][SYMBOL]["intervals"]["15m"]["source_file"])
        self.assertEqual("run-cold", bundle.run_id)
        self.assertTrue(bundle.trust_decision.allowed)
        self.assertEqual(data_dir / "generations" / "run-cold" / "okx" / SYMBOL / "15m.csv", bundle.files_by_interval["15m"])

    def test_incremental_refresh_reads_current_generation_and_preserves_old_generation(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            refresher = RefreshTrustedMarketData(store, StaticProvider())
            request = dict(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=3_600_000)
            refresher.execute(RefreshTrustedMarketDataRequest(**request, run_id="run-old"))
            old_path = data_dir / "generations" / "run-old" / "okx" / SYMBOL / "5m.csv"
            old_bytes = old_path.read_bytes()

            RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-new"))
            new_path = data_dir / "generations" / "run-new" / "okx" / SYMBOL / "5m.csv"
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=3_900_000),
                observe_only_policy(),
            )

            self.assertEqual("run-new", read_current(data_dir)["generation_id"])
            self.assertEqual(old_bytes, old_path.read_bytes())
            self.assertNotEqual(old_bytes, new_path.read_bytes())
            self.assertEqual("run-new", bundle.run_id)
            self.assertEqual(new_path, bundle.files_by_interval["5m"])

    def test_csv_storage_failure_keeps_old_current_generation(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            request = dict(requested_intervals=("15m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=3_600_000)
            RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-old"))
            original_write_csv = store.write_csv
            calls = []

            def fail_after_first(candles, path):
                calls.append(path)
                if len(calls) == 1:
                    return original_write_csv(candles, path)
                raise OSError("disk full")

            with patch.object(store, "write_csv", side_effect=fail_after_first):
                with self.assertRaisesRegex(OSError, "disk full"):
                    RefreshTrustedMarketData(store, StaticProvider(offset=10)).execute(
                        RefreshTrustedMarketDataRequest(**request, run_id="run-failed")
                    )
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("15m",), days=1, now_ms=3_600_000),
                observe_only_policy(),
            )

            self.assertEqual("run-old", read_current(data_dir)["generation_id"])
            self.assertEqual("run-old", bundle.run_id)

    def test_manifest_or_pointer_publication_failure_keeps_old_current_generation(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = (
            ("write_generation_manifest", OSError("manifest write failed")),
            ("replace_current", OSError("replace failed")),
        )
        for method_name, error in cases:
            with self.subTest(method_name=method_name):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    store = TrustedDataStore(data_dir=data_dir)
                    request = dict(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=3_600_000)
                    RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-old"))
                    with patch.object(store, method_name, side_effect=error):
                        with self.assertRaisesRegex(OSError, str(error)):
                            RefreshTrustedMarketData(store, StaticProvider(offset=10)).execute(
                                RefreshTrustedMarketDataRequest(**request, run_id="run-failed")
                            )

                    self.assertEqual("run-old", read_current(data_dir)["generation_id"])

    def test_current_replace_os_error_preserves_old_pointer(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            request = dict(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=3_600_000)
            RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-old"))
            old_pointer = (data_dir / "current.json").read_bytes()
            original_replace = os.replace

            def fail_current_replace(src, dst):
                if Path(dst).name == "current.json":
                    raise OSError("current replace failed")
                return original_replace(src, dst)

            with patch("mu_strategy.market_data.trusted_data.store.os.replace", side_effect=fail_current_replace):
                with self.assertRaisesRegex(OSError, "current replace failed"):
                    RefreshTrustedMarketData(store, StaticProvider(offset=10)).execute(
                        RefreshTrustedMarketDataRequest(**request, run_id="run-failed")
                    )

            self.assertEqual(old_pointer, (data_dir / "current.json").read_bytes())

    def test_run_log_failure_after_pointer_reports_audit_failure_without_rollback(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            stock_config = Path(tmp) / "stock.json"
            stock_config.write_text("[]", encoding="utf-8")
            stdout = _Sink()
            with patch("mu_strategy.market_data.trusted_data.refresh.uuid.uuid4", return_value=_Hex("run-audit")):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers", return_value=[{"instId": SYMBOL, "last": "100", "volCcy24h": "10"}]):
                    with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical", side_effect=lambda symbol, interval, *, days: candles_by_interval()[interval]):
                        with patch.object(TrustedDataStore, "append_run_log", side_effect=OSError("audit offline")):
                            exit_code = main(
                                [
                                    "--data-dir",
                                    str(data_dir),
                                    "--stock-token-config",
                                    str(stock_config),
                                    "--limit",
                                    "1",
                                    "--days",
                                    "1",
                                    "--html-output",
                                    str(Path(tmp) / "health.html"),
                                ],
                                stdout=stdout,
                            )

            self.assertNotEqual(0, exit_code)
            self.assertEqual("run-audit", read_current(data_dir)["generation_id"])
            self.assertIn("audit log append failed", json.loads(stdout.text)["message"])

    def test_context_pins_generation_while_new_refresh_publishes(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            request = dict(requested_intervals=("5m", "15m"), days=1, limit=1, stock_token_inst_ids=set(), now_ms=3_600_000)
            RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-old"))
            loader = LoadTrustedBundle(store)
            old_context = loader.open_context(now_ms=3_600_000)
            RefreshTrustedMarketData(store, StaticProvider(offset=10)).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-new"))

            old_bundle = loader.execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m", "15m"), days=1),
                observe_only_policy(),
                context=old_context,
            )
            new_bundle = loader.execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m", "15m"), days=1, now_ms=3_600_000),
                observe_only_policy(),
            )

        self.assertEqual("run-old", old_bundle.run_id)
        self.assertTrue(all("run-old" in str(path) for path in old_bundle.files_by_interval.values()))
        self.assertEqual("run-new", new_bundle.run_id)
        self.assertTrue(all("run-new" in str(path) for path in new_bundle.files_by_interval.values()))

    def test_malformed_current_or_missing_generation_fail_closed_without_flat_fallback(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = (
            lambda data_dir: (data_dir / "current.json").write_text("{not-json", encoding="utf-8"),
            lambda data_dir: (data_dir / "current.json").write_text(
                json.dumps({"schema_version": 1, "generation_id": "missing", "manifest": "generations/missing/manifest.json"}),
                encoding="utf-8",
            ),
        )
        for arrange in cases:
            with self.subTest(arrange=arrange):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    write_flat_v3_publication(data_dir)
                    arrange(data_dir)
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                        LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=3_600_000),
                        observe_only_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_generation_source_file_must_stay_relative_inside_generation_root(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        bad_sources = (str(Path("C:/outside.csv")), "../outside.csv")
        for source_file in bad_sources:
            with self.subTest(source_file=source_file):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    manifest = write_flat_v3_publication(data_dir)
                    (data_dir / "manifest.json").unlink()
                    manifest["run_id"] = "bad-source"
                    manifest["symbols"][SYMBOL]["intervals"]["5m"]["source_file"] = source_file
                    write_generation_pointer(data_dir, generation_id="bad-source", manifest=manifest)
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                        LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=3_600_000),
                        observe_only_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_generation_csv_modification_is_detected_by_hash(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            RefreshTrustedMarketData(store, StaticProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                    run_id="run-good",
                )
            )
            csv_path = current_generation_dir(data_dir) / "okx" / SYMBOL / "5m.csv"
            csv_path.write_text(csv_path.read_text(encoding="utf-8").replace("100.0", "100.5", 1), encoding="utf-8")
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=3_600_000),
                trading_strict_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.CACHE_CONTENT_MISMATCH, bundle.trust_decision.reason)

    def test_flat_layout_loads_read_only_and_next_refresh_creates_generation(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            flat_manifest = write_flat_v3_publication(data_dir)
            flat_manifest_bytes = (data_dir / "manifest.json").read_bytes()
            store = TrustedDataStore(data_dir=data_dir)
            flat_bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("15m",), days=1, now_ms=3_600_000),
                trading_strict_policy(),
            )
            RefreshTrustedMarketData(store, StaticProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                    run_id="run-migrated",
                )
            )

            self.assertEqual("flat-run", flat_bundle.run_id)
            self.assertEqual(flat_manifest_bytes, (data_dir / "manifest.json").read_bytes())
            self.assertEqual("run-migrated", read_current(data_dir)["generation_id"])
            self.assertEqual("flat-run", flat_manifest["run_id"])

    def test_stale_invalid_and_failed_attempts_still_publish_full_generations_and_block_strict_consumers(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = (
            ("run-stale", StaticProvider(), 86_400_000, HealthReason.MANIFEST_STALE),
            ("run-invalid", HoleyProvider(), 3_600_000, HealthReason.MANIFEST_INVALID),
            ("run-failed", UniverseFailureProvider(), 3_600_000, HealthReason.RUN_FAILED),
        )
        for run_id, provider, now_ms, expected_reason in cases:
            with self.subTest(run_id=run_id):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    store = TrustedDataStore(data_dir=data_dir)
                    RefreshTrustedMarketData(store, provider).execute(
                        RefreshTrustedMarketDataRequest(
                            requested_intervals=("5m",),
                            days=1,
                            limit=1,
                            stock_token_inst_ids=set(),
                            now_ms=now_ms,
                            run_id=run_id,
                        )
                    )
                    bundle = LoadTrustedBundle(store).execute(
                        LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=now_ms),
                        trading_strict_policy(),
                    )

                    self.assertEqual(run_id, read_current(data_dir)["generation_id"])
                    self.assertFalse(bundle.trust_decision.allowed)
                    self.assertEqual(expected_reason, bundle.trust_decision.reason)


class _Sink:
    def __init__(self):
        self.text = ""

    def write(self, value):
        self.text += value

    def flush(self):
        pass


class _Hex:
    def __init__(self, value: str):
        self.hex = value


if __name__ == "__main__":
    unittest.main()
