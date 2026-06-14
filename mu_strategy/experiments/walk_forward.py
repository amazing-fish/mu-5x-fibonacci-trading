from __future__ import annotations

import argparse
import html
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.cli import build_hourly_context
from mu_strategy.data import cached_historical
from mu_strategy.models import BacktestResult, Candle, Trade
from mu_strategy.reporting import _format_float
from mu_strategy.strategy import FEE_PROFILE_CHOICES, StrategyConfig, fee_profile_label, with_fee_profile
from mu_strategy.strategies.registry import StrategyGroup, selected_strategy_groups


DAY_MS = 86_400_000


@dataclass(frozen=True)
class WindowBacktest:
    index: int
    start_time_ms: int
    end_time_ms: int
    result: BacktestResult
    candle_count: int


@dataclass(frozen=True)
class StrategyGroupBacktest:
    group: StrategyGroup
    windows: list[WindowBacktest]


@dataclass(frozen=True)
class StrategyGroupSummary:
    starting_equity: float
    ending_equity: float
    trades: list[Trade]
    max_drawdown_pct: float

    @property
    def total_return_pct(self) -> float:
        return (self.ending_equity / self.starting_equity) - 1 if self.starting_equity else 0.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for trade in self.trades if trade.pnl > 0) / len(self.trades)


def split_into_windows(candles: list[Candle], *, window_days: int = 14, windows: int = 2) -> list[list[Candle]]:
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if windows <= 0:
        raise ValueError("windows must be positive")
    if not candles:
        return [[] for _ in range(windows)]

    ordered = sorted(candles, key=lambda bar: bar.open_time_ms)
    interval_ms = _infer_interval_ms(ordered)
    window_ms = window_days * DAY_MS
    end_exclusive = ordered[-1].open_time_ms + interval_ms
    start_all = end_exclusive - (window_ms * windows)

    output: list[list[Candle]] = []
    for offset in range(windows):
        start = start_all + (offset * window_ms)
        end = start + window_ms
        output.append([bar for bar in ordered if start <= bar.open_time_ms < end])
    return output


def run_walk_forward_backtests(
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    *,
    config: StrategyConfig,
    window_days: int = 14,
    windows: int = 2,
) -> list[WindowBacktest]:
    segments = split_into_windows(candles_15m, window_days=window_days, windows=windows)
    interval_ms = _infer_interval_ms(candles_15m) if candles_15m else 0
    hourly_interval_ms = _infer_interval_ms(candles_1h) if candles_1h else 0
    results: list[WindowBacktest] = []

    for index, segment in enumerate(segments, start=1):
        if not segment:
            results.append(WindowBacktest(index, 0, 0, BacktestResult(10_000.0, 10_000.0, [], []), 0))
            continue

        start_time_ms = segment[0].open_time_ms
        end_time_ms = segment[-1].open_time_ms + interval_ms
        hourly_segment = [
            bar
            for bar in candles_1h
            if _overlaps_range(bar.open_time_ms, hourly_interval_ms, start_time_ms, end_time_ms)
        ]
        context = build_hourly_context(segment, hourly_segment)
        result = run_backtest(segment, context, config=config)
        results.append(WindowBacktest(index, start_time_ms, end_time_ms, result, len(segment)))
    return results


def run_strategy_group_walk_forward_backtests(
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    *,
    groups: list[StrategyGroup],
    window_days: int = 14,
    windows: int = 2,
) -> list[StrategyGroupBacktest]:
    return [
        StrategyGroupBacktest(
            group,
            run_walk_forward_backtests(
                candles_15m,
                candles_1h,
                config=group.config,
                window_days=window_days,
                windows=windows,
            ),
        )
        for group in groups
    ]


