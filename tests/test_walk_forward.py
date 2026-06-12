import unittest

from mu_strategy.models import BacktestResult, Candle
from mu_strategy.strategy import default_strategy_groups, StrategyConfig
from mu_strategy.walk_forward import (
    StrategyGroupBacktest,
    WindowBacktest,
    render_strategy_group_report,
    render_walk_forward_report,
    split_into_windows,
)


DAY_MS = 86_400_000


class WalkForwardTests(unittest.TestCase):
    def test_split_into_two_independent_fourteen_day_windows(self):
        candles = [_candle(day) for day in range(28)]

        windows = split_into_windows(candles, window_days=14, windows=2)

        self.assertEqual(2, len(windows))
        self.assertEqual(14, len(windows[0]))
        self.assertEqual(14, len(windows[1]))
        self.assertEqual(0, windows[0][0].open_time_ms)
        self.assertEqual(13 * DAY_MS, windows[0][-1].open_time_ms)
        self.assertEqual(14 * DAY_MS, windows[1][0].open_time_ms)
        self.assertLess(windows[0][-1].open_time_ms, windows[1][0].open_time_ms)

    def test_report_contains_walk_forward_metrics_and_strategy_analysis(self):
        window_results = [
            WindowBacktest(1, 0, 14 * DAY_MS, BacktestResult(10_000, 8_953, [], []), 1344),
            WindowBacktest(2, 14 * DAY_MS, 28 * DAY_MS, BacktestResult(10_000, 9_449, [], []), 1344),
        ]

        report = render_walk_forward_report(
            window_results,
            config=StrategyConfig(symbol="MUUSDT"),
            symbol="MUUSDT",
            data_files=[],
        )

        self.assertIn("MUUSDT 两段 14 天回测", report)
        self.assertIn("第1段", report)
        self.assertIn("第2段", report)
        self.assertIn("防止过拟合", report)
        self.assertIn("入场在高点", report)
        self.assertIn("第二段加仓高点", report)
        self.assertIn("连续三天的大跌", report)
        self.assertIn("7%收益", report)
        self.assertIn("顶层设计", report)

    def test_strategy_group_report_keeps_baseline_and_optimized_side_by_side(self):
        groups = default_strategy_groups("MUUSDT")
        group_results = [
            StrategyGroupBacktest(group, [WindowBacktest(1, 0, 14 * DAY_MS, BacktestResult(10_000, 9_000, [], []), 14)])
            for group in groups
        ]

        report = render_strategy_group_report(group_results, symbol="MUUSDT", data_files=[])

        self.assertIn("策略组对比", report)
        self.assertIn("legacy_break_high", report)
        self.assertIn("baseline", report)
        self.assertIn("direct_next_open", report)
        self.assertIn("baseline_half_protect", report)
        self.assertIn("baseline_green_wide", report)
        self.assertIn("baseline_yellow_wide", report)
        self.assertIn("baseline_yellow_green_wide", report)
        self.assertIn("baseline_half_green_wide", report)
        self.assertIn("optimized_v2", report)
        self.assertIn("旧突破前高", report)
        self.assertIn("新baseline", report)
        self.assertIn("下一根开盘", report)
        self.assertIn("二次回踩", report)
        self.assertIn("半保护", report)
        self.assertIn("反向 Fibonacci", report)


def _candle(day: int) -> Candle:
    price = 100 + day
    return Candle(day * DAY_MS, price, price + 1, price - 1, price + 0.5, 1000)


if __name__ == "__main__":
    unittest.main()
