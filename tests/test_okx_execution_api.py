import io
import json
import ssl
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import mu_strategy.live.okx as okx_module
from mu_strategy.live.okx import (
    DemoOrderRequest,
    OKXCredentials,
    OKXHTTPError,
    OKXInstrumentSpec,
    OKXRestClient,
    ShadowExecutionEvent,
    ShadowExecutionLedger,
    _PooledHTTPTransport,
    build_shadow_event,
)


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout": timeout,
            }
        )
        return {"code": "0", "data": [{"ok": True}], "msg": ""}


class OKXRestClientTests(unittest.TestCase):
    def test_credentials_auto_source_prioritizes_process_env_over_windows_registry(self):
        process_env = {
            "OKX_API_KEY": "process-key",
            "OKX_SECRET_KEY": "process-secret",
            "OKX_PASSPHRASE": "process-passphrase",
        }
        user_env = {
            "OKX_API_KEY": "registry-user-key",
            "OKX_SECRET_KEY": "registry-user-secret",
            "OKX_PASSPHRASE": "registry-passphrase",
        }

        with patch.dict("os.environ", process_env, clear=True):
            with patch("mu_strategy.live.okx.os.name", "nt"):
                with patch(
                    "mu_strategy.live.okx._read_windows_environment",
                    return_value=user_env,
                ) as read_winreg:
                    credentials = OKXCredentials.from_env()

        self.assertEqual("process-key", credentials.api_key)
        self.assertEqual("process-secret", credentials.secret_key)
        self.assertEqual("process-passphrase", credentials.passphrase)
        read_winreg.assert_not_called()

    def test_credentials_auto_source_falls_back_to_windows_registry_when_process_env_is_incomplete(self):
        process_env = {
            "OKX_API_KEY": "only-partial-key",
        }
        user_env = {
            "OKX_API_KEY": "registry-user-key",
            "OKX_SECRET_KEY": "registry-user-secret",
            "OKX_PASSPHRASE": "registry-passphrase",
        }

        def read_windows_environment(prefix, scope):
            return user_env if scope == "user" else {}

        with patch.dict("os.environ", process_env, clear=True):
            with patch("mu_strategy.live.okx.os.name", "nt"):
                with patch(
                    "mu_strategy.live.okx._read_windows_environment",
                    side_effect=read_windows_environment,
                ):
                    credentials = OKXCredentials.from_env()

        self.assertEqual("registry-user-key", credentials.api_key)
        self.assertEqual("registry-user-secret", credentials.secret_key)
        self.assertEqual("registry-passphrase", credentials.passphrase)

    def test_credentials_process_source_keeps_explicit_process_override(self):
        process_env = {
            "OKX_API_KEY": "process-key",
            "OKX_SECRET_KEY": "process-secret",
            "OKX_PASSPHRASE": "process-passphrase",
            "OKX_ENV_SOURCE": "process",
        }
        user_env = {
            "OKX_API_KEY": "user-key",
            "OKX_SECRET_KEY": "user-secret",
            "OKX_PASSPHRASE": "user-passphrase",
        }

        with patch.dict("os.environ", process_env, clear=True):
            with patch("mu_strategy.live.okx.os.name", "nt"):
                with patch(
                    "mu_strategy.live.okx._read_windows_environment",
                    return_value=user_env,
                ):
                    credentials = OKXCredentials.from_env()

        self.assertEqual("process-key", credentials.api_key)
        self.assertEqual("process-secret", credentials.secret_key)
        self.assertEqual("process-passphrase", credentials.passphrase)

    def test_credentials_auto_source_falls_back_to_process_when_windows_user_env_is_incomplete(self):
        process_env = {
            "OKX_API_KEY": "process-key",
            "OKX_SECRET_KEY": "process-secret",
            "OKX_PASSPHRASE": "process-passphrase",
        }
        incomplete_user_env = {
            "OKX_API_KEY": "user-key",
            "OKX_SECRET_KEY": "",
            "OKX_PASSPHRASE": "user-passphrase",
        }

        with patch.dict("os.environ", process_env, clear=True):
            with patch("mu_strategy.live.okx.os.name", "nt"):
                with patch(
                    "mu_strategy.live.okx._read_windows_environment",
                    return_value=incomplete_user_env,
                ):
                    credentials = OKXCredentials.from_env()

        self.assertEqual("process-key", credentials.api_key)
        self.assertEqual("process-secret", credentials.secret_key)
        self.assertEqual("process-passphrase", credentials.passphrase)

    def test_read_only_private_request_signs_without_exposing_secret(self):
        transport = RecordingTransport()
        credentials = OKXCredentials(
            api_key="key",
            secret_key="secret",
            passphrase="passphrase",
        )
        client = OKXRestClient(
            credentials=credentials,
            demo=True,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )

        response = client.get_balance(ccy="USDT")

        self.assertEqual({"code": "0", "data": [{"ok": True}], "msg": ""}, response)
        call = transport.calls[0]
        self.assertEqual("GET", call["method"])
        self.assertTrue(call["url"].endswith("/api/v5/account/balance?ccy=USDT"))
        self.assertEqual("key", call["headers"]["OK-ACCESS-KEY"])
        self.assertIn("OK-ACCESS-SIGN", call["headers"])
        self.assertEqual("1", call["headers"]["x-simulated-trading"])
        self.assertNotIn("secret", json.dumps(call))
        self.assertIsNone(call["body"])

    def test_public_instrument_request_does_not_require_credentials(self):
        transport = RecordingTransport()
        client = OKXRestClient(credentials=None, demo=True, transport=transport)

        client.get_instruments(inst_type="SWAP", inst_id="MU-USDT-SWAP")

        call = transport.calls[0]
        self.assertEqual("GET", call["method"])
        self.assertTrue(call["url"].endswith("/api/v5/public/instruments?instType=SWAP&instId=MU-USDT-SWAP"))
        self.assertNotIn("OK-ACCESS-KEY", call["headers"])
        self.assertEqual("1", call["headers"]["x-simulated-trading"])
        self.assertEqual("Mozilla/5.0", call["headers"]["User-Agent"])
        self.assertEqual("application/json", call["headers"]["Accept"])

    def test_demo_public_instrument_request_uses_simulated_trading_header(self):
        transport = RecordingTransport()
        client = OKXRestClient(credentials=None, demo=True, transport=transport)

        client.get_instruments(inst_type="SWAP", inst_id="MU-USDT-SWAP")

        self.assertEqual("1", transport.calls[0]["headers"]["x-simulated-trading"])

    def test_set_leverage_sends_isolated_five_x_demo_private_request(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )

        client.set_leverage(inst_id="BTC-USDT-SWAP", lever=5, margin_mode="isolated")

        call = transport.calls[0]
        self.assertEqual("POST", call["method"])
        self.assertTrue(call["url"].endswith("/api/v5/account/set-leverage"))
        self.assertEqual("1", call["headers"]["x-simulated-trading"])
        self.assertEqual(
            '{"instId":"BTC-USDT-SWAP","lever":"5","mgnMode":"isolated"}',
            call["body"],
        )

    def test_set_leverage_cannot_be_sent_with_production_client(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=False,
            transport=transport,
        )

        with self.assertRaisesRegex(PermissionError, "demo"):
            client.set_leverage(inst_id="BTC-USDT-SWAP", lever=5, margin_mode="isolated")

        self.assertEqual([], transport.calls)

    def test_get_open_orders_adds_swap_and_symbol_filters(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )

        client.get_open_orders(inst_type="SWAP", inst_id="BTC-USDT-SWAP")

        call = transport.calls[0]
        self.assertEqual("GET", call["method"])
        self.assertTrue(call["url"].endswith("/api/v5/trade/orders-pending?instType=SWAP&instId=BTC-USDT-SWAP"))
        self.assertEqual("1", call["headers"]["x-simulated-trading"])

    def test_instrument_spec_rounds_limit_price_and_size_for_notional(self):
        spec = OKXInstrumentSpec.from_row(
            {
                "instId": "BTC-USDT-SWAP",
                "tickSz": "0.1",
                "lotSz": "0.01",
                "ctVal": "0.01",
            }
        )

        self.assertEqual("100.1", spec.price_to_string(100.19))
        self.assertEqual("9.99", spec.size_for_notional(10.0, price=100.1))

    def test_place_limit_buy_uses_isolated_limit_order(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )

        response = client.place_limit_buy(
            inst_id="BTC-USDT-SWAP",
            size="0.01",
            price="65000.1",
            client_order_id="DEMO3",
            confirm_demo_order=True,
        )

        self.assertEqual({"code": "0", "data": [{"ok": True}], "msg": ""}, response)
        self.assertEqual(
            '{"instId":"BTC-USDT-SWAP","tdMode":"isolated","side":"buy","ordType":"limit","sz":"0.01","px":"65000.1","clOrdId":"DEMO3"}',
            transport.calls[0]["body"],
        )

    def test_cancel_order_uses_client_order_id_endpoint(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )

        self.assertTrue(hasattr(client, "cancel_order"), "OKXRestClient needs a cancel_order wrapper")

        response = client.cancel_order(
            inst_id="BTC-USDT-SWAP",
            client_order_id="DEMO3",
            confirm_demo_order=True,
        )

        self.assertEqual({"code": "0", "data": [{"ok": True}], "msg": ""}, response)
        call = transport.calls[0]
        self.assertEqual("POST", call["method"])
        self.assertTrue(call["url"].endswith("/api/v5/trade/cancel-order"))
        self.assertEqual("1", call["headers"]["x-simulated-trading"])
        self.assertEqual(
            '{"instId":"BTC-USDT-SWAP","clOrdId":"DEMO3"}',
            call["body"],
        )

    def test_cancel_order_cannot_be_sent_with_production_client(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=False,
            transport=transport,
        )

        with self.assertRaisesRegex(PermissionError, "demo"):
            client.cancel_order(
                inst_id="BTC-USDT-SWAP",
                client_order_id="DEMO3",
                confirm_demo_order=True,
            )

        self.assertEqual([], transport.calls)


