from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.cli import build_hourly_context
from mu_strategy.data import cached_historical
from mu_strategy.models import BacktestResult, Candle
from mu_strategy.reporting import _format_float
from mu_strategy.strategy import FEE_PROFILE_CHOICES, StrategyConfig, fee_profile_label, with_fee_profile
from mu_strategy.strategies.registry import selected_strategy_groups


DAY_MS = 86_400_000


@dataclass(frozen=True)
class AssetSpec:
    requested: str
    symbol: str
    source: str
    note: str


@dataclass(frozen=True)
class MonthlyBacktest:
    month: str
    start_time_ms: int
    end_time_ms: int
    result: BacktestResult
    candle_count: int


@dataclass(frozen=True)
class FibonacciHorizonBacktest:
    horizon_hours: int
    fib_lookback_bars: int
    full_result: BacktestResult
    monthly_results: list[MonthlyBacktest]


@dataclass(frozen=True)
class AssetFibonacciBacktest:
    asset: AssetSpec
    horizon_results: list[FibonacciHorizonBacktest]
    data_files: list[Path]


@dataclass(frozen=True)
class HorizonVerdict:
    horizon_hours: int
    rank: int | None
    status: str
    total_return_pct: float | None
    max_drawdown_pct: float | None


ASSET_ALIASES = {
    "MU": AssetSpec("MU", "MU-USDT-SWAP", "okx", "OKX MU-USDT-SWAP mapping"),
    "MUUSDT": AssetSpec("MU", "MU-USDT-SWAP", "okx", "OKX MU-USDT-SWAP mapping"),
    "MU-USDT-SWAP": AssetSpec("MU", "MU-USDT-SWAP", "okx", "OKX MU-USDT-SWAP mapping"),
    "SPACEX": AssetSpec("SPACEX", "SPCX-USDT-SWAP", "okx", "SPACEX alias mapped to OKX SPCX-USDT-SWAP"),
    "SPACE X": AssetSpec("SPACEX", "SPCX-USDT-SWAP", "okx", "SPACEX alias mapped to OKX SPCX-USDT-SWAP"),
    "SPCX": AssetSpec("SPACEX", "SPCX-USDT-SWAP", "okx", "SPACEX alias mapped to OKX SPCX-USDT-SWAP"),
    "SPCX-USDT-SWAP": AssetSpec("SPACEX", "SPCX-USDT-SWAP", "okx", "SPACEX alias mapped to OKX SPCX-USDT-SWAP"),
    "META": AssetSpec("META", "META-USDT-SWAP", "okx", "OKX META-USDT-SWAP mapping"),
    "META-USDT-SWAP": AssetSpec("META", "META-USDT-SWAP", "okx", "OKX META-USDT-SWAP mapping"),
    "BTC": AssetSpec("BTC", "BTC-USDT-SWAP", "okx", "OKX BTC-USDT-SWAP mapping"),
    "BTCUSDT": AssetSpec("BTC", "BTC-USDT-SWAP", "okx", "OKX BTC-USDT-SWAP mapping"),
    "BTC-USDT-SWAP": AssetSpec("BTC", "BTC-USDT-SWAP", "okx", "OKX BTC-USDT-SWAP mapping"),
}


def resolve_asset(value: str) -> AssetSpec:
    key = value.strip().upper()
    if key in ASSET_ALIASES:
        return ASSET_ALIASES[key]
    if key.endswith("-USDT-SWAP"):
        requested = key.removesuffix("-USDT-SWAP")
        return AssetSpec(requested, key, "okx", "explicit OKX swap symbol")
    if key.endswith("USDT"):
        requested = key.removesuffix("USDT")
        return AssetSpec(requested, key, "binance", "explicit Binance futures symbol")
    raise ValueError(f"unknown asset alias: {value}")


def fib_lookback_bars(hours: int, *, candle_minutes: int = 15) -> int:
    if hours <= 0:
        raise ValueError("hours must be positive")
    if candle_minutes <= 0:
        raise ValueError("candle_minutes must be positive")
    minutes = hours * 60
    if minutes % candle_minutes != 0:
        raise ValueError("hours must map exactly to candle_minutes")
    return minutes // candle_minutes


