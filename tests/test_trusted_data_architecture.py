import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

            (data_dir / "manifest.json").write_text("{not-json", encoding="utf-8")
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
                status="invalid",
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

    def test_strict_policy_rejects_partial_manifest_even_when_requested_symbol_is_valid(self):
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
                status="ok",
                run_id="run-partial",
            )
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1),
                trading_strict_policy(),
            )

        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.RUN_PARTIAL, bundle.trust_decision.reason)

    def test_strict_policy_rejects_success_manifest_with_invalid_or_stale_status(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = [
            ("invalid", HealthReason.MANIFEST_INVALID),
            ("stale", HealthReason.MANIFEST_STALE),
        ]
        for status, reason in cases:
            with self.subTest(status=status):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    _write_manifest_and_caches(
                        data_dir,
                        symbol="MU-USDT-SWAP",
                        days=1,
                        outcome="success",
                        status=status,
                    )
                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1),
                        trading_strict_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(reason, bundle.trust_decision.reason)

    def test_observe_only_failed_manifest_preserves_real_manifest_context(self):
        from mu_strategy.market_data.trusted_data.contracts import ManifestStatus, RefreshRunOutcome
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
                status="invalid",
                run_id="run-diagnostic",
            )
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                observe_only_policy(),
            )

        self.assertTrue(bundle.trust_decision.allowed)
        self.assertEqual("run-diagnostic", bundle.run_id)
        self.assertIsNotNone(bundle.load_context)
        self.assertEqual(RefreshRunOutcome.FAILED, bundle.load_context.manifest.outcome)
        self.assertEqual(ManifestStatus.INVALID, bundle.load_context.manifest.status)

    def test_malformed_schema_v2_fail_closed(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        cases = {
            "missing_outcome": lambda manifest: manifest.pop("outcome"),
            "unknown_outcome": lambda manifest: manifest.__setitem__("outcome", "unknown"),
            "missing_status": lambda manifest: manifest.pop("status"),
            "unknown_status": lambda manifest: manifest.__setitem__("status", "missing"),
            "wrong_schema_version": lambda manifest: manifest.__setitem__("schema_version", 3),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                with TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    manifest = _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=1)
                    mutate(manifest)
                    (data_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

                    bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir), clock=_FakeClock(86_400_000)).execute(
                        LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1),
                        observe_only_policy(),
                    )

                self.assertFalse(bundle.trust_decision.allowed)
                self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

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
            target = data_dir / "manifest.json"

            with patch("mu_strategy.market_data.trusted_data.store.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.write_manifest({"schema_version": 2, "status": "ok"})

            self.assertFalse(target.exists())


class TrustedDataRefreshTests(unittest.TestCase):
    def test_refresh_records_requested_and_effective_intervals(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshRunOutcome
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
            manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(RefreshRunOutcome.SUCCESS, run.outcome)
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

    def test_ticker_timeout_produces_failed_run_log(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshRunOutcome
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

        self.assertEqual(RefreshRunOutcome.FAILED, run.outcome)
        self.assertEqual("failed", log_rows[-1]["outcome"])
        self.assertEqual("TimeoutError", log_rows[-1]["cycle_error"]["error_type"])

    def test_single_interval_failure_is_partial_and_other_intervals_continue(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshRunOutcome
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

        self.assertEqual(RefreshRunOutcome.PARTIAL, run.outcome)
        self.assertFalse(run.datasets[("BTC-USDT-SWAP", "15m")].is_usable)
        self.assertTrue(run.datasets[("ETH-USDT-SWAP", "15m")].is_usable)

    def test_manifest_status_uses_validity_and_freshness_dimensions_separately(self):
        from mu_strategy.market_data.trusted_data.contracts import (
            AvailabilityState,
            FreshnessState,
            IntegrityState,
            RefreshRun,
            RefreshRunOutcome,
            UniverseSnapshot,
        )

        cases = [
            ("valid_fresh", RefreshRunOutcome.SUCCESS, _health_state("BTC-USDT-SWAP", "5m"), "ok"),
            (
                "valid_stale",
                RefreshRunOutcome.SUCCESS,
                _health_state("BTC-USDT-SWAP", "5m", freshness=FreshnessState.STALE),
                "stale",
            ),
            (
                "invalid",
                RefreshRunOutcome.SUCCESS,
                _health_state("BTC-USDT-SWAP", "5m", integrity=IntegrityState.INVALID),
                "invalid",
            ),
            (
                "missing",
                RefreshRunOutcome.SUCCESS,
                _health_state("BTC-USDT-SWAP", "5m", availability=AvailabilityState.MISSING),
                "invalid",
            ),
            ("failed_run", RefreshRunOutcome.FAILED, _health_state("BTC-USDT-SWAP", "5m"), "invalid"),
        ]
        for name, outcome, health, expected in cases:
            with self.subTest(name=name):
                run = RefreshRun(
                    run_id=name,
                    outcome=outcome,
                    started_at_ms=0,
                    completed_at_ms=0,
                    requested_intervals=("5m",),
                    effective_intervals=("5m",),
                    universe_snapshot=UniverseSnapshot(),
                    datasets={("BTC-USDT-SWAP", "5m"): health},
                )

                self.assertEqual(expected, run.manifest_status())

    def test_explicit_symbol_refresh_request_is_rejected_before_manifest_publish(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            with patch.object(store, "write_manifest", side_effect=AssertionError("write_manifest")):
                with patch.object(store, "append_run_log", side_effect=AssertionError("append_run_log")):
                    with self.assertRaisesRegex(ValueError, "explicit_symbols"):
                        RefreshTrustedMarketData(store, _Provider(ticker_rows=[])).execute(
                            RefreshTrustedMarketDataRequest(
                                requested_intervals=("5m",),
                                days=1,
                                limit=0,
                                explicit_symbols=("BTC-USDT-SWAP",),
                                stock_token_inst_ids=set(),
                            )
                        )

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

            manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))
            html_exists = html_output.exists()

        self.assertEqual(0, exit_code)
        self.assertEqual(["BTC-USDT-SWAP"], [row["inst_id"] for row in manifest["universes"]["crypto_top"]])
        self.assertEqual("success", manifest["outcome"])
        self.assertEqual("success", run_log["outcome"])
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
            (data_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "run_id": "run-abc",
                        "outcome": "success",
                        "status": "ok",
                        "started_at_ms": 0,
                        "completed_at_ms": 0,
                        "requested_intervals": ["15m", "1h"],
                        "effective_intervals": ["5m", "15m", "1h"],
                        "universes": {
                            "crypto_top": [
                                {"inst_id": "BTC-USDT-SWAP", "last": 100, "volume_ccy_24h": 10, "source": "top"}
                            ],
                            "stock_token_top": [
                                {"inst_id": "MU-USDT-SWAP", "last": 5, "volume_ccy_24h": 8, "source": "stock_token"}
                            ],
                        },
                        "symbols": {},
                        "warnings": [],
                        "cycle_error": None,
                    }
                ),
                encoding="utf-8",
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
            ("failed", "invalid", "run_failed"),
            ("partial", "ok", "run_partial"),
        ]
        for outcome, status, reason in cases:
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
                    )
                    with patch("mu_strategy.demo_trading.time.time", return_value=86_400):
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
                    with patch("mu_strategy.demo_trading.time.time", return_value=86_400):
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
            manifest_path = data_dir / "manifest.json"
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
        self.assertEqual(["BTC-USDT-SWAP"], [ticker.inst_id for ticker in _tickers_from_universe_snapshot(snapshot, limit=1)])

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


