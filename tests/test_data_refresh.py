import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.market_data.cache import read_csv
from mu_strategy.market_data.refresh import (
    CandleValidationError,
    aggregate_from_base_interval,
    validate_built_candles,
)
from mu_strategy.models import Candle


FIVE_MIN_MS = 300_000


class DataRefreshTests(unittest.TestCase):
    def test_aggregates_complete_5m_candles_into_15m(self):
        candles = [
            _candle(0, 100, 101, 99, 100.5, 10),
            _candle(FIVE_MIN_MS, 100.5, 102, 100, 101.5, 20),
            _candle(2 * FIVE_MIN_MS, 101.5, 103, 101, 102.5, 30),
        ]

        aggregated = aggregate_from_base_interval(candles, base_interval="5m", target_interval="15m")

        self.assertEqual(1, len(aggregated))
        self.assertEqual(0, aggregated[0].open_time_ms)
        self.assertAlmostEqual(100, aggregated[0].open)
        self.assertAlmostEqual(103, aggregated[0].high)
        self.assertAlmostEqual(99, aggregated[0].low)
        self.assertAlmostEqual(102.5, aggregated[0].close)
        self.assertAlmostEqual(60, aggregated[0].volume)

    def test_drops_incomplete_target_groups(self):
        candles = [
            _candle(0, 100, 101, 99, 100.5, 10),
            _candle(FIVE_MIN_MS, 100.5, 102, 100, 101.5, 20),
            _candle(3 * FIVE_MIN_MS, 102.5, 104, 102, 103.5, 30),
        ]

        aggregated = aggregate_from_base_interval(candles, base_interval="5m", target_interval="15m")

        self.assertEqual([], aggregated)

    def test_aggregates_complete_5m_candles_into_1h(self):
        candles = [
            _candle(index * FIVE_MIN_MS, 100 + index, 101 + index, 99 + index, 100.25 + index, 10 + index)
            for index in range(12)
        ]

        aggregated = aggregate_from_base_interval(candles, base_interval="5m", target_interval="1h")

        self.assertEqual(1, len(aggregated))
        self.assertEqual(0, aggregated[0].open_time_ms)
        self.assertAlmostEqual(100, aggregated[0].open)
        self.assertAlmostEqual(112, aggregated[0].high)
        self.assertAlmostEqual(99, aggregated[0].low)
        self.assertAlmostEqual(111.25, aggregated[0].close)
        self.assertAlmostEqual(sum(10 + index for index in range(12)), aggregated[0].volume)

    def test_validation_raises_on_native_1h_mismatch(self):
        built = [_candle(0, 100, 110, 90, 105, 1000)]
        native = [_candle(0, 100, 111, 90, 105, 1000)]

        with self.assertRaises(CandleValidationError) as exc:
            validate_built_candles(built, native, tolerance=0.000001)

        self.assertIn("high", str(exc.exception))
        self.assertEqual(1, len(exc.exception.mismatches))

    def test_validation_raises_when_native_1h_is_missing_from_built_series(self):
        built = [_candle(0, 100, 110, 90, 105, 1000)]
        native = [
            _candle(0, 100, 110, 90, 105, 1000),
            _candle(3_600_000, 105, 112, 104, 111, 1200),
        ]

        with self.assertRaises(CandleValidationError) as exc:
            validate_built_candles(built, native, tolerance=0.000001)

        self.assertIn("missing_built", str(exc.exception))
        self.assertEqual(1, len(exc.exception.mismatches))

    def test_once_refresh_fetches_5m_and_1h_writes_live_outputs(self):
        from mu_strategy import data_refresh

        fetched_calls = []
        five_minute = [
            _candle(index * FIVE_MIN_MS, 100 + index, 101 + index, 99 + index, 100.25 + index, 10 + index)
            for index in range(18)
        ]
        native_1h = [
            _candle(0, 100, 112, 99, 111.25, sum(10 + index for index in range(12))),
        ]

        def fake_fetch(symbol, interval, **kwargs):
            fetched_calls.append((symbol, interval, kwargs))
            return five_minute if interval == "5m" else native_1h

        with TemporaryDirectory() as tmp:
            argv = [
                "mu_strategy.data_refresh",
                "--once",
                "--source",
                "okx",
                "--symbol",
                "MU-USDT-SWAP",
                "--window-minutes",
                "90",
                "--data-dir",
                tmp,
            ]
            with patch("sys.argv", argv):
                with patch("mu_strategy.data_refresh.fetch_latest_window", side_effect=fake_fetch):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        data_refresh.main()

            output_dir = Path(tmp)
            built_15m = output_dir / "OKX_MU-USDT-SWAP_15m_built_from_5m_latest.csv"
            built_1h = output_dir / "OKX_MU-USDT-SWAP_1h_built_from_5m_latest.csv"
            native_1h_path = output_dir / "OKX_MU-USDT-SWAP_1h_native_latest.csv"
            fetched_5m = output_dir / "OKX_MU-USDT-SWAP_5m_native_latest.csv"

            self.assertTrue(built_15m.exists())
            self.assertTrue(built_1h.exists())
            self.assertTrue(native_1h_path.exists())
            self.assertTrue(fetched_5m.exists())
            self.assertEqual(6, len(read_csv(built_15m)))
            self.assertEqual(1, len(read_csv(built_1h)))
            self.assertEqual([("MU-USDT-SWAP", "5m"), ("MU-USDT-SWAP", "1h")], [(c[0], c[1]) for c in fetched_calls])
            self.assertIn("validation=ok", stdout.getvalue())

    def test_latest_window_anchors_to_latest_available_candle_and_aligns_to_hour(self):
        from mu_strategy import data_refresh

        candles = [
            _candle(index * FIVE_MIN_MS, 100 + index, 101 + index, 99 + index, 100.25 + index, 10 + index)
            for index in range(20)
        ]

        with patch("mu_strategy.data_refresh.fetch_okx_candles", return_value=candles):
            selected = data_refresh.fetch_latest_window(
                "MU-USDT-SWAP",
                "5m",
                source="okx",
                window_minutes=90,
            )

            self.assertEqual(20, len(selected))
        self.assertEqual(0, selected[0].open_time_ms)
        self.assertEqual(19 * FIVE_MIN_MS, selected[-1].open_time_ms)

    def test_okx_latest_5m_window_fetches_enough_bars_for_hour_alignment(self):
        from mu_strategy import data_refresh

        candles = [
            _candle(index * FIVE_MIN_MS, 100 + index, 101 + index, 99 + index, 100.25 + index, 10 + index)
            for index in range(83)
        ]
        requested_limits = []

        def fake_fetch_okx_candles(symbol, interval, *, limit):
            requested_limits.append(limit)
            return candles[-limit:]

        with patch("mu_strategy.data_refresh.fetch_okx_candles", side_effect=fake_fetch_okx_candles):
            selected = data_refresh.fetch_latest_window(
                "MU-USDT-SWAP",
                "5m",
                source="okx",
                window_minutes=360,
            )

        self.assertEqual([84], requested_limits)
        self.assertEqual(83, len(selected))
        self.assertEqual(0, selected[0].open_time_ms)
        self.assertEqual(82 * FIVE_MIN_MS, selected[-1].open_time_ms)

    def test_once_refresh_defaults_to_four_hour_validation_window(self):
        from mu_strategy import data_refresh

        argv = ["mu_strategy.data_refresh", "--once"]
        with patch("sys.argv", argv):
            with patch("mu_strategy.data_refresh._run_and_print") as run_and_print:
                data_refresh.main()

        run_and_print.assert_called_once_with("okx", "MU-USDT-SWAP", 360, Path("data/live"))

    def test_loop_refresh_defaults_to_four_hour_interval(self):
        from mu_strategy import data_refresh

        argv = ["mu_strategy.data_refresh", "--loop"]
        with patch("sys.argv", argv):
            with patch("mu_strategy.data_refresh._run_and_print"):
                with patch("mu_strategy.data_refresh.time.sleep", side_effect=KeyboardInterrupt) as sleep:
                    with self.assertRaises(KeyboardInterrupt):
                        data_refresh.main()

        sleep.assert_called_once_with(240 * 60)

    def test_okx_latest_refresh_uses_recent_candles_endpoint(self):
        from mu_strategy.market_data.providers.okx import fetch_okx_candles

        opened_urls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                payload = {
                    "code": "0",
                    "data": [[str(FIVE_MIN_MS), "100", "101", "99", "100.5", "10", "10", "1005", "1"]],
                }
                return json.dumps(payload).encode("utf-8")

        def fake_urlopen(request, timeout):
            opened_urls.append(request.full_url)
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            candles = fetch_okx_candles("MU-USDT-SWAP", "5m", limit=120)

        self.assertEqual(1, len(candles))
        self.assertIn("/api/v5/market/candles?", opened_urls[0])
        self.assertIn("limit=120", opened_urls[0])


def _candle(open_time_ms: int, open_: float, high: float, low: float, close: float, volume: float) -> Candle:
    return Candle(
        open_time_ms=open_time_ms,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


if __name__ == "__main__":
    unittest.main()
