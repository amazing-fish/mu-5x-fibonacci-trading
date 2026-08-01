import io
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle
from tests.factories.trusted_publication import range_candles, write_generation_publication


DAY_MS = 86_400_000
SYMBOL = "MU-USDT-SWAP"


class _ReuseOnlyProvider:
    def __init__(self):
        self.history_calls = []
        self.incremental_calls = []

    def fetch_tickers(self):
        raise AssertionError("explicit-symbol refresh must not fetch tickers")

    def fetch_history(self, symbol, interval, *, days):
        self.history_calls.append((symbol, interval, days))
        raise AssertionError("current generation should prevent a full refetch")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        self.incremental_calls.append((symbol, interval, since_time_ms))
        return []


class _StaticProvider:
    def fetch_tickers(self):
        raise AssertionError("explicit-symbol refresh must not fetch tickers")

    def fetch_history(self, symbol, interval, *, days):
        return range_candles(0, DAY_MS - 300_000)

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        return []


class GenerationRetentionTests(unittest.TestCase):
    def test_windows_store_mutex_uses_cross_session_namespace_and_canonical_path(self):
        from mu_strategy.market_data.trusted_data.store import _WINDOWS_MUTEX_SDDL, _windows_store_mutex_name

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            data_dir.mkdir()

            direct = _windows_store_mutex_name(data_dir)
            equivalent = _windows_store_mutex_name(data_dir / ".")

            self.assertTrue(direct.startswith("Global\\mu_strategy_trusted_store_"))
            self.assertEqual(direct, equivalent)
            self.assertIn(";;;SY)", _WINDOWS_MUTEX_SDDL)
            self.assertIn(";;;BA)", _WINDOWS_MUTEX_SDDL)
            self.assertIn(";;;AU)", _WINDOWS_MUTEX_SDDL)
            self.assertNotIn(";;;WD)", _WINDOWS_MUTEX_SDDL)

    def test_current_generation_survives_outside_keep_recent_window(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_plain_generation(data_dir, "current-oldest", b"current", mtime_ns=1_000_000_000)
            _write_plain_generation(data_dir, "middle", b"middle", mtime_ns=2_000_000_000)
            _write_plain_generation(data_dir, "newest", b"newest", mtime_ns=3_000_000_000)
            _write_current_pointer(data_dir, "current-oldest")

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1)
            )

            self.assertTrue((data_dir / "generations" / "current-oldest").is_dir())
            self.assertTrue((data_dir / "generations" / "newest").is_dir())
            self.assertFalse((data_dir / "generations" / "middle").exists())
            self.assertEqual(("middle",), report.removed_ids)

    def test_incremental_reuse_still_reads_oldest_current_after_reclamation(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="current-oldest",
            )
            os.utime(data_dir / "generations" / "current-oldest", ns=(1_000_000_000, 1_000_000_000))
            _write_plain_generation(data_dir, "middle", b"middle", mtime_ns=2_000_000_000)
            _write_plain_generation(data_dir, "newest", b"newest", mtime_ns=3_000_000_000)
            store = TrustedDataStore(data_dir=data_dir)

            store.reclaim_generations(GenerationRetentionPolicy(keep_recent=1))
            provider = _ReuseOnlyProvider()
            run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=DAY_MS,
                    run_id="after-reclamation",
                )
            )

            self.assertEqual([], provider.history_calls)
            self.assertEqual(1, len(provider.incremental_calls))
            self.assertTrue(run.refresh_segments[0].reused_prior_generation)
            self.assertEqual("incremental_reuse", run.refresh_segments[0].fetch_mode)

    def test_open_load_context_survives_generation_reclamation(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-old",
            )
            store = TrustedDataStore(
                data_dir=data_dir,
                retention_policy=GenerationRetentionPolicy(keep_recent=1),
            )
            loader = LoadTrustedBundle(store)
            old_context = loader.open_context(now_ms=DAY_MS)

            RefreshTrustedMarketData(store, _StaticProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    symbols=(SYMBOL,),
                    now_ms=DAY_MS,
                    run_id="run-new",
                )
            )
            self.assertFalse((data_dir / "generations" / "run-old").exists())

            bundle = loader.execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=DAY_MS),
                trading_strict_policy(),
                context=old_context,
            )

            self.assertEqual("run-old", bundle.run_id)
            self.assertTrue(bundle.candles_by_interval["5m"])
            self.assertTrue(bundle.trust_decision.allowed)

    def test_load_context_snapshot_blocks_publication_until_file_read_completes(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-old",
            )
            reader_store = TrustedDataStore(data_dir=data_dir)
            publisher_store = TrustedDataStore(
                data_dir=data_dir,
                retention_policy=GenerationRetentionPolicy(keep_recent=1),
            )
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()
            publication_attempted = threading.Event()
            publication_done = threading.Event()
            errors = []
            original_read_file_bytes = reader_store.read_file_bytes
            original_commit = publisher_store.commit_generation_publication

            def blocking_read_file_bytes(path):
                snapshot_started.set()
                if not release_snapshot.wait(10):
                    raise TimeoutError("snapshot release timed out")
                return original_read_file_bytes(path)

            def observed_commit(*args, **kwargs):
                publication_attempted.set()
                return original_commit(*args, **kwargs)

            def load_context():
                try:
                    LoadTrustedBundle(reader_store).open_context(now_ms=DAY_MS)
                except BaseException as exc:
                    errors.append(exc)

            def publish():
                try:
                    RefreshTrustedMarketData(publisher_store, _StaticProvider()).execute(
                        RefreshTrustedMarketDataRequest(
                            requested_intervals=("5m",),
                            days=1,
                            symbols=(SYMBOL,),
                            now_ms=DAY_MS,
                            run_id="run-new",
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    publication_done.set()

            with patch.object(reader_store, "read_file_bytes", side_effect=blocking_read_file_bytes):
                with patch.object(publisher_store, "commit_generation_publication", side_effect=observed_commit):
                    reader_thread = threading.Thread(target=load_context)
                    publisher_thread = threading.Thread(target=publish)
                    reader_thread.start()
                    try:
                        self.assertTrue(snapshot_started.wait(10))
                        publisher_thread.start()
                        self.assertTrue(publication_attempted.wait(10))
                        self.assertFalse(
                            publication_done.wait(0.25),
                            "publication entered reclamation while a load snapshot was open",
                        )
                    finally:
                        release_snapshot.set()
                        reader_thread.join(10)
                        publisher_thread.join(10)

            self.assertFalse(reader_thread.is_alive())
            self.assertFalse(publisher_thread.is_alive())
            self.assertEqual([], errors)

    def test_overlapping_refreshes_serialize_prior_generation_reuse(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        class BlockingIncrementalProvider(_ReuseOnlyProvider):
            def __init__(self):
                super().__init__()
                self.incremental_started = threading.Event()
                self.release_incremental = threading.Event()

            def fetch_incremental(self, symbol, interval, *, since_time_ms):
                self.incremental_calls.append((symbol, interval, since_time_ms))
                self.incremental_started.set()
                if not self.release_incremental.wait(10):
                    raise TimeoutError("incremental release timed out")
                return []

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-old",
            )
            policy = GenerationRetentionPolicy(keep_recent=1)
            slow_provider = BlockingIncrementalProvider()
            fast_provider = BlockingIncrementalProvider()
            fast_provider.release_incremental.set()
            slow_refresh = RefreshTrustedMarketData(
                TrustedDataStore(data_dir=data_dir, retention_policy=policy),
                slow_provider,
            )
            fast_refresh = RefreshTrustedMarketData(
                TrustedDataStore(data_dir=data_dir, retention_policy=policy),
                fast_provider,
            )
            results = {}
            errors = []

            def execute(name, refresh, run_id):
                try:
                    results[name] = refresh.execute(
                        RefreshTrustedMarketDataRequest(
                            requested_intervals=("5m",),
                            days=1,
                            symbols=(SYMBOL,),
                            now_ms=DAY_MS,
                            run_id=run_id,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            slow_thread = threading.Thread(target=execute, args=("slow", slow_refresh, "run-slow"))
            fast_thread = threading.Thread(target=execute, args=("fast", fast_refresh, "run-fast"))
            slow_thread.start()
            try:
                self.assertTrue(slow_provider.incremental_started.wait(10))
                fast_thread.start()
                self.assertFalse(
                    fast_provider.incremental_started.wait(0.25),
                    "overlapping refresh read the prior generation before its owner committed",
                )
            finally:
                slow_provider.release_incremental.set()
                slow_thread.join(10)
                fast_thread.join(10)

            self.assertFalse(slow_thread.is_alive())
            self.assertFalse(fast_thread.is_alive())
            self.assertEqual([], errors)
            self.assertTrue(results["slow"].refresh_segments[0].reused_prior_generation)
            self.assertTrue(results["fast"].refresh_segments[0].reused_prior_generation)
            self.assertEqual([], slow_provider.history_calls)
            self.assertEqual([], fast_provider.history_calls)
            self.assertEqual(
                "run-fast",
                json.loads((data_dir / "current.json").read_text(encoding="utf-8"))["generation_id"],
            )

    def test_refresh_lifecycle_lock_releases_after_prepublication_failure(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-existing",
            )
            policy = GenerationRetentionPolicy(keep_recent=1)
            failing_refresh = RefreshTrustedMarketData(
                TrustedDataStore(data_dir=data_dir, retention_policy=policy),
                _ReuseOnlyProvider(),
            )
            with self.assertRaises(FileExistsError):
                failing_refresh.execute(
                    RefreshTrustedMarketDataRequest(
                        requested_intervals=("5m",),
                        days=1,
                        symbols=(SYMBOL,),
                        now_ms=DAY_MS,
                        run_id="run-existing",
                    )
                )

            succeeding_refresh = RefreshTrustedMarketData(
                TrustedDataStore(data_dir=data_dir, retention_policy=policy),
                _ReuseOnlyProvider(),
            )
            results = []
            errors = []

            def execute_succeeding_refresh():
                try:
                    results.append(
                        succeeding_refresh.execute(
                            RefreshTrustedMarketDataRequest(
                                requested_intervals=("5m",),
                                days=1,
                                symbols=(SYMBOL,),
                                now_ms=DAY_MS,
                                run_id="run-next",
                            )
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=execute_succeeding_refresh, daemon=True)
            thread.start()
            thread.join(10)

            self.assertFalse(thread.is_alive(), "refresh lifecycle lock remained held after failure")
            self.assertEqual([], errors)
            self.assertEqual("run-next", results[0].run_id)

    def test_keep_one_removes_exactly_the_oldest_non_current_generations(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            for index, generation_id in enumerate(("run-a", "run-b", "run-c", "run-current"), start=1):
                _write_plain_generation(
                    data_dir,
                    generation_id,
                    generation_id.encode("ascii"),
                    mtime_ns=index * 1_000_000_000,
                )
            _write_current_pointer(data_dir, "run-current")

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1)
            )

            self.assertEqual(("run-a", "run-b", "run-c"), report.candidate_ids)
            self.assertEqual(report.candidate_ids, report.removed_ids)
            self.assertEqual(["run-current"], _generation_ids(data_dir))

    def test_keep_recent_lower_bound_rejects_zero_and_bool(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy

        for value in (0, -1, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "at least 1"):
                    GenerationRetentionPolicy(keep_recent=value)

    def test_retention_pin_preserves_release_provenance_outside_recent_window(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            pinned = _write_plain_generation(
                data_dir,
                "release-evidence",
                b"pinned",
                mtime_ns=1_000_000_000,
            )
            _write_retention_pin(pinned, reason="strategy_release_provenance")
            removable = _write_plain_generation(
                data_dir,
                "run-old",
                b"old",
                mtime_ns=2_000_000_000,
            )
            _write_plain_generation(
                data_dir,
                "run-current",
                b"current",
                mtime_ns=3_000_000_000,
            )
            _write_current_pointer(data_dir, "run-current")

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1)
            )

            self.assertTrue(pinned.is_dir())
            self.assertFalse(removable.exists())
            self.assertEqual(("release-evidence",), report.pinned_ids)
            self.assertEqual(("run-old",), report.removed_ids)

    def test_malformed_retention_pin_preserves_generation_without_blocking_valid_cleanup(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            malformed = _write_plain_generation(
                data_dir,
                "release-evidence",
                b"preserve",
                mtime_ns=1_000_000_000,
            )
            (malformed / "retention-pin.json").write_bytes(b"{")
            removable = _write_plain_generation(
                data_dir,
                "run-old",
                b"old",
                mtime_ns=2_000_000_000,
            )
            _write_plain_generation(
                data_dir,
                "run-current",
                b"current",
                mtime_ns=3_000_000_000,
            )
            _write_current_pointer(data_dir, "run-current")

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1)
            )

            self.assertTrue(malformed.is_dir())
            self.assertFalse(removable.exists())
            self.assertEqual(("run-old",), report.removed_ids)
            self.assertEqual("release-evidence", report.failures[0].generation_id)
            self.assertIn("retention pin is malformed", report.failures[0].message)

    def test_retention_pin_contract_rejects_identity_schema_reason_and_extra_fields(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        invalid_payloads = (
            {"schema_version": 2, "generation_id": "release-evidence", "reason": "strategy_release_provenance"},
            {"schema_version": 1, "generation_id": "another-generation", "reason": "strategy_release_provenance"},
            {"schema_version": 1, "generation_id": "release-evidence", "reason": "manual"},
            {
                "schema_version": 1,
                "generation_id": "release-evidence",
                "reason": "strategy_release_provenance",
                "unexpected": True,
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), TemporaryDirectory() as tmp:
                data_dir = Path(tmp)
                preserved = _write_plain_generation(
                    data_dir,
                    "release-evidence",
                    b"preserve",
                    mtime_ns=1_000_000_000,
                )
                (preserved / "retention-pin.json").write_text(json.dumps(payload), encoding="utf-8")
                removable = _write_plain_generation(
                    data_dir,
                    "run-old",
                    b"old",
                    mtime_ns=2_000_000_000,
                )
                _write_plain_generation(
                    data_dir,
                    "run-current",
                    b"current",
                    mtime_ns=3_000_000_000,
                )
                _write_current_pointer(data_dir, "run-current")

                report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                    GenerationRetentionPolicy(keep_recent=1)
                )

                self.assertTrue(preserved.is_dir())
                self.assertFalse(removable.exists())
                self.assertEqual(("run-old",), report.removed_ids)
                self.assertEqual("release-evidence", report.failures[0].generation_id)

    def test_retention_pin_must_be_a_regular_file(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            preserved = _write_plain_generation(
                data_dir,
                "release-evidence",
                b"preserve",
                mtime_ns=1_000_000_000,
            )
            (preserved / "retention-pin.json").mkdir()
            removable = _write_plain_generation(
                data_dir,
                "run-old",
                b"old",
                mtime_ns=2_000_000_000,
            )
            _write_plain_generation(
                data_dir,
                "run-current",
                b"current",
                mtime_ns=3_000_000_000,
            )
            _write_current_pointer(data_dir, "run-current")

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1)
            )

            self.assertTrue(preserved.is_dir())
            self.assertFalse(removable.exists())
            self.assertEqual(("run-old",), report.removed_ids)
            self.assertIn("regular file", report.failures[0].message)

    def test_malformed_generation_is_preserved_without_blocking_valid_cleanup(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            malformed = _write_plain_generation(
                data_dir,
                "malformed",
                b"preserve",
                mtime_ns=1_000_000_000,
                committed=False,
            )
            (malformed / "manifest.json").write_bytes(b"{")
            removable = _write_plain_generation(
                data_dir,
                "run-old",
                b"old",
                mtime_ns=2_000_000_000,
            )
            _write_plain_generation(
                data_dir,
                "run-current",
                b"current",
                mtime_ns=3_000_000_000,
            )
            _write_current_pointer(data_dir, "run-current")

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1)
            )

            self.assertTrue(malformed.is_dir())
            self.assertFalse(removable.exists())
            self.assertEqual(("run-old",), report.removed_ids)
            self.assertEqual("malformed", report.failures[0].generation_id)

    def test_malformed_current_manifest_aborts_reclamation(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            fallback = _write_plain_generation(
                data_dir,
                "run-fallback",
                b"fallback",
                mtime_ns=1_000_000_000,
            )
            current = _write_plain_generation(
                data_dir,
                "run-current",
                b"current",
                mtime_ns=2_000_000_000,
            )
            (current / "manifest.json").write_bytes(b"{")
            _write_current_pointer(data_dir, "run-current")

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1)
            )

            self.assertTrue(fallback.is_dir())
            self.assertTrue(current.is_dir())
            self.assertEqual((), report.candidate_ids)
            self.assertEqual("run-current", report.failures[0].generation_id)

    def test_publication_serializes_manifest_to_pointer_with_reclamation(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_plain_generation(data_dir, "run-old", b"old", mtime_ns=1_000_000_000)
            _write_plain_generation(data_dir, "run-current", b"current", mtime_ns=2_000_000_000)
            _write_current_pointer(data_dir, "run-current")
            publisher = TrustedDataStore(data_dir=data_dir)
            publisher.prepare_generation("run-next")
            manifest = json.loads(_plain_generation_manifest_bytes("run-next"))
            original_replace = publisher.replace_current
            manifest_visible = threading.Event()
            release_publication = threading.Event()
            reclamation_done = threading.Event()
            errors = []

            def blocking_replace(generation_id):
                os.utime(
                    data_dir / "generations" / generation_id,
                    ns=(500_000_000, 500_000_000),
                )
                manifest_visible.set()
                if not release_publication.wait(10):
                    raise TimeoutError("publication release timed out")
                return original_replace(generation_id)

            def publish():
                try:
                    publisher.commit_generation_publication(
                        "run-next",
                        manifest,
                        {"run_id": "run-next"},
                    )
                except BaseException as exc:
                    errors.append(exc)

            def reclaim():
                try:
                    TrustedDataStore(data_dir=data_dir).reclaim_generations(
                        GenerationRetentionPolicy(keep_recent=1)
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    reclamation_done.set()

            with patch.object(publisher, "replace_current", side_effect=blocking_replace):
                publisher_thread = threading.Thread(target=publish)
                reclaimer_thread = threading.Thread(target=reclaim)
                publisher_thread.start()
                try:
                    self.assertTrue(manifest_visible.wait(10))
                    reclaimer_thread.start()
                    self.assertFalse(
                        reclamation_done.wait(0.25),
                        "reclamation entered the manifest-to-pointer publication window",
                    )
                finally:
                    release_publication.set()
                    publisher_thread.join(10)
                    reclaimer_thread.join(10)

            self.assertFalse(publisher_thread.is_alive())
            self.assertFalse(reclaimer_thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual("run-next", json.loads((data_dir / "current.json").read_text())["generation_id"])
            self.assertTrue((data_dir / "generations" / "run-next").is_dir())

    def test_reclamation_runs_after_current_pointer_and_before_run_log(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(
                data_dir=Path(tmp),
                retention_policy=GenerationRetentionPolicy(keep_recent=1),
            )
            calls = []
            original_replace = store.replace_current
            original_reclaim = store._reclaim_generations_locked
            original_append = store.append_run_log

            with patch.object(store, "replace_current", side_effect=lambda *args, **kwargs: calls.append("current") or original_replace(*args, **kwargs)):
                with patch.object(store, "_reclaim_generations_locked", side_effect=lambda *args, **kwargs: calls.append("reclamation") or original_reclaim(*args, **kwargs)):
                    with patch.object(store, "append_run_log", side_effect=lambda *args, **kwargs: calls.append("run_log") or original_append(*args, **kwargs)):
                        RefreshTrustedMarketData(store, _StaticProvider()).execute(
                            RefreshTrustedMarketDataRequest(
                                requested_intervals=("5m",),
                                days=1,
                                symbols=(SYMBOL,),
                                now_ms=DAY_MS,
                                run_id="run-order",
                            )
                        )

            self.assertEqual(["current", "reclamation", "run_log"], calls)

    def test_delete_failure_warns_without_failing_refresh_and_reaches_run_log(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-old",
            )
            _write_plain_generation(data_dir, "orphan", b"orphan", mtime_ns=1_000_000_000)
            stdout = io.StringIO()
            with patch("mu_strategy.market_data.trusted_data.refresh.uuid.uuid4", return_value=_Hex("run-published")):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_incremental", return_value=[]):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=DAY_MS):
                        with patch("mu_strategy.market_data.trusted_data.store.shutil.rmtree", side_effect=PermissionError("delete denied")):
                            exit_code = main(
                                [
                                    "--symbol",
                                    SYMBOL,
                                    "--interval",
                                    "5m",
                                    "--days",
                                    "1",
                                    "--keep-generations",
                                    "1",
                                    "--data-dir",
                                    str(data_dir),
                                    "--html-output",
                                    str(Path(tmp) / "health.html"),
                                ],
                                stdout=stdout,
                            )

            output = json.loads(stdout.getvalue())
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1, now_ms=DAY_MS),
                trading_strict_policy(),
            )

            self.assertEqual(0, exit_code)
            self.assertTrue(bundle.trust_decision.allowed)
            self.assertEqual("run-published", bundle.run_id)
            self.assertTrue(any("generation_reclamation_failed" in warning for warning in output["warnings"]))
            self.assertTrue(run_log[-1]["reclamation"]["failures"])
            self.assertTrue(any("generation_reclamation_failed" in warning for warning in run_log[-1]["warnings"]))

    def test_crafted_traversal_target_is_refused_without_touching_outside(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            _write_plain_generation(data_dir, "current", b"current", mtime_ns=2_000_000_000)
            _write_current_pointer(data_dir, "current")
            outside = data_dir / "outside"
            outside.mkdir()
            marker = outside / "marker.bin"
            marker.write_bytes(b"do not delete")
            crafted = data_dir / "generations" / ".." / "outside"
            store = TrustedDataStore(data_dir=data_dir)

            with patch.object(Path, "iterdir", return_value=iter((crafted,))):
                report = store.reclaim_generations(GenerationRetentionPolicy(keep_recent=1))

            self.assertEqual((), report.removed_ids)
            self.assertTrue(report.failures)
            self.assertEqual(b"do not delete", marker.read_bytes())
            self.assertTrue((data_dir / "generations" / "current").is_dir())

    def test_dry_run_reports_candidates_and_preserves_tree_bytes(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_plain_generation(data_dir, "run-old", b"old bytes", mtime_ns=1_000_000_000)
            _write_plain_generation(data_dir, "run-current", b"current bytes", mtime_ns=2_000_000_000)
            _write_current_pointer(data_dir, "run-current")
            before = _tree_bytes(data_dir)

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1),
                dry_run=True,
            )

            self.assertEqual(before, _tree_bytes(data_dir))
            self.assertEqual(("run-old",), report.candidate_ids)
            self.assertEqual((), report.removed_ids)
            expected_bytes = sum(
                len(payload)
                for relative, payload in before.items()
                if relative.startswith("generations/run-old/") and payload is not None
            )
            self.assertEqual(expected_bytes, report.bytes_reclaimable)
            self.assertEqual(0, report.bytes_reclaimed)

    def test_command_dry_run_reports_candidates_without_deleting_them(self):
        from mu_strategy.commands.refresh_market_data import main

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-old",
            )
            orphan = _write_plain_generation(data_dir, "orphan", b"orphan bytes", mtime_ns=1_000_000_000)
            orphan_before = _tree_bytes(orphan)
            stdout = io.StringIO()
            with patch("mu_strategy.market_data.trusted_data.refresh.uuid.uuid4", return_value=_Hex("run-current")):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_incremental", return_value=[]):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=DAY_MS):
                        exit_code = main(
                            [
                                "--symbol",
                                SYMBOL,
                                "--interval",
                                "5m",
                                "--days",
                                "1",
                                "--keep-generations",
                                "1",
                                "--dry-run",
                                "--data-dir",
                                str(data_dir),
                                "--html-output",
                                str(Path(tmp) / "health.html"),
                            ],
                            stdout=stdout,
                        )

            reclamation = json.loads(stdout.getvalue())["reclamation"]
            self.assertEqual(0, exit_code)
            self.assertTrue(reclamation["dry_run"])
            self.assertEqual([], reclamation["removed_ids"])
            self.assertIn("orphan", reclamation["candidate_ids"])
            self.assertEqual(orphan_before, _tree_bytes(orphan))

    def test_command_rejects_keep_generations_below_one(self):
        from mu_strategy.commands.refresh_market_data import main

        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                main(["--keep-generations", "0"], stdout=io.StringIO())
        self.assertEqual(2, raised.exception.code)

    def test_run_log_keeps_recent_entries_at_fixed_line_bound(self):
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp), run_log_max_lines=3)
            sizes = []
            for index in range(6):
                store.append_run_log({"run_id": f"run-{index}"})
                sizes.append(store.run_log_path.stat().st_size)

            rows = [json.loads(line) for line in store.run_log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["run-3", "run-4", "run-5"], [row["run_id"] for row in rows])
            self.assertEqual(3, len(rows))
            self.assertEqual(sizes[2], sizes[3])
            self.assertEqual(sizes[3], sizes[4])
            self.assertEqual(sizes[4], sizes[5])

    def test_run_log_records_removed_ids_and_bytes(self):
        from mu_strategy.commands.refresh_market_data import main

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-old",
            )
            _write_plain_generation(data_dir, "orphan", b"orphan bytes", mtime_ns=1_000_000_000)
            stdout = io.StringIO()
            with patch("mu_strategy.market_data.trusted_data.refresh.uuid.uuid4", return_value=_Hex("run-current")):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_incremental", return_value=[]):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=DAY_MS):
                        exit_code = main(
                            [
                                "--symbol",
                                SYMBOL,
                                "--interval",
                                "5m",
                                "--days",
                                "1",
                                "--keep-generations",
                                "1",
                                "--data-dir",
                                str(data_dir),
                                "--html-output",
                                str(Path(tmp) / "health.html"),
                            ],
                            stdout=stdout,
                        )

            output = json.loads(stdout.getvalue())
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(0, exit_code)
            self.assertEqual(["orphan", "run-old"], sorted(run_log["reclamation"]["removed_ids"]))
            self.assertGreater(run_log["reclamation"]["bytes_reclaimed"], 0)
            self.assertEqual(run_log["reclamation"], output["reclamation"])

    def test_six_equal_shape_refresh_cycles_plateau_at_keep_three(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(
                data_dir=data_dir,
                run_log_max_lines=1,
                retention_policy=GenerationRetentionPolicy(keep_recent=3),
            )
            provider = _StaticProvider()
            refresh = RefreshTrustedMarketData(store, provider, clock=_FixedClock(DAY_MS))
            counts = []
            sizes = []
            reused = []

            for index in range(1, 7):
                run = refresh.execute(
                    RefreshTrustedMarketDataRequest(
                        requested_intervals=("5m",),
                        days=1,
                        symbols=(SYMBOL,),
                        now_ms=DAY_MS,
                        run_id=f"fixture-{index:02d}",
                    )
                )
                counts.append(len(_generation_ids(data_dir)))
                sizes.append(sum(path.stat().st_size for path in data_dir.rglob("*") if path.is_file()))
                reused.append(run.refresh_segments[0].reused_prior_generation)

            self.assertEqual([1, 2, 3, 3, 3, 3], counts)
            self.assertEqual(sizes[3], sizes[4])
            self.assertEqual(sizes[4], sizes[5])
            self.assertEqual([False, True, True, True, True, True], reused)
            pointer = json.loads((data_dir / "current.json").read_text(encoding="utf-8"))
            self.assertEqual("fixture-06", pointer["generation_id"])
            self.assertTrue((data_dir / pointer["manifest"]).is_file())

    def test_run_log_discards_incomplete_tail_and_keeps_recent_complete_entries(self):
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp), run_log_max_lines=3)
            store.run_log_path.write_text(
                '{"run_id":"run-0"}\n{"run_id":"run-1"}\n{"run_id":"torn"',
                encoding="utf-8",
            )

            store.append_run_log({"run_id": "run-2"})

            rows = [json.loads(line) for line in store.run_log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["run-0", "run-1", "run-2"], [row["run_id"] for row in rows])

    def test_run_log_discards_tail_torn_inside_multibyte_utf8(self):
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp), run_log_max_lines=3)
            complete = json.dumps({"run_id": "run-0"}, ensure_ascii=False).encode("utf-8") + b"\n"
            multibyte_prefix = "中".encode("utf-8")[:1]
            store.run_log_path.write_bytes(complete + b'{"message":"' + multibyte_prefix)

            store.append_run_log({"run_id": "run-1", "message": "中文"})

            rows = [json.loads(line) for line in store.run_log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["run-0", "run-1"], [row["run_id"] for row in rows])
            self.assertEqual("中文", rows[-1]["message"])


