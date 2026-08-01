import io
import os
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.market_data.service import refresh_trusted_candle_bundle
from mu_strategy.models import BacktestResult, Candle
from mu_strategy.strategy import default_strategy_groups, StrategyConfig
from mu_strategy.experiments.walk_forward import (
    StrategyGroupBacktest,
    WindowBacktest,
    render_strategy_group_html_dashboard,
    render_strategy_group_report,
    render_walk_forward_report,
    run_walk_forward_backtests,
    split_into_windows,
)
from tests.factories.trusted_publication import write_generation_publication


DAY_MS = 86_400_000
HOUR_MS = 3_600_000
QUARTER_HOUR_MS = 900_000


class WalkForwardTests(unittest.TestCase):
    def test_main_is_cache_only_and_defaults_to_trusted_live_store(self):
        from mu_strategy.experiments import walk_forward

        now_ms = 20 * DAY_MS
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "live"
            report_path = root / "walk-forward.md"
            html_report_path = root / "walk-forward.html"
            write_generation_publication(
                data_dir,
                symbol="MU-USDT-SWAP",
                start_ms=now_ms - DAY_MS,
                end_ms=now_ms,
            )
            argv = [
                "mu_strategy.experiments.walk_forward",
                "--window-days",
                "1",
                "--windows",
                "1",
                "--strategy",
                "baseline",
                "--report",
                str(report_path),
                "--html-report",
                str(html_report_path),
            ]
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                with _blocked_market_data_paths("mu_strategy.experiments.walk_forward"):
                    with patch(
                        "mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms",
                        return_value=now_ms,
                    ):
                        with patch(
                            "mu_strategy.experiments.walk_forward.refresh_trusted_candle_bundle",
                            wraps=refresh_trusted_candle_bundle,
                        ) as trusted_loader:
                            with patch("sys.argv", argv):
                                with patch("sys.stdout", new_callable=io.StringIO):
                                    walk_forward.main()
            finally:
                os.chdir(original_cwd)

            self.assertTrue(report_path.exists())
            self.assertTrue(html_report_path.exists())
            self.assertIn("策略组对比", report_path.read_text(encoding="utf-8"))
            self.assertIn('<html lang="zh-CN">', html_report_path.read_text(encoding="utf-8"))
            self.assertEqual(("15m", "1h"), trusted_loader.call_args.kwargs["intervals"])
            self.assertEqual(Path("data/live"), trusted_loader.call_args.kwargs["data_dir"])
            self.assertFalse(trusted_loader.call_args.kwargs["refresh"])

    def test_main_fails_closed_without_trusted_publication_and_writes_no_report(self):
        from mu_strategy.experiments import walk_forward

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "walk-forward.md"
            html_report_path = root / "walk-forward.html"
            argv = [
                "mu_strategy.experiments.walk_forward",
                "--window-days",
                "1",
                "--windows",
                "1",
                "--data-dir",
                str(root / "missing"),
                "--report",
                str(report_path),
                "--html-report",
                str(html_report_path),
            ]
            with patch("sys.argv", argv):
                with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as raised:
                        walk_forward.main()

            self.assertNotEqual(0, raised.exception.code)
            self.assertIn("trusted data blocked", stderr.getvalue())
            self.assertFalse(report_path.exists())
            self.assertFalse(html_report_path.exists())

    def test_removed_legacy_cli_options_are_rejected(self):
        from mu_strategy.experiments import walk_forward

        for option in ("--refresh", "--source"):
            with self.subTest(option=option):
                argv = ["mu_strategy.experiments.walk_forward", option]
                if option == "--source":
                    argv.append("okx")
                with patch("sys.argv", argv):
                    with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        with self.assertRaises(SystemExit) as raised:
                            walk_forward.main()

                self.assertNotEqual(0, raised.exception.code)
                self.assertIn("unrecognized arguments", stderr.getvalue())

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
        self.assertIn("## 策略组件矩阵", report)
        self.assertIn("入场策略", report)
        self.assertIn("加减仓策略", report)
        self.assertIn("出场策略", report)
        self.assertIn("过滤策略", report)

    def test_strategy_group_html_dashboard_visualizes_components(self):
        groups = default_strategy_groups("MU-USDT-SWAP")
        group_results = [
            StrategyGroupBacktest(group, [WindowBacktest(1, 0, 14 * DAY_MS, BacktestResult(10_000, 11_000, [], []), 14)])
            for group in groups[:2]
        ]

        html = render_strategy_group_html_dashboard(group_results, symbol="MU-USDT-SWAP", data_files=[])

        self.assertIn('<html lang="zh-CN">', html)
        self.assertIn("策略组件矩阵", html)
        self.assertIn("入场策略", html)
        self.assertIn("加减仓策略", html)
        self.assertIn("出场策略", html)
        self.assertIn("过滤策略", html)
        self.assertIn("baseline", html)

    def test_strategy_group_html_uses_per_window_drawdown_without_reset_drop(self):
        group = default_strategy_groups("MU-USDT-SWAP")[0]
        group_result = StrategyGroupBacktest(
            group,
            [
                WindowBacktest(
                    1,
                    0,
                    DAY_MS,
                    BacktestResult(10_000, 12_000, [], [(0, 10_000), (1, 12_000)]),
                    2,
                ),
                WindowBacktest(
                    2,
                    DAY_MS,
                    2 * DAY_MS,
                    BacktestResult(10_000, 10_000, [], [(2, 10_000), (3, 9_500), (4, 10_000)]),
                    3,
                ),
            ],
        )

        html = render_strategy_group_html_dashboard([group_result], symbol="MU-USDT-SWAP", data_files=[])

        self.assertIn("-5.00%", html)
        self.assertNotIn("-20.83%", html)

    def test_walk_forward_partitions_preserve_canonical_hourly_context(self):
        candles_15m = [_priced_candle(index * QUARTER_HOUR_MS, 200.0) for index in range(96)]
        candles_1h = [
            _priced_candle((index - 40) * HOUR_MS, 100.0 * (1.02**index))
            for index in range(64)
        ]
        canonical_context = build_hourly_context(candles_15m, candles_1h)
        captured_contexts: list[dict[int, str]] = []

        def capture_context(_segment, context, *, config):
            captured_contexts.append(context)
            return BacktestResult(10_000.0, 10_000.0, [], [])

        with patch("mu_strategy.experiments.walk_forward.run_backtest", side_effect=capture_context):
            results = run_walk_forward_backtests(
                candles_15m,
                candles_1h,
                config=StrategyConfig(),
                window_days=1,
                windows=1,
            )

        self.assertEqual(1, len(results))
        self.assertEqual("green", canonical_context[0])
        self.assertEqual([canonical_context], captured_contexts)


