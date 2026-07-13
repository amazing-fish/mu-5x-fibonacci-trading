import unittest
from unittest.mock import patch

from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.experiments.fibonacci_pullback import (
    AssetFibonacciBacktest,
    FibonacciHorizonBacktest,
    MonthlyBacktest,
    fib_lookback_bars,
    rank_target_horizons,
    render_fibonacci_pullback_report,
    render_multi_asset_report,
    resolve_asset,
    run_fibonacci_horizon_backtests,
    split_by_utc_month,
)
from mu_strategy.models import BacktestResult, Candle
from mu_strategy.strategy import StrategyConfig


DAY_MS = 86_400_000
HOUR_MS = 3_600_000
QUARTER_HOUR_MS = 900_000


class FibonacciPullbackExperimentTests(unittest.TestCase):
    def test_horizon_hours_translate_to_15m_fib_lookback_bars(self):
        self.assertEqual(4, fib_lookback_bars(1))
        self.assertEqual(8, fib_lookback_bars(2))
        self.assertEqual(32, fib_lookback_bars(8))
        self.assertEqual(48, fib_lookback_bars(12))

    def test_resolve_asset_maps_requested_names_to_okx_swaps(self):
        self.assertEqual("MU-USDT-SWAP", resolve_asset("mu").symbol)
        self.assertEqual("SPCX-USDT-SWAP", resolve_asset("spacex").symbol)
        self.assertEqual("META-USDT-SWAP", resolve_asset("meta").symbol)
        self.assertEqual("BTC-USDT-SWAP", resolve_asset("btc").symbol)
        self.assertEqual("okx", resolve_asset("spacex").source)

    def test_split_by_utc_month_uses_calendar_months(self):
        candles = [
            _candle(1_767_225_600_000),  # 2026-01-01 00:00 UTC
            _candle(1_769_817_600_000),  # 2026-01-31 00:00 UTC
            _candle(1_769_904_000_000),  # 2026-02-01 00:00 UTC
        ]

        months = split_by_utc_month(candles)

        self.assertEqual(["2026-01", "2026-02"], [month for month, _ in months])
        self.assertEqual(2, len(months[0][1]))
        self.assertEqual(1, len(months[1][1]))

    def test_run_horizon_backtests_applies_each_lookback_to_full_and_monthly_runs(self):
        candles_15m = [
            _candle(1_767_225_600_000),
            _candle(1_767_226_500_000),
            _candle(1_769_904_000_000),
            _candle(1_769_904_900_000),
        ]
        candles_1h = [
            _candle(1_767_225_600_000),
            _candle(1_769_904_000_000),
        ]
        seen_lookbacks: list[int] = []

        def fake_run_backtest(segment, context, *, config):
            seen_lookbacks.append(config.fib_lookback)
            return BacktestResult(10_000, 10_100 + config.fib_lookback, [], [(segment[0].open_time_ms, 10_000)])

        with patch("mu_strategy.experiments.fibonacci_pullback.run_backtest", side_effect=fake_run_backtest):
            results = run_fibonacci_horizon_backtests(
                candles_15m,
                candles_1h,
                base_config=StrategyConfig(),
                horizons_hours=[2, 3],
            )

        self.assertEqual([2, 3], [result.horizon_hours for result in results])
        self.assertEqual([8, 12], [result.fib_lookback_bars for result in results])
        self.assertEqual([8, 8, 8, 12, 12, 12], seen_lookbacks)
        self.assertEqual(["2026-01", "2026-02"], [month.month for month in results[0].monthly_results])

    def test_monthly_partitions_preserve_canonical_hourly_context(self):
        month_start = 1_769_904_000_000  # 2026-02-01 00:00 UTC
        candles_15m = [
            _priced_candle(month_start, 200.0),
            _priced_candle(month_start + QUARTER_HOUR_MS, 200.0),
        ]
        candles_1h = [
            _priced_candle(month_start + ((index - 40) * HOUR_MS), 100.0 * (1.02**index))
            for index in range(41)
        ]
        canonical_context = build_hourly_context(candles_15m, candles_1h)
        captured_contexts: list[dict[int, str]] = []

        def capture_context(_segment, context, *, config):
            captured_contexts.append(context)
            return BacktestResult(10_000.0, 10_000.0, [], [])

        with patch("mu_strategy.experiments.fibonacci_pullback.run_backtest", side_effect=capture_context):
            run_fibonacci_horizon_backtests(
                candles_15m,
                candles_1h,
                base_config=StrategyConfig(),
                horizons_hours=[2],
            )

        self.assertEqual("green", canonical_context[month_start])
        self.assertEqual([canonical_context, canonical_context], captured_contexts)

    def test_report_contains_horizon_summary_and_monthly_analysis(self):
        horizon_result = FibonacciHorizonBacktest(
            horizon_hours=2,
            fib_lookback_bars=8,
            full_result=BacktestResult(10_000, 10_500, [], [(0, 10_000), (1, 10_500)]),
            monthly_results=[
                MonthlyBacktest("2026-01", 0, DAY_MS, BacktestResult(10_000, 10_200, [], []), 96),
                MonthlyBacktest("2026-02", DAY_MS, 2 * DAY_MS, BacktestResult(10_000, 9_900, [], []), 96),
            ],
        )

        report = render_fibonacci_pullback_report(
            [horizon_result],
            symbol="MU-USDT-SWAP",
            source="okx",
            days=180,
            strategy_name="baseline",
            data_files=[],
        )

        self.assertIn("Fibonacci 回调 2h-2h 请求 180d 月度回测", report)
        self.assertIn("actual 15m coverage:", report)
        self.assertIn("fib_lookback bars: 8", report)
        self.assertIn("| 2h | 8 | 5.00%", report)
        self.assertIn("| 2026-01 | 2h |", report)
        self.assertIn("| 2026-02 | 2h |", report)
        self.assertIn("研究用途，不构成投资建议", report)

    def test_rank_target_horizons_marks_best_and_top_three_as_near_best(self):
        results = [
            _horizon(1, 0.05),
            _horizon(2, 0.20),
            _horizon(3, 0.10),
            _horizon(4, 0.15),
            _horizon(5, 0.01),
        ]

        verdicts = rank_target_horizons(results, target_hours=(2, 4), near_top=3)

        self.assertEqual("最优", verdicts[2].status)
        self.assertEqual(1, verdicts[2].rank)
        self.assertEqual("较优", verdicts[4].status)
        self.assertEqual(2, verdicts[4].rank)

    def test_multi_asset_report_compares_two_and_four_hour_rankings(self):
        asset_result = AssetFibonacciBacktest(
            asset=resolve_asset("spacex"),
            horizon_results=[
                _horizon_with_month(1, 0.05, "2026-01", 0.01),
                _horizon_with_month(2, 0.20, "2026-01", 0.03),
                _horizon_with_month(4, 0.08, "2026-01", 0.02),
            ],
            data_files=[],
        )

        report = render_multi_asset_report(
            [asset_result],
            days=180,
            strategy_name="baseline",
            min_hour=1,
            max_hour=12,
            target_hours=(2, 4),
        )

        self.assertIn("多标的 Fibonacci 回调 1h-12h 请求 180d 回测", report)
        self.assertIn("SPACEX", report)
        self.assertIn("SPCX-USDT-SWAP", report)
        self.assertIn("2h 状态", report)
        self.assertIn("最优", report)
        self.assertIn("较优", report)
        self.assertIn("月度 2h/4h 判定", report)
        self.assertIn("| SPACEX | 2026-01 | 2h", report)

    def test_multi_asset_report_prints_selected_strategy(self):
        asset_result = AssetFibonacciBacktest(
            asset=resolve_asset("mu"),
            horizon_results=[_horizon(2, 0.05)],
            data_files=[],
        )

        report = render_multi_asset_report(
            [asset_result],
            days=180,
            strategy_name="baseline_half_protect",
            min_hour=1,
            max_hour=12,
            target_hours=(2, 4),
        )

        self.assertIn("同一套 baseline_half_protect 规则", report)
        self.assertIn("- strategy: baseline_half_protect", report)
        self.assertNotIn("同一套 baseline 规则", report)


