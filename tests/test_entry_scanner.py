import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.models import Candle, EntryDecisionCode, EntryDecisionStage, EntryDisposition
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
        self.assertIs(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, result.decision_code)
        self.assertIs(EntryDisposition.READY, result.disposition)
        self.assertIs(EntryDecisionStage.PENDING_ENTRY, result.stage)

    def test_scan_entry_second_pullback_does_not_reapply_current_filters_to_pending_fill(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        signal_index = len(candles) - 2
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 101.5, 102, 101.2, 101.5, 1000)
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
        self.assertEqual("recent retest confirmed; resting second-pullback fib limit", result.reason)
        self.assertEqual("red", result.regime_1h)
        self.assertEqual(candles[signal_index].open_time_ms, result.signal_time_ms)
        self.assertAlmostEqual(100.0, result.trigger_price)
        self.assertIs(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, result.decision_code)
        self.assertIs(EntryDisposition.READY, result.disposition)
        self.assertIs(EntryDecisionStage.PENDING_ENTRY, result.stage)

    def test_scan_entry_second_pullback_ignores_signal_filled_by_latest_confirmed_candle(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        signal_index = len(candles) - 2
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 103, 99.8, 102.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 102.5, 103, 99.9, 101.5, 1000)
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

        self.assertEqual("wait", result.action)
        self.assertEqual("filters are not fully blocked, but no recent confirmed fib retest", result.reason)
        self.assertIsNone(result.trigger_price)
        self.assertIs(EntryDecisionCode.NO_RECENT_CONFIRMED_FIB_RETEST, result.decision_code)
        self.assertIs(EntryDisposition.WAIT, result.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, result.stage)

    def test_scan_entry_second_pullback_keeps_tolerance_only_pullback_pending(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        signal_index = len(candles) - 2
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 103, 99.8, 102.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 102.5, 103, 100.5, 101.5, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="second_pullback",
            fib_tolerance_pct=0.01,
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
        self.assertEqual("recent retest confirmed; resting second-pullback fib limit", result.reason)
        self.assertEqual(candles[signal_index].open_time_ms, result.signal_time_ms)
        self.assertAlmostEqual(100.0, result.trigger_price)
        self.assertIs(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, result.decision_code)

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
        self.assertIs(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, result.decision_code)
        self.assertIs(EntryDisposition.READY, result.disposition)
        self.assertIs(EntryDecisionStage.PENDING_ENTRY, result.stage)

    def test_scan_entry_second_pullback_ignores_signal_already_filled_after_signal(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 15))
        signal_index = len(candles) - 3
        fill_index = len(candles) - 2
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 103, 99.8, 102.5, 1000)
        candles[fill_index] = Candle(candles[fill_index].open_time_ms, 102.5, 103, 99.9, 101.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 101.5, 102, 101.0, 101.6, 1000)
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

        self.assertEqual("wait", result.action)
        self.assertEqual("filters are not fully blocked, but no recent confirmed fib retest", result.reason)
        self.assertIsNone(result.trigger_price)

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
        self.assertIs(EntryDecisionCode.CURRENT_BAR_OUTSIDE_TRADING_WINDOW, result.decision_code)
        self.assertIs(EntryDisposition.WAIT, result.disposition)
        self.assertIs(EntryDecisionStage.INPUT, result.stage)

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
        self.assertIs(EntryDecisionCode.REGIME_BLOCKED, result.decision_code)
        self.assertIs(EntryDisposition.BLOCK, result.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, result.stage)

    def test_scan_entry_without_candles_has_typed_input_wait(self):
        from mu_strategy.entry.scanner import scan_entry

        result = scan_entry(
            "BTC-USDT-SWAP",
            [],
            [],
            config=baseline_strategy_group("BTC-USDT-SWAP").config,
        )

        self.assertEqual("wait", result.action)
        self.assertEqual("no 15m candles", result.reason)
        self.assertIs(EntryDecisionCode.NO_CANDLES, result.decision_code)
        self.assertIs(EntryDisposition.WAIT, result.disposition)
        self.assertIs(EntryDecisionStage.INPUT, result.stage)

    def test_non_pending_rsi_and_macd_filters_have_typed_block_codes(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        config = baseline_strategy_group("BTC-USDT-SWAP").config
        cases = [
            (
                "rsi",
                [44.0] * len(candles),
                [0.2] * len(candles),
                "15m RSI is below floor",
                EntryDecisionCode.RSI_BELOW_FLOOR,
            ),
            (
                "macd",
                [55.0] * len(candles),
                [0.2] * (len(candles) - 2) + [0.0, -0.2],
                "15m MACD histogram still weakening",
                EntryDecisionCode.MACD_WEAKENING,
            ),
        ]

        for name, rsi_values, hist_values, reason, code in cases:
            with self.subTest(name=name):
                with patch(
                    "mu_strategy.entry.scanner.build_hourly_context",
                    return_value={bar.open_time_ms: "green" for bar in candles},
                ):
                    with patch("mu_strategy.entry.scanner.rsi", return_value=rsi_values):
                        with patch(
                            "mu_strategy.entry.scanner.macd",
                            return_value=([0.0] * len(candles), [0.0] * len(candles), hist_values),
                        ):
                            with patch("mu_strategy.entry.scanner.nearest_fib_retest_level", return_value=None):
                                result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config)

                self.assertEqual("skip", result.action)
                self.assertEqual(reason, result.reason)
                self.assertIs(code, result.decision_code)
                self.assertIs(EntryDisposition.BLOCK, result.disposition)
                self.assertIs(EntryDecisionStage.SIGNAL, result.stage)

    def test_non_second_pullback_price_away_has_typed_pending_wait(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        signal_index = len(candles) - 2
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 103, 104, 102.5, 103, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="direct_next_open",
        )

        with patch(
            "mu_strategy.entry.scanner.build_hourly_context",
            return_value={bar.open_time_ms: "green" for bar in candles},
        ):
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch(
                    "mu_strategy.entry.scanner.macd",
                    return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles)),
                ):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = (
                            lambda _candles, index, _config: 100.0 if index == signal_index else None
                        )
                        result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

        self.assertEqual("wait", result.action)
        self.assertEqual("recent retest confirmed but price has moved away from fib zone", result.reason)
        self.assertIs(EntryDecisionCode.PRICE_AWAY_FROM_FIB, result.decision_code)
        self.assertIs(EntryDisposition.WAIT, result.disposition)
        self.assertIs(EntryDecisionStage.PENDING_ENTRY, result.stage)

    def test_non_second_pullback_near_fib_has_typed_signal_ready(self):
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        signal_index = len(candles) - 2
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.5, 101, 100.4, 100.8, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="direct_next_open",
        )

        with patch(
            "mu_strategy.entry.scanner.build_hourly_context",
            return_value={bar.open_time_ms: "green" for bar in candles},
        ):
            with patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)):
                with patch(
                    "mu_strategy.entry.scanner.macd",
                    return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles)),
                ):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib:
                        nearest_fib.side_effect = (
                            lambda _candles, index, _config: 100.0 if index == signal_index else None
                        )
                        result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

        self.assertEqual("enter", result.action)
        self.assertEqual("recent retest confirmed and price is near fib zone", result.reason)
        self.assertIs(EntryDecisionCode.SIGNAL_CONFIRMED, result.decision_code)
        self.assertIs(EntryDisposition.READY, result.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, result.stage)

    def test_typed_scan_action_is_projected_from_disposition(self):
        result = EntryScanResult(
            "BTC-USDT-SWAP",
            "wait",
            "copy is not part of control flow",
            100.0,
            "red",
            40.0,
            -0.2,
            0.0,
            decision_code=EntryDecisionCode.REGIME_BLOCKED,
        )

        self.assertEqual("skip", result.action)
        self.assertIs(EntryDisposition.BLOCK, result.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, result.stage)

    def test_legacy_scan_constructor_remains_compatible(self):
        result = EntryScanResult("BTC-USDT-SWAP", "watch", "legacy", 100.0, "yellow", None, None, None)

        self.assertEqual("watch", result.action)
        self.assertIs(EntryDecisionCode.UNKNOWN, result.decision_code)
        self.assertIs(EntryDisposition.UNKNOWN, result.disposition)
        self.assertIs(EntryDecisionStage.UNKNOWN, result.stage)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


def _candles_ending_at(last_open_time_ms: int) -> list[Candle]:
    first_open_time_ms = last_open_time_ms - (39 * 900_000)
    return [_candle(first_open_time_ms + (i * 900_000), 100 + i * 0.1) for i in range(40)]


def _utc_ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


if __name__ == "__main__":
    unittest.main()
