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
    write_flat_v1_publication,
    write_flat_v2_publication,
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
                    now_ms=86_400_000,
                    run_id="run-cold",
                )
            )
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("15m", "1h"), days=1, now_ms=86_400_000),
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

    def test_storage_segments_reject_path_attacks_without_changing_current(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        attacks = ("../../escaped", "../escaped", "a/b", "a\\b", "C:\\escaped", "C:escaped", ".", "..", "bad\0id", "a" * 129)
        for value in attacks:
            with self.subTest(value=value):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    data_dir = root / "data" / "live"
                    store = TrustedDataStore(data_dir=data_dir)
                    request = dict(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
                    RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-old"))
                    before = _tree(data_dir)
                    old_current = (data_dir / "current.json").read_bytes()

                    with self.assertRaises(ValueError):
                        RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id=value))
                    old_bundle = LoadTrustedBundle(store).execute(
                        LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000),
                        observe_only_policy(),
                    )

                    self.assertEqual(before, _tree(data_dir))
                    self.assertEqual(old_current, (data_dir / "current.json").read_bytes())
                    self.assertFalse((root / "escaped").exists())
                    self.assertEqual("run-old", old_bundle.run_id)

    def test_generated_run_id_segment_is_validated_before_paths(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data" / "live"
            store = TrustedDataStore(data_dir=data_dir)
            request = RefreshTrustedMarketDataRequest(
                requested_intervals=("5m",),
                days=1,
                limit=1,
                stock_token_inst_ids=set(),
                now_ms=86_400_000,
            )

            with patch("mu_strategy.market_data.trusted_data.refresh.uuid.uuid4", return_value=_Hex("../escaped")):
                with self.assertRaises(ValueError):
                    RefreshTrustedMarketData(store, StaticProvider()).execute(request)

            self.assertFalse(data_dir.exists())

    def test_provider_symbol_and_requested_interval_attacks_do_not_publish_current(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = (
            ("provider_symbol", StaticProvider(symbol="../escaped-USDT-SWAP"), ("5m",)),
            ("requested_interval", StaticProvider(), ("../escaped",)),
        )
        for name, provider, intervals in cases:
            with self.subTest(name=name):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    data_dir = root / "data" / "live"
                    store = TrustedDataStore(data_dir=data_dir)
                    request = dict(days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
                    RefreshTrustedMarketData(store, StaticProvider()).execute(
                        RefreshTrustedMarketDataRequest(**request, requested_intervals=("5m",), run_id="run-old")
                    )
                    before = _tree(data_dir)
                    current_before = (data_dir / "current.json").read_bytes()

                    with self.assertRaises(ValueError):
                        RefreshTrustedMarketData(store, provider).execute(
                            RefreshTrustedMarketDataRequest(**request, requested_intervals=intervals, run_id="run-attack")
                        )
                    old_bundle = LoadTrustedBundle(store).execute(
                        LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000),
                        observe_only_policy(),
                    )

                    self.assertEqual(current_before, (data_dir / "current.json").read_bytes())
                    self.assertFalse((root / "escaped").exists())
                    self.assertEqual("run-old", old_bundle.run_id)
                    self.assertTrue(set(before).issubset(set(_tree(data_dir))))

    def test_incremental_refresh_reads_current_generation_and_preserves_old_generation(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            refresher = RefreshTrustedMarketData(store, StaticProvider())
            request = dict(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
            refresher.execute(RefreshTrustedMarketDataRequest(**request, run_id="run-old"))
            old_path = data_dir / "generations" / "run-old" / "okx" / SYMBOL / "5m.csv"
            old_bytes = old_path.read_bytes()

            RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-new"))
            new_path = data_dir / "generations" / "run-new" / "okx" / SYMBOL / "5m.csv"
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_700_000),
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
            request = dict(requested_intervals=("15m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
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
                    RefreshTrustedMarketData(store, StaticProvider()).execute(
                        RefreshTrustedMarketDataRequest(**request, run_id="run-failed")
                    )
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("15m",), days=1, now_ms=86_400_000),
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
                    request = dict(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
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
            request = dict(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
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
            request = dict(requested_intervals=("5m", "15m"), days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
            RefreshTrustedMarketData(store, StaticProvider()).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-old"))
            loader = LoadTrustedBundle(store)
            old_context = loader.open_context(now_ms=86_400_000)
            RefreshTrustedMarketData(store, StaticProvider(offset=10)).execute(RefreshTrustedMarketDataRequest(**request, run_id="run-new"))

            old_bundle = loader.execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m", "15m"), days=1),
                observe_only_policy(),
                context=old_context,
            )
            new_bundle = loader.execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m", "15m"), days=1, now_ms=86_400_000),
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
                        LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000),
                        observe_only_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_generation_manifest_identity_must_match_current_pointer(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, ManifestSchemaError
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest = write_flat_v3_publication(data_dir, run_id="run-b")
            (data_dir / "manifest.json").unlink()
            write_generation_pointer(data_dir, generation_id="run-a", manifest=manifest)
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000),
                observe_only_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            manifest = write_flat_v3_publication(data_dir, run_id="run-b")
            with self.assertRaises(ManifestSchemaError):
                store.write_generation_manifest("run-a", manifest)

    def test_generation_source_file_must_match_dataset_key_exactly(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = {
            "other_symbol": f"okx/ETH-USDT-SWAP/5m.csv",
            "wrong_interval": f"okx/{SYMBOL}/15m.csv",
            "nested_relative": f"okx/{SYMBOL}/nested/5m.csv",
        }
        for name, source_file in cases.items():
            with self.subTest(name=name):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    manifest = write_flat_v3_publication(data_dir, run_id="run-source")
                    (data_dir / "manifest.json").unlink()
                    manifest["symbols"][SYMBOL]["intervals"]["5m"]["source_file"] = source_file
                    write_generation_pointer(data_dir, generation_id="run-source", manifest=manifest)
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                        LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000),
                        observe_only_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_generation_source_file_must_stay_relative_inside_generation_root(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        bad_sources = ("C:\\outside.csv", "C:/outside.csv", "\\\\server\\share\\outside.csv", "/outside.csv", "../outside.csv")
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
                        LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000),
                        observe_only_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_store_no_longer_exposes_dynamic_cache_or_manifest_helpers(self):
        store_source = Path("mu_strategy/market_data/trusted_data/store.py").read_text(encoding="utf-8")
        load_source = Path("mu_strategy/market_data/trusted_data/load.py").read_text(encoding="utf-8")

        self.assertNotIn("def cache_path(", store_source)
        self.assertNotIn("def manifest_path(", store_source)
        self.assertNotIn("_current_generation_id_or_none", store_source)
        self.assertNotIn(".cache_path(", load_source)
        self.assertEqual(1, load_source.count("self.store.read_manifest("))

    def test_flat_manifest_strict_and_compatibility_read_matrix(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = (
            ("flat v3 strict", write_flat_v3_publication, False, True, None),
            ("flat v2 strict", write_flat_v2_publication, False, False, HealthReason.MALFORMED_MANIFEST),
            ("flat v2 compatibility", write_flat_v2_publication, True, True, None),
            ("flat v1 strict", write_flat_v1_publication, False, False, HealthReason.MALFORMED_MANIFEST),
            ("flat v1 compatibility", write_flat_v1_publication, True, True, None),
        )
        for name, writer, compatibility_mode, expected_ok, expected_reason in cases:
            with self.subTest(name=name):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    writer(data_dir)
                    result = TrustedDataStore(data_dir=data_dir).read_manifest(compatibility_mode=compatibility_mode)

                self.assertEqual(expected_ok, result.ok)
                self.assertEqual(expected_reason, result.reason)

    def test_flat_v3_reads_from_canonical_dataset_path_with_relative_data_dir(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        source_forms = (
            lambda data_dir, symbol, interval, path: f"okx/{symbol}/{interval}.csv",
            lambda data_dir, symbol, interval, path: f"{data_dir.as_posix()}/okx/{symbol}/{interval}.csv",
            lambda data_dir, symbol, interval, path: str(path.resolve()),
            lambda data_dir, symbol, interval, path: "",
        )
        for source_form in source_forms:
            with self.subTest(source_form=source_form):
                with TemporaryDirectory() as tmp:
                    temp_root = Path(tmp)
                    old_cwd = Path.cwd()
                    os.chdir(temp_root)
                    try:
                        data_dir = Path("data/live")
                        write_flat_v3_publication(data_dir, source_file=lambda symbol, interval, path: source_form(data_dir, symbol, interval, path))
                        bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                            LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000),
                            trading_strict_policy(),
                        )
                    finally:
                        os.chdir(old_cwd)

                self.assertTrue(bundle.trust_decision.allowed, bundle.trust_decision)
                self.assertEqual(Path("data/live/okx") / SYMBOL / "5m.csv", bundle.files_by_interval["5m"])

    def test_generation_mode_rejects_schema_v2_even_with_compatibility_mode(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest = write_flat_v2_publication(data_dir, run_id="run-v2")
            (data_dir / "manifest.json").unlink()
            write_generation_pointer(data_dir, generation_id="run-v2", manifest=manifest)
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000, compatibility_mode=True),
                observe_only_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_next_refresh_preserves_flat_manifest_and_publishes_generation_pointer(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_v2_publication(data_dir)
            flat_manifest_before = (data_dir / "manifest.json").read_bytes()
            flat_csv_before = (data_dir / "okx" / SYMBOL / "5m.csv").read_bytes()

            RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), StaticProvider(offset=10)).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=86_400_000,
                    run_id="run-next",
                )
            )

            self.assertEqual(flat_manifest_before, (data_dir / "manifest.json").read_bytes())
            self.assertEqual(flat_csv_before, (data_dir / "okx" / SYMBOL / "5m.csv").read_bytes())
            self.assertEqual("run-next", read_current(data_dir)["generation_id"])
            manifest = generation_manifest(data_dir, "run-next")
            self.assertEqual(3, manifest["schema_version"])
            self.assertTrue((data_dir / "generations" / "run-next" / "manifest.json").exists())

    def test_publication_order_is_csv_manifest_current_then_audit_log(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            calls = []
            original_write_csv = store.write_csv
            original_write_manifest = store.write_generation_manifest
            original_replace_current = store.replace_current
            original_append_log = store.append_run_log

            def write_csv(*args, **kwargs):
                calls.append("generation CSV")
                return original_write_csv(*args, **kwargs)

            def write_manifest(*args, **kwargs):
                calls.append("generation manifest")
                return original_write_manifest(*args, **kwargs)

            def replace_current(*args, **kwargs):
                calls.append("current pointer")
                return original_replace_current(*args, **kwargs)

            def append_log(*args, **kwargs):
                calls.append("audit log")
                return original_append_log(*args, **kwargs)

            with patch.object(store, "write_csv", side_effect=write_csv):
                with patch.object(store, "write_generation_manifest", side_effect=write_manifest):
                    with patch.object(store, "replace_current", side_effect=replace_current):
                        with patch.object(store, "append_run_log", side_effect=append_log):
                            RefreshTrustedMarketData(store, StaticProvider()).execute(
                                RefreshTrustedMarketDataRequest(
                                    requested_intervals=("5m",),
                                    days=1,
                                    limit=1,
                                    stock_token_inst_ids=set(),
                                    now_ms=86_400_000,
                                    run_id="run-order",
                                )
                            )

        self.assertEqual(["generation CSV", "generation manifest", "current pointer", "audit log"], calls)

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
                    now_ms=86_400_000,
                    run_id="run-good",
                )
            )
            csv_path = current_generation_dir(data_dir) / "okx" / SYMBOL / "5m.csv"
            csv_path.write_text(csv_path.read_text(encoding="utf-8").replace("100.0", "100.5", 1), encoding="utf-8")
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=86_400_000),
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
                LoadTrustedBundleQuery(SYMBOL, intervals=("15m",), days=1, now_ms=86_400_000),
                trading_strict_policy(),
            )
            RefreshTrustedMarketData(store, StaticProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=86_400_000,
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
            ("run-stale", StaticProvider(), 10 * 86_400_000, HealthReason.MANIFEST_STALE),
            ("run-invalid", HoleyProvider(), 86_400_000, HealthReason.MANIFEST_INVALID),
            ("run-failed", UniverseFailureProvider(), 86_400_000, HealthReason.RUN_FAILED),
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


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


def _tree(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    return tuple(sorted(item.relative_to(path).as_posix() for item in path.rglob("*")))


if __name__ == "__main__":
    unittest.main()
