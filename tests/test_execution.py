from decimal import Decimal
import unittest

from mu_strategy.execution import OKXInstrumentSpec, initial_stop_price, planned_margin_steps
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.strategy import StrategyConfig


class ExecutionPlanningTests(unittest.TestCase):
    def test_planned_margin_steps_are_copied_from_config(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config

        self.assertEqual((0.20, 0.20, 0.20, 0.40), planned_margin_steps(config))

    def test_initial_stop_price_applies_initial_stop_pct(self):
        config = StrategyConfig(symbol="MU-USDT-SWAP", initial_stop_pct=0.02)

        self.assertAlmostEqual(98.0, initial_stop_price(100.0, config))


class OKXInstrumentSpecTests(unittest.TestCase):
    def test_from_row_and_floor_rounding_for_price_and_size(self):
        spec = OKXInstrumentSpec.from_row(
            {"instId": "MU-USDT-SWAP", "tickSz": "0.1", "lotSz": "0.01", "ctVal": "1"}
        )

        self.assertEqual("100.2", spec.price_to_string(100.29))
        self.assertEqual("12.34", spec.size_to_string(12.349))
        self.assertEqual("9.97", spec.size_for_notional(1000, price=100.29))

    def test_invalid_spec_and_non_positive_values_fail_closed(self):
        with self.assertRaises(ValueError):
            OKXInstrumentSpec.from_row({"instId": "MU-USDT-SWAP", "tickSz": "0.1"})
        with self.assertRaises(ValueError):
            OKXInstrumentSpec("MU-USDT-SWAP", Decimal("0"), Decimal("0.01"), Decimal("1"))

        spec = OKXInstrumentSpec("MU-USDT-SWAP", Decimal("0.1"), Decimal("0.01"), Decimal("1"))
        for bad in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    spec.price_to_string(bad)
        with self.assertRaises(ValueError):
            spec.price_to_string(0.05)


if __name__ == "__main__":
    unittest.main()
