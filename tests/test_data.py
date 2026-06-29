import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.data import cached_historical, write_csv
from mu_strategy.market_data.cache import (
    DataQualityError,
    cache_path,
    merge_incremental_candles,
    prune_candles_to_window,
    validate_close_to_next_open_gaps,
)
from mu_strategy.market_data.providers.okx import fetch_okx_historical, okx_row_to_candle
from mu_strategy.market_data.trusted_data.windowing import assess_requested_coverage
from mu_strategy.market_data.utils import DAY_MS, interval_to_ms
from mu_strategy.models import Candle


class DataTests(unittest.TestCase):
    def test_okx_row_to_candle_ignores_unconfirmed_candle(self):
        row = ["1781291700000", "990.91", "992.44", "990.42", "991.16", "68.9", "68.9", "68292.7062", "0"]

        self.assertIsNone(okx_row_to_candle(row))

    def test_okx_row_to_candle_parses_confirmed_candle(self):
        row = ["1781290800000", "992.03", "996.15", "989.97", "991.48", "703.05", "703.05", "698201.1617", "1"]

        candle = okx_row_to_candle(row)

        self.assertIsNotNone(candle)
        self.assertEqual(1781290800000, candle.open_time_ms)
        self.assertAlmostEqual(992.03, candle.open)
        self.assertAlmostEqual(703.05, candle.volume)

    def test_okx_cache_path_is_provider_scoped(self):
        path = cache_path("MU-USDT-SWAP", "15m", days=180, data_dir=Path("data"), source="okx")

        self.assertEqual(Path("data/OKX_MU-USDT-SWAP_15m_180d.csv"), path)

    def test_incremental_merge_reprocesses_last_cached_candle(self):
        existing = [_candle(0, 100), _candle(900_000, 101)]
        fetched = [_candle(900_000, 101.5), _candle(1_800_000, 102)]

        merged = merge_incremental_candles(existing, fetched)

        self.assertEqual([0, 900_000, 1_800_000], [bar.open_time_ms for bar in merged])
        self.assertAlmostEqual(101.5, merged[1].close)

    def test_incremental_merge_keeps_cache_when_no_replacement_arrives(self):
        existing = [_candle(0, 100), _candle(900_000, 101)]

        merged = merge_incremental_candles(existing, [])

        self.assertEqual(existing, merged)

    def test_prune_candles_to_window_keeps_requested_days_only(self):
        candles = [_candle(0, 100), _candle(86_400_000, 101), _candle(2 * 86_400_000, 102)]

        pruned = prune_candles_to_window(candles, days=1, end_time_ms=2 * 86_400_000)

        self.assertEqual([86_400_000, 2 * 86_400_000], [bar.open_time_ms for bar in pruned])

    def test_validate_close_to_next_open_gaps_blocks_large_data_jump(self):
        candles = [
            Candle(0, 99, 101, 98, 100, 1000),
            Candle(900_000, 108, 109, 107, 108.5, 1000),
        ]

        with self.assertRaisesRegex(DataQualityError, "close_to_next_open_gap"):
            validate_close_to_next_open_gaps(candles, max_gap_pct=0.02)

    def test_validate_close_to_next_open_gaps_allows_small_continuity_noise(self):
        candles = [
            Candle(0, 99, 101, 98, 100, 1000),
            Candle(900_000, 100.5, 101, 99, 100.7, 1000),
        ]

        validate_close_to_next_open_gaps(candles, max_gap_pct=0.02)

    def test_okx_cached_historical_updates_existing_cache_incrementally_and_prunes_window(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = cache_path("MU-USDT-SWAP", "15m", days=1, data_dir=data_dir, source="okx")
            write_csv([_candle(0, 100), _candle(86_400_000, 101)], path)

            with patch(
                "mu_strategy.market_data.cache.fetch_okx_incremental",
                return_value=[_candle(86_400_000, 101.5), _candle(2 * 86_400_000, 102)],
            ):
                candles, returned_path = cached_historical(
                    "MU-USDT-SWAP",
                    "15m",
                    days=1,
                    data_dir=data_dir,
                    source="okx",
                )

        self.assertEqual(path, returned_path)
        self.assertEqual([86_400_000, 2 * 86_400_000], [bar.open_time_ms for bar in candles])
        self.assertAlmostEqual(101.5, candles[0].close)

    def test_okx_cached_historical_falls_back_to_existing_cache_on_incremental_failure(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = cache_path("MU-USDT-SWAP", "15m", days=180, data_dir=data_dir, source="okx")
            existing = [_candle(0, 100), _candle(900_000, 101)]
            write_csv(existing, path)

            with patch("mu_strategy.market_data.cache.fetch_okx_incremental", side_effect=TimeoutError("blocked")):
                candles, returned_path = cached_historical(
                    "MU-USDT-SWAP",
                    "15m",
                    days=180,
                    data_dir=data_dir,
                    source="okx",
                )

        self.assertEqual(path, returned_path)
        self.assertEqual(existing, candles)

    def test_okx_cached_historical_blocks_existing_cache_with_large_close_to_open_gap(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = cache_path("MU-USDT-SWAP", "15m", days=180, data_dir=data_dir, source="okx")
            write_csv(
                [
                    Candle(0, 99, 101, 98, 100, 1000),
                    Candle(900_000, 108, 109, 107, 108.5, 1000),
                ],
                path,
            )

            with self.assertRaisesRegex(DataQualityError, "close_to_next_open_gap"):
                cached_historical(
                    "MU-USDT-SWAP",
                    "15m",
                    days=180,
                    data_dir=data_dir,
                    source="okx",
                    incremental=False,
                )

    def test_okx_fetch_uses_browser_user_agent(self):
        from mu_strategy.market_data.providers.okx import fetch_okx_candles

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"code":"0","msg":"","data":[]}'

        captured = {}

        def fake_urlopen(request, timeout):
            captured["user_agent"] = request.headers.get("User-agent")
            captured["accept"] = request.headers.get("Accept")
            return FakeResponse()

        with patch("mu_strategy.market_data.providers.okx.urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_okx_candles("MU-USDT-SWAP", "15m")

        self.assertIn("Mozilla", captured["user_agent"])
        self.assertEqual("application/json", captured["accept"])

    def test_okx_fetch_retries_transient_urlopen_errors(self):
        from mu_strategy.market_data.providers.okx import fetch_okx_candles

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"code":"0","msg":"","data":[]}'

        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.URLError("transient eof")
            return FakeResponse()

        with patch("mu_strategy.market_data.providers.okx.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("mu_strategy.market_data.providers.okx.time.sleep"):
                candles = fetch_okx_candles("MU-USDT-SWAP", "15m")

        self.assertEqual([], candles)
        self.assertEqual(2, len(calls))

    def test_okx_historical_starts_pagination_at_requested_end_time(self):
        end_time_ms = 2 * 86_400_000
        requested_after_values = []

        def fake_fetch(symbol, interval, *, after=None):
            requested_after_values.append(after)
            return [_candle(86_400_000, 100)]

        with patch("mu_strategy.market_data.providers.okx.fetch_okx_candles", side_effect=fake_fetch):
            candles = fetch_okx_historical("MU-USDT-SWAP", "15m", days=1, end_time_ms=end_time_ms)

        self.assertEqual(end_time_ms, requested_after_values[0])
        self.assertEqual([86_400_000], [candle.open_time_ms for candle in candles])

    def test_okx_historical_paginates_to_latest_confirmed_requested_window(self):
        days = 14
        cases = (
            ("5m", 15 * 60_000),
            ("15m", 30 * 60_000),
            ("1h", 120 * 60_000),
        )
        for interval, confirmed_lag_ms in cases:
            with self.subTest(interval=interval):
                step_ms = interval_to_ms(interval)
                latest_confirmed_ms = days * DAY_MS
                end_time_ms = latest_confirmed_ms + confirmed_lag_ms
                wall_clock_start_ms = end_time_ms - (days * DAY_MS)
                required_start_ms = latest_confirmed_ms - (days * DAY_MS)
                first_page = _range_interval_candles(wall_clock_start_ms, latest_confirmed_ms, step_ms)
                older_page = _range_interval_candles(required_start_ms, wall_clock_start_ms - step_ms, step_ms)
                requested_after_values = []

                def fake_fetch(symbol, candidate_interval, *, after=None):
                    self.assertEqual("MU-USDT-SWAP", symbol)
                    self.assertEqual(interval, candidate_interval)
                    requested_after_values.append(after)
                    if after == end_time_ms:
                        return first_page
                    if after == wall_clock_start_ms:
                        return older_page
                    return []

                with patch("mu_strategy.market_data.providers.okx.fetch_okx_candles", side_effect=fake_fetch):
                    candles = fetch_okx_historical("MU-USDT-SWAP", interval, days=days, end_time_ms=end_time_ms)

                coverage = assess_requested_coverage(
                    candles,
                    interval=interval,
                    requested_days=days,
                    window_end_time_ms=candles[-1].open_time_ms if candles else None,
                )
                self.assertEqual([end_time_ms, wall_clock_start_ms], requested_after_values)
                self.assertTrue(coverage.covered, coverage.message)
                self.assertLessEqual(candles[0].open_time_ms, required_start_ms + step_ms)

    def test_okx_historical_does_not_hide_short_history(self):
        days = 14
        interval = "15m"
        step_ms = interval_to_ms(interval)
        latest_confirmed_ms = days * DAY_MS
        end_time_ms = latest_confirmed_ms + (30 * 60_000)
        short_start_ms = DAY_MS
        first_page = _range_interval_candles(short_start_ms, latest_confirmed_ms, step_ms)
        requested_after_values = []

        def fake_fetch(symbol, candidate_interval, *, after=None):
            self.assertEqual("MU-USDT-SWAP", symbol)
            self.assertEqual(interval, candidate_interval)
            requested_after_values.append(after)
            if after == end_time_ms:
                return first_page
            return []

        with patch("mu_strategy.market_data.providers.okx.fetch_okx_candles", side_effect=fake_fetch):
            candles = fetch_okx_historical("MU-USDT-SWAP", interval, days=days, end_time_ms=end_time_ms)

        coverage = assess_requested_coverage(
            candles,
            interval=interval,
            requested_days=days,
            window_end_time_ms=candles[-1].open_time_ms if candles else None,
        )
        self.assertEqual([end_time_ms, short_start_ms], requested_after_values)
        self.assertFalse(coverage.covered)
        self.assertIn("partial_available_history", coverage.message)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


def _range_interval_candles(start_ms: int, end_ms: int, step_ms: int) -> list[Candle]:
    rows: list[Candle] = []
    timestamp = start_ms
    while timestamp <= end_ms:
        rows.append(_candle(timestamp, 100 + len(rows)))
        timestamp += step_ms
    return rows


if __name__ == "__main__":
    unittest.main()
