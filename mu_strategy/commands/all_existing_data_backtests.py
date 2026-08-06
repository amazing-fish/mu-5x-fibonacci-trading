from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mu_strategy.backtest import run_backtest
from mu_strategy.cli import build_hourly_context
from mu_strategy.data import read_csv
from mu_strategy.market_data.utils import interval_to_ms
from mu_strategy.models import BacktestResult, Candle
from mu_strategy.reporting import render_markdown_report
from mu_strategy.strategy import FEE_PROFILE_CHOICES, fee_profile_label, fee_rate_for_profile, with_fee_profile
from mu_strategy.strategies.registry import selected_strategy_groups
from mu_strategy.viz.backtest import render_html_visualization


@dataclass(frozen=True)
class LocalDataset:
    source: str
    symbol: str
    nominal_days: int
    file_15m: Path
    file_1h: Path


@dataclass(frozen=True)
class CandleMove:
    open_time_iso: str
    pct: float
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class DataQualityAudit:
    expected_bars: int
    duplicate_timestamps: int
    gaps: int
    invalid_ohlc: int
    open_close_warnings: int
    high_low_warnings: int
    prev_close_open_gap_warnings: int
    true_duration_label: str
    max_open_close_move_pct: float
    max_high_low_range_pct: float
    max_prev_close_open_gap_pct: float
    top_open_close_moves: list[CandleMove]
    top_high_low_ranges: list[CandleMove]
    top_prev_close_open_gaps: list[CandleMove]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run baseline backtests for every existing local MU dataset.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports/live"))
    parser.add_argument("--strategy", default="baseline")
    parser.add_argument(
        "--fee-profile",
        choices=FEE_PROFILE_CHOICES,
        default="market",
        help="Backtest cost assumption: market/taker=0.0500%%, limit/maker=0.0200%%.",
    )
    parser.add_argument("--open-close-warning-pct", type=float, default=0.05)
    parser.add_argument("--high-low-warning-pct", type=float, default=0.05)
    parser.add_argument("--prev-close-open-warning-pct", type=float, default=0.01)
    args = parser.parse_args()

    rows = run_all_existing_data_backtests(
        data_dir=args.data_dir,
        report_dir=args.report_dir,
        strategy=args.strategy,
        fee_profile=args.fee_profile,
        open_close_warning_pct=args.open_close_warning_pct,
        high_low_warning_pct=args.high_low_warning_pct,
        prev_close_open_warning_pct=args.prev_close_open_warning_pct,
    )
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    md_path = args.report_dir / "mu_all_existing_data_backtests.md"
    html_path = args.report_dir / "mu_all_existing_data_backtests.html"
    md_path.write_text(
        render_summary_markdown(
            rows,
            generated_at=generated_at,
            strategy=args.strategy,
            fee_profile=args.fee_profile,
            open_close_warning_pct=args.open_close_warning_pct,
            high_low_warning_pct=args.high_low_warning_pct,
            prev_close_open_warning_pct=args.prev_close_open_warning_pct,
        ),
        encoding="utf-8",
    )
    html_path.write_text(
        render_summary_html(
            rows,
            generated_at=generated_at,
            strategy=args.strategy,
            fee_profile=args.fee_profile,
            open_close_warning_pct=args.open_close_warning_pct,
            high_low_warning_pct=args.high_low_warning_pct,
            prev_close_open_warning_pct=args.prev_close_open_warning_pct,
        ),
        encoding="utf-8",
    )

    print(f"datasets={len(rows)}")
    for row in rows:
        print(
            f"{row['source']} {row['symbol']} {row['nominal_days']}d "
            f"duration15={row['duration_15m_label']} trades={row['trades']} "
            f"return={row['return_pct']:.2%} max_dd={row['max_drawdown_pct']:.2%} "
            f"range_anomalies={row['range_anomalies']} "
            f"open_close_warnings={row['open_close_warnings']}"
        )
    print(f"wrote {md_path.resolve()}")
    print(f"wrote {html_path.resolve()}")


