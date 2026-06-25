import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mu_strategy.models import Candle


class TrustedDataPolicyTests(unittest.TestCase):
    def test_interval_dependency_planner_adds_5m_for_native_intervals(self):
        from mu_strategy.market_data.trusted_data.policy import IntervalDependencyPlanner

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

    def test_freshness_policy_uses_clock_interval_and_confirmed_candle(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState
        from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy

        policy = FreshnessPolicy(max_staleness_bars=2)

        fresh = policy.assess(now_ms=29 * 60_000, interval="15m", last_confirmed_open_time_ms=0)
        stale = policy.assess(now_ms=31 * 60_000, interval="15m", last_confirmed_open_time_ms=0)

        self.assertEqual(FreshnessState.FRESH, fresh.state)
        self.assertEqual(FreshnessState.STALE, stale.state)
        self.assertEqual("stale_by_clock", stale.reason.value)


class TrustedDataValidationTests(unittest.TestCase):
    def test_normalization_rejects_missing_five_minute_candle_with_diagnostics(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, ValidationReport
        from mu_strategy.market_data.trusted_data.validation import normalize_and_validate_candles

        candles = _constant_candles((0, 600_000))

        ordered, report = normalize_and_validate_candles(candles, interval="5m")

        expected_gap = {
            "previous_timestamp_ms": 0,
            "current_timestamp_ms": 600_000,
            "expected_interval_ms": 300_000,
            "actual_interval_ms": 600_000,
            "missing_count": 1,
        }
        self.assertEqual([0, 600_000], [candle.open_time_ms for candle in ordered])
        self.assertFalse(report.ok)
        self.assertEqual(HealthReason.TIMESTAMP_GAP, report.reason)
        self.assertEqual((expected_gap,), getattr(report, "timestamp_gaps", ()))
        payload = report.to_dict()
        self.assertEqual([expected_gap], payload.get("timestamp_gaps"))
        self.assertEqual((expected_gap,), getattr(ValidationReport.from_dict(payload), "timestamp_gaps", ()))

    def test_normalization_rejects_missing_fifteen_minute_and_one_hour_candles(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.validation import normalize_and_validate_candles

        cases = (
            ("15m", (0, 1_800_000), 900_000),
            ("1h", (0, 7_200_000), 3_600_000),
        )
        for interval, timestamps, expected_ms in cases:
            with self.subTest(interval=interval):
                _, report = normalize_and_validate_candles(_constant_candles(timestamps), interval=interval)

                self.assertFalse(report.ok)
                self.assertEqual(HealthReason.TIMESTAMP_GAP, report.reason)
                self.assertEqual(expected_ms, getattr(report, "timestamp_gaps", ({},))[0].get("expected_interval_ms"))
                self.assertEqual(1, getattr(report, "timestamp_gaps", ({},))[0].get("missing_count"))

    def test_normalization_allows_continuous_and_deduped_candles(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.validation import normalize_and_validate_candles

        candles = [*_constant_candles((0, 300_000)), _constant_candles((300_000,))[0]]

        ordered, report = normalize_and_validate_candles(candles, interval="5m")

        self.assertTrue(report.ok)
        self.assertEqual(HealthReason.OK, report.reason)
        self.assertEqual([0, 300_000], [candle.open_time_ms for candle in ordered])

    def test_matching_holes_are_blocked_by_single_interval_normalization(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        def fetch_history(symbol: str, interval: str, *, days: int):
            if interval == "5m":
                return _constant_candles((0, 300_000, 600_000, 1_800_000, 2_100_000, 2_400_000))
            if interval == "15m":
                return [Candle(0, 100.0, 101.0, 99.0, 100.0, 30.0), Candle(1_800_000, 100.0, 101.0, 99.0, 100.0, 30.0)]
            raise AssertionError(interval)

        provider = _Provider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            history_fetcher=fetch_history,
        )
        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )

        self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        base_health = run.datasets[("BTC-USDT-SWAP", "5m")]
        native_health = run.datasets[("BTC-USDT-SWAP", "15m")]
        self.assertEqual(HealthReason.TIMESTAMP_GAP, base_health.primary_reason)
        self.assertEqual(HealthReason.TIMESTAMP_GAP, base_health.validation.reason)
        self.assertEqual(HealthReason.TIMESTAMP_GAP, native_health.primary_reason)

    def test_refresh_command_rejects_and_does_not_publish_holey_candles(self):
        from mu_strategy.commands.refresh_market_data import main

        def fetch_history(symbol: str, interval: str, *, days: int):
            self.assertEqual("5m", interval)
            return _constant_candles((0, 600_000))

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            stock_config = Path(tmp) / "stock.json"
            stock_config.write_text("[]", encoding="utf-8")
            stdout = _TextSink()
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers",
                return_value=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            ):
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
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            csv_path = data_dir / "okx" / "BTC-USDT-SWAP" / "5m.csv"
            command_result = json.loads(stdout.text)

        self.assertNotEqual(0, exit_code)
        self.assertFalse(command_result["usable"])
        self.assertEqual("success", command_result["attempt_status"])
        self.assertEqual("invalid", command_result["snapshot_usability"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("timestamp_gap", manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["5m"]["reason"])
        self.assertFalse(csv_path.exists())


class TrustedDataStoreLoadTests(unittest.TestCase):
    def test_load_missing_and_malformed_manifest_fail_closed_with_distinct_reasons(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            loader = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir))

            missing = loader.execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1, now_ms=0),
                trading_strict_policy(),
            )
            self.assertFalse(missing.trust_decision.allowed)
            self.assertEqual(HealthReason.MANIFEST_MISSING, missing.trust_decision.reason)

            _manifest_path(data_dir).write_text("{not-json", encoding="utf-8")
            malformed = loader.execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1, now_ms=0),
                trading_strict_policy(),
            )
            self.assertFalse(malformed.trust_decision.allowed)
            self.assertEqual(HealthReason.MALFORMED_MANIFEST, malformed.trust_decision.reason)

    def test_load_path_never_calls_network_or_writes(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=2)
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

        self.assertEqual(("5m", "15m", "1h"), tuple(bundle.health_by_interval))
        self.assertTrue(bundle.trust_decision.allowed)
        self.assertLess(len(bundle.candles_by_interval["15m"]), 192)

    def test_strict_policies_reject_failed_manifest_even_when_csv_is_valid(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import research_strict_policy, trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                outcome="failed",
                status="ok",
                run_id="run-failed",
            )
            loader = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000))

            for policy in (trading_strict_policy(), research_strict_policy()):
                with self.subTest(policy=policy.name):
                    bundle = loader.execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1),
                        policy,
                    )

                    self.assertFalse(bundle.trust_decision.allowed)
                    self.assertEqual(HealthReason.RUN_FAILED, bundle.trust_decision.reason)
                    self.assertEqual("run-failed", bundle.run_id)

    def test_strict_policy_rejects_degraded_invalid_manifest_even_when_requested_symbol_is_valid(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                outcome="partial",
                status="invalid",
                integrity="invalid",
                run_id="run-partial",
            )
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1),
                trading_strict_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.MANIFEST_INVALID, bundle.trust_decision.reason)

    def test_strict_policy_rejects_success_manifest_with_invalid_or_stale_status(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = [
            ("invalid", HealthReason.MANIFEST_INVALID, {"integrity": "invalid"}),
            ("stale", HealthReason.MANIFEST_STALE, {"freshness": "stale"}),
        ]
        for status, reason, health_kwargs in cases:
            with self.subTest(status=status):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    _write_manifest_and_caches(
                        data_dir,
                        symbol="MU-USDT-SWAP",
                        days=1,
                        outcome="success",
                        status=status,
                        **health_kwargs,
                    )
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1),
                        trading_strict_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(reason, bundle.trust_decision.reason)

    def test_observe_only_failed_manifest_preserves_real_manifest_context(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                outcome="failed",
                status="ok",
                run_id="run-diagnostic",
            )
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                observe_only_policy(),
            )

        self.assertTrue(bundle.trust_decision.allowed)
        self.assertEqual("run-diagnostic", bundle.run_id)
        self.assertIsNotNone(bundle.load_context)
        self.assertEqual(RefreshAttemptStatus.FAILED, bundle.load_context.manifest.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, bundle.load_context.manifest.snapshot_usability)

    def test_manifest_failed_ohlcv_validation_report_survives_local_valid_cache(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy, trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        mismatch = {"timestamp_ms": 0, "field": "high", "built": 101.0, "native": 102.0}
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest = _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                outcome="failed",
                status="invalid",
                run_id="run-validation-failed",
            )
            status = manifest["symbols"]["MU-USDT-SWAP"]["intervals"]["15m"]
            status.update(
                {
                    "integrity": "invalid",
                    "freshness": "stale",
                    "reasons": ["ohlcv_mismatch"],
                    "validation": {
                        "ok": False,
                        "reason": "ohlcv_mismatch",
                        "value_mismatches": [mismatch],
                    },
                }
            )
            _manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")
            loader = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000))

            strict = loader.execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                trading_strict_policy(),
            )
            observed = loader.execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                observe_only_policy(),
            )

        self.assertFalse(strict.trust_decision.allowed)
        self.assertEqual(HealthReason.RUN_FAILED, strict.trust_decision.reason)
        health = observed.health_by_interval["15m"]
        payload = health.to_dict()
        self.assertTrue(observed.trust_decision.allowed)
        self.assertEqual(HealthReason.OHLCV_MISMATCH, health.primary_reason)
        self.assertFalse(health.validation.ok)
        self.assertEqual(HealthReason.OHLCV_MISMATCH, health.validation.reason)
        self.assertEqual((mismatch,), health.validation.value_mismatches)
        self.assertEqual("ohlcv_mismatch", payload["reason"])
        self.assertFalse(payload["validation"]["ok"])
        self.assertEqual("ohlcv_mismatch", payload["validation"]["reason"])
        self.assertEqual([mismatch], payload["validation"]["value_mismatches"])

    def test_manifest_failed_missing_native_report_survives_local_valid_cache(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest = _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                outcome="failed",
                status="invalid",
            )
            status = manifest["symbols"]["MU-USDT-SWAP"]["intervals"]["15m"]
            status.update(
                {
                    "integrity": "invalid",
                    "freshness": "stale",
                    "reasons": ["missing_in_native"],
                    "validation": {
                        "ok": False,
                        "reason": "missing_in_native",
                        "missing_in_native": [900_000, 1_800_000],
                    },
                }
            )
            _manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                observe_only_policy(),
            )

        health = bundle.health_by_interval["15m"]
        self.assertEqual(HealthReason.MISSING_IN_NATIVE, health.primary_reason)
        self.assertFalse(health.validation.ok)
        self.assertEqual(HealthReason.MISSING_IN_NATIVE, health.validation.reason)
        self.assertEqual((900_000, 1_800_000), health.validation.missing_in_native)

    def test_local_failed_validation_overrides_successful_manifest_report(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            store = TrustedDataStore(data_dir=data_dir)
            write_csv([Candle(60_000, 100.0, 101.0, 99.0, 100.0, 1.0)], store.cache_path("MU-USDT-SWAP", "15m"))

            bundle = LoadTrustedBundle(store, clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                observe_only_policy(),
            )

        health = bundle.health_by_interval["15m"]
        self.assertEqual(HealthReason.TIMESTAMP_MISALIGNED, health.primary_reason)
        self.assertFalse(health.validation.ok)
        self.assertEqual(HealthReason.TIMESTAMP_MISALIGNED, health.validation.reason)
        self.assertEqual((60_000,), health.validation.misaligned_timestamps)

    def test_local_valid_validation_overrides_successful_manifest_report(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest = _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            manifest_status = manifest["symbols"]["MU-USDT-SWAP"]["intervals"]["15m"]
            manifest_status["validation"] = {"ok": True, "reason": "ok", "warnings": ["manifest-only"]}
            _manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")

            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                observe_only_policy(),
            )

        health = bundle.health_by_interval["15m"]
        self.assertEqual(HealthReason.OK, health.primary_reason)
        self.assertTrue(health.validation.ok)
        self.assertEqual(HealthReason.OK, health.validation.reason)
        self.assertEqual((), health.validation.warnings)

    def test_load_path_blocks_manifest_csv_with_internal_timestamp_gap(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            store = TrustedDataStore(data_dir=data_dir)
            write_csv(_constant_candles((0, 1_800_000)), store.cache_path("MU-USDT-SWAP", "15m"))

            bundle = LoadTrustedBundle(store, clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                trading_strict_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.TIMESTAMP_GAP, bundle.health_by_interval["15m"].primary_reason)
        self.assertFalse(bundle.health_by_interval["15m"].validation.ok)
        self.assertEqual(HealthReason.TIMESTAMP_GAP, bundle.health_by_interval["15m"].validation.reason)

    def test_strict_load_rejects_csv_content_not_bound_to_manifest(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            store = TrustedDataStore(data_dir=data_dir)
            original = store.read_csv(store.cache_path("MU-USDT-SWAP", "5m"))
            replacement = [
                Candle(
                    candle.open_time_ms,
                    candle.open + 10_000,
                    candle.high + 10_000,
                    candle.low + 10_000,
                    candle.close + 10_000,
                    candle.volume,
                )
                for candle in original
            ]
            write_csv(replacement, store.cache_path("MU-USDT-SWAP", "5m"))

            bundle = LoadTrustedBundle(store, clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("5m",), days=1),
                trading_strict_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.CACHE_CONTENT_MISMATCH, bundle.trust_decision.reason)
        self.assertEqual(HealthReason.CACHE_CONTENT_MISMATCH, bundle.health_by_interval["5m"].primary_reason)
        self.assertEqual([], bundle.candles_by_interval["5m"])

    def test_strict_load_rejects_valid_manifest_without_content_hash(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest = _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            manifest["symbols"]["MU-USDT-SWAP"]["intervals"]["5m"].pop("content_sha256")
            store = TrustedDataStore(data_dir=data_dir)
            store.write_manifest(manifest)

            bundle = LoadTrustedBundle(store, clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("5m",), days=1),
                trading_strict_policy(),
            )

        health = bundle.health_by_interval["5m"]
        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.CACHE_CONTENT_MISMATCH, bundle.trust_decision.reason)
        self.assertEqual(HealthReason.CACHE_CONTENT_MISMATCH, health.primary_reason)
        self.assertEqual("manifest dataset is missing content_sha256", health.message)
        self.assertEqual([], bundle.candles_by_interval["5m"])

    def test_malformed_schema_v2_fail_closed(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        def first_dataset(manifest):
            return manifest["symbols"]["MU-USDT-SWAP"]["intervals"]["5m"]

        cases = {
            "missing_attempt_status": lambda manifest: manifest.pop("attempt_status"),
            "unknown_attempt_status": lambda manifest: manifest.__setitem__("attempt_status", "unknown"),
            "missing_snapshot_usability": lambda manifest: manifest.pop("snapshot_usability"),
            "unknown_snapshot_usability": lambda manifest: manifest.__setitem__("snapshot_usability", "missing"),
            "wrong_schema_version": lambda manifest: manifest.__setitem__("schema_version", 2),
            "missing_nested_availability": lambda manifest: first_dataset(manifest).pop("availability"),
            "unknown_nested_reason": lambda manifest: first_dataset(manifest).__setitem__("reasons", ["new_reason"]),
            "dataset_key_payload_mismatch": lambda manifest: first_dataset(manifest).__setitem__("symbol", "BTC-USDT-SWAP"),
            "requested_not_effective_subset": lambda manifest: manifest.__setitem__("effective_intervals", ["5m", "15m"]),
            "native_effective_without_5m": lambda manifest: manifest.__setitem__("effective_intervals", ["15m", "1h"]),
            "success_ok_catalog_incomplete": lambda manifest: (
                manifest.__setitem__(
                    "universes",
                    {"crypto_top": [{"inst_id": "MU-USDT-SWAP", "last": 100, "volume_ccy_24h": 10, "source": "top"}], "stock_token_top": []},
                ),
                manifest["symbols"]["MU-USDT-SWAP"]["intervals"].pop("1h"),
            ),
            "available_zero_rows": lambda manifest: first_dataset(manifest).__setitem__("rows", 0),
            "available_missing_timestamp_range": lambda manifest: first_dataset(manifest).pop("last_timestamp_ms"),
            "inverted_timestamp_range": lambda manifest: first_dataset(manifest).__setitem__("first_timestamp_ms", 99_999_999),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    manifest = _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
                    mutate(manifest)
                    _manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")

                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                        observe_only_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_strict_load_rejects_unpublished_symbol_without_reading_orphan_csv(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="BTC-USDT-SWAP",
                days=1,
                universe_symbols=("BTC-USDT-SWAP",),
            )
            _write_orphan_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            store = TrustedDataStore(data_dir=data_dir)
            loader = LoadTrustedBundle(store, clock=_FakeClock(86_400_000))

            with patch.object(store, "read_csv", side_effect=AssertionError("orphan CSV must not be read")):
                bundle = loader.execute(
                    LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                    trading_strict_policy(),
                )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.NOT_PUBLISHED, bundle.trust_decision.reason)
        self.assertEqual(HealthReason.NOT_PUBLISHED, bundle.health_by_interval["5m"].primary_reason)
        self.assertEqual(HealthReason.NOT_PUBLISHED, bundle.health_by_interval["15m"].primary_reason)
        self.assertEqual([], bundle.candles_by_interval["15m"])

    def test_missing_effective_interval_is_publication_missing(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = [
            ("5m", ("15m",)),
            ("15m", ("15m",)),
            ("1h", ("1h",)),
        ]
        for missing_interval, requested in cases:
            with self.subTest(missing_interval=missing_interval):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    manifest = _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
                    manifest["symbols"]["MU-USDT-SWAP"]["intervals"].pop(missing_interval)
                    _manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")

                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=requested, days=1),
                        trading_strict_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.NOT_PUBLISHED, bundle.health_by_interval[missing_interval].primary_reason)
                self.assertEqual([], bundle.candles_by_interval.get(missing_interval, []))

    def test_observe_only_unpublished_dataset_keeps_context_without_orphan_candles(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="BTC-USDT-SWAP", days=1, run_id="run-catalog")
            _write_orphan_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            store = TrustedDataStore(data_dir=data_dir)

            with patch.object(store, "read_csv", side_effect=AssertionError("orphan CSV must not be read")):
                bundle = LoadTrustedBundle(store, clock=_FakeClock(86_400_000)).execute(
                    LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                    observe_only_policy(),
                )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual("run-catalog", bundle.run_id)
        self.assertEqual(HealthReason.NOT_PUBLISHED, bundle.health_by_interval["15m"].primary_reason)
        self.assertEqual([], bundle.candles_by_interval["15m"])

    def test_published_dataset_with_missing_file_reports_cache_missing_not_not_published(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            (data_dir / "okx" / "MU-USDT-SWAP" / "15m.csv").unlink()

            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                trading_strict_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.CACHE_MISSING, bundle.health_by_interval["15m"].primary_reason)
        self.assertNotEqual(HealthReason.NOT_PUBLISHED, bundle.health_by_interval["15m"].primary_reason)

    def test_default_wall_clock_marks_old_cache_stale(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            bundle = LoadTrustedBundle(
                TrustedDataStore(data_dir=data_dir),
                clock=_FakeClock(10 * 86_400_000),
            ).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                trading_strict_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.STALE_BY_CLOCK, bundle.trust_decision.reason)
        self.assertEqual(FreshnessState.STALE, bundle.health_by_interval["5m"].freshness)

    def test_default_wall_clock_can_mark_cache_fresh(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                trading_strict_policy(),
            )

        self.assertTrue(bundle.trust_decision.allowed)

    def test_query_now_ms_overrides_injected_clock(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        clock = _FakeClock(10 * 86_400_000)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=clock).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1, now_ms=86_400_000),
                trading_strict_policy(),
            )

        self.assertTrue(bundle.trust_decision.allowed)
        self.assertEqual(0, clock.calls)
        self.assertEqual(86_400_000, bundle.load_context.observed_at_ms)

    def test_clock_is_read_once_per_execute_for_multi_interval_bundle(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        clock = _FakeClock(86_400_000)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=clock).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1),
                trading_strict_policy(),
            )

        self.assertTrue(bundle.trust_decision.allowed)
        self.assertEqual(1, clock.calls)
        self.assertEqual(86_400_000, bundle.load_context.observed_at_ms)

    def test_refresh_trusted_bundle_facade_uses_wall_clock_by_default(self):
        from mu_strategy.market_data.service import refresh_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import HealthReason

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
            bundle = refresh_trusted_candle_bundle(
                "MU-USDT-SWAP",
                intervals=("15m", "1h"),
                days=1,
                data_dir=data_dir,
                refresh=False,
                clock=_FakeClock(10 * 86_400_000),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.STALE_BY_CLOCK, bundle.trust_decision.reason)

    def test_compatibility_facades_do_not_import_okx_fetchers(self):
        import mu_strategy.market_data.service as service
        import mu_strategy.market_data.trusted as trusted

        self.assertFalse(hasattr(trusted, "fetch_okx_historical"))
        self.assertFalse(hasattr(trusted, "fetch_okx_incremental"))
        self.assertFalse(hasattr(service, "fetch_okx_historical"))
        self.assertFalse(hasattr(service, "fetch_okx_incremental"))

    def test_atomic_write_failure_does_not_leave_partial_target(self):
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            target = _manifest_path(data_dir)

            with patch("mu_strategy.market_data.trusted_data.store.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.write_manifest({"schema_version": 3, "snapshot_usability": "usable"})

            self.assertFalse(target.exists())


class TrustedDataRefreshTests(unittest.TestCase):
    def test_refresh_records_requested_and_effective_intervals(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _Provider(ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}])
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))

        self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
        self.assertEqual(["15m", "1h"], manifest["requested_intervals"])
        self.assertEqual(["5m", "15m", "1h"], manifest["effective_intervals"])

    def test_refresh_uses_history_then_incremental_provider(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _Provider(ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}])
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            use_case = RefreshTrustedMarketData(store, provider)
            request = RefreshTrustedMarketDataRequest(
                requested_intervals=("5m",),
                days=1,
                limit=1,
                stock_token_inst_ids=set(),
                now_ms=3_600_000,
            )

            use_case.execute(request)
            use_case.execute(request)

        self.assertEqual([("BTC-USDT-SWAP", "5m", 1)], provider.history_calls)
        self.assertEqual([("BTC-USDT-SWAP", "5m", 3_000_000)], provider.incremental_calls)

    def test_refresh_freshness_uses_current_clock_after_long_fetch(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _Provider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            history_fetcher=lambda symbol, interval, *, days: [
                Candle(0, 100.0, 101.0, 99.0, 100.0, 10.0),
                Candle(300_000, 101.0, 102.0, 100.0, 101.0, 10.0),
            ],
        )
        clock = _SequenceClock(0, 600_000, 600_000)
        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), provider, clock=clock).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                )
            )

        health = run.datasets[("BTC-USDT-SWAP", "5m")]
        self.assertEqual(0, run.started_at_ms)
        self.assertEqual(600_000, run.completed_at_ms)
        self.assertEqual(600_000, health.updated_at_ms)
        self.assertEqual(FreshnessState.FRESH, health.freshness)
        self.assertEqual(HealthReason.OK, health.primary_reason)

    def test_refresh_keeps_fresh_cache_usable_when_incremental_fetch_fails(self):
        from mu_strategy.market_data.trusted_data.contracts import (
            FreshnessState,
            HealthReason,
            IntegrityState,
            RefreshAttemptStatus,
            SnapshotUsability,
        )
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            from tests.factories.trusted_publication import write_flat_v3_publication

            write_flat_v3_publication(Path(tmp), symbol="BTC-USDT-SWAP")

            run = RefreshTrustedMarketData(store, _IncrementalFailureProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )
            manifest = json.loads(_manifest_path(Path(tmp)).read_text(encoding="utf-8"))

        health = run.datasets[("BTC-USDT-SWAP", "5m")]
        manifest_health = manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["5m"]
        self.assertEqual(RefreshAttemptStatus.DEGRADED, run.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
        self.assertEqual("usable", manifest["snapshot_usability"])
        self.assertEqual(IntegrityState.VALID, health.integrity)
        self.assertEqual(FreshnessState.FRESH, health.freshness)
        self.assertEqual(HealthReason.OK, health.primary_reason)
        self.assertIn("incremental_refresh_failed", health.warnings)
        self.assertTrue(manifest_health["is_valid"])
        self.assertFalse(manifest_health["is_stale"])
        self.assertEqual("ok", manifest_health["reason"])
        self.assertIn("incremental_refresh_failed", manifest_health["warnings"])
        self.assertIsNotNone(manifest_health["content_sha256"])

    def test_ticker_timeout_produces_failed_run_log(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), _TickerFailureProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )
            log_rows = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual(RefreshAttemptStatus.FAILED, run.attempt_status)
        self.assertEqual("failed", log_rows[-1]["attempt_status"])
        self.assertEqual("TimeoutError", log_rows[-1]["cycle_error"]["error_type"])

    def test_single_interval_failure_is_degraded_and_other_intervals_continue(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _Provider(
            ticker_rows=[
                {"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"},
                {"instId": "ETH-USDT-SWAP", "last": "50", "volCcy24h": "9"},
            ],
            fail_history={("BTC-USDT-SWAP", "15m")},
        )
        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m", "15m"),
                    days=1,
                    limit=2,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )

        self.assertEqual(RefreshAttemptStatus.DEGRADED, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        self.assertFalse(run.datasets[("BTC-USDT-SWAP", "15m")].is_usable)
        self.assertTrue(run.datasets[("ETH-USDT-SWAP", "15m")].is_usable)

    def test_all_invalid_materialized_datasets_are_failed_not_degraded(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _Provider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            fail_history={("BTC-USDT-SWAP", "5m"), ("BTC-USDT-SWAP", "15m")},
        )
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m", "15m"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))

        self.assertEqual(RefreshAttemptStatus.FAILED, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])

    def test_refresh_still_runs_built_native_validation_for_stale_structural_inputs(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from mu_strategy.market_data.utils import DAY_MS

        def fetch_history(symbol: str, interval: str, *, days: int):
            candles = _Provider().fetch_history(symbol, interval, days=days)
            if interval == "15m":
                first = candles[0]
                candles[0] = Candle(
                    first.open_time_ms,
                    first.open,
                    first.high + 1,
                    first.low,
                    first.close,
                    first.volume,
                )
            return candles

        provider = _Provider(
            ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            history_fetcher=fetch_history,
        )
        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m", "15m"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=10 * DAY_MS,
                )
            )

        native_health = run.datasets[("BTC-USDT-SWAP", "15m")]
        self.assertEqual(HealthReason.OHLCV_MISMATCH, native_health.primary_reason)
        self.assertFalse(native_health.validation.ok)

    def test_snapshot_usability_uses_validity_and_freshness_dimensions_separately(self):
        from mu_strategy.market_data.trusted_data.contracts import (
            AvailabilityState,
            FreshnessState,
            IntegrityState,
            SnapshotUsability,
            derive_snapshot_usability,
        )

        cases = [
            ("valid_fresh", _health_state("BTC-USDT-SWAP", "5m"), SnapshotUsability.USABLE),
            (
                "valid_stale",
                _health_state("BTC-USDT-SWAP", "5m", freshness=FreshnessState.STALE),
                SnapshotUsability.STALE,
            ),
            (
                "invalid",
                _health_state("BTC-USDT-SWAP", "5m", integrity=IntegrityState.INVALID),
                SnapshotUsability.INVALID,
            ),
            (
                "missing",
                _health_state("BTC-USDT-SWAP", "5m", availability=AvailabilityState.MISSING),
                SnapshotUsability.INVALID,
            ),
        ]
        for name, health, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, derive_snapshot_usability({("BTC-USDT-SWAP", "5m"): health}))

    def test_refresh_request_no_longer_accepts_explicit_symbol_scope(self):
        from dataclasses import fields

        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketDataRequest

        names = {field.name for field in fields(RefreshTrustedMarketDataRequest)}

        self.assertNotIn("explicit_symbols", names)
        self.assertNotIn("scope", names)

    def test_canonical_refresh_limit_zero_short_circuits_before_provider_config_or_candles(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = Mock()
        provider.fetch_tickers.side_effect = AssertionError("fetch_tickers")
        provider.fetch_history.side_effect = AssertionError("fetch_history")
        provider.fetch_incremental.side_effect = AssertionError("fetch_incremental")
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch("mu_strategy.market_data.trusted_data.refresh.load_stock_token_inst_ids", side_effect=AssertionError("stock config")) as load_config:
                run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), provider).execute(
                    RefreshTrustedMarketDataRequest(
                        requested_intervals=("5m",),
                        days=1,
                        limit=0,
                        stock_token_config=data_dir / "missing-stock-config.json",
                        now_ms=3_600_000,
                    )
                )
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))
            csv_paths = list(data_dir.rglob("*.csv"))

        provider.fetch_tickers.assert_not_called()
        provider.fetch_history.assert_not_called()
        provider.fetch_incremental.assert_not_called()
        load_config.assert_not_called()
        self.assertEqual(RefreshAttemptStatus.FAILED, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        self.assertEqual({}, run.datasets)
        self.assertEqual((), run.universe_snapshot.crypto_top)
        self.assertEqual((), run.universe_snapshot.stock_token_top)
        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual({"crypto_top": [], "stock_token_top": []}, manifest["universes"])
        self.assertEqual({}, manifest["symbols"])
        self.assertEqual("failed", run_log["attempt_status"])
        self.assertEqual("invalid", run_log["snapshot_usability"])
        self.assertEqual(0, run_log["symbol_count"])
        self.assertEqual([], csv_paths)

    def test_cache_read_failure_marks_refresh_attempt_failed(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from tests.factories.trusted_publication import write_flat_v3_publication

        provider = _Provider(ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}])
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            write_flat_v3_publication(data_dir, symbol="BTC-USDT-SWAP")
            store.cache_path("BTC-USDT-SWAP", "5m").write_text(
                "open_time_ms,open,high,low,close,volume\nnot-an-int,1,1,1,1,1\n",
                encoding="utf-8",
            )

            run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))

        health = run.datasets[("BTC-USDT-SWAP", "5m")]
        self.assertEqual(HealthReason.CACHE_READ_FAILED, health.primary_reason)
        self.assertEqual(RefreshAttemptStatus.FAILED, run.attempt_status)
        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("failed", run_log["attempt_status"])
        self.assertEqual((), run.provider_failures)
        self.assertEqual([], manifest["provider_failures"])
        self.assertEqual([], run_log["provider_failures"])
        self.assertEqual([], provider.history_calls)
        self.assertEqual([], provider.incremental_calls)

    def test_refresh_market_data_command_is_canonical_writer(self):
        from mu_strategy.commands.refresh_market_data import main

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            html_output = Path(tmp) / "health.html"
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers",
                return_value=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            ):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical", side_effect=_fake_fetcher):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=3_600_000):
                        exit_code = main(
                            [
                                "--limit",
                                "1",
                                "--days",
                                "1",
                                "--data-dir",
                                str(data_dir),
                                "--html-output",
                                str(html_output),
                            ],
                            stdout=_Sink(),
                        )

            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))
            html_exists = html_output.exists()

        self.assertEqual(0, exit_code)
        self.assertEqual(["BTC-USDT-SWAP"], [row["inst_id"] for row in manifest["universes"]["crypto_top"]])
        self.assertEqual("success", manifest["attempt_status"])
        self.assertEqual("usable", manifest["snapshot_usability"])
        self.assertEqual("success", run_log["attempt_status"])
        self.assertEqual("usable", run_log["snapshot_usability"])
        self.assertTrue(html_exists)


