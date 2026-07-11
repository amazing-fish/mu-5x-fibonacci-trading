import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import mu_strategy.execution.decision as decision_module
from mu_strategy.execution.decision import ExecutionDecision, execution_decision
from mu_strategy.execution.plan import planned_margin_steps
from mu_strategy.models import (
    Candle,
    EntryDecisionCode,
    EntryDecisionStage,
    EntryDisposition,
    EntrySignal,
)
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.strategy import EntryExecution, StrategyConfig, optimized_strategy_group


class ExecutionPlanningTests(unittest.TestCase):
    def test_execution_decision_without_candles_has_typed_input_wait(self):
        decision = execution_decision(
            [],
            config=baseline_strategy_group("MU-USDT-SWAP").config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
        )

        self.assertEqual("wait", decision.action)
        self.assertEqual("no candles", decision.reason)
        self.assertIs(EntryDecisionCode.NO_CANDLES, decision.decision_code)
        self.assertIs(EntryDisposition.WAIT, decision.disposition)
        self.assertIs(EntryDecisionStage.INPUT, decision.stage)

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
        self.assertIs(EntryDecisionCode.EXECUTION_ACCEPTED, decision.decision_code)
        self.assertIs(EntryDisposition.READY, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

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
        self.assertIs(EntryDecisionCode.WAITING_SECOND_PULLBACK, decision.decision_code)
        self.assertIs(EntryDisposition.WAIT, decision.disposition)
        self.assertIs(EntryDecisionStage.PENDING_ENTRY, decision.stage)

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
        self.assertIs(EntryDecisionCode.CURRENT_BAR_OUTSIDE_TRADING_WINDOW, decision.decision_code)
        self.assertIs(EntryDisposition.WAIT, decision.disposition)
        self.assertIs(EntryDecisionStage.INPUT, decision.stage)

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
        self.assertIs(EntryDecisionCode.NEXT_FILL_OUTSIDE_TRADING_WINDOW, decision.decision_code)
        self.assertIs(EntryDisposition.WAIT, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

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
        self.assertIs(EntryDecisionCode.SIGNAL_CANDLE_TOO_WIDE, decision.decision_code)
        self.assertIs(EntryDisposition.BLOCK, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

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
        self.assertIs(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, decision.decision_code)
        self.assertIs(EntryDisposition.WAIT, decision.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, decision.stage)

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
        self.assertIs(EntryDecisionCode.REGIME_BLOCKED, decision.decision_code)
        self.assertIs(EntryDisposition.BLOCK, decision.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, decision.stage)

    def test_execution_decision_waits_for_next_candle_with_typed_code(self):
        config = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="direct_next_open")
        candles = _candidate_signal_candles()[:4]

        decision = execution_decision(
            candles,
            config=config,
            regime="green",
            rsi14=55,
            macd_hist=0.3,
            macd_hist_prev=0.1,
            now_index=3,
        )

        self.assertEqual("wait", decision.action)
        self.assertEqual("next candle required for execution gate", decision.reason)
        self.assertIs(EntryDecisionCode.NEXT_CANDLE_REQUIRED, decision.decision_code)
        self.assertIs(EntryDisposition.WAIT, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

    def test_execution_decision_preserves_wait_for_missing_validated_entry_price(self):
        config = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="direct_next_open")
        missing_price = EntryExecution(
            True,
            "validated execution price is unavailable",
            decision_code=EntryDecisionCode.EXECUTION_ACCEPTED,
        )

        with patch("mu_strategy.execution.decision.should_execute_entry", return_value=missing_price):
            decision = execution_decision(
                _candidate_signal_candles(),
                config=config,
                regime="green",
                rsi14=55,
                macd_hist=0.3,
                macd_hist_prev=0.1,
                now_index=3,
            )

        self.assertEqual("wait", decision.action)
        self.assertEqual("validated execution price is unavailable", decision.reason)
        self.assertIs(EntryDecisionCode.EXECUTION_PRICE_UNAVAILABLE, decision.decision_code)
        self.assertIs(EntryDisposition.WAIT, decision.disposition)
        self.assertIs(EntryDecisionStage.EXECUTION, decision.stage)

    def test_signal_code_blocks_even_when_reason_copy_changes_completely(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        signal = EntrySignal(
            False,
            "policy copy was rewritten without legacy keywords",
            decision_code=EntryDecisionCode.REGIME_BLOCKED,
        )

        with patch("mu_strategy.execution.decision.should_enter_long", return_value=signal):
            decision = execution_decision(
                _candidate_signal_candles(),
                config=config,
                regime="green",
                rsi14=55,
                macd_hist=0.3,
                macd_hist_prev=0.1,
                now_index=3,
            )

        self.assertEqual("block", decision.action)
        self.assertEqual("policy copy was rewritten without legacy keywords", decision.reason)
        self.assertIs(EntryDecisionCode.REGIME_BLOCKED, decision.decision_code)

    def test_wait_code_stays_wait_when_reason_contains_legacy_macd_keyword(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        signal = EntrySignal(
            False,
            "MACD is mentioned for diagnostics, but the signal is merely incomplete",
            decision_code=EntryDecisionCode.NO_CONFIRMED_FIB_RETEST,
        )

        with patch("mu_strategy.execution.decision.should_enter_long", return_value=signal):
            decision = execution_decision(
                _candidate_signal_candles(),
                config=config,
                regime="green",
                rsi14=55,
                macd_hist=0.3,
                macd_hist_prev=0.1,
                now_index=3,
            )

        self.assertEqual("wait", decision.action)
        self.assertIn("MACD", decision.reason)
        self.assertIs(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, decision.decision_code)

    def test_unknown_legacy_signal_cannot_fall_back_to_reason_classification(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        legacy_signal = EntrySignal(False, "MACD legacy copy")

        with patch("mu_strategy.execution.decision.should_enter_long", return_value=legacy_signal):
            with self.assertRaisesRegex(ValueError, "UNKNOWN"):
                execution_decision(
                    _candidate_signal_candles(),
                    config=config,
                    regime="green",
                    rsi14=55,
                    macd_hist=0.3,
                    macd_hist_prev=0.1,
                    now_index=3,
                )

    def test_typed_execution_decision_action_is_projected_from_disposition(self):
        decision = ExecutionDecision(
            "wait",
            "copy is not part of control flow",
            decision_code=EntryDecisionCode.REGIME_BLOCKED,
        )

        self.assertEqual("block", decision.action)
        self.assertIs(EntryDisposition.BLOCK, decision.disposition)
        self.assertIs(EntryDecisionStage.SIGNAL, decision.stage)

    def test_legacy_execution_decision_constructor_remains_compatible(self):
        decision = ExecutionDecision("legacy", "legacy reason", 98.0, (0.2,), 100.0)

        self.assertEqual("legacy", decision.action)
        self.assertEqual("legacy reason", decision.reason)
        self.assertEqual(98.0, decision.initial_stop)
        self.assertEqual((0.2,), decision.margin_steps)
        self.assertEqual(100.0, decision.fib_level)
        self.assertIs(EntryDecisionCode.UNKNOWN, decision.decision_code)
        self.assertIs(EntryDisposition.UNKNOWN, decision.disposition)
        self.assertIs(EntryDecisionStage.UNKNOWN, decision.stage)

    def test_reason_classifier_is_removed(self):
        self.assertFalse(hasattr(decision_module, "_is_hard_block"))

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