def render_strategy_group_report(
    group_results: list[StrategyGroupBacktest],
    *,
    symbol: str,
    data_files: list[Path],
) -> str:
    lines = [
        f"# {symbol} 策略组对比",
        "",
        "目的：保留 legacy_break_high 旧突破前高策略作为备用，同时把二次回踩确认升级为 baseline；后续可按名称加载策略组，避免直接丢弃备用策略或只看单段过拟合结果。",
        "",
        "## 数据",
        "",
        f"- data files: {', '.join(str(path) for path in data_files) if data_files else '-'}",
        "",
        "## 策略组",
        "",
    ]
    for group_result in group_results:
        lines.extend(_strategy_group_lines(group_result.group))

    lines.extend(_strategy_component_matrix_lines(group_results))

    lines.extend(
        [
            "",
            "## 分段结果",
            "",
            "| 策略组 | 样本 | UTC 区间 | K线数 | 总收益 | 最大回撤 | 交易数 | 胜率 | 首仓止损 | 加仓后胜率 | 最佳单笔 |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group_result in group_results:
        for window in group_result.windows:
            result = window.result
            lines.append(
                f"| {group_result.group.name} | 第{window.index}段 | {_fmt_range(window)} | {window.candle_count} | "
                f"{result.total_return_pct:.2%} | {result.max_drawdown_pct:.2%} | {result.trade_count} | "
                f"{result.win_rate:.2%} | {_stage1_stop_count(result.trades)} | "
                f"{_format_pct(_added_win_rate(result))} | {_format_pct(_best_trade_return(result))} |"
            )

    lines.extend(
        [
            "",
            "## 反向 Fibonacci 说明",
            "",
            "optimized_v2 会检查最近下跌波段的 0.382/0.5/0.618 反抽位；如果反弹买入价格正贴近这些反向 Fibonacci 压力位，就跳过这次首仓。这个过滤用于识别“反弹到压力位后立即回落”的结构，不用于替代 1h regime 过滤。",
            "",
            "研究用途，不构成投资建议。",
        ]
    )
    return "\n".join(lines)


def render_strategy_group_html_dashboard(
    group_results: list[StrategyGroupBacktest],
    *,
    symbol: str,
    data_files: list[Path],
) -> str:
    rows = "\n".join(_strategy_group_html_row(group_result) for group_result in group_results)
    data_file_text = html.escape(", ".join(str(path) for path in data_files) if data_files else "-")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(symbol)} 策略组件矩阵</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #687386;
      --border: #d9dfe8;
      --good: #0f766e;
      --bad: #b42318;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", Arial, sans-serif;
    }}
    main {{
      max-width: 1320px;
      margin: 0 auto;
      padding: 28px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 26px;
    }}
    .subtitle {{
      color: var(--muted);
      margin-bottom: 18px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow-x: auto;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }}
    table {{
      width: 100%;
      min-width: 1220px;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      padding: 9px 10px;
      vertical-align: top;
      text-align: left;
    }}
    th {{
      background: #eef2f6;
      font-weight: 650;
      white-space: nowrap;
    }}
    td.metric {{
      text-align: right;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
<main>
  <h1>{html.escape(symbol)} 策略组件矩阵</h1>
  <div class="subtitle">数据文件：{data_file_text}。按入场策略、加减仓策略、出场策略、过滤策略拆分，方便持续组合回测。</div>
  <div class="panel">
    <table>
      <thead>
        <tr>
          <th>策略组</th>
          <th>入场策略</th>
          <th>加减仓策略</th>
          <th>出场策略</th>
          <th>过滤策略</th>
          <th>手续费</th>
          <th>总收益</th>
          <th>最大回撤</th>
          <th>交易数</th>
          <th>胜率</th>
          <th>加仓后胜率</th>
          <th>首仓止损</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
  </div>
</main>
</body>
</html>"""


def render_walk_forward_report(
    window_results: list[WindowBacktest],
    *,
    config: StrategyConfig,
    symbol: str,
    data_files: list[Path],
) -> str:
    window_days = _infer_window_days(window_results)
    lines = [
        f"# {symbol} 两段 {window_days} 天回测",
        "",
        "目的：把最近数据切成连续、不重叠、独立起始权益的两段样本，先看规则是否跨样本稳定，防止过拟合到单一行情。",
        "",
        "## 参数",
        "",
        f"- leverage: {config.leverage}x",
        f"- margin steps: {', '.join(f'{step:.0%}' for step in config.margin_steps)}",
        f"- initial stop: {config.initial_stop_pct:.2%}",
        f"- fee profile: {fee_profile_label(config)}",
        f"- fee rate: {config.fee_rate:.4%}",
        f"- add thresholds: {', '.join(f'{step:.2%}' for step in config.add_thresholds)}",
        f"- data files: {', '.join(str(path) for path in data_files) if data_files else '-'}",
        "",
        "## 分段结果",
        "",
        "| 样本 | UTC 区间 | K线数 | 总收益 | 最大回撤 | 交易数 | 胜率 | 盈亏因子 | 加仓后胜率 | 最佳单笔 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for window in window_results:
        result = window.result
        lines.append(
            f"| 第{window.index}段 | {_fmt_range(window)} | {window.candle_count} | "
            f"{result.total_return_pct:.2%} | {result.max_drawdown_pct:.2%} | {result.trade_count} | "
            f"{result.win_rate:.2%} | {_format_float(result.profit_factor)} | "
            f"{_format_pct(_added_win_rate(result))} | {_format_pct(_best_trade_return(result))} |"
        )

    lines.extend(
        [
            "",
            "## 当前入场策略分析",
            "",
            "- 劣势：胜率低的主要原因是入场在高点较多；当前“回踩 Fibonacci 后收回 + 下一根突破前高”的确认方式，会把信号质量和追价执行混在一起。",
            "- 劣势：第二段加仓高点较多；固定涨幅触发在趋势脉冲里能放大利润，但在接近 1h 压力位或短线衰竭时会变成追高加仓。",
            "- 优势：1h 结构过滤和交易窗口避开了连续三天的大跌未开仓，说明大级别风控是有效模块，不能为了提高交易次数轻易放宽。",
            "- 优势：加仓后胜率提高、亏损减少，且有次拿到7%收益，说明金字塔本身不是主要问题；真正需要优化的是首仓位置和加仓触发位置。",
            "",
            "## 顶层设计",
            "",
            "1. 先优化位置质量，再优化仓位曲线：首仓必须尽量贴近 Fibonacci 回踩区，离回踩位太远的突破只记录信号，不追价成交。",
            "2. 入场拆成两层：信号层判断“能不能做多”，执行层判断“这个价格是否值得买”；若确认后直接拉离，宁可错过，不在高点补票。",
            "3. 加仓从“固定涨幅触发”升级为“浮盈推进 + 回踩确认 + 压力过滤”：第2段尤其要避免在局部高点、1h 前高、整数位或放量上影附近自动加仓。",
            "4. 保留 1h red/yellow/green 过滤：red 禁止开仓；yellow 只允许首仓或轻仓；green 才允许推进到第3/第4段。",
            "5. 把两段 14 天回测作为参数治理门槛：后续任何策略调整都必须同时跑两段；只有两段都改善，或一段改善且另一段不显著恶化，才进入下一轮。",
            "",
            "研究用途，不构成投资建议。",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run strategy-group walk-forward backtests.")
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--source", choices=("binance", "okx"), default="okx")
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--windows", type=int, default=2)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--report", type=Path, default=Path("reports/mu_okx_strategy_group_review.md"))
    parser.add_argument("--html-report", type=Path, default=Path("reports/mu_okx_strategy_components.html"))
    parser.add_argument(
        "--fee-profile",
        choices=FEE_PROFILE_CHOICES,
        default="market",
        help="Backtest cost assumption: market/taker=0.0500%, limit/maker=0.0200%.",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        help="Strategy group name to run. Repeat or pass comma-separated names. Defaults to all registered groups.",
    )
    args = parser.parse_args()

    total_days = args.window_days * args.windows
    candles_15m, file_15m = cached_historical(
        args.symbol,
        "15m",
        days=total_days,
        data_dir=args.data_dir,
        refresh=args.refresh,
        source=args.source,
    )
    candles_1h, file_1h = cached_historical(
        args.symbol,
        "1h",
        days=total_days,
        data_dir=args.data_dir,
        refresh=args.refresh,
        source=args.source,
    )
    group_results = run_strategy_group_walk_forward_backtests(
        candles_15m,
        candles_1h,
        groups=[
            replace(group, config=with_fee_profile(group.config, args.fee_profile))
            for group in selected_strategy_groups(args.symbol, args.strategy)
        ],
        window_days=args.window_days,
        windows=args.windows,
    )
    report = render_strategy_group_report(
        group_results,
        symbol=args.symbol,
        data_files=[file_15m, file_1h],
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    if args.html_report:
        dashboard = render_strategy_group_html_dashboard(
            group_results,
            symbol=args.symbol,
            data_files=[file_15m, file_1h],
        )
        args.html_report.parent.mkdir(parents=True, exist_ok=True)
        args.html_report.write_text(dashboard, encoding="utf-8")
    print(report)


def _infer_interval_ms(candles: list[Candle]) -> int:
    if len(candles) < 2:
        return 0
    diffs = [
        candles[index].open_time_ms - candles[index - 1].open_time_ms
        for index in range(1, len(candles))
        if candles[index].open_time_ms > candles[index - 1].open_time_ms
    ]
    return min(diffs) if diffs else 0


def _overlaps_range(open_time_ms: int, interval_ms: int, start_time_ms: int, end_time_ms: int) -> bool:
    if interval_ms <= 0:
        return start_time_ms <= open_time_ms < end_time_ms
    return open_time_ms < end_time_ms and open_time_ms + interval_ms > start_time_ms


def _infer_window_days(window_results: list[WindowBacktest]) -> int:
    for window in window_results:
        if window.end_time_ms > window.start_time_ms:
            return max(1, round((window.end_time_ms - window.start_time_ms) / DAY_MS))
    return 14


def _fmt_range(window: WindowBacktest) -> str:
    if window.end_time_ms <= window.start_time_ms:
        return "-"
    return f"{_fmt_time(window.start_time_ms)} ~ {_fmt_time(window.end_time_ms)}"


def _fmt_time(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _added_win_rate(result: BacktestResult) -> float | None:
    added = [trade for trade in result.trades if trade.max_stage >= 2]
    if not added:
        return None
    return sum(1 for trade in added if trade.pnl > 0) / len(added)


def _best_trade_return(result: BacktestResult) -> float | None:
    if not result.trades:
        return None
    return max(trade.return_pct for trade in result.trades)


def _format_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2%}"


def _stage1_stop_count(trades: list[Trade]) -> int:
    return sum(1 for trade in trades if trade.max_stage == 1 and trade.exit_reason == "stop")


def _strategy_component_matrix_lines(group_results: list[StrategyGroupBacktest]) -> list[str]:
    lines = [
        "",
        "## 策略组件矩阵",
        "",
        "| 策略组 | 入场策略 | 加减仓策略 | 出场策略 | 过滤策略 |",
        "|---|---|---|---|---|",
    ]
    for group_result in group_results:
        group = group_result.group
        components = group.components
        lines.append(
            f"| {group.name} | {components.entry} | {components.position} | {components.exit} | "
            f"{'<br>'.join(components.filters)} |"
        )
    return lines


def _strategy_group_html_row(group_result: StrategyGroupBacktest) -> str:
    group = group_result.group
    result = _combined_result(group_result)
    components = group.components
    total_return_class = "good" if result.total_return_pct >= 0 else "bad"
    return (
        "<tr>"
        f"<td><strong>{html.escape(group.name)}</strong><br><span class=\"muted\">{html.escape(group.label)}</span></td>"
        f"<td>{html.escape(components.entry)}</td>"
        f"<td>{html.escape(components.position)}</td>"
        f"<td>{html.escape(components.exit)}</td>"
        f"<td>{'<br>'.join(html.escape(value) for value in components.filters)}</td>"
        f"<td>{html.escape(fee_profile_label(group.config))}<br>{group.config.fee_rate:.4%}</td>"
        f"<td class=\"metric {total_return_class}\">{result.total_return_pct:.2%}</td>"
        f"<td class=\"metric bad\">{result.max_drawdown_pct:.2%}</td>"
        f"<td class=\"metric\">{result.trade_count}</td>"
        f"<td class=\"metric\">{result.win_rate:.2%}</td>"
        f"<td class=\"metric\">{_format_pct(_added_win_rate(result))}</td>"
        f"<td class=\"metric\">{_stage1_stop_count(result.trades)}</td>"
        "</tr>"
    )


def _combined_result(group_result: StrategyGroupBacktest) -> StrategyGroupSummary:
    if not group_result.windows:
        return StrategyGroupSummary(10_000, 10_000, [], 0.0)
    starting = sum(window.result.starting_equity for window in group_result.windows)
    ending = sum(window.result.ending_equity for window in group_result.windows)
    trades = [trade for window in group_result.windows for trade in window.result.trades]
    max_drawdown = min((window.result.max_drawdown_pct for window in group_result.windows), default=0.0)
    return StrategyGroupSummary(starting, ending, trades, max_drawdown)


def _strategy_group_lines(group: StrategyGroup) -> list[str]:
    config = group.config
    output = [f"- {group.name} ({group.label})"]
    output.append(f"  - 费用：{fee_profile_label(config)}，费率 {config.fee_rate:.4%}。")
    if config.entry_execution == "break_high":
        output.append("  - 规则：原始 Fib 回踩确认 + 下一根突破前高执行。")
    elif config.entry_execution == "direct_next_open":
        output.append("  - 规则：Fib 回踩确认后下一根开盘直接买入，不等待突破前高。")
    elif config.entry_execution == "second_pullback":
        output.append(
            f"  - 规则：Fib 回踩确认后等待二次回踩买入，最多等待 {config.second_pullback_wait_bars} 根 15m K。"
        )
    else:
        output.append(f"  - 规则：entry_execution={config.entry_execution}")
    if config.yellow_stop_tightening is not None or config.green_stop_tightening is not None:
        yellow_mode = config.yellow_stop_tightening or config.stop_tightening
        green_mode = config.green_stop_tightening or config.stop_tightening
        output.append(f"  - 止损：yellow={_stop_policy_label(yellow_mode)}，green={_stop_policy_label(green_mode)}。")
    elif config.stop_tightening == "baseline":
        output.append("  - 止损：baseline 抬止损。")
    elif config.stop_tightening == "half_protect_green_wide":
        output.append("  - 止损：半保护；1h green 时更宽，不立即抬到首仓成本/均价。")
    elif config.stop_tightening == "half_protect":
        output.append("  - 止损：半保护。")
    elif config.stop_tightening == "green_wide":
        output.append("  - 止损：1h green 更宽。")
    if (
        config.max_entry_above_fib_pct is None
        and config.max_signal_range_pct is None
        and not config.block_reverse_fib_resistance
    ):
        return output
    output.append(
        "  - 过滤：限制首仓追价、限制信号 K 过宽、yellow 更严格，并可启用反向 Fibonacci 压力位过滤。"
    )
    output.append(f"  - max entry above Fib: {config.max_entry_above_fib_pct:.2%}")
    if config.yellow_max_entry_above_fib_pct is not None:
        output.append(f"  - yellow max entry above Fib: {config.yellow_max_entry_above_fib_pct:.2%}")
    if config.max_signal_range_pct is not None:
        output.append(f"  - max signal range: {config.max_signal_range_pct:.2%}")
    if config.max_entry_above_signal_close_pct is not None:
        output.append(f"  - max entry above signal close: {config.max_entry_above_signal_close_pct:.2%}")
    output.append(f"  - reverse Fibonacci resistance filter: {config.block_reverse_fib_resistance}")
    return output


def _stop_policy_label(mode: str) -> str:
    return {
        "baseline": "窄止损/baseline",
        "half_protect": "半保护",
        "wide": "宽止损",
        "green_wide": "green宽止损",
        "half_protect_green_wide": "半保护+green宽止损",
    }.get(mode, mode)


if __name__ == "__main__":
    main()
