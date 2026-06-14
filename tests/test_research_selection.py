import unittest

from mu_strategy.research.mu_current import current_mu_strategy_name
from mu_strategy.selection.basket import rank_candidates


class ResearchSelectionTests(unittest.TestCase):
    def test_current_mu_strategy_name_is_baseline(self):
        self.assertEqual("baseline", current_mu_strategy_name())

    def test_rank_candidates_sorts_rows_without_network(self):
        rows = [
            {"symbol": "HIGH_DD", "total_return_pct": 0.20, "max_drawdown_pct": -0.18, "profit_factor": 1.4},
            {"symbol": "BALANCED", "total_return_pct": 0.16, "max_drawdown_pct": -0.04, "profit_factor": 1.2},
            {"symbol": "WEAK", "total_return_pct": -0.02, "max_drawdown_pct": -0.03, "profit_factor": 0.8},
        ]

        ranked = rank_candidates(rows)

        self.assertEqual(["BALANCED", "HIGH_DD", "WEAK"], [row["symbol"] for row in ranked])
        self.assertIn("score", ranked[0])
        self.assertNotIn("score", rows[0])


if __name__ == "__main__":
    unittest.main()
