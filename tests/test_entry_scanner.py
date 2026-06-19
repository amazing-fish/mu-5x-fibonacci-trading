import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from mu_strategy.models import Candle
from mu_strategy.strategies.registry import baseline_strategy_group


class EntryScannerTests(unittest.TestCase):
    def test_scan_entry_returns_enter_when_recent_retest_is_near_fib(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        candles[-2] = Candle(candles[-2].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.5, 101, 100.4, 100.8, 1000)
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

        self.assertEqual("enter", result.action)
        self.assertEqual("recent retest confirmed and price is near fib zone", result.reason)
        self.assertEqual("green", result.regime_1h)
        self.assertAlmostEqual(100.0, result.fib_level)
        self.assertAlmostEqual(0.008, result.fib_distance_pct)
        self.assertAlmostEqual(100.0, result.trigger_price)
        self.assertAlmostEqual(98.0, result.initial_stop)

    def test_scan_entry_second_pullback_does_not_reapply_current_filters_to_pending_fill(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        signal_index = len(candles) - 2
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.5, 101, 99.9, 100.8, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
        )

        hourly_context = {bar.open_time_ms: "green" for bar in candles}
        hourly_context[candles[-1].open_time_ms] = "red"
        rsi_values = [55.0] * len(candles)
        rsi_values[-1] = 30.0
        hist_values = [0.2] * len(candles)
        hist_values[-2] = 0.0
        hist_values[-1] = -0.2

        with patch("mu_strategy.entry.scanner.build_hourly_context", return_value=hourly_context):
            with patch("mu_strategy.entry.scanner.rsi", return_value=rsi_values):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), hist_values)):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = lambda _candles, index, _config: 100.0 if index == signal_index else None

                        result = scan_entry(
                            "BTC-USDT-SWAP",
                            candles,
                            candles,
                            config=config,
                            lookback_bars=4,
                        )

        self.assertEqual("enter", result.action)
        self.assertEqual("recent retest confirmed and price is near fib zone", result.reason)
        self.assertEqual("red", result.regime_1h)
        self.assertEqual(candles[signal_index].open_time_ms, result.signal_time_ms)
        self.assertAlmostEqual(100.0, result.trigger_price)

    def test_scan_entry_second_pullback_preserves_older_active_pending_signal(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 15))
        first_signal_index = len(candles) - 3
        later_signal_index = len(candles) - 2
        candles[first_signal_index] = Candle(candles[first_signal_index].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[later_signal_index] = Candle(candles[later_signal_index].open_time_ms, 105, 106, 104.8, 105.2, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.5, 101, 100.4, 100.5, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
        )

        with patch("mu_strategy.entry.scanner.build_hourly_context") as build_context:
            build_context.return_value = {bar.open_time_ms: "green" for bar in candles}
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = (
                            lambda _candles, index, _config: 100.0
                            if index == first_signal_index
                            else 105.0
                            if index == later_signal_index
                            else None
                        )

                        result = scan_entry(
                            "BTC-USDT-SWAP",
                            candles,
                            candles,
                            config=config,
                            lookback_bars=4,
                        )

        self.assertEqual("enter", result.action)
        self.assertEqual(candles[first_signal_index].open_time_ms, result.signal_time_ms)
        self.assertAlmostEqual(100.0, result.trigger_price)
        self.assertAlmostEqual(0.005, result.fib_distance_pct)

    def test_scan_entry_second_pullback_places_limit_when_current_close_moved_away_from_fib(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 15))
        signal_index = len(candles) - 2
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 103, 99.8, 102.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 102.5, 103, 102.0, 102.2, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
        )

        with patch("mu_strategy.entry.scanner.build_hourly_context") as build_context:
            build_context.return_value = {bar.open_time_ms: "green" for bar in candles}
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = lambda _candles, index, _config: 100.0 if index == signal_index else None

                        result = scan_entry(
                            "BTC-USDT-SWAP",
                            candles,
                            candles,
                            config=config,
                            lookback_bars=4,
                        )

        self.assertEqual("enter", result.action)
        self.assertEqual(candles[signal_index].open_time_ms, result.signal_time_ms)
        self.assertAlmostEqual(100.0, result.trigger_price)
        self.assertAlmostEqual(0.022, result.fib_distance_pct)

    def test_scan_entry_second_pullback_expires_signal_at_wait_bar_boundary(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 15))
        signal_index = len(candles) - 9
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 103, 99.8, 102.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 102.5, 103, 102.0, 102.2, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
            trading_windows_et=(("00:00", "23:59"),),
        )

        with patch("mu_strategy.entry.scanner.build_hourly_context") as build_context:
            build_context.return_value = {bar.open_time_ms: "green" for bar in candles}
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = lambda _candles, index, _config: 100.0 if index == signal_index else None

                        result = scan_entry(
                            "BTC-USDT-SWAP",
                            candles,
                            candles,
                            config=config,
                            lookback_bars=16,
                        )

        self.assertEqual("wait", result.action)
        self.assertEqual("filters are not fully blocked, but no recent confirmed fib retest", result.reason)
        self.assertIsNone(result.trigger_price)

    def test_scan_entry_waits_when_current_bar_is_outside_trading_window(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 15, 45))
        candles[-2] = Candle(candles[-2].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.5, 101, 100.4, 100.8, 1000)
        config = baseline_strategy_group("BTC-USDT-SWAP").config

        with patch("mu_strategy.entry.scanner.build_hourly_context") as build_context:
            build_context.return_value = {bar.open_time_ms: "green" for bar in candles}
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = lambda _candles, index, _config: 100.0 if index == len(candles) - 2 else None

                        result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

        self.assertEqual("wait", result.action)
        self.assertEqual("current bar is outside configured trading window", result.reason)
        self.assertIsNone(result.trigger_price)

    def test_scan_entry_ignores_signal_outside_trading_window(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        signal_index = len(candles) - 3
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.5, 101, 100.4, 100.8, 1000)
        config = baseline_strategy_group("BTC-USDT-SWAP").config

        with patch("mu_strategy.entry.scanner.build_hourly_context") as build_context:
            build_context.return_value = {bar.open_time_ms: "green" for bar in candles}
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = lambda _candles, index, _config: 100.0 if index == signal_index else None

                        result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

        self.assertEqual("wait", result.action)
        self.assertEqual("filters are not fully blocked, but no recent confirmed fib retest", result.reason)
        self.assertIsNone(result.trigger_price)

    def test_scan_entry_expires_second_pullback_after_configured_wait_window(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 19, 45))
        signal_index = len(candles) - 10
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.5, 101, 100.4, 100.8, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            second_pullback_wait_bars=8,
            trading_windows_et=(("09:00", "16:00"),),
        )

        with patch("mu_strategy.entry.scanner.build_hourly_context") as build_context:
            build_context.return_value = {bar.open_time_ms: "green" for bar in candles}
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = lambda _candles, index, _config: 100.0 if index == signal_index else None

                        result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=16)

        self.assertEqual("wait", result.action)
        self.assertEqual("filters are not fully blocked, but no recent confirmed fib retest", result.reason)
        self.assertIsNone(result.trigger_price)

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


def _candles_ending_at(last_open_time_ms: int) -> list[Candle]:
    first_open_time_ms = last_open_time_ms - (39 * 900_000)
    return [_candle(first_open_time_ms + (i * 900_000), 100 + i * 0.1) for i in range(40)]


def _utc_ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


if __name__ == "__main__":
    unittest.main()
