import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle
from tests.factories.trusted_publication import (
    FixedClock,
    IncrementalFailureProvider as _IncrementalFailureProvider,
    RecordingProvider,
    SequenceClock,
    TextSink,
    constant_candles,
    manifest_path,
    range_candles,
    write_flat_manifest_and_caches,
    write_flat_v3_publication,
    write_generation_publication,
    write_orphan_flat_caches,
)


class TrustedDataPolicyTests(unittest.TestCase):
    def test_interval_dependency_planner_and_freshness_policy_contracts(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState
        from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy, IntervalDependencyPlanner

        planner = IntervalDependencyPlanner()
        cases = [
            (("5m",), ("5m",)),
            (("15m",), ("5m", "15m")),
            (("1h",), ("5m", "1h")),
            (("15m", "1h"), ("5m", "15m", "1h")),
            (("1h", "15m", "1h"), ("5m", "1h", "15m")),
        ]
        for requested, effective in cases:
            with self.subTest(requested=requested):
                plan = planner.plan(requested)
                self.assertEqual(tuple(dict.fromkeys(requested)), plan.requested_intervals)
                self.assertEqual(effective, plan.effective_intervals)

        policy = FreshnessPolicy(max_staleness_bars=2)
        self.assertEqual(FreshnessState.FRESH, policy.assess(now_ms=29 * 60_000, interval="15m", last_confirmed_open_time_ms=0).state)
        stale = policy.assess(now_ms=31 * 60_000, interval="15m", last_confirmed_open_time_ms=0)
        self.assertEqual(FreshnessState.STALE, stale.state)
        self.assertEqual("stale_by_clock", stale.reason.value)


class TrustedDataValidationTests(unittest.TestCase):
    def test_normalization_timestamp_gap_matrix_keeps_diagnostics_round_trip(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, ValidationReport
        from mu_strategy.market_data.trusted_data.validation import normalize_and_validate_candles

        cases = (("5m", (0, 600_000), 300_000), ("15m", (0, 1_800_000), 900_000), ("1h", (0, 7_200_000), 3_600_000))
        for interval, timestamps, expected_ms in cases:
            with self.subTest(interval=interval):
                ordered, report = normalize_and_validate_candles(constant_candles(timestamps), interval=interval)

                self.assertEqual(list(timestamps), [candle.open_time_ms for candle in ordered])
                self.assertFalse(report.ok)
                self.assertEqual(HealthReason.TIMESTAMP_GAP, report.reason)
                gap = report.timestamp_gaps[0]
                self.assertEqual(expected_ms, gap["expected_interval_ms"])
                self.assertEqual(1, gap["missing_count"])
                self.assertEqual((gap,), ValidationReport.from_dict(report.to_dict()).timestamp_gaps)

        ordered, report = normalize_and_validate_candles([*constant_candles((0, 300_000)), constant_candles((300_000,))[0]], interval="5m")
        self.assertTrue(report.ok)
        self.assertEqual([0, 300_000], [candle.open_time_ms for candle in ordered])

    def test_refresh_command_rejects_and_does_not_publish_holey_candles(self):
        from mu_strategy.commands.refresh_market_data import main

        def fetch_history(symbol: str, interval: str, *, days: int):
            self.assertEqual("5m", interval)
            return constant_candles((0, 600_000))

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            stock_config = Path(tmp) / "stock.json"
            stock_config.write_text("[]", encoding="utf-8")
            stdout = TextSink()
            with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers", return_value=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}]):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical", side_effect=fetch_history):
                    exit_code = main(
                        [
                            "--limit",
                            "1",
                            "--days",
                            "1",
                            "--interval",
                            "5m",
                            "--data-dir",
                            str(data_dir),
                            "--stock-token-config",
                            str(stock_config),
                            "--html-output",
                            str(Path(tmp) / "health.html"),
                        ],
                        stdout=stdout,
                    )
            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))

        self.assertNotEqual(0, exit_code)
        self.assertFalse(json.loads(stdout.text)["usable"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("timestamp_gap", manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["5m"]["reason"])
        self.assertFalse((data_dir / "okx" / "BTC-USDT-SWAP" / "5m.csv").exists())


class TrustedDataStoreLoadTests(unittest.TestCase):
    def test_load_missing_malformed_and_cache_only_paths_fail_closed(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy, trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            loader = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir))
            missing = loader.execute(LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1, now_ms=0), trading_strict_policy())
            self.assertFalse(missing.trust_decision.allowed)
            self.assertEqual(HealthReason.MANIFEST_MISSING, missing.trust_decision.reason)

            manifest_path(data_dir).write_text("{not-json", encoding="utf-8")
            malformed = loader.execute(LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1, now_ms=0), trading_strict_policy())
            self.assertFalse(malformed.trust_decision.allowed)
            self.assertEqual(HealthReason.MALFORMED_MANIFEST, malformed.trust_decision.reason)

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=2)
            store = TrustedDataStore(data_dir=data_dir)
            loader = LoadTrustedBundle(store)
            with patch("mu_strategy.market_data.providers.okx.fetch_okx_historical", side_effect=AssertionError("network")):
                with patch("mu_strategy.market_data.providers.okx.fetch_okx_incremental", side_effect=AssertionError("network")):
                    with patch.object(store, "write_csv", side_effect=AssertionError("write_csv")):
                        with patch.object(store, "write_manifest", side_effect=AssertionError("write_manifest")):
                            with patch.object(store, "append_run_log", side_effect=AssertionError("append_run_log")):
                                bundle = loader.execute(
                                    LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1, now_ms=86_400_000),
                                    observe_only_policy(),
                                )
            self.assertTrue(bundle.trust_decision.allowed)
            self.assertEqual(("5m", "15m", "1h"), tuple(bundle.health_by_interval))

    def test_requested_days_coverage_gate_accepts_only_complete_generation_windows(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason, IntegrityState
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.utils import DAY_MS

        cases = (
            ("short", 20 * DAY_MS, 20 * DAY_MS + DAY_MS - 300_000, False),
            ("complete", 20 * DAY_MS + 300_000, 20 * DAY_MS + 300_000 + 14 * DAY_MS, True),
        )
        for name, start_ms, end_ms, allowed in cases:
            with self.subTest(name=name):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    write_generation_publication(data_dir, symbol="MU-USDT-SWAP", start_ms=start_ms, end_ms=end_ms)
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=14, now_ms=end_ms),
                        trading_strict_policy(),
                    )

                self.assertEqual(allowed, bundle.trust_decision.allowed, bundle.trust_decision)
                if not allowed:
                    health = bundle.health_by_interval["5m"]
                    self.assertEqual(HealthReason.INSUFFICIENT_COVERAGE, health.primary_reason)
                    self.assertEqual(IntegrityState.INVALID, health.integrity)
                    self.assertEqual(FreshnessState.FRESH, health.freshness)
                    self.assertIn("requested_days=14", health.message)

    def test_strict_policy_rejects_manifest_run_and_dataset_states(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = [
            ("failed", {"outcome": "failed", "status": "ok", "run_id": "run-failed"}, HealthReason.RUN_FAILED),
            ("partial_invalid", {"outcome": "partial", "status": "invalid", "integrity": "invalid"}, HealthReason.MANIFEST_INVALID),
            ("success_invalid", {"outcome": "success", "status": "invalid", "integrity": "invalid"}, HealthReason.MANIFEST_INVALID),
            ("success_stale", {"outcome": "success", "status": "stale", "freshness": "stale"}, HealthReason.MALFORMED_MANIFEST),
        ]
        for name, kwargs, reason in cases:
            with self.subTest(name=name):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1, **kwargs)
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=FixedClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1),
                        trading_strict_policy(),
                    )
                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(reason, bundle.trust_decision.reason)

    def test_manifest_validation_and_local_validation_precedence_are_preserved(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest = write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1, outcome="failed", status="invalid")
            manifest["symbols"]["MU-USDT-SWAP"]["intervals"]["15m"].update(
                {
                    "integrity": "invalid",
                    "freshness": "stale",
                    "reasons": ["missing_in_native"],
                    "validation": {"ok": False, "reason": "missing_in_native", "missing_in_native": [900_000, 1_800_000]},
                }
            )
            manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=FixedClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                observe_only_policy(),
            )
            health = bundle.health_by_interval["15m"]
            self.assertEqual(HealthReason.MISSING_IN_NATIVE, health.primary_reason)
            self.assertEqual((900_000, 1_800_000), health.validation.missing_in_native)

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            store = TrustedDataStore(data_dir=data_dir)
            write_csv([Candle(60_000, 100.0, 101.0, 99.0, 100.0, 1.0)], store.flat_cache_path("MU-USDT-SWAP", "15m"))
            bundle = LoadTrustedBundle(store, clock=FixedClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                observe_only_policy(),
            )
            self.assertEqual(HealthReason.TIMESTAMP_MISALIGNED, bundle.health_by_interval["15m"].validation.reason)

    def test_canonical_csv_content_hash_binding_is_strict(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = ("hash_mismatch", "missing_hash")
        for case in cases:
            with self.subTest(case=case):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    manifest = write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
                    store = TrustedDataStore(data_dir=data_dir)
                    if case == "missing_hash":
                        manifest["symbols"]["MU-USDT-SWAP"]["intervals"]["5m"].pop("content_sha256")
                        store.write_manifest(manifest)
                    else:
                        original = store.read_csv(store.flat_cache_path("MU-USDT-SWAP", "5m"))
                        write_csv([Candle(c.open_time_ms, c.open + 10_000, c.high + 10_000, c.low + 10_000, c.close + 10_000, c.volume) for c in original], store.flat_cache_path("MU-USDT-SWAP", "5m"))
                    bundle = LoadTrustedBundle(store, clock=FixedClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("5m",), days=1),
                        trading_strict_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.CACHE_CONTENT_MISMATCH, bundle.health_by_interval["5m"].primary_reason)
                self.assertEqual([], bundle.candles_by_interval["5m"])

    def test_schema_v3_malformed_cases_fail_closed_without_self_generated_expectations(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        def first_dataset(manifest):
            return manifest["symbols"]["MU-USDT-SWAP"]["intervals"]["5m"]

        cases = {
            "missing_attempt_status": lambda m: m.pop("attempt_status"),
            "unknown_attempt_status": lambda m: m.__setitem__("attempt_status", "unknown"),
            "missing_snapshot_usability": lambda m: m.pop("snapshot_usability"),
            "unknown_snapshot_usability": lambda m: m.__setitem__("snapshot_usability", "missing"),
            "wrong_schema_version": lambda m: m.__setitem__("schema_version", 4),
            "missing_nested_availability": lambda m: first_dataset(m).pop("availability"),
            "unknown_nested_reason": lambda m: first_dataset(m).__setitem__("reasons", ["new_reason"]),
            "dataset_key_payload_mismatch": lambda m: first_dataset(m).__setitem__("symbol", "BTC-USDT-SWAP"),
            "requested_not_effective_subset": lambda m: m.__setitem__("effective_intervals", ["5m", "15m"]),
            "native_effective_without_5m": lambda m: m.__setitem__("effective_intervals", ["15m", "1h"]),
            "available_zero_rows": lambda m: first_dataset(m).__setitem__("rows", 0),
            "available_missing_timestamp_range": lambda m: first_dataset(m).pop("last_timestamp_ms"),
            "inverted_timestamp_range": lambda m: first_dataset(m).__setitem__("first_timestamp_ms", 99_999_999),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    manifest = write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
                    mutate(manifest)
                    manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=FixedClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                        observe_only_policy(),
                    )
                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_unpublished_and_missing_intervals_do_not_read_orphan_caches(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_manifest_and_caches(data_dir, symbol="BTC-USDT-SWAP", days=1, universe_symbols=("BTC-USDT-SWAP",))
            write_orphan_flat_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            store = TrustedDataStore(data_dir=data_dir)
            with patch.object(store, "read_csv", side_effect=AssertionError("orphan CSV must not be read")):
                bundle = LoadTrustedBundle(store, clock=FixedClock(86_400_000)).execute(
                    LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                    trading_strict_policy(),
                )
            self.assertEqual(HealthReason.NOT_PUBLISHED, bundle.trust_decision.reason)
            self.assertEqual([], bundle.candles_by_interval["15m"])

        for missing_interval, requested in [("5m", ("15m",)), ("15m", ("15m",)), ("1h", ("1h",))]:
            with self.subTest(missing_interval=missing_interval):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    manifest = write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
                    manifest["symbols"]["MU-USDT-SWAP"]["intervals"].pop(missing_interval)
                    manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=FixedClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=requested, days=1),
                        trading_strict_policy(),
                    )
                self.assertEqual(HealthReason.NOT_PUBLISHED, bundle.health_by_interval[missing_interval].primary_reason)

    def test_wall_clock_is_authoritative_and_read_once_per_load(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            clock = FixedClock(10 * 86_400_000)
            stale = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=clock).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                trading_strict_policy(),
            )
            fresh = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=clock).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1, now_ms=86_400_000),
                trading_strict_policy(),
            )
            sequence = SequenceClock(86_400_000)
            LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=sequence).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1),
                trading_strict_policy(),
            )
        self.assertFalse(stale.trust_decision.allowed)
        self.assertTrue(fresh.trust_decision.allowed)
        self.assertEqual(1, sequence.calls)


