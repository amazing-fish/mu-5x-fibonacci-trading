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
        self.assertAlmostEqual(99.8, result.initial_stop)
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

        # Without an active pending order, latest-bar signal filters still block cleanly.
        with patch("mu_strategy.entry.scanner.build_hourly_context", return_value=hourly_context):
            with patch("mu_strategy.entry.scanner.rsi", return_value=rsi_values):
                with patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), hist_values)):
                    with patch("mu_strategy.entry.scanner.nearest_fib_retest_level", return_value=None):
                        blocked = scan_entry(
                            "BTC-USDT-SWAP",
                            candles,
                            candles,
                            config=config,
                            lookback_bars=4,
                        )

        self.assertEqual("skip", blocked.action)
        self.assertIs(EntryDecisionCode.REGIME_BLOCKED, blocked.decision_code)

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

    def test_scan_entry_second_pullback_preserves_older_active_pending_signal_without_duplicate_scan(self):
        import mu_strategy.entry.scanner as scanner_mod
        from mu_strategy.entry.scanner import scan_entry

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 15))
        first_signal_index = len(candles) - 3
        later_signal_index = len(candles) - 2
        candles[first_signal_index] = Candle(candles[first_signal_index].open_time_ms, 100, 101, 99.8, 100.5, 1000)
        candles[later_signal_index] = Candle(candles[later_signal_index].open_time_ms, 105, 106, 104.8, 105.2, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 105.5, 106, 105.4, 105.5, 1000)
        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
        )

        with (
            patch("mu_strategy.entry.scanner.build_hourly_context", return_value={bar.open_time_ms: "green" for bar in candles}),
            patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)),
            patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))),
            patch("mu_strategy.entry.scanner.nearest_fib_retest_level") as nearest_fib,
            patch.object(scanner_mod, "_latest_recent_signal", wraps=scanner_mod._latest_recent_signal) as latest_spy,
        ):
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

        self.assertEqual(1, latest_spy.call_count)
        self.assertEqual("enter", result.action)
        self.assertEqual(candles[first_signal_index].open_time_ms, result.signal_time_ms)
        self.assertAlmostEqual(100.0, result.trigger_price)
        self.assertAlmostEqual((105.5 / 100.0) - 1, result.fib_distance_pct)

    def test_scan_entry_honors_config_allowed_regimes_for_green_only(self):
        from mu_strategy.entry.scanner import scan_entry
        from mu_strategy.strategies.registry import strategy_group

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        config = strategy_group("baseline_green_only_wide", "BTC-USDT-SWAP").config

        with (
            patch("mu_strategy.entry.scanner.build_hourly_context", return_value={bar.open_time_ms: "yellow" for bar in candles}),
            patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)),
            patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))),
        ):
            result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

        self.assertEqual("skip", result.action)
        self.assertEqual("yellow", result.regime_1h)
        self.assertIs(EntryDecisionCode.REGIME_BLOCKED, result.decision_code)

    def test_scan_entry_enforces_should_execute_entry_for_break_high_and_optimized_v2(self):
        from mu_strategy.entry.scanner import scan_entry
        from mu_strategy.strategies.registry import strategy_group

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
        signal_index = len(candles) - 2
        # Case 1: next candle does not break signal high -> wait with NEXT_CANDLE_DID_NOT_BREAK_SIGNAL_HIGH.
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100.0, 101.0, 99.8, 100.5, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.4, 100.9, 100.2, 100.6, 1000)
        config = strategy_group("optimized_v2", "BTC-USDT-SWAP").config

        with (
            patch("mu_strategy.entry.scanner.build_hourly_context", return_value={bar.open_time_ms: "green" for bar in candles}),
            patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)),
            patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))),
            patch("mu_strategy.entry.scanner.nearest_fib_retest_level", side_effect=lambda _c, idx, _cfg: 100.0 if idx == signal_index else None),
        ):
            no_break = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

        self.assertEqual("wait", no_break.action)
        self.assertIs(EntryDecisionCode.NEXT_CANDLE_DID_NOT_BREAK_SIGNAL_HIGH, no_break.decision_code)

        # Case 2: next candle breaks high, but signal candle range exceeds max_signal_range_pct (0.015) -> skip.
        candles[signal_index] = Candle(candles[signal_index].open_time_ms, 100.0, 101.0, 98.8, 100.2, 1000)
        candles[-1] = Candle(candles[-1].open_time_ms, 100.2, 101.2, 100.0, 100.8, 1000)
        with (
            patch("mu_strategy.entry.scanner.build_hourly_context", return_value={bar.open_time_ms: "green" for bar in candles}),
            patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)),
            patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))),
            patch("mu_strategy.entry.scanner.nearest_fib_retest_level", side_effect=lambda _c, idx, _cfg: 100.0 if idx == signal_index else None),
        ):
            wide_bar = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

        self.assertEqual("skip", wide_bar.action)
        self.assertIs(EntryDecisionCode.SIGNAL_CANDLE_TOO_WIDE, wide_bar.decision_code)

        # Case 3: valid signal on latest bar for break_high waits for NEXT_CANDLE_REQUIRED.
        candles[-1] = Candle(candles[-1].open_time_ms, 100.0, 100.6, 99.8, 100.3, 1000)
        with (
            patch("mu_strategy.entry.scanner.build_hourly_context", return_value={bar.open_time_ms: "green" for bar in candles}),
            patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)),
            patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))),
            patch("mu_strategy.entry.scanner.nearest_fib_retest_level", side_effect=lambda _c, idx, _cfg: 100.0 if idx == len(candles) - 1 else None),
        ):
            needs_next = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

        self.assertEqual("wait", needs_next.action)
        self.assertIs(EntryDecisionCode.NEXT_CANDLE_REQUIRED, needs_next.decision_code)
        self.assertAlmostEqual(100.6, needs_next.trigger_price)

    def test_scan_entry_waits_when_next_execution_candle_is_outside_trading_window(self):
        from mu_strategy.entry.scanner import scan_entry

        # 2026-06-18 (EDT = UTC-4): morning window 09:45-11:30 ET = 13:45-15:30 UTC; afternoon 14:30-15:45 ET = 18:30-19:45 UTC.
        # Signal forms at 15:30 UTC (in window), next_candle is at 15:45 UTC (outside window!), latest bar is 18:30 UTC (in window).
        signal_ms = _utc_ms(2026, 6, 18, 15, 30)
        outside_next_ms = _utc_ms(2026, 6, 18, 15, 45)
        latest_in_window_ms = _utc_ms(2026, 6, 18, 18, 30)
        candles = [
            Candle(signal_ms - 1_800_000, 99.0, 100.0, 98.5, 99.5, 1000),
            Candle(signal_ms - 900_000, 99.5, 101.0, 99.2, 100.5, 1000),
            Candle(signal_ms, 100.0, 101.0, 99.8, 100.5, 1000),
            Candle(outside_next_ms, 100.5, 101.5, 100.3, 100.8, 1000),
            Candle(latest_in_window_ms, 100.6, 101.0, 100.4, 100.7, 1000),
        ]
        for execution in ("break_high", "direct_next_open"):
            with self.subTest(execution=execution):
                config = replace(
                    baseline_strategy_group("BTC-USDT-SWAP").config,
                    entry_execution=execution,
                )
                with (
                    patch("mu_strategy.entry.scanner.build_hourly_context", return_value={bar.open_time_ms: "green" for bar in candles}),
                    patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)),
                    patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))),
                    patch("mu_strategy.entry.scanner.nearest_fib_retest_level", side_effect=lambda _c, idx, _cfg: 100.0 if idx == 2 else None),
                ):
                    result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

                self.assertEqual("wait", result.action)
                self.assertIs(EntryDecisionCode.NEXT_FILL_OUTSIDE_TRADING_WINDOW, result.decision_code)
                self.assertIs(EntryDisposition.WAIT, result.disposition)
                self.assertIs(EntryDecisionStage.EXECUTION, result.stage)

    def test_scan_entry_direct_next_open_and_break_high_block_prospective_bar_outside_trading_window(self):
        from mu_strategy.entry.scanner import scan_entry

        # 2026-06-18 (EDT = UTC-4): 15:45 ET = 19:45 UTC (in window); next synthetic bar 16:00 ET = 20:00 UTC (outside).
        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 19, 45))
        candles[-1] = Candle(candles[-1].open_time_ms, 100.0, 101.0, 99.8, 100.5, 1000)
        for execution in ("direct_next_open", "break_high"):
            with self.subTest(execution=execution):
                config = replace(
                    baseline_strategy_group("BTC-USDT-SWAP").config,
                    entry_execution=execution,
                )
                with (
                    patch("mu_strategy.entry.scanner.build_hourly_context", return_value={bar.open_time_ms: "green" for bar in candles}),
                    patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(candles)),
                    patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(candles), [0.0] * len(candles), [0.2] * len(candles))),
                    patch("mu_strategy.entry.scanner.nearest_fib_retest_level", side_effect=lambda _c, idx, _cfg: 100.0 if idx == len(candles) - 1 else None),
                ):
                    result = scan_entry("BTC-USDT-SWAP", candles, candles, config=config, lookback_bars=4)

                self.assertEqual("wait", result.action)
                self.assertIs(EntryDecisionCode.NEXT_FILL_OUTSIDE_TRADING_WINDOW, result.decision_code)
                self.assertIs(EntryDisposition.WAIT, result.disposition)
                self.assertIs(EntryDecisionStage.EXECUTION, result.stage)

    def test_scan_entry_second_pullback_does_not_revive_signal_suppressed_by_earlier_order(self):
        from mu_strategy.entry.scanner import scan_entry

        # A confirms at bar i (100), B confirms at i+2 (105) while A is active (wait_bars=8 -> expires at i+8),
        # and C confirms at i+9 (110) after A expires unfilled.
        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 15, 0))
        base = len(candles) - 10  # i = 30, i+2 = 32, i+8 = 38, i+9 = 39
        idx_a, idx_b, idx_c = base, base + 2, base + 9
        for idx in range(base, len(candles)):
            candles[idx] = Candle(candles[idx].open_time_ms, 106.0, 107.0, 105.5, 106.5, 1000)
        candles[idx_a] = Candle(candles[idx_a].open_time_ms, 100.0, 101.0, 99.8, 100.5, 1000)
        candles[idx_b] = Candle(candles[idx_b].open_time_ms, 105.0, 106.0, 104.8, 105.2, 1000)
        candles[idx_c] = Candle(candles[idx_c].open_time_ms, 110.0, 111.0, 109.8, 110.5, 1000)

        config = replace(
            baseline_strategy_group("BTC-USDT-SWAP").config,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
            trading_windows_et=(("00:00", "23:59"),),
        )

        def run_scan(slice_end: int, *, include_c: bool):
            sub = candles[:slice_end]
            fib_map = {idx_a: 100.0, idx_b: 105.0}
            if include_c:
                fib_map[idx_c] = 110.0
            with (
                patch("mu_strategy.entry.scanner.build_hourly_context", return_value={bar.open_time_ms: "green" for bar in sub}),
                patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(sub)),
                patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(sub), [0.0] * len(sub), [0.2] * len(sub))),
                patch("mu_strategy.entry.scanner.nearest_fib_retest_level", side_effect=lambda _c, idx, _cfg: fib_map.get(idx)),
            ):
                return scan_entry("BTC-USDT-SWAP", sub, sub, config=config, lookback_bars=8)

        # 1. Before expiry (ending at i+7): A remains active at 100.
        before_expiry = run_scan(base + 8, include_c=False)
        self.assertEqual("enter", before_expiry.action)
        self.assertEqual(candles[idx_a].open_time_ms, before_expiry.signal_time_ms)
        self.assertAlmostEqual(100.0, before_expiry.trigger_price)

        # 2. At expiry (ending at i+8, no C yet): A expired and suppressed B must NOT be revived.
        at_expiry = run_scan(base + 9, include_c=False)
        self.assertEqual("wait", at_expiry.action)
        self.assertIs(EntryDecisionCode.NO_RECENT_CONFIRMED_FIB_RETEST, at_expiry.decision_code)

        # 3. After expiry (ending at i+9 with C at 110): C becomes active, NOT suppressed B at 105.
        after_expiry = run_scan(base + 10, include_c=True)
        self.assertEqual("enter", after_expiry.action)
        self.assertEqual(candles[idx_c].open_time_ms, after_expiry.signal_time_ms)
        self.assertAlmostEqual(110.0, after_expiry.trigger_price)

    def test_scan_entry_excludes_pre_coverage_history_from_order_replay_without_discarding_indicators(self):
        from mu_strategy.core.trading_calendar import CalendarUnavailable, US_EQUITIES_CALENDAR
        from mu_strategy.entry.scanner import scan_entry
        from mu_strategy.indicators import macd, rsi
        from mu_strategy.models import closes

        # Build continuous 15m candles starting Dec 31, 2024 09:00 ET (14:00 UTC) — before
        # us_equities_2025_2028_v1 lower coverage — through Jan 10, 2025 10:00 ET (15:00 UTC).
        # Also test right after the lower boundary (Jan 2, 2025 10:15 ET) so RSI/MACD depend on Dec 2024 bars.
        config = replace(
            baseline_strategy_group("MU-USDT-SWAP").config,
            trading_calendar_id=US_EQUITIES_CALENDAR,
            trading_calendar_sha256=None,
            entry_execution="second_pullback",
            second_pullback_wait_bars=8,
            trading_windows_et=(("09:45", "11:30"),),
        )

        # Part 1: Tight boundary crossing (Dec 31, 2024 -> Jan 2, 2025 10:15 ET) with REAL indicators (unmocked).
        # 2025-01-01 is a holiday; 2025-01-02 09:45 ET (14:45 UTC) is the first open window in coverage.
        dec_bars = [
            Candle(_utc_ms(2024, 12, 31, 14, 0) + i * 900_000, 100.0 + i * 0.4, 101.0 + i * 0.4, 99.5 + i * 0.4, 100.3 + i * 0.4, 1000)
            for i in range(30)
        ]
        jan2_bars = [
            Candle(_utc_ms(2025, 1, 2, 14, 30), 112.0, 113.0, 111.5, 112.5, 1000),  # 09:30 ET (outside 09:45 window)
            Candle(_utc_ms(2025, 1, 2, 14, 45), 112.5, 114.0, 111.9, 113.2, 1000),  # 09:45 ET (in window, signal)
            Candle(_utc_ms(2025, 1, 2, 15, 0), 113.2, 114.5, 113.0, 114.0, 1000),   # 10:00 ET (in window)
            Candle(_utc_ms(2025, 1, 2, 15, 15), 114.0, 115.0, 113.8, 114.6, 1000),  # 10:15 ET (in window, current)
        ]
        boundary_candles = dec_bars + jan2_bars
        expected_rsi = rsi(closes(boundary_candles), 14)[-1]
        expected_hist = macd(closes(boundary_candles))[2][-1]
        jan_only_rsi = rsi(closes(jan2_bars), 14)[-1]
        self.assertNotAlmostEqual(expected_rsi, jan_only_rsi)

        dec_idx = len(dec_bars) - 2
        jan2_sig_idx = len(dec_bars) + 1
        with (
            patch("mu_strategy.entry.scanner.build_hourly_context", return_value={b.open_time_ms: "green" for b in boundary_candles}),
            patch(
                "mu_strategy.entry.scanner.nearest_fib_retest_level",
                side_effect=lambda _c, idx, _cfg: 110.0 if idx == dec_idx else (112.0 if idx == jan2_sig_idx else None),
            ),
        ):
            boundary_res = scan_entry("MU-USDT-SWAP", boundary_candles, boundary_candles, config=config, lookback_bars=8)

        # Dec 2024 bar is excluded from order replay (never raises CalendarUnavailable, never suppresses Jan 2 signal),
        # while full Dec 2024 + Jan 2025 indicator history is preserved.
        self.assertEqual("enter", boundary_res.action)
        self.assertEqual(boundary_candles[jan2_sig_idx].open_time_ms, boundary_res.signal_time_ms)
        self.assertAlmostEqual(112.0, boundary_res.trigger_price)
        self.assertAlmostEqual(expected_rsi, boundary_res.rsi14)
        self.assertAlmostEqual(expected_hist, boundary_res.macd_hist)

        # Non-second_pullback lookback reaching across the lower coverage boundary also skips unsupported dates.
        direct_cfg = replace(config, entry_execution="direct_next_open")
        with (
            patch("mu_strategy.entry.scanner.build_hourly_context", return_value={b.open_time_ms: "green" for b in boundary_candles}),
            patch("mu_strategy.entry.scanner.nearest_fib_retest_level", side_effect=lambda _c, idx, _cfg: 110.0 if idx == dec_idx else None),
        ):
            direct_res = scan_entry("MU-USDT-SWAP", boundary_candles, boundary_candles, config=direct_cfg, lookback_bars=16)
        self.assertEqual("wait", direct_res.action)
        self.assertIs(EntryDecisionCode.NO_RECENT_CONFIRMED_FIB_RETEST, direct_res.decision_code)

        # Part 2: History starting in Dec 2024 ending Jan 10, 2025 10:00 ET (15:00 UTC), verifying chronological
        # suppression within the supported 2025 range (A at Jan 8 suppresses B, then C on Jan 10 becomes active).
        jan_seq_start = _utc_ms(2025, 1, 10, 14, 45)  # 09:45 ET on Jan 10, 2025
        # Construct 12 supported 15m bars across Jan 8 & Jan 10 (Jan 9, 2025 is Carter National Day of Mourning holiday!)
        jan8_bars = [
            Candle(_utc_ms(2025, 1, 8, 14, 45) + i * 900_000, 106.0, 107.0, 105.5, 106.5, 1000)
            for i in range(9)
        ]
        jan10_bars = [
            Candle(jan_seq_start, 110.0, 111.0, 109.8, 110.5, 1000),               # 09:45 ET (C)
            Candle(jan_seq_start + 900_000, 110.6, 111.5, 110.4, 111.0, 1000),     # 10:00 ET (current bar)
        ]
        jan8_bars[0] = Candle(jan8_bars[0].open_time_ms, 100.0, 101.0, 99.8, 100.5, 1000)   # A
        jan8_bars[2] = Candle(jan8_bars[2].open_time_ms, 105.0, 106.0, 104.8, 105.2, 1000)  # B (suppressed by A)
        full_candles = dec_bars + jan8_bars + jan10_bars
        idx_a = len(dec_bars)
        idx_b = len(dec_bars) + 2
        idx_c = len(dec_bars) + len(jan8_bars)
        fib_map = {dec_idx: 99.0, idx_a: 100.0, idx_b: 105.0, idx_c: 110.0}

        with (
            patch("mu_strategy.entry.scanner.build_hourly_context", return_value={b.open_time_ms: "green" for b in full_candles}),
            patch("mu_strategy.entry.scanner.rsi", return_value=[55.0] * len(full_candles)),
            patch("mu_strategy.entry.scanner.macd", return_value=([0.0] * len(full_candles), [0.0] * len(full_candles), [0.2] * len(full_candles))),
            patch("mu_strategy.entry.scanner.nearest_fib_retest_level", side_effect=lambda _c, idx, _cfg: fib_map.get(idx)),
        ):
            jan10_res = scan_entry("MU-USDT-SWAP", full_candles, full_candles, config=config, lookback_bars=8)
            # Without C, suppressed B must still NOT be revived once A expires.
            fib_map.pop(idx_c)
            jan10_no_c = scan_entry("MU-USDT-SWAP", full_candles, full_candles, config=config, lookback_bars=8)

        self.assertEqual("enter", jan10_res.action)
        self.assertEqual(full_candles[idx_c].open_time_ms, jan10_res.signal_time_ms)
        self.assertAlmostEqual(110.0, jan10_res.trigger_price)
        self.assertEqual("wait", jan10_no_c.action)
        self.assertIs(EntryDecisionCode.NO_RECENT_CONFIRMED_FIB_RETEST, jan10_no_c.decision_code)

        # Part 3: Current candle outside calendar coverage or non-coverage calendar failures still fail closed.
        with self.assertRaises(CalendarUnavailable):
            scan_entry("MU-USDT-SWAP", dec_bars, dec_bars, config=config, lookback_bars=8)
        with patch("mu_strategy.core.trading_calendar.ZoneInfo", side_effect=RuntimeError("tz missing")):
            with self.assertRaises(CalendarUnavailable):
                scan_entry("MU-USDT-SWAP", boundary_candles, boundary_candles, config=config, lookback_bars=8)

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

        candles = _candles_ending_at(_utc_ms(2026, 6, 18, 14, 0))
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
