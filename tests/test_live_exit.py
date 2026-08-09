import unittest

from mu_strategy.live_exit import evaluate_exit, map_okx_position, observe_okx_position
from mu_strategy.models import Candle
from mu_strategy.strategies.position_rules import PositionFillSnapshot, PositionStateSnapshot
from mu_strategy.strategy import StrategyConfig


def candle(index, open_, high, low, close):
    return Candle(index * 900_000, open_, high, low, close, 1000)


def stage_one_position(*, stop_price=98):
    return PositionStateSnapshot(
        fills=(PositionFillSnapshot(0, 100, 1),),
        stop_price=stop_price,
        entry_anchor=100,
        initial_stop_price=98,
        max_stage=1,
    )


class LiveExitTests(unittest.TestCase):
    def test_unbroken_stop_does_not_report_exit(self):
        candles = [candle(0, 100, 101, 99, 100)]

        observation = evaluate_exit(
            stage_one_position(),
            candles[0],
            index=0,
            candles=candles,
            regime="yellow",
            config=StrategyConfig(),
        )

        self.assertFalse(observation.exit_triggered)
        self.assertIsNone(observation.exit_reason)
        self.assertEqual(98, observation.stop_before_candle)
        self.assertEqual(98, observation.stop_after_candle_if_open)

    def test_stop_trigger_uses_backtest_low_and_reports_backtest_reason(self):
        candles = [candle(0, 100, 101, 97, 99)]

        observation = evaluate_exit(
            stage_one_position(),
            candles[0],
            index=0,
            candles=candles,
            regime="yellow",
            config=StrategyConfig(),
        )

        self.assertTrue(observation.exit_triggered)
        self.assertEqual("stop", observation.exit_reason)
        self.assertEqual("candle_low_at_or_below_stop_before_candle", observation.trigger_basis)

    def test_close_below_new_stop_is_diagnostic_not_current_candle_exit(self):
        candles = [
            candle(0, 111, 112, 110, 111),
            candle(1, 112, 113, 111, 112),
            candle(2, 113, 114, 112, 113),
            candle(3, 114, 115, 113, 114),
            candle(4, 115, 116, 106, 108),
        ]
        position = PositionStateSnapshot(
            fills=tuple(PositionFillSnapshot(index * 900_000, price, 1) for index, price in enumerate((100, 102, 104, 106))),
            stop_price=105,
            entry_anchor=100,
            initial_stop_price=98,
            max_stage=4,
        )

        observation = evaluate_exit(
            position,
            candles[-1],
            index=4,
            candles=candles,
            regime="green",
            config=StrategyConfig(),
        )

        self.assertFalse(observation.exit_triggered)
        self.assertGreater(observation.stop_after_candle_if_open, observation.latest_close)
        self.assertTrue(observation.latest_close_at_or_below_tightened_stop)

    def test_mismatched_current_candle_propagates_shared_contract_error(self):
        candles = [candle(0, 100, 101, 99, 100)]
        mismatched = candle(1, 100, 101, 99, 100)

        with self.assertRaisesRegex(ValueError, r"current candle must match candles\[index\]"):
            evaluate_exit(
                stage_one_position(),
                mismatched,
                index=0,
                candles=candles,
                regime="yellow",
                config=StrategyConfig(),
            )

    def test_missing_okx_fields_are_unknown_without_fabricated_snapshot(self):
        candles = [candle(0, 100, 101, 99, 100)]

        mapped = map_okx_position(
            {"instId": "MU-USDT-SWAP", "pos": "1"},
            candle=candles[0],
            config=StrategyConfig(),
        )
        observation = observe_okx_position(
            {"instId": "MU-USDT-SWAP", "pos": "1"},
            candles=candles,
            regime="yellow",
            config=StrategyConfig(),
        )

        self.assertIsNone(mapped.assumption_snapshot)
        self.assertIn("avgPx", mapped.unknown_fields)
        self.assertEqual("unknown", observation.decision_status)
        self.assertEqual("unavailable", observation.state_quality)
        self.assertIsNone(observation.assumption_evaluation)
        self.assertEqual("missing_or_invalid_average_entry_price", observation.unavailable_reason)

    def test_aggregate_okx_row_keeps_unknown_state_visible_in_degraded_evaluation(self):
        candles = [candle(0, 100, 101, 97, 99)]

        observation = observe_okx_position(
            {"instId": "MU-USDT-SWAP", "pos": "2", "avgPx": "100", "posSide": "long"},
            candles=candles,
            regime="yellow",
            config=StrategyConfig(),
        )

        self.assertEqual("unknown", observation.decision_status)
        self.assertEqual("degraded", observation.state_quality)
        self.assertIn("fills", observation.unknown_fields)
        self.assertIn("max_stage", observation.unknown_fields)
        self.assertIn("max_stage=1", observation.assumptions)
        self.assertIsNotNone(observation.assumption_evaluation)
        self.assertEqual(98, observation.assumption_evaluation.stop_before_candle)
        self.assertTrue(observation.assumption_evaluation.exit_triggered)

    def test_no_candles_returns_unknown_observation(self):
        observation = observe_okx_position(
            {"instId": "MU-USDT-SWAP", "pos": "2", "avgPx": "100"},
            candles=(),
            regime="yellow",
            config=StrategyConfig(),
            unavailable_reason="market_data_stale",
        )

        self.assertEqual("unknown", observation.decision_status)
        self.assertEqual("market_data_stale", observation.unavailable_reason)
        self.assertIsNone(observation.assumption_evaluation)


if __name__ == "__main__":
    unittest.main()