class TrustedDataRefreshTests(unittest.TestCase):
    def test_refresh_rejects_short_requested_coverage_for_all_effective_intervals_and_load_agrees(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason, IntegrityState, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles
        from mu_strategy.market_data.utils import DAY_MS

        end_ms = 20 * DAY_MS
        five = range_candles(end_ms - DAY_MS, end_ms)
        history = {"5m": five, "15m": aggregate_candles(five, interval="15m"), "1h": aggregate_candles(five, interval="1h")}
        provider = RecordingProvider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            history_fetcher=lambda symbol, interval, *, days: history[interval],
        )

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=14,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=end_ms,
                )
            )
            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            bundle = LoadTrustedBundle(store, clock=FixedClock(end_ms)).execute(
                LoadTrustedBundleQuery("BTC-USDT-SWAP", intervals=("15m", "1h"), days=14),
                trading_strict_policy(),
            )

        self.assertEqual(RefreshAttemptStatus.FAILED, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        for interval in ("5m", "15m", "1h"):
            with self.subTest(interval=interval):
                health = run.datasets[("BTC-USDT-SWAP", interval)]
                self.assertEqual(HealthReason.INSUFFICIENT_COVERAGE, health.primary_reason)
                self.assertEqual(IntegrityState.INVALID, health.integrity)
                self.assertEqual(FreshnessState.FRESH, health.freshness)
                self.assertIn("requested_days=14", health.message)
                self.assertIn("expected_start_ms=", health.message)
                self.assertIn("actual_start_ms=", health.message)
                persisted = manifest["symbols"]["BTC-USDT-SWAP"]["intervals"][interval]
                self.assertEqual("invalid", persisted["integrity"])
                self.assertEqual("insufficient_coverage", persisted["reason"])
                self.assertEqual("insufficient_coverage", bundle.health_by_interval[interval].primary_reason.value)
        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual([], bundle.candles_by_interval["15m"])
        self.assertEqual([], bundle.candles_by_interval["1h"])

    def test_refresh_accepts_requested_coverage_left_boundary_one_interval_tolerance(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles
        from mu_strategy.market_data.utils import DAY_MS, interval_to_ms

        end_ms = 14 * DAY_MS
        five = range_candles(interval_to_ms("5m"), end_ms)
        history = {"5m": five, "15m": aggregate_candles(five, interval="15m"), "1h": aggregate_candles(five, interval="1h")}
        provider = RecordingProvider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            history_fetcher=lambda symbol, interval, *, days: history[interval],
        )

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=14,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=end_ms,
                )
            )
            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))

        self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
        self.assertEqual("usable", manifest["snapshot_usability"])
        for interval in ("5m", "15m", "1h"):
            with self.subTest(interval=interval):
                health = run.datasets[("BTC-USDT-SWAP", interval)]
                self.assertEqual("ok", health.primary_reason.value)
                self.assertEqual(interval_to_ms(interval), health.first_timestamp_ms)

    def test_refresh_accepts_okx_history_when_confirmed_candles_lag_wall_clock(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import OKXMarketDataProvider, RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles
        from mu_strategy.market_data.utils import DAY_MS

        days = 14
        wall_end_ms = (days * DAY_MS) + (120 * 60_000)
        wall_start_ms = wall_end_ms - (days * DAY_MS)
        confirmed_lags_ms = {
            "5m": 15 * 60_000,
            "15m": 30 * 60_000,
            "1h": 120 * 60_000,
        }
        five = range_candles(0, wall_end_ms - confirmed_lags_ms["5m"])
        history = {
            "5m": five,
            "15m": aggregate_candles(five, interval="15m"),
            "1h": aggregate_candles(five, interval="1h"),
        }
        pages: dict[str, tuple[list[Candle], list[Candle]]] = {}
        for interval, rows in history.items():
            latest_confirmed_ms = wall_end_ms - confirmed_lags_ms[interval]
            required_start_ms = latest_confirmed_ms - (days * DAY_MS)
            interval_rows = [bar for bar in rows if bar.open_time_ms <= latest_confirmed_ms]
            pages[interval] = (
                [bar for bar in interval_rows if wall_start_ms <= bar.open_time_ms <= latest_confirmed_ms],
                [bar for bar in interval_rows if required_start_ms <= bar.open_time_ms < wall_start_ms],
            )
            self.assertTrue(pages[interval][0], interval)
            self.assertTrue(pages[interval][1], interval)
        requested_pages: list[tuple[str, int | None]] = []

        def fake_fetch(symbol, interval, *, after=None):
            self.assertEqual("MU-USDT-SWAP", symbol)
            requested_pages.append((interval, after))
            first_page, older_page = pages[interval]
            if after == wall_end_ms:
                return first_page
            if after == wall_start_ms:
                return older_page
            return []

        provider = OKXMarketDataProvider(
            ticker_rows=[{"instId": "MU-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
        )

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch("mu_strategy.market_data.providers.okx.time.time", return_value=wall_end_ms / 1000):
                with patch("mu_strategy.market_data.providers.okx.fetch_okx_candles", side_effect=fake_fetch):
                    run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), provider).execute(
                        RefreshTrustedMarketDataRequest(
                            requested_intervals=("15m", "1h"),
                            days=days,
                            limit=1,
                            stock_token_inst_ids=set(),
                            now_ms=wall_end_ms,
                        )
                    )
            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))

        self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
        self.assertEqual("usable", manifest["snapshot_usability"])
        self.assertNotIn(HealthReason.INSUFFICIENT_COVERAGE, [health.primary_reason for health in run.datasets.values()])
        for interval in ("5m", "15m", "1h"):
            with self.subTest(interval=interval):
                self.assertIn((interval, wall_end_ms), requested_pages)
                self.assertIn((interval, wall_start_ms), requested_pages)
                health = run.datasets[("MU-USDT-SWAP", interval)]
                self.assertEqual(HealthReason.OK, health.primary_reason)

    def test_refresh_records_effective_intervals_and_uses_incremental_after_history(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles
        from mu_strategy.market_data.utils import DAY_MS

        five = range_candles(0, DAY_MS)
        history = {"5m": five, "15m": aggregate_candles(five, interval="15m"), "1h": aggregate_candles(five, interval="1h")}
        provider = RecordingProvider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            history_fetcher=lambda symbol, interval, *, days: history[interval],
        )
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            request = RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=DAY_MS)
            first = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("15m", "1h"), days=1, limit=1, stock_token_inst_ids=set(), now_ms=DAY_MS)
            )
            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            RefreshTrustedMarketData(store, provider).execute(request)
            RefreshTrustedMarketData(store, provider).execute(request)

        self.assertEqual(RefreshAttemptStatus.SUCCESS, first.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, first.snapshot_usability)
        self.assertEqual(["15m", "1h"], manifest["requested_intervals"])
        self.assertEqual(["5m", "15m", "1h"], manifest["effective_intervals"])
        self.assertIn(("BTC-USDT-SWAP", "5m", 1), provider.history_calls)
        self.assertIn(("BTC-USDT-SWAP", "5m", DAY_MS - 300_000), provider.incremental_calls)

    def test_refresh_does_not_reuse_unusable_prior_health_as_incremental_base(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles
        from mu_strategy.market_data.utils import DAY_MS, interval_to_ms

        short_end_ms = 20 * DAY_MS
        short_five = range_candles(short_end_ms - DAY_MS, short_end_ms)
        short_history = {"5m": short_five, "15m": aggregate_candles(short_five, interval="15m"), "1h": aggregate_candles(short_five, interval="1h")}
        short_provider = RecordingProvider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            history_fetcher=lambda symbol, interval, *, days: short_history[interval],
        )
        full_five = range_candles(interval_to_ms("5m"), 14 * DAY_MS)
        full_history = {"5m": full_five, "15m": aggregate_candles(full_five, interval="15m"), "1h": aggregate_candles(full_five, interval="1h")}

        class FullHistoryProvider(RecordingProvider):
            def __init__(self):
                super().__init__(ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}])

            def fetch_history(self, symbol, interval, *, days):
                self.history_calls.append((symbol, interval, days))
                return full_history[interval]

            def fetch_incremental(self, symbol, interval, *, since_time_ms):
                self.incremental_calls.append((symbol, interval, since_time_ms))
                raise AssertionError("unusable prior health must not be reused incrementally")

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            first = RefreshTrustedMarketData(store, short_provider).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=14, limit=1, stock_token_inst_ids=set(), now_ms=short_end_ms)
            )
            provider = FullHistoryProvider()
            second = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=14, limit=1, stock_token_inst_ids=set(), now_ms=14 * DAY_MS)
            )

        self.assertEqual(RefreshAttemptStatus.FAILED, first.attempt_status)
        self.assertEqual(HealthReason.INSUFFICIENT_COVERAGE, first.datasets[("BTC-USDT-SWAP", "5m")].primary_reason)
        self.assertEqual(RefreshAttemptStatus.SUCCESS, second.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, second.snapshot_usability)
        self.assertEqual([], provider.incremental_calls)
        self.assertIn(("BTC-USDT-SWAP", "5m", 14), provider.history_calls)

    def test_refresh_falls_back_to_history_when_requested_retention_expands(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles
        from mu_strategy.market_data.utils import DAY_MS, interval_to_ms

        one_day_end_ms = 20 * DAY_MS
        one_day_five = range_candles(one_day_end_ms - DAY_MS, one_day_end_ms)
        one_day_history = {
            "5m": one_day_five,
            "15m": aggregate_candles(one_day_five, interval="15m"),
            "1h": aggregate_candles(one_day_five, interval="1h"),
        }
        full_end_ms = 34 * DAY_MS
        full_five = range_candles(full_end_ms - (14 * DAY_MS) + interval_to_ms("5m"), full_end_ms)
        full_history = {
            "5m": full_five,
            "15m": aggregate_candles(full_five, interval="15m"),
            "1h": aggregate_candles(full_five, interval="1h"),
        }
        first_provider = RecordingProvider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            history_fetcher=lambda symbol, interval, *, days: one_day_history[interval],
        )

        class FullHistoryProvider(RecordingProvider):
            def __init__(self):
                super().__init__(ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}])

            def fetch_history(self, symbol, interval, *, days):
                self.history_calls.append((symbol, interval, days))
                return full_history[interval]

            def fetch_incremental(self, symbol, interval, *, since_time_ms):
                self.incremental_calls.append((symbol, interval, since_time_ms))
                raise AssertionError("expanded retention must fetch full history")

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            first = RefreshTrustedMarketData(store, first_provider).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=one_day_end_ms)
            )
            provider = FullHistoryProvider()
            second = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=14, limit=1, stock_token_inst_ids=set(), now_ms=full_end_ms)
            )

        self.assertEqual(RefreshAttemptStatus.SUCCESS, first.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, first.snapshot_usability)
        self.assertEqual(RefreshAttemptStatus.SUCCESS, second.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, second.snapshot_usability)
        self.assertEqual([], provider.incremental_calls)
        self.assertIn(("BTC-USDT-SWAP", "5m", 14), provider.history_calls)

    def test_refresh_attempt_and_snapshot_axes_remain_separate(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.evaluate import classify_publication_health
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            write_flat_v3_publication(Path(tmp), symbol="BTC-USDT-SWAP")
            degraded_fresh = RefreshTrustedMarketData(store, _IncrementalFailureProvider()).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
            )

        provider = RecordingProvider(
            ticker_rows=[
                {"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"},
                {"instId": "ETH-USDT-SWAP", "last": "90", "volCcy24h": "9"},
            ],
            fail_history={("BTC-USDT-SWAP", "5m"), ("ETH-USDT-SWAP", "5m")},
        )
        with TemporaryDirectory() as tmp:
            all_invalid = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), provider).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=1, limit=2, stock_token_inst_ids=set(), now_ms=3_600_000)
            )

        self.assertEqual(RefreshAttemptStatus.DEGRADED, degraded_fresh.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, degraded_fresh.snapshot_usability)
        self.assertEqual(RefreshAttemptStatus.FAILED, all_invalid.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, all_invalid.snapshot_usability)
        summary = classify_publication_health(all_invalid.datasets, provider_failures=all_invalid.provider_failures)
        self.assertTrue(summary.zero_usable)
        self.assertEqual(0, summary.usable_count)
        self.assertEqual(len(all_invalid.datasets), summary.unusable_count)

    def test_refresh_validation_only_mixed_usable_publication_is_degraded(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.evaluate import classify_publication_health
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.utils import DAY_MS

        def fetch_history(symbol: str, interval: str, *, days: int):
            if symbol == "ETH-USDT-SWAP":
                return [Candle(0, 100.0, 99.0, 101.0, 100.0, 1.0)]
            return range_candles(0, DAY_MS)

        provider = RecordingProvider(
            ticker_rows=[
                {"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"},
                {"instId": "ETH-USDT-SWAP", "last": "90", "volCcy24h": "9"},
            ],
            history_fetcher=fetch_history,
        )
        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), provider).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=1, limit=2, stock_token_inst_ids=set(), now_ms=DAY_MS)
            )

        self.assertEqual(RefreshAttemptStatus.DEGRADED, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        self.assertTrue(run.datasets[("BTC-USDT-SWAP", "5m")].is_usable)
        self.assertEqual(HealthReason.OHLCV_INVALID, run.datasets[("ETH-USDT-SWAP", "5m")].primary_reason)
        summary = classify_publication_health(run.datasets, provider_failures=run.provider_failures)
        self.assertTrue(summary.partial_usable)
        self.assertEqual(1, summary.usable_count)
        self.assertEqual(1, summary.unusable_count)
        self.assertEqual(1, summary.validation_failure_count)

    def test_refresh_covering_prior_preserves_incremental_warning(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason, IntegrityState, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles
        from mu_strategy.market_data.utils import DAY_MS

        end_ms = 20 * DAY_MS
        five = range_candles(end_ms - DAY_MS, end_ms)
        candles_by_interval = {"5m": five, "15m": aggregate_candles(five, interval="15m"), "1h": aggregate_candles(five, interval="1h")}
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_publication(data_dir, symbol="BTC-USDT-SWAP", candles_by_interval=candles_by_interval)
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), _IncrementalFailureProvider()).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=end_ms)
            )

        health = run.datasets[("BTC-USDT-SWAP", "5m")]
        self.assertEqual(RefreshAttemptStatus.DEGRADED, run.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
        self.assertEqual(HealthReason.OK, health.primary_reason)
        self.assertEqual(IntegrityState.VALID, health.integrity)
        self.assertEqual(FreshnessState.FRESH, health.freshness)
        self.assertIn("incremental_refresh_failed", health.warnings)

    def test_limit_zero_and_cache_read_failure_do_not_escape_canonical_publication(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch("mu_strategy.market_data.trusted_data.refresh.load_stock_token_inst_ids", side_effect=AssertionError("stock config")) as load_config:
                run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), RecordingProvider()).execute(
                    RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=1, limit=0, stock_token_config=data_dir / "missing.json")
                )
            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
        load_config.assert_not_called()
        self.assertEqual(RefreshAttemptStatus.FAILED, run.attempt_status)
        self.assertEqual({"crypto_top": [], "stock_token_top": []}, manifest["universes"])
        self.assertEqual({}, manifest["symbols"])

        class FailingReadStore(TrustedDataStore):
            def read_csv(self, path):
                raise OSError("disk offline")

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_v3_publication(data_dir, symbol="BTC-USDT-SWAP")
            run = RefreshTrustedMarketData(FailingReadStore(data_dir=data_dir), RecordingProvider(ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}])).execute(
                RefreshTrustedMarketDataRequest(requested_intervals=("5m",), days=1, limit=1, stock_token_inst_ids=set(), now_ms=86_400_000)
            )
        self.assertEqual(RefreshAttemptStatus.FAILED, run.attempt_status)
        self.assertEqual("cache_read_failed", run.datasets[("BTC-USDT-SWAP", "5m")].primary_reason.value)


