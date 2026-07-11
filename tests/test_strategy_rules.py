import unittest
from datetime import datetime, timezone

from mu_strategy.models import Candle, EntryDecisionCode, EntryDecisionStage, EntryDisposition, EntrySignal
from mu_strategy.strategy import (
    EntryExecution,
    StrategyConfig,
    fee_profile_label,
    fibonacci_levels,
    is_preferred_us_cash_window,
    one_hour_regime,
    optimized_strategy_group,
    should_enter_long,
    should_execute_entry,
    with_fee_profile,
)
from mu_strategy.strategies.components import StrategyComponents
from mu_strategy.strategies.presets.fibonacci import preferred_fibonacci_parameter, preferred_fib_lookback
from mu_strategy.strategies.registry import StrategyGroup, default_strategy_groups, selected_strategy_groups


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
        self.assertEqual("confirmed Fibonacci retest", signal.reason)
        self.assertAlmostEqual(signal.stop_price, 98.8)
        self.assertIs(EntryDecisionCode.SIGNAL_CONFIRMED, signal.decision_code)
        self.assertIs(EntryDisposition.READY, signal.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, signal.stage)

    def test_15m_entry_rejects_red_regime(self):
        config = StrategyConfig()
        candle = Candle(open_time_ms=1, open=101, high=103, low=99.2, close=100.5, volume=1000)

        signal = should_enter_long(candle, 100, "red", 55, 0.4, 0.2, config)

        self.assertFalse(signal.allowed)
        self.assertEqual("1h regime blocks long", signal.reason)
        self.assertIs(EntryDecisionCode.REGIME_BLOCKED, signal.decision_code)
        self.assertIs(EntryDisposition.BLOCK, signal.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, signal.stage)

    def test_entry_signal_rejections_have_stable_typed_codes(self):
        config = StrategyConfig()
        confirmed_retest = Candle(open_time_ms=1, open=101, high=103, low=99.2, close=100.5, volume=1000)
        no_retest = Candle(open_time_ms=1, open=103, high=104, low=102, close=103, volume=1000)
        cases = [
            (
                "rsi",
                should_enter_long(confirmed_retest, 100, "green", 44, 0.4, 0.2, config),
                "15m RSI below floor",
                EntryDecisionCode.RSI_BELOW_FLOOR,
                EntryDisposition.BLOCK,
            ),
            (
                "macd",
                should_enter_long(confirmed_retest, 100, "green", 55, -0.4, -0.2, config),
                "15m MACD histogram still weakening",
                EntryDecisionCode.MACD_WEAKENING,
                EntryDisposition.BLOCK,
            ),
            (
                "no_retest",
                should_enter_long(no_retest, 100, "green", 55, 0.4, 0.2, config),
                "no confirmed Fibonacci retest",
                EntryDecisionCode.NO_CONFIRMED_FIB_RETEST,
                EntryDisposition.WAIT,
            ),
        ]

        for name, signal, reason, code, disposition in cases:
            with self.subTest(name=name):
                self.assertFalse(signal.allowed)
                self.assertEqual(reason, signal.reason)
                self.assertIsNone(signal.stop_price)
                self.assertIs(code, signal.decision_code)
                self.assertIs(disposition, signal.disposition)
                self.assertIs(EntryDecisionStage.SIGNAL, signal.stage)

    def test_legacy_primitive_positional_constructors_remain_compatible(self):
        signal = EntrySignal(False, "legacy signal", 99.0)
        execution = EntryExecution(False, "legacy execution", 101.0)

        self.assertEqual((False, "legacy signal", 99.0), (signal.allowed, signal.reason, signal.stop_price))
        self.assertIs(EntryDecisionCode.UNKNOWN, signal.decision_code)
        self.assertIs(EntryDisposition.UNKNOWN, signal.disposition)
        self.assertIs(EntryDecisionStage.UNKNOWN, signal.stage)
        self.assertEqual(
            (False, "legacy execution", 101.0),
            (execution.allowed, execution.reason, execution.entry_price),
        )
        self.assertIs(EntryDecisionCode.UNKNOWN, execution.decision_code)
        self.assertIs(EntryDisposition.UNKNOWN, execution.disposition)
        self.assertIs(EntryDecisionStage.UNKNOWN, execution.stage)

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
                "baseline_yellow_wide",
                "baseline_yellow_green_wide",
                "baseline_half_green_wide",
                "baseline_delayed_tighten_fast_start",
                "optimized_v2",
            ],
            [group.name for group in groups],
        )
        self.assertIsInstance(groups[0], StrategyGroup)
        self.assertIsInstance(groups[1].components, StrategyComponents)
        self.assertEqual("break_high", groups[0].config.entry_execution)
        self.assertEqual("second_pullback", groups[1].config.entry_execution)
        self.assertEqual(8, groups[1].config.fib_lookback)
        self.assertEqual("market", groups[1].config.fee_profile)
        self.assertAlmostEqual(0.0005, groups[1].config.fee_rate)
        self.assertEqual("二次回踩限价", groups[1].components.entry)
        self.assertEqual("direct_next_open", groups[2].config.entry_execution)
        self.assertEqual("second_pullback", groups[3].config.entry_execution)
        self.assertEqual("half_protect", groups[3].config.stop_tightening)
        self.assertEqual("wide", groups[4].config.green_stop_tightening)
        self.assertEqual("baseline", groups[4].config.yellow_stop_tightening)
        self.assertEqual("wide", groups[5].config.yellow_stop_tightening)
        self.assertEqual("baseline", groups[5].config.green_stop_tightening)
        self.assertEqual("wide", groups[6].config.yellow_stop_tightening)
        self.assertEqual("wide", groups[6].config.green_stop_tightening)
        self.assertEqual("half_protect_green_wide", groups[7].config.stop_tightening)
        self.assertEqual("delayed_baseline", groups[8].config.stop_tightening)
        self.assertEqual(8, groups[8].config.stop_transition_bars)
        self.assertEqual("fast_start", groups[8].config.stop_transition_curve)
        self.assertTrue(groups[9].config.block_reverse_fib_resistance)

    def test_selected_strategy_groups_loads_requested_names(self):
        groups = selected_strategy_groups(
            "MUUSDT",
            ["legacy_break_high,baseline", "second_pullback_limit_8,baseline_delayed_tighten_fast_start"],
        )

        self.assertEqual(
            ["legacy_break_high", "baseline", "baseline", "baseline_delayed_tighten_fast_start"],
            [group.name for group in groups],
        )
        self.assertEqual("second_pullback", groups[1].config.entry_execution)
        self.assertEqual(8, groups[1].config.fib_lookback)
        self.assertEqual("fast_start", groups[3].config.stop_transition_curve)

    def test_known_assets_record_preferred_fibonacci_lookbacks(self):
        self.assertEqual(8, preferred_fib_lookback("MU-USDT-SWAP"))
        self.assertEqual(8, preferred_fib_lookback("SPCX-USDT-SWAP"))
        self.assertEqual(36, preferred_fib_lookback("META-USDT-SWAP"))
        self.assertEqual(12, preferred_fib_lookback("BTC-USDT-SWAP"))
        self.assertEqual(32, preferred_fib_lookback("UNKNOWN-USDT-SWAP"))

        meta = preferred_fibonacci_parameter("META-USDT-SWAP")

        self.assertIsNotNone(meta)
        self.assertEqual(9, meta.horizon_hours)
        self.assertTrue(meta.evidence_report.startswith("reports/live/"))
        self.assertIn("fibonacci_pullback_multi_asset", meta.evidence_report)

    def test_baseline_uses_preferred_fibonacci_lookback_for_known_assets(self):
        groups = selected_strategy_groups("BTC-USDT-SWAP", ["baseline"])

        self.assertEqual(12, groups[0].config.fib_lookback)

    def test_fee_profile_switches_between_market_and_limit_costs(self):
        market_config = with_fee_profile(StrategyConfig(fee_rate=0), "market")
        limit_config = with_fee_profile(market_config, "limit")

        self.assertEqual("market", market_config.fee_profile)
        self.assertEqual("market/taker (市价/吃单)", fee_profile_label(market_config))
        self.assertAlmostEqual(0.0005, market_config.fee_rate)
        self.assertEqual("limit", limit_config.fee_profile)
        self.assertEqual("limit/maker (限价挂单成本假设)", fee_profile_label(limit_config))
        self.assertAlmostEqual(0.0002, limit_config.fee_rate)

    def test_optimized_execution_rejects_chasing_too_far_above_fib(self):
        config = optimized_strategy_group().config
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100.4, 101.2, 100.0, 101.0, 1000),
            Candle(1_800_000, 101.2, 101.6, 100.8, 101.1, 1000),
        ]

        decision = should_execute_entry(candles, 1, candles[2], 100, "green", config)

        self.assertFalse(decision.allowed)
        self.assertEqual("entry too far above Fibonacci retest", decision.reason)
        self.assertIs(EntryDecisionCode.ENTRY_TOO_FAR_ABOVE_FIB, decision.decision_code)
        self.assertIs(EntryDisposition.BLOCK, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

    def test_optimized_execution_rejects_wide_signal_candle(self):
        config = optimized_strategy_group().config
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100, 101, 98.8, 100.2, 1000),
            Candle(1_800_000, 100.2, 101.2, 99.8, 100.8, 1000),
        ]

        decision = should_execute_entry(candles, 1, candles[2], 100, "green", config)

        self.assertFalse(decision.allowed)
        self.assertEqual("signal candle too wide", decision.reason)
        self.assertIs(EntryDecisionCode.SIGNAL_CANDLE_TOO_WIDE, decision.decision_code)
        self.assertIs(EntryDisposition.BLOCK, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

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
        self.assertEqual("execution accepted", decision.reason)
        self.assertIs(EntryDecisionCode.EXECUTION_ACCEPTED, decision.decision_code)
        self.assertIs(EntryDisposition.READY, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

    def test_execution_waits_have_stable_typed_codes(self):
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100, 102, 99.8, 100.5, 1000),
            Candle(1_800_000, 100.4, 101.9, 100.2, 101.0, 1000),
        ]
        cases = [
            (
                "break_high",
                should_execute_entry(candles, 1, candles[2], 100, "green", StrategyConfig()),
                "next candle does not break signal high",
                EntryDecisionCode.NEXT_CANDLE_DID_NOT_BREAK_SIGNAL_HIGH,
                EntryDecisionStage.EXECUTION,
            ),
            (
                "second_pullback",
                should_execute_entry(
                    candles,
                    1,
                    candles[2],
                    100,
                    "green",
                    StrategyConfig(entry_execution="second_pullback"),
                ),
                "waiting for second pullback",
                EntryDecisionCode.WAITING_SECOND_PULLBACK,
                EntryDecisionStage.PENDING_ENTRY,
            ),
        ]

        for name, decision, reason, code, stage in cases:
            with self.subTest(name=name):
                self.assertFalse(decision.allowed)
                self.assertEqual(reason, decision.reason)
                self.assertIsNone(decision.entry_price)
                self.assertIs(code, decision.decision_code)
                self.assertIs(EntryDisposition.WAIT, decision.disposition)
                self.assertIs(stage, decision.stage)

    def test_execution_rejects_entry_too_far_above_signal_close_with_stable_code(self):
        config = StrategyConfig(
            entry_execution="direct_next_open",
            max_entry_above_signal_close_pct=0.01,
        )
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100, 101, 99.8, 100, 1000),
            Candle(1_800_000, 105, 106, 104, 105, 1000),
        ]

        decision = should_execute_entry(candles, 1, candles[2], 100, "green", config)

        self.assertFalse(decision.allowed)
        self.assertEqual("entry too far above signal close", decision.reason)
        self.assertIs(EntryDecisionCode.ENTRY_TOO_FAR_ABOVE_SIGNAL_CLOSE, decision.decision_code)
        self.assertIs(EntryDisposition.BLOCK, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

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
        self.assertIs(EntryDecisionCode.REVERSE_FIB_RESISTANCE, decision.decision_code)
        self.assertIs(EntryDisposition.BLOCK, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)


if __name__ == "__main__":
    unittest.main()
