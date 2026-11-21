import math
import unittest

from mu_strategy.selection.basket import MAX_PROFIT_FACTOR_FOR_SCORE, rank_candidates


class ResearchSelectionTests(unittest.TestCase):
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

    def test_rank_candidates_clamps_infinite_and_nan_profit_factor(self):
        rows = [
            {"symbol": "ONE_WIN_FLUKE", "total_return_pct": 0.01, "max_drawdown_pct": 0.0, "profit_factor": float("inf")},
            {"symbol": "STRONG", "total_return_pct": 0.25, "max_drawdown_pct": -0.05, "profit_factor": 2.0},
            {"symbol": "NAN_PF", "total_return_pct": 0.05, "max_drawdown_pct": -0.01, "profit_factor": float("nan")},
        ]

        ranked = rank_candidates(rows)

        self.assertEqual(["STRONG", "ONE_WIN_FLUKE", "NAN_PF"], [row["symbol"] for row in ranked])
        for row in ranked:
            self.assertTrue(math.isfinite(row["score"]), row)
        fluke = next(row for row in ranked if row["symbol"] == "ONE_WIN_FLUKE")
        self.assertAlmostEqual(0.01 + MAX_PROFIT_FACTOR_FOR_SCORE * 0.01, fluke["score"])


if __name__ == "__main__":
    unittest.main()