def run_all_existing_data_backtests(
    *,
    data_dir: Path,
    report_dir: Path,
    strategy: str,
    fee_profile: str = "market",
    open_close_warning_pct: float = 0.05,
    high_low_warning_pct: float = 0.05,
    prev_close_open_warning_pct: float = 0.01,
) -> list[dict[str, Any]]:
    report_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for dataset in discover_datasets(data_dir):
        candles_15m = read_csv(dataset.file_15m)
        candles_1h = read_csv(dataset.file_1h)
        group = selected_strategy_groups(dataset.symbol, [strategy])[0]
        config = with_fee_profile(group.config, fee_profile)
        context = build_hourly_context(candles_15m, candles_1h)
        result = run_backtest(candles_15m, context, config=config)
        range_audit = audit_price_ranges(result, candles_15m, candles_1h)
        audit_15m = audit_candles(
            candles_15m,
            "15m",
            open_close_warning_pct=open_close_warning_pct,
            high_low_warning_pct=high_low_warning_pct,
            prev_close_open_warning_pct=prev_close_open_warning_pct,
        )
        audit_1h = audit_candles(candles_1h, "1h")

        fee_slug = "" if config.fee_profile == "market" else f"_{config.fee_profile}"
        slug = f"mu_{dataset.source}_{dataset.symbol.replace('-', '_')}_{dataset.nominal_days}d_{strategy}{fee_slug}"
        report_path = report_dir / f"{slug}_backtest.md"
        chart_path = report_dir / f"{slug}_backtest.html"
        report_path.write_text(
            render_markdown_report(
                result,
                config=config,
                symbol=dataset.symbol,
                data_files=[dataset.file_15m, dataset.file_1h],
                candles=candles_15m,
            ),
            encoding="utf-8",
        )
        chart_path.write_text(
            render_html_visualization(
                candles_1h,
                result,
                config=config,
                symbol=dataset.symbol,
                chart_interval="1h",
                strategy_name=group.name,
                strategy_label=group.label,
                strategy_components=group.components,
            ),
            encoding="utf-8",
        )

        rows.append(
            {
                "source": dataset.source,
                "symbol": dataset.symbol,
                "nominal_days": dataset.nominal_days,
                "bars_15m": len(candles_15m),
                "bars_1h": len(candles_1h),
                "duration_15m_label": audit_15m.true_duration_label,
                "duration_1h_label": audit_1h.true_duration_label,
                "start_15m": candles_15m[0].open_time_iso if candles_15m else "-",
                "end_15m": candles_15m[-1].open_time_iso if candles_15m else "-",
                "ending_equity": result.ending_equity,
                "return_pct": result.total_return_pct,
                "max_drawdown_pct": result.max_drawdown_pct,
                "trades": result.trade_count,
                "win_rate": result.win_rate,
                "profit_factor": result.profit_factor,
                "fee_profile": config.fee_profile,
                "fee_rate": config.fee_rate,
                "audited_events": range_audit["events"],
                "range_anomalies": range_audit["bad_15m"] + range_audit["bad_1h"] + range_audit["missing"],
                "invalid_ohlc": audit_15m.invalid_ohlc,
                "duplicate_timestamps": audit_15m.duplicate_timestamps,
                "gaps_15m": audit_15m.gaps,
                "open_close_warnings": audit_15m.open_close_warnings,
                "high_low_warnings": audit_15m.high_low_warnings,
                "prev_close_open_gap_warnings": audit_15m.prev_close_open_gap_warnings,
                "max_open_close_move_pct": audit_15m.max_open_close_move_pct,
                "max_high_low_range_pct": audit_15m.max_high_low_range_pct,
                "max_prev_close_open_gap_pct": audit_15m.max_prev_close_open_gap_pct,
                "top_open_close_moves": audit_15m.top_open_close_moves,
                "top_high_low_ranges": audit_15m.top_high_low_ranges,
                "top_prev_close_open_gaps": audit_15m.top_prev_close_open_gaps,
                "report_path": report_path,
                "chart_path": chart_path,
            }
        )
    return rows