def _write_manifest_and_caches(
    data_dir: Path,
    *,
    symbol: str,
    days: int,
    outcome: str = "success",
    status: str = "ok",
    run_id: str = "run-1",
    universe_symbols: tuple[str, ...] | None = None,
) -> dict:
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
    manifest_path = data_dir / "manifest.json"
    previous_symbols = {}
    if manifest_path.exists():
        previous_symbols = json.loads(manifest_path.read_text(encoding="utf-8")).get("symbols") or {}
    symbols = dict(previous_symbols)
    symbols.setdefault(symbol, {"intervals": {}})
    for interval, candles in by_interval.items():
        path = store.cache_path(symbol, interval)
        store.write_csv(candles, path)
        symbols[symbol]["intervals"][interval] = {
            "availability": "available",
            "integrity": "valid",
            "freshness": "fresh",
            "reasons": ["ok"],
            "rows": len(candles),
            "first_timestamp_ms": candles[0].open_time_ms,
            "last_timestamp_ms": candles[-1].open_time_ms,
            "source_file": str(path),
            "validation": {"ok": True, "reason": "ok"},
        }
    universe_rows = [
        {"inst_id": item, "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"}
        for item in (universe_symbols or ())
    ]
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "outcome": outcome,
        "status": status,
        "started_at_ms": 0,
        "completed_at_ms": 0,
        "requested_intervals": ["15m", "1h"],
        "effective_intervals": ["5m", "15m", "1h"],
        "universes": {"crypto_top": universe_rows, "stock_token_top": []},
        "symbols": symbols,
        "warnings": [],
        "cycle_error": {"error_type": "TimeoutError", "message": "blocked"} if outcome == "failed" else None,
    }
    store.write_manifest(manifest)
    return manifest


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


class _Provider:
    def __init__(self, *, ticker_rows, fail_history=None):
        self.ticker_rows = ticker_rows
        self.fail_history = set(fail_history or ())
        self.history_calls = []
        self.incremental_calls = []

    def fetch_tickers(self):
        return list(self.ticker_rows)

    def fetch_history(self, symbol, interval, *, days):
        if (symbol, interval) in self.fail_history:
            raise TimeoutError(f"blocked {symbol} {interval}")
        self.history_calls.append((symbol, interval, days))
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


def _candles(interval: str) -> list[Candle]:
    five = [Candle(index * 300_000, 100 + index, 101 + index, 99 + index, 100 + index, 10.0) for index in range(12)]
    if interval == "5m":
        return five
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles

    return aggregate_candles(five, interval=interval)


def _fake_fetcher(symbol: str, interval: str, *, days: int) -> list[Candle]:
    return _candles(interval)


if __name__ == "__main__":
    unittest.main()
