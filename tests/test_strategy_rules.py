import unittest
from datetime import datetime, timezone

from mu_strategy.models import Candle
from mu_strategy.strategy import (
    StrategyGroup,
    StrategyConfig,
    default_strategy_groups,
    fibonacci_levels,
    is_preferred_us_cash_window,
    one_hour_regime,
    optimized_strategy_group,
    selected_strategy_groups,
    should_enter_long,
    should_execute_entry,
)


class StrategyRuleTests(unittest.TestCase):
    def test_fibonacci_levels_from_swing(self):
        levels = fibonacci_levels(100, 120)

        self.assertAlmostEqual(levels["0.382"], 112.36)
        self.assertAlmostEqual(levels["0.5"], 110.0)
        self.assertAlmostEqual(levels["0.618"], 107.64)

    def test_one_hour_red_blocks_longs(self):
        state = one_hour_regime(close=100, ema21=105, rsi14=42, macd_hist=-0.5, macd_hist_prev=-0.2)

        self.assertEqual(state, "red")

    def test_one_hour_green_allows_full_strategy(self):
        state = one_hour_regime(close=110, ema21=100, rsi14=58, macd_hist=0.6, macd_hist_prev=0.3)

        self.assertEqual(state, "green")

    def test_15m_fibonacci_retest_requires_confirmation_and_filters(self):
        config = StrategyConfig()
        candle = Candle(
            open_time_ms=1,
            open=101,
            high=103,
            low=98.8,
            close=100.8,
            volume=1000,
        )

        signal = should_enter_long(
            candle=candle,
            fib_level=100,
            regime="green",
            rsi14=51,
            macd_hist=0.2,
            macd_hist_prev=0.1,
            config=config,
        )

        self.assertTrue(signal.allowed)
        self.assertAlmostEqual(signal.stop_price, 98.8)

    def test_15m_entry_rejects_red_regime(self):
        config = StrategyConfig()
        candle = Candle(open_time_ms=1, open=101, high=103, low=99.2, close=100.5, volume=1000)

        signal = should_enter_long(candle, 100, "red", 55, 0.4, 0.2, config)

        self.assertFalse(signal.allowed)

    def test_preferred_us_cash_window_filters_non_session_bars(self):
        config = StrategyConfig()
        in_window = int(datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        out_of_window = int(datetime(2026, 6, 11, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)

        self.assertTrue(is_preferred_us_cash_window(in_window, config))
        self.assertFalse(is_preferred_us_cash_window(out_of_window, config))

    def test_strategy_groups_keep_baseline_and_optimized_variant(self):
        groups = default_strategy_groups()

        self.assertEqual(
            [
                "legacy_break_high",
                "baseline",
                "direct_next_open",
                "baseline_half_protect",
                "baseline_green_wide",
                "baseline_half_green_wide",
                "optimized_v2",
            ],
            [group.name for group in groups],
        )
        self.assertIsInstance(groups[0], StrategyGroup)
        self.assertEqual("break_high", groups[0].config.entry_execution)
        self.assertEqual("second_pullback", groups[1].config.entry_execution)
        self.assertEqual("direct_next_open", groups[2].config.entry_execution)
        self.assertEqual("second_pullback", groups[3].config.entry_execution)
        self.assertEqual("half_protect", groups[3].config.stop_tightening)
        self.assertEqual("green_wide", groups[4].config.stop_tightening)
        self.assertEqual("half_protect_green_wide", groups[5].config.stop_tightening)
        self.assertTrue(groups[6].config.block_reverse_fib_resistance)

    def test_selected_strategy_groups_loads_requested_names(self):
        groups = selected_strategy_groups("MUUSDT", ["legacy_break_high,baseline", "second_pullback_limit_8"])

        self.assertEqual(["legacy_break_high", "baseline", "baseline"], [group.name for group in groups])
        self.assertEqual("second_pullback", groups[1].config.entry_execution)

    def test_optimized_execution_rejects_chasing_too_far_above_fib(self):
        config = optimized_strategy_group().config
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100.4, 101.2, 100.0, 101.0, 1000),
            Candle(1_800_000, 101.2, 101.6, 100.8, 101.1, 1000),
        ]

        decision = should_execute_entry(candles, 1, candles[2], 100, "green", config)

        self.assertFalse(decision.allowed)
        self.assertIn("too far above Fibonacci", decision.reason)

    def test_optimized_execution_rejects_wide_signal_candle(self):
        config = optimized_strategy_group().config
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100, 101, 98.8, 100.2, 1000),
            Candle(1_800_000, 100.2, 101.2, 99.8, 100.8, 1000),
        ]

        decision = should_execute_entry(candles, 1, candles[2], 100, "green", config)

        self.assertFalse(decision.allowed)
        self.assertIn("signal candle too wide", decision.reason)

    def test_direct_next_open_execution_does_not_require_breaking_signal_high(self):
        config = StrategyConfig(entry_execution="direct_next_open")
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100.4, 101.2, 100.0, 101.0, 1000),
            Candle(1_800_000, 100.9, 101.0, 100.6, 100.8, 1000),
        ]

        decision = should_execute_entry(candles, 1, candles[2], 100, "green", config)

        self.assertTrue(decision.allowed)
        self.assertAlmostEqual(100.9, decision.entry_price)

    def test_execution_rejects_reverse_fibonacci_resistance(self):
        config = StrategyConfig(block_reverse_fib_resistance=True)
        candles = [
            Candle(0, 118, 120, 117, 119, 1000),
            Candle(900_000, 112, 113, 108, 109, 1000),
            Candle(1_800_000, 103, 104, 100, 101, 1000),
            Candle(2_700_000, 105, 110, 104, 108, 1000),
            Candle(3_600_000, 109, 111, 108, 110, 1000),
        ]

        decision = should_execute_entry(candles, 3, candles[4], 106, "green", config)

        self.assertFalse(decision.allowed)
        self.assertIn("reverse Fibonacci resistance", decision.reason)


if __name__ == "__main__":
    unittest.main()
