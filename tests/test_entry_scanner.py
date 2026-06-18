import unittest
from unittest.mock import patch

from mu_strategy.models import Candle
from mu_strategy.strategies.registry import baseline_strategy_group


class EntryScannerTests(unittest.TestCase):
    def test_scan_entry_returns_watch_when_recent_retest_is_near_fib(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = [_candle(i * 900_000, 100 + i * 0.1) for i in range(40)]
        candles[-2] = Candle(38 * 900_000, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(39 * 900_000, 100.5, 101, 100.4, 100.8, 1000)
        config = baseline_strategy_group("BTC-USDT-SWAP").config

        with patch("mu_strategy.entry.scanner.build_hourly_context") as build_context:
            build_context.return_value = {bar.open_time_ms: "green" for bar in candles}
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = lambda _candles, index, _config: 100.0 if index == len(candles) - 2 else None

                        result = scan_entry(
                            "BTC-USDT-SWAP",
                            candles,
                            candles,
                            config=config,
                            lookback_bars=4,
                        )

        self.assertEqual("watch", result.action)
        self.assertEqual("recent retest confirmed and price is near fib zone", result.reason)
        self.assertEqual("green", result.regime_1h)
        self.assertAlmostEqual(100.0, result.fib_level)
        self.assertAlmostEqual(0.008, result.fib_distance_pct)
        self.assertAlmostEqual(100.0, result.trigger_price)
        self.assertAlmostEqual(98.0, result.initial_stop)

    def test_scan_entry_skips_when_last_regime_is_red(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = [_candle(i * 900_000, 100 + i * 0.1) for i in range(40)]
        config = baseline_strategy_group("BTC-USDT-SWAP").config

        with patch("mu_strategy.entry.scanner.build_hourly_context") as build_context:
            build_context.return_value = {bar.open_time_ms: "red" for bar in candles}
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))):
                    result = scan_entry(
                        "BTC-USDT-SWAP",
                        candles,
                        candles,
                        config=config,
                        lookback_bars=4,
                    )

        self.assertEqual("skip", result.action)
        self.assertEqual("1h regime is red", result.reason)
        self.assertIsNone(result.trigger_price)
        self.assertIsNone(result.initial_stop)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


if __name__ == "__main__":
    unittest.main()