def _candle(day: int) -> Candle:
    price = 100 + day
    return Candle(day * DAY_MS, price, price + 1, price - 1, price + 0.5, 1000)


def _priced_candle(open_time_ms: int, price: float) -> Candle:
    return Candle(open_time_ms, price, price, price, price, 1000)


@contextmanager
def _blocked_market_data_paths(module_name: str):
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                f"{module_name}.cached_historical",
                side_effect=AssertionError("legacy cache must not be used"),
                create=True,
            )
        )
        stack.enter_context(
            patch(
                f"{module_name}.refresh_candle_bundle",
                side_effect=AssertionError("legacy bundle must not be used"),
                create=True,
            )
        )
        for target in (
            "mu_strategy.market_data.cache.fetch_okx_historical",
            "mu_strategy.market_data.cache.fetch_okx_incremental",
            "mu_strategy.market_data.cache.fetch_historical",
            "mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical",
            "mu_strategy.market_data.trusted_data.refresh.fetch_okx_incremental",
        ):
            stack.enter_context(patch(target, side_effect=AssertionError("network must not be used")))
        stack.enter_context(
            patch(
                "mu_strategy.market_data.cache.write_csv",
                side_effect=AssertionError("cache write must not be used"),
            )
        )
        stack.enter_context(
            patch(
                "mu_strategy.market_data.trusted_data.store.TrustedDataStore.write_csv",
                side_effect=AssertionError("trusted store write must not be used"),
            )
        )
        yield


if __name__ == "__main__":
    unittest.main()
