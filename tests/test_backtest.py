import unittest

from mu_strategy.backtest import run_backtest
from mu_strategy.models import Candle
from mu_strategy.strategy import StrategyConfig


def candle(index, open_, high, low, close):
    return Candle(
        open_time_ms=index * 900_000,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


class BacktestTests(unittest.TestCase):
    def test_initial_stop_loss_is_limited_by_5x_20_percent_margin(self):
        config = StrategyConfig(fee_rate=0, trading_windows_et=(("00:00", "23:59"),))
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 100.8, 103, 100.7, 102.5),
            candle(3, 102.5, 103.5, 98.6, 100.1),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(result.trade_count, 1)
        self.assertAlmostEqual(result.trades[0].return_pct, -0.02, places=4)

    def test_fees_are_included_in_trade_return(self):
        config = StrategyConfig(fee_rate=0.001, trading_windows_et=(("00:00", "23:59"),))
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 100.8, 103.2, 100.7, 102.5),
            candle(3, 102.5, 103.5, 98.6, 100.1),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertLess(result.trades[0].return_pct, -0.02)

    def test_pyramid_adds_only_when_filters_are_green(self):
        config = StrategyConfig(fee_rate=0, trading_windows_et=(("00:00", "23:59"),))
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 100.8, 103.2, 100.7, 102.6),
            candle(3, 102.6, 105.4, 102.2, 104.6),
            candle(4, 104.6, 107.5, 104.1, 106.8),
            candle(5, 106.8, 108.0, 105.5, 107.2),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(result.trade_count, 1)
        self.assertGreaterEqual(result.trades[0].max_stage, 3)
        self.assertGreater(result.total_return_pct, 0)


if __name__ == "__main__":
    unittest.main()
