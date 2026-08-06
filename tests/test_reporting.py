import unittest

from mu_strategy.models import BacktestResult, Candle, Trade
from mu_strategy.reporting import render_markdown_report
from mu_strategy.strategy import StrategyConfig, with_fee_profile


def trade(pnl: float, *, stage: int, index: int) -> Trade:
    return Trade(
        entry_time_ms=index * 1_000,
        exit_time_ms=(index + 1) * 1_000,
        entry_price=100.0,
        exit_price=100.0,
        fills=[],
        pnl=pnl,
        fees=0.0,
        return_pct=0.0,
        max_stage=stage,
        exit_reason="test",
    )


def candle(index: int, open_: float, close: float) -> Candle:
    return Candle(
        open_time_ms=index * 900_000,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1_000.0,
    )


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

    def test_markdown_report_self_reports_benchmark_concentration_and_stage_distribution(self):
        trades = [
            trade(100, stage=4, index=0),
            trade(90, stage=4, index=1),
            trade(80, stage=4, index=2),
            trade(70, stage=4, index=3),
            trade(60, stage=4, index=4),
            trade(10, stage=4, index=5),
            trade(-20, stage=1, index=6),
            trade(-30, stage=2, index=7),
        ]
        report = render_markdown_report(
            BacktestResult(1_000, 1_300, trades, []),
            config=StrategyConfig(leverage=5.0),
            symbol="MU-USDT-SWAP",
            data_files=[],
            candles=[candle(0, 100, 105), candle(1, 105, 120)],
        )

        section_positions = [
            report.index("## Config"),
            report.index("## Metrics"),
            report.index("## Robustness"),
            report.index("## Trades"),
        ]
        self.assertEqual(sorted(section_positions), section_positions)
        self.assertIn("- buy-and-hold benchmark (1x): 20.00%", report)
        self.assertIn("- buy-and-hold benchmark (5x price-only diagnostic): 100.00%", report)
        self.assertIn("- strategy excess vs 5x price-only diagnostic: -70.00%", report)
        self.assertIn("top 5 winners' share of net PnL: 111.11%", report)
        self.assertIn("removing the top 5 leaves the strategy unprofitable", report)
        self.assertIn("| 4 | 6 | 6 | 100.00% | 410.00 |", report)
        self.assertIn("return (on margin)", report)

    def test_markdown_report_without_candles_skips_only_benchmark_rows(self):
        report = render_markdown_report(
            BacktestResult(10_000, 9_990, [trade(-10, stage=1, index=0)], []),
            config=StrategyConfig(),
            symbol="MU-USDT-SWAP",
            data_files=[],
        )

        self.assertIn("## Robustness", report)
        self.assertNotIn("buy-and-hold benchmark", report)
        self.assertIn("top 5 winners' share of net PnL", report)
        self.assertIn("| 1 | 1 | 0 | 0.00% | -10.00 |", report)

    def test_markdown_report_handles_no_trades_in_robustness_section(self):
        report = render_markdown_report(
            BacktestResult(10_000, 10_000, [], []),
            config=StrategyConfig(),
            symbol="MU-USDT-SWAP",
            data_files=[],
            candles=[],
        )

        self.assertIn("- top 5 winners' share of net PnL: n/a (net PnL is not positive)", report)
        self.assertIn("| - | 0 | 0 | 0.00% | 0.00 |", report)


if __name__ == "__main__":
    unittest.main()