def split_by_utc_month(candles: list[Candle]) -> list[tuple[str, list[Candle]]]:
    groups: OrderedDict[str, list[Candle]] = OrderedDict()
    for candle in sorted(candles, key=lambda bar: bar.open_time_ms):
        month = datetime.fromtimestamp(candle.open_time_ms / 1000, tz=timezone.utc).strftime("%Y-%m")
        groups.setdefault(month, []).append(candle)
    return list(groups.items())


def run_asset_fibonacci_backtest(
    asset: AssetSpec,
    *,
    days: int,
    strategy_name: str,
    fee_profile: str,
    horizons_hours: range | list[int],
    data_dir: Path = Path("data"),
    refresh: bool = False,
) -> AssetFibonacciBacktest:
    groups = selected_strategy_groups(asset.symbol, [strategy_name])
    if len(groups) != 1:
        raise ValueError("strategy_name must resolve to exactly one strategy group")
    base_config = with_fee_profile(groups[0].config, fee_profile)
    candles_15m, file_15m = cached_historical(
        asset.symbol,
        "15m",
        days=days,
        data_dir=data_dir,
        refresh=refresh,
        source=asset.source,
    )
    candles_1h, file_1h = cached_historical(
        asset.symbol,
        "1h",
        days=days,
        data_dir=data_dir,
        refresh=refresh,
        source=asset.source,
    )
    return AssetFibonacciBacktest(
        asset=asset,
        horizon_results=run_fibonacci_horizon_backtests(
            candles_15m,
            candles_1h,
            base_config=base_config,
            horizons_hours=horizons_hours,
        ),
        data_files=[file_15m, file_1h],
    )


def run_fibonacci_horizon_backtests(
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    *,
    base_config: StrategyConfig,
    horizons_hours: list[int] | range = range(1, 13),
) -> list[FibonacciHorizonBacktest]:
    ordered_15m = sorted(candles_15m, key=lambda bar: bar.open_time_ms)
    ordered_1h = sorted(candles_1h, key=lambda bar: bar.open_time_ms)
    full_context = build_hourly_context(ordered_15m, ordered_1h)
    monthly_segments = split_by_utc_month(ordered_15m)
    interval_15m = _infer_interval_ms(ordered_15m)
    interval_1h = _infer_interval_ms(ordered_1h)

    results: list[FibonacciHorizonBacktest] = []
    for hours in horizons_hours:
        lookback_bars = fib_lookback_bars(hours)
        config = replace(base_config, fib_lookback=lookback_bars)
        full_result = run_backtest(ordered_15m, full_context, config=config)
        monthly_results: list[MonthlyBacktest] = []

        for month, segment in monthly_segments:
            if not segment:
                continue
            start_time_ms = segment[0].open_time_ms
            end_time_ms = segment[-1].open_time_ms + interval_15m
            hourly_segment = [
                bar
                for bar in ordered_1h
                if _overlaps_range(bar.open_time_ms, interval_1h, start_time_ms, end_time_ms)
            ]
            context = build_hourly_context(segment, hourly_segment)
            result = run_backtest(segment, context, config=config)
            monthly_results.append(
                MonthlyBacktest(
                    month=month,
                    start_time_ms=start_time_ms,
                    end_time_ms=end_time_ms,
                    result=result,
                    candle_count=len(segment),
                )
            )

        results.append(
            FibonacciHorizonBacktest(
                horizon_hours=hours,
                fib_lookback_bars=lookback_bars,
                full_result=full_result,
                monthly_results=monthly_results,
            )
        )
    return results