def discover_datasets(data_dir: Path) -> list[LocalDataset]:
    datasets: list[LocalDataset] = []
    for file_15m in data_dir.glob("*_15m_*d.csv"):
        okx_match = re.fullmatch(r"OKX_(.+)_15m_(\d+)d\.csv", file_15m.name)
        if okx_match:
            symbol, nominal_days = okx_match.groups()
            file_1h = data_dir / f"OKX_{symbol}_1h_{nominal_days}d.csv"
            if file_1h.exists():
                datasets.append(LocalDataset("okx", symbol, int(nominal_days), file_15m, file_1h))
            continue

        binance_match = re.fullmatch(r"(.+)_15m_(\d+)d\.csv", file_15m.name)
        if not binance_match:
            continue
        symbol, nominal_days = binance_match.groups()
        file_1h = data_dir / f"{symbol}_1h_{nominal_days}d.csv"
        if file_1h.exists():
            datasets.append(LocalDataset("binance", symbol, int(nominal_days), file_15m, file_1h))
    return sorted(datasets, key=lambda item: (item.source, item.symbol, item.nominal_days))


def audit_candles(
    candles: list[Candle],
    interval: str,
    *,
    open_close_warning_pct: float = 0.05,
    high_low_warning_pct: float = 0.05,
    prev_close_open_warning_pct: float = 0.01,
) -> DataQualityAudit:
    if not candles:
        return DataQualityAudit(0, 0, 0, 0, 0, 0, 0, "0d 0h 0m", 0.0, 0.0, 0.0, [], [], [])

    step_ms = interval_to_ms(interval)
    timestamps = [candle.open_time_ms for candle in candles]
    expected_bars = int((timestamps[-1] - timestamps[0]) // step_ms) + 1
    duplicate_timestamps = len(timestamps) - len(set(timestamps))
    gaps = sum(1 for left, right in zip(timestamps, timestamps[1:]) if right - left != step_ms)
    invalid_ohlc = 0
    open_close_moves: list[CandleMove] = []
    high_low_ranges: list[CandleMove] = []
    prev_close_open_gaps: list[CandleMove] = []

    for candle in candles:
        if (
            candle.open <= 0
            or candle.high <= 0
            or candle.low <= 0
            or candle.close <= 0
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
            or candle.high < candle.low
        ):
            invalid_ohlc += 1
        open_close_moves.append(
            CandleMove(
                candle.open_time_iso,
                abs(candle.close / candle.open - 1) if candle.open else 0.0,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
            )
        )
        high_low_ranges.append(
            CandleMove(
                candle.open_time_iso,
                (candle.high / candle.low - 1) if candle.low else 0.0,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
            )
        )

    for previous, current in zip(candles, candles[1:]):
        prev_close_open_gaps.append(
            CandleMove(
                current.open_time_iso,
                abs(current.open / previous.close - 1) if previous.close else 0.0,
                current.open,
                current.high,
                current.low,
                current.close,
            )
        )

    open_close_moves.sort(key=lambda move: move.pct, reverse=True)
    high_low_ranges.sort(key=lambda move: move.pct, reverse=True)
    prev_close_open_gaps.sort(key=lambda move: move.pct, reverse=True)

    return DataQualityAudit(
        expected_bars=expected_bars,
        duplicate_timestamps=duplicate_timestamps,
        gaps=gaps,
        invalid_ohlc=invalid_ohlc,
        open_close_warnings=sum(1 for move in open_close_moves if move.pct > open_close_warning_pct),
        high_low_warnings=sum(1 for move in high_low_ranges if move.pct > high_low_warning_pct),
        prev_close_open_gap_warnings=sum(1 for move in prev_close_open_gaps if move.pct > prev_close_open_warning_pct),
        true_duration_label=duration_label(true_duration_ms(candles, interval)),
        max_open_close_move_pct=open_close_moves[0].pct if open_close_moves else 0.0,
        max_high_low_range_pct=high_low_ranges[0].pct if high_low_ranges else 0.0,
        max_prev_close_open_gap_pct=prev_close_open_gaps[0].pct if prev_close_open_gaps else 0.0,
        top_open_close_moves=open_close_moves[:5],
        top_high_low_ranges=high_low_ranges[:5],
        top_prev_close_open_gaps=prev_close_open_gaps[:5],
    )


def true_duration_ms(candles: list[Candle], interval: str) -> int:
    if not candles:
        return 0
    return candles[-1].open_time_ms + interval_to_ms(interval) - candles[0].open_time_ms


def duration_label(duration_ms: int) -> str:
    total_minutes = max(0, duration_ms // 60_000)
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m"


def audit_price_ranges(result: BacktestResult, candles_15m: list[Candle], candles_1h: list[Candle]) -> dict[str, int]:
    by_15m = {candle.open_time_ms: candle for candle in candles_15m}
    by_1h = {candle.open_time_ms: candle for candle in candles_1h}
    audit = {"events": 0, "bad_15m": 0, "bad_1h": 0, "missing": 0}

    def bucket_1h(open_time_ms: int) -> int:
        return open_time_ms - (open_time_ms % interval_to_ms("1h"))

    def check(open_time_ms: int, price: float) -> None:
        audit["events"] += 1
        candle_15m = by_15m.get(open_time_ms)
        if candle_15m is None:
            audit["missing"] += 1
        elif price < candle_15m.low - 1e-9 or price > candle_15m.high + 1e-9:
            audit["bad_15m"] += 1
        candle_1h = by_1h.get(bucket_1h(open_time_ms))
        if candle_1h is None:
            audit["missing"] += 1
        elif price < candle_1h.low - 1e-9 or price > candle_1h.high + 1e-9:
            audit["bad_1h"] += 1

    for trade in result.trades:
        for fill in trade.fills:
            check(fill.time_ms, fill.price)
        check(trade.exit_time_ms, trade.exit_price)
    return audit


def render_summary_markdown(
    rows: list[dict[str, Any]],
    *,
    generated_at: str,
    strategy: str = "baseline",
    fee_profile: str = "market",
    open_close_warning_pct: float = 0.05,
    high_low_warning_pct: float = 0.05,
    prev_close_open_warning_pct: float = 0.01,
) -> str:
    lines = [
        "# MU All Existing Local Data Backtests",
        "",
        f"- strategy: {strategy}",
        f"- fee profile: {fee_profile_label(fee_profile)}",
        f"- fee rate: {fee_rate_for_profile(fee_profile):.4%}",
        "- source policy: local CSV only; no refresh/network fetch during this aggregate run",
        f"- generated at UTC: {generated_at}",
        f"- datasets: {len(rows)}",
        "- data quality thresholds: "
        f"15m open-close warning > {_format_pct(open_close_warning_pct)}, "
        f"15m high-low warning > {_format_pct(high_low_warning_pct)}, "
        f"prev close -> next open warning > {_format_pct(prev_close_open_warning_pct)}",
        "",
        "## Summary",
        "",
        "| source | symbol | nominal days | true 15m duration | true 1h duration | 15m bars | 1h bars | 15m coverage UTC | ending equity | return | max DD | trades | win rate | profit factor | audited events | price range anomalies | OHLC invalid | dup ts | 15m gaps | open-close warnings | high-low warnings | max O-C | max H-L | max prevC-open | report | chart |",
        "|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(_summary_markdown_row(row))
    lines.extend(["", "## Price-Range Audit", ""])
    if all(row["range_anomalies"] == 0 for row in rows):
        lines.append(
            "- PASS: every fill and exit price is inside its corresponding 15m candle and corresponding 1h chart candle for all datasets."
        )
    else:
        for row in rows:
            if row["range_anomalies"]:
                lines.append(
                    f"- {row['source']} {row['symbol']} {row['nominal_days']}d: "
                    f"range anomalies={row['range_anomalies']}"
                )
    lines.extend(["", "## Data Quality Review", ""])
    if all(_data_quality_issue_count(row) == 0 for row in rows):
        lines.append("- PASS: no duplicate timestamps, 15m gaps, OHLC invalid rows, or configured large-move warnings.")
    else:
        for row in rows:
            issue_count = _data_quality_issue_count(row)
            if issue_count == 0:
                continue
            lines.append(
                f"- {row['source']} {row['symbol']} nominal {row['nominal_days']}d: "
                f"issues={issue_count}, open-close warnings={row['open_close_warnings']}, "
                f"high-low warnings={row['high_low_warnings']}, prevC-open warnings={row['prev_close_open_gap_warnings']}, "
                f"invalid OHLC={row['invalid_ohlc']}, dup ts={row['duplicate_timestamps']}, gaps={row['gaps_15m']}."
            )
            if row["top_open_close_moves"]:
                lines.append("  Top 15m open-close moves:")
                for move in row["top_open_close_moves"][:3]:
                    lines.append(
                        f"  - {move.open_time_iso}: {move.pct:.2%}, "
                        f"O={move.open:.4f}, H={move.high:.4f}, L={move.low:.4f}, C={move.close:.4f}"
                    )
    lines.extend(["", "This report is a research artifact, not financial advice."])
    return "\n".join(lines)


def render_summary_html(
    rows: list[dict[str, Any]],
    *,
    generated_at: str,
    strategy: str = "baseline",
    fee_profile: str = "market",
    open_close_warning_pct: float = 0.05,
    high_low_warning_pct: float = 0.05,
    prev_close_open_warning_pct: float = 0.01,
) -> str:
    total_range_anomalies = sum(row["range_anomalies"] for row in rows)
    total_quality_issues = sum(_data_quality_issue_count(row) for row in rows)
    css = """
body{font-family:Segoe UI,Arial,sans-serif;margin:24px;background:#f7f8fb;color:#17202a}h1{font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:16px 0}.card{background:#fff;border:1px solid #dde3eb;border-radius:8px;padding:12px}.label{font-size:12px;color:#667085}.value{font-size:20px;font-weight:700;margin-top:4px}table{border-collapse:collapse;width:100%;background:white;font-size:13px}th,td{border:1px solid #d9dee7;padding:7px 8px;text-align:left;vertical-align:top}th{background:#eef2f6}.ok{color:#0f766e;font-weight:700}.bad{color:#b42318;font-weight:700}.scroll{overflow-x:auto}.muted{color:#667085}a{color:#175cd3;text-decoration:none}
"""
    parts = [
        '<!doctype html><html><head><meta charset="utf-8">',
        "<title>MU All Existing Local Data Backtests</title>",
        f"<style>{css}</style></head><body>",
        "<h1>MU All Existing Local Data Backtests</h1>",
        '<p class="muted">Local CSV only; no refresh/network fetch during this aggregate run. Research artifact, not financial advice.</p>',
        '<div class="grid">',
        _card("Datasets", str(len(rows))),
        _card("Strategy", strategy),
        _card("Fee Profile", fee_profile_label(fee_profile)),
        _card("Fee Rate", f"{fee_rate_for_profile(fee_profile):.4%}"),
        _card("Price range anomalies", str(total_range_anomalies)),
        _card("Data quality issues", str(total_quality_issues)),
        "</div>",
        f'<p class="muted">Generated at UTC: {html.escape(generated_at)}. '
        "Data quality thresholds: "
        f"15m open-close > {_format_pct(open_close_warning_pct)}, "
        f"15m high-low > {_format_pct(high_low_warning_pct)}, "
        f"prev close -> next open > {_format_pct(prev_close_open_warning_pct)}.</p>",
        '<div class="scroll"><table><tr>',
    ]
    headers = [
        "Source",
        "Symbol",
        "Nominal Days",
        "True 15m Duration",
        "True 1h Duration",
        "15m Bars",
        "1h Bars",
        "15m Coverage UTC",
        "Ending Equity",
        "Return",
        "Max DD",
        "Trades",
        "Win Rate",
        "Profit Factor",
        "Audited Events",
        "Price Range Anomalies",
        "OHLC Invalid",
        "Dup TS",
        "15m Gaps",
        "Open-Close Warnings",
        "High-Low Warnings",
        "Max O-C",
        "Max H-L",
        "Max PrevC-Open",
        "Report",
        "Chart",
    ]
    parts.extend(f"<th>{html.escape(header)}</th>" for header in headers)
    parts.append("</tr>")
    for row in rows:
        parts.append(_summary_html_row(row))
    parts.extend(["</table></div>", "</body></html>"])
    return "\n".join(parts)


def _summary_markdown_row(row: dict[str, Any]) -> str:
    profit_factor = "inf" if row["profit_factor"] == float("inf") else f"{row['profit_factor']:.2f}"
    return (
        f"| {row['source']} | {row['symbol']} | {row['nominal_days']} | "
        f"{row['duration_15m_label']} | {row['duration_1h_label']} | {row['bars_15m']} | {row['bars_1h']} | "
        f"{row['start_15m']} to {row['end_15m']} | {row['ending_equity']:.2f} | "
        f"{row['return_pct']:.2%} | {row['max_drawdown_pct']:.2%} | {row['trades']} | "
        f"{row['win_rate']:.2%} | {profit_factor} | {row['audited_events']} | {row['range_anomalies']} | "
        f"{row['invalid_ohlc']} | {row['duplicate_timestamps']} | {row['gaps_15m']} | "
        f"{row['open_close_warnings']} | {row['high_low_warnings']} | {row['max_open_close_move_pct']:.2%} | "
        f"{row['max_high_low_range_pct']:.2%} | {row['max_prev_close_open_gap_pct']:.2%} | "
        f"{row['report_path'].as_posix()} | {row['chart_path'].as_posix()} |"
    )


def _summary_html_row(row: dict[str, Any]) -> str:
    profit_factor = "inf" if row["profit_factor"] == float("inf") else f"{row['profit_factor']:.2f}"
    range_class = "ok" if row["range_anomalies"] == 0 else "bad"
    cells = [
        row["source"],
        row["symbol"],
        str(row["nominal_days"]),
        row["duration_15m_label"],
        row["duration_1h_label"],
        str(row["bars_15m"]),
        str(row["bars_1h"]),
        f"{row['start_15m']} to {row['end_15m']}",
        f"{row['ending_equity']:.2f}",
        f"{row['return_pct']:.2%}",
        f"{row['max_drawdown_pct']:.2%}",
        str(row["trades"]),
        f"{row['win_rate']:.2%}",
        profit_factor,
        str(row["audited_events"]),
    ]
    parts = ["<tr>"]
    parts.extend(f"<td>{html.escape(value)}</td>" for value in cells)
    parts.append(f'<td class="{range_class}">{row["range_anomalies"]}</td>')
    parts.append(_quality_cell(row["invalid_ohlc"]))
    parts.append(_quality_cell(row["duplicate_timestamps"]))
    parts.append(_quality_cell(row["gaps_15m"]))
    parts.append(_quality_cell(row["open_close_warnings"]))
    parts.append(_quality_cell(row["high_low_warnings"]))
    parts.append(f"<td>{row['max_open_close_move_pct']:.2%}</td>")
    parts.append(f"<td>{row['max_high_low_range_pct']:.2%}</td>")
    parts.append(f"<td>{row['max_prev_close_open_gap_pct']:.2%}</td>")
    parts.append(f'<td><a href="{html.escape(row["report_path"].name)}">md</a></td>')
    parts.append(f'<td><a href="{html.escape(row["chart_path"].name)}">html</a></td>')
    parts.append("</tr>")
    return "".join(parts)


def _card(label: str, value: str) -> str:
    return f'<div class="card"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(value)}</div></div>'


def _format_pct(value: float) -> str:
    return f"{value:.2%}"


def _quality_cell(value: int) -> str:
    cell_class = "ok" if value == 0 else "bad"
    return f'<td class="{cell_class}">{value}</td>'


def _data_quality_issue_count(row: dict[str, Any]) -> int:
    return (
        row["invalid_ohlc"]
        + row["duplicate_timestamps"]
        + row["gaps_15m"]
        + row["open_close_warnings"]
        + row["high_low_warnings"]
        + row["prev_close_open_gap_warnings"]
    )


if __name__ == "__main__":
    main()
