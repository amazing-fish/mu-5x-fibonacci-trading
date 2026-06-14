import unittest
from datetime import datetime, timezone

from mu_strategy.execution.decision import execution_decision
from mu_strategy.execution.plan import planned_margin_steps
from mu_strategy.models import Candle
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.strategy import StrategyConfig, optimized_strategy_group


class ExecutionPlanningTests(unittest.TestCase):
    def test_direct_next_open_strategy_can_allow_entry_with_initial_stop(self):
        config = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="direct_next_open")
        candles = _candidate_signal_candles()

        decision = execution_decision(
            candles,
            config=config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
            current_price=104,
        )

        self.assertEqual("allow", decision.action)
        self.assertEqual("confirmed Fibonacci retest", decision.reason)
        self.assertAlmostEqual(101.92, decision.initial_stop)
        self.assertEqual((0.20, 0.20, 0.20, 0.40), decision.margin_steps)

    def test_execution_decision_bases_stop_on_validated_entry_price(self):
        config = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="direct_next_open")
        candles = _candidate_signal_candles()

        decision = execution_decision(
            candles,
            config=config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
            current_price=110,
        )

        self.assertEqual("allow", decision.action)
        self.assertAlmostEqual(101.92, decision.initial_stop)

    def test_fixed_baseline_waits_for_second_pullback_before_planning_entry(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        candles = _candidate_signal_candles()

        decision = execution_decision(
            candles,
            config=config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
            current_price=104,
        )

        self.assertEqual("wait", decision.action)
        self.assertEqual("waiting for second pullback", decision.reason)
        self.assertIsNone(decision.initial_stop)
        self.assertEqual((), decision.margin_steps)

    def test_execution_decision_waits_outside_us_cash_session(self):
        config = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="direct_next_open")
        candles = _candidate_signal_candles(start_utc=datetime(2026, 6, 11, 3, 15, tzinfo=timezone.utc))

        decision = execution_decision(
            candles,
            config=config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
            current_price=104,
        )

        self.assertEqual("wait", decision.action)
        self.assertEqual("outside preferred US cash session", decision.reason)
        self.assertIsNone(decision.initial_stop)
        self.assertEqual((), decision.margin_steps)

    def test_execution_decision_waits_when_next_fill_bar_is_outside_us_cash_session(self):
        config = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="direct_next_open")
        start_ms = _utc_ms(datetime(2026, 6, 11, 19, 0, tzinfo=timezone.utc))
        candles = [
            Candle(start_ms, 100, 105, 99, 104, 1000),
            Candle(start_ms + 900_000, 104, 110, 103, 109, 1000),
            Candle(start_ms + 1_800_000, 109, 112, 108, 111, 1000),
            Candle(start_ms + 2_700_000, 111, 112, 102.5, 106, 1000),
            Candle(start_ms + 3_600_000, 106, 114, 103, 107, 1000),
        ]

        decision = execution_decision(
            candles,
            config=config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
            current_price=106,
        )

        self.assertEqual("wait", decision.action)
        self.assertEqual("next fill bar outside preferred US cash session", decision.reason)
        self.assertIsNone(decision.initial_stop)
        self.assertEqual((), decision.margin_steps)

    def test_execution_decision_blocks_optimized_execution_filters(self):
        config = optimized_strategy_group("MU-USDT-SWAP").config
        candles = _candidate_signal_candles()

        decision = execution_decision(
            candles,
            config=config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
            current_price=104,
        )

        self.assertEqual("block", decision.action)
        self.assertEqual("signal candle too wide", decision.reason)
        self.assertIsNone(decision.initial_stop)
        self.assertEqual((), decision.margin_steps)

    def test_fixed_strategy_waits_when_fibonacci_retest_has_not_confirmed(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        start_ms = _utc_ms(datetime(2026, 6, 11, 13, 15, tzinfo=timezone.utc))
        candles = [
            Candle(start_ms, 100, 105, 99, 104, 1000),
            Candle(start_ms + 900_000, 104, 110, 103, 109, 1000),
            Candle(start_ms + 1_800_000, 109, 112, 108, 111, 1000),
            Candle(start_ms + 2_700_000, 111, 113, 110, 112, 1000),
        ]

        decision = execution_decision(
            candles,
            config=config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
            current_price=112,
        )

        self.assertEqual("wait", decision.action)
        self.assertIn("Fibonacci", decision.reason)

    def test_fixed_strategy_blocks_red_regime(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        candles = _candidate_signal_candles()

        decision = execution_decision(
            candles,
            config=config,
            regime="red",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
            current_price=104,
        )

        self.assertEqual("block", decision.action)
        self.assertIn("regime", decision.reason)

    def test_planned_margin_steps_are_copied_from_config(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config

        self.assertEqual((0.20, 0.20, 0.20, 0.40), planned_margin_steps(config))


def _candidate_signal_candles(start_utc: datetime | None = None) -> list[Candle]:
    start_utc = start_utc or datetime(2026, 6, 11, 13, 15, tzinfo=timezone.utc)
    start_ms = _utc_ms(start_utc)
    return [
        Candle(start_ms, 100, 105, 99, 104, 1000),
        Candle(start_ms + 900_000, 104, 110, 103, 109, 1000),
        Candle(start_ms + 1_800_000, 109, 112, 108, 111, 1000),
        Candle(start_ms + 2_700_000, 111, 112, 102.5, 104, 1000),
        Candle(start_ms + 3_600_000, 104, 114, 103, 107, 1000),
    ]


def _utc_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


if __name__ == "__main__":
    unittest.main()