class TrustedDemoConsumerTests(unittest.TestCase):
    def test_demo_defaults_to_live_trusted_store_and_cache_only_loader(self):
        from mu_strategy.demo_trading import DemoTradingConfig

        config = DemoTradingConfig()

        self.assertEqual(Path("data/live"), config.data_dir)
        self.assertFalse(config.refresh)

    def test_demo_default_universe_comes_from_trusted_manifest_snapshot(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="BTC-USDT-SWAP",
                days=1,
                run_id="run-abc",
                universe_symbols=("BTC-USDT-SWAP", "MU-USDT-SWAP"),
            )
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                run_id="run-abc",
                universe_symbols=("BTC-USDT-SWAP", "MU-USDT-SWAP"),
            )
            result = run_once(
                DemoTradingConfig(universe_limit=2, dry_run=True, data_dir=data_dir, watchlist_symbols=()),
                broker=None,
                candle_loader=lambda symbol, **kwargs: _valid_bundle(symbol, run_id="run-abc"),
                scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _wait(symbol),
            )

        self.assertEqual(["BTC-USDT-SWAP", "MU-USDT-SWAP"], [item["inst_id"] for item in result["universe"]])
        self.assertEqual("run-abc", result["run_id"])
        self.assertTrue(all(scan["run_id"] == "run-abc" for scan in result["scans"]))

    def test_demo_dry_run_blocks_invalid_trusted_data_before_scanner(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once
        from mu_strategy.market_data.universe import OKXSwapTicker

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=True, watchlist_symbols=()),
            broker=None,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 100.0, 1.0)],
            candle_loader=lambda symbol, **kwargs: _invalid_bundle(symbol),
            scanner=lambda *args, **kwargs: self.fail("scanner must not run for invalid trusted data"),
        )

        self.assertEqual("dry_run", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual("skip", result["scans"][0]["action"])

    def test_demo_default_strict_gate_blocks_failed_or_partial_manifest_before_scanner(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        cases = [
            ("failed", "ok", "run_failed", {}),
            ("partial", "invalid", "manifest_invalid", {"integrity": "invalid"}),
        ]
        for outcome, status, reason, health_kwargs in cases:
            with self.subTest(outcome=outcome):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    _write_manifest_and_caches(
                        data_dir,
                        symbol="MU-USDT-SWAP",
                        days=1,
                        outcome=outcome,
                        status=status,
                        universe_symbols=("MU-USDT-SWAP",),
                        **health_kwargs,
                    )
                    result = run_once(
                        DemoTradingConfig(universe_limit=1, dry_run=True, data_dir=data_dir, watchlist_symbols=()),
                        broker=None,
                        scanner=lambda *args, **kwargs: self.fail("scanner must not run for blocked trusted data"),
                    )

                self.assertEqual([], result["orders"])
                self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
                self.assertEqual(reason, result["data_errors"][0]["status_reason"])

    def test_demo_default_cycle_reuses_single_manifest_snapshot_and_observed_time(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            for symbol in ("BTC-USDT-SWAP", "MU-USDT-SWAP"):
                _write_manifest_and_caches(
                    data_dir,
                    symbol=symbol,
                    days=1,
                    run_id="run-shared",
                    universe_symbols=("BTC-USDT-SWAP", "MU-USDT-SWAP"),
                )
            read_calls = []
            original_read_manifest = TrustedDataStore.read_manifest

            def counted_read_manifest(store, *args, **kwargs):
                read_calls.append(store.manifest_path)
                return original_read_manifest(store, *args, **kwargs)

            with patch.object(TrustedDataStore, "read_manifest", counted_read_manifest):
                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                    result = run_once(
                        DemoTradingConfig(universe_limit=2, dry_run=True, data_dir=data_dir, watchlist_symbols=()),
                        broker=None,
                        scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _wait(symbol),
                    )

        self.assertEqual(1, len(read_calls))
        self.assertEqual(["BTC-USDT-SWAP", "MU-USDT-SWAP"], [scan["symbol"] for scan in result["scans"]])
        self.assertEqual({"run-shared"}, {scan["run_id"] for scan in result["scans"]})
        self.assertEqual({86_400_000}, {scan["observed_at_ms"] for scan in result["scans"]})

    def test_demo_default_trusted_loader_rejects_refresh_before_broker_universe_or_scanner(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once
        from mu_strategy.market_data.trusted_data.contracts import TrustedConsumerRefreshError

        class Broker:
            def get_positions(self, **kwargs):
                raise AssertionError("broker positions")

            def get_open_orders(self, **kwargs):
                raise AssertionError("broker orders")

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                run_id="run-shared",
                universe_symbols=("BTC-USDT-SWAP", "MU-USDT-SWAP"),
            )
            manifest_path = _manifest_path(data_dir)
            manifest_before = manifest_path.read_bytes()

            with self.assertRaisesRegex(TrustedConsumerRefreshError, "refresh_market_data"):
                run_once(
                    DemoTradingConfig(refresh=True, dry_run=False, data_dir=data_dir),
                    broker=Broker(),
                    universe_provider=lambda limit: self.fail("universe provider"),
                    scanner=lambda *args, **kwargs: self.fail("scanner"),
                )

            self.assertEqual(manifest_before, manifest_path.read_bytes())

    def test_manifest_universe_limit_zero_and_one_are_exact(self):
        from mu_strategy.demo_trading import _tickers_from_universe_snapshot
        from mu_strategy.market_data.trusted_data.contracts import UniverseSnapshot

        snapshot = UniverseSnapshot(
            crypto_top=(
                {"inst_id": "BTC-USDT-SWAP", "last": 100, "volume_ccy_24h": 10, "source": "top"},
                {"inst_id": "ETH-USDT-SWAP", "last": 90, "volume_ccy_24h": 9, "source": "top"},
            ),
            stock_token_top=(
                {"inst_id": "MU-USDT-SWAP", "last": 5, "volume_ccy_24h": 8, "source": "stock_token"},
            ),
        )

        self.assertEqual([], _tickers_from_universe_snapshot(snapshot, limit=0))
        self.assertEqual(
            ["BTC-USDT-SWAP", "MU-USDT-SWAP"],
            [ticker.inst_id for ticker in _tickers_from_universe_snapshot(snapshot, limit=1)],
        )

    def test_manifest_universe_limit_applies_per_bucket(self):
        from mu_strategy.demo_trading import _tickers_from_universe_snapshot
        from mu_strategy.market_data.trusted_data.contracts import UniverseSnapshot

        snapshot = UniverseSnapshot(
            crypto_top=tuple(
                {"inst_id": f"CRYPTO-{index}-USDT-SWAP", "last": index, "volume_ccy_24h": 100 - index, "source": "top"}
                for index in range(10)
            ),
            stock_token_top=tuple(
                {"inst_id": f"STOCK-{index}-USDT-SWAP", "last": index, "volume_ccy_24h": 10 - index, "source": "stock_token"}
                for index in range(3)
            ),
        )

        tickers = _tickers_from_universe_snapshot(snapshot, limit=10)

        self.assertEqual(13, len(tickers))
        self.assertEqual([f"STOCK-{index}-USDT-SWAP" for index in range(3)], [ticker.inst_id for ticker in tickers[-3:]])

    def test_manifest_universe_limit_can_return_ten_crypto_plus_ten_stock_tokens(self):
        from mu_strategy.demo_trading import _tickers_from_universe_snapshot
        from mu_strategy.market_data.trusted_data.contracts import UniverseSnapshot

        snapshot = UniverseSnapshot(
            crypto_top=tuple({"inst_id": f"CRYPTO-{index}-USDT-SWAP"} for index in range(10)),
            stock_token_top=tuple({"inst_id": f"STOCK-{index}-USDT-SWAP"} for index in range(10)),
        )

        tickers = _tickers_from_universe_snapshot(snapshot, limit=10)

        self.assertEqual(20, len(tickers))
        self.assertEqual([f"CRYPTO-{index}-USDT-SWAP" for index in range(10)], [ticker.inst_id for ticker in tickers[:10]])
        self.assertEqual([f"STOCK-{index}-USDT-SWAP" for index in range(10)], [ticker.inst_id for ticker in tickers[10:]])

    def test_manifest_universe_small_limit_keeps_each_bucket_order(self):
        from mu_strategy.demo_trading import _tickers_from_universe_snapshot
        from mu_strategy.market_data.trusted_data.contracts import UniverseSnapshot

        snapshot = UniverseSnapshot(
            crypto_top=tuple({"inst_id": f"CRYPTO-{index}-USDT-SWAP"} for index in range(5)),
            stock_token_top=tuple({"inst_id": f"STOCK-{index}-USDT-SWAP"} for index in range(5)),
        )

        tickers = _tickers_from_universe_snapshot(snapshot, limit=2)

        self.assertEqual(
            ["CRYPTO-0-USDT-SWAP", "CRYPTO-1-USDT-SWAP", "STOCK-0-USDT-SWAP", "STOCK-1-USDT-SWAP"],
            [ticker.inst_id for ticker in tickers],
        )

    def test_manifest_universe_dedupes_across_buckets_after_bucket_limits(self):
        from mu_strategy.demo_trading import _tickers_from_universe_snapshot
        from mu_strategy.market_data.trusted_data.contracts import UniverseSnapshot

        snapshot = UniverseSnapshot(
            crypto_top=(
                {"inst_id": "BTC-USDT-SWAP", "source": "top"},
                {"inst_id": "ETH-USDT-SWAP", "source": "top"},
            ),
            stock_token_top=(
                {"inst_id": "BTC-USDT-SWAP", "source": "stock_token"},
                {"inst_id": "MU-USDT-SWAP", "source": "stock_token"},
            ),
        )

        tickers = _tickers_from_universe_snapshot(snapshot, limit=2)

        self.assertEqual(["BTC-USDT-SWAP", "ETH-USDT-SWAP", "MU-USDT-SWAP"], [ticker.inst_id for ticker in tickers])

    def test_demo_dry_run_scans_crypto_and_stock_token_manifest_symbols(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        scanned = []
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="BTC-USDT-SWAP",
                days=1,
                universe_symbols=("BTC-USDT-SWAP",),
                stock_token_symbols=("MU-USDT-SWAP",),
            )
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                universe_symbols=("BTC-USDT-SWAP",),
                stock_token_symbols=("MU-USDT-SWAP",),
            )

            with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                result = run_once(
                    DemoTradingConfig(universe_limit=1, dry_run=True, data_dir=data_dir, watchlist_symbols=()),
                    broker=None,
                    scanner=lambda symbol, candles_15m, candles_1h, **kwargs: scanned.append(symbol) or _wait(symbol),
                )

        self.assertEqual(["BTC-USDT-SWAP", "MU-USDT-SWAP"], [item["inst_id"] for item in result["universe"]])
        self.assertEqual(["BTC-USDT-SWAP", "MU-USDT-SWAP"], scanned)

    def test_demo_confirmed_mode_keeps_stock_token_when_crypto_bucket_is_full(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        scanned = []
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "MU-USDT-SWAP"):
                _write_manifest_and_caches(
                    data_dir,
                    symbol=symbol,
                    days=1,
                    universe_symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
                    stock_token_symbols=("MU-USDT-SWAP",),
                )
            with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                result = run_once(
                    DemoTradingConfig(universe_limit=2, dry_run=False, data_dir=data_dir, watchlist_symbols=()),
                    broker=_EmptyBroker(),
                    scanner=lambda symbol, candles_15m, candles_1h, **kwargs: scanned.append(symbol) or _wait(symbol),
                )

        self.assertEqual(["BTC-USDT-SWAP", "ETH-USDT-SWAP", "MU-USDT-SWAP"], [item["inst_id"] for item in result["universe"]])
        self.assertEqual(["BTC-USDT-SWAP", "ETH-USDT-SWAP", "MU-USDT-SWAP"], scanned)
        self.assertEqual([], result["orders"])

    def test_demo_watchlist_duplicate_is_not_scanned_twice(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        scanned = []
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                universe_symbols=("BTC-USDT-SWAP",),
                stock_token_symbols=("MU-USDT-SWAP",),
            )
            _write_manifest_and_caches(
                data_dir,
                symbol="BTC-USDT-SWAP",
                days=1,
                universe_symbols=("BTC-USDT-SWAP",),
                stock_token_symbols=("MU-USDT-SWAP",),
            )
            with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                run_once(
                    DemoTradingConfig(universe_limit=1, dry_run=True, data_dir=data_dir, watchlist_symbols=("MU-USDT-SWAP",)),
                    broker=None,
                    scanner=lambda symbol, candles_15m, candles_1h, **kwargs: scanned.append(symbol) or _wait(symbol),
                )

        self.assertEqual(["BTC-USDT-SWAP", "MU-USDT-SWAP"], scanned)

    def test_trusted_manifest_universe_provider_limit_zero_returns_empty_without_manifest(self):
        from mu_strategy.demo_trading import trusted_manifest_universe_provider

        with TemporaryDirectory() as tmp:
            self.assertEqual([], trusted_manifest_universe_provider(limit=0, data_dir=Path(tmp)))

    def test_consumer_paths_do_not_call_manifest_writers(self):
        import mu_strategy.demo_trading as demo_trading
        import mu_strategy.market_data.service as service
        from mu_strategy import cli, visualize
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                run_id="run-shared",
                universe_symbols=("MU-USDT-SWAP",),
            )

            with patch.object(TrustedDataStore, "write_manifest", side_effect=AssertionError("write_manifest")):
                with patch.object(TrustedDataStore, "append_run_log", side_effect=AssertionError("append_run_log")):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                        service.refresh_trusted_candle_bundle(
                            "MU-USDT-SWAP",
                            intervals=("15m", "1h"),
                            days=1,
                            data_dir=data_dir,
                            refresh=False,
                            clock=_FakeClock(86_400_000),
                        )
                        with patch("sys.argv", ["mu_strategy.cli", "--trusted-data", "--data-dir", str(data_dir), "--report", str(data_dir / "report.md")]):
                            with patch("mu_strategy.cli.run_backtest", return_value=_empty_backtest()):
                                with patch("mu_strategy.cli.render_markdown_report", return_value="# report"):
                                    with patch("sys.stdout", new_callable=_Sink):
                                        cli.main()
                        with patch(
                            "sys.argv",
                            ["mu_strategy.visualize", "--trusted-data", "--data-dir", str(data_dir), "--output", str(data_dir / "chart.html")],
                        ):
                            with patch("mu_strategy.viz.backtest.run_backtest", return_value=_empty_backtest()):
                                with patch("sys.stdout", new_callable=_Sink):
                                    visualize.main()
                        demo_trading.run_once(
                            demo_trading.DemoTradingConfig(universe_limit=1, dry_run=True, data_dir=data_dir, watchlist_symbols=()),
                            broker=None,
                            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _wait(symbol),
                        )

    def test_consumer_modules_do_not_reference_canonical_refresh_or_manifest_writers(self):
        for relative_path in (
            "mu_strategy/market_data/service.py",
            "mu_strategy/cli.py",
            "mu_strategy/viz/backtest.py",
            "mu_strategy/demo_trading.py",
            "mu_strategy/commands/okx_demo_loop.py",
        ):
            source = Path(relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertNotIn("refresh_with_okx_provider", source)
                self.assertNotIn("RefreshTrustedMarketData", source)
                self.assertNotIn(".write_manifest(", source)
                self.assertNotIn(".append_run_log(", source)

    def test_okx_demo_loop_cli_defaults_to_live_data_dir(self):
        from mu_strategy.commands.okx_demo_loop import main

        captured = {}

        def runner(config, broker):
            captured["data_dir"] = config.data_dir
            return {"mode": "dry_run"}

        main(["--once", "--dry-run"], stdout=_Sink(), runner=runner)

        self.assertEqual(Path("data/live"), captured["data_dir"])

    def test_demo_watchlist_symbol_missing_from_manifest_blocks_scanner_without_orphan_csv(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="BTC-USDT-SWAP",
                days=1,
                universe_symbols=("BTC-USDT-SWAP",),
            )
            _write_orphan_caches(data_dir, symbol="MU-USDT-SWAP", days=1)

            with patch.object(TrustedDataStore, "read_csv", side_effect=AssertionError("watchlist orphan CSV must not be read")):
                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                    result = run_once(
                        DemoTradingConfig(universe_limit=0, dry_run=True, data_dir=data_dir),
                        broker=None,
                        scanner=lambda *args, **kwargs: self.fail("scanner must not run for unpublished watchlist data"),
                    )

        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual(HealthReason.NOT_PUBLISHED.value, result["data_errors"][0]["status_reason"])
        self.assertEqual("skip", result["scans"][0]["action"])

    def test_demo_staleness_setting_is_enforced_by_loader_policy_without_second_wall_clock(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                universe_symbols=("MU-USDT-SWAP",),
            )
            with patch("time.time", side_effect=AssertionError("demo must not use wall-clock freshness")):
                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_001):
                    result = run_once(
                        DemoTradingConfig(
                            universe_limit=0,
                            dry_run=True,
                            data_dir=data_dir,
                            max_candle_staleness_bars=1,
                        ),
                        broker=None,
                        scanner=lambda *args, **kwargs: self.fail("scanner must not run for loader-stale data"),
                    )

        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual("stale_by_clock", result["data_errors"][0]["status_reason"])

    def test_demo_blocks_timestamp_gap_before_scanner(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                universe_symbols=("MU-USDT-SWAP",),
            )
            store = TrustedDataStore(data_dir=data_dir)
            write_csv(_constant_candles((0, 1_800_000)), store.cache_path("MU-USDT-SWAP", "15m"))

            with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                result = run_once(
                    DemoTradingConfig(universe_limit=1, dry_run=True, data_dir=data_dir, watchlist_symbols=()),
                    broker=None,
                    scanner=lambda *args, **kwargs: self.fail("scanner must not run for timestamp_gap data"),
                )

        self.assertEqual([], result["orders"])
        self.assertEqual([], [scan for scan in result["scans"] if scan["action"] == "enter"])
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual("timestamp_gap", result["data_errors"][0]["status_reason"])


def _write_manifest_and_caches(
    data_dir: Path,
    *,
    symbol: str,
    days: int,
    outcome: str = "success",
    status: str = "ok",
    integrity: str = "valid",
    freshness: str = "fresh",
    run_id: str = "run-1",
    universe_symbols: tuple[str, ...] | None = None,
    stock_token_symbols: tuple[str, ...] | None = None,
) -> dict:
    from mu_strategy.market_data.trusted_data.contracts import (
        RefreshAttemptStatus,
        SnapshotUsability,
    )
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles
    from mu_strategy.market_data.utils import DAY_MS

    store = TrustedDataStore(data_dir=data_dir)
    five = [
        Candle(index * 300_000, 100.0 + index, 101.0 + index, 99.0 + index, 100.0 + index, 1000.0)
        for index in range(days * DAY_MS // 300_000)
    ]
    by_interval = {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }
    manifest_path = _manifest_path(data_dir)
    previous_symbols = {}
    if manifest_path.exists():
        previous_symbols = json.loads(manifest_path.read_text(encoding="utf-8")).get("symbols") or {}
    symbols = dict(previous_symbols)
    symbols.setdefault(symbol, {"intervals": {}})
    for interval, candles in by_interval.items():
        path = store.cache_path(symbol, interval)
        store.write_csv(candles, path)
        reason = "ok"
        if integrity == "invalid":
            reason = "refresh_failed"
        elif freshness == "stale":
            reason = "stale_by_clock"
        elif freshness == "unknown":
            reason = "freshness_unknown"
        symbols[symbol]["intervals"][interval] = {
            "symbol": symbol,
            "interval": interval,
            "availability": "available",
            "integrity": integrity,
            "freshness": freshness,
            "reasons": [reason],
            "rows": len(candles),
            "first_timestamp_ms": candles[0].open_time_ms,
            "last_timestamp_ms": candles[-1].open_time_ms,
            "updated_at_ms": 86_400_000,
            "source_file": str(path),
            "content_sha256": candles_content_sha256(candles) if integrity == "valid" else None,
            "validation": {"ok": integrity == "valid", "reason": "ok" if integrity == "valid" else reason},
        }
    universe_rows = [
        {"inst_id": item, "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"}
        for item in (universe_symbols or ())
    ]
    stock_token_rows = [
        {"inst_id": item, "last": 100.0, "volume_ccy_24h": 10.0, "source": "stock_token"}
        for item in (stock_token_symbols or ())
    ]
    attempt_status = (
        RefreshAttemptStatus.FAILED.value
        if outcome == "failed"
        else RefreshAttemptStatus.DEGRADED.value
        if outcome == "partial"
        else RefreshAttemptStatus.SUCCESS.value
    )
    if status == "ok":
        snapshot_usability = SnapshotUsability.USABLE.value
    elif status == "stale":
        snapshot_usability = SnapshotUsability.STALE.value
    elif status == "invalid":
        snapshot_usability = SnapshotUsability.INVALID.value
    else:
        snapshot_usability = status
    manifest = {
        "schema_version": 3,
        "run_id": run_id,
        "attempt_status": attempt_status,
        "snapshot_usability": snapshot_usability,
        "started_at_ms": 0,
        "completed_at_ms": 0,
        "requested_intervals": ["15m", "1h"],
        "effective_intervals": ["5m", "15m", "1h"],
        "universes": {"crypto_top": universe_rows, "stock_token_top": stock_token_rows},
        "symbols": symbols,
        "provider_failures": [],
        "warnings": [],
        "cycle_error": {"error_type": "TimeoutError", "message": "blocked"} if outcome == "failed" else None,
    }
    store.write_manifest(manifest)
    return manifest


def _write_orphan_caches(data_dir: Path, *, symbol: str, days: int) -> None:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles
    from mu_strategy.market_data.utils import DAY_MS

    store = TrustedDataStore(data_dir=data_dir)
    five = [
        Candle(index * 300_000, 100.0 + index, 101.0 + index, 99.0 + index, 100.0 + index, 1000.0)
        for index in range(days * DAY_MS // 300_000)
    ]
    by_interval = {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }
    for interval, candles in by_interval.items():
        store.write_csv(candles, store.cache_path(symbol, interval))


def _constant_candles(timestamps: tuple[int, ...]) -> list[Candle]:
    return [Candle(timestamp, 100.0, 101.0, 99.0, 100.0, 10.0) for timestamp in timestamps]


def _health_state(
    symbol: str,
    interval: str,
    *,
    availability=None,
    integrity=None,
    freshness=None,
):
    from mu_strategy.market_data.trusted_data.contracts import (
        AvailabilityState,
        DatasetHealth,
        DatasetKey,
        FreshnessState,
        HealthReason,
        IntegrityState,
    )

    availability = availability or AvailabilityState.AVAILABLE
    integrity = integrity or IntegrityState.VALID
    freshness = freshness or FreshnessState.FRESH
    reasons = (HealthReason.OK,)
    if availability == AvailabilityState.MISSING:
        reasons = (HealthReason.CACHE_MISSING,)
    elif integrity == IntegrityState.INVALID:
        reasons = (HealthReason.REFRESH_FAILED,)
    elif freshness == FreshnessState.STALE:
        reasons = (HealthReason.STALE_BY_CLOCK,)
    return DatasetHealth(
        key=DatasetKey(symbol, interval),
        availability=availability,
        integrity=integrity,
        freshness=freshness,
        reasons=reasons,
        rows=1 if availability == AvailabilityState.AVAILABLE else 0,
        first_timestamp_ms=0 if availability == AvailabilityState.AVAILABLE else None,
        last_timestamp_ms=0 if availability == AvailabilityState.AVAILABLE else None,
        source_file=Path(f"data/live/okx/{symbol}/{interval}.csv"),
    )


class _FakeClock:
    def __init__(self, now_ms: int):
        self.now = now_ms
        self.calls = 0

    def now_ms(self) -> int:
        self.calls += 1
        return self.now


class _SequenceClock:
    def __init__(self, *values: int):
        self.values = list(values)
        self.calls = 0

    def now_ms(self) -> int:
        self.calls += 1
        if self.values:
            return self.values.pop(0)
        raise AssertionError("sequence clock exhausted")


def _invalid_bundle(symbol):
    from mu_strategy.market_data.service import CandleBundle
    from mu_strategy.market_data.symbols import ResolvedSymbol
    from mu_strategy.market_data.trusted import DataStatus
    from mu_strategy.models import Candle

    candle = Candle(0, 100.0, 101.0, 99.0, 100.0, 1.0)
    status = DataStatus(
        symbol=symbol,
        interval="15m",
        rows=1,
        first_timestamp_ms=0,
        last_timestamp_ms=0,
        updated_at_ms=0,
        source_file=Path("data/live/okx/MU-USDT-SWAP/15m.csv"),
        is_valid=False,
        is_stale=False,
        reason="native_empty",
    )
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": [candle], "1h": [candle]},
        files_by_interval={},
        days=1,
        statuses_by_interval={"15m": status},
    )


def _valid_bundle(symbol, *, run_id=None):
    from mu_strategy.market_data.service import CandleBundle
    from mu_strategy.market_data.symbols import ResolvedSymbol
    from mu_strategy.market_data.trusted import DataStatus

    candle = Candle(0, 100.0, 101.0, 99.0, 100.0, 1.0)
    statuses = {
        interval: DataStatus(
            symbol=symbol,
            interval=interval,
            rows=1,
            first_timestamp_ms=0,
            last_timestamp_ms=0,
            updated_at_ms=0,
            source_file=Path(f"data/live/okx/{symbol}/{interval}.csv"),
            is_valid=True,
            is_stale=False,
            reason="ok",
        )
        for interval in ("5m", "15m", "1h")
    }
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": [candle], "1h": [candle]},
        files_by_interval={},
        days=1,
        statuses_by_interval=statuses,
        run_id=run_id,
    )


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


def _empty_backtest():
    from mu_strategy.models import BacktestResult

    return BacktestResult(10_000, 10_000, [], [])


class _Sink:
    def write(self, value):
        return len(value)

    def flush(self):
        return None


class _TextSink:
    def __init__(self):
        self.values = []

    def write(self, value):
        self.values.append(value)
        return len(value)

    def flush(self):
        return None

    @property
    def text(self):
        return "".join(self.values)


class _Provider:
    def __init__(self, *, ticker_rows=None, fail_history=None, history_fetcher=None):
        self.ticker_rows = ticker_rows or []
        self.fail_history = set(fail_history or ())
        self.history_fetcher = history_fetcher
        self.history_calls = []
        self.incremental_calls = []

    def fetch_tickers(self):
        return list(self.ticker_rows)

    def fetch_history(self, symbol, interval, *, days):
        if (symbol, interval) in self.fail_history:
            raise TimeoutError(f"blocked {symbol} {interval}")
        self.history_calls.append((symbol, interval, days))
        if self.history_fetcher is not None:
            return self.history_fetcher(symbol, interval, days=days)
        return _candles(interval)

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        self.incremental_calls.append((symbol, interval, since_time_ms))
        return _candles(interval)


class _TickerFailureProvider:
    def fetch_tickers(self):
        raise TimeoutError("ticker timeout")

    def fetch_history(self, symbol, interval, *, days):
        raise AssertionError("must not fetch history")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise AssertionError("must not fetch incremental")


class _IncrementalFailureProvider:
    def fetch_tickers(self):
        return [{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}]

    def fetch_history(self, symbol, interval, *, days):
        raise AssertionError("cache should force incremental path")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise TimeoutError("blocked incremental")


class _EmptyBroker:
    def get_positions(self, **kwargs):
        return {"code": "0", "data": []}

    def get_open_orders(self, **kwargs):
        return {"code": "0", "data": []}


def _candles(interval: str) -> list[Candle]:
    five = [Candle(index * 300_000, 100 + index, 101 + index, 99 + index, 100 + index, 10.0) for index in range(12)]
    if interval == "5m":
        return five
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles

    return aggregate_candles(five, interval=interval)


def _fake_fetcher(symbol: str, interval: str, *, days: int) -> list[Candle]:
    return _candles(interval)


def _manifest_path(data_dir: Path) -> Path:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore

    return TrustedDataStore(data_dir=data_dir).manifest_path


if __name__ == "__main__":
    unittest.main()
