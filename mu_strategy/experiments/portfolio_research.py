"""MU cross-mechanism research with chronological parameter/portfolio selection.

Protocol: docs/plans/2026-09-13-mu-portfolio.md. Research only, no release or runtime writes.
"""
from __future__ import annotations

import argparse
import html
import itertools
import json
import math
import statistics
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.experiments.strategy_ladder import CandidateDefinition, DEFAULT_MU_INSTRUMENT, run_long_only_candidate
from mu_strategy.indicators import ema, rsi
from mu_strategy.market_data.utils import DAY_MS
from mu_strategy.models import BacktestResult, Candle
from mu_strategy.research.historical_data import load_historical_window, validate_replay_outputs
from mu_strategy.research.robustness import trade_concentration
from mu_strategy.strategies.registry import strategy_group_registrations


@dataclass(frozen=True)
class SignalSpec:
    name: str
    family: str
    period: int = 24
    slow: int = 168
    trend_filter: bool = False


def signal_specs() -> list[SignalSpec]:
    specs = [SignalSpec(f"momentum_{p}h", "momentum", p) for p in (24,96,168)]
    specs += [SignalSpec(f"ema_{fast}_{slow}h", "ema", fast, slow) for fast,slow in ((12,48),(24,96),(48,168))]
    specs += [SignalSpec(f"breakout_{p}h", "breakout", p) for p in (24,72,168)]
    for family, periods in (("zscore",(24,72,168)),("rsi",(2,7,14))):
        specs += [SignalSpec(f"{family}_{p}h{'_trend' if trend else ''}",family,p,168,trend)
                  for p in periods for trend in (False,True)]
    return specs + [SignalSpec("buy_hold", "benchmark")]


