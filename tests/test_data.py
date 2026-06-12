import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from mu_strategy.data import (
    cache_path,
    cached_historical,
    merge_incremental_candles,
    okx_row_to_candle,
    write_csv,
)
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
        existing = [
            _candle(0, 100),
            _candle(900_000, 101),
        ]
        fetched = [
            _candle(900_000, 101.5),
            _candle(1_800_000, 102),
        ]

        merged = merge_incremental_candles(existing, fetched)

        self.assertEqual([0, 900_000, 1_800_000], [bar.open_time_ms for bar in merged])
        self.assertAlmostEqual(101.5, merged[1].close)

    def test_okx_cached_historical_updates_existing_cache_incrementally(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = cache_path("MU-USDT-SWAP", "15m", days=180, data_dir=data_dir, source="okx")

            write_csv([_candle(0, 100), _candle(900_000, 101)], path)
            with patch("mu_strategy.data.fetch_okx_incremental", return_value=[_candle(900_000, 101.5), _candle(1_800_000, 102)]):
                candles, returned_path = cached_historical(
                    "MU-USDT-SWAP",
                    "15m",
                    days=180,
                    data_dir=data_dir,
                    source="okx",
                )

        self.assertEqual(path, returned_path)
        self.assertEqual([0, 900_000, 1_800_000], [bar.open_time_ms for bar in candles])
        self.assertAlmostEqual(101.5, candles[1].close)

    def test_okx_fetch_uses_browser_user_agent(self):
        from mu_strategy.data import fetch_okx_candles

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
            return FakeResponse()

        with patch("mu_strategy.data.urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_okx_candles("MU-USDT-SWAP", "15m")

        self.assertIn("Mozilla", captured["user_agent"])


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


if __name__ == "__main__":
    unittest.main()
