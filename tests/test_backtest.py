import unittest

from mu_strategy.backtest import OpenPosition, _make_fill, _tighten_stop, run_backtest
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

    def test_second_pullback_entry_waits_for_fibonacci_retest(self):
        config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("00:00", "23:59"),),
            entry_execution="second_pullback",
            second_pullback_wait_bars=3,
        )
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 101.1, 101.4, 101.0, 101.2),
            candle(3, 101.0, 101.2, 100.7, 101.0),
            candle(4, 101.0, 103.5, 100.9, 103.0),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(candles_15m[3].open_time_ms, result.trades[0].entry_time_ms)

    def test_half_protect_green_wide_stop_does_not_jump_to_first_entry_cost(self):
        config = StrategyConfig(fee_rate=0, stop_tightening="half_protect_green_wide")
        first = _make_fill(0, 100, 0.2, 10_000, config)
        second = _make_fill(900_000, 102, 0.2, 10_000, config)
        position = OpenPosition([first, second], stop_price=98, entry_anchor=100, initial_stop_price=98, max_stage=2)

        _tighten_stop(position, candle(2, 102, 103, 101, 102.5), 2, [], "green", config)

        self.assertAlmostEqual(99, position.stop_price)
        self.assertLess(position.stop_price, first.price)


if __name__ == "__main__":
    unittest.main()
