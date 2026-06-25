import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mu_strategy.market_data.trusted_data.validation import aggregate_candles
from mu_strategy.market_data.utils import DAY_MS
from mu_strategy.models import Candle


FIVE_MINUTES_MS = 300_000
FIFTEEN_MINUTES_MS = 900_000
ONE_HOUR_MS = 3_600_000
SYMBOL = "BTC-USDT-SWAP"
SECOND_SYMBOL = "ETH-USDT-SWAP"


class TrustedDataSharedWindowRefreshTests(unittest.TestCase):
    def test_warm_cache_refresh_uses_shared_window_before_native_validation(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        five_end = 2 * DAY_MS + 55 * 60_000
        five = _five_minute_candles(0, five_end)
        bundle = _confirmed_bundle(five, fifteen_end=2 * DAY_MS + 45 * 60_000, hour_end=2 * DAY_MS)

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _write_bundle(store, SYMBOL, bundle)
            run = RefreshTrustedMarketData(store, _Provider(_ticker_rows(SYMBOL))).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=five_end,
                )
            )
            manifest = _manifest(store)

            self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
            self.assertEqual("usable", manifest["snapshot_usability"])
            for interval in ("15m", "1h"):
                health = run.datasets[(SYMBOL, interval)]
                self.assertTrue(health.validation.ok)
                self.assertNotEqual(HealthReason.MISSING_IN_BUILT, health.primary_reason)
                self.assertNotEqual(HealthReason.MISSING_IN_NATIVE, health.primary_reason)
            _assert_health_matches_csv(self, store, run, SYMBOL, ("5m", "15m", "1h"))

    def test_shared_start_boundary_drops_first_incomplete_built_bucket(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        five_end = 2 * DAY_MS + 55 * 60_000
        five = _five_minute_candles(0, five_end)
        bundle = _confirmed_bundle(five, fifteen_end=2 * DAY_MS + 45 * 60_000, hour_end=2 * DAY_MS)

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _write_bundle(store, SYMBOL, bundle)
            run = RefreshTrustedMarketData(store, _Provider(_ticker_rows(SYMBOL))).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=five_end,
                )
            )

            persisted_five = store.read_csv(store.generation_cache_path(run.run_id, SYMBOL, "5m"))
            persisted_15m = store.read_csv(store.generation_cache_path(run.run_id, SYMBOL, "15m"))
            persisted_1h = store.read_csv(store.generation_cache_path(run.run_id, SYMBOL, "1h"))

        self.assertEqual(DAY_MS + ONE_HOUR_MS, persisted_15m[0].open_time_ms)
        self.assertEqual(DAY_MS + ONE_HOUR_MS, persisted_1h[0].open_time_ms)
        self.assertEqual(
            [candle.open_time_ms for candle in persisted_15m],
            [candle.open_time_ms for candle in aggregate_candles(persisted_five, interval="15m")],
        )
        self.assertEqual(
            [candle.open_time_ms for candle in persisted_1h],
            [candle.open_time_ms for candle in aggregate_candles(persisted_five, interval="1h")],
        )
        self.assertTrue(run.datasets[(SYMBOL, "15m")].validation.ok)
        self.assertTrue(run.datasets[(SYMBOL, "1h")].validation.ok)

    def test_partial_right_boundary_does_not_create_false_native_mismatch(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        five_end = 2 * DAY_MS + 5 * 60_000
        five = _five_minute_candles(0, five_end)
        bundle = _confirmed_bundle(five, fifteen_end=2 * DAY_MS - FIFTEEN_MINUTES_MS, hour_end=2 * DAY_MS - ONE_HOUR_MS)

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _write_bundle(store, SYMBOL, bundle)
            run = RefreshTrustedMarketData(store, _Provider(_ticker_rows(SYMBOL))).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=five_end,
                )
            )
            persisted_five = store.read_csv(store.generation_cache_path(run.run_id, SYMBOL, "5m"))
            persisted_15m = store.read_csv(store.generation_cache_path(run.run_id, SYMBOL, "15m"))
            persisted_1h = store.read_csv(store.generation_cache_path(run.run_id, SYMBOL, "1h"))

        self.assertTrue(run.datasets[(SYMBOL, "15m")].validation.ok)
        self.assertTrue(run.datasets[(SYMBOL, "1h")].validation.ok)
        self.assertEqual(
            [candle.open_time_ms for candle in persisted_15m],
            [candle.open_time_ms for candle in aggregate_candles(persisted_five, interval="15m")],
        )
        self.assertEqual(
            [candle.open_time_ms for candle in persisted_1h],
            [candle.open_time_ms for candle in aggregate_candles(persisted_five, interval="1h")],
        )
        self.assertLess(persisted_15m[-1].open_time_ms, five_end)
        self.assertLess(persisted_1h[-1].open_time_ms, five_end)

    def test_cold_history_refresh_uses_shared_window_before_native_validation(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        five_end = 2 * DAY_MS + 55 * 60_000
        five = _five_minute_candles(0, five_end)
        history = {
            (SYMBOL, "5m"): five,
            (SYMBOL, "15m"): _native_until(five, "15m", 2 * DAY_MS + 45 * 60_000),
            (SYMBOL, "1h"): _native_until(five, "1h", 2 * DAY_MS),
        }

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            run = RefreshTrustedMarketData(store, _Provider(_ticker_rows(SYMBOL), history=history)).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=five_end,
                )
            )

            self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
            self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
            self.assertTrue(run.datasets[(SYMBOL, "15m")].validation.ok)
            self.assertTrue(run.datasets[(SYMBOL, "1h")].validation.ok)
            _assert_health_matches_csv(self, store, run, SYMBOL, ("5m", "15m", "1h"))

    def test_internal_missing_five_minute_bucket_still_fails_exact_validation(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        five_end = 2 * DAY_MS + 55 * 60_000
        complete_five = _five_minute_candles(0, five_end)
        missing_timestamp = DAY_MS + 2 * ONE_HOUR_MS + 5 * 60_000
        broken_five = [candle for candle in complete_five if candle.open_time_ms != missing_timestamp]
        history = {
            (SYMBOL, "5m"): broken_five,
            (SYMBOL, "15m"): _native_until(complete_five, "15m", 2 * DAY_MS + 45 * 60_000),
        }

        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), _Provider(_ticker_rows(SYMBOL), history=history)).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=five_end,
                )
            )

        self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        health = run.datasets[(SYMBOL, "15m")]
        self.assertFalse(health.validation.ok)
        self.assertEqual(HealthReason.MISSING_IN_BUILT, health.validation.reason)
        self.assertIn(DAY_MS + 2 * ONE_HOUR_MS, health.validation.missing_in_built)

    def test_internal_missing_native_candle_still_fails_exact_validation(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        five_end = 2 * DAY_MS + 55 * 60_000
        five = _five_minute_candles(0, five_end)
        missing_native = DAY_MS + 2 * ONE_HOUR_MS
        native_15m = [
            candle
            for candle in _native_until(five, "15m", 2 * DAY_MS + 45 * 60_000)
            if candle.open_time_ms != missing_native
        ]
        history = {
            (SYMBOL, "5m"): five,
            (SYMBOL, "15m"): native_15m,
        }

        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), _Provider(_ticker_rows(SYMBOL), history=history)).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=five_end,
                )
            )

        self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        health = run.datasets[(SYMBOL, "15m")]
        self.assertFalse(health.validation.ok)
        self.assertEqual(HealthReason.MISSING_IN_NATIVE, health.validation.reason)
        self.assertIn(missing_native, health.validation.missing_in_native)

    def test_failed_base_incremental_refresh_keeps_fresh_cache_with_warning(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        five_end = 2 * DAY_MS + 55 * 60_000
        five = _five_minute_candles(0, five_end)
        bundle = _confirmed_bundle(five, fifteen_end=2 * DAY_MS + 45 * 60_000, hour_end=2 * DAY_MS)
        provider = _Provider(_ticker_rows(SYMBOL), fail_incremental={(SYMBOL, "5m")})

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _write_bundle(store, SYMBOL, bundle)
            run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=five_end,
                )
            )
            manifest = _manifest(store)

        self.assertEqual(RefreshAttemptStatus.DEGRADED, run.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
        self.assertEqual("usable", manifest["snapshot_usability"])
        base_health = run.datasets[(SYMBOL, "5m")]
        native_health = run.datasets[(SYMBOL, "15m")]
        self.assertEqual(HealthReason.OK, base_health.primary_reason)
        self.assertTrue(base_health.is_usable)
        self.assertIn("incremental_refresh_failed", base_health.warnings)
        self.assertTrue(native_health.validation.ok)
        self.assertIn("incremental_refresh_failed", manifest["symbols"][SYMBOL]["intervals"]["5m"]["warnings"])

    def test_loader_and_refresh_share_window_planner_contract(self):
        from mu_strategy.market_data.trusted_data import load
        from mu_strategy.market_data.trusted_data.windowing import assess_requested_coverage, prune_candle_bundle, resolve_shared_window

        five_end = 2 * DAY_MS + 55 * 60_000
        five = _five_minute_candles(0, five_end)
        raw = _confirmed_bundle(five, fifteen_end=2 * DAY_MS + 45 * 60_000, hour_end=2 * DAY_MS)

        plan = resolve_shared_window(raw, days=1)
        pruned = prune_candle_bundle(raw, plan=plan)
        load_source = Path(load.__file__).read_text(encoding="utf-8")

        self.assertEqual(1, plan.days)
        self.assertEqual(five_end, plan.end_time_ms)
        self.assertEqual(DAY_MS + 55 * 60_000, pruned["5m"][0].open_time_ms)
        self.assertEqual(DAY_MS + ONE_HOUR_MS, pruned["15m"][0].open_time_ms)
        self.assertEqual(DAY_MS + ONE_HOUR_MS, pruned["1h"][0].open_time_ms)
        self.assertNotIn("def _shared_window_end", load_source)
        self.assertIn("resolve_shared_window", load_source)
        self.assertIn("assess_requested_coverage", load_source)

        expected_start_ms = five_end - DAY_MS
        covered = assess_requested_coverage(
            _five_minute_candles(expected_start_ms + FIVE_MINUTES_MS, five_end),
            interval="5m",
            requested_days=1,
            window_end_time_ms=five_end,
        )
        short = assess_requested_coverage(
            _five_minute_candles(expected_start_ms + 2 * FIVE_MINUTES_MS, five_end),
            interval="5m",
            requested_days=1,
            window_end_time_ms=five_end,
        )
        missing_anchor = assess_requested_coverage([], interval="5m", requested_days=1, window_end_time_ms=None)
        self.assertTrue(covered.covered)
        self.assertFalse(short.covered)
        self.assertIn("requested_days=1", short.message)
        self.assertIsNone(missing_anchor.expected_start_ms)
        self.assertTrue(missing_anchor.covered)

    def test_multiple_symbols_resolve_independent_shared_windows(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        first_end = 2 * DAY_MS + 55 * 60_000
        second_end = 2 * DAY_MS + 40 * 60_000
        first_five = _five_minute_candles(0, first_end)
        second_five = _five_minute_candles(0, second_end)

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            _write_bundle(store, SYMBOL, _confirmed_bundle(first_five, fifteen_end=2 * DAY_MS + 45 * 60_000, hour_end=2 * DAY_MS))
            _write_bundle(store, SECOND_SYMBOL, _confirmed_bundle(second_five, fifteen_end=2 * DAY_MS + 30 * 60_000, hour_end=2 * DAY_MS))
            run = RefreshTrustedMarketData(store, _Provider(_ticker_rows(SYMBOL, SECOND_SYMBOL))).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=1,
                    limit=2,
                    stock_token_inst_ids=set(),
                    now_ms=first_end,
                )
            )
            first_15m = store.read_csv(store.generation_cache_path(run.run_id, SYMBOL, "15m"))
            second_15m = store.read_csv(store.generation_cache_path(run.run_id, SECOND_SYMBOL, "15m"))

        self.assertEqual(RefreshAttemptStatus.SUCCESS, run.attempt_status)
        self.assertEqual(SnapshotUsability.USABLE, run.snapshot_usability)
        self.assertTrue(run.datasets[(SYMBOL, "15m")].validation.ok)
        self.assertTrue(run.datasets[(SECOND_SYMBOL, "15m")].validation.ok)
        self.assertEqual(DAY_MS + ONE_HOUR_MS, first_15m[0].open_time_ms)
        self.assertEqual(DAY_MS + 45 * 60_000, second_15m[0].open_time_ms)


