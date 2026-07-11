import unittest

from mu_strategy.models import (
    ENTRY_DECISION_CATALOG,
    EntryDecisionCode,
    EntryDecisionStage,
    EntryDisposition,
    entry_decision_metadata,
    execution_action_for,
    scanner_action_for,
)


class EntryDecisionContractTests(unittest.TestCase):
    def test_catalog_registers_every_code_with_unique_stable_values(self):
        self.assertEqual(len(EntryDecisionCode), len({code.value for code in EntryDecisionCode}))
        self.assertEqual(set(EntryDecisionCode), set(ENTRY_DECISION_CATALOG))

        for code in EntryDecisionCode:
            with self.subTest(code=code):
                metadata = entry_decision_metadata(code)
                self.assertIs(metadata, ENTRY_DECISION_CATALOG[code])
                if code is EntryDecisionCode.UNKNOWN:
                    self.assertIs(EntryDisposition.UNKNOWN, metadata.disposition)
                    self.assertIs(EntryDecisionStage.UNKNOWN, metadata.stage)
                else:
                    self.assertIsNot(EntryDisposition.UNKNOWN, metadata.disposition)
                    self.assertIsNot(EntryDecisionStage.UNKNOWN, metadata.stage)

    def test_catalog_encodes_the_required_decision_semantics(self):
        expected = {
            EntryDecisionCode.MARKET_DATA_UNAVAILABLE: (EntryDisposition.BLOCK, EntryDecisionStage.INPUT),
            EntryDecisionCode.NO_CANDLES: (EntryDisposition.WAIT, EntryDecisionStage.INPUT),
            EntryDecisionCode.CURRENT_BAR_OUTSIDE_TRADING_WINDOW: (
                EntryDisposition.WAIT,
                EntryDecisionStage.INPUT,
            ),
            EntryDecisionCode.REGIME_BLOCKED: (EntryDisposition.BLOCK, EntryDecisionStage.SIGNAL),
            EntryDecisionCode.RSI_BELOW_FLOOR: (EntryDisposition.BLOCK, EntryDecisionStage.SIGNAL),
            EntryDecisionCode.MACD_WEAKENING: (EntryDisposition.BLOCK, EntryDecisionStage.SIGNAL),
            EntryDecisionCode.NO_CONFIRMED_FIB_RETEST: (EntryDisposition.WAIT, EntryDecisionStage.SIGNAL),
            EntryDecisionCode.NO_RECENT_CONFIRMED_FIB_RETEST: (
                EntryDisposition.WAIT,
                EntryDecisionStage.SIGNAL,
            ),
            EntryDecisionCode.SIGNAL_CONFIRMED: (EntryDisposition.READY, EntryDecisionStage.SIGNAL),
            EntryDecisionCode.WAITING_SECOND_PULLBACK: (
                EntryDisposition.WAIT,
                EntryDecisionStage.PENDING_ENTRY,
            ),
            EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY: (
                EntryDisposition.READY,
                EntryDecisionStage.PENDING_ENTRY,
            ),
            EntryDecisionCode.PRICE_AWAY_FROM_FIB: (
                EntryDisposition.WAIT,
                EntryDecisionStage.PENDING_ENTRY,
            ),
            EntryDecisionCode.NEXT_CANDLE_REQUIRED: (EntryDisposition.WAIT, EntryDecisionStage.EXECUTION),
            EntryDecisionCode.NEXT_FILL_OUTSIDE_TRADING_WINDOW: (
                EntryDisposition.WAIT,
                EntryDecisionStage.EXECUTION,
            ),
            EntryDecisionCode.NEXT_CANDLE_DID_NOT_BREAK_SIGNAL_HIGH: (
                EntryDisposition.WAIT,
                EntryDecisionStage.EXECUTION,
            ),
            EntryDecisionCode.EXECUTION_PRICE_UNAVAILABLE: (
                EntryDisposition.WAIT,
                EntryDecisionStage.EXECUTION,
            ),
            EntryDecisionCode.SIGNAL_CANDLE_TOO_WIDE: (
                EntryDisposition.BLOCK,
                EntryDecisionStage.EXECUTION,
            ),
            EntryDecisionCode.ENTRY_TOO_FAR_ABOVE_FIB: (
                EntryDisposition.BLOCK,
                EntryDecisionStage.EXECUTION,
            ),
            EntryDecisionCode.ENTRY_TOO_FAR_ABOVE_SIGNAL_CLOSE: (
                EntryDisposition.BLOCK,
                EntryDecisionStage.EXECUTION,
            ),
            EntryDecisionCode.REVERSE_FIB_RESISTANCE: (
                EntryDisposition.BLOCK,
                EntryDecisionStage.EXECUTION,
            ),
            EntryDecisionCode.EXECUTION_ACCEPTED: (
                EntryDisposition.READY,
                EntryDecisionStage.EXECUTION,
            ),
        }

        self.assertEqual(set(EntryDecisionCode) - {EntryDecisionCode.UNKNOWN}, set(expected))
        for code, (disposition, stage) in expected.items():
            with self.subTest(code=code):
                metadata = entry_decision_metadata(code)
                self.assertIs(disposition, metadata.disposition)
                self.assertIs(stage, metadata.stage)

    def test_ready_wait_and_block_have_complete_fixed_action_mappings(self):
        expected = {
            EntryDisposition.READY: ("enter", "allow"),
            EntryDisposition.WAIT: ("wait", "wait"),
            EntryDisposition.BLOCK: ("skip", "block"),
        }

        for disposition, (scanner_action, execution_action) in expected.items():
            with self.subTest(disposition=disposition):
                self.assertEqual(scanner_action, scanner_action_for(disposition))
                self.assertEqual(execution_action, execution_action_for(disposition))

    def test_unknown_and_untyped_values_cannot_fall_back_to_text_classification(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN"):
            scanner_action_for(EntryDisposition.UNKNOWN)
        with self.assertRaisesRegex(ValueError, "UNKNOWN"):
            execution_action_for(EntryDisposition.UNKNOWN)
        with self.assertRaises(TypeError):
            entry_decision_metadata("MACD appears in a message")
        with self.assertRaises(TypeError):
            scanner_action_for("wait")
        with self.assertRaises(TypeError):
            execution_action_for("block")


if __name__ == "__main__":
    unittest.main()
