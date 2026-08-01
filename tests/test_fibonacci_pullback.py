import io
import inspect
import os
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
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
    run_asset_fibonacci_backtest,
    run_fibonacci_horizon_backtests,
    split_by_utc_month,
)
from mu_strategy.market_data.service import refresh_trusted_candle_bundle
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.models import BacktestResult, Candle
from mu_strategy.strategy import StrategyConfig
from tests.factories.trusted_publication import (
    write_generation_manifest_and_caches,
    write_generation_publication,
)


DAY_MS = 86_400_000
HOUR_MS = 3_600_000
QUARTER_HOUR_MS = 900_000


class FibonacciPullbackExperimentTests(unittest.TestCase):
    def test_main_is_cache_only_and_defaults_to_trusted_live_store(self):
        from mu_strategy.experiments import fibonacci_pullback

        now_ms = 20 * DAY_MS
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "live"
            report_path = root / "fibonacci.md"
            write_generation_publication(
                data_dir,
                symbol="MU-USDT-SWAP",
                start_ms=now_ms - DAY_MS,
                end_ms=now_ms,
            )
            argv = [
                "mu_strategy.experiments.fibonacci_pullback",
                "--days",
                "1",
                "--min-hour",
                "2",
                "--max-hour",
                "2",
                "--strategy",
                "baseline",
                "--report",
                str(report_path),
            ]
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                with _blocked_market_data_paths("mu_strategy.experiments.fibonacci_pullback"):
                    with patch(
                        "mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms",
                        return_value=now_ms,
                    ):
                        with patch(
                            "mu_strategy.experiments.fibonacci_pullback.refresh_trusted_candle_bundle",
                            wraps=refresh_trusted_candle_bundle,
                        ) as trusted_loader:
                            with patch("sys.argv", argv):
                                with patch("sys.stdout", new_callable=io.StringIO):
                                    fibonacci_pullback.main()
            finally:
                os.chdir(original_cwd)

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("Fibonacci 回调 2h-2h 请求 1d 月度回测", report)
            self.assertIn("- source: okx", report)
            self.assertEqual(("15m", "1h"), trusted_loader.call_args.kwargs["intervals"])
            self.assertEqual(Path("data/live"), trusted_loader.call_args.kwargs["data_dir"])
            self.assertFalse(trusted_loader.call_args.kwargs["refresh"])

    def test_multi_asset_main_is_cache_only_and_preserves_report_structure(self):
        from mu_strategy.experiments import fibonacci_pullback

        now_ms = 20 * DAY_MS
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "trusted"
            report_path = root / "multi-asset.md"
            write_generation_publication(
                data_dir,
                symbol="MU-USDT-SWAP",
                start_ms=now_ms - DAY_MS,
                end_ms=now_ms,
            )
            argv = [
                "mu_strategy.experiments.fibonacci_pullback",
                "--asset",
                "MU",
                "--days",
                "1",
                "--min-hour",
                "2",
                "--max-hour",
                "2",
                "--strategy",
                "baseline",
                "--data-dir",
                str(data_dir),
                "--multi-report",
                str(report_path),
            ]
            with _blocked_market_data_paths("mu_strategy.experiments.fibonacci_pullback"):
                with patch(
                    "mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms",
                    return_value=now_ms,
                ):
                    with patch("sys.argv", argv):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            fibonacci_pullback.main()

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("多标的 Fibonacci 回调 2h-2h 请求 1d 回测", report)
            self.assertIn("MU-USDT-SWAP / okx", report)

    def test_multi_asset_main_pins_one_generation_when_current_pointer_changes(self):
        from mu_strategy.experiments import fibonacci_pullback

        now_ms = DAY_MS
        symbols = ("MU-USDT-SWAP", "BTC-USDT-SWAP")
        all_symbols = (*symbols, "SPCX-USDT-SWAP")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "trusted"
            report_path = root / "multi-asset.md"
            for run_id in ("old-run", "new-run"):
                for index, symbol in enumerate(all_symbols, start=1):
                    write_generation_manifest_and_caches(
                        data_dir,
                        symbol=symbol,
                        days=1,
                        run_id=run_id,
                        universe_symbols=all_symbols[:index],
                    )
            store = TrustedDataStore(data_dir=data_dir)
            store.replace_current("old-run")
            contexts = []

            def load_then_advance_pointer(symbol, **kwargs):
                contexts.append(kwargs["context"])
                bundle = refresh_trusted_candle_bundle(symbol, **kwargs)
                if len(contexts) == 1:
                    store.replace_current("new-run")
                return bundle

            argv = [
                "mu_strategy.experiments.fibonacci_pullback",
                "--asset",
                "MU",
                "--asset",
                "BTC",
                "--days",
                "1",
                "--min-hour",
                "2",
                "--max-hour",
                "2",
                "--strategy",
                "baseline",
                "--data-dir",
                str(data_dir),
                "--multi-report",
                str(report_path),
            ]
            with _blocked_market_data_paths("mu_strategy.experiments.fibonacci_pullback"):
                with patch(
                    "mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms",
                    return_value=now_ms,
                ):
                    with patch(
                        "mu_strategy.experiments.fibonacci_pullback.refresh_trusted_candle_bundle",
                        side_effect=load_then_advance_pointer,
                    ):
                        with patch("sys.argv", argv):
                            with patch("sys.stdout", new_callable=io.StringIO):
                                fibonacci_pullback.main()

            report = report_path.read_text(encoding="utf-8")
            self.assertEqual(["old-run", "old-run"], [context.generation_id for context in contexts])
            self.assertIs(contexts[0], contexts[1])
            self.assertEqual(
                set(symbols),
                {snapshot.key.symbol for snapshot in contexts[0].dataset_file_snapshots},
            )
            self.assertIn(str(Path("generations") / "old-run"), report)
            self.assertNotIn(str(Path("generations") / "new-run"), report)

    def test_main_fails_closed_without_trusted_publication_and_writes_no_report(self):
        from mu_strategy.experiments import fibonacci_pullback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "fibonacci.md"
            argv = [
                "mu_strategy.experiments.fibonacci_pullback",
                "--days",
                "1",
                "--min-hour",
                "2",
                "--max-hour",
                "2",
                "--data-dir",
                str(root / "missing"),
                "--report",
                str(report_path),
            ]
            with patch("sys.argv", argv):
                with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as raised:
                        fibonacci_pullback.main()

            self.assertNotEqual(0, raised.exception.code)
            self.assertIn("trusted data blocked", stderr.getvalue())
            self.assertFalse(report_path.exists())

    def test_non_okx_asset_fails_closed_before_loading_or_writing_report(self):
        from mu_strategy.experiments import fibonacci_pullback

        with TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "multi-asset.md"
            argv = [
                "mu_strategy.experiments.fibonacci_pullback",
                "--asset",
                "ETHUSDT",
                "--days",
                "1",
                "--data-dir",
                str(Path(tmp) / "trusted"),
                "--multi-report",
                str(report_path),
            ]
            with patch(
                "mu_strategy.experiments.fibonacci_pullback.refresh_trusted_candle_bundle",
                side_effect=AssertionError("trusted load must not start for non-OKX source"),
            ):
                with patch("sys.argv", argv):
                    with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        with self.assertRaises(SystemExit) as raised:
                            fibonacci_pullback.main()

            self.assertNotEqual(0, raised.exception.code)
            self.assertIn("trusted data layer supports OKX sources only", stderr.getvalue())
            self.assertFalse(report_path.exists())

    def test_removed_legacy_cli_options_are_rejected(self):
        from mu_strategy.experiments import fibonacci_pullback

        for option in ("--refresh", "--source"):
            with self.subTest(option=option):
                argv = ["mu_strategy.experiments.fibonacci_pullback", option]
                if option == "--source":
                    argv.append("okx")
                with patch("sys.argv", argv):
                    with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        with self.assertRaises(SystemExit) as raised:
                            fibonacci_pullback.main()

                self.assertNotEqual(0, raised.exception.code)
                self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_asset_backtest_api_no_longer_accepts_refresh(self):
        self.assertNotIn("refresh", inspect.signature(run_asset_fibonacci_backtest).parameters)

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
