import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle


class TrustedStateSeparationTests(unittest.TestCase):
    def test_incremental_failure_keeps_fresh_snapshot_usable_but_attempt_degraded_everywhere(self):
        from mu_strategy.commands.refresh_market_data import classify_refresh_run
        from mu_strategy.market_data.trusted_data.contracts import (
            RefreshAttemptStatus,
            SnapshotUsability,
        )
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            from tests.factories.trusted_publication import write_flat_v3_publication

            write_flat_v3_publication(data_dir, symbol="BTC-USDT-SWAP")

            run = RefreshTrustedMarketData(store, _IncrementalFailureProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=86_400_000,
                )
            )
            command_result = classify_refresh_run(run)
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))

        self.assertEqual(RefreshAttemptStatus.DEGRADED, run.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
        self.assertEqual(2, command_result.exit_code)
        self.assertEqual("degraded", command_result.to_dict()["attempt_status"])
        self.assertEqual("usable", command_result.to_dict()["snapshot_usability"])
        for payload in (manifest, run_log):
            with self.subTest(payload=payload):
                self.assertEqual("degraded", payload["attempt_status"])
                self.assertEqual("usable", payload["snapshot_usability"])
                self.assertEqual(
                    [
                        {
                            "symbol": "BTC-USDT-SWAP",
                            "interval": "5m",
                            "reason": "incremental_refresh_failed",
                            "error_type": "TimeoutError",
                            "message": "blocked incremental",
                        }
                    ],
                    payload["provider_failures"],
                )

    def test_empty_exception_message_provider_failure_manifest_round_trips(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            run = RefreshTrustedMarketData(store, _HistoryTimeoutProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )

            manifest_result = store.read_manifest()
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery("BTC-USDT-SWAP", intervals=("5m",), days=1, now_ms=3_600_000),
                trading_strict_policy(),
            )

        self.assertEqual(
            (
                {
                    "symbol": "BTC-USDT-SWAP",
                    "interval": "5m",
                    "reason": "refresh_failed",
                    "error_type": "TimeoutError",
                    "message": "TimeoutError",
                },
            ),
            run.provider_failures,
        )
        self.assertTrue(manifest_result.ok, manifest_result.message)
        self.assertNotEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_empty_exception_message_universe_failure_manifest_round_trips(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            run = RefreshTrustedMarketData(store, _UniverseTimeoutProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )

            manifest_result = store.read_manifest()
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery("BTC-USDT-SWAP", intervals=("5m",), days=1, now_ms=3_600_000),
                trading_strict_policy(),
            )

        self.assertEqual(
            (
                {
                    "symbol": "*",
                    "interval": "*",
                    "reason": "refresh_failed",
                    "error_type": "TimeoutError",
                    "message": "TimeoutError",
                },
            ),
            run.provider_failures,
        )
        self.assertEqual({"error_type": "TimeoutError", "message": "TimeoutError"}, run.cycle_error)
        self.assertTrue(manifest_result.ok, manifest_result.message)
        self.assertNotEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_timestamp_gaps_round_trip_through_single_compat_adapter(self):
        from mu_strategy.market_data.trusted_data.compat import data_status_from_health
        from mu_strategy.market_data.trusted_data.contracts import (
            AvailabilityState,
            DatasetHealth,
            DatasetKey,
            FreshnessState,
            HealthReason,
            IntegrityState,
            ValidationReport,
        )

        gap = {
            "previous_timestamp_ms": 0,
            "current_timestamp_ms": 600_000,
            "expected_interval_ms": 300_000,
            "actual_interval_ms": 600_000,
            "missing_count": 1,
        }
        health = DatasetHealth(
            key=DatasetKey("BTC-USDT-SWAP", "5m"),
            availability=AvailabilityState.AVAILABLE,
            integrity=IntegrityState.INVALID,
            freshness=FreshnessState.STALE,
            reasons=(HealthReason.TIMESTAMP_GAP,),
            rows=2,
            first_timestamp_ms=0,
            last_timestamp_ms=600_000,
            source_file=Path("data/live/okx/BTC-USDT-SWAP/5m.csv"),
            validation=ValidationReport(False, HealthReason.TIMESTAMP_GAP, timestamp_gaps=(gap,)),
        )

        status = data_status_from_health(health)
        payload = status.to_dict()

        self.assertEqual([gap], status.validation.timestamp_gaps)
        self.assertEqual([gap], payload["validation"]["timestamp_gaps"])

    def test_demo_custom_loader_missing_legacy_status_is_blocked_without_consumer_freshness_clock(self):
        import mu_strategy.demo_trading as demo_trading
        from mu_strategy.market_data.service import CandleBundle
        from mu_strategy.market_data.symbols import ResolvedSymbol
        from mu_strategy.market_data.universe import OKXSwapTicker

        status = _valid_status("15m")
        legacy_bundle = CandleBundle(
            symbol=ResolvedSymbol(requested="BTC-USDT-SWAP", inst_id="BTC-USDT-SWAP", source="okx"),
            candles_by_interval={
                "15m": [Candle(0, 100.0, 101.0, 99.0, 100.0, 1.0)],
                "1h": [Candle(0, 100.0, 101.0, 99.0, 100.0, 1.0)],
            },
            files_by_interval={},
            days=1,
            statuses_by_interval={"15m": status},
        )

        with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", side_effect=AssertionError("consumer clock")):
            result = demo_trading.run_once(
                demo_trading.DemoTradingConfig(universe_limit=1, dry_run=True, watchlist_symbols=()),
                broker=None,
                universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 100.0, 1.0)],
                candle_loader=lambda symbol, **kwargs: legacy_bundle,
                scanner=lambda *args, **kwargs: self.fail("legacy loader must be adapted and blocked"),
            )

        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual("cache_missing", result["data_errors"][0]["status_reason"])

    def test_legacy_statuses_missing_5m_dependency_are_blocked(self):
        from mu_strategy.market_data.trusted_data.compat import CandleBundle, ensure_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.symbols import ResolvedSymbol

        bundle = CandleBundle(
            symbol=ResolvedSymbol(requested="BTC-USDT-SWAP", inst_id="BTC-USDT-SWAP", source="okx"),
            candles_by_interval={"15m": [Candle(0, 1, 1, 1, 1, 1)], "1h": [Candle(0, 1, 1, 1, 1, 1)]},
            files_by_interval={},
            days=1,
            statuses_by_interval={"15m": _valid_status("15m"), "1h": _valid_status("1h")},
        )

        decision = ensure_trusted_candle_bundle(bundle, requested_intervals=("15m", "1h")).trust_decision

        self.assertFalse(decision.allowed)
        self.assertEqual(HealthReason.CACHE_MISSING, decision.reason)
        self.assertEqual("legacy trusted status missing for 5m", decision.message)

    def test_legacy_statuses_all_effective_intervals_valid_are_allowed(self):
        from mu_strategy.market_data.trusted_data.compat import CandleBundle, ensure_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.symbols import ResolvedSymbol

        bundle = CandleBundle(
            symbol=ResolvedSymbol(requested="BTC-USDT-SWAP", inst_id="BTC-USDT-SWAP", source="okx"),
            candles_by_interval={"15m": [Candle(0, 1, 1, 1, 1, 1)], "1h": [Candle(0, 1, 1, 1, 1, 1)]},
            files_by_interval={},
            days=1,
            statuses_by_interval={interval: _valid_status(interval) for interval in ("5m", "15m", "1h")},
        )

        decision = ensure_trusted_candle_bundle(bundle, requested_intervals=("15m", "1h")).trust_decision

        self.assertTrue(decision.allowed)
        self.assertEqual(HealthReason.OK, decision.reason)

    def test_legacy_statuses_missing_extra_candle_interval_are_blocked(self):
        from mu_strategy.market_data.trusted_data.compat import CandleBundle, ensure_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.symbols import ResolvedSymbol

        bundle = CandleBundle(
            symbol=ResolvedSymbol(requested="BTC-USDT-SWAP", inst_id="BTC-USDT-SWAP", source="okx"),
            candles_by_interval={
                "15m": [Candle(0, 1, 1, 1, 1, 1)],
                "1h": [Candle(0, 1, 1, 1, 1, 1)],
                "4h": [Candle(0, 1, 1, 1, 1, 1)],
            },
            files_by_interval={},
            days=1,
            statuses_by_interval={interval: _valid_status(interval) for interval in ("5m", "15m", "1h")},
        )

        decision = ensure_trusted_candle_bundle(bundle, requested_intervals=("15m", "1h")).trust_decision

        self.assertFalse(decision.allowed)
        self.assertEqual(HealthReason.CACHE_MISSING, decision.reason)
        self.assertEqual("legacy trusted status missing for 4h", decision.message)

    def test_existing_allowed_trust_decision_is_not_rederived_from_legacy_statuses(self):
        from mu_strategy.market_data.trusted_data.compat import CandleBundle, ensure_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, TrustDecision
        from mu_strategy.market_data.symbols import ResolvedSymbol

        decision = TrustDecision(True, HealthReason.OK)
        bundle = CandleBundle(
            symbol=ResolvedSymbol(requested="BTC-USDT-SWAP", inst_id="BTC-USDT-SWAP", source="okx"),
            candles_by_interval={"15m": [Candle(0, 1, 1, 1, 1, 1)]},
            files_by_interval={},
            days=1,
            statuses_by_interval={},
            trust_decision=decision,
        )

        self.assertIs(decision, ensure_trusted_candle_bundle(bundle, requested_intervals=("15m", "1h")).trust_decision)

    def test_existing_blocked_trust_decision_is_not_overridden_by_legacy_statuses(self):
        from mu_strategy.market_data.trusted_data.compat import CandleBundle, ensure_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, TrustDecision
        from mu_strategy.market_data.symbols import ResolvedSymbol

        decision = TrustDecision(False, HealthReason.MANIFEST_STALE, "official block")
        bundle = CandleBundle(
            symbol=ResolvedSymbol(requested="BTC-USDT-SWAP", inst_id="BTC-USDT-SWAP", source="okx"),
            candles_by_interval={"15m": [Candle(0, 1, 1, 1, 1, 1)], "1h": [Candle(0, 1, 1, 1, 1, 1)]},
            files_by_interval={},
            days=1,
            statuses_by_interval={interval: _valid_status(interval) for interval in ("5m", "15m", "1h")},
            trust_decision=decision,
        )

        self.assertIs(decision, ensure_trusted_candle_bundle(bundle, requested_intervals=("15m", "1h")).trust_decision)

    def test_cli_exit_code_contract_for_attempt_and_snapshot_axes(self):
        from mu_strategy.commands.refresh_market_data import classify_refresh_run
        from mu_strategy.market_data.trusted_data.contracts import (
            RefreshAttemptStatus,
            RefreshRun,
            SnapshotUsability,
            UniverseSnapshot,
        )

        cases = [
            (RefreshAttemptStatus.SUCCESS, SnapshotUsability.USABLE, 0),
            (RefreshAttemptStatus.DEGRADED, SnapshotUsability.USABLE, 2),
            (RefreshAttemptStatus.SUCCESS, SnapshotUsability.STALE, 1),
            (RefreshAttemptStatus.DEGRADED, SnapshotUsability.INVALID, 1),
            (RefreshAttemptStatus.FAILED, SnapshotUsability.USABLE, 1),
            (RefreshAttemptStatus.FAILED, SnapshotUsability.INVALID, 1),
        ]
        for attempt_status, snapshot_usability, exit_code in cases:
            with self.subTest(attempt_status=attempt_status, snapshot_usability=snapshot_usability):
                run = RefreshRun(
                    run_id=f"{attempt_status.value}-{snapshot_usability.value}",
                    attempt_status=attempt_status,
                    snapshot_usability=snapshot_usability,
                    started_at_ms=0,
                    completed_at_ms=0,
                    requested_intervals=("5m",),
                    effective_intervals=("5m",),
                    universe_snapshot=UniverseSnapshot(),
                )

                self.assertEqual(exit_code, classify_refresh_run(run).exit_code)

    def test_consumers_have_no_freshness_clock_or_status_gate_fallbacks(self):
        forbidden = {
            "mu_strategy/cli.py": ("trusted_status_error", "FreshnessPolicy", "SystemClock"),
            "mu_strategy/viz/backtest.py": ("trusted_status_error", "FreshnessPolicy", "SystemClock"),
            "mu_strategy/demo_trading.py": ("_legacy_market_data_staleness_error", "FreshnessPolicy", "SystemClock"),
        }
        for relative_path, needles in forbidden.items():
            source = Path(relative_path).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(path=relative_path, needle=needle):
                    self.assertNotIn(needle, source)


