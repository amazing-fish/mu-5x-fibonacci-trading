from __future__ import annotations

from pathlib import Path

from mu_strategy.market_data.utils import DAY_MS
from mu_strategy.models import BacktestResult, Candle
from mu_strategy.strategy import StrategyConfig, fee_profile_label


def render_markdown_report(
    result: BacktestResult,
    *,
    config: StrategyConfig,
    symbol: str,
    data_files: list[Path],
    sample_summary: list[str] | None = None,
) -> str:
    sample_summary = sample_summary or []
    lines = [
        f"# {symbol} Backtest Report",
        "",
        "## Config",
        "",
        f"- leverage: {config.leverage}x",
        f"- margin steps: {', '.join(f'{step:.0%}' for step in config.margin_steps)}",
        f"- initial stop: {config.initial_stop_pct:.2%}",
        f"- stop tightening: {config.stop_tightening}",
        f"- stop transition bars: {config.stop_transition_bars}",
        f"- stop transition curve: {config.stop_transition_curve}",
        f"- fee profile: {fee_profile_label(config)}",
        f"- fee rate: {config.fee_rate:.4%}",
        f"- data files: {', '.join(str(path) for path in data_files)}",
        *(f"- {item}" for item in sample_summary),
        "",
        "## Metrics",
        "",
        f"- starting equity: {result.starting_equity:.2f}",
        f"- ending equity: {result.ending_equity:.2f}",
        f"- total return: {result.total_return_pct:.2%}",
        f"- max drawdown: {result.max_drawdown_pct:.2%}",
        f"- trades: {result.trade_count}",
        f"- win rate: {result.win_rate:.2%}",
        f"- profit factor: {_format_float(result.profit_factor)}",
        "",
        "## Trades",
        "",
        "| entry UTC | exit UTC | entry | exit | stage | return | reason |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for trade in result.trades:
        lines.append(
            f"| {trade.entry_time_iso} | {trade.exit_time_iso} | "
            f"{trade.entry_price:.2f} | {trade.exit_price:.2f} | "
            f"{trade.max_stage} | {trade.return_pct:.2%} | {trade.exit_reason} |"
        )
    if not result.trades:
        lines.append("| - | - | - | - | - | - | no trades |")
    lines.append("")
    lines.append("This report is a research artifact, not financial advice.")
    return "\n".join(lines)


def _format_float(value: float) -> str:
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def candle_sample_summary(candles_by_interval: dict[str, list[Candle]]) -> list[str]:
    return [
        f"{interval} actual sample: {_coverage_duration_label(candles)} ({len(candles)} rows)"
        for interval, candles in sorted(candles_by_interval.items())
    ]


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
