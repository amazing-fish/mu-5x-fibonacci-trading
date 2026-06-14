import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
        self.assertIn("策略组详情", html)
        self.assertIn("入场策略", html)
        self.assertIn("二次回踩限价", html)
        self.assertIn("entry_execution", html)
        self.assertIn("trading_windows_et", html)

    def test_visualize_cli_defaults_to_okx_mu_source(self):
        from mu_strategy import visualize

        cached_calls = []

        def fake_cached_historical(symbol, interval, **kwargs):
            cached_calls.append((symbol, interval, kwargs))
            return [], Path(f"data/{symbol}_{interval}.csv")

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "chart.html"
            argv = ["mu_strategy.visualize", "--days", "180", "--strategy", "baseline", "--output", str(output_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.viz.backtest.cached_historical", side_effect=fake_cached_historical):
                    with patch("mu_strategy.viz.backtest.run_backtest", return_value=BacktestResult(10_000, 10_000, [], [])):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            visualize.main()

        self.assertEqual("MU-USDT-SWAP", cached_calls[0][0])
        self.assertEqual("okx", cached_calls[0][2]["source"])


if __name__ == "__main__":
    unittest.main()
