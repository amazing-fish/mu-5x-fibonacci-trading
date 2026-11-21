import unittest
from datetime import datetime, timezone

from mu_strategy.backtest import OpenPosition, _has_non_session_liquidation_risk
from mu_strategy.live_exit import evaluate_exit, map_okx_position, observe_okx_position
from mu_strategy.models import Candle, Fill
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
        preferred_window_ms = int(datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        candles = [Candle(preferred_window_ms, 100, 101, 97, 99, 1000)]

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

    def test_non_session_stop_beats_liquidation_unless_price_gaps_past_stop_to_liquidation(self):
        weekend_ms = int(datetime(2026, 8, 8, tzinfo=timezone.utc).timestamp() * 1000)
        snapshot = stage_one_position()
        config = StrategyConfig()
        backtest_position = OpenPosition(
            fills=[Fill(0, 100, 0.2, 100, 1, 0)],
            stop_price=98,
            entry_anchor=100,
            initial_stop_price=98,
            max_stage=1,
        )

        # Continuous drop from open=100 -> low=79 hits active stop 98 BEFORE liquidation 80.
        continuous = [Candle(weekend_ms, 100, 101, 79, 90, 1000)]
        obs_stop = evaluate_exit(snapshot, continuous[0], index=0, candles=continuous, regime="yellow", config=config)
        self.assertFalse(_has_non_session_liquidation_risk(continuous[0], backtest_position, config))
        self.assertTrue(obs_stop.exit_triggered)
        self.assertEqual("stop", obs_stop.exit_reason)
        self.assertEqual("candle_low_at_or_below_stop_before_candle", obs_stop.trigger_basis)

        # Gap at bar open straight into liquidation territory (open=79 <= liq=80) triggers non_session_liquidation_risk.
        gapped = [Candle(weekend_ms, 79, 85, 75, 82, 1000)]
        obs_liq = evaluate_exit(snapshot, gapped[0], index=0, candles=gapped, regime="yellow", config=config)
        self.assertTrue(_has_non_session_liquidation_risk(gapped[0], backtest_position, config))
        self.assertTrue(obs_liq.exit_triggered)
        self.assertEqual("non_session_liquidation_risk", obs_liq.exit_reason)
        self.assertEqual(
            "candle_low_at_or_below_non_session_liquidation_risk_price",
            obs_liq.trigger_basis,
        )

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

    def test_aggregate_okx_row_reports_actionable_exit_or_hold_with_degraded_quality(self):
        triggered_candles = [candle(0, 100, 101, 97, 99)]
        exit_obs = observe_okx_position(
            {"instId": "MU-USDT-SWAP", "pos": "2", "avgPx": "100", "posSide": "long"},
            candles=triggered_candles,
            regime="yellow",
            config=StrategyConfig(),
        )

        self.assertEqual("exit", exit_obs.decision_status)
        self.assertEqual("degraded", exit_obs.state_quality)
        self.assertIn("fills", exit_obs.unknown_fields)
        self.assertIn("max_stage", exit_obs.unknown_fields)
        self.assertIn("max_stage=1", exit_obs.assumptions)
        self.assertIsNotNone(exit_obs.assumption_evaluation)
        self.assertEqual(98, exit_obs.assumption_evaluation.stop_before_candle)
        self.assertTrue(exit_obs.assumption_evaluation.exit_triggered)

        safe_candles = [candle(0, 100, 101, 99, 100.5)]
        hold_obs = observe_okx_position(
            {"instId": "MU-USDT-SWAP", "pos": "2", "avgPx": "100", "posSide": "long"},
            candles=safe_candles,
            regime="yellow",
            config=StrategyConfig(),
        )
        self.assertEqual("hold", hold_obs.decision_status)
        self.assertEqual("degraded", hold_obs.state_quality)
        self.assertFalse(hold_obs.assumption_evaluation.exit_triggered)

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
