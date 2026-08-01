import io
import json
import multiprocessing
import os
import unittest
from contextlib import contextmanager
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
    def test_publication_snapshot_lock_serializes_processes_without_store_writes(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            tree_before = _tree_bytes(data_dir)
            context = multiprocessing.get_context("spawn")
            holder_ready = context.Event()
            release_holder = context.Event()
            waiter_attempting = context.Event()
            waiter_acquired = context.Event()
            holder = context.Process(
                target=_hold_store_lock,
                args=(str(data_dir), holder_ready, release_holder),
            )
            waiter = context.Process(
                target=_wait_for_store_lock,
                args=(str(data_dir), waiter_attempting, waiter_acquired),
            )
            holder.start()
            try:
                self.assertTrue(holder_ready.wait(10), "holder did not acquire the store lock")
                waiter.start()
                self.assertTrue(waiter_attempting.wait(10), "waiter did not attempt the store lock")
                self.assertFalse(waiter_acquired.wait(0.25), "waiter acquired before holder released")
                release_holder.set()
                self.assertTrue(waiter_acquired.wait(10), "waiter did not acquire after holder released")
            finally:
                release_holder.set()
                for process in (holder, waiter):
                    if process.pid is None:
                        continue
                    process.join(10)
                    if process.is_alive():
                        process.terminate()
                        process.join(10)
            self.assertEqual(0, holder.exitcode)
            self.assertEqual(0, waiter.exitcode)
            self.assertEqual(tree_before, _tree_bytes(data_dir))

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

    def test_refresh_reloads_and_snapshots_current_after_competing_universe_fetch(self):
        from mu_strategy.market_data.trusted_data.contracts import SnapshotUsability
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
            os.utime(data_dir / "generations" / "run-old", ns=(1_000_000_000, 1_000_000_000))
            writer_store = TrustedDataStore(
                data_dir=data_dir,
                retention_policy=GenerationRetentionPolicy(keep_recent=1),
            )

            class CompetingPublicationProvider(_ReuseOnlyProvider):
                def fetch_tickers(self):
                    competing = RefreshTrustedMarketData(writer_store, _StaticProvider()).execute(
                        RefreshTrustedMarketDataRequest(
                            requested_intervals=("5m",),
                            days=1,
                            symbols=(SYMBOL,),
                            now_ms=DAY_MS,
                            run_id="run-new",
                        )
                    )
                    self.assert_competing_usable = competing.snapshot_usability
                    self.old_generation_exists = (data_dir / "generations" / "run-old").exists()
                    return [{"instId": SYMBOL, "last": "100", "volCcy24h": "10"}]

            provider = CompetingPublicationProvider()
            run = RefreshTrustedMarketData(writer_store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=DAY_MS,
                    run_id="run-outer",
                )
            )

            self.assertEqual(SnapshotUsability.USABLE, provider.assert_competing_usable)
            self.assertFalse(provider.old_generation_exists)
            self.assertEqual([], provider.history_calls)
            self.assertEqual(1, len(provider.incremental_calls))
            self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
            self.assertTrue(run.refresh_segments[0].reused_prior_generation)
            self.assertEqual("incremental_reuse", run.refresh_segments[0].fetch_mode)

    def test_refresh_manifest_and_prior_bytes_share_one_lock_before_network_fetch(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-current",
            )
            store = TrustedDataStore(data_dir=data_dir)
            active = {"value": False}
            test_case = self
            original_read_manifest = store.read_manifest
            original_read_file_bytes = store.read_file_bytes

            @contextmanager
            def recording_lock():
                self.assertFalse(active["value"])
                active["value"] = True
                try:
                    yield
                finally:
                    active["value"] = False

            def read_manifest():
                self.assertTrue(active["value"])
                return original_read_manifest()

            def read_file_bytes(path):
                self.assertTrue(active["value"])
                return original_read_file_bytes(path)

            class LockAwareProvider(_ReuseOnlyProvider):
                def fetch_incremental(self, symbol, interval, *, since_time_ms):
                    test_case.assertFalse(active["value"])
                    return super().fetch_incremental(symbol, interval, since_time_ms=since_time_ms)

            provider = LockAwareProvider()
            with patch.object(store, "publication_snapshot_lock", side_effect=recording_lock):
                with patch.object(store, "read_manifest", side_effect=read_manifest):
                    with patch.object(store, "read_file_bytes", side_effect=read_file_bytes):
                        run = RefreshTrustedMarketData(store, provider).execute(
                            RefreshTrustedMarketDataRequest(
                                requested_intervals=("5m",),
                                days=1,
                                symbols=(SYMBOL,),
                                now_ms=DAY_MS,
                                run_id="run-after-snapshot",
                            )
                        )

            self.assertTrue(run.refresh_segments[0].reused_prior_generation)
            self.assertEqual("incremental_reuse", run.refresh_segments[0].fetch_mode)
            self.assertFalse(active["value"])

    def test_open_load_context_survives_reclamation_of_its_generation(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            writer_store = TrustedDataStore(
                data_dir=data_dir,
                retention_policy=GenerationRetentionPolicy(keep_recent=1),
            )
            request = dict(
                requested_intervals=("5m",),
                days=1,
                symbols=(SYMBOL,),
                now_ms=DAY_MS,
            )
            RefreshTrustedMarketData(writer_store, _StaticProvider()).execute(
                RefreshTrustedMarketDataRequest(**request, run_id="run-old")
            )
            loader = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir))
            old_context = loader.open_context(now_ms=DAY_MS)

            RefreshTrustedMarketData(writer_store, _StaticProvider()).execute(
                RefreshTrustedMarketDataRequest(**request, run_id="run-new")
            )
            self.assertFalse((data_dir / "generations" / "run-old").exists())
            old_bundle = loader.execute(
                LoadTrustedBundleQuery(SYMBOL, intervals=("5m",), days=1),
                observe_only_policy(),
                context=old_context,
            )

            self.assertEqual("run-old", old_bundle.run_id)
            self.assertTrue(old_bundle.trust_decision.allowed)
            self.assertTrue(old_bundle.candles_by_interval["5m"])

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
            original_reclaim = store.reclaim_generations
            original_append = store.append_run_log

            with patch.object(store, "replace_current", side_effect=lambda *args, **kwargs: calls.append("current") or original_replace(*args, **kwargs)):
                with patch.object(store, "reclaim_generations", side_effect=lambda *args, **kwargs: calls.append("reclamation") or original_reclaim(*args, **kwargs)):
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

    def test_reclamation_skips_a_manifestless_in_progress_generation(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_plain_generation(data_dir, "run-old", b"old", mtime_ns=1_000_000_000)
            _write_plain_generation(data_dir, "run-current", b"current", mtime_ns=2_000_000_000)
            _write_plain_generation(
                data_dir,
                "run-in-progress",
                b"partial",
                mtime_ns=3_000_000_000,
                committed=False,
            )
            _write_current_pointer(data_dir, "run-current")

            report = TrustedDataStore(data_dir=data_dir).reclaim_generations(
                GenerationRetentionPolicy(keep_recent=1)
            )

            self.assertEqual(("run-old",), report.removed_ids)
            self.assertTrue((data_dir / "generations" / "run-in-progress").is_dir())

    def test_context_manifest_and_file_snapshot_share_one_store_lock(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_publication(
                data_dir,
                symbol=SYMBOL,
                start_ms=0,
                end_ms=DAY_MS - 300_000,
                run_id="run-current",
            )
            store = TrustedDataStore(data_dir=data_dir)
            active = {"value": False}
            original_read_manifest = store.read_manifest
            original_read_file_bytes = store.read_file_bytes

            @contextmanager
            def recording_lock():
                self.assertFalse(active["value"])
                active["value"] = True
                try:
                    yield
                finally:
                    active["value"] = False

            def read_manifest():
                self.assertTrue(active["value"])
                return original_read_manifest()

            def read_file_bytes(path):
                self.assertTrue(active["value"])
                return original_read_file_bytes(path)

            with patch.object(store, "publication_snapshot_lock", side_effect=recording_lock):
                with patch.object(store, "read_manifest", side_effect=read_manifest):
                    with patch.object(store, "read_file_bytes", side_effect=read_file_bytes):
                        context = LoadTrustedBundle(store).open_context(now_ms=DAY_MS)

            self.assertEqual("run-current", context.generation_id)
            self.assertFalse(active["value"])

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
            self.assertEqual(len(b"old bytes") + len(b"{}"), report.bytes_reclaimable)
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
        (root / "manifest.json").write_bytes(b"{}")
    os.utime(root, ns=(mtime_ns, mtime_ns))
    return root


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


def _hold_store_lock(data_dir: str, ready, release) -> None:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore

    store = TrustedDataStore(data_dir=Path(data_dir))
    with store.publication_snapshot_lock():
        ready.set()
        if not release.wait(10):
            raise TimeoutError("lock holder release timed out")


def _wait_for_store_lock(data_dir: str, attempting, acquired) -> None:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore

    store = TrustedDataStore(data_dir=Path(data_dir))
    attempting.set()
    with store.publication_snapshot_lock():
        acquired.set()


if __name__ == "__main__":
    unittest.main()
