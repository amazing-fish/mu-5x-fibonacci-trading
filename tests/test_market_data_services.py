import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle


class MarketDataSymbolTests(unittest.TestCase):
    def test_resolve_okx_swap_symbol_normalizes_common_inputs(self):
        from mu_strategy.market_data.symbols import resolve_okx_swap_symbol

        self.assertEqual("BTC-USDT-SWAP", resolve_okx_swap_symbol("btc").inst_id)
        self.assertEqual("BTC-USDT-SWAP", resolve_okx_swap_symbol("BTCUSDT").inst_id)
        self.assertEqual("BTC-USDT-SWAP", resolve_okx_swap_symbol("BTC-USDT-SWAP").inst_id)
        self.assertEqual("SPCX-USDT-SWAP", resolve_okx_swap_symbol("SPACEX").inst_id)


class MarketDataUniverseTests(unittest.TestCase):
    def test_select_top_okx_usdt_swaps_filters_and_sorts_by_usdt_turnover(self):
        from mu_strategy.market_data.universe import select_top_okx_usdt_swaps

        rows = [
            {"instId": "ETH-USDT-SWAP", "instType": "SWAP", "volCcy24h": "20", "last": "2000"},
            {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "volCcy24h": "50", "last": "65000"},
            {"instId": "BTC-USD-SWAP", "instType": "SWAP", "volCcy24h": "999", "last": "65000"},
            {"instId": "DOGE-USDT-SWAP", "instType": "SWAP", "volCcy24h": "0", "last": "0.1"},
            {"instId": "SOL-USDT-SWAP", "instType": "SWAP", "volCcy24h": "30", "last": "75"},
            {"instId": "PEPE-USDT-SWAP", "instType": "SWAP", "volCcy24h": "1000000", "last": "0.000001"},
        ]

        selected = select_top_okx_usdt_swaps(rows, limit=2)

        self.assertEqual(["BTC-USDT-SWAP", "ETH-USDT-SWAP"], [item.inst_id for item in selected])
        self.assertEqual([50.0, 20.0], [item.volume_ccy_24h for item in selected])


class CandleBundleServiceTests(unittest.TestCase):
    def test_refresh_candle_bundle_uses_resolved_symbol_for_15m_and_1h(self):
        from mu_strategy.market_data.service import refresh_candle_bundle

        calls = []

        def fake_cached_historical(symbol, interval, **kwargs):
            calls.append((symbol, interval, kwargs))
            return [_candle(0, 100)], Path(f"data/OKX_{symbol}_{interval}_28d.csv")

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch("mu_strategy.market_data.service.cached_historical", side_effect=fake_cached_historical):
                bundle = refresh_candle_bundle(
                    "btc",
                    intervals=("15m", "1h"),
                    days=28,
                    data_dir=data_dir,
                    refresh=False,
                )

        self.assertEqual("BTC-USDT-SWAP", bundle.symbol.inst_id)
        self.assertEqual(["15m", "1h"], list(bundle.candles_by_interval))
        self.assertEqual(["15m", "1h"], [call[1] for call in calls])
        self.assertTrue(all(call[0] == "BTC-USDT-SWAP" for call in calls))
        self.assertTrue(all(call[2]["source"] == "okx" for call in calls))
        self.assertTrue(all(call[2]["data_dir"] == data_dir for call in calls))


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


if __name__ == "__main__":
    unittest.main()