class ShadowExecutionLedgerTests(unittest.TestCase):
    def test_shadow_event_is_append_only_and_metrics_are_computed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ShadowExecutionLedger(Path(tmp) / "shadow.jsonl")
            first = ShadowExecutionEvent(
                event_id="evt-1",
                symbol="MU-USDT-SWAP",
                action="buy",
                plan_price=100.0,
                observed_price=100.3,
                quantity=2.0,
                status="filled",
                reason="demo",
                timestamp_ms=1,
            )
            second = ShadowExecutionEvent(
                event_id="evt-2",
                symbol="MU-USDT-SWAP",
                action="buy",
                plan_price=100.0,
                observed_price=None,
                quantity=2.0,
                status="missed",
                reason="timeout",
                timestamp_ms=2,
            )

            ledger.append(first)
            ledger.append(second)

            rows = ledger.read_events()
            metrics = ledger.metrics()

            self.assertEqual([first, second], rows)
            self.assertEqual(2, metrics.total_events)
            self.assertEqual(0.5, metrics.fill_rate)
            self.assertAlmostEqual(30.0, metrics.average_slippage_bps)

    def test_build_shadow_event_uses_plan_price_without_sending_order(self):
        event = build_shadow_event(
            event_id="evt-3",
            symbol="MU-USDT-SWAP",
            action="buy",
            plan_price=105.5,
            observed_price=105.6,
            quantity=1.25,
            status="paper",
            reason="signal allowed",
            timestamp_ms=123,
        )

        self.assertEqual("paper", event.status)
        self.assertEqual("MU-USDT-SWAP", event.symbol)


