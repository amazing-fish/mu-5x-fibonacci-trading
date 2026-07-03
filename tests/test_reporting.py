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

    def test_markdown_report_discloses_stop_transition_config(self):
        config = StrategyConfig(
            stop_tightening="delayed_baseline",
            stop_transition_bars=8,
            stop_transition_curve="slow_start",
        )

        report = render_markdown_report(
            BacktestResult(10_000, 10_000, [], []),
            config=config,
            symbol="MU-USDT-SWAP",
            data_files=[],
        )

        self.assertIn("- stop tightening: delayed_baseline", report)
        self.assertIn("- stop transition bars: 8", report)
        self.assertIn("- stop transition curve: slow_start", report)

    def test_markdown_report_discloses_actual_sample_length_when_provided(self):
        config = with_fee_profile(StrategyConfig(), "market")

        report = render_markdown_report(
            BacktestResult(10_000, 10_000, [], []),
            config=config,
            symbol="MU-USDT-SWAP",
            data_files=[],
            sample_summary=["15m actual sample: 117d 1h", "1h actual sample: 117d 1h"],
        )

        self.assertIn("- 15m actual sample: 117d 1h", report)
        self.assertIn("- 1h actual sample: 117d 1h", report)


if __name__ == "__main__":
    unittest.main()
