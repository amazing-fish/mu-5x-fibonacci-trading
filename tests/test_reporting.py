import unittest

from mu_strategy.models import BacktestResult
from mu_strategy.reporting import render_markdown_report
from mu_strategy.strategy import StrategyConfig, with_fee_profile


class ReportingTests(unittest.TestCase):
    def test_markdown_report_discloses_fee_profile_and_rate(self):
        config = with_fee_profile(StrategyConfig(), "market")

        report = render_markdown_report(
            BacktestResult(10_000, 10_000, [], []),
            config=config,
            symbol="MU-USDT-SWAP",
            data_files=[],
        )

        self.assertIn("- fee profile: market/taker (市价/吃单)", report)
        self.assertIn("- fee rate: 0.0500%", report)


if __name__ == "__main__":
    unittest.main()