class DemoOrderGuardTests(unittest.TestCase):
    def test_demo_order_dry_run_returns_request_without_transport_call(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )
        request = DemoOrderRequest(
            inst_id="MU-USDT-SWAP",
            side="buy",
            size="1",
            order_type="limit",
            price="100",
            client_order_id="SHADOW1",
        )

        prepared = client.prepare_demo_order(request)

        self.assertEqual("/api/v5/trade/order", prepared.path)
        self.assertEqual("POST", prepared.method)
        self.assertEqual("1", prepared.headers["x-simulated-trading"])
        self.assertEqual([], transport.calls)

    def test_demo_order_requires_explicit_confirm_before_network_call(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )
        request = DemoOrderRequest(
            inst_id="MU-USDT-SWAP",
            side="buy",
            size="1",
            order_type="market",
            client_order_id="SHADOW2",
        )

        with self.assertRaisesRegex(PermissionError, "confirm_demo_order"):
            client.place_demo_order(request, confirm_demo_order=False)

        response = client.place_demo_order(request, confirm_demo_order=True)

        self.assertEqual({"code": "0", "data": [{"ok": True}], "msg": ""}, response)
        self.assertEqual(1, len(transport.calls))
        self.assertEqual("POST", transport.calls[0]["method"])
        self.assertEqual('{"instId":"MU-USDT-SWAP","tdMode":"isolated","side":"buy","ordType":"market","sz":"1","clOrdId":"SHADOW2"}', transport.calls[0]["body"])

    def test_demo_order_reduce_only_serializes_as_json_boolean(self):
        transport = RecordingTransport()
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )
        request = DemoOrderRequest(
            inst_id="MU-USDT-SWAP",
            side="sell",
            size="1",
            order_type="market",
            reduce_only=True,
        )

        client.place_demo_order(request, confirm_demo_order=True)

        self.assertEqual(
            '{"instId":"MU-USDT-SWAP","tdMode":"isolated","side":"sell","ordType":"market","sz":"1","reduceOnly":true}',
            transport.calls[0]["body"],
        )

    def test_demo_order_rejects_client_order_id_with_non_alphanumeric_characters(self):
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=RecordingTransport(),
        )

        with self.assertRaisesRegex(ValueError, "client_order_id"):
            client.prepare_demo_order(
                DemoOrderRequest(
                    inst_id="MU-USDT-SWAP",
                    side="buy",
                    size="1",
                    client_order_id="demo-001",
                )
            )

    def test_demo_order_rejects_price_on_market_order(self):
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=RecordingTransport(),
        )

        with self.assertRaisesRegex(ValueError, "price"):
            client.prepare_demo_order(
                DemoOrderRequest(
                    inst_id="MU-USDT-SWAP",
                    side="buy",
                    size="1",
                    order_type="market",
                    price="100",
                )
            )

    def test_demo_order_cannot_be_sent_with_production_client(self):
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=False,
            transport=RecordingTransport(),
        )

        with self.assertRaisesRegex(PermissionError, "demo"):
            client.prepare_demo_order(DemoOrderRequest(inst_id="MU-USDT-SWAP", side="buy", size="1"))

    def test_dead_floor_to_step_removed(self):
        self.assertFalse(hasattr(okx_module, "_floor_to_step"))


