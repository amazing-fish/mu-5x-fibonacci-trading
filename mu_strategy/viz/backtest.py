from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.cli import build_hourly_context
from mu_strategy.data import cached_historical
from mu_strategy.models import BacktestResult, Candle, Trade
from mu_strategy.reporting import _format_float
from mu_strategy.strategy import FEE_PROFILE_CHOICES, StrategyConfig, fee_profile_label, with_fee_profile
from mu_strategy.strategies.components import StrategyComponents
from mu_strategy.strategies.registry import selected_strategy_groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an HTML visualization for the MU strategy backtest.")
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--source", choices=("binance", "okx"), default="okx")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("reports/mu_okx_baseline_backtest.html"))
    parser.add_argument("--chart-interval", choices=("15m", "1h"), default="1h")
    parser.add_argument("--strategy", default="baseline", help="Single strategy group name to visualize.")
    parser.add_argument(
        "--fee-profile",
        choices=FEE_PROFILE_CHOICES,
        default="market",
        help="Backtest cost assumption: market/taker=0.0500%%, limit/maker=0.0200%%.",
    )
    args = parser.parse_args()

    try:
        groups = selected_strategy_groups(args.symbol, [args.strategy])
    except ValueError as exc:
        parser.error(str(exc))
    if len(groups) != 1:
        parser.error("--strategy must resolve to exactly one strategy group")
    group = groups[0]
    config = with_fee_profile(group.config, args.fee_profile)
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
        strategy_components=group.components,
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
    strategy_components: StrategyComponents | None = None,
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
    trade_table = _trade_table(result.trades)
    detail_section = _strategy_detail_section(config, strategy_components)
    duration_label = _coverage_duration_label(candles)
    risk_event_count = sum(1 for trade in result.trades if trade.exit_reason == "non_session_liquidation_risk")
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
      grid-template-columns: repeat(6, minmax(0, 1fr));
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
    .card-detail {{
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }}
    .detail-cell {{
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px 12px;
      background: #fbfcfe;
    }}
    .detail-cell strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 12px;
      color: var(--muted);
    }}
    .config-table {{
      width: 100%;
      margin-top: 12px;
    }}
    .config-table th, .config-table td {{
      text-align: left;
      white-space: normal;
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
    .trade-table-wrap {{
      --trade-row-height: 34px;
      max-height: calc((var(--trade-row-height) * 15) + 40px);
      overflow-y: auto;
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 6px;
    }}
    .trade-table-wrap table {{
      min-width: 760px;
    }}
    .trade-table-wrap tr {{
      height: var(--trade-row-height);
    }}
    .trade-table-wrap thead th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: #eef2f6;
    }}
    .trade-positive td {{
      background: rgba(15, 139, 111, 0.08);
    }}
    .trade-return-positive {{
      color: var(--up);
      font-weight: 650;
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
    {_metric_card("交易次数", str(result.trade_count), f"样本时长 {duration_label}")}
    {_metric_card("胜率", f"{result.win_rate:.2%}")}
    {_metric_card("盈亏因子", _format_float(result.profit_factor))}
    {_metric_card("非美股风险", str(risk_event_count))}
  </div>
  <p class="note">非美股风险统计：持仓期间若非美股现金时段 K 线低点跌穿按杠杆估算的爆仓线，会以“非美股时段爆仓风险”记录。</p>
  <p class="note">出场执行说明：入场和加仓只在配置的美股现金窗口执行；止损和非美股时段爆仓风险检查会在持仓期间持续执行。</p>
  {detail_section}
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
    {trade_table}
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
  xaxis: {{
    rangeslider: {{ visible: false }},
    gridcolor: "#e6ebf1",
    showspikes: true,
    spikemode: "across",
    ...(chartData.fullXRange.length === 2 ? {{ range: chartData.fullXRange.slice() }} : {{}})
  }},
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
const linkedChartIds = ["price-chart", "volume-chart", "equity-chart"];
function linkXAxis(sourceId, targetIds) {{
  const source = document.getElementById(sourceId);
  source.on("plotly_relayout", eventData => {{
    if (syncingZoom) return;
    if (isXAxisReset(eventData)) {{
      requestAnimationFrame(resetLinkedCharts);
      return;
    }}
    const update = extractXAxisUpdate(eventData);
    if (update) {{
      syncXAxisToTargets(targetIds, update);
      return;
    }}
  }});
  source.on("plotly_doubleclick", () => {{
    requestAnimationFrame(resetLinkedCharts);
  }});
}}

function extractXAxisUpdate(eventData) {{
  if (Array.isArray(eventData["xaxis.range"])) {{
    return {{ "xaxis.range": eventData["xaxis.range"] }};
  }}
  if (eventData["xaxis.range[0]"] !== undefined && eventData["xaxis.range[1]"] !== undefined) {{
    return {{ "xaxis.range": [eventData["xaxis.range[0]"], eventData["xaxis.range[1]"]] }};
  }}
  return null;
}}

function isXAxisReset(eventData) {{
  return (
    eventData["xaxis.autorange"] === true ||
    eventData["xaxis.autorange"] === "true" ||
    eventData["yaxis.autorange"] === true ||
    eventData["yaxis.autorange"] === "true"
  );
}}

function resetLinkedCharts() {{
  const update = {{ "yaxis.autorange": true, shapes: [] }};
  if (chartData.fullXRange.length === 2) {{
    update["xaxis.range"] = chartData.fullXRange.slice();
  }} else {{
    update["xaxis.autorange"] = true;
  }}
  syncingZoom = true;
  Promise.all(linkedChartIds.map(id => Plotly.relayout(id, update))).finally(() => {{
    syncingZoom = false;
  }});
}}

function syncXAxisToTargets(targetIds, update) {{
  syncingZoom = true;
  Promise.all(targetIds.map(id => Plotly.relayout(id, update))).finally(() => {{
    syncingZoom = false;
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


def _metric_card(label: str, value: str, detail: str | None = None) -> str:
    detail_html = f"<div class=\"card-detail\">{html.escape(detail)}</div>" if detail else ""
    return (
        f"<div class=\"card\"><div class=\"label\">{html.escape(label)}</div>"
        f"<div class=\"value\">{html.escape(value)}</div>{detail_html}</div>"
    )


def _strategy_detail_section(config: StrategyConfig, components: StrategyComponents | None) -> str:
    lines = ["<section>", "    <h2>策略组详情</h2>"]
    if components is not None:
        filters = "<br>".join(html.escape(value) for value in components.filters)
        lines.extend(
            [
                '    <div class="detail-grid">',
                f'      <div class="detail-cell"><strong>入场策略</strong>{html.escape(components.entry)}</div>',
                f'      <div class="detail-cell"><strong>加减仓策略</strong>{html.escape(components.position)}</div>',
                f'      <div class="detail-cell"><strong>出场策略</strong>{html.escape(components.exit)}</div>',
                f'      <div class="detail-cell"><strong>过滤策略</strong>{filters}</div>',
                "    </div>",
            ]
        )

    rows = "\n".join(
        f"<tr><th>{html.escape(name)}</th><td>{html.escape(value)}</td></tr>"
        for name, value in _strategy_config_rows(config)
    )
    lines.extend(
        [
            '    <table class="config-table">',
            "      <tbody>",
            f"        {rows}",
            "      </tbody>",
            "    </table>",
            "  </section>",
        ]
    )
    return "\n".join(lines)


def _strategy_config_rows(config: StrategyConfig) -> list[tuple[str, str]]:
    return [
        ("symbol", config.symbol),
        ("entry_execution", config.entry_execution),
        ("second_pullback_wait_bars", str(config.second_pullback_wait_bars)),
        ("fee_profile", fee_profile_label(config)),
        ("fee_rate", f"{config.fee_rate:.4%}"),
        ("leverage", f"{config.leverage:.2f}x"),
        ("margin_steps", ", ".join(f"{step:.0%}" for step in config.margin_steps)),
        ("initial_stop_pct", f"{config.initial_stop_pct:.2%}"),
        ("add_thresholds", ", ".join(f"{step:.2%}" for step in config.add_thresholds)),
        ("stop_tightening", config.stop_tightening),
        ("yellow_stop_tightening", config.yellow_stop_tightening or "-"),
        ("green_stop_tightening", config.green_stop_tightening or "-"),
        ("allowed_regimes", ", ".join(config.allowed_regimes)),
        ("trading_windows_et", ", ".join(f"{start}-{end}" for start, end in config.trading_windows_et)),
        ("max_entry_above_fib_pct", _format_optional_pct(config.max_entry_above_fib_pct)),
        ("yellow_max_entry_above_fib_pct", _format_optional_pct(config.yellow_max_entry_above_fib_pct)),
        ("max_signal_range_pct", _format_optional_pct(config.max_signal_range_pct)),
        ("max_entry_above_signal_close_pct", _format_optional_pct(config.max_entry_above_signal_close_pct)),
        ("block_reverse_fib_resistance", str(config.block_reverse_fib_resistance)),
    ]


def _format_optional_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.2%}"


def _plotly_data(candles: list[Candle], result: BacktestResult) -> dict:
    times = [bar.open_time_iso for bar in candles]
    opens = [round(bar.open, 6) for bar in candles]
    highs = [round(bar.high, 6) for bar in candles]
    lows = [round(bar.low, 6) for bar in candles]
    closes = [round(bar.close, 6) for bar in candles]
    volumes = [round(bar.volume, 6) for bar in candles]
    volume_colors = ["#0f8b6f" if bar.close >= bar.open else "#d03535" for bar in candles]
    return {
        "fullXRange": [times[0], times[-1]] if times else [],
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


def _trade_table(trades: list[Trade]) -> str:
    include_reason = any(trade.exit_reason != "stop" for trade in trades)
    headers = ["开仓 UTC", "平仓 UTC", "开仓均价", "平仓价", "最高段位", "收益"]
    if include_reason:
        headers.append("原因")
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    rows = "\n".join(_trade_row(trade, include_reason) for trade in sorted(trades, key=lambda item: item.exit_time_ms, reverse=True))
    if not rows:
        rows = f"<tr><td colspan=\"{len(headers)}\">当前规则没有产生交易。</td></tr>"
    return (
        '<div class="trade-table-wrap">'
        "<table>"
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</div>"
    )


def _trade_row(trade: Trade, include_reason: bool) -> str:
    row_class = ' class="trade-positive"' if trade.pnl > 0 else ""
    return_class = ' class="trade-return-positive"' if trade.pnl > 0 else ""
    reason_cell = f"<td>{html.escape(_translate_reason(trade.exit_reason))}</td>" if include_reason else ""
    return (
        f"<tr{row_class}>"
        f"<td>{html.escape(_fmt_time(trade.entry_time_ms))}</td>"
        f"<td>{html.escape(_fmt_time(trade.exit_time_ms))}</td>"
        f"<td>{trade.entry_price:.2f}</td>"
        f"<td>{trade.exit_price:.2f}</td>"
        f"<td>{trade.max_stage}</td>"
        f"<td{return_class}>{trade.return_pct:.2%}</td>"
        f"{reason_cell}"
        "</tr>"
    )


def _fmt_time(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _translate_reason(reason: str) -> str:
    return {
        "initial_stop": "首仓止损",
        "stop": "止损",
        "non_session_liquidation_risk": "非美股时段爆仓风险",
        "end_of_data": "数据结束",
    }.get(reason, reason)


def _coverage_duration_label(candles: list[Candle]) -> str:
    if not candles:
        return "-"
    ordered = sorted(candles, key=lambda bar: bar.open_time_ms)
    interval_ms = _infer_interval_ms(ordered)
    duration_ms = ordered[-1].open_time_ms + interval_ms - ordered[0].open_time_ms
    total_minutes = max(0, round(duration_ms / 60_000))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _infer_interval_ms(candles: list[Candle]) -> int:
    if len(candles) < 2:
        return 0
    diffs = [
        candles[index].open_time_ms - candles[index - 1].open_time_ms
        for index in range(1, len(candles))
        if candles[index].open_time_ms > candles[index - 1].open_time_ms
    ]
    return min(diffs) if diffs else 0


if __name__ == "__main__":
    main()
