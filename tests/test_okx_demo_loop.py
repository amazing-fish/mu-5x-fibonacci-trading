import io
import json
import time
import unittest
from unittest.mock import patch

from mu_strategy.demo_trading import DemoTradingConfig, generate_client_order_id, run_once
from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.market_data.service import CandleBundle
from mu_strategy.market_data.symbols import ResolvedSymbol
from mu_strategy.market_data.universe import OKXSwapTicker
from mu_strategy.models import Candle
from mu_strategy.strategies.registry import baseline_strategy_group


class StubBroker:
    def __init__(self):
        self.calls = []

    def get_positions(self, *, inst_type=None, inst_id=None):
        self.calls.append(("get_positions", inst_type, inst_id))
        return {"code": "0", "data": [], "msg": ""}

    def get_open_orders(self, *, inst_type=None, inst_id=None):
        self.calls.append(("get_open_orders", inst_type, inst_id))
        return {"code": "0", "data": [], "msg": ""}

    def get_instruments(self, *, inst_type, inst_id):
        self.calls.append(("get_instruments", inst_type, inst_id))
        return {
            "code": "0",
            "data": [{"instId": inst_id, "tickSz": "0.1", "lotSz": "0.01", "ctVal": "0.01"}],
            "msg": "",
        }

    def set_leverage(self, *, inst_id, lever, margin_mode="isolated"):
        self.calls.append(("set_leverage", inst_id, lever, margin_mode))
        return {"code": "0", "data": [{"lever": str(lever)}], "msg": ""}

    def place_limit_buy(self, *, inst_id, size, price, client_order_id, confirm_demo_order, td_mode="isolated", pos_side=None):
        self.calls.append(("place_limit_buy", inst_id, size, price, client_order_id, confirm_demo_order, td_mode, pos_side))
        return {"code": "0", "data": [{"ordId": "1", "clOrdId": client_order_id, "sCode": "0", "sMsg": ""}], "msg": ""}

    def cancel_order(self, *, inst_id, order_id=None, client_order_id=None, confirm_demo_order):
        self.calls.append(("cancel_order", inst_id, order_id, client_order_id, confirm_demo_order))
        return {"code": "0", "data": [{"ordId": order_id or "", "clOrdId": client_order_id or "", "sCode": "0", "sMsg": ""}], "msg": ""}


