import unittest

from mu_strategy.execution.decision import execution_decision
from mu_strategy.execution.plan import planned_margin_steps
from mu_strategy.models import Candle
from mu_strategy.strategies.registry import baseline_strategy_group


class ExecutionPlanningTests(unittest.TestCase):
    def test_fixed_strategy_can_allow_entry_with_initial_stop(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        candles = [
            Candle(0, 100, 105, 99, 104, 1000),
            Candle(900_000, 104, 110, 103, 109, 1000),
            Candle(1_800_000, 109, 112, 108, 111, 1000),
            Candle(2_700_000, 111, 112, 102.5, 104, 1000),
            Candle(3_600_000, 104, 108, 103, 107, 1000),
        ]

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

    def test_fixed_strategy_waits_when_fibonacci_retest_has_not_confirmed(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        candles = [
            Candle(0, 100, 105, 99, 104, 1000),
            Candle(900_000, 104, 110, 103, 109, 1000),
            Candle(1_800_000, 109, 112, 108, 111, 1000),
            Candle(2_700_000, 111, 113, 110, 112, 1000),
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
        candles = [
            Candle(0, 100, 105, 99, 104, 1000),
            Candle(900_000, 104, 110, 103, 109, 1000),
            Candle(1_800_000, 109, 112, 108, 111, 1000),
            Candle(2_700_000, 111, 112, 102.5, 104, 1000),
        ]

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


if __name__ == "__main__":
    unittest.main()