def _candle(open_time_ms: int) -> Candle:
    return Candle(open_time_ms, 100, 101, 99, 100.5, 1000)


def _priced_candle(open_time_ms: int, price: float) -> Candle:
    return Candle(open_time_ms, price, price, price, price, 1000)


def _horizon(hours: int, total_return_pct: float) -> FibonacciHorizonBacktest:
    return FibonacciHorizonBacktest(
        horizon_hours=hours,
        fib_lookback_bars=fib_lookback_bars(hours),
        full_result=BacktestResult(10_000, 10_000 * (1 + total_return_pct), [], [(0, 10_000)]),
        monthly_results=[],
    )


def _horizon_with_month(
    hours: int, total_return_pct: float, month: str, monthly_return_pct: float
) -> FibonacciHorizonBacktest:
    return FibonacciHorizonBacktest(
        horizon_hours=hours,
        fib_lookback_bars=fib_lookback_bars(hours),
        full_result=BacktestResult(10_000, 10_000 * (1 + total_return_pct), [], [(0, 10_000)]),
        monthly_results=[
            MonthlyBacktest(
                month,
                0,
                DAY_MS,
                BacktestResult(10_000, 10_000 * (1 + monthly_return_pct), [], [(0, 10_000)]),
                96,
            )
        ],
    )


if __name__ == "__main__":
    unittest.main()
