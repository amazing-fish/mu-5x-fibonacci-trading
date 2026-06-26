import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle


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

    def test_refresh_once_marks_bad_cache_invalid_without_aborting_other_symbols(self):
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
        self.assertEqual("degraded", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual([], manifest["provider_failures"])
        self.assertFalse(btc_status["is_valid"])
        self.assertEqual("cache_read_failed", btc_status["reason"])
        self.assertTrue(eth_status["is_valid"])

    def test_refresh_once_skips_invalid_native_cache_during_validation(self):
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
        self.assertEqual("degraded", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("degraded", persisted["attempt_status"])
        self.assertEqual("invalid", persisted["snapshot_usability"])
        self.assertEqual("degraded", run_log["attempt_status"])
        self.assertEqual("invalid", run_log["snapshot_usability"])
        self.assertEqual([], manifest["provider_failures"])
        self.assertEqual([], persisted["provider_failures"])
        self.assertEqual([], run_log["provider_failures"])
        self.assertFalse(status["is_valid"])
        self.assertEqual("cache_read_failed", status["reason"])

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


class TrustedCandleBundleTests(unittest.TestCase):
    def test_refresh_trusted_candle_bundle_refresh_true_fails_before_provider_or_writes(self):
        from mu_strategy.market_data.service import refresh_trusted_candle_bundle
        from mu_strategy.market_data.trusted_data.contracts import TrustedConsumerRefreshError
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(TrustedDataStore, "write_csv", side_effect=AssertionError("write_csv")):
                with patch.object(TrustedDataStore, "write_manifest", side_effect=AssertionError("write_manifest")):
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
                path = data_dir / "okx" / "MU-USDT-SWAP" / f"{interval}.csv"
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
                    "source_file": str(path),
                    "is_valid": True,
                    "is_stale": False,
                    "reason": "ok",
                    "warnings": [],
                    "content_sha256": candles_content_sha256(candles),
                    "validation": {"ok": True, "reason": "ok"},
                }
            _manifest_path(data_dir).write_text(
                json.dumps(_manifest(symbols={"MU-USDT-SWAP": {"intervals": manifest_intervals}})),
                encoding="utf-8",
            )

            with patch.object(TrustedDataStore, "write_manifest", side_effect=AssertionError("write_manifest")):
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
                path = data_dir / "okx" / "MU-USDT-SWAP" / f"{interval}.csv"
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
                    "source_file": str(path),
                    "is_valid": True,
                    "is_stale": False,
                    "reason": "ok",
                    "warnings": [],
                    "content_sha256": candles_content_sha256(candles),
                    "validation": {"ok": True, "reason": "ok"},
                }
            manifest = _manifest(symbols={"MU-USDT-SWAP": {"intervals": manifest_intervals}})
            _manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")

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
                path = data_dir / "okx" / "MU-USDT-SWAP" / f"{interval}.csv"
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
                                "source_file": str(data_dir / "okx" / "MU-USDT-SWAP" / "15m.csv"),
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
            _manifest_path(data_dir).write_text(json.dumps(manifest), encoding="utf-8")

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


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


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
    symbols = {}
    for symbol, intervals in intervals_by_symbol.items():
        symbols[symbol] = {"intervals": {}}
        for interval in intervals:
            path = data_dir / "okx" / symbol / f"{interval}.csv"
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
                "source_file": str(path),
                "content_sha256": "not-checked-during-refresh",
                "validation": {"ok": True, "reason": "ok"},
            }
    _manifest_path(data_dir).write_text(
        json.dumps(
            {
                "schema_version": 3,
                "run_id": "flat-corrupt-cache",
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
            }
        ),
        encoding="utf-8",
    )


class _FixedClock:
    def __init__(self, now_ms: int):
        self.now = now_ms

    def now_ms(self) -> int:
        return self.now


def _manifest_path(data_dir: Path) -> Path:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore

    current = data_dir / "current.json"
    if current.exists():
        return data_dir / json.loads(current.read_text(encoding="utf-8"))["manifest"]
    return TrustedDataStore(data_dir=data_dir).flat_manifest_path


if __name__ == "__main__":
    unittest.main()
