import base64
import json
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    def test_https_proxy_tunnels_with_auth_and_honors_bypass_and_direct_pooling(self):
        from mu_strategy.market_data import http_pool

        fake_context = MagicMock(spec=ssl.SSLContext)
        created = []

        def fake_https_connection(host, *, port=None, timeout=None, context=None):
            conn = MagicMock()
            created.append((host, port, timeout, context, conn))
            return conn

        with (
            patch.object(http_pool, "_DEFAULT_SSL_CONTEXT", fake_context),
            patch("mu_strategy.market_data.http_pool.http.client.HTTPSConnection", side_effect=fake_https_connection),
        ):
            # 1. Configured HTTPS proxy with credentials -> connects to proxy & sets CONNECT tunnel.
            with (
                patch(
                    "mu_strategy.market_data.http_pool.urllib.request.getproxies",
                    return_value={"https": "http://alice:s%40cret@proxy.internal:8080"},
                ),
                patch("mu_strategy.market_data.http_pool.urllib.request.proxy_bypass", return_value=False),
            ):
                http_pool._new_connection("https", "www.okx.com", None, 15.0)

            host, port, timeout, ctx, conn = created[-1]
            self.assertEqual(("proxy.internal", 8080, 15.0), (host, port, timeout))
            self.assertIs(fake_context, ctx)
            expected_auth = "Basic " + base64.b64encode(b"alice:s@cret").decode("ascii")
            conn.set_tunnel.assert_called_once_with(
                "www.okx.com",
                port=443,
                headers={"Proxy-Authorization": expected_auth},
            )

            # 2. NO_PROXY / proxy_bypass -> connects directly without tunneling.
            with (
                patch(
                    "mu_strategy.market_data.http_pool.urllib.request.getproxies",
                    return_value={"https": "http://proxy.internal:8080"},
                ),
                patch("mu_strategy.market_data.http_pool.urllib.request.proxy_bypass", return_value=True),
            ):
                http_pool._new_connection("https", "www.okx.com", 443, 15.0)

            host, port, _timeout, _ctx, conn = created[-1]
            self.assertEqual(("www.okx.com", 443), (host, port))
            conn.set_tunnel.assert_not_called()

            # 3. No proxy configured -> connects directly without tunneling.
            with patch("mu_strategy.market_data.http_pool.urllib.request.getproxies", return_value={}):
                http_pool._new_connection("https", "fapi.binance.com", None, 10.0)

            host, port, _timeout, _ctx, conn = created[-1]
            self.assertEqual(("fapi.binance.com", None), (host, port))
            conn.set_tunnel.assert_not_called()

    def test_http_proxy_forwards_absolute_uri_with_keep_alive_pooling(self):
        from mu_strategy.market_data import http_pool

        connections = []
        seen_requests = []

        class ProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                connections.append(self.client_address)

            def log_message(self, *_args):
                pass

            def do_GET(self):
                seen_requests.append(
                    (self.path, self.headers.get("Host"), self.headers.get("Proxy-Authorization"))
                )
                body = json.dumps({"ok": True, "n": len(seen_requests)}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(http_pool.close_pooled_connections)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)

        proxy_url = f"http://bob:pw@127.0.0.1:{server.server_port}"
        with (
            patch("mu_strategy.market_data.http_pool.urllib.request.getproxies", return_value={"http": proxy_url}),
            patch("mu_strategy.market_data.http_pool.urllib.request.proxy_bypass", return_value=False),
        ):
            first = http_pool.get_json("http://exchange.invalid/api/v5/ping?x=1")
            second = http_pool.get_json("http://exchange.invalid/api/v5/ping?x=2")

        self.assertEqual({"ok": True, "n": 1}, first)
        self.assertEqual({"ok": True, "n": 2}, second)
        self.assertEqual(1, len(connections), "expected HTTP proxy keep-alive socket reuse")
        expected_auth = "Basic " + base64.b64encode(b"bob:pw").decode("ascii")
        self.assertEqual(
            [
                ("http://exchange.invalid/api/v5/ping?x=1", "exchange.invalid", expected_auth),
                ("http://exchange.invalid/api/v5/ping?x=2", "exchange.invalid", expected_auth),
            ],
            seen_requests,
        )

    def test_rejects_https_proxy_for_https_target_and_unsupported_proxy_schemes_before_io(self):
        from mu_strategy.market_data import http_pool

        self.addCleanup(http_pool.close_pooled_connections)
        with (
            patch(
                "mu_strategy.market_data.http_pool.http.client.HTTPSConnection",
                side_effect=AssertionError("must fail closed before opening socket"),
            ),
            patch(
                "mu_strategy.market_data.http_pool.urllib.request.urlopen",
                side_effect=AssertionError("must never silently delegate unsupported proxy to urlopen"),
            ),
            patch("mu_strategy.market_data.http_pool.urllib.request.proxy_bypass", return_value=False),
        ):
            # 1. https:// proxy for https:// target would leak CONNECT/Proxy-Authorization before proxy TLS.
            with patch(
                "mu_strategy.market_data.http_pool.urllib.request.getproxies",
                return_value={"https": "https://alice:topsecret@proxy.internal:443"},
            ):
                with self.assertRaisesRegex(ValueError, "TLS-in-TLS") as raised:
                    http_pool.get_json("https://www.okx.com/api/v5/public/time", retries=2)
                self.assertNotIn("topsecret", str(raised.exception))
                self.assertNotIn("alice", str(raised.exception))

            # 2. socks5:// via ALL_PROXY must fail explicitly instead of silently bypassing via urllib.
            with patch(
                "mu_strategy.market_data.http_pool.urllib.request.getproxies",
                return_value={"all": "socks5://127.0.0.1:1080"},
            ):
                with self.assertRaisesRegex(ValueError, "unsupported proxy scheme 'socks5'"):
                    http_pool.get_json("https://www.okx.com/api/v5/public/time", retries=2)


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