def _ticker_rows(*symbols: str) -> list[dict]:
    return [
        {"instId": symbol, "last": str(100 - index), "volCcy24h": str(100 - index)}
        for index, symbol in enumerate(symbols)
    ]


def _five_minute_candles(start_ms: int, end_ms: int) -> list[Candle]:
    candles = []
    for timestamp in range(start_ms, end_ms + FIVE_MINUTES_MS, FIVE_MINUTES_MS):
        index = timestamp // FIVE_MINUTES_MS
        price = 100.0 + index * 0.001
        candles.append(Candle(timestamp, price, price + 0.01, price - 0.01, price + 0.0005, 10.0 + index * 0.001))
    return candles


def _native_until(five: list[Candle], interval: str, end_ms: int) -> list[Candle]:
    return [candle for candle in aggregate_candles(five, interval=interval) if candle.open_time_ms <= end_ms]


def _confirmed_bundle(
    five: list[Candle],
    *,
    fifteen_end: int,
    hour_end: int,
) -> dict[str, list[Candle]]:
    return {
        "5m": list(five),
        "15m": _native_until(five, "15m", fifteen_end),
        "1h": _native_until(five, "1h", hour_end),
    }


def _write_bundle(store, symbol: str, bundle: dict[str, list[Candle]]) -> None:
    from mu_strategy.market_data.trusted_data.store import candles_content_sha256

    manifest_path = store.flat_manifest_path
    symbols = {}
    if manifest_path.exists():
        symbols = json.loads(manifest_path.read_text(encoding="utf-8")).get("symbols") or {}
    symbols.setdefault(symbol, {"intervals": {}})
    for interval, candles in bundle.items():
        path = store.flat_cache_path(symbol, interval)
        store.write_csv(candles, path)
        symbols[symbol]["intervals"][interval] = {
            "symbol": symbol,
            "interval": interval,
            "availability": "available",
            "integrity": "valid",
            "freshness": "fresh",
            "reasons": ["ok"],
            "rows": len(candles),
            "first_timestamp_ms": candles[0].open_time_ms,
            "last_timestamp_ms": candles[-1].open_time_ms,
            "updated_at_ms": candles[-1].open_time_ms,
            "source_file": str(path),
            "content_sha256": candles_content_sha256(candles),
            "validation": {"ok": True, "reason": "ok"},
        }
    store.write_manifest(
        {
            "schema_version": 3,
            "run_id": "flat-windowing",
            "attempt_status": "success",
            "snapshot_usability": "usable",
            "started_at_ms": 0,
            "completed_at_ms": 0,
            "requested_intervals": ["15m", "1h"],
            "effective_intervals": ["5m", "15m", "1h"],
            "universes": {"crypto_top": [], "stock_token_top": []},
            "symbols": symbols,
            "provider_failures": [],
            "warnings": [],
            "cycle_error": None,
        }
    )


