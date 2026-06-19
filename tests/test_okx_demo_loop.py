import io
import json
import unittest
from unittest.mock import patch

from mu_strategy.demo_trading import DemoTradingConfig, generate_client_order_id, run_once
from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.market_data.service import CandleBundle
from mu_strategy.market_data.symbols import ResolvedSymbol
from mu_strategy.market_data.universe import OKXSwapTicker
from mu_strategy.models import Candle


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


def _bundle(symbol: str) -> CandleBundle:
    candles = [
        Candle(index * 900_000, 100.0, 101.0, 99.0, 100.0 + index * 0.01, 1000.0)
        for index in range(40)
    ]
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": candles, "1h": candles},
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
