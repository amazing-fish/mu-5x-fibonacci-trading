import time
import unittest
from pathlib import Path

from mu_strategy.demo_trading import DemoTradingConfig, run_once
from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.market_data.service import CandleBundle
from mu_strategy.market_data.symbols import ResolvedSymbol
from mu_strategy.market_data.trusted import DataStatus
from mu_strategy.market_data.universe import OKXSwapTicker
from mu_strategy.models import Candle


class DataHealthConsumerTests(unittest.TestCase):
    def test_live_demo_blocks_when_trusted_status_is_invalid(self):
        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=_Broker(),
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _invalid_bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: self.fail("invalid data must not be scanned"),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual("native_empty", result["data_errors"][0]["status_reason"])
        self.assertEqual("15m", result["data_errors"][0]["interval"])
        self.assertEqual("skip", result["scans"][0]["action"])


class _Broker:
    def get_positions(self, *, inst_type=None, inst_id=None):
        return {"code": "0", "data": [], "msg": ""}

    def get_open_orders(self, *, inst_type=None, inst_id=None):
        return {"code": "0", "data": [], "msg": ""}


def _invalid_bundle(symbol: str) -> CandleBundle:
    last_open_time_ms = int(time.time() * 1000) - 900_000
    candles = [Candle(last_open_time_ms, 100, 101, 99, 100, 1000)]
    status = DataStatus(
        symbol=symbol,
        interval="15m",
        rows=1,
        first_timestamp_ms=last_open_time_ms,
        last_timestamp_ms=last_open_time_ms,
        updated_at_ms=last_open_time_ms,
        source_file=Path("data/live/okx/BTC-USDT-SWAP/15m.csv"),
        is_valid=False,
        is_stale=False,
        reason="native_empty",
    )
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": candles, "1h": candles},
        files_by_interval={},
        days=1,
        statuses_by_interval={"15m": status},
    )


def _entry(symbol: str) -> EntryScanResult:
    return EntryScanResult(
        symbol=symbol,
        action="enter",
        reason="entry",
        last_close=100,
        regime_1h="green",
        rsi14=55,
        macd_hist=0.1,
        macd_hist_prev=0,
        trigger_price=100,
    )


if __name__ == "__main__":
    unittest.main()
