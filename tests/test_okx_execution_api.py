import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mu_strategy.live.okx import (
    DemoOrderRequest,
    OKXCredentials,
    OKXInstrumentSpec,
    OKXRestClient,
    ShadowExecutionEvent,
    ShadowExecutionLedger,
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
    def test_credentials_auto_source_prefers_windows_user_env_over_stale_process_env(self):
        process_env = {
            "OKX_API_KEY": "stale-process-key",
            "OKX_SECRET_KEY": "stale-process-secret",
            "OKX_PASSPHRASE": "shared-passphrase",
        }
        user_env = {
            "OKX_API_KEY": "fresh-user-key",
            "OKX_SECRET_KEY": "fresh-user-secret",
            "OKX_PASSPHRASE": "shared-passphrase",
        }

        def read_windows_environment(prefix, scope):
            return user_env if scope == "user" else {}

        with patch.dict("os.environ", process_env, clear=True):
            with patch("mu_strategy.live.okx.os.name", "nt"):
                with patch(
                    "mu_strategy.live.okx._read_windows_environment",
                    side_effect=read_windows_environment,
                    create=True,
                ):
                    credentials = OKXCredentials.from_env()

        self.assertEqual("fresh-user-key", credentials.api_key)
        self.assertEqual("fresh-user-secret", credentials.secret_key)
        self.assertEqual("shared-passphrase", credentials.passphrase)

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


if __name__ == "__main__":
    unittest.main()