class ConsumerReclamationBoundaryTests(unittest.TestCase):
    def test_all_consumer_entrypoints_complete_with_reclamation_blocked(self):
        from mu_strategy import cli, visualize
        from mu_strategy.commands import okx_demo_loop
        from mu_strategy.demo_trading import run_once
        from mu_strategy.entry.scanner import EntryScanResult
        from mu_strategy.experiments import fibonacci_pullback, walk_forward
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        now_ms = 20 * DAY_MS
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "live"
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=now_ms - DAY_MS,
                end_ms=now_ms,
                run_id="consumer-current",
            )
            commands = (
                (
                    cli.main,
                    ["mu_strategy.cli", "--days", "1", "--data-dir", str(data_dir), "--report", str(root / "cli.md")],
                ),
                (
                    visualize.main,
                    ["mu_strategy.visualize", "--days", "1", "--data-dir", str(data_dir), "--output", str(root / "viz.html")],
                ),
                (
                    walk_forward.main,
                    [
                        "mu_strategy.experiments.walk_forward",
                        "--window-days",
                        "1",
                        "--windows",
                        "1",
                        "--data-dir",
                        str(data_dir),
                        "--report",
                        str(root / "wf.md"),
                        "--html-report",
                        str(root / "wf.html"),
                    ],
                ),
                (
                    fibonacci_pullback.main,
                    [
                        "mu_strategy.experiments.fibonacci_pullback",
                        "--days",
                        "1",
                        "--min-hour",
                        "2",
                        "--max-hour",
                        "2",
                        "--data-dir",
                        str(data_dir),
                        "--report",
                        str(root / "fib.md"),
                    ],
                ),
            )

            with patch.object(TrustedDataStore, "reclaim_generations", side_effect=AssertionError("consumer reclamation")):
                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=now_ms):
                    for entrypoint, argv in commands:
                        with self.subTest(entrypoint=argv[0]):
                            with patch("sys.argv", argv):
                                with patch("sys.stdout", new_callable=io.StringIO):
                                    entrypoint()

                    exit_code = okx_demo_loop.main(
                        ["--once", "--dry-run", "--data-dir", str(data_dir), "--limit", "1"],
                        stdout=io.StringIO(),
                        runner=lambda config, broker: run_once(
                            config,
                            broker=None,
                            scanner=lambda symbol, *_args, **_kwargs: EntryScanResult(
                                symbol=symbol,
                                action="wait",
                                reason="test",
                                last_close=100.0,
                                regime_1h="yellow",
                                rsi14=None,
                                macd_hist=None,
                                macd_hist_prev=None,
                            ),
                        ),
                    )

            self.assertEqual(0, exit_code)