def render_fibonacci_pullback_report(
    horizon_results: list[FibonacciHorizonBacktest],
    *,
    symbol: str,
    source: str,
    days: int,
    strategy_name: str,
    data_files: list[Path],
) -> str:
    min_hour = min((result.horizon_hours for result in horizon_results), default=0)
    max_hour = max((result.horizon_hours for result in horizon_results), default=0)
    lookbacks = ", ".join(str(result.fib_lookback_bars) for result in horizon_results) or "-"
    coverage = _coverage_values(horizon_results)
    lines = [
        f"# Fibonacci 回调 {min_hour}h-{max_hour}h 请求 {days}d 月度回测",
        "",
        f"目的：固定现有交易执行、止损和过滤规则，只改变 Fibonacci 回调观察窗口，比较 {min_hour}h 到 {max_hour}h 在可用样本和各 UTC 月份里的稳定性。",
        "",
        "## 参数",
        "",
        f"- symbol: {symbol}",
        f"- source: {source}",
        f"- strategy: {strategy_name}",
        f"- days: {days}",
        f"- horizons: {min_hour}h-{max_hour}h",
        f"- fib_lookback bars: {lookbacks}",
        f"- actual 15m coverage: {_format_coverage(coverage)}",
        f"- data files: {', '.join(str(path) for path in data_files) if data_files else '-'}",
        "",
    ]
    if coverage is not None and coverage[3] < days * 0.95:
        lines.extend(
            [
                f"- data caveat: 请求 {days}d，但数据源实际返回约 {coverage[3]:.1f}d；以下月度分析只覆盖实际 CSV 行。",
                "",
            ]
        )
    lines.extend(
        [
            "## 实际样本汇总",
            "",
            "| 窗口 | fib bars | 总收益 | 最大回撤 | 交易数 | 胜率 | 盈亏因子 | 最好月份 | 最差月份 |",
            "|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for result in horizon_results:
        best_month = _best_month(result.monthly_results)
        worst_month = _worst_month(result.monthly_results)
        lines.append(
            f"| {result.horizon_hours}h | {result.fib_lookback_bars} | "
            f"{result.full_result.total_return_pct:.2%} | {result.full_result.max_drawdown_pct:.2%} | "
            f"{result.full_result.trade_count} | {result.full_result.win_rate:.2%} | "
            f"{_format_float(result.full_result.profit_factor)} | {_format_month(best_month)} | "
            f"{_format_month(worst_month)} |"
        )

    lines.extend(
        [
            "",
            "## 月度最佳/最弱窗口",
            "",
            "| 月份 | 最佳窗口 | 最佳收益 | 最弱窗口 | 最弱收益 | 有交易窗口数 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for month in _all_months(horizon_results):
        monthly = _month_results_for(horizon_results, month)
        if not monthly:
            continue
        best = max(monthly, key=lambda item: item[1].result.total_return_pct)
        worst = min(monthly, key=lambda item: item[1].result.total_return_pct)
        active_count = sum(1 for _, month_result in monthly if month_result.result.trade_count > 0)
        lines.append(
            f"| {month} | {best[0]}h | {best[1].result.total_return_pct:.2%} | "
            f"{worst[0]}h | {worst[1].result.total_return_pct:.2%} | {active_count} |"
        )

    lines.extend(
        [
            "",
            "## 月度明细",
            "",
            "| 月份 | 窗口 | fib bars | K线数 | 总收益 | 最大回撤 | 交易数 | 胜率 | 盈亏因子 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for result in horizon_results:
        for month in result.monthly_results:
            lines.append(
                f"| {month.month} | {result.horizon_hours}h | {result.fib_lookback_bars} | "
                f"{month.candle_count} | {month.result.total_return_pct:.2%} | "
                f"{month.result.max_drawdown_pct:.2%} | {month.result.trade_count} | "
                f"{month.result.win_rate:.2%} | {_format_float(month.result.profit_factor)} |"
            )

    lines.extend(_interpretation_lines(horizon_results))
    lines.extend(["", "研究用途，不构成投资建议。"])
    return "\n".join(lines)


def rank_target_horizons(
    horizon_results: list[FibonacciHorizonBacktest],
    *,
    target_hours: tuple[int, ...] = (2, 4),
    near_top: int = 3,
) -> dict[int, HorizonVerdict]:
    ranked = sorted(horizon_results, key=lambda result: result.full_result.total_return_pct, reverse=True)
    by_hour = {result.horizon_hours: result for result in ranked}
    ranks = {result.horizon_hours: index for index, result in enumerate(ranked, start=1)}
    verdicts: dict[int, HorizonVerdict] = {}
    for hour in target_hours:
        result = by_hour.get(hour)
        if result is None:
            verdicts[hour] = HorizonVerdict(hour, None, "无数据", None, None)
            continue
        rank = ranks[hour]
        if rank == 1:
            status = "最优"
        elif rank <= near_top:
            status = "较优"
        else:
            status = "非较优"
        verdicts[hour] = HorizonVerdict(
            hour,
            rank,
            status,
            result.full_result.total_return_pct,
            result.full_result.max_drawdown_pct,
        )
    return verdicts


def render_multi_asset_report(
    asset_results: list[AssetFibonacciBacktest],
    *,
    days: int,
    strategy_name: str,
    min_hour: int,
    max_hour: int,
    target_hours: tuple[int, ...] = (2, 4),
) -> str:
    lines = [
        f"# 多标的 Fibonacci 回调 {min_hour}h-{max_hour}h 请求 {days}d 回测",
        "",
        "目的：对同一套 baseline 规则做多标的窗口敏感性检查，确认 2h/4h 是否在各自样本内属于最优或较优。",
        "",
        "## 判定口径",
        "",
        "- 排名依据：完整实际样本总收益。",
        "- 最优：排名第 1。",
        "- 较优：排名第 2 或第 3。",
        "- 非较优：排名第 4 或更低。",
        "- 月度明细仍需单独看；完整样本排名不能替代稳定性判断。",
        "",
        "## 2h/4h 结论",
        "",
        "| 标的 | 解析符号 | 来源 | 实际覆盖 | 最优窗口 | 2h 状态 | 2h 收益/排名 | 4h 状态 | 4h 收益/排名 |",
        "|---|---|---|---|---:|---|---:|---|---:|",
    ]
    for asset_result in asset_results:
        best = _best_horizon(asset_result.horizon_results)
        coverage = _coverage_values(asset_result.horizon_results)
        verdicts = rank_target_horizons(asset_result.horizon_results, target_hours=target_hours)
        two = verdicts.get(2, HorizonVerdict(2, None, "无数据", None, None))
        four = verdicts.get(4, HorizonVerdict(4, None, "无数据", None, None))
        lines.append(
            f"| {asset_result.asset.requested} | {asset_result.asset.symbol} | {asset_result.asset.source} | "
            f"{_format_coverage(coverage)} | {_format_best_horizon(best)} | "
            f"{two.status} | {_format_verdict_metrics(two)} | {four.status} | {_format_verdict_metrics(four)} |"
        )

    lines.extend(
        [
            "",
            "## 月度 2h/4h 判定",
            "",
            "| 标的 | 月份 | 最优窗口 | 2h 状态 | 2h 收益/排名 | 4h 状态 | 4h 收益/排名 |",
            "|---|---|---:|---|---:|---|---:|",
        ]
    )
    for asset_result in asset_results:
        for month in _all_months(asset_result.horizon_results):
            verdicts = _rank_target_month(asset_result.horizon_results, month, target_hours=target_hours)
            two = verdicts.get(2, HorizonVerdict(2, None, "无数据", None, None))
            four = verdicts.get(4, HorizonVerdict(4, None, "无数据", None, None))
            best = _best_month_horizon(asset_result.horizon_results, month)
            lines.append(
                f"| {asset_result.asset.requested} | {month} | {_format_month_best_horizon(best)} | "
                f"{two.status} | {_format_verdict_metrics(two)} | {four.status} | {_format_verdict_metrics(four)} |"
            )

    lines.extend(
        [
            "",
            "## 各标的窗口汇总",
            "",
            "| 标的 | 窗口 | fib bars | 总收益 | 最大回撤 | 交易数 | 胜率 | 盈亏因子 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for asset_result in asset_results:
        for result in asset_result.horizon_results:
            lines.append(
                f"| {asset_result.asset.requested} | {result.horizon_hours}h | {result.fib_lookback_bars} | "
                f"{result.full_result.total_return_pct:.2%} | {result.full_result.max_drawdown_pct:.2%} | "
                f"{result.full_result.trade_count} | {result.full_result.win_rate:.2%} | "
                f"{_format_float(result.full_result.profit_factor)} |"
            )

    lines.extend(
        [
            "",
            "## 数据源说明",
            "",
        ]
    )
    for asset_result in asset_results:
        lines.append(
            f"- {asset_result.asset.requested}: {asset_result.asset.symbol} / {asset_result.asset.source}; "
            f"{asset_result.asset.note}; files: {', '.join(str(path) for path in asset_result.data_files)}"
        )
    lines.extend(["", "研究用途，不构成投资建议。"])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Fibonacci pullback lookback horizons by UTC month.")
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--source", choices=("binance", "okx"), default="okx")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--min-hour", type=int, default=1)
    parser.add_argument("--max-hour", type=int, default=12)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--report", type=Path, default=Path("reports/mu_okx_fibonacci_pullback_1h_12h_180d.md"))
    parser.add_argument("--multi-report", type=Path, default=Path("reports/fibonacci_pullback_multi_asset_1h_12h_180d.md"))
    parser.add_argument("--asset", action="append", help="Asset alias for batch mode. Repeat or pass comma-separated values.")
    parser.add_argument("--strategy", default="baseline", help="Single strategy group name to backtest.")
    parser.add_argument(
        "--fee-profile",
        choices=FEE_PROFILE_CHOICES,
        default="market",
        help="Backtest cost assumption: market/taker=0.0500%%, limit/maker=0.0200%%.",
    )
    args = parser.parse_args()

    if args.min_hour > args.max_hour:
        parser.error("--min-hour must be <= --max-hour")

    horizons = range(args.min_hour, args.max_hour + 1)
    if args.asset:
        asset_values: list[str] = []
        for value in args.asset:
            asset_values.extend(item.strip() for item in value.split(",") if item.strip())
        asset_results = [
            run_asset_fibonacci_backtest(
                resolve_asset(value),
                days=args.days,
                strategy_name=args.strategy,
                fee_profile=args.fee_profile,
                horizons_hours=horizons,
                data_dir=args.data_dir,
                refresh=args.refresh,
            )
            for value in asset_values
        ]
        report = render_multi_asset_report(
            asset_results,
            days=args.days,
            strategy_name=args.strategy,
            min_hour=args.min_hour,
            max_hour=args.max_hour,
        )
        args.multi_report.parent.mkdir(parents=True, exist_ok=True)
        args.multi_report.write_text(report, encoding="utf-8")
        print(report)
        return

    groups = selected_strategy_groups(args.symbol, [args.strategy])
    if len(groups) != 1:
        parser.error("--strategy must resolve to exactly one strategy group")
    base_config = with_fee_profile(groups[0].config, args.fee_profile)

    candles_15m, file_15m = cached_historical(
        args.symbol,
        "15m",
        days=args.days,
        data_dir=args.data_dir,
        refresh=args.refresh,
        source=args.source,
    )
    candles_1h, file_1h = cached_historical(
        args.symbol,
        "1h",
        days=args.days,
        data_dir=args.data_dir,
        refresh=args.refresh,
        source=args.source,
    )
    results = run_fibonacci_horizon_backtests(
        candles_15m,
        candles_1h,
        base_config=base_config,
        horizons_hours=horizons,
    )
    report = render_fibonacci_pullback_report(
        results,
        symbol=args.symbol,
        source=args.source,
        days=args.days,
        strategy_name=args.strategy,
        data_files=[file_15m, file_1h],
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
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


def _best_month(months: list[MonthlyBacktest]) -> MonthlyBacktest | None:
    return max(months, key=lambda month: month.result.total_return_pct, default=None)


def _worst_month(months: list[MonthlyBacktest]) -> MonthlyBacktest | None:
    return min(months, key=lambda month: month.result.total_return_pct, default=None)


def _format_month(month: MonthlyBacktest | None) -> str:
    if month is None:
        return "-"
    return f"{month.month} ({month.result.total_return_pct:.2%})"


def _best_horizon(horizon_results: list[FibonacciHorizonBacktest]) -> FibonacciHorizonBacktest | None:
    return max(horizon_results, key=lambda result: result.full_result.total_return_pct, default=None)


def _best_month_horizon(
    horizon_results: list[FibonacciHorizonBacktest], month: str
) -> tuple[int, MonthlyBacktest] | None:
    monthly = _month_results_for(horizon_results, month)
    return max(monthly, key=lambda item: item[1].result.total_return_pct, default=None)


def _format_best_horizon(result: FibonacciHorizonBacktest | None) -> str:
    if result is None:
        return "-"
    return f"{result.horizon_hours}h ({result.full_result.total_return_pct:.2%})"


def _format_month_best_horizon(best: tuple[int, MonthlyBacktest] | None) -> str:
    if best is None:
        return "-"
    return f"{best[0]}h ({best[1].result.total_return_pct:.2%})"


def _format_verdict_metrics(verdict: HorizonVerdict) -> str:
    if verdict.total_return_pct is None or verdict.rank is None:
        return "-"
    return f"{verdict.total_return_pct:.2%} / #{verdict.rank}"


def _all_months(horizon_results: list[FibonacciHorizonBacktest]) -> list[str]:
    months = sorted({month.month for result in horizon_results for month in result.monthly_results})
    return months


def _month_results_for(
    horizon_results: list[FibonacciHorizonBacktest], month: str
) -> list[tuple[int, MonthlyBacktest]]:
    output: list[tuple[int, MonthlyBacktest]] = []
    for result in horizon_results:
        for month_result in result.monthly_results:
            if month_result.month == month:
                output.append((result.horizon_hours, month_result))
    return output


def _rank_target_month(
    horizon_results: list[FibonacciHorizonBacktest],
    month: str,
    *,
    target_hours: tuple[int, ...],
    near_top: int = 3,
) -> dict[int, HorizonVerdict]:
    monthly = _month_results_for(horizon_results, month)
    ranked = sorted(monthly, key=lambda item: item[1].result.total_return_pct, reverse=True)
    by_hour = {hour: result for hour, result in ranked}
    ranks = {hour: index for index, (hour, _) in enumerate(ranked, start=1)}
    verdicts: dict[int, HorizonVerdict] = {}
    for hour in target_hours:
        result = by_hour.get(hour)
        if result is None:
            verdicts[hour] = HorizonVerdict(hour, None, "无数据", None, None)
            continue
        rank = ranks[hour]
        if rank == 1:
            status = "最优"
        elif rank <= near_top:
            status = "较优"
        else:
            status = "非较优"
        verdicts[hour] = HorizonVerdict(
            hour,
            rank,
            status,
            result.result.total_return_pct,
            result.result.max_drawdown_pct,
        )
    return verdicts


def _interpretation_lines(horizon_results: list[FibonacciHorizonBacktest]) -> list[str]:
    if not horizon_results:
        return []
    best_full = max(horizon_results, key=lambda result: result.full_result.total_return_pct)
    lowest_drawdown = max(horizon_results, key=lambda result: result.full_result.max_drawdown_pct)
    most_active = max(horizon_results, key=lambda result: result.full_result.trade_count)
    return [
        "",
        "## 结论提示",
        "",
        f"- 实际样本总收益最高窗口：{best_full.horizon_hours}h，收益 {best_full.full_result.total_return_pct:.2%}，"
        f"最大回撤 {best_full.full_result.max_drawdown_pct:.2%}。",
        f"- 实际样本回撤最浅窗口：{lowest_drawdown.horizon_hours}h，最大回撤 "
        f"{lowest_drawdown.full_result.max_drawdown_pct:.2%}，收益 {lowest_drawdown.full_result.total_return_pct:.2%}。",
        f"- 交易次数最多窗口：{most_active.horizon_hours}h，共 {most_active.full_result.trade_count} 笔；"
        "若月度表现不稳定，优先看月度表而不是只看完整样本汇总。",
    ]


def _coverage_values(horizon_results: list[FibonacciHorizonBacktest]) -> tuple[int, int, int, float] | None:
    if not horizon_results or not horizon_results[0].monthly_results:
        return None
    months = horizon_results[0].monthly_results
    start_time_ms = min(month.start_time_ms for month in months)
    end_time_ms = max(month.end_time_ms for month in months)
    candle_count = sum(month.candle_count for month in months)
    actual_days = (end_time_ms - start_time_ms) / DAY_MS if end_time_ms > start_time_ms else 0.0
    return start_time_ms, end_time_ms, candle_count, actual_days


def _format_coverage(coverage: tuple[int, int, int, float] | None) -> str:
    if coverage is None:
        return "-"
    start_time_ms, end_time_ms, candle_count, actual_days = coverage
    return (
        f"{_fmt_time(start_time_ms)} ~ {_fmt_time(end_time_ms)} UTC "
        f"({actual_days:.1f}d, {candle_count} candles)"
    )


def _fmt_time(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    main()