class OKXDemoLoopTests(unittest.TestCase):
    def test_generate_client_order_id_is_stable_and_okx_safe(self):
        first = generate_client_order_id("BTC-USDT-SWAP", 123, 100.0)
        second = generate_client_order_id("BTC-USDT-SWAP", 123, 100.0)

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[A-Za-z0-9]{1,32}$")

    def test_run_once_dry_run_scans_without_credentials_or_private_broker(self):
        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=True),
            broker=None,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol),
        )

        self.assertEqual("dry_run", result["mode"])
        self.assertEqual("planned", result["orders"][0]["status"])
        self.assertEqual("BTC-USDT-SWAP", result["orders"][0]["symbol"])
        self.assertEqual(10.0, result["orders"][0]["notional_usdt"])

    def test_run_once_scans_with_current_baseline_strategy_config(self):
        captured = {}

        def scanner(symbol, candles_15m, candles_1h, **kwargs):
            captured["symbol"] = symbol
            captured["config"] = kwargs["config"]
            return _scan_result(symbol, action="wait", trigger_price=None)

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=True),
            broker=None,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=scanner,
        )

        self.assertEqual([], result["orders"])
        self.assertEqual("BTC-USDT-SWAP", captured["symbol"])
        self.assertEqual(baseline_strategy_group("BTC-USDT-SWAP").config, captured["config"])

    def test_run_once_blocks_when_open_exposure_limit_is_reached(self):
        class FullBroker(StubBroker):
            def get_positions(self, *, inst_type=None, inst_id=None):
                return {"code": "0", "data": [{"instId": "ETH-USDT-SWAP", "pos": "1"}], "msg": ""}

            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {
                    "code": "0",
                    "data": [
                        {"instId": "BTC-USDT-SWAP", "clOrdId": "A"},
                        {"instId": "SOL-USDT-SWAP", "clOrdId": "B"},
                    ],
                    "msg": "",
                }

        broker = FullBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol),
        )

        self.assertEqual("blocked", result["orders"][0]["status"])
        self.assertEqual("max_open_exposure_reached", result["orders"][0]["reason"])
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_blocks_initial_entry_when_symbol_position_is_open(self):
        class PositionedBroker(StubBroker):
            def get_positions(self, *, inst_type=None, inst_id=None):
                return {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "pos": "0.01"}], "msg": ""}

        broker = PositionedBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol, trigger_price=100.19),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual("blocked", result["orders"][0]["status"])
        self.assertEqual("symbol_position_already_open", result["orders"][0]["reason"])
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_blocks_initial_entry_when_symbol_pending_order_is_open(self):
        class PendingOrderBroker(StubBroker):
            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "clOrdId": "OLD-FIB-ORDER"}], "msg": ""}

        broker = PendingOrderBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol, trigger_price=100.19),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual("blocked", result["orders"][0]["status"])
        self.assertEqual("symbol_order_already_open", result["orders"][0]["reason"])
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_expires_stale_bot_limit_order_when_signal_is_no_longer_active(self):
        stale_client_order_id = generate_client_order_id("BTC-USDT-SWAP", 1, 100.0)

        class StaleOrderBroker(StubBroker):
            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "ordId": "OLD1",
                            "clOrdId": stale_client_order_id,
                            "ordType": "limit",
                            "side": "buy",
                            "state": "live",
                        }
                    ],
                    "msg": "",
                }

        broker = StaleOrderBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _scan_result(
                symbol,
                action="wait",
                trigger_price=None,
            ),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertIn("expired_orders", result)
        self.assertEqual("expired", result["expired_orders"][0]["status"])
        self.assertEqual(stale_client_order_id, result["expired_orders"][0]["client_order_id"])
        self.assertIn(("cancel_order", "BTC-USDT-SWAP", "OLD1", stale_client_order_id, True), broker.calls)
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_expires_stale_orders_before_enforcing_capacity(self):
        stale_client_order_id = generate_client_order_id("SOL-USDT-SWAP", 1, 100.0)

        class StaleLaterOrderBroker(StubBroker):
            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "SOL-USDT-SWAP",
                            "ordId": "OLD-SOL",
                            "clOrdId": stale_client_order_id,
                            "ordType": "limit",
                            "side": "buy",
                            "state": "live",
                        }
                    ],
                    "msg": "",
                }

        def scanner(symbol, candles_15m, candles_1h, **kwargs):
            if symbol == "BTC-USDT-SWAP":
                return _entry(symbol, trigger_price=100.19)
            return _scan_result(symbol, action="wait", trigger_price=None)

        broker = StaleLaterOrderBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=1, notional_usdt=10.0),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=scanner,
        )

        self.assertEqual("expired", result["expired_orders"][0]["status"])
        self.assertEqual("SOL-USDT-SWAP", result["expired_orders"][0]["symbol"])
        self.assertEqual("submitted", result["orders"][0]["status"])
        self.assertEqual("BTC-USDT-SWAP", result["orders"][0]["symbol"])
        self.assertIn(("cancel_order", "SOL-USDT-SWAP", "OLD-SOL", stale_client_order_id, True), broker.calls)
        self.assertIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_does_not_reenter_off_universe_stale_order_symbol(self):
        stale_client_order_id = generate_client_order_id("SOL-USDT-SWAP", 1, 100.0)

        class OffUniverseStaleOrderBroker(StubBroker):
            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "SOL-USDT-SWAP",
                            "ordId": "OLD-SOL",
                            "clOrdId": stale_client_order_id,
                            "ordType": "limit",
                            "side": "buy",
                            "state": "live",
                        }
                    ],
                    "msg": "",
                }

        def scanner(symbol, candles_15m, candles_1h, **kwargs):
            if symbol == "SOL-USDT-SWAP":
                return _entry(symbol, trigger_price=100.19)
            return _scan_result(symbol, action="wait", trigger_price=None)

        broker = OffUniverseStaleOrderBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3, notional_usdt=10.0),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=scanner,
        )

        self.assertEqual("expired", result["expired_orders"][0]["status"])
        self.assertEqual("SOL-USDT-SWAP", result["expired_orders"][0]["symbol"])
        self.assertEqual([], result["orders"])
        self.assertIn(("cancel_order", "SOL-USDT-SWAP", "OLD-SOL", stale_client_order_id, True), broker.calls)
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_blocks_live_demo_orders_when_market_data_is_stale(self):
        broker = StubBroker()
        stale_bundle = _stale_bundle("BTC-USDT-SWAP")

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: stale_bundle,
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol, trigger_price=100.19),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_stale", result["data_errors"][0]["reason"])
        self.assertEqual("BTC-USDT-SWAP", result["data_errors"][0]["symbol"])
        self.assertEqual("skip", result["scans"][0]["action"])
        self.assertEqual("market_data_stale", result["scans"][0]["reason"])
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_expires_stale_bot_limit_order_when_market_data_is_stale(self):
        stale_client_order_id = generate_client_order_id("BTC-USDT-SWAP", 1, 100.0)

        class StaleOrderBroker(StubBroker):
            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "ordId": "OLD1",
                            "clOrdId": stale_client_order_id,
                            "ordType": "limit",
                            "side": "buy",
                            "state": "live",
                        }
                    ],
                    "msg": "",
                }

        broker = StaleOrderBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _stale_bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: self.fail("stale data must not be scanned"),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_stale", result["data_errors"][0]["reason"])
        self.assertEqual("expired", result["expired_orders"][0]["status"])
        self.assertEqual("market_data_stale", result["expired_orders"][0]["reason"])
        self.assertEqual(stale_client_order_id, result["expired_orders"][0]["client_order_id"])
        self.assertIn(("cancel_order", "BTC-USDT-SWAP", "OLD1", stale_client_order_id, True), broker.calls)
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_expires_stale_bot_limit_order_when_candle_loader_fails(self):
        stale_client_order_id = generate_client_order_id("BTC-USDT-SWAP", 1, 100.0)

        class StaleOrderBroker(StubBroker):
            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "ordId": "OLD1",
                            "clOrdId": stale_client_order_id,
                            "ordType": "limit",
                            "side": "buy",
                            "state": "live",
                        }
                    ],
                    "msg": "",
                }

        broker = StaleOrderBroker()

        def failing_loader(symbol, **kwargs):
            raise RuntimeError("fetch timeout")

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=failing_loader,
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: self.fail("failed data load must not be scanned"),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_load_failed", result["data_errors"][0]["reason"])
        self.assertEqual("RuntimeError", result["data_errors"][0]["error_type"])
        self.assertEqual("fetch timeout", result["data_errors"][0]["message"])
        self.assertEqual("skip", result["scans"][0]["action"])
        self.assertEqual("market_data_load_failed", result["scans"][0]["reason"])
        self.assertEqual("expired", result["expired_orders"][0]["status"])
        self.assertEqual("market_data_load_failed", result["expired_orders"][0]["reason"])
        self.assertIn(("cancel_order", "BTC-USDT-SWAP", "OLD1", stale_client_order_id, True), broker.calls)
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_expires_stale_bot_limit_order_when_universe_provider_fails(self):
        stale_client_order_id = generate_client_order_id("BTC-USDT-SWAP", 1, 100.0)

        class StaleOrderBroker(StubBroker):
            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "ordId": "OLD1",
                            "clOrdId": stale_client_order_id,
                            "ordType": "limit",
                            "side": "buy",
                            "state": "live",
                        }
                    ],
                    "msg": "",
                }

        broker = StaleOrderBroker()

        def failing_universe_provider(*, limit):
            raise RuntimeError("ticker timeout")

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=failing_universe_provider,
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _scan_result(
                symbol,
                action="wait",
                trigger_price=None,
            ),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual([], result["universe"])
        self.assertEqual("universe_load_failed", result["universe_error"]["reason"])
        self.assertEqual("RuntimeError", result["universe_error"]["error_type"])
        self.assertEqual("ticker timeout", result["universe_error"]["message"])
        self.assertEqual("expired", result["expired_orders"][0]["status"])
        self.assertEqual(stale_client_order_id, result["expired_orders"][0]["client_order_id"])
        self.assertEqual([], result["orders"])
        self.assertIn(("cancel_order", "BTC-USDT-SWAP", "OLD1", stale_client_order_id, True), broker.calls)
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_blocks_live_demo_when_account_context_has_business_error(self):
        class ErrorBroker(StubBroker):
            def get_positions(self, *, inst_type=None, inst_id=None):
                return {"code": "50011", "data": [], "msg": "request failed"}

        broker = ErrorBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol),
        )

        self.assertEqual("blocked", result["mode"])
        self.assertEqual("account_context_error", result["reason"])
        self.assertEqual([], result["orders"])
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_expires_stale_bot_limit_order_when_positions_context_has_business_error(self):
        stale_client_order_id = generate_client_order_id("BTC-USDT-SWAP", 1, 100.0)

        class StaleOrderPositionErrorBroker(StubBroker):
            def get_positions(self, *, inst_type=None, inst_id=None):
                return {"code": "50011", "data": [], "msg": "request failed"}

            def get_open_orders(self, *, inst_type=None, inst_id=None):
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "ordId": "OLD1",
                            "clOrdId": stale_client_order_id,
                            "ordType": "limit",
                            "side": "buy",
                            "state": "live",
                        }
                    ],
                    "msg": "",
                }

        broker = StaleOrderPositionErrorBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=broker,
            universe_provider=lambda limit: self.fail("account error must block fresh universe entries"),
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _scan_result(
                symbol,
                action="wait",
                trigger_price=None,
            ),
        )

        self.assertEqual("blocked", result["mode"])
        self.assertEqual("account_context_error", result["reason"])
        self.assertEqual("positions", result["account_error"]["component"])
        self.assertEqual([], result["universe"])
        self.assertEqual([], result["orders"])
        self.assertEqual("expired", result["expired_orders"][0]["status"])
        self.assertEqual(stale_client_order_id, result["expired_orders"][0]["client_order_id"])
        self.assertIn(("cancel_order", "BTC-USDT-SWAP", "OLD1", stale_client_order_id, True), broker.calls)
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_sends_isolated_limit_order_when_confirmed(self):
        broker = StubBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, notional_usdt=10.0),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol, trigger_price=100.19),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual("submitted", result["orders"][0]["status"])
        self.assertIn(("set_leverage", "BTC-USDT-SWAP", 5, "isolated"), broker.calls)
        place_call = [call for call in broker.calls if call[0] == "place_limit_buy"][0]
        self.assertEqual("9.99", place_call[2])
        self.assertEqual("100.1", place_call[3])
        self.assertTrue(place_call[5])

    def test_run_once_blocks_failed_okx_order_response(self):
        class FailedOrderBroker(StubBroker):
            def place_limit_buy(self, *, inst_id, size, price, client_order_id, confirm_demo_order, td_mode="isolated", pos_side=None):
                self.calls.append(("place_limit_buy", inst_id, size, price, client_order_id, confirm_demo_order, td_mode, pos_side))
                return {"code": "51008", "data": [], "msg": "Insufficient balance"}

        broker = FailedOrderBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=2, dry_run=False, notional_usdt=10.0, max_open_positions=1),
            broker=broker,
            universe_provider=lambda limit: [
                OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0),
                OKXSwapTicker("ETH-USDT-SWAP", 101.0, 900.0),
            ],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol, trigger_price=100.19),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual(2, len(result["orders"]))
        self.assertEqual("blocked", result["orders"][0]["status"])
        self.assertEqual("order_placement_failed", result["orders"][0]["reason"])
        self.assertEqual("51008", result["orders"][0]["response"]["code"])
        self.assertEqual("blocked", result["orders"][1]["status"])
        self.assertEqual("order_placement_failed", result["orders"][1]["reason"])

    def test_run_once_blocks_failed_okx_order_data_scode(self):
        class FailedOrderDataBroker(StubBroker):
            def place_limit_buy(self, *, inst_id, size, price, client_order_id, confirm_demo_order, td_mode="isolated", pos_side=None):
                self.calls.append(("place_limit_buy", inst_id, size, price, client_order_id, confirm_demo_order, td_mode, pos_side))
                return {
                    "code": "0",
                    "data": [{"ordId": "", "clOrdId": client_order_id, "sCode": "51008", "sMsg": "Insufficient margin"}],
                    "msg": "",
                }

        broker = FailedOrderDataBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, notional_usdt=10.0),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol, trigger_price=100.19),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual("blocked", result["orders"][0]["status"])
        self.assertEqual("order_placement_failed", result["orders"][0]["reason"])
        self.assertEqual("51008", result["orders"][0]["response"]["data"][0]["sCode"])

    def test_run_once_blocks_failed_leverage_setup_response(self):
        class FailedLeverageBroker(StubBroker):
            def set_leverage(self, *, inst_id, lever, margin_mode="isolated"):
                self.calls.append(("set_leverage", inst_id, lever, margin_mode))
                return {"code": "51000", "data": [], "msg": "Leverage setting failed"}

        broker = FailedLeverageBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, notional_usdt=10.0),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _entry(symbol, trigger_price=100.19),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual("blocked", result["orders"][0]["status"])
        self.assertEqual("leverage_setup_failed", result["orders"][0]["reason"])
        self.assertEqual("51000", result["orders"][0]["response"]["code"])
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_run_once_does_not_order_on_watch_action(self):
        broker = StubBroker()

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, notional_usdt=10.0),
            broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _scan_result(symbol, action="watch", trigger_price=100.19),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertNotIn("place_limit_buy", [call[0] for call in broker.calls])

    def test_cli_once_dry_run_uses_runner_without_credentials(self):
        from mu_strategy.commands.okx_demo_loop import main

        stdout = io.StringIO()

        exit_code = main(
            ["--once", "--dry-run", "--limit", "1"],
            stdout=stdout,
            runner=lambda config, broker: {"mode": "dry_run", "limit": config.universe_limit, "broker": broker is not None},
        )

        self.assertEqual(0, exit_code)
        output = json.loads(stdout.getvalue())
        self.assertEqual({"broker": True, "limit": 1, "mode": "dry_run"}, output)

    def test_cli_confirm_mode_requires_credentials_before_running(self):
        from mu_strategy.commands.okx_demo_loop import main

        stdout = io.StringIO()

        with patch(
            "mu_strategy.commands.okx_demo_loop.OKXCredentials.from_env",
            side_effect=RuntimeError("Missing OKX_API_KEY, OKX_SECRET_KEY, or OKX_PASSPHRASE"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Missing OKX_API_KEY"):
                main(
                    ["--once", "--confirm-demo-orders"],
                    stdout=stdout,
                    runner=lambda config, broker: {"mode": "should_not_run"},
                )

        self.assertEqual("", stdout.getvalue())

    def test_run_forever_schedules_next_cycle_from_fixed_rate_tick(self):
        from mu_strategy.commands.okx_demo_loop import _run_forever

        class StopLoop(Exception):
            pass

        stdout = io.StringIO()
        sleep_calls = []
        clock_values = iter([100.0, 100.0, 112.0, 112.0])

        def clock():
            return next(clock_values)

        def sleeper(seconds):
            sleep_calls.append(seconds)
            raise StopLoop()

        with self.assertRaises(StopLoop):
            _run_forever(
                DemoTradingConfig(dry_run=True),
                broker=None,
                interval_seconds=300,
                runner=lambda config, broker: {"mode": "dry_run"},
                stdout=stdout,
                clock=clock,
                sleeper=sleeper,
            )

        self.assertEqual([288.0], sleep_calls)

    def test_run_forever_skips_missed_ticks_after_overrun(self):
        from mu_strategy.commands.okx_demo_loop import _run_forever

        class StopLoop(Exception):
            pass

        stdout = io.StringIO()
        sleep_calls = []
        clock_values = iter([100.0, 100.0, 760.0, 760.0])

        def clock():
            return next(clock_values)

        def sleeper(seconds):
            sleep_calls.append(seconds)
            raise StopLoop()

        with self.assertRaises(StopLoop):
            _run_forever(
                DemoTradingConfig(dry_run=True),
                broker=None,
                interval_seconds=300,
                runner=lambda config, broker: {"mode": "dry_run"},
                stdout=stdout,
                clock=clock,
                sleeper=sleeper,
            )

        self.assertEqual([240.0], sleep_calls)

    def test_run_forever_logs_runner_failure_and_continues_next_cycle(self):
        from mu_strategy.commands.okx_demo_loop import _run_forever

        class StopLoop(Exception):
            pass

        stdout = io.StringIO()
        sleep_calls = []
        runner_calls = []
        clock_values = iter([100.0, 100.0, 100.0, 100.0, 400.0, 400.0])

        def clock():
            return next(clock_values)

        def runner(config, broker):
            runner_calls.append(config.dry_run)
            if len(runner_calls) == 1:
                raise RuntimeError("temporary OKX timeout")
            return {"mode": "dry_run", "cycle": len(runner_calls)}

        def sleeper(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) == 2:
                raise StopLoop()

        with self.assertRaises(StopLoop):
            _run_forever(
                DemoTradingConfig(dry_run=True),
                broker=None,
                interval_seconds=300,
                runner=runner,
                stdout=stdout,
                clock=clock,
                sleeper=sleeper,
            )

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual("cycle_failed", lines[0]["mode"])
        self.assertEqual("runner_failed", lines[0]["reason"])
        self.assertEqual("RuntimeError", lines[0]["error_type"])
        self.assertEqual("temporary OKX timeout", lines[0]["message"])
        self.assertEqual({"cycle": 2, "mode": "dry_run"}, lines[1])
        self.assertEqual([300.0, 300.0], sleep_calls)
        self.assertEqual([True, True], runner_calls)


def _bundle(symbol: str) -> CandleBundle:
    last_open_time_ms = int(time.time() * 1000) - 900_000
    first_open_time_ms = last_open_time_ms - (39 * 900_000)
    candles = [
        Candle(first_open_time_ms + index * 900_000, 100.0, 101.0, 99.0, 100.0 + index * 0.01, 1000.0)
        for index in range(40)
    ]
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": candles, "1h": candles},
        files_by_interval={},
        days=28,
    )


def _stale_bundle(symbol: str) -> CandleBundle:
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={
            "15m": [Candle(0, 100.0, 101.0, 99.0, 100.0, 1000.0)],
            "1h": [Candle(0, 100.0, 101.0, 99.0, 100.0, 1000.0)],
        },
        files_by_interval={},
        days=28,
    )


def _entry(symbol: str, trigger_price: float = 100.0) -> EntryScanResult:
    return _scan_result(symbol, action="enter", trigger_price=trigger_price)


def _scan_result(symbol: str, *, action: str, trigger_price: float = 100.0) -> EntryScanResult:
    return EntryScanResult(
        symbol=symbol,
        action=action,
        reason="recent retest confirmed and price is near fib zone",
        last_close=100.5,
        regime_1h="green",
        rsi14=55.0,
        macd_hist=0.2,
        macd_hist_prev=0.1,
        fib_level=trigger_price,
        fib_distance_pct=0.005,
        trigger_price=trigger_price,
        initial_stop=98.0,
        signal_time_ms=123,
    )


if __name__ == "__main__":
    unittest.main()
