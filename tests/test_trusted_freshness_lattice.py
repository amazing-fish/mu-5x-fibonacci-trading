import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle


class DatasetHealthUsabilityTests(unittest.TestCase):
    def test_dataset_health_usable_requires_positive_fresh_state(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState

        cases = [
            (FreshnessState.FRESH, True),
            (FreshnessState.STALE, False),
            (FreshnessState.UNKNOWN, False),
        ]
        for freshness, expected in cases:
            with self.subTest(freshness=freshness):
                self.assertEqual(expected, _health("MU-USDT-SWAP", "5m", freshness=freshness).is_usable)

        self.assertFalse(_health("MU-USDT-SWAP", "5m", freshness=FreshnessState.UNKNOWN).is_usable)


class SnapshotUsabilityDerivationTests(unittest.TestCase):
    def test_derive_snapshot_usability_fails_closed_on_unknown_or_unusable_state(self):
        from mu_strategy.market_data.trusted_data.contracts import (
            AvailabilityState,
            FreshnessState,
            IntegrityState,
            SnapshotUsability,
            derive_snapshot_usability,
        )

        cases = [
            (
                "all_fresh",
                {("MU-USDT-SWAP", "5m"): _health("MU-USDT-SWAP", "5m")},
                SnapshotUsability.USABLE,
            ),
            (
                "one_stale",
                {("MU-USDT-SWAP", "5m"): _health("MU-USDT-SWAP", "5m", freshness=FreshnessState.STALE)},
                SnapshotUsability.INVALID,
            ),
            (
                "one_unknown",
                {("MU-USDT-SWAP", "5m"): _health("MU-USDT-SWAP", "5m", freshness=FreshnessState.UNKNOWN)},
                SnapshotUsability.INVALID,
            ),
            (
                "one_invalid",
                {("MU-USDT-SWAP", "5m"): _health("MU-USDT-SWAP", "5m", integrity=IntegrityState.INVALID)},
                SnapshotUsability.INVALID,
            ),
            (
                "one_missing",
                {("MU-USDT-SWAP", "5m"): _health("MU-USDT-SWAP", "5m", availability=AvailabilityState.MISSING)},
                SnapshotUsability.INVALID,
            ),
            ("empty_catalog", {}, SnapshotUsability.INVALID),
        ]
        for name, datasets, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, derive_snapshot_usability(datasets))

    def test_refresh_run_snapshot_usability_delegates_to_derived_lattice(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState

        run = _run(freshness=FreshnessState.UNKNOWN)

        self.assertEqual("invalid", run.snapshot_usability.value)


class ManifestSchemaConsistencyTests(unittest.TestCase):
    def test_schema_v3_rejects_declared_usability_that_disagrees_with_derived_usability(self):
        from mu_strategy.market_data.trusted_data.contracts import ManifestSchemaError, trusted_manifest_snapshot_from_dict

        invalid_cases = [
            ("success_usable_unknown", _manifest(snapshot_usability="usable", freshness="unknown")),
            ("success_usable_empty", _manifest(snapshot_usability="usable", include_datasets=False)),
            ("success_usable_invalid", _manifest(snapshot_usability="usable", integrity="invalid")),
            ("success_stale_all_fresh", _manifest(snapshot_usability="stale")),
            ("success_invalid_all_fresh", _manifest(snapshot_usability="invalid")),
            ("success_stale_zero_usable", _manifest(snapshot_usability="stale", freshness="stale")),
        ]
        for name, payload in invalid_cases:
            with self.subTest(name=name):
                with self.assertRaises(ManifestSchemaError):
                    trusted_manifest_snapshot_from_dict(payload)

    def test_schema_v3_accepts_only_usability_matching_the_derived_lattice(self):
        from mu_strategy.market_data.trusted_data.contracts import trusted_manifest_snapshot_from_dict

        valid_cases = [
            ("success_usable_fresh", _manifest(snapshot_usability="usable")),
            ("success_invalid_stale", _manifest(snapshot_usability="invalid", freshness="stale")),
            ("success_invalid_unknown", _manifest(snapshot_usability="invalid", freshness="unknown")),
            ("success_invalid_integrity", _manifest(snapshot_usability="invalid", integrity="invalid")),
            ("success_invalid_missing", _manifest(snapshot_usability="invalid", availability="missing", integrity="invalid", freshness="unknown")),
            ("degraded_invalid", _manifest(attempt_status="degraded", snapshot_usability="invalid", integrity="invalid")),
            ("failed_invalid", _manifest(attempt_status="failed", snapshot_usability="invalid", integrity="invalid")),
            ("failed_usable", _manifest(attempt_status="failed", snapshot_usability="usable")),
        ]
        for name, payload in valid_cases:
            with self.subTest(name=name):
                snapshot = trusted_manifest_snapshot_from_dict(payload)
                self.assertEqual(payload["snapshot_usability"], snapshot.snapshot_usability.value)


class StrictPolicyUnknownFreshnessTests(unittest.TestCase):
    def test_strict_policy_uses_requested_intervals_for_freshness_when_base_is_validation_only(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason
        from mu_strategy.market_data.trusted_data.policy import research_strict_policy

        context = _context_with_status("stale", attempt_status="degraded")
        health_by_interval = {
            "5m": _health("MU-USDT-SWAP", "5m", freshness=FreshnessState.STALE),
            "15m": _health("MU-USDT-SWAP", "15m"),
            "1h": _health("MU-USDT-SWAP", "1h"),
        }

        decision = research_strict_policy().decide(
            context=context,
            health_by_interval=health_by_interval,
            required_intervals=("5m", "15m", "1h"),
            freshness_intervals=("15m", "1h"),
        )

        self.assertTrue(decision.allowed)

        requested_stale = dict(health_by_interval)
        requested_stale["15m"] = _health("MU-USDT-SWAP", "15m", freshness=FreshnessState.STALE)
        blocked = research_strict_policy().decide(
            context=context,
            health_by_interval=requested_stale,
            required_intervals=("5m", "15m", "1h"),
            freshness_intervals=("15m", "1h"),
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(HealthReason.STALE_BY_CLOCK, blocked.reason)

    def test_strict_policies_block_unknown_freshness_with_non_ok_reason(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason
        from mu_strategy.market_data.trusted_data.policy import (
            TrustPolicy,
            observe_only_policy,
            research_strict_policy,
            trading_strict_policy,
        )

        context = _context_with_status("ok")
        health_by_interval = {"5m": _health("MU-USDT-SWAP", "5m", freshness=FreshnessState.UNKNOWN, reason=HealthReason.OK)}

        policies = (
            trading_strict_policy(),
            research_strict_policy(),
            TrustPolicy(name="custom", require_manifest_success=False, require_fresh=True),
        )
        for policy in policies:
            with self.subTest(policy=policy.name):
                decision = policy.decide(
                    context=context,
                    health_by_interval=health_by_interval,
                    required_intervals=("5m",),
                )
                self.assertFalse(decision.allowed)
                self.assertNotEqual(HealthReason.OK, decision.reason)
                self.assertEqual(HealthReason.FRESHNESS_UNKNOWN, decision.reason)

        observe_decision = observe_only_policy().decide(
            context=context,
            health_by_interval=health_by_interval,
            required_intervals=("5m",),
        )
        self.assertTrue(observe_decision.allowed)

    def test_observe_only_bundle_preserves_unknown_freshness_diagnostics(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, status="invalid", freshness="unknown")
            bundle = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1, now_ms=86_400_000),
                observe_only_policy(),
            )

        self.assertTrue(bundle.trust_decision.allowed)
        self.assertEqual(FreshnessState.UNKNOWN, bundle.health_by_interval["5m"].freshness)
        self.assertFalse(bundle.health_by_interval["5m"].is_usable)


class MalformedUnknownManifestIntegrationTests(unittest.TestCase):
    def test_success_ok_unknown_manifest_read_fails_closed(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, status="ok", freshness="unknown")
            store = TrustedDataStore(data_dir=data_dir)

            manifest_result = store.read_manifest()
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1, now_ms=86_400_000),
                trading_strict_policy(),
            )

        self.assertFalse(manifest_result.ok)
        self.assertEqual(HealthReason.MALFORMED_MANIFEST, manifest_result.reason)
        self.assertFalse(bundle.trust_decision.allowed)
        self.assertEqual(HealthReason.MALFORMED_MANIFEST, bundle.trust_decision.reason)

    def test_cli_does_not_backtest_success_ok_unknown_manifest(self):
        from mu_strategy import cli

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            report_path = Path(tmp) / "report.md"
            _write_manifest_and_caches(data_dir, status="ok", freshness="unknown")
            argv = ["mu_strategy.cli", "--data-dir", str(data_dir), "--report", str(report_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.run_backtest", side_effect=AssertionError("backtest must not run")):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            cli.main()

            self.assertFalse(report_path.exists())

        self.assertNotEqual(0, raised.exception.code)

    def test_visualization_does_not_render_success_ok_unknown_manifest(self):
        from mu_strategy import visualize

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            output_path = Path(tmp) / "chart.html"
            _write_manifest_and_caches(data_dir, status="ok", freshness="unknown")
            argv = ["mu_strategy.visualize", "--data-dir", str(data_dir), "--output", str(output_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.run_backtest", side_effect=AssertionError("backtest must not run")):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            visualize.main()

            self.assertFalse(output_path.exists())

        self.assertNotEqual(0, raised.exception.code)


class DemoUnknownFreshnessGateTests(unittest.TestCase):
    def test_demo_unknown_freshness_manifest_does_not_scan_or_order(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        scan_calls = []
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            _write_manifest_and_caches(data_dir, status="invalid", freshness="unknown")
            result = run_once(
                DemoTradingConfig(
                    data_dir=data_dir,
                    universe_limit=1,
                    dry_run=False,
                    watchlist_symbols=(),
                ),
                broker=_Broker(),
                scanner=lambda *args, **kwargs: scan_calls.append(args) or self.fail("unknown freshness must not scan"),
            )

        self.assertEqual([], scan_calls)
        self.assertEqual([], result["orders"])
        self.assertEqual(1, len(result["data_errors"]))
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual("manifest_invalid", result["data_errors"][0]["status_reason"])


class LegacyCompatibilityFreshnessTests(unittest.TestCase):
    def test_unknown_freshness_maps_to_stale_legacy_status_through_single_adapter(self):
        from mu_strategy.market_data.trusted_data.compat import data_status_from_health
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason

        health = _health("MU-USDT-SWAP", "5m", freshness=FreshnessState.UNKNOWN, reason=HealthReason.OK)

        status = data_status_from_health(health)

        self.assertTrue(status.is_valid)
        self.assertTrue(status.is_stale)
        self.assertNotEqual("ok", status.reason)


class FutureTimestampFreshnessTests(unittest.TestCase):
    def test_future_confirmed_candle_is_unknown_and_blocks_strict_policies(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, HealthReason
        from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy, observe_only_policy, trading_strict_policy

        assessment = FreshnessPolicy(max_staleness_bars=3).assess(
            now_ms=1_000,
            interval="5m",
            last_confirmed_open_time_ms=2_000,
        )
        health = _health(
            "MU-USDT-SWAP",
            "5m",
            freshness=assessment.state,
            reason=assessment.reason,
        )
        context = _context_with_status("ok")

        strict = trading_strict_policy().decide(context=context, health_by_interval={"5m": health}, required_intervals=("5m",))
        observe = observe_only_policy().decide(context=context, health_by_interval={"5m": health}, required_intervals=("5m",))
        run = _run(freshness=assessment.state, reason=assessment.reason)

        self.assertEqual(FreshnessState.UNKNOWN, assessment.state)
        self.assertEqual(HealthReason.FUTURE_TIMESTAMP, assessment.reason)
        self.assertFalse(strict.allowed)
        self.assertEqual(HealthReason.FUTURE_TIMESTAMP, strict.reason)
        self.assertTrue(observe.allowed)
        self.assertEqual("invalid", run.snapshot_usability.value)


class RefreshCommandUnknownFreshnessTests(unittest.TestCase):
    def test_one_shot_command_result_marks_unknown_freshness_publication_unusable(self):
        from mu_strategy.commands.refresh_market_data import REFRESH_COMMAND_UNUSABLE_EXIT_CODE, classify_refresh_run
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState

        result = classify_refresh_run(_run(freshness=FreshnessState.UNKNOWN))

        self.assertFalse(result.usable)
        self.assertEqual("invalid", result.snapshot_usability)
        self.assertEqual(REFRESH_COMMAND_UNUSABLE_EXIT_CODE, result.exit_code)


def _health(symbol, interval, *, availability=None, integrity=None, freshness=None, reason=None):
    from mu_strategy.market_data.trusted_data.contracts import (
        AvailabilityState,
        DatasetHealth,
        DatasetKey,
        FreshnessState,
        HealthReason,
        IntegrityState,
        ValidationReport,
    )

    availability = availability or AvailabilityState.AVAILABLE
    integrity = integrity or IntegrityState.VALID
    freshness = freshness or FreshnessState.FRESH
    if reason is None:
        if availability == AvailabilityState.MISSING:
            reason = HealthReason.CACHE_MISSING
        elif integrity == IntegrityState.INVALID:
            reason = HealthReason.REFRESH_FAILED
        elif freshness == FreshnessState.STALE:
            reason = HealthReason.STALE_BY_CLOCK
        elif freshness == FreshnessState.UNKNOWN:
            reason = HealthReason.FRESHNESS_UNKNOWN
        else:
            reason = HealthReason.OK
    rows = 1 if availability == AvailabilityState.AVAILABLE else 0
    return DatasetHealth(
        key=DatasetKey(symbol, interval),
        availability=availability,
        integrity=integrity,
        freshness=freshness,
        reasons=(reason,),
        rows=rows,
        first_timestamp_ms=0 if rows else None,
        last_timestamp_ms=0 if rows else None,
        source_file=Path(f"data/live/okx/{symbol}/{interval}.csv"),
        validation=ValidationReport(ok=integrity == IntegrityState.VALID, reason=HealthReason.OK if integrity == IntegrityState.VALID else reason),
    )


def _context_with_status(status: str, *, attempt_status: str = "success"):
    from mu_strategy.market_data.trusted_data.contracts import (
        RefreshAttemptStatus,
        SnapshotUsability,
        TrustedLoadContext,
        TrustedManifestSnapshot,
        UniverseSnapshot,
    )
    snapshot_usability = "usable" if status == "ok" else status

    return TrustedLoadContext(
        manifest=TrustedManifestSnapshot(
            schema_version=3,
            run_id=f"run-{status}",
            attempt_status=RefreshAttemptStatus(attempt_status),
            snapshot_usability=SnapshotUsability(snapshot_usability),
            started_at_ms=0,
            completed_at_ms=0,
            requested_intervals=("5m",),
            effective_intervals=("5m",),
            universe_snapshot=UniverseSnapshot(),
            datasets={("MU-USDT-SWAP", "5m"): _health("MU-USDT-SWAP", "5m")},
        ),
        observed_at_ms=0,
    )


def _run(*, freshness=None, reason=None):
    from mu_strategy.market_data.trusted_data.contracts import (
        RefreshAttemptStatus,
        RefreshRun,
        UniverseSnapshot,
        derive_snapshot_usability,
    )

    health = _health("MU-USDT-SWAP", "5m", freshness=freshness, reason=reason)
    datasets = {("MU-USDT-SWAP", "5m"): health}
    return RefreshRun(
        run_id="run-lattice",
        attempt_status=RefreshAttemptStatus.SUCCESS,
        snapshot_usability=derive_snapshot_usability(datasets),
        started_at_ms=0,
        completed_at_ms=0,
        requested_intervals=("5m",),
        effective_intervals=("5m",),
        universe_snapshot=UniverseSnapshot(crypto_top=({"inst_id": "MU-USDT-SWAP", "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"},)),
        datasets=datasets,
    )


def _manifest(
    *,
    attempt_status: str = "success",
    snapshot_usability: str,
    availability: str = "available",
    integrity: str = "valid",
    freshness: str = "fresh",
    include_datasets: bool = True,
) -> dict:
    symbols = {}
    if include_datasets:
        symbols = {"MU-USDT-SWAP": {"intervals": {"5m": _health_payload(availability=availability, integrity=integrity, freshness=freshness)}}}
    return {
        "schema_version": 3,
        "run_id": f"{attempt_status}-{snapshot_usability}",
        "attempt_status": attempt_status,
        "snapshot_usability": snapshot_usability,
        "started_at_ms": 0,
        "completed_at_ms": 86_400_000,
        "requested_intervals": ["5m"],
        "effective_intervals": ["5m"],
        "universes": {"crypto_top": [], "stock_token_top": []},
        "symbols": symbols,
        "provider_failures": [],
        "warnings": [],
        "cycle_error": None,
    }


def _health_payload(*, availability: str = "available", integrity: str = "valid", freshness: str = "fresh", interval: str = "5m") -> dict:
    rows = 1 if availability == "available" else 0
    reason = "ok"
    if availability == "missing":
        reason = "cache_missing"
    elif integrity == "invalid":
        reason = "refresh_failed"
    elif freshness == "stale":
        reason = "stale_by_clock"
    elif freshness == "unknown":
        reason = "freshness_unknown"
    return {
        "symbol": "MU-USDT-SWAP",
        "interval": interval,
        "availability": availability,
        "integrity": integrity,
        "freshness": freshness,
        "reasons": [reason],
        "rows": rows,
        "first_timestamp_ms": 0 if rows else None,
        "last_timestamp_ms": 0 if rows else None,
        "updated_at_ms": 86_400_000,
        "source_file": f"data/live/okx/MU-USDT-SWAP/{interval}.csv",
        "validation": {"ok": integrity == "valid", "reason": "ok" if integrity == "valid" else reason},
    }


def _write_manifest_and_caches(data_dir: Path, *, status: str, freshness: str) -> None:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles
    from mu_strategy.market_data.utils import DAY_MS

    store = TrustedDataStore(data_dir=data_dir)
    five = [Candle(index * 300_000, 100 + index, 101 + index, 99 + index, 100 + index, 10.0) for index in range(DAY_MS // 300_000)]
    by_interval = {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }
    symbols = {"MU-USDT-SWAP": {"intervals": {}}}
    for interval, candles in by_interval.items():
        path = store.flat_cache_path("MU-USDT-SWAP", interval)
        store.write_csv(candles, path)
        health = _health_payload(freshness=freshness, interval=interval)
        health.update(
            {
                "rows": len(candles),
                "first_timestamp_ms": candles[0].open_time_ms,
                "last_timestamp_ms": candles[-1].open_time_ms,
                "source_file": str(path),
                "content_sha256": candles_content_sha256(candles),
            }
        )
        symbols["MU-USDT-SWAP"]["intervals"][interval] = health
    store.write_manifest(
        {
            "schema_version": 3,
            "run_id": f"run-{status}-{freshness}",
            "attempt_status": "success",
            "snapshot_usability": "usable" if status == "ok" else status,
            "started_at_ms": 0,
            "completed_at_ms": 86_400_000,
            "requested_intervals": ["5m", "15m", "1h"],
            "effective_intervals": ["5m", "15m", "1h"],
            "universes": {
                "crypto_top": [{"inst_id": "MU-USDT-SWAP", "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"}],
                "stock_token_top": [],
            },
            "symbols": symbols,
            "provider_failures": [],
            "warnings": [],
            "cycle_error": None,
        }
    )


class _Broker:
    def get_positions(self, *, inst_type=None, inst_id=None):
        return {"code": "0", "data": [], "msg": ""}

    def get_open_orders(self, *, inst_type=None, inst_id=None):
        return {"code": "0", "data": [], "msg": ""}

    def get_instruments(self, *, inst_type, inst_id):
        raise AssertionError("orders must not reach instrument lookup")


if __name__ == "__main__":
    unittest.main()
