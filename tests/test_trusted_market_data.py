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
                now_ms=3_600_000,
            )

            manifest_path = data_dir / "manifest.json"
            run_log_path = data_dir / "refresh_runs.jsonl"
            self.assertTrue(manifest_path.exists())
            self.assertTrue(run_log_path.exists())
            self.assertEqual("ok", manifest["status"])
            self.assertEqual(["BTC-USDT-SWAP"], [item["inst_id"] for item in manifest["universes"]["crypto_top"]])
            self.assertEqual(["MU-USDT-SWAP"], [item["inst_id"] for item in manifest["universes"]["stock_token_top"]])
            self.assertTrue((data_dir / "okx" / "BTC-USDT-SWAP" / "5m.csv").exists())
            self.assertTrue(manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["15m"]["is_valid"])

            html = render_data_health_dashboard(json.loads(manifest_path.read_text(encoding="utf-8")))

        self.assertIn("OKX 数据健康看板", html)
        self.assertIn("BTC-USDT-SWAP", html)
        self.assertIn("MU-USDT-SWAP", html)
        self.assertIn("stock_token_top", html)

    def test_refresh_interval_marks_stale_when_incremental_fetch_fails(self):
        from mu_strategy.market_data.cache import write_csv
        from mu_strategy.market_data.trusted import refresh_trusted_interval

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = data_dir / "okx" / "BTC-USDT-SWAP" / "5m.csv"
            write_csv([_candle(0, 100), _candle(300_000, 101)], path)

            with patch("mu_strategy.market_data.trusted.fetch_okx_incremental", side_effect=TimeoutError("blocked")):
                status = refresh_trusted_interval("BTC-USDT-SWAP", "5m", days=1, data_dir=data_dir, now_ms=900_000)

        self.assertTrue(status.is_stale)
        self.assertFalse(status.is_valid)
        self.assertEqual("incremental_refresh_failed", status.reason)
        self.assertEqual("TimeoutError", status.error_type)
        self.assertEqual("blocked", status.message)

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
                now_ms=3_600_000,
            )

            persisted = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
            run_log = json.loads((data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8"))

        self.assertEqual("invalid", manifest["status"])
        self.assertEqual("invalid", persisted["status"])
        self.assertEqual("invalid", run_log["status"])
        self.assertEqual(2, run_log["invalid_count"])
        self.assertFalse(manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["15m"]["is_valid"])
        self.assertEqual("refresh_failed", manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["15m"]["reason"])

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

            manifest = refresh_market_data_once(
                data_dir=data_dir,
                ticker_rows=rows,
                stock_token_inst_ids=set(),
                limit=2,
                days=1,
                intervals=("5m",),
                fetcher=_fake_fetcher,
                now_ms=3_600_000,
            )

        btc_status = manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["5m"]
        eth_status = manifest["symbols"]["ETH-USDT-SWAP"]["intervals"]["5m"]
        self.assertEqual("invalid", manifest["status"])
        self.assertFalse(btc_status["is_valid"])
        self.assertEqual("cache_read_failed", btc_status["reason"])
        self.assertTrue(eth_status["is_valid"])


class TrustedCandleBundleTests(unittest.TestCase):
    def test_refresh_trusted_candle_bundle_attaches_built_native_validation(self):
        from mu_strategy.market_data.service import refresh_trusted_candle_bundle

        with TemporaryDirectory() as tmp:
            bundle = refresh_trusted_candle_bundle(
                "MU-USDT-SWAP",
                intervals=("15m", "1h"),
                days=1,
                data_dir=Path(tmp),
                fetcher=_fake_fetcher,
            )

        self.assertEqual(["15m", "1h"], list(bundle.candles_by_interval))
        self.assertIn("5m", bundle.statuses_by_interval)
        self.assertTrue(bundle.statuses_by_interval["15m"].validation.ok)
        self.assertTrue(bundle.statuses_by_interval["1h"].validation.ok)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


def _fake_fetcher(symbol: str, interval: str, *, days: int) -> list[Candle]:
    step = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}[interval]
    count = {"5m": 12, "15m": 4, "1h": 1}[interval]
    return [_candle(index * step, 100 + index) for index in range(count)]


if __name__ == "__main__":
    unittest.main()