def _manifest(store) -> dict:
    current = store.current_path
    if current.exists():
        return json.loads((store.data_dir / json.loads(current.read_text(encoding="utf-8"))["manifest"]).read_text(encoding="utf-8"))
    return json.loads(store.flat_manifest_path.read_text(encoding="utf-8"))


def _assert_health_matches_csv(self, store, run, symbol: str, intervals: tuple[str, ...]) -> None:
    for interval in intervals:
        with self.subTest(symbol=symbol, interval=interval):
            health = run.datasets[(symbol, interval)]
            csv_rows = store.read_csv(store.generation_cache_path(run.run_id, symbol, interval))
            self.assertEqual(len(csv_rows), health.rows)
            self.assertEqual(csv_rows[0].open_time_ms, health.first_timestamp_ms)
            self.assertEqual(csv_rows[-1].open_time_ms, health.last_timestamp_ms)


class _Provider:
    def __init__(
        self,
        ticker_rows: list[dict],
        *,
        history: dict[tuple[str, str], list[Candle]] | None = None,
        incremental: dict[tuple[str, str], list[Candle]] | None = None,
        fail_history: set[tuple[str, str]] | None = None,
        fail_incremental: set[tuple[str, str]] | None = None,
    ):
        self.ticker_rows = ticker_rows
        self.history = history or {}
        self.incremental = incremental or {}
        self.fail_history = fail_history or set()
        self.fail_incremental = fail_incremental or set()

    def fetch_tickers(self) -> list[dict]:
        return list(self.ticker_rows)

    def fetch_history(self, symbol: str, interval: str, *, days: int) -> list[Candle]:
        if (symbol, interval) in self.fail_history:
            raise TimeoutError(f"history failed {symbol} {interval}")
        return list(self.history.get((symbol, interval), []))

    def fetch_incremental(self, symbol: str, interval: str, *, since_time_ms: int) -> list[Candle]:
        if (symbol, interval) in self.fail_incremental:
            raise TimeoutError(f"incremental failed {symbol} {interval}")
        return list(self.incremental.get((symbol, interval), []))


if __name__ == "__main__":
    unittest.main()