class TrustedDemoConsumerTests(unittest.TestCase):
    def test_demo_uses_trusted_manifest_universe_and_blocks_invalid_data_before_scanner(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_manifest_and_caches(data_dir, symbol="BTC-USDT-SWAP", days=1, universe_symbols=("BTC-USDT-SWAP",), stock_token_symbols=("MU-USDT-SWAP",))
            write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1, universe_symbols=("BTC-USDT-SWAP",), stock_token_symbols=("MU-USDT-SWAP",))
            scanned = []
            with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                result = run_once(
                    DemoTradingConfig(universe_limit=1, dry_run=True, data_dir=data_dir, days=1, watchlist_symbols=()),
                    broker=None,
                    scanner=lambda symbol, candles_15m, candles_1h, **kwargs: scanned.append(symbol) or _wait(symbol),
                )
        self.assertEqual(["BTC-USDT-SWAP", "MU-USDT-SWAP"], [item["inst_id"] for item in result["universe"]])
        self.assertEqual(["BTC-USDT-SWAP", "MU-USDT-SWAP"], scanned)

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1, universe_symbols=("MU-USDT-SWAP",), integrity="invalid", status="invalid")
            result = run_once(
                DemoTradingConfig(universe_limit=1, dry_run=True, data_dir=data_dir, days=1, watchlist_symbols=()),
                broker=None,
                scanner=lambda *args, **kwargs: self.fail("invalid trusted data must not be scanned"),
            )
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])

    def test_consumer_paths_reject_refresh_and_do_not_call_manifest_writers(self):
        import mu_strategy.demo_trading as demo_trading
        from mu_strategy.demo_trading import DemoTradingConfig, run_once
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_flat_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1, universe_symbols=("MU-USDT-SWAP",))
            manifest_before = manifest_path(data_dir).read_bytes()
            with patch.object(TrustedDataStore, "write_manifest", side_effect=AssertionError("writer")):
                with patch.object(TrustedDataStore, "write_csv", side_effect=AssertionError("writer")):
                    with patch.object(TrustedDataStore, "append_run_log", side_effect=AssertionError("writer")):
                        result = run_once(DemoTradingConfig(universe_limit=1, dry_run=True, data_dir=data_dir, days=1, watchlist_symbols=()), broker=None, scanner=lambda symbol, *_args, **_kwargs: _wait(symbol))
            self.assertEqual("dry_run", result["mode"])
            self.assertEqual(manifest_before, manifest_path(data_dir).read_bytes())

            class Broker:
                def get_positions(self, **kwargs):
                    return {"code": "0", "data": []}

                def get_open_orders(self, **kwargs):
                    return {"code": "0", "data": []}

            with self.assertRaisesRegex(demo_trading.TrustedConsumerRefreshError, "refresh_market_data"):
                run_once(DemoTradingConfig(refresh=True, dry_run=False, data_dir=data_dir), broker=Broker(), scanner=lambda *args, **kwargs: self.fail("scanner"))

    def test_consumer_modules_keep_refresh_and_freshness_logic_out(self):
        forbidden = {
            "mu_strategy/cli.py": ("RefreshTrustedMarketData(", "write_manifest(", "append_run_log("),
            "mu_strategy/viz/backtest.py": ("RefreshTrustedMarketData(", "write_manifest(", "append_run_log("),
            "mu_strategy/demo_trading.py": ("RefreshTrustedMarketData(", "write_manifest(", "append_run_log("),
        }
        for relative_path, needles in forbidden.items():
            source = Path(relative_path).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(path=relative_path, needle=needle):
                    self.assertNotIn(needle, source)


def _wait(symbol):
    from mu_strategy.entry.scanner import EntryScanResult

    return EntryScanResult(
        symbol=symbol,
        action="wait",
        reason="wait",
        last_close=100,
        regime_1h="yellow",
        rsi14=None,
        macd_hist=None,
        macd_hist_prev=None,
    )


if __name__ == "__main__":
    unittest.main()
