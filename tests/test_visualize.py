import unittest

from mu_strategy.models import BacktestResult, Candle, Fill, Trade
from mu_strategy.strategy import StrategyConfig
from mu_strategy.visualize import render_html_visualization


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
            exit_reason="stop",
        )
        result = BacktestResult(10_000, 10_198, [trade], [(0, 10_000), (1_800_000, 10_198)])

        html = render_html_visualization(candles, result, config=StrategyConfig(), symbol="MUUSDT", chart_interval="1h")

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
        self.assertIn("同步缩放", html)
        self.assertIn("linkXAxis", html)
        self.assertIn("竖向虚线", html)
        self.assertIn("syncHoverLine", html)
        self.assertIn("plotly_hover", html)
        self.assertIn('dash: "dot"', html)
        self.assertIn("开仓/加仓 第1段", html)
        self.assertIn("平仓 盈利", html)
        self.assertIn("止损", html)


if __name__ == "__main__":
    unittest.main()
