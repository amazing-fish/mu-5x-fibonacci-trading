import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mu_strategy.market_data.trusted import DataStatus
from mu_strategy.models import BacktestResult, Candle, Fill, Trade
from mu_strategy.strategies.components import StrategyComponents
from mu_strategy.strategy import StrategyConfig
from mu_strategy.viz.backtest import render_html_visualization


class VisualizationTests(unittest.TestCase):
    def test_html_visualization_contains_charts_and_trade_markers(self):
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100, 103, 100, 102, 1000),
            Candle(1_800_000, 102, 104, 101, 103, 1000),
        ]
        fill = Fill(900_000, 101, 0.2, 10_000, 99.0, 5.0)
        trade = Trade(
            entry_time_ms=900_000,
            exit_time_ms=1_800_000,
            entry_price=101,
            exit_price=103,
            fills=[fill],
            pnl=198,
            fees=6,
            return_pct=0.0198,
            max_stage=1,
            exit_reason="non_session_liquidation_risk",
        )
        result = BacktestResult(10_000, 10_198, [trade], [(0, 10_000), (1_800_000, 10_198)])

        html = render_html_visualization(
            candles,
            result,
            config=StrategyConfig(),
            symbol="MUUSDT",
            chart_interval="1h",
            strategy_name="baseline",
            strategy_label="新baseline：二次回踩确认买入",
            strategy_components=StrategyComponents(
                entry="二次回踩限价",
                position="5x 金字塔 20/20/20/40",
                exit="baseline 抬止损",
                filters=("1h regime", "15m RSI/MACD", "美股现金盘窗口"),
            ),
        )

        self.assertIn("id=\"price-chart\"", html)
        self.assertIn("id=\"volume-chart\"", html)
        self.assertIn("id=\"equity-chart\"", html)
        self.assertIn("<html lang=\"zh-CN\">", html)
        self.assertIn("https://cdn.plot.ly/", html)
        self.assertIn('"type": "candlestick"', html)
        self.assertIn('"open": [', html)
        self.assertIn('"high": [', html)
        self.assertIn('"low": [', html)
        self.assertIn('"close": [', html)
        self.assertIn('"name": "成交量"', html)
        self.assertIn("1h K线", html)
        self.assertIn("策略可视化", html)
        self.assertIn("baseline", html)
        self.assertIn("新baseline：二次回踩确认买入", html)
        self.assertIn("同步缩放", html)
        self.assertIn("linkXAxis", html)
        self.assertIn("extractXAxisUpdate", html)
        self.assertIn('"fullXRange": [', html)
        self.assertIn("resetLinkedCharts", html)
        self.assertIn("plotly_doubleclick", html)
        self.assertIn("yaxis.autorange", html)
        self.assertIn("requestAnimationFrame", html)
        self.assertIn("xaxis.range", html)
        self.assertLess(
            html.index("if (isXAxisReset(eventData))"),
            html.index("const update = extractXAxisUpdate(eventData)"),
        )
        self.assertIn("竖向虚线", html)
        self.assertIn("syncHoverLine", html)
        self.assertIn("plotly_hover", html)
        self.assertIn('dash: "dot"', html)
        self.assertIn("开仓/加仓 第1段", html)
        self.assertIn("平仓 盈利", html)
        self.assertIn("非美股时段爆仓风险", html)
        self.assertIn("非美股风险统计", html)
        self.assertIn("样本时长", html)
        self.assertIn("45m", html)
        self.assertIn("止损和非美股时段爆仓风险检查会在持仓期间持续执行", html)
        self.assertIn("策略组详情", html)
        self.assertIn("入场策略", html)
        self.assertIn("二次回踩限价", html)
        self.assertIn("entry_execution", html)
        self.assertIn("fee_profile", html)
        self.assertIn("market/taker (市价/吃单)", html)
        self.assertIn("0.0500%", html)
        self.assertIn("trading_windows_et", html)

    def test_strategy_detail_exposes_selected_group_and_module_composition(self):
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100, 103, 100, 102, 1000),
        ]
        result = BacktestResult(10_000, 10_000, [], [(0, 10_000), (900_000, 10_000)])

        html = render_html_visualization(
            candles,
            result,
            config=StrategyConfig(
                symbol="MU-USDT-SWAP",
                entry_execution="second_pullback",
                second_pullback_wait_bars=8,
                fib_lookback=8,
            ),
            symbol="MU-USDT-SWAP",
            chart_interval="1h",
            strategy_name="baseline",
            strategy_label="新baseline：二次回踩确认买入",
            strategy_components=StrategyComponents(entry="二次回踩限价"),
        )

        self.assertIn("已选策略组", html)
        self.assertIn("<th>strategy_group</th><td>baseline</td>", html)
        self.assertIn("<th>strategy_label</th><td>新baseline：二次回踩确认买入</td>", html)
        self.assertIn("策略模块组合", html)
        self.assertIn("<td>入场信号</td>", html)
        self.assertIn("<td>二次回踩限价</td>", html)
        self.assertIn("<td>入场执行</td>", html)
        self.assertIn("second_pullback", html)
        self.assertIn("等待 8 根 15m K", html)
        self.assertIn("<td>Fibonacci 窗口</td>", html)
        self.assertIn("2h / 8 根 15m K", html)
        self.assertIn("<td>成本模型</td>", html)
        self.assertIn("market/taker (市价/吃单)", html)
        self.assertIn("fib_lookback", html)

    def test_trade_table_reverses_scrolls_and_hides_stop_only_reason(self):
        candles = [
            Candle(0, 100, 101, 99, 100, 1000),
            Candle(900_000, 100, 103, 100, 102, 1000),
            Candle(1_800_000, 102, 104, 101, 103, 1000),
            Candle(2_700_000, 103, 105, 102, 104, 1000),
        ]
        older_positive = Trade(
            entry_time_ms=900_000,
            exit_time_ms=1_800_000,
            entry_price=101,
            exit_price=103,
            fills=[Fill(900_000, 101, 0.2, 10_000, 99.0, 5.0)],
            pnl=198,
            fees=6,
            return_pct=0.0198,
            max_stage=1,
            exit_reason="stop",
        )
        newer_negative = Trade(
            entry_time_ms=1_800_000,
            exit_time_ms=2_700_000,
            entry_price=103,
            exit_price=101,
            fills=[Fill(1_800_000, 103, 0.2, 10_000, 97.0, 5.0)],
            pnl=-200,
            fees=6,
            return_pct=-0.0200,
            max_stage=1,
            exit_reason="stop",
        )
        result = BacktestResult(
            10_000,
            9_998,
            [older_positive, newer_negative],
            [(0, 10_000), (1_800_000, 10_198), (2_700_000, 9_998)],
        )

        html = render_html_visualization(
            candles,
            result,
            config=StrategyConfig(),
            symbol="MUUSDT",
            chart_interval="15m",
        )

        self.assertIn("trade-table-wrap", html)
        self.assertIn("--trade-row-height: 34px", html)
        self.assertIn("var(--trade-row-height) * 15", html)
        self.assertIn("overflow-y: auto", html)
        self.assertNotIn("<th>原因</th>", html)
        self.assertNotIn("<td>止损</td>", html)
        self.assertIn('class="trade-positive"', html)
        self.assertLess(
            html.index("1970-01-01 00:30</td><td>1970-01-01 00:45"),
            html.index("1970-01-01 00:15</td><td>1970-01-01 00:30"),
        )

    def test_visualize_cli_defaults_to_okx_mu_source(self):
        from mu_strategy import visualize

        bundle_calls = []
        configs = []

        def fake_refresh_candle_bundle(symbol, **kwargs):
            bundle_calls.append((symbol, kwargs))
            return SimpleNamespace(
                candles_by_interval={"15m": [], "1h": []},
                files_by_interval={"15m": Path("data/15m.csv"), "1h": Path("data/1h.csv")},
            )

        def fake_run_backtest(candles_15m, context, *, config):
            configs.append(config)
            return BacktestResult(10_000, 10_000, [], [])

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "chart.html"
            argv = ["mu_strategy.visualize", "--days", "180", "--strategy", "baseline", "--output", str(output_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.refresh_candle_bundle", side_effect=fake_refresh_candle_bundle):
                    with patch("mu_strategy.viz.backtest.cached_historical", side_effect=AssertionError("visualize must use market_data.service"), create=True):
                        with patch("mu_strategy.viz.backtest.run_backtest", side_effect=fake_run_backtest):
                            with patch("sys.stdout", new_callable=io.StringIO):
                                visualize.main()

        self.assertEqual("MU-USDT-SWAP", bundle_calls[0][0])
        self.assertEqual(("15m", "1h"), bundle_calls[0][1]["intervals"])
        self.assertEqual("okx", bundle_calls[0][1]["source"])
        self.assertEqual(180, bundle_calls[0][1]["days"])
        self.assertFalse(bundle_calls[0][1]["refresh"])
        self.assertEqual("market", configs[0].fee_profile)
        self.assertAlmostEqual(0.0005, configs[0].fee_rate)

    def test_visualize_trusted_data_consumes_cache_without_refresh_by_default(self):
        from mu_strategy import visualize

        trusted_calls = []

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            trusted_calls.append((symbol, kwargs))
            return SimpleNamespace(
                candles_by_interval={"15m": [_candle(0, 100)], "1h": [_candle(0, 100)]},
                statuses_by_interval={
                    "5m": _status("MU-USDT-SWAP", "5m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/5m.csv")),
                    "15m": _status("MU-USDT-SWAP", "15m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/15m.csv")),
                    "1h": _status("MU-USDT-SWAP", "1h", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/1h.csv")),
                },
            )

        def fake_run_backtest(candles, context, *, config):
            return BacktestResult(10_000, 10_000, [], [(0, 10_000)])

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "chart.html"
            argv = ["mu_strategy.visualize", "--trusted-data", "--output", str(output_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle):
                    with patch("mu_strategy.viz.backtest.run_backtest", side_effect=fake_run_backtest):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            visualize.main()

        self.assertFalse(trusted_calls[0][1]["refresh"])

    def test_visualize_rejects_trusted_refresh_before_rendering_or_writing_output(self):
        from mu_strategy import visualize

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            output_path = Path(tmp) / "chart.html"
            manifest_path = data_dir / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("canonical manifest", encoding="utf-8")
            manifest_before = manifest_path.read_bytes()
            argv = [
                "mu_strategy.visualize",
                "--trusted-data",
                "--refresh",
                "--data-dir",
                str(data_dir),
                "--output",
                str(output_path),
            ]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.refresh_trusted_candle_bundle", side_effect=AssertionError("trusted loader")):
                    with patch("mu_strategy.viz.backtest.run_backtest", side_effect=AssertionError("backtest")):
                        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                            with self.assertRaises(SystemExit) as raised:
                                visualize.main()

            self.assertNotEqual(0, raised.exception.code)
            self.assertIn("refresh_market_data", stderr.getvalue())
            self.assertFalse(output_path.exists())
            self.assertEqual(manifest_before, manifest_path.read_bytes())

    def test_visualize_cli_can_use_trusted_data_layer(self):
        from mu_strategy import visualize

        trusted_calls = []
        candles_15m = [_candle(0, 100), _candle(900_000, 101)]
        candles_1h = [_candle(0, 100)]

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            trusted_calls.append((symbol, kwargs))
            return SimpleNamespace(
                candles_by_interval={"15m": candles_15m, "1h": candles_1h},
                statuses_by_interval={
                    "5m": _status("MU-USDT-SWAP", "5m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/5m.csv")),
                    "15m": _status("MU-USDT-SWAP", "15m", rows=len(candles_15m), path=Path("data/live/okx/MU-USDT-SWAP/15m.csv")),
                    "1h": _status("MU-USDT-SWAP", "1h", rows=len(candles_1h), path=Path("data/live/okx/MU-USDT-SWAP/1h.csv")),
                },
            )

        def fake_run_backtest(candles, context, *, config):
            return BacktestResult(10_000, 10_000, [], [(0, 10_000)])

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "chart.html"
            argv = [
                "mu_strategy.visualize",
                "--trusted-data",
                "--days",
                "7",
                "--data-dir",
                str(Path(tmp) / "live"),
                "--output",
                str(output_path),
            ]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle, create=True):
                    with patch("mu_strategy.viz.backtest.cached_historical", side_effect=AssertionError("legacy cache must not be used"), create=True):
                        with patch("mu_strategy.viz.backtest.run_backtest", side_effect=fake_run_backtest):
                            with patch("sys.stdout", new_callable=io.StringIO):
                                try:
                                    visualize.main()
                                except SystemExit as exc:
                                    self.fail(f"--trusted-data should run without parser exit: {exc}")

            html = output_path.read_text(encoding="utf-8")

        self.assertEqual("MU-USDT-SWAP", trusted_calls[0][0])
        self.assertEqual(("5m", "15m", "1h"), trusted_calls[0][1]["intervals"])
        self.assertEqual(7, trusted_calls[0][1]["days"])
        self.assertIn("trusted OKX data layer", html)

    def test_visualize_cli_defaults_trusted_data_dir_to_live_store(self):
        from mu_strategy import visualize

        trusted_calls = []

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            trusted_calls.append((symbol, kwargs))
            return SimpleNamespace(
                candles_by_interval={"15m": [_candle(0, 100)], "1h": [_candle(0, 100)]},
                statuses_by_interval={
                    "5m": _status("MU-USDT-SWAP", "5m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/5m.csv")),
                    "15m": _status("MU-USDT-SWAP", "15m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/15m.csv")),
                    "1h": _status("MU-USDT-SWAP", "1h", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/1h.csv")),
                },
            )

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "chart.html"
            argv = ["mu_strategy.visualize", "--trusted-data", "--output", str(output_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle):
                    with patch("mu_strategy.viz.backtest.run_backtest", return_value=BacktestResult(10_000, 10_000, [], [(0, 10_000)])):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            visualize.main()

        self.assertEqual(Path("data/live"), trusted_calls[0][1]["data_dir"])

    def test_visualize_cli_rejects_invalid_trusted_status(self):
        from mu_strategy import visualize

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            return SimpleNamespace(
                candles_by_interval={"15m": [_candle(0, 100)], "1h": [_candle(0, 100)]},
                statuses_by_interval={
                    "5m": _status("MU-USDT-SWAP", "5m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/5m.csv")),
                    "15m": _status(
                        "MU-USDT-SWAP",
                        "15m",
                        rows=1,
                        path=Path("data/live/okx/MU-USDT-SWAP/15m.csv"),
                        is_valid=False,
                        reason="missing_in_built",
                    )
                },
            )

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "chart.html"
            argv = ["mu_strategy.visualize", "--trusted-data", "--output", str(output_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle, create=True):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            visualize.main()

            self.assertFalse(output_path.exists())

        self.assertNotEqual(0, raised.exception.code)

    def test_visualize_cli_rejects_invalid_base_5m_status(self):
        from mu_strategy import visualize

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            return SimpleNamespace(
                candles_by_interval={"15m": [_candle(0, 100)], "1h": [_candle(0, 100)]},
                statuses_by_interval={
                    "5m": _status(
                        "MU-USDT-SWAP",
                        "5m",
                        rows=0,
                        path=Path("data/live/okx/MU-USDT-SWAP/5m.csv"),
                        is_valid=False,
                        reason="cache_read_failed",
                    ),
                    "15m": _status("MU-USDT-SWAP", "15m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/15m.csv")),
                    "1h": _status("MU-USDT-SWAP", "1h", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/1h.csv")),
                },
            )

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "chart.html"
            argv = ["mu_strategy.visualize", "--trusted-data", "--output", str(output_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            visualize.main()

            self.assertFalse(output_path.exists())

        self.assertNotEqual(0, raised.exception.code)

    def test_visualize_cli_uses_trust_decision_gate(self):
        from mu_strategy import visualize
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, TrustDecision

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            return SimpleNamespace(
                candles_by_interval={"15m": [_candle(0, 100)], "1h": [_candle(0, 100)]},
                statuses_by_interval={
                    "5m": _status("MU-USDT-SWAP", "5m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/5m.csv")),
                    "15m": _status("MU-USDT-SWAP", "15m", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/15m.csv")),
                    "1h": _status("MU-USDT-SWAP", "1h", rows=1, path=Path("data/live/okx/MU-USDT-SWAP/1h.csv")),
                },
                trust_decision=TrustDecision(False, HealthReason.MALFORMED_MANIFEST),
            )

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "chart.html"
            argv = ["mu_strategy.visualize", "--trusted-data", "--output", str(output_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            visualize.main()

            self.assertFalse(output_path.exists())

        self.assertNotEqual(0, raised.exception.code)

    def test_visualize_trusted_data_rejects_failed_manifest_with_valid_csv(self):
        from mu_strategy import visualize

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            output_path = Path(tmp) / "chart.html"
            _write_failed_manifest_with_valid_csv(data_dir)
            argv = [
                "mu_strategy.visualize",
                "--trusted-data",
                "--data-dir",
                str(data_dir),
                "--output",
                str(output_path),
            ]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.run_backtest", return_value=BacktestResult(10_000, 10_000, [], [(0, 10_000)])):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            visualize.main()

            self.assertFalse(output_path.exists())

        self.assertNotEqual(0, raised.exception.code)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


def _status(
    symbol: str,
    interval: str,
    *,
    rows: int,
    path: Path,
    is_valid: bool = True,
    is_stale: bool = False,
    reason: str = "ok",
) -> DataStatus:
    return DataStatus(
        symbol=symbol,
        interval=interval,
        rows=rows,
        first_timestamp_ms=0 if rows else None,
        last_timestamp_ms=0 if rows else None,
        updated_at_ms=0,
        source_file=path,
        is_valid=is_valid,
        is_stale=is_stale,
        reason=reason,
    )


def _write_failed_manifest_with_valid_csv(data_dir: Path) -> None:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles
    from mu_strategy.market_data.utils import DAY_MS

    symbol = "MU-USDT-SWAP"
    store = TrustedDataStore(data_dir=data_dir)
    five = [_candle(index * 300_000, 100 + index) for index in range(DAY_MS // 300_000)]
    by_interval = {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }
    symbols = {symbol: {"intervals": {}}}
    for interval, candles in by_interval.items():
        path = store.cache_path(symbol, interval)
        store.write_csv(candles, path)
        symbols[symbol]["intervals"][interval] = {
            "symbol": symbol,
            "interval": interval,
            "availability": "available",
            "integrity": "valid",
            "freshness": "fresh",
            "reasons": ["ok"],
            "rows": len(candles),
            "first_timestamp_ms": candles[0].open_time_ms,
            "last_timestamp_ms": candles[-1].open_time_ms,
            "updated_at_ms": 86_400_000,
            "source_file": str(path),
            "content_sha256": candles_content_sha256(candles),
            "validation": {"ok": True, "reason": "ok"},
        }
    store.write_manifest(
        {
            "schema_version": 2,
            "run_id": "failed-run",
            "outcome": "failed",
            "status": "invalid",
            "started_at_ms": 0,
            "completed_at_ms": 86_400_000,
            "requested_intervals": ["5m", "15m", "1h"],
            "effective_intervals": ["5m", "15m", "1h"],
            "universes": {"crypto_top": [], "stock_token_top": []},
            "symbols": symbols,
            "warnings": [],
            "cycle_error": {"error_type": "TimeoutError", "message": "blocked"},
        }
    )


if __name__ == "__main__":
    unittest.main()
