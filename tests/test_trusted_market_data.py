import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle
from mu_strategy.market_data.trusted_data.store import TrustedDataStore


class TrustedMarketDataUniverseTests(unittest.TestCase):
    def test_selects_crypto_top10_excluding_stock_token_candidates(self):
        from mu_strategy.market_data.trusted import select_top_okx_crypto_swaps, select_top_okx_stock_tokens

        rows = [
            {"instId": "BTC-USDT-SWAP", "last": "65000", "volCcy24h": "10"},
            {"instId": "ETH-USDT-SWAP", "last": "3000", "volCcy24h": "20"},
            {"instId": "MU-USDT-SWAP", "last": "5", "volCcy24h": "1000000"},
            {"instId": "SPCX-USDT-SWAP", "last": "1", "volCcy24h": "2"},
            {"instId": "BTC-USD-SWAP", "last": "65000", "volCcy24h": "999"},
        ]

        crypto = select_top_okx_crypto_swaps(rows, stock_token_inst_ids={"MU-USDT-SWAP", "SPCX-USDT-SWAP"}, limit=2)
        stocks = select_top_okx_stock_tokens(rows, stock_token_inst_ids={"MU-USDT-SWAP", "SPCX-USDT-SWAP"}, limit=2)

        self.assertEqual(["BTC-USDT-SWAP", "ETH-USDT-SWAP"], [item.inst_id for item in crypto])
        self.assertEqual(["MU-USDT-SWAP", "SPCX-USDT-SWAP"], [item.inst_id for item in stocks])
        self.assertEqual(["top", "top"], [item.source for item in crypto])
        self.assertEqual(["stock_token", "stock_token"], [item.source for item in stocks])

    def test_selectors_honor_zero_and_reject_negative_limits(self):
        from mu_strategy.market_data.trusted import select_top_okx_crypto_swaps, select_top_okx_stock_tokens
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketDataRequest

        rows = [
            {"instId": "MU-USDT-SWAP", "last": "5", "volCcy24h": "1000000"},
            {"instId": "BTC-USDT-SWAP", "last": "65000", "volCcy24h": "10"},
        ]

        self.assertEqual([], select_top_okx_stock_tokens(rows, stock_token_inst_ids={"MU-USDT-SWAP"}, limit=0))
        self.assertEqual([], select_top_okx_crypto_swaps(rows, stock_token_inst_ids={"MU-USDT-SWAP"}, limit=0))
        with self.assertRaisesRegex(ValueError, "limit"):
            select_top_okx_stock_tokens(rows, stock_token_inst_ids={"MU-USDT-SWAP"}, limit=-1)
        with self.assertRaisesRegex(ValueError, "limit"):
            select_top_okx_crypto_swaps(rows, stock_token_inst_ids={"MU-USDT-SWAP"}, limit=-1)
        with self.assertRaisesRegex(ValueError, "limit"):
            RefreshTrustedMarketDataRequest(limit=-1)

    def test_request_normalizes_and_dedupes_explicit_symbols(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketDataRequest

        request = RefreshTrustedMarketDataRequest(symbols=("MU", "MU-USDT-SWAP", "mu_usdt_swap"))

        self.assertEqual(("MU-USDT-SWAP",), request.symbols)

    def test_request_rejects_invalid_explicit_symbol(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketDataRequest

        with self.assertRaisesRegex(ValueError, "symbol"):
            RefreshTrustedMarketDataRequest(symbols=("!!!",))

    def test_request_defaults_to_serial_compatibility_and_accepts_concurrency(self):
        from mu_strategy.market_data.trusted_data.refresh import (
            DEFAULT_MAX_CONCURRENCY,
            DEFAULT_REQUEST_MAX_CONCURRENCY,
            RefreshTrustedMarketDataRequest,
        )

        self.assertEqual(2, DEFAULT_MAX_CONCURRENCY)
        self.assertEqual(1, DEFAULT_REQUEST_MAX_CONCURRENCY)
        self.assertEqual(DEFAULT_REQUEST_MAX_CONCURRENCY, RefreshTrustedMarketDataRequest().max_concurrency)
        self.assertEqual(DEFAULT_MAX_CONCURRENCY, RefreshTrustedMarketDataRequest(max_concurrency=2).max_concurrency)

    def test_request_rejects_non_positive_concurrency(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketDataRequest

        for value in (0, -1):
            with self.subTest(max_concurrency=value):
                with self.assertRaisesRegex(ValueError, "max_concurrency must be positive"):
                    RefreshTrustedMarketDataRequest(max_concurrency=value)

    def test_request_preserves_existing_positional_symbol_argument(self):
        from mu_strategy.market_data.trusted_data.refresh import (
            DEFAULT_REQUEST_MAX_CONCURRENCY,
            RefreshTrustedMarketDataRequest,
        )

        request = RefreshTrustedMarketDataRequest(("5m",), 1, 10, ("MU",))

        self.assertEqual(("MU-USDT-SWAP",), request.symbols)
        self.assertEqual(DEFAULT_REQUEST_MAX_CONCURRENCY, request.max_concurrency)


class TrustedCandleValidationTests(unittest.TestCase):
    def test_validate_built_native_rejects_empty_built(self):
        from mu_strategy.market_data.trusted import validate_built_native_candles

        result = validate_built_native_candles([], [_candle(0, 100)], interval="15m")

        self.assertFalse(result.ok)
        self.assertEqual("built_empty", result.reason)

    def test_validate_built_native_rejects_empty_native(self):
        from mu_strategy.market_data.trusted import validate_built_native_candles

        result = validate_built_native_candles([_candle(0, 100)], [], interval="15m")

        self.assertFalse(result.ok)
        self.assertEqual("native_empty", result.reason)

    def test_validate_built_native_reports_native_timestamp_missing_from_built(self):
        from mu_strategy.market_data.trusted import validate_built_native_candles

        result = validate_built_native_candles([_candle(0, 100)], [_candle(0, 100), _candle(900_000, 101)], interval="15m")

        self.assertFalse(result.ok)
        self.assertEqual("missing_in_built", result.reason)
        self.assertEqual([900_000], result.missing_in_built)

    def test_validate_built_native_reports_built_timestamp_missing_from_native(self):
        from mu_strategy.market_data.trusted import validate_built_native_candles

        result = validate_built_native_candles([_candle(0, 100), _candle(900_000, 101)], [_candle(0, 100)], interval="15m")

        self.assertFalse(result.ok)
        self.assertEqual("missing_in_native", result.reason)
        self.assertEqual([900_000], result.missing_in_native)

    def test_validate_built_native_rejects_misaligned_timestamp(self):
        from mu_strategy.market_data.trusted import validate_built_native_candles

        result = validate_built_native_candles([_candle(60_000, 100)], [_candle(60_000, 100)], interval="15m")

        self.assertFalse(result.ok)
        self.assertEqual("timestamp_misaligned", result.reason)
        self.assertEqual([60_000], result.misaligned_timestamps)

    def test_validate_built_native_accepts_matching_coverage(self):
        from mu_strategy.market_data.trusted import validate_built_native_candles

        result = validate_built_native_candles([_candle(0, 100), _candle(900_000, 101)], [_candle(0, 100), _candle(900_000, 101)], interval="15m")

        self.assertTrue(result.ok)
        self.assertEqual("ok", result.reason)

    def test_validate_built_native_rejects_ohlcv_mismatch(self):
        from mu_strategy.market_data.trusted import validate_built_native_candles

        built = [Candle(0, 100, 110, 90, 105, 123)]
        native = [Candle(0, 100, 111, 90, 105, 123)]

        result = validate_built_native_candles(built, native, interval="15m")

        self.assertFalse(result.ok)
        self.assertEqual("ohlcv_mismatch", result.reason)
        self.assertEqual([{"timestamp_ms": 0, "field": "high", "built": 110, "native": 111}], result.value_mismatches)

    def test_validate_built_native_accepts_price_and_volume_within_tolerance(self):
        from mu_strategy.market_data.trusted import validate_built_native_candles

        built = [Candle(0, 100.0, 110.0, 90.0, 105.0, 123.0)]
        native = [Candle(0, 100.0001, 110.0001, 90.0001, 105.0001, 123.0001)]

        result = validate_built_native_candles(
            built,
            native,
            interval="15m",
            value_rel_tol=1e-5,
            value_abs_tol=1e-5,
        )

        self.assertTrue(result.ok)
        self.assertEqual("ok", result.reason)

    def test_okx_native_parent_ignores_zero_volume_child_ohlc_but_preserves_volume(self):
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles

        five = [
            Candle(0, 555.33, 555.33, 555.33, 555.33, 0.0),
            Candle(300_000, 555.00, 555.00, 555.00, 555.00, 0.16),
            Candle(600_000, 555.00, 555.00, 555.00, 555.00, 0.28),
        ]

        standard = aggregate_candles(five, interval="15m")
        okx_native = aggregate_candles(five, interval="15m", ohlc_policy="okx_native")

        self.assertEqual(555.33, standard[0].open)
        self.assertEqual(555.00, okx_native[0].open)
        self.assertEqual(555.00, okx_native[0].high)
        self.assertEqual(555.00, okx_native[0].low)
        self.assertEqual(555.00, okx_native[0].close)
        self.assertAlmostEqual(0.44, okx_native[0].volume)

    def test_okx_native_parent_preserves_consistent_all_zero_no_trade_bucket(self):
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles

        five = [
            Candle(0, 100.0, 100.0, 100.0, 100.0, 0.0),
            Candle(300_000, 100.0, 100.0, 100.0, 100.0, 0.0),
            Candle(600_000, 100.0, 100.0, 100.0, 100.0, 0.0),
        ]

        result = aggregate_candles(five, interval="15m", ohlc_policy="okx_native")

        self.assertEqual([Candle(0, 100.0, 100.0, 100.0, 100.0, 0.0)], result)

    def test_okx_native_parent_rejects_inconsistent_all_zero_no_trade_bucket(self):
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles

        five = [
            Candle(0, 100.0, 100.0, 100.0, 100.0, 0.0),
            Candle(300_000, 101.0, 101.0, 101.0, 101.0, 0.0),
            Candle(600_000, 100.0, 100.0, 100.0, 100.0, 0.0),
        ]

        with self.assertRaisesRegex(ValueError, "inconsistent all-zero"):
            aggregate_candles(five, interval="15m", ohlc_policy="okx_native")

    def test_normalize_rejects_nan_ohlc_prices(self):
        for field in ("open", "high", "low", "close"):
            with self.subTest(field=field):
                report = _normalize_report([_ohlcv_candle(**{field: float("nan")})])

                self.assertFalse(report.ok)
                self.assertEqual("ohlcv_invalid", report.reason.value)

    def test_normalize_rejects_infinite_ohlc_prices(self):
        for field in ("open", "high", "low", "close"):
            with self.subTest(field=field):
                report = _normalize_report([_ohlcv_candle(**{field: float("inf")})])

                self.assertFalse(report.ok)
                self.assertEqual("ohlcv_invalid", report.reason.value)

    def test_normalize_rejects_zero_ohlc_prices(self):
        for field in ("open", "high", "low", "close"):
            with self.subTest(field=field):
                report = _normalize_report([_ohlcv_candle(**{field: 0.0})])

                self.assertFalse(report.ok)
                self.assertEqual("ohlcv_invalid", report.reason.value)

    def test_normalize_rejects_negative_ohlc_prices(self):
        for field in ("open", "high", "low", "close"):
            with self.subTest(field=field):
                report = _normalize_report([_ohlcv_candle(**{field: -1.0})])

                self.assertFalse(report.ok)
                self.assertEqual("ohlcv_invalid", report.reason.value)

    def test_normalize_rejects_nan_or_infinite_volume(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                report = _normalize_report([_ohlcv_candle(volume=value)])

                self.assertFalse(report.ok)
                self.assertEqual("ohlcv_invalid", report.reason.value)

    def test_normalize_rejects_negative_volume(self):
        report = _normalize_report([_ohlcv_candle(volume=-1.0)])

        self.assertFalse(report.ok)
        self.assertEqual("ohlcv_invalid", report.reason.value)

    def test_normalize_accepts_zero_volume_with_valid_prices(self):
        report = _normalize_report([_ohlcv_candle(volume=0.0)])

        self.assertTrue(report.ok)
        self.assertEqual("ok", report.reason.value)


class TrustedRefreshStoreTests(unittest.TestCase):
    def test_refresh_once_writes_manifest_run_log_and_health_dashboard(self):
        from mu_strategy.market_data.trusted import refresh_market_data_once
        from mu_strategy.viz.data_health import render_data_health_dashboard

        rows = [
            {"instId": "BTC-USDT-SWAP", "last": "65000", "volCcy24h": "10"},
            {"instId": "ETH-USDT-SWAP", "last": "3000", "volCcy24h": "20"},
            {"instId": "MU-USDT-SWAP", "last": "5", "volCcy24h": "100"},
        ]

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            manifest = refresh_market_data_once(
                data_dir=data_dir,
                ticker_rows=rows,
                stock_token_inst_ids={"MU-USDT-SWAP"},
                limit=1,
                days=1,
                intervals=("5m", "15m", "1h"),
                fetcher=_fake_fetcher,
                now_ms=86_400_000,
            )

            manifest_path = _manifest_path(data_dir)
            run_log_path = data_dir / "refresh_runs.jsonl"
            self.assertTrue(manifest_path.exists())
            self.assertTrue(run_log_path.exists())
            self.assertEqual("success", manifest["attempt_status"])
            self.assertEqual("usable", manifest["snapshot_usability"])
            self.assertEqual(["BTC-USDT-SWAP"], [item["inst_id"] for item in manifest["universes"]["crypto_top"]])
            self.assertEqual(["MU-USDT-SWAP"], [item["inst_id"] for item in manifest["universes"]["stock_token_top"]])
            from mu_strategy.market_data.trusted_data.store import TrustedDataStore

            self.assertTrue(TrustedDataStore(data_dir=data_dir).generation_cache_path(manifest["run_id"], "BTC-USDT-SWAP", "5m").exists())
            self.assertTrue(manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["15m"]["is_valid"])

            html = render_data_health_dashboard(json.loads(manifest_path.read_text(encoding="utf-8")))

        self.assertIn("OKX 数据健康看板", html)
        self.assertIn("BTC-USDT-SWAP", html)
        self.assertIn("MU-USDT-SWAP", html)
        self.assertIn("stock_token_top", html)

    def test_per_symbol_refresh_facades_are_removed(self):
        import mu_strategy.market_data.trusted as trusted

        self.assertFalse(hasattr(trusted, "refresh_trusted_interval"))
        self.assertFalse(hasattr(trusted, "refresh_trusted_symbol_statuses"))

    def test_refresh_once_marks_manifest_invalid_when_any_interval_fails(self):
        from mu_strategy.market_data.trusted import refresh_market_data_once

        rows = [
            {"instId": "BTC-USDT-SWAP", "last": "65000", "volCcy24h": "10"},
            {"instId": "MU-USDT-SWAP", "last": "5", "volCcy24h": "100"},
        ]

        def fetcher(symbol: str, interval: str, *, days: int) -> list[Candle]:
            if interval == "15m":
                raise TimeoutError("blocked")
            return _fake_fetcher(symbol, interval, days=days)

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            manifest = refresh_market_data_once(
                data_dir=data_dir,
                ticker_rows=rows,
                stock_token_inst_ids={"MU-USDT-SWAP"},
                limit=1,
                days=1,
                intervals=("5m", "15m"),
                fetcher=fetcher,
                now_ms=86_400_000,
            )

            persisted = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))

        self.assertEqual("degraded", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("degraded", persisted["attempt_status"])
        self.assertEqual("invalid", persisted["snapshot_usability"])
        self.assertEqual("degraded", run_log["attempt_status"])
        self.assertEqual("invalid", run_log["snapshot_usability"])
        self.assertEqual(2, run_log["invalid_count"])
        self.assertFalse(manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["15m"]["is_valid"])
        self.assertEqual("refresh_failed", manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["15m"]["reason"])

    def test_refresh_once_marks_empty_universe_invalid(self):
        from mu_strategy.market_data.trusted import refresh_market_data_once

        def fetcher(symbol: str, interval: str, *, days: int) -> list[Candle]:
            raise AssertionError("empty universe must not fetch symbol candles")

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            manifest = refresh_market_data_once(
                data_dir=data_dir,
                ticker_rows=[],
                stock_token_inst_ids=set(),
                limit=1,
                days=1,
                intervals=("5m",),
                fetcher=fetcher,
                now_ms=3_600_000,
            )

            persisted = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))

        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("failed", persisted["attempt_status"])
        self.assertEqual("invalid", persisted["snapshot_usability"])
        self.assertEqual("failed", run_log["attempt_status"])
        self.assertEqual("invalid", run_log["snapshot_usability"])
        self.assertEqual({}, manifest["symbols"])
        self.assertIn("empty_universe", manifest["warnings"])

    def test_refresh_once_rebuilds_bad_cache_without_aborting_other_symbols(self):
        from mu_strategy.market_data.trusted import refresh_market_data_once

        rows = [
            {"instId": "BTC-USDT-SWAP", "last": "65000", "volCcy24h": "20"},
            {"instId": "ETH-USDT-SWAP", "last": "3000", "volCcy24h": "10"},
        ]

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            corrupt_cache = data_dir / "okx" / "BTC-USDT-SWAP" / "5m.csv"
            corrupt_cache.parent.mkdir(parents=True)
            corrupt_cache.write_text("not,a,valid,candle\n1,2,3,4\n", encoding="utf-8")
            _write_flat_manifest_for_paths(data_dir, {"BTC-USDT-SWAP": ("5m",)})

            manifest = refresh_market_data_once(
                data_dir=data_dir,
                ticker_rows=rows,
                stock_token_inst_ids=set(),
                limit=2,
                days=1,
                intervals=("5m",),
                fetcher=_fake_fetcher,
                now_ms=86_400_000,
            )

        btc_status = manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["5m"]
        eth_status = manifest["symbols"]["ETH-USDT-SWAP"]["intervals"]["5m"]
        self.assertEqual("success", manifest["attempt_status"])
        self.assertEqual("usable", manifest["snapshot_usability"])
        self.assertEqual([], manifest["provider_failures"])
        self.assertTrue(btc_status["is_valid"])
        self.assertEqual("ok", btc_status["reason"])
        self.assertTrue(eth_status["is_valid"])

    def test_refresh_once_rebuilds_invalid_native_cache_during_validation(self):
        from mu_strategy.market_data.trusted import refresh_market_data_once

        rows = [
            {"instId": "BTC-USDT-SWAP", "last": "65000", "volCcy24h": "20"},
        ]

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            corrupt_native = data_dir / "okx" / "BTC-USDT-SWAP" / "15m.csv"
            corrupt_native.parent.mkdir(parents=True)
            corrupt_native.write_text("not,a,valid,candle\n1,2,3,4\n", encoding="utf-8")
            _write_flat_manifest_for_paths(data_dir, {"BTC-USDT-SWAP": ("15m",)})

            manifest = refresh_market_data_once(
                data_dir=data_dir,
                ticker_rows=rows,
                stock_token_inst_ids=set(),
                limit=1,
                days=1,
                intervals=("5m", "15m"),
                fetcher=_fake_fetcher,
                now_ms=86_400_000,
            )

            persisted = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))

        status = manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["15m"]
        self.assertEqual("success", manifest["attempt_status"])
        self.assertEqual("usable", manifest["snapshot_usability"])
        self.assertEqual("success", persisted["attempt_status"])
        self.assertEqual("usable", persisted["snapshot_usability"])
        self.assertEqual("success", run_log["attempt_status"])
        self.assertEqual("usable", run_log["snapshot_usability"])
        self.assertEqual([], manifest["provider_failures"])
        self.assertEqual([], persisted["provider_failures"])
        self.assertEqual([], run_log["provider_failures"])
        self.assertTrue(status["is_valid"])
        self.assertEqual("ok", status["reason"])

    def test_refresh_once_marks_native_ohlcv_mismatch_invalid(self):
        from mu_strategy.market_data.trusted import refresh_market_data_once

        rows = [
            {"instId": "BTC-USDT-SWAP", "last": "65000", "volCcy24h": "20"},
        ]

        def fetcher(symbol: str, interval: str, *, days: int) -> list[Candle]:
            candles = _fake_fetcher(symbol, interval, days=days)
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

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            manifest = refresh_market_data_once(
                data_dir=data_dir,
                ticker_rows=rows,
                stock_token_inst_ids=set(),
                limit=1,
                days=1,
                intervals=("5m", "15m"),
                fetcher=fetcher,
                now_ms=86_400_000,
            )

            persisted = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))

        status = manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["15m"]
        self.assertEqual("degraded", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("degraded", persisted["attempt_status"])
        self.assertEqual("invalid", persisted["snapshot_usability"])
        self.assertEqual("degraded", run_log["attempt_status"])
        self.assertEqual("invalid", run_log["snapshot_usability"])
        self.assertFalse(status["is_valid"])
        self.assertEqual("ohlcv_mismatch", status["reason"])
        self.assertEqual("ohlcv_mismatch", status["validation"]["reason"])

    def test_explicit_symbol_refresh_publishes_generation_consumable_by_loader(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _NoTickerRecordingProvider()
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    symbols=("MU", "MU-USDT-SWAP"),
                    requested_intervals=("15m",),
                    limit=99,
                    days=1,
                    max_concurrency=1,
                    now_ms=86_400_000,
                    run_id="run-explicit",
                )
            )
            bundle = LoadTrustedBundle(store).execute(
                LoadTrustedBundleQuery("MU", intervals=("15m",), days=1, now_ms=86_400_000),
                trading_strict_policy(),
            )
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))

        self.assertEqual("success", run.attempt_status.value)
        self.assertEqual(("5m", "15m"), run.effective_intervals)
        self.assertEqual([("MU-USDT-SWAP", "5m", 1), ("MU-USDT-SWAP", "15m", 1)], provider.history_calls)
        self.assertEqual([], provider.incremental_calls)
        self.assertTrue(bundle.trust_decision.allowed, bundle.trust_decision)
        self.assertEqual("run-explicit", bundle.run_id)
        self.assertEqual(["MU-USDT-SWAP"], list(manifest["symbols"]))
        self.assertEqual("explicit", manifest["symbols"]["MU-USDT-SWAP"]["source"])
        self.assertEqual("explicit", manifest["universes"]["crypto_top"][0]["source"])

    def test_compatibility_refresh_custom_fetcher_remains_serial_by_default(self):
        from mu_strategy.market_data.trusted import refresh_market_data_once

        lock = threading.Lock()
        first_started = threading.Event()
        second_started = threading.Event()
        active = 0
        max_active = 0

        def stateful_fetcher(symbol, interval, *, days):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                if symbol == "BTC-USDT-SWAP":
                    first_started.set()
                    second_started.wait(timeout=0.2)
                else:
                    second_started.set()
                return _fake_fetcher(symbol, interval, days=days)
            finally:
                with lock:
                    active -= 1

        with TemporaryDirectory() as tmp:
            manifest = refresh_market_data_once(
                data_dir=Path(tmp),
                symbols=("BTC", "ETH"),
                intervals=("5m",),
                days=1,
                fetcher=stateful_fetcher,
                now_ms=86_400_000,
            )

        self.assertTrue(first_started.is_set())
        self.assertTrue(second_started.is_set())
        self.assertEqual(1, max_active)
        self.assertEqual("success", manifest["attempt_status"])

    def test_explicit_symbol_refresh_reuses_current_generation_incrementally(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            first_provider = _NoTickerRecordingProvider()
            request = dict(symbols=("MU",), requested_intervals=("5m",), days=1, limit=10, now_ms=86_400_000)
            first = RefreshTrustedMarketData(store, first_provider).execute(
                RefreshTrustedMarketDataRequest(**request, run_id="run-old")
            )
            second_provider = _NoTickerRecordingProvider()
            second = RefreshTrustedMarketData(store, second_provider).execute(
                RefreshTrustedMarketDataRequest(**request, run_id="run-new")
            )
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual([("MU-USDT-SWAP", "5m", 1)], first_provider.history_calls)
        self.assertEqual([], first_provider.incremental_calls)
        self.assertEqual("full_history", first.refresh_segments[0].fetch_mode)
        self.assertEqual(0, first.refresh_segments[0].existing_rows)
        self.assertEqual([], second_provider.history_calls)
        self.assertEqual([("MU-USDT-SWAP", "5m", 85_800_000)], second_provider.incremental_calls)
        self.assertEqual("incremental_reuse", second.refresh_segments[0].fetch_mode)
        self.assertTrue(second.refresh_segments[0].had_existing)
        self.assertTrue(second.refresh_segments[0].reused_prior_generation)
        self.assertGreater(second.refresh_segments[0].existing_rows, 0)
        self.assertEqual(0, second.refresh_segments[0].fetched_rows)
        self.assertEqual(second.datasets[("MU-USDT-SWAP", "5m")].rows, second.refresh_segments[0].output_rows)
        self.assertEqual("incremental_reuse", manifest["diagnostics"]["refresh_segments"][0]["fetch_mode"])
        self.assertEqual("incremental_reuse", run_log[-1]["refresh_segments"][0]["fetch_mode"])

    def test_segment_timing_uses_clock_when_health_now_is_pinned(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(
                TrustedDataStore(data_dir=Path(tmp)),
                _NoTickerRecordingProvider(),
                clock=_SequenceClock(100, 350),
            ).execute(
                RefreshTrustedMarketDataRequest(
                    symbols=("MU",),
                    requested_intervals=("5m",),
                    days=1,
                    now_ms=86_400_000,
                    run_id="run-wall-clock-timing",
                )
            )

        segment = run.refresh_segments[0]
        self.assertEqual(100, segment.started_at_ms)
        self.assertEqual(350, segment.completed_at_ms)
        self.assertEqual(250, segment.elapsed_ms)

    def test_concurrent_partial_symbol_failure_publishes_unrelated_usable_dataset(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest

        provider = _ConcurrentHistoryProvider(fail_symbol="BTC-USDT-SWAP")
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = _ThreadRecordingStore(data_dir=data_dir)
            publisher_thread = threading.get_ident()
            run = RefreshTrustedMarketData(store, provider).execute(
                RefreshTrustedMarketDataRequest(
                    symbols=("BTC", "ETH"),
                    requested_intervals=("5m",),
                    days=1,
                    now_ms=86_400_000,
                    run_id="run-concurrent-partial",
                    max_concurrency=2,
                )
            )
            current = json.loads((data_dir / "current.json").read_text(encoding="utf-8"))
            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            healthy_exists = _generation_cache_path(data_dir, run.run_id, "ETH-USDT-SWAP", "5m").exists()
            failed_exists = _generation_cache_path(data_dir, run.run_id, "BTC-USDT-SWAP", "5m").exists()

        self.assertGreaterEqual(provider.max_active, 2)
        self.assertEqual(RefreshAttemptStatus.DEGRADED, run.attempt_status)
        self.assertEqual(SnapshotUsability.INVALID, run.snapshot_usability)
        self.assertEqual("run-concurrent-partial", current["generation_id"])
        self.assertTrue(healthy_exists)
        self.assertFalse(failed_exists)
        healthy = manifest["symbols"]["ETH-USDT-SWAP"]["intervals"]["5m"]
        failed = manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["5m"]
        self.assertEqual(("available", "valid", "fresh", "ok"), (
            healthy["availability"],
            healthy["integrity"],
            healthy["freshness"],
            healthy["reason"],
        ))
        self.assertEqual(("missing", "invalid", "refresh_failed"), (
            failed["availability"],
            failed["integrity"],
            failed["reason"],
        ))
        segments = manifest["diagnostics"]["refresh_segments"]
        self.assertEqual(["BTC-USDT-SWAP", "ETH-USDT-SWAP"], [segment["symbol"] for segment in segments])
        self.assertEqual("refresh_failed", segments[0]["fetch_mode"])
        self.assertEqual("ok", segments[1]["health_reason"])
        self.assertEqual([publisher_thread], store.write_thread_ids)
        self.assertEqual([publisher_thread], store.replace_current_thread_ids)

    def test_parallel_and_serial_fetch_paths_preserve_diagnostics_semantics(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        runs = []
        manifests = []
        for max_concurrency in (1, 2):
            with TemporaryDirectory() as tmp:
                data_dir = Path(tmp)
                run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), _NoTickerRecordingProvider()).execute(
                    RefreshTrustedMarketDataRequest(
                        symbols=("BTC", "ETH"),
                        requested_intervals=("5m",),
                        days=1,
                        now_ms=86_400_000,
                        run_id=f"run-concurrency-{max_concurrency}",
                        max_concurrency=max_concurrency,
                    )
                )
                runs.append(run)
                manifests.append(json.loads(_manifest_path(data_dir).read_text(encoding="utf-8")))

        def stable_fields(segment):
            payload = segment.to_dict()
            for key in ("started_at_ms", "completed_at_ms", "elapsed_ms"):
                payload.pop(key)
            return payload

        self.assertEqual(
            [stable_fields(segment) for segment in runs[0].refresh_segments],
            [stable_fields(segment) for segment in runs[1].refresh_segments],
        )
        self.assertEqual(
            ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            [segment.symbol for segment in runs[1].refresh_segments],
        )
        for run, manifest in zip(runs, manifests):
            for segment in run.refresh_segments:
                self.assertGreaterEqual(segment.completed_at_ms, segment.started_at_ms)
                self.assertEqual(segment.completed_at_ms - segment.started_at_ms, segment.elapsed_ms)
            self.assertEqual(
                [segment.to_dict() for segment in run.refresh_segments],
                manifest["diagnostics"]["refresh_segments"],
            )


class TrustedCandleBundleTests(unittest.TestCase):
    def test_refresh_trusted_candle_bundle_refresh_true_fails_before_provider_or_writes(self):
        from mu_strategy.market_data.service import refresh_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import TrustedConsumerRefreshError
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(TrustedDataStore, "write_csv", side_effect=AssertionError("write_csv")):
                with patch.object(TrustedDataStore, "write_generation_manifest", side_effect=AssertionError("write_generation_manifest")):
                    with patch.object(TrustedDataStore, "append_run_log", side_effect=AssertionError("append_run_log")):
                        with patch("mu_strategy.market_data.service.refresh_with_okx_provider", side_effect=AssertionError("provider"), create=True):
                            with self.assertRaisesRegex(TrustedConsumerRefreshError, "refresh_market_data"):
                                refresh_trusted_candle_bundle(
                                    "MU-USDT-SWAP",
                                    intervals=("15m", "1h"),
                                    days=1,
                                    data_dir=data_dir,
                                    refresh=True,
                                )

            self.assertFalse(_manifest_path(data_dir).exists())
            self.assertFalse((data_dir / "refresh_runs.jsonl").exists())

    def test_refresh_trusted_candle_bundle_honors_refresh_false(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.service import refresh_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest_intervals = {}
            five_minute = _five_minute_candles(days=1)
            for interval in ("5m", "15m", "1h"):
                path = _generation_cache_path(data_dir, "test-run", "MU-USDT-SWAP", interval)
                if interval == "5m":
                    candles = five_minute
                else:
                    from mu_strategy.market_data.trusted import aggregate_candles

                    candles = aggregate_candles(five_minute, interval=interval)
                write_csv(candles, path)
                manifest_intervals[interval] = {
                    "symbol": "MU-USDT-SWAP",
                    "interval": interval,
                    "availability": "available",
                    "integrity": "valid",
                    "freshness": "fresh",
                    "reasons": ["ok"],
                    "rows": len(candles),
                    "first_timestamp_ms": candles[0].open_time_ms,
                    "last_timestamp_ms": candles[-1].open_time_ms,
                    "updated_at_ms": 3_600_000,
                    "source_file": f"okx/MU-USDT-SWAP/{interval}.csv",
                    "is_valid": True,
                    "is_stale": False,
                    "reason": "ok",
                    "warnings": [],
                    "content_sha256": candles_content_sha256(candles),
                    "validation": {"ok": True, "reason": "ok"},
                }
            _write_generation_manifest(data_dir, _manifest(symbols={"MU-USDT-SWAP": {"intervals": manifest_intervals}}))

            with patch.object(TrustedDataStore, "write_generation_manifest", side_effect=AssertionError("write_generation_manifest")):
                with patch.object(TrustedDataStore, "append_run_log", side_effect=AssertionError("append_run_log")):
                    with patch.object(TrustedDataStore, "write_csv", side_effect=AssertionError("write_csv")):
                        bundle = refresh_trusted_candle_bundle(
                            "MU-USDT-SWAP",
                            intervals=("15m", "1h"),
                            days=1,
                            data_dir=data_dir,
                            refresh=False,
                            clock=_FixedClock(86_400_000),
                        )

        self.assertEqual(96, len(bundle.candles_by_interval["15m"]))
        self.assertEqual(24, len(bundle.candles_by_interval["1h"]))
        self.assertTrue(bundle.statuses_by_interval["15m"].validation.ok)

    def test_refresh_trusted_candle_bundle_prunes_cached_window_and_statuses_to_days(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.service import refresh_trusted_candle_bundle
        from mu_strategy.market_data.trusted import aggregate_candles
        from mu_strategy.market_data.trusted_data.store import candles_content_sha256
        from mu_strategy.market_data.utils import DAY_MS

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            five_minute = _five_minute_candles(days=3)
            candles_by_interval = {
                "5m": five_minute,
                "15m": aggregate_candles(five_minute, interval="15m"),
                "1h": aggregate_candles(five_minute, interval="1h"),
            }
            manifest_intervals = {}
            for interval, candles in candles_by_interval.items():
                path = _generation_cache_path(data_dir, "test-run", "MU-USDT-SWAP", interval)
                write_csv(candles, path)
                manifest_intervals[interval] = {
                    "symbol": "MU-USDT-SWAP",
                    "interval": interval,
                    "availability": "available",
                    "integrity": "valid",
                    "freshness": "fresh",
                    "reasons": ["ok"],
                    "rows": 999,
                    "first_timestamp_ms": candles[0].open_time_ms,
                    "last_timestamp_ms": candles[-1].open_time_ms,
                    "updated_at_ms": 3 * DAY_MS,
                    "source_file": f"okx/MU-USDT-SWAP/{interval}.csv",
                    "is_valid": True,
                    "is_stale": False,
                    "reason": "ok",
                    "warnings": [],
                    "content_sha256": candles_content_sha256(candles),
                    "validation": {"ok": True, "reason": "ok"},
                }
            manifest = _manifest(symbols={"MU-USDT-SWAP": {"intervals": manifest_intervals}})
            _write_generation_manifest(data_dir, manifest)

            bundle = refresh_trusted_candle_bundle(
                "MU-USDT-SWAP",
                intervals=("15m", "1h"),
                days=1,
                data_dir=data_dir,
                refresh=False,
                clock=_FixedClock(3 * DAY_MS),
            )

        shared_end_time_ms = five_minute[-1].open_time_ms
        earliest_allowed_time_ms = shared_end_time_ms - DAY_MS
        for interval in ("5m", "15m", "1h"):
            status = bundle.statuses_by_interval[interval]
            self.assertNotEqual(999, status.rows)
            self.assertGreaterEqual(status.first_timestamp_ms, earliest_allowed_time_ms)
            self.assertLessEqual(status.last_timestamp_ms, shared_end_time_ms)
        for interval in ("15m", "1h"):
            candles = bundle.candles_by_interval[interval]
            status = bundle.statuses_by_interval[interval]
            self.assertTrue(candles)
            self.assertEqual(len(candles), status.rows)
            self.assertEqual(candles[0].open_time_ms, status.first_timestamp_ms)
            self.assertEqual(candles[-1].open_time_ms, status.last_timestamp_ms)
            self.assertTrue(status.validation.ok)

    def test_refresh_trusted_candle_bundle_preserves_manifest_failure_status(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.service import refresh_trusted_candle_bundle

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            for interval in ("5m", "15m"):
                path = _generation_cache_path(data_dir, "test-run", "MU-USDT-SWAP", interval)
                write_csv(_fake_fetcher("MU-USDT-SWAP", interval, days=1), path)
            manifest = _manifest(
                outcome="failed",
                status="invalid",
                symbols={
                    "MU-USDT-SWAP": {
                        "intervals": {
                            "15m": {
                                "symbol": "MU-USDT-SWAP",
                                "interval": "15m",
                                "availability": "available",
                                "integrity": "invalid",
                                "freshness": "stale",
                                "reasons": ["incremental_refresh_failed"],
                                "rows": 4,
                                "first_timestamp_ms": 0,
                                "last_timestamp_ms": 2_700_000,
                                "updated_at_ms": 3_600_000,
                                "source_file": "okx/MU-USDT-SWAP/15m.csv",
                                "is_valid": False,
                                "is_stale": True,
                                "reason": "incremental_refresh_failed",
                                "error_type": "TimeoutError",
                                "message": "blocked",
                                "warnings": [],
                                "validation": {"ok": False, "reason": "incremental_refresh_failed"},
                            }
                        }
                    }
                },
                cycle_error={"error_type": "TimeoutError", "message": "blocked"},
            )
            _write_generation_manifest(data_dir, manifest)

            bundle = refresh_trusted_candle_bundle(
                "MU-USDT-SWAP",
                intervals=("15m",),
                days=1,
                data_dir=data_dir,
                refresh=False,
                clock=_FixedClock(3_600_000),
            )

        status = bundle.statuses_by_interval["15m"]
        self.assertFalse(status.is_valid)
        self.assertTrue(status.is_stale)
        self.assertEqual("incremental_refresh_failed", status.reason)
        self.assertEqual("TimeoutError", status.error_type)
        self.assertEqual("blocked", status.message)
        self.assertEqual([], bundle.candles_by_interval["15m"])

    def test_refresh_trusted_candle_bundle_refresh_true_preserves_existing_manifest_and_run_log(self):
        from mu_strategy.market_data.service import refresh_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import TrustedConsumerRefreshError

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest_path = _manifest_path(data_dir)
            run_log_path = data_dir / "refresh_runs.jsonl"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(_manifest(symbols={}, outcome="success", status="ok")), encoding="utf-8")
            run_log_path.write_text('{"run_id":"canonical"}\n', encoding="utf-8")
            manifest_before = manifest_path.read_bytes()
            run_log_before = run_log_path.read_bytes()

            with self.assertRaises(TrustedConsumerRefreshError):
                refresh_trusted_candle_bundle(
                    "MU-USDT-SWAP",
                    intervals=("15m",),
                    days=1,
                    data_dir=data_dir,
                    refresh=True,
                )

            self.assertEqual(manifest_before, manifest_path.read_bytes())
            self.assertEqual(run_log_before, run_log_path.read_bytes())


class _NoTickerRecordingProvider:
    def __init__(self):
        self.history_calls = []
        self.incremental_calls = []

    def fetch_tickers(self):
        raise AssertionError("explicit symbol refresh must not fetch ticker universe")

    def fetch_history(self, symbol, interval, *, days):
        self.history_calls.append((symbol, interval, days))
        return _fake_fetcher(symbol, interval, days=days)

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        self.incremental_calls.append((symbol, interval, since_time_ms))
        return []


class _ConcurrentHistoryProvider:
    def __init__(self, *, fail_symbol: str | None = None):
        self.fail_symbol = fail_symbol
        self.barrier = threading.Barrier(2)
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def fetch_tickers(self):
        raise AssertionError("explicit symbol refresh must not fetch ticker universe")

    def fetch_history(self, symbol, interval, *, days):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.barrier.wait(timeout=2)
            if symbol == self.fail_symbol:
                raise TimeoutError(f"{symbol} blocked")
            return _fake_fetcher(symbol, interval, days=days)
        finally:
            with self.lock:
                self.active -= 1

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise AssertionError("first refresh must use full history")


class _ThreadRecordingStore(TrustedDataStore):
    def __init__(self, *, data_dir: Path):
        super().__init__(data_dir=data_dir)
        self.write_thread_ids = []
        self.replace_current_thread_ids = []

    def write_csv(self, candles, path):
        self.write_thread_ids.append(threading.get_ident())
        return super().write_csv(candles, path)

    def replace_current(self, generation_id):
        self.replace_current_thread_ids.append(threading.get_ident())
        return super().replace_current(generation_id)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


def _ohlcv_candle(
    *,
    open: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: float = 1000.0,
) -> Candle:
    return Candle(0, open, high, low, close, volume)


def _normalize_report(candles: list[Candle]):
    from mu_strategy.market_data.trusted_data.validation import normalize_and_validate_candles

    _, report = normalize_and_validate_candles(candles, interval="5m")
    return report


def _fake_fetcher(symbol: str, interval: str, *, days: int) -> list[Candle]:
    five_minute = _five_minute_candles(days=days)
    if interval == "5m":
        return five_minute
    from mu_strategy.market_data.trusted import aggregate_candles

    return aggregate_candles(five_minute, interval=interval)


def _five_minute_candles(*, days: int) -> list[Candle]:
    from mu_strategy.market_data.utils import DAY_MS

    count = days * DAY_MS // 300_000
    return [_candle(index * 300_000, 100 + index) for index in range(count)]


def _manifest(
    *,
    symbols: dict,
    outcome: str = "success",
    status: str = "ok",
    cycle_error: dict | None = None,
) -> dict:
    attempt_status = "failed" if outcome == "failed" else "degraded" if outcome == "partial" else "success"
    snapshot_usability = "usable" if status == "ok" else status
    return {
        "schema_version": 3,
        "run_id": "test-run",
        "attempt_status": attempt_status,
        "snapshot_usability": snapshot_usability,
        "started_at_ms": 0,
        "completed_at_ms": 3_600_000,
        "requested_intervals": ["15m", "1h"],
        "effective_intervals": ["5m", "15m", "1h"],
        "universes": {"crypto_top": [], "stock_token_top": []},
        "symbols": symbols,
        "provider_failures": [],
        "warnings": [],
        "cycle_error": cycle_error,
    }


def _write_flat_manifest_for_paths(data_dir: Path, intervals_by_symbol: dict[str, tuple[str, ...]]) -> None:
    run_id = "generation-corrupt-cache"
    symbols = {}
    for symbol, intervals in intervals_by_symbol.items():
        symbols[symbol] = {"intervals": {}}
        for interval in intervals:
            symbols[symbol]["intervals"][interval] = {
                "symbol": symbol,
                "interval": interval,
                "availability": "available",
                "integrity": "valid",
                "freshness": "fresh",
                "reasons": ["ok"],
                "rows": 1,
                "first_timestamp_ms": 0,
                "last_timestamp_ms": 0,
                "updated_at_ms": 0,
                "source_file": f"okx/{symbol}/{interval}.csv",
                "content_sha256": "not-checked-during-refresh",
                "validation": {"ok": True, "reason": "ok"},
            }
    _write_generation_manifest(
        data_dir,
        {
            "schema_version": 3,
            "run_id": run_id,
            "attempt_status": "success",
            "snapshot_usability": "usable",
            "started_at_ms": 0,
            "completed_at_ms": 0,
            "requested_intervals": ["5m", "15m", "1h"],
            "effective_intervals": ["5m", "15m", "1h"],
            "universes": {"crypto_top": [], "stock_token_top": []},
            "symbols": symbols,
            "provider_failures": [],
            "warnings": [],
            "cycle_error": None,
        },
    )


def _generation_cache_path(data_dir: Path, run_id: str, symbol: str, interval: str) -> Path:
    return data_dir / "generations" / run_id / "okx" / symbol / f"{interval}.csv"


def _write_generation_manifest(data_dir: Path, manifest: dict) -> None:
    run_id = manifest["run_id"]
    manifest_path = data_dir / "generations" / run_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (data_dir / "current.json").write_text(
        json.dumps({"schema_version": 1, "generation_id": run_id, "manifest": f"generations/{run_id}/manifest.json"}),
        encoding="utf-8",
    )


class _FixedClock:
    def __init__(self, now_ms: int):
        self.now = now_ms

    def now_ms(self) -> int:
        return self.now


class _SequenceClock:
    def __init__(self, *values: int):
        self.values = iter(values)

    def now_ms(self) -> int:
        return next(self.values)


def _manifest_path(data_dir: Path) -> Path:
    current = data_dir / "current.json"
    if current.exists():
        return data_dir / json.loads(current.read_text(encoding="utf-8"))["manifest"]
    return data_dir / "manifest.json"


if __name__ == "__main__":
    unittest.main()
