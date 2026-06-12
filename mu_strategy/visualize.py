from __future__ import annotations

import argparse
import html
import json
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.cli import build_hourly_context
from mu_strategy.data import cached_historical
from mu_strategy.models import BacktestResult, Candle, Trade
from mu_strategy.reporting import _format_float
from mu_strategy.strategy import StrategyConfig, selected_strategy_groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an HTML visualization for the MU strategy backtest.")
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--source", choices=("binance", "okx"), default="okx")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("reports/mu_backtest.html"))
    parser.add_argument("--chart-interval", choices=("15m", "1h"), default="1h")
    parser.add_argument("--strategy", default="baseline", help="Single strategy group name to visualize.")
    args = parser.parse_args()

    try:
        groups = selected_strategy_groups(args.symbol, [args.strategy])
    except ValueError as exc:
        parser.error(str(exc))
    if len(groups) != 1:
        parser.error("--strategy must resolve to exactly one strategy group")
    group = groups[0]
    config = group.config
    candles_15m, _ = cached_historical(
        args.symbol,
        "15m",
        days=args.days,
        data_dir=args.data_dir,
        refresh=args.refresh,
        source=args.source,
    )
    candles_1h, _ = cached_historical(
        args.symbol,
        "1h",
        days=args.days,
        data_dir=args.data_dir,
        refresh=args.refresh,
        source=args.source,
    )
    context = build_hourly_context(candles_15m, candles_1h)
    result = run_backtest(candles_15m, context, config=config)
    chart_candles = candles_1h if args.chart_interval == "1h" else candles_15m
    html_text = render_html_visualization(
        chart_candles,
        result,
        config=config,
        symbol=args.symbol,
        chart_interval=args.chart_interval,
        strategy_name=group.name,
        strategy_label=group.label,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_text, encoding="utf-8")
    print(f"Wrote {args.output.resolve()}")