class PooledHTTPTransportTests(unittest.TestCase):
    def _start_server(self, handler_cls):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_keep_alive_reuses_single_tcp_connection_and_decodes_http_errors(self):
        connections = []
        requests_seen = []

        class KeepAliveHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                connections.append(self.client_address)

            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests_seen.append(self.path)
                if "fail400" in self.path:
                    payload = json.dumps({"code": "51001", "msg": "Instrument does not exist", "data": []}).encode()
                    self.send_response(400)
                else:
                    payload = json.dumps({"code": "0", "data": [{"ok": len(requests_seen)}], "msg": ""}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        base_url = self._start_server(KeepAliveHandler)
        transport = _PooledHTTPTransport(max_get_attempts=3, backoff_seconds=0.0, sleeper=lambda _s: None)
        self.addCleanup(transport.close)
        client = OKXRestClient(credentials=None, demo=True, base_url=base_url, transport=transport)

        first = client.get_instruments(inst_type="SWAP", inst_id="MU-USDT-SWAP")
        second = client.get_instruments(inst_type="SWAP", inst_id="BTC-USDT-SWAP")
        self.assertEqual("0", first["code"])
        self.assertEqual("0", second["code"])
        self.assertEqual(1, len(connections), "expected HTTP keep-alive to reuse one TCP socket")

        with self.assertRaises(OKXHTTPError) as raised:
            client.get_instruments(inst_type="SWAP", inst_id="fail400")
        self.assertEqual(400, raised.exception.status)
        self.assertEqual("51001", raised.exception.code)
        self.assertEqual("Instrument does not exist", raised.exception.msg)
        self.assertEqual({"code": "51001", "msg": "Instrument does not exist", "data": []}, raised.exception.payload)
        self.assertIn("51001", str(raised.exception))
        # Non-retryable 400 must not be retried.
        self.assertEqual(3, len(requests_seen))

    def test_idempotent_get_retries_transient_503_and_429_but_post_never_retries(self):
        get_attempts = 0
        post_attempts = 0
        sleeps = []

        class TransientErrorHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                nonlocal get_attempts
                get_attempts += 1
                if get_attempts == 1:
                    body = json.dumps({"code": "50011", "msg": "Too Many Requests", "data": []}).encode()
                    self.send_response(429)
                    self.send_header("Retry-After", "0.05")
                elif get_attempts == 2:
                    body = b"temporary gateway error"
                    self.send_response(503)
                else:
                    body = json.dumps({"code": "0", "data": [{"recovered": True}], "msg": ""}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                nonlocal post_attempts
                post_attempts += 1
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                body = json.dumps({"code": "50001", "msg": "Service temporarily unavailable", "data": []}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        base_url = self._start_server(TransientErrorHandler)
        transport = _PooledHTTPTransport(max_get_attempts=3, backoff_seconds=0.1, sleeper=sleeps.append)
        self.addCleanup(transport.close)
        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            base_url=base_url,
            transport=transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )

        result = client.get_instruments(inst_type="SWAP", inst_id="MU-USDT-SWAP")
        self.assertEqual("0", result["code"])
        self.assertEqual(3, get_attempts)
        self.assertEqual([0.05, 0.2], sleeps)

        # Non-idempotent POST order placement must NEVER retry on 503.
        with self.assertRaises(OKXHTTPError) as raised:
            client.place_demo_order(
                DemoOrderRequest(inst_id="MU-USDT-SWAP", side="buy", size="1", order_type="market", client_order_id="NORE1"),
                confirm_demo_order=True,
            )
        self.assertEqual(1, post_attempts)
        self.assertEqual(503, raised.exception.status)
        self.assertEqual("50001", raised.exception.code)

    def test_urllib_http_error_from_custom_transport_preserves_okx_json_payload(self):
        def failing_transport(method, url, *, headers, body, timeout):
            fp = io.BytesIO(json.dumps({"code": "50111", "msg": "Invalid OK-ACCESS-KEY", "data": []}).encode())
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, fp)

        client = OKXRestClient(
            credentials=OKXCredentials("key", "secret", "passphrase"),
            demo=True,
            transport=failing_transport,
            timestamp_factory=lambda: "2026-06-17T00:00:00.000Z",
        )
        with self.assertRaises(OKXHTTPError) as raised:
            client.get_balance(ccy="USDT")
        self.assertEqual(401, raised.exception.status)
        self.assertEqual("50111", raised.exception.code)
        self.assertEqual("Invalid OK-ACCESS-KEY", raised.exception.msg)

    def test_stale_keep_alive_socket_reconnects_automatically_on_get(self):
        calls = 0

        class DropAfterFirstHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                nonlocal calls
                calls += 1
                payload = json.dumps({"code": "0", "data": [{"call": calls}], "msg": ""}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                if calls == 1:
                    # Force-close the server side without a Connection: close header so the
                    # client sees a stale keep-alive socket on its next request.
                    self.close_connection = True

        base_url = self._start_server(DropAfterFirstHandler)
        with OKXRestClient(credentials=None, demo=True, base_url=base_url) as client:
            first = client.get_instruments(inst_type="SWAP", inst_id="MU-USDT-SWAP")
            second = client.get_instruments(inst_type="SWAP", inst_id="MU-USDT-SWAP")
        self.assertEqual(1, first["data"][0]["call"])
        self.assertEqual(2, second["data"][0]["call"])

    def test_https_transport_caches_and_reuses_default_ssl_context(self):
        fake_context = MagicMock(spec=ssl.SSLContext)
        seen_contexts = []

        def fake_https_connection(host, port, *, timeout=None, context=None):
            seen_contexts.append((host, port, timeout, context))
            conn = MagicMock()
            conn.sock = None
            return conn

        transport = _PooledHTTPTransport()
        self.addCleanup(transport.close)
        with patch.object(okx_module, "_DEFAULT_SSL_CONTEXT", None):
            with patch("mu_strategy.live.okx.ssl.create_default_context", return_value=fake_context) as create_ctx:
                with patch("mu_strategy.live.okx.http.client.HTTPSConnection", side_effect=fake_https_connection):
                    transport._acquire(("https", "www.okx.com", 443, 20))
                    transport._acquire(("https", "aws.okx.com", 443, 20))

        create_ctx.assert_called_once_with()
        self.assertEqual(2, len(seen_contexts))
        self.assertIs(fake_context, seen_contexts[0][3])
        self.assertIs(fake_context, seen_contexts[1][3])

    def test_https_transport_uses_custom_ssl_context_when_supplied(self):
        custom_context = MagicMock(spec=ssl.SSLContext)
        captured = {}

        def fake_https_connection(host, port, *, timeout=None, context=None):
            captured["context"] = context
            conn = MagicMock()
            conn.sock = None
            return conn

        client = OKXRestClient(credentials=None, demo=True, ssl_context=custom_context)
        self.addCleanup(client.close)
        with patch.object(okx_module, "_DEFAULT_SSL_CONTEXT", None):
            with patch("mu_strategy.live.okx.ssl.create_default_context", side_effect=AssertionError("default ctx must not be created")):
                with patch("mu_strategy.live.okx.http.client.HTTPSConnection", side_effect=fake_https_connection):
                    client._owned_transport._acquire(("https", "www.okx.com", 443, 20))

        self.assertIs(custom_context, captured["context"])

    def test_https_transport_honors_configured_proxy_and_bypass(self):
        fake_context = MagicMock(spec=ssl.SSLContext)
        created = []

        def fake_https_connection(host, port, *, timeout=None, context=None):
            conn = MagicMock()
            conn.sock = None
            created.append((host, port, timeout, context, conn))
            return conn

        transport = _PooledHTTPTransport(ssl_context=fake_context)
        self.addCleanup(transport.close)
        with patch("mu_strategy.live.okx.http.client.HTTPSConnection", side_effect=fake_https_connection):
            with (
                patch(
                    "mu_strategy.market_data.http_pool.urllib.request.getproxies",
                    return_value={"https": "http://u:p@proxy.local:8443"},
                ),
                patch("mu_strategy.market_data.http_pool.urllib.request.proxy_bypass", return_value=False),
            ):
                transport._acquire(("https", "www.okx.com", 443, 20))

            host, port, timeout, ctx, conn = created[-1]
            self.assertEqual(("proxy.local", 8443, 20), (host, port, timeout))
            self.assertIs(fake_context, ctx)
            conn.set_tunnel.assert_called_once()
            self.assertEqual(("www.okx.com",), conn.set_tunnel.call_args.args)
            self.assertEqual(443, conn.set_tunnel.call_args.kwargs["port"])
            self.assertIn("Proxy-Authorization", conn.set_tunnel.call_args.kwargs["headers"])

            with (
                patch(
                    "mu_strategy.market_data.http_pool.urllib.request.getproxies",
                    return_value={"https": "http://proxy.local:8443"},
                ),
                patch("mu_strategy.market_data.http_pool.urllib.request.proxy_bypass", return_value=True),
            ):
                transport._acquire(("https", "aws.okx.com", 443, 20))

            host, port, _timeout, _ctx, conn = created[-1]
            self.assertEqual(("aws.okx.com", 443), (host, port))
            conn.set_tunnel.assert_not_called()

    def test_http_proxy_routes_client_requests_with_keep_alive(self):
        connections = []
        seen_paths = []

        class ProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                connections.append(self.client_address)

            def log_message(self, *_args):
                pass

            def do_GET(self):
                seen_paths.append((self.path, self.headers.get("Host")))
                payload = json.dumps({"code": "0", "data": [{"ok": len(seen_paths)}], "msg": ""}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        proxy_url = self._start_server(ProxyHandler)
        with (
            patch("mu_strategy.market_data.http_pool.urllib.request.getproxies", return_value={"http": proxy_url}),
            patch("mu_strategy.market_data.http_pool.urllib.request.proxy_bypass", return_value=False),
        ):
            with OKXRestClient(credentials=None, demo=True, base_url="http://okx.invalid") as client:
                first = client.get_instruments(inst_type="SWAP", inst_id="MU-USDT-SWAP")
                second = client.get_instruments(inst_type="SWAP", inst_id="BTC-USDT-SWAP")

        self.assertEqual("0", first["code"])
        self.assertEqual("0", second["code"])
        self.assertEqual(1, len(connections))
        self.assertEqual(
            [
                ("http://okx.invalid/api/v5/public/instruments?instType=SWAP&instId=MU-USDT-SWAP", "okx.invalid"),
                ("http://okx.invalid/api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP", "okx.invalid"),
            ],
            seen_paths,
        )

    def test_transport_rejects_https_proxy_for_https_target_and_socks_proxy_before_io(self):
        transport = _PooledHTTPTransport(max_get_attempts=3, backoff_seconds=0.0, sleeper=lambda _s: None)
        self.addCleanup(transport.close)
        with (
            patch(
                "mu_strategy.live.okx.http.client.HTTPSConnection",
                side_effect=AssertionError("must fail closed before opening socket"),
            ),
            patch("mu_strategy.market_data.http_pool.urllib.request.proxy_bypass", return_value=False),
        ):
            with patch(
                "mu_strategy.market_data.http_pool.urllib.request.getproxies",
                return_value={"https": "https://user:s3cr3t@proxy.local:443"},
            ):
                with self.assertRaisesRegex(ValueError, "TLS-in-TLS") as raised:
                    transport("GET", "https://www.okx.com/api/v5/public/time", headers={}, body=None, timeout=10)
                self.assertNotIn("s3cr3t", str(raised.exception))
                self.assertNotIn("user", str(raised.exception))

            with patch(
                "mu_strategy.market_data.http_pool.urllib.request.getproxies",
                return_value={"all": "socks5://127.0.0.1:1080"},
            ):
                with self.assertRaisesRegex(ValueError, "unsupported proxy scheme 'socks5'"):
                    transport("GET", "https://www.okx.com/api/v5/public/time", headers={}, body=None, timeout=10)


if __name__ == "__main__":
    unittest.main()
