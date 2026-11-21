import ssl
import unittest
from unittest.mock import MagicMock, patch


class HttpPoolSslContextTests(unittest.TestCase):
    def test_default_ssl_context_is_cached_and_reused_across_https_connections(self):
        from mu_strategy.market_data import http_pool

        fake_context = MagicMock(spec=ssl.SSLContext)
        created_contexts = []

        def fake_https_connection(host, *, port=None, timeout=None, context=None):
            created_contexts.append((host, port, timeout, context))
            return MagicMock()

        with patch.object(http_pool, "_DEFAULT_SSL_CONTEXT", None):
            with patch("mu_strategy.market_data.http_pool.ssl.create_default_context", return_value=fake_context) as create_ctx:
                with patch("mu_strategy.market_data.http_pool.http.client.HTTPSConnection", side_effect=fake_https_connection):
                    http_pool._new_connection("https", "www.okx.com", None, 20.0)
                    http_pool._new_connection("https", "fapi.binance.com", 443, 10.0)

        create_ctx.assert_called_once_with()
        self.assertEqual(2, len(created_contexts))
        self.assertIs(fake_context, created_contexts[0][3])
        self.assertIs(fake_context, created_contexts[1][3])

    def test_custom_ssl_context_is_passed_without_loading_default_context(self):
        from mu_strategy.market_data import http_pool

        custom_context = MagicMock(spec=ssl.SSLContext)
        captured = {}

        def fake_https_connection(host, *, port=None, timeout=None, context=None):
            captured["context"] = context
            return MagicMock()

        with patch.object(http_pool, "_DEFAULT_SSL_CONTEXT", None):
            with patch("mu_strategy.market_data.http_pool.ssl.create_default_context", side_effect=AssertionError("default ctx must not be created")):
                with patch("mu_strategy.market_data.http_pool.http.client.HTTPSConnection", side_effect=fake_https_connection):
                    http_pool._new_connection("https", "www.okx.com", 443, 20.0, context=custom_context)

        self.assertIs(custom_context, captured["context"])


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
    def test_service_exports_cache_only_trusted_loader_without_legacy_cache(self):
        from mu_strategy.market_data import service
        from mu_strategy.market_data.trusted_data.compat import CandleBundle
        from mu_strategy.market_data.trusted_data.load import load_trusted_candle_bundle

        self.assertIs(load_trusted_candle_bundle, service.load_trusted_candle_bundle)
        self.assertIs(load_trusted_candle_bundle, service.refresh_trusted_candle_bundle)
        self.assertIs(CandleBundle, service.CandleBundle)
        self.assertFalse(hasattr(service, "refresh_candle_bundle"))
        self.assertFalse(hasattr(service, "cached_historical"))


if __name__ == "__main__":
    unittest.main()
