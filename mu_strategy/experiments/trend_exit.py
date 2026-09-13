"""Fixed, retrospective MU trend/exit experiment; never promotes a strategy.

Design and sources: docs/plans/2026-09-13-mu-trend-exit-experiment.md.
Ratios use decimal units; drawdown is signed (more negative is worse).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, replace
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.market_data.utils import DAY_MS
from mu_strategy.models import BacktestResult
from mu_strategy.research.historical_data import (
    HistoricalGenerationError, HistoricalResearchWindow, load_historical_window, validate_replay_outputs,
)
from mu_strategy.research.robustness import stage_distribution, trade_concentration
from mu_strategy.strategies.registry import selected_strategy_groups
from mu_strategy.strategy import StrategyConfig


def candidate_configs(symbol: str) -> dict[str, StrategyConfig]:
    names = ["baseline", "baseline_delayed_tighten_smooth", "baseline_green_wide", "baseline_yellow_green_wide"]
    configs = {group.name: group.config for group in selected_strategy_groups(symbol, names)}
    configs.update({
        "green_only_baseline": replace(configs["baseline"], allowed_regimes=("green",)),
        "green_only_smooth": replace(configs["baseline_delayed_tighten_smooth"], allowed_regimes=("green",)),
        "green_only_green_wide": replace(configs["baseline_green_wide"], allowed_regimes=("green",)),
        "smooth_rsi50": replace(configs["baseline_delayed_tighten_smooth"], rsi_floor=50),
    })
    return configs


def split_periods(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    days, remainder = divmod(end_ms - start_ms, DAY_MS)
    if remainder or days < 3:
        raise ValueError("experiment needs at least three complete days")
    size = (days + 2) // 3
    boundaries = [start_ms, start_ms + size * DAY_MS, start_ms + 2 * size * DAY_MS, end_ms]
    # For four days, use 2/1/1 instead of an empty third segment.
    if boundaries[2] >= end_ms:
        boundaries[2] = end_ms - DAY_MS
    return list(zip(boundaries, boundaries[1:]))


def metrics(result: BacktestResult) -> dict:
    return {
        "return_pct": result.total_return_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "ending_equity": result.ending_equity,
        "trade_count": result.trade_count,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor if math.isfinite(result.profit_factor) else None,
        "concentration": asdict(trade_concentration(result.trades)),
        "stages": asdict(stage_distribution(result.trades))["stages"],
    }


def retention_failures(row: dict, baseline: dict) -> list[str]:
    failures = []
    if row["full"]["return_pct"] <= baseline["full"]["return_pct"]:
        failures.append("full_return")
    if row["full"]["max_drawdown_pct"] < baseline["full"]["max_drawdown_pct"]:
        failures.append("drawdown")
    for index in (1, 2):
        if row["periods"][index]["return_pct"] < baseline["periods"][index]["return_pct"]:
            failures.append(f"period_{index + 1}_return")
    if row["full"]["trade_count"] < 20:
        failures.append("trade_count")
    if row["double_fee"]["return_pct"] <= 0:
        failures.append("double_fee_return")
    return failures


def run_experiment(window: HistoricalResearchWindow) -> dict:
    configs = candidate_configs(window.generation.reference.symbol)
    candles = list(window.candles_by_interval["15m"])
    context = build_hourly_context(candles, list(window.candles_by_interval["1h"]))
    periods = split_periods(window.start_ms, window.end_ms)
    rows = []
    for name, config in configs.items():
        full = run_backtest(candles, context, config=config)
        period_results = [
            metrics(run_backtest([bar for bar in candles if start <= bar.open_time_ms < end], context, config=config))
            for start, end in periods
        ]
        rows.append({
            "name": name,
            "configuration": asdict(config),
            "full": metrics(full),
            "periods": period_results,
            "double_fee": metrics(run_backtest(candles, context, config=replace(config, fee_rate=config.fee_rate * 2))),
            "trades": [asdict(trade) for trade in full.trades],
            "equity_curve": full.equity_curve,
        })
    for row in rows:
        row["retention_failures"] = retention_failures(row, rows[0]) if row["name"] != "baseline" else []
        row["retained_for_forward_observation"] = row["name"] != "baseline" and not row["retention_failures"]
    retained = sorted(
        (row for row in rows if row["retained_for_forward_observation"]),
        key=lambda row: (row["periods"][2]["return_pct"], row["full"]["return_pct"]), reverse=True,
    )
    return {
        "design": "2026-09-13-mu-trend-exit-v1",
        "sample_status": "reused historical sample; not untouched out-of-sample or forward validation",
        "cost_model": "0.0005 per side; double-fee rerun 0.001; no funding, spread, queue or partial-fill model",
        "period_initialization": "10000 equity and no positions each period; fresh 15m indicators; continuous closed-hour context",
        "periods": [{"start_ms": start, "end_ms": end} for start, end in periods],
        "provenance": json.loads(window.provenance({name: asdict(config) for name, config in configs.items()})),
        "preferred_for_forward_observation": retained[0]["name"] if retained else None,
        "strategies": rows,
    }


def render_markdown(report: dict) -> str:
    preferred = report["preferred_for_forward_observation"] or "无候选满足固定条件"
    lines = ["# MU 趋势与退出实验", "", f"优先前向观察：**{preferred}**。此结果不自动切换运行策略。",
             "", "重复使用的历史样本；三个分段均不是未见样本。所有收益已扣每侧 0.05% 手续费，成本压力重跑为每侧 0.10%。未建模资金费、盘口和成交概率。",
             "", "初始资金 10,000，5x，20/20/20/40% 仓位阶梯。分段各自重新开始，15m 指标独立初始化，1h 仅使用已收盘状态。",
             "", "| 配置 | 全期收益 | 最大回撤 | 笔数 | 前段 | 中段 | 末段 | 双费率收益 | 未满足条件 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in report["strategies"]:
        full = row["full"]
        values = [f"{part['return_pct']:.2%}" for part in row["periods"]]
        verdict = ", ".join(row["retention_failures"]) or ("对照" if row["name"] == "baseline" else "保留观察")
        lines.append(f"| {row['name']} | {full['return_pct']:.2%} | {full['max_drawdown_pct']:.2%} | {full['trade_count']} | "
                     + " | ".join(values) + f" | {row['double_fee']['return_pct']:.2%} | {verdict} |")
    lines += ["", "条件：全期收益提升、回撤不恶化、中段及末段各不退步、至少 20 笔、双费率收益为正。保留后按末段收益、全期收益依次排序。",
              "", "完整配置、每笔交易、权益路径、分段时间和数据身份见同目录 experiment.json。剔除前五盈利交易的净利润只用于衡量利润集中度，不是删除交易后的重新回测。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--days", type=int, default=179)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    json_path, md_path = args.output_dir / "experiment.json", args.output_dir / "experiment.md"
    try:
        validate_replay_outputs(args.data_dir, json_path, md_path)
        window = load_historical_window(data_dir=args.data_dir, generation_id=args.generation_id, symbol=args.symbol, days=args.days)
        report = run_experiment(window)
    except (HistoricalGenerationError, ValueError) as exc:
        parser.error(str(exc))
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(payload + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))


if __name__ == "__main__":
    main()