class _Hex:
    def __init__(self, value):
        self.hex = value


class _FixedClock:
    def __init__(self, now_ms):
        self.value = now_ms

    def now_ms(self):
        return self.value


def _write_plain_generation(
    data_dir: Path,
    generation_id: str,
    payload: bytes,
    *,
    mtime_ns: int,
    committed: bool = True,
) -> Path:
    root = data_dir / "generations" / generation_id
    root.mkdir(parents=True)
    (root / "payload.bin").write_bytes(payload)
    if committed:
        (root / "manifest.json").write_bytes(_plain_generation_manifest_bytes(generation_id))
    os.utime(root, ns=(mtime_ns, mtime_ns))
    return root


def _plain_generation_manifest_bytes(generation_id: str) -> bytes:
    return json.dumps(
        {
            "schema_version": 3,
            "run_id": generation_id,
            "attempt_status": "failed",
            "snapshot_usability": "invalid",
            "started_at_ms": 0,
            "completed_at_ms": 0,
            "requested_intervals": ["5m"],
            "effective_intervals": ["5m"],
            "universes": {"crypto_top": [], "stock_token_top": []},
            "symbols": {},
            "provider_failures": [],
            "warnings": [],
            "cycle_error": {"error_type": "TestFixture", "message": "no datasets"},
        },
        sort_keys=True,
    ).encode("utf-8")


def _write_retention_pin(generation_root: Path, *, reason: str) -> Path:
    path = generation_root / "retention-pin.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation_id": generation_root.name,
                "reason": reason,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _write_current_pointer(data_dir: Path, generation_id: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation_id": generation_id,
                "manifest": f"generations/{generation_id}/manifest.json",
            }
        ),
        encoding="utf-8",
    )


def _generation_ids(data_dir: Path) -> list[str]:
    return sorted(path.name for path in (data_dir / "generations").iterdir())


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = None if path.is_dir() else path.read_bytes()
    return snapshot


if __name__ == "__main__":
    unittest.main()