def build_targets(candles: list[Candle], spec: SignalSpec) -> dict[int, bool]:
    closes = [bar.close for bar in candles]
    fast, slow, oscillator = ema(closes,spec.period), ema(closes,spec.slow), rsi(closes,spec.period)
    targets, state = {}, False
    for index, bar in enumerate(candles):
        j = index-1  # This is the last CLOSED hour before the execution open.
        if j < 0:
            targets[bar.open_time_ms] = False
            continue
        price = closes[j]
        if spec.family == "benchmark":
            state = True
        elif spec.family == "momentum":
            state = j >= spec.period and price > closes[j-spec.period]
        elif spec.family == "ema":
            state = j+1 >= spec.slow and fast[j] > slow[j]
        elif spec.family == "breakout":
            if j >= spec.period:
                if price > max(c.high for c in candles[j-spec.period:j]):
                    state = True
                elif price < min(c.low for c in candles[j-spec.period//2:j]):
                    state = False
        elif spec.family in ("zscore","rsi"):
            ready = j >= spec.period and (not spec.trend_filter or j+1 >= spec.slow)
            trend_ok = not spec.trend_filter or price > slow[j]
            if not ready or not trend_ok:
                state = False
            elif spec.family == "rsi":
                if oscillator[j] <= 20:
                    state = True
                elif oscillator[j] >= 55:
                    state = False
            else:
                sample = closes[j-spec.period+1:j+1]
                mean, std = statistics.fmean(sample), statistics.pstdev(sample)
                if std > 0 and price <= mean-1.5*std:
                    state = True
                elif price >= mean:
                    state = False
        else:
            raise ValueError(f"unsupported signal family: {spec.family}")
        targets[bar.open_time_ms] = state
    return targets


def daily_equity(result: BacktestResult, start: int, end: int, label_offset: int) -> list[float]:
    # Fibonacci labels events by 15m open; local candidates already label closes.
    samples = [(time+label_offset,value) for time,value in result.equity_curve]
    values, cursor, current = [result.starting_equity], 0, result.starting_equity
    for boundary in range(start+DAY_MS,end+1,DAY_MS):
        while cursor < len(samples) and samples[cursor][0] <= boundary:
            current = samples[cursor][1]
            cursor += 1
        values.append(current)
    return values


def curve_metrics(curve: list[float]) -> dict:
    peak, drawdown = curve[0], 0.
    for value in curve:
        peak = max(peak,value)
        drawdown = min(drawdown,value/peak-1)
    return {"return_pct":curve[-1]/curve[0]-1,"daily_max_drawdown_pct":drawdown}


def combine_equity(curves: dict[str,list[float]], weights: dict[str,float]) -> list[float]:
    if not weights or any(not math.isfinite(w) or w < 0 for w in weights.values()) or not math.isclose(sum(weights.values()),1.):
        raise ValueError("weights must be nonnegative and sum to one capital budget")
    lengths = {len(curves[name]) for name in weights}
    if len(lengths) != 1 or not next(iter(lengths)) or any(curves[name][0] <= 0 for name in weights):
        raise ValueError("aligned positive-start equity curves are required")
    return [10000*sum(weight*curves[name][i]/curves[name][0] for name,weight in weights.items()) for i in range(next(iter(lengths)))]


def select_portfolio(rows: list[dict]) -> dict:
    winners = {}
    for row in rows:
        if row["family"] == "benchmark":
            continue
        training = row["periods"][0]
        metrics = curve_metrics(training["daily_equity"])
        if metrics["return_pct"] <= 0 or training["trade_count"] < 3:
            continue
        score = metrics["return_pct"] + metrics["daily_max_drawdown_pct"]
        if row["family"] not in winners or score > winners[row["family"]][0]:
            winners[row["family"]] = (score,row["name"])
    names = sorted(item[1] for item in winners.values())
    curves = {row["name"]:row["periods"][1]["daily_equity"] for row in rows}
    curves["cash"] = [10000.]*len(curves["baseline"])
    allocations = [{"cash":1.}]
    for name in names:
        allocations.append({name:1.})
        allocations += [{name:w,"cash":1-w} for w in (.25,.5,.75)]
    for a,b in itertools.combinations(names,2):
        allocations += [{a:w,b:1-w} for w in (.25,.5,.75)]
    allocations += [{name:1/3 for name in members} for members in itertools.combinations(names,3)]
    cap = curve_metrics(curves["baseline"])["daily_max_drawdown_pct"]
    candidates = [{"weights":weights,**curve_metrics(combine_equity(curves,weights))} for weights in allocations]
    eligible = [item for item in candidates if item["daily_max_drawdown_pct"] >= cap-1e-12]
    chosen = max(eligible,key=lambda item:(item["return_pct"],item["daily_max_drawdown_pct"]))
    frontier = [item for item in candidates if not any(
        other["return_pct"] >= item["return_pct"] and other["daily_max_drawdown_pct"] >= item["daily_max_drawdown_pct"]
        and (other["return_pct"] > item["return_pct"] or other["daily_max_drawdown_pct"] > item["daily_max_drawdown_pct"])
        for other in candidates)]
    return {"family_winners":{family:value[1] for family,value in winners.items()},
            "weights":chosen["weights"],"validation_drawdown_limit":cap,
            "frontier":sorted(frontier,key=lambda item:item["daily_max_drawdown_pct"],reverse=True),
            "allocations":sorted(candidates,key=lambda item:item["return_pct"],reverse=True)}


def daily_returns(curve: list[float]) -> list[float]:
    return [right/left-1 for left,right in zip(curve,curve[1:])]


def correlation(a: list[float], b: list[float]) -> float | None:
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    numerator = sum((x-ma)*(y-mb) for x,y in zip(a,b))
    denominator = math.sqrt(sum((x-ma)**2 for x in a)*sum((y-mb)**2 for y in b))
    return numerator/denominator if denominator else None


def run_research(window, progress=print) -> dict:
    start,end = window.start_ms+8*DAY_MS,window.end_ms
    if end-start <= 120*DAY_MS:
        raise ValueError("research requires more than 128 input days")
    periods = [(start,start+60*DAY_MS),(start+60*DAY_MS,start+120*DAY_MS),(start+120*DAY_MS,end)]
    hourly = list(window.candles_by_interval["1h"])
    quarter = list(window.candles_by_interval["15m"])
    context = build_hourly_context(quarter,hourly)
    specs = signal_specs()
    configs = {reg.name:replace(reg.build(window.generation.reference.symbol).config,leverage=1.,fee_rate=.0005)
               for reg in strategy_group_registrations() if reg.selectable}
    definitions = [(name,"fibonacci",config) for name,config in configs.items()]
    definitions += [(spec.name,spec.family,spec) for spec in specs]
    rows = []
    for number,(name,family,definition) in enumerate(definitions,1):
        targets = build_targets(hourly,definition) if isinstance(definition,SignalSpec) else None
        row = {"name":name,"family":family,"configuration":asdict(definition)}
        for stress in (False,True):
            results = []
            for left,right in [(start,end),*periods]:
                if targets is None:
                    result = run_backtest([bar for bar in quarter if left <= bar.open_time_ms < right],context,
                                          config=replace(definition,fee_rate=.001 if stress else .0005))
                    offset = 900000
                else:
                    result = run_long_only_candidate(hourly,definition=CandidateDefinition(name,family,name,"portfolio protocol"),
                        fee_bps_per_side=10 if stress else 5,slippage_ticks=1 if stress else 0,
                        instrument=DEFAULT_MU_INSTRUMENT,execution_start_time_ms=left,execution_end_time_ms=right,
                        target_long_by_open_time=targets)
                    offset = 0
                curve = daily_equity(result,left,right,offset)
                results.append({**curve_metrics(curve),"native_max_drawdown_pct":result.max_drawdown_pct,
                    "trade_count":result.trade_count,"win_rate":result.win_rate,"daily_equity":curve,
                    "concentration":asdict(trade_concentration(result.trades))})
            row["stress_full" if stress else "full"] = results[0]
            row["stress_periods" if stress else "periods"] = results[1:]
        rows.append(row)
        progress(f"{number}/{len(definitions)} {name}: full={row['full']['return_pct']:.2%}")
    selection = select_portfolio(rows)
    weights = selection["weights"]
    portfolio = {"weights":weights}
    for key in ("full","stress_full"):
        curves = {row["name"]:row[key]["daily_equity"] for row in rows}
        curves["cash"] = [10000.] * len(next(iter(curves.values())))
        curve = combine_equity(curves,weights)
        portfolio[key] = {**curve_metrics(curve),"daily_equity":curve}
    for key in ("periods","stress_periods"):
        portfolio[key] = []
        for i in range(3):
            curves = {row["name"]:row[key][i]["daily_equity"] for row in rows}
            curves["cash"] = [10000.] * len(next(iter(curves.values())))
            curve = combine_equity(curves,weights)
            portfolio[key].append({**curve_metrics(curve),"daily_equity":curve})
    frontier = []
    for item in selection["frontier"]:
        evaluated = {"weights":item["weights"],"validation":{key:item[key] for key in ("return_pct","daily_max_drawdown_pct")}}
        for key,index,label in (("full",None,"full"),("periods",2,"final"),("stress_periods",2,"stress_final")):
            curves = {row["name"]:(row[key] if index is None else row[key][index])["daily_equity"] for row in rows}
            curves["cash"] = [10000.]*len(next(iter(curves.values())))
            curve = combine_equity(curves,item["weights"])
            evaluated[label] = {**curve_metrics(curve),"daily_equity":curve}
        frontier.append(evaluated)
    chosen_names = sorted(set(selection["family_winners"].values())|{"baseline","buy_hold"})
    returns = {row["name"]:daily_returns(row["full"]["daily_equity"]) for row in rows if row["name"] in chosen_names}
    correlations = {a:{b:correlation(returns[a],returns[b]) for b in chosen_names} for a in chosen_names}
    prices = {bar.open_time_ms+3600000:bar.close for bar in hourly}
    phase_labels = ["up" if prices[t]/prices[t-DAY_MS]-1 > .01 else "down" if prices[t]/prices[t-DAY_MS]-1 < -.01 else "sideways"
                    for t in range(start+DAY_MS,end+1,DAY_MS)]
    contributions = {}
    for name,curve in [(row["name"],row["full"]["daily_equity"]) for row in rows]+[("selected_portfolio",portfolio["full"]["daily_equity"])]:
        contributions[name] = {phase:sum((b-a)/10000 for a,b,label in zip(curve,curve[1:],phase_labels) if label==phase)
                               for phase in ("up","down","sideways")}
    anchors = {}
    for name in ("baseline","baseline_delayed_tighten_smooth"):
        result = run_backtest([bar for bar in quarter if start <= bar.open_time_ms < end],context,config=replace(configs[name],leverage=5.))
        anchors[name] = {"configured_leverage":5.,"trade_count":result.trade_count,
                        **curve_metrics(daily_equity(result,start,end,900000)),"native_max_drawdown_pct":result.max_drawdown_pct}
    return {"protocol":"mu-cross-mechanism-portfolio-v1","sample":"reused historical data; chronological selection, not untouched OOS",
        "leverage":1,"starting_equity":10000,"drawdown_basis":"UTC daily closing net equity",
        "costs":{"base_fee_per_side":.0005,"stress_fee_per_side":.001,"stress_hourly_slippage_ticks":1,"stress_fibonacci_slippage_ticks":0,
                 "omitted":["funding","spread/order queue","partial fills"]},
        "periods":[{"start_ms":a,"end_ms":b} for a,b in periods],
        "provenance":json.loads(window.provenance({"registry":{name:asdict(config) for name,config in configs.items()},"signals":[asdict(spec) for spec in specs]})),
        "selection":selection,"portfolio":portfolio,"frontier":frontier,"strategies":rows,"correlations":correlations,
        "phase_days":{label:phase_labels.count(label) for label in sorted(set(phase_labels))},"phase_contributions":contributions,
        "current_5x_anchors_full_period":anchors}


def markdown(report: dict) -> str:
    p = report["portfolio"]
    lines = ["# MU 跨机制策略与组合研究", "", "固定组合："+", ".join(f"{name} {weight:.1%}" for name,weight in p["weights"].items()),
        "", f"共 {len(report['strategies'])} 个候选；第二段比较 {len(report['selection']['allocations'])} 个资金分配。统一1x配置重跑，组合按初始本金分仓、不再平衡。",
        "", "首8天预热，之后60天选每族参数、60天选资金权重、末段检验。所有历史数据曾被使用，不是完全未见样本。回撤为UTC日末采样，不等于日内最大回撤。",
        "", "| 策略 | 全期收益（事后） | 日末回撤 | 交易数 | 第一段 | 第二段 | 最终段 | 最终段压力成本 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in sorted(report["strategies"],key=lambda item:item["full"]["return_pct"],reverse=True):
        lines.append(f"| {row['name']} | {row['full']['return_pct']:.2%} | {row['full']['daily_max_drawdown_pct']:.2%} | {row['full']['trade_count']} | "
                     +" | ".join(f"{part['return_pct']:.2%}" for part in row["periods"])+f" | {row['stress_periods'][2]['return_pct']:.2%} |")
    lines += ["", "组合各段："+" / ".join(f"收益 {part['return_pct']:.2%}，日末回撤 {part['daily_max_drawdown_pct']:.2%}" for part in p["periods"]),
        f"组合最终段压力成本：{p['stress_periods'][2]['return_pct']:.2%}。全期固定权重事后回放：{p['full']['return_pct']:.2%}，不得作为事前可实现业绩。",
        "", "基础每侧5bps手续费；压力每侧10bps，小时策略另加每侧1tick不利滑点，Fibonacci仅提高费率。未建模funding、盘口队列及部分成交。",
        "", "所有明细、参数、组合分配、相关性和上涨/下跌/震荡日损益贡献见portfolio.json。"]
    lines += ["", "## 收益与回撤取舍", "", "以下仅从同一批已测试分配的第二段结果取非劣组合：不存在另一个分配同时收益更高、回撤更低。最后段只展示，不参与取舍曲线生成。",
              "", "| 初始资金分配 | 第二段收益 | 第二段日末回撤 | 最终段收益 | 最终段日末回撤 | 最终段压力收益 |",
              "|---|---:|---:|---:|---:|---:|"]
    for item in report["frontier"]:
        lines.append("| "+", ".join(f"{n} {w:.0%}" for n,w in item["weights"].items())+
                     f" | {item['validation']['return_pct']:.2%} | {item['validation']['daily_max_drawdown_pct']:.2%} | {item['final']['return_pct']:.2%} | {item['final']['daily_max_drawdown_pct']:.2%} | {item['stress_final']['return_pct']:.2%} |")
    lines += ["", "## 与现有5x配置的同窗口对照", "", "以下只用于理解现有资金效率，未参加1x组合权重选择；回撤仍用日末采样。"]
    for name,item in report["current_5x_anchors_full_period"].items():
        lines.append(f"- {name} 5x：全期收益 {item['return_pct']:.2%}，日末回撤 {item['daily_max_drawdown_pct']:.2%}，{item['trade_count']} 笔。")
    return "\n".join(lines)+"\n"


def render_html(report: dict) -> str:
    portfolio = report["portfolio"]
    names = set(report["selection"]["family_winners"].values())|{"baseline","buy_hold"}
    rows = [row for row in report["strategies"] if row["name"] in names]
    start,end = (report["periods"][2][key] for key in ("start_ms","end_ms"))
    dates = [datetime.fromtimestamp(t/1000,timezone.utc).date().isoformat() for t in range(start,end+1,DAY_MS)]
    traces = [{"x":dates,"y":portfolio["periods"][2]["daily_equity"],"name":"固定组合","line":{"width":4}}]
    traces += [{"x":dates,"y":row["periods"][2]["daily_equity"],"name":row["name"],
                "visible":True if row["name"] in ("baseline","buy_hold") else "legendonly"} for row in rows]
    traces += [{"x":dates,"y":item["final"]["daily_equity"],"name":" + ".join(f"{n} {w:.0%}" for n,w in item["weights"].items()),
                "visible":"legendonly"} for item in report["frontier"] if len(item["weights"]) > 1 and "cash" not in item["weights"]]
    def table(headers, body):
        return "<div class='scroll'><table><thead><tr>"+"".join(f"<th>{html.escape(str(v))}</th>" for v in headers)+"</tr></thead><tbody>"+"".join(
            "<tr>"+"".join(f"<td>{html.escape(str(v))}</td>" for v in line)+"</tr>" for line in body)+"</tbody></table></div>"
    summary_rows = [["固定组合",f"{portfolio['periods'][2]['return_pct']:.2%}",f"{portfolio['periods'][2]['daily_max_drawdown_pct']:.2%}",f"{portfolio['stress_periods'][2]['return_pct']:.2%}"]]
    summary_rows += [[row["name"],f"{row['periods'][2]['return_pct']:.2%}",f"{row['periods'][2]['daily_max_drawdown_pct']:.2%}",f"{row['stress_periods'][2]['return_pct']:.2%}"] for row in rows]
    all_rows = [[row["name"],row["family"],f"{row['full']['return_pct']:.2%}",f"{row['full']['daily_max_drawdown_pct']:.2%}",row["full"]["trade_count"],
                 *[f"{part['return_pct']:.2%}" for part in row["periods"]]] for row in sorted(report["strategies"],key=lambda row:row["full"]["return_pct"],reverse=True)]
    corr = report["correlations"]
    corr_rows = [[a,*["—" if corr[a][b] is None else f"{corr[a][b]:.2f}" for b in corr]] for a in corr]
    phase_rows = [[name,*[f"{value:.2%}" for value in report["phase_contributions"][name].values()]] for name in ["selected_portfolio",*sorted(names)]]
    frontier_rows = [[", ".join(f"{n} {w:.0%}" for n,w in item["weights"].items()),
                     f"{item['validation']['return_pct']:.2%}",f"{item['validation']['daily_max_drawdown_pct']:.2%}",
                     f"{item['final']['return_pct']:.2%}",f"{item['final']['daily_max_drawdown_pct']:.2%}",f"{item['stress_final']['return_pct']:.2%}"] for item in report["frontier"]]
    weights = " + ".join(f"{html.escape(name)} <strong>{weight:.1%}</strong>" for name,weight in portfolio["weights"].items())
    payload = json.dumps(traces,ensure_ascii=False,allow_nan=False).replace("<","\\u003c")
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MU 跨机制策略与组合</title><script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>body{margin:0;background:#f3f4f6;color:#142335;font:15px system-ui}main{max-width:1400px;margin:auto;padding:32px}h1{font-size:32px}h2{margin-top:36px}.lead{font-size:20px;padding:22px;background:#e6f0ed;border-left:5px solid #087d69}p{line-height:1.8;color:#48576a}table{border-collapse:collapse;background:white;min-width:100%;font-variant-numeric:tabular-nums}th,td{text-align:right;padding:12px;border-bottom:1px solid #dde3eb;white-space:nowrap}th{background:#e9eef4}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto}#equity{height:470px}a{color:#087d69}</style><main>
<h1>MU 跨机制策略与资金组合</h1>"""+f"<div class='lead'>{weights}</div>"+f"<p>{len(report['strategies'])} 个候选 · {len(report['selection']['allocations'])} 个资金分配 · 统一 1x 配置重跑 · 同一份 179 天行情</p>"+"""
<p>先用 8 天预热，再用 60 天选每类参数、60 天选资金权重，最后 51 天只检验。以下主图只显示最终检验段。行情曾用于此前研究，不能称为完全未见样本。现金收益设为零；按初始本金分仓，不再平衡。</p>
<h2>最终检验段：固定参数与资金权重</h2><div id="equity"></div>"""+table(["配置","扣费收益","日末回撤","压力成本收益"],summary_rows)+"""
<p>回撤采用 UTC 日末净权益采样，可能漏掉日内低点。基础每侧 5bps；压力每侧 10bps，小时策略额外每侧 1tick 滑点，Fibonacci 只提高费率。未模拟 funding、盘口队列和部分成交。</p>
<h2>更高资金使用率：收益与回撤的取舍</h2><p>仅从同一批第二段资金分配选出非劣点，没有新增参数或权重搜索。最后段不参与选择。</p>"""+table(["资金分配","第二段收益","第二段回撤","最终段收益","最终段回撤","最终段压力收益"],frontier_rows)+"""
<h2>完整候选：全期排序是事后描述</h2>"""+table(["名称","机制","全期收益","日末回撤","交易数","参数筛选段","组合选择段","最终段"],all_rows)+"""
<h2>每日收益相关性：相似指标是否提供了分散</h2><p>全期事后诊断，未输入权重选择。接近 1 表示同涨同跌更明显。</p>"""+table(["相关性",*corr],corr_rows)+"""
<h2>上涨、下跌与震荡日的损益贡献</h2><p>MU 当日价格变化高于 1% / 低于 -1% / 其余；为事后标签，不作交易条件。各列是初始本金收益贡献，合计为全期收益。全期固定组合为事后回放。</p>"""+table(["配置","上涨日贡献","下跌日贡献","震荡日贡献"],phase_rows)+"""
<p><a href="portfolio.json">完整数据 JSON</a> · <a href="portfolio.md">文字报告</a> · <a href="selected-portfolio.json">固定组合配置</a></p>
</main><script>Plotly.newPlot('equity',"""+payload+""",{margin:{t:20},yaxis:{title:'起始本金 10,000'},xaxis:{title:'UTC'},legend:{orientation:'h'}},{responsive:true});</script></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",type=Path,required=True)
    parser.add_argument("--generation-id",required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    args = parser.parse_args()
    paths = [args.output_dir/f"portfolio.{ext}" for ext in ("json","md","html")]
    selected_path = args.output_dir/"selected-portfolio.json"
    try:
        validate_replay_outputs(args.data_dir,*paths,selected_path)
        window = load_historical_window(data_dir=args.data_dir,generation_id=args.generation_id,symbol="MU-USDT-SWAP",days=179)
        report = run_research(window,progress=lambda line:print(line,flush=True))
    except ValueError as exc:
        parser.error(str(exc))
    payload = json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)
    md = markdown(report)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    paths[0].write_text(payload+"\n",encoding="utf-8")
    paths[1].write_text(md,encoding="utf-8")
    paths[2].write_text(render_html(report),encoding="utf-8")
    selection = {"protocol":report["protocol"],"mode":"research_only","allocation":"initial capital sleeves, no rebalancing",
        "weights":report["portfolio"]["weights"],"components":{row["name"]:row["configuration"] for row in report["strategies"] if row["name"] in report["portfolio"]["weights"]},
        "generation_id":args.generation_id,"code_sha256":report["provenance"]["code_sha256"],"costs":report["costs"],"periods":report["periods"],
        "validation_frontier_weights":[item["weights"] for item in report["frontier"]]}
    selected_path.write_text(json.dumps(selection,ensure_ascii=False,allow_nan=False,indent=2)+"\n",encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