def _candles(interval: str) -> list[Candle]:
    five = [Candle(index * 300_000, 100 + index, 101 + index, 99 + index, 100 + index, 10.0) for index in range(12)]
    if interval == "5m":
        return five
    raise AssertionError(f"unexpected interval: {interval}")


def _valid_status(interval: str):
    from mu_strategy.market_data.trusted_data.compat import DataStatus

    return DataStatus(
        symbol="BTC-USDT-SWAP",
        interval=interval,
        rows=1,
        first_timestamp_ms=0,
        last_timestamp_ms=0,
        updated_at_ms=0,
        source_file=Path(f"data/live/okx/BTC-USDT-SWAP/{interval}.csv"),
    )


class _IncrementalFailureProvider:
    def fetch_tickers(self):
        return [{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}]

    def fetch_history(self, symbol, interval, *, days):
        raise AssertionError("cache should force incremental path")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise TimeoutError("blocked incremental")


class _HistoryTimeoutProvider:
    def fetch_tickers(self):
        return [{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}]

    def fetch_history(self, symbol, interval, *, days):
        raise TimeoutError()

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise AssertionError("empty cache should force history path")


class _UniverseTimeoutProvider:
    def fetch_tickers(self):
        raise TimeoutError()

    def fetch_history(self, symbol, interval, *, days):
        raise AssertionError("universe failure must not fetch history")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise AssertionError("universe failure must not fetch incremental")


def _manifest_path(data_dir: Path) -> Path:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore

    current = data_dir / "current.json"
    if current.exists():
        return data_dir / json.loads(current.read_text(encoding="utf-8"))["manifest"]
    return TrustedDataStore(data_dir=data_dir).flat_manifest_path


if __name__ == "__main__":
    unittest.main()