def render_html_visualization(
    candles: list[Candle],
    result: BacktestResult,
    *,
    config: StrategyConfig,
    symbol: str,
    chart_interval: str = "15m",
    strategy_name: str | None = None,
    strategy_label: str | None = None,
) -> str:
    interval_label = "1h" if chart_interval == "1h" else "15m"
    strategy_title = f" {html.escape(strategy_name)}" if strategy_name else ""
    title = f"{html.escape(symbol)}{strategy_title} 策略可视化"
    strategy_note = ""
    if strategy_name:
        escaped_name = html.escape(strategy_name)
        escaped_label = html.escape(strategy_label or "")
        strategy_note = f"策略组：<strong>{escaped_name}</strong>"
        if escaped_label:
            strategy_note += f"（{escaped_label}）"
        strategy_note += "。"
    chart_data = _plotly_data(candles, result)
    rows = "\n".join(_trade_row(trade) for trade in result.trades) or (
        "<tr><td colspan=\"7\">当前规则没有产生交易。</td></tr>"
    )
    payload = json.dumps(chart_data, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --ink: #18212f;
      --muted: #657386;
      --grid: #dce3eb;
      --price: #1f6feb;
      --equity: #0f8b6f;
      --entry: #2563eb;
      --exit-win: #0f8b6f;
      --exit-loss: #d03535;
      --border: #d7dde5;
      --up: #0f8b6f;
      --down: #d03535;
    }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 28px;
    }}
    h1 {{
      margin: 0 0 4px;
      font-size: 26px;
    }}
    .subtitle {{
      color: var(--muted);
      margin-bottom: 20px;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .card, section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }}
    .card {{
      padding: 12px 14px;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .value {{
      font-size: 20px;
      font-weight: 650;
      margin-top: 4px;
    }}
    section {{
      padding: 16px;
      margin-top: 14px;
      overflow-x: auto;
    }}
    .chart {{
      width: 100%;
      min-width: 980px;
    }}
    #price-chart {{
      height: 560px;
    }}
    #volume-chart {{
      height: 190px;
    }}
    #equity-chart {{
      height: 320px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      padding: 8px 10px;
      text-align: right;
      white-space: nowrap;
    }}
    th:first-child, td:first-child,
    th:nth-child(2), td:nth-child(2),
    th:last-child, td:last-child {{
      text-align: left;
    }}
    .note {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
<main>
  <h1>{title}</h1>
  <div class="subtitle">{strategy_note}当前规则：1h 结构过滤、15m Fibonacci 回踩确认、5x 金字塔加仓、美股现金盘窗口。K线展示周期：{interval_label}。</div>
  <div class="cards">
    {_metric_card("总收益", f"{result.total_return_pct:.2%}")}
    {_metric_card("最大回撤", f"{result.max_drawdown_pct:.2%}")}
    {_metric_card("交易次数", str(result.trade_count))}
    {_metric_card("胜率", f"{result.win_rate:.2%}")}
    {_metric_card("盈亏因子", _format_float(result.profit_factor))}
  </div>
  <section>
    <h2>{interval_label} K线 + 开仓/加仓/平仓</h2>
    <div id="price-chart" class="chart"></div>
  </section>
  <section>
    <h2>成交量</h2>
    <div id="volume-chart" class="chart"></div>
  </section>
  <section>
    <h2>权益曲线</h2>
    <div id="equity-chart" class="chart"></div>
  </section>
  <section>
    <h2>交易明细</h2>
    <table>
      <thead>
        <tr><th>开仓 UTC</th><th>平仓 UTC</th><th>开仓均价</th><th>平仓价</th><th>最高段位</th><th>收益</th><th>原因</th></tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
  </section>
  <p class="note">研究用途。页面展示历史规则执行，不预测未来收益。价格图、成交量图和权益图支持同步缩放；鼠标悬停时三图会显示同一时间点的竖向虚线。</p>
</main>
<script>
const chartData = {payload};
const plotConfig = {{ responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"] }};
const baseLayout = {{
  paper_bgcolor: "#ffffff",
  plot_bgcolor: "#ffffff",
  margin: {{ l: 56, r: 24, t: 20, b: 42 }},
  font: {{ family: "Segoe UI, Arial, sans-serif", color: "#18212f" }},
  xaxis: {{ rangeslider: {{ visible: false }}, gridcolor: "#e6ebf1", showspikes: true, spikemode: "across" }},
  yaxis: {{ gridcolor: "#e6ebf1", zeroline: false }},
  legend: {{ orientation: "h", y: 1.08, x: 0 }},
  hovermode: "x unified"
}};

Plotly.newPlot(
  "price-chart",
  [chartData.candlestick, ...chartData.entryTraces, ...chartData.exitTraces],
  {{
    ...baseLayout,
    yaxis: {{ ...baseLayout.yaxis, title: "价格" }}
  }},
  plotConfig
);

Plotly.newPlot(
  "volume-chart",
  [chartData.volume],
  {{
    ...baseLayout,
    showlegend: false,
    yaxis: {{ ...baseLayout.yaxis, title: "成交量" }}
  }},
  plotConfig
);

Plotly.newPlot(
  "equity-chart",
  [chartData.equity],
  {{
    ...baseLayout,
    showlegend: false,
    yaxis: {{ ...baseLayout.yaxis, title: "权益" }}
  }},
  plotConfig
);

let syncingZoom = false;
function linkXAxis(sourceId, targetIds) {{
  const source = document.getElementById(sourceId);
  source.on("plotly_relayout", eventData => {{
    if (syncingZoom) return;
    const hasRange = eventData["xaxis.range[0]"] && eventData["xaxis.range[1]"];
    const hasAutoRange = eventData["xaxis.autorange"];
    if (!hasRange && !hasAutoRange) return;
    syncingZoom = true;
    const update = hasRange
      ? {{ "xaxis.range": [eventData["xaxis.range[0]"], eventData["xaxis.range[1]"]] }}
      : {{ "xaxis.autorange": true }};
    Promise.all(targetIds.map(id => Plotly.relayout(id, update))).finally(() => {{
      syncingZoom = false;
    }});
  }});
}}

linkXAxis("price-chart", ["volume-chart", "equity-chart"]);
linkXAxis("volume-chart", ["price-chart", "equity-chart"]);
linkXAxis("equity-chart", ["price-chart", "volume-chart"]);

function hoverLineShape(xValue) {{
  return {{
    type: "line",
    xref: "x",
    yref: "paper",
    x0: xValue,
    x1: xValue,
    y0: 0,
    y1: 1,
    line: {{ color: "#475569", width: 1, dash: "dot" }}
  }};
}}

function syncHoverLine(sourceId, targetIds) {{
  const source = document.getElementById(sourceId);
  const chartIds = [sourceId, ...targetIds];
  source.on("plotly_hover", eventData => {{
    const point = eventData.points && eventData.points[0];
    if (!point || !point.x) return;
    const update = {{ shapes: [hoverLineShape(point.x)] }};
    chartIds.forEach(id => Plotly.relayout(id, update));
  }});
  source.on("plotly_unhover", () => {{
    chartIds.forEach(id => Plotly.relayout(id, {{ shapes: [] }}));
  }});
}}

syncHoverLine("price-chart", ["volume-chart", "equity-chart"]);
syncHoverLine("volume-chart", ["price-chart", "equity-chart"]);
syncHoverLine("equity-chart", ["price-chart", "volume-chart"]);
</script>
</body>
</html>
"""


def _metric_card(label: str, value: str) -> str:
    return f"<div class=\"card\"><div class=\"label\">{html.escape(label)}</div><div class=\"value\">{html.escape(value)}</div></div>"


def _plotly_data(candles: list[Candle], result: BacktestResult) -> dict:
    times = [bar.open_time_iso for bar in candles]
    opens = [round(bar.open, 6) for bar in candles]
    highs = [round(bar.high, 6) for bar in candles]
    lows = [round(bar.low, 6) for bar in candles]
    closes = [round(bar.close, 6) for bar in candles]
    volumes = [round(bar.volume, 6) for bar in candles]
    volume_colors = ["#0f8b6f" if bar.close >= bar.open else "#d03535" for bar in candles]
    return {
        "candlestick": {
            "type": "candlestick",
            "name": "K线",
            "x": times,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "increasing": {"line": {"color": "#0f8b6f"}, "fillcolor": "#0f8b6f"},
            "decreasing": {"line": {"color": "#d03535"}, "fillcolor": "#d03535"},
        },
        "volume": {
            "type": "bar",
            "name": "成交量",
            "x": times,
            "y": volumes,
            "marker": {"color": volume_colors, "opacity": 0.64},
        },
        "entryTraces": _entry_traces(result.trades),
        "exitTraces": _exit_traces(result.trades),
        "equity": {
            "type": "scatter",
            "mode": "lines",
            "name": "权益曲线",
            "x": [_fmt_iso(time_ms) for time_ms, _ in result.equity_curve],
            "y": [round(equity, 6) for _, equity in result.equity_curve],
            "line": {"color": "#0f8b6f", "width": 2},
            "hovertemplate": "%{x}<br>权益 %{y:.2f}<extra></extra>",
        },
    }


def _entry_traces(trades: list[Trade]) -> list[dict]:
    traces: list[dict] = []
    for stage in range(1, 5):
        fills = [(trade, fill) for trade in trades for fill_index, fill in enumerate(trade.fills, start=1) if fill_index == stage]
        if not fills:
            continue
        traces.append(
            {
                "type": "scatter",
                "mode": "markers",
                "name": f"开仓/加仓 第{stage}段",
                "x": [_fmt_iso(fill.time_ms) for _, fill in fills],
                "y": [round(fill.price, 6) for _, fill in fills],
                "text": [
                    f"第{stage}段开仓/加仓<br>价格 {fill.price:.2f}<br>保证金 {fill.margin_fraction:.0%}"
                    for _, fill in fills
                ],
                "hovertemplate": "%{text}<extra></extra>",
                "marker": {
                    "symbol": "triangle-up",
                    "size": 11 if stage == 1 else 9,
                    "color": "#2563eb",
                    "line": {"color": "#ffffff", "width": 1},
                },
            }
        )
    return traces


def _exit_traces(trades: list[Trade]) -> list[dict]:
    output: list[dict] = []
    for label, color, selected in (
        ("平仓 盈利", "#0f8b6f", [trade for trade in trades if trade.pnl >= 0]),
        ("平仓 亏损", "#d03535", [trade for trade in trades if trade.pnl < 0]),
    ):
        if not selected:
            continue
        output.append(
            {
                "type": "scatter",
                "mode": "markers",
                "name": label,
                "x": [_fmt_iso(trade.exit_time_ms) for trade in selected],
                "y": [round(trade.exit_price, 6) for trade in selected],
                "text": [
                    f"平仓<br>价格 {trade.exit_price:.2f}<br>收益 {trade.return_pct:.2%}<br>{_translate_reason(trade.exit_reason)}"
                    for trade in selected
                ],
                "hovertemplate": "%{text}<extra></extra>",
                "marker": {
                    "symbol": "x",
                    "size": 11,
                    "color": color,
                    "line": {"color": "#ffffff", "width": 1},
                },
            }
        )
    return output
    

def _fmt_iso(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).isoformat()


def _render_price_chart(candles: list[Candle], trades: list[Trade]) -> str:
    width, height, pad = 1200, 430, 48
    if not candles:
        return f"<svg id=\"price-chart\" viewBox=\"0 0 {width} {height}\"><text x=\"{pad}\" y=\"{pad}\">No price data.</text></svg>"
    prices = [bar.close for bar in candles]
    low = min(bar.low for bar in candles)
    high = max(bar.high for bar in candles)
    x = _x_scale(len(candles), width, pad)
    y = _y_scale(low, high, height, pad)
    path = _line_path([(x(index), y(value)) for index, value in enumerate(prices)])
    markers = _price_markers(candles, trades, x, y)
    grid = _grid(width, height, pad)
    labels = _axis_labels(low, high, width, height, pad)
    return f"""<svg id="price-chart" viewBox="0 0 {width} {height}" role="img" aria-label="Price chart">
  {grid}
  <path class="price-line" d="{path}"/>
  {markers}
  {labels}
</svg>"""


def _render_equity_chart(candles: list[Candle], equity_curve: list[tuple[int, float]]) -> str:
    width, height, pad = 1200, 260, 48
    if not equity_curve:
        return f"<svg id=\"equity-chart\" viewBox=\"0 0 {width} {height}\"><text x=\"{pad}\" y=\"{pad}\">No equity data.</text></svg>"
    values = [equity for _, equity in equity_curve]
    low = min(values)
    high = max(values)
    if low == high:
        low *= 0.99
        high *= 1.01
    candle_times = [bar.open_time_ms for bar in candles]
    x = _x_scale(max(len(candles), 1), width, pad)
    y = _y_scale(low, high, height, pad)
    points = [(x(_nearest_index(candle_times, time_ms)), y(equity)) for time_ms, equity in equity_curve]
    path = _line_path(points)
    grid = _grid(width, height, pad)
    labels = _axis_labels(low, high, width, height, pad)
    return f"""<svg id="equity-chart" viewBox="0 0 {width} {height}" role="img" aria-label="Equity curve">
  {grid}
  <path class="equity-line" d="{path}"/>
  {labels}
</svg>"""


def _price_markers(candles: list[Candle], trades: list[Trade], x_scale, y_scale) -> str:
    times = [bar.open_time_ms for bar in candles]
    output: list[str] = []
    for trade in trades:
        for stage, fill in enumerate(trade.fills, start=1):
            x = x_scale(_nearest_index(times, fill.time_ms))
            y = y_scale(fill.price)
            output.append(f"<circle class=\"trade-entry\" cx=\"{x:.2f}\" cy=\"{y:.2f}\" r=\"5\"><title>Stage {stage} entry {fill.price:.2f}</title></circle>")
        exit_x = x_scale(_nearest_index(times, trade.exit_time_ms))
        exit_y = y_scale(trade.exit_price)
        cls = "win" if trade.pnl >= 0 else "loss"
        output.append(
            f"<rect class=\"trade-exit {cls}\" x=\"{exit_x - 5:.2f}\" y=\"{exit_y - 5:.2f}\" width=\"10\" height=\"10\" transform=\"rotate(45 {exit_x:.2f} {exit_y:.2f})\"><title>Exit {trade.exit_price:.2f}, {trade.return_pct:.2%}</title></rect>"
        )
    return "\n  ".join(output)


def _trade_row(trade: Trade) -> str:
    return (
        "<tr>"
        f"<td>{html.escape(_fmt_time(trade.entry_time_ms))}</td>"
        f"<td>{html.escape(_fmt_time(trade.exit_time_ms))}</td>"
        f"<td>{trade.entry_price:.2f}</td>"
        f"<td>{trade.exit_price:.2f}</td>"
        f"<td>{trade.max_stage}</td>"
        f"<td>{trade.return_pct:.2%}</td>"
        f"<td>{html.escape(_translate_reason(trade.exit_reason))}</td>"
        "</tr>"
    )


def _x_scale(count: int, width: int, pad: int):
    usable = width - (pad * 2)
    denominator = max(count - 1, 1)
    return lambda index: pad + (usable * index / denominator)


def _y_scale(low: float, high: float, height: int, pad: int):
    if high == low:
        high = low * 1.01
        low = low * 0.99
    span = high - low
    usable = height - (pad * 2)
    return lambda value: pad + (high - value) * usable / span


def _line_path(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    first_x, first_y = points[0]
    parts = [f"M {first_x:.2f} {first_y:.2f}"]
    parts.extend(f"L {x:.2f} {y:.2f}" for x, y in points[1:])
    return " ".join(parts)


def _grid(width: int, height: int, pad: int) -> str:
    lines = []
    for step in range(5):
        y = pad + ((height - pad * 2) * step / 4)
        lines.append(f"<line class=\"grid\" x1=\"{pad}\" x2=\"{width - pad}\" y1=\"{y:.2f}\" y2=\"{y:.2f}\"/>")
    lines.append(f"<line class=\"axis\" x1=\"{pad}\" x2=\"{width - pad}\" y1=\"{height - pad}\" y2=\"{height - pad}\"/>")
    lines.append(f"<line class=\"axis\" x1=\"{pad}\" x2=\"{pad}\" y1=\"{pad}\" y2=\"{height - pad}\"/>")
    return "\n  ".join(lines)


def _axis_labels(low: float, high: float, width: int, height: int, pad: int) -> str:
    return (
        f"<text x=\"{width - pad + 6}\" y=\"{pad + 4}\">{high:.2f}</text>"
        f"<text x=\"{width - pad + 6}\" y=\"{height - pad + 4}\">{low:.2f}</text>"
    )


def _nearest_index(times: list[int], target: int) -> int:
    if not times:
        return 0
    index = bisect_left(times, target)
    if index <= 0:
        return 0
    if index >= len(times):
        return len(times) - 1
    before = times[index - 1]
    after = times[index]
    return index if abs(after - target) < abs(target - before) else index - 1


def _fmt_time(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _translate_reason(reason: str) -> str:
    return {
        "initial_stop": "首仓止损",
        "stop": "止损",
        "end_of_data": "数据结束",
    }.get(reason, reason)


if __name__ == "__main__":
    main()
