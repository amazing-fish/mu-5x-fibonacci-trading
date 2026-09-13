"""Offline comparison of named strategies from awesome-systematic-trading."""
import argparse
import bisect
import hashlib
import html
import itertools
import json
import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

SOURCE_COMMIT = '4e23dd84c9ff746ddfcbc856316bcbcef0855b81'
SOURCE_ROOT = f'https://github.com/paperswithbacktest/awesome-systematic-trading/blob/{SOURCE_COMMIT}/static/strategies/'
SOURCES = {
    'asset_momentum': 'asset-class-momentum-rotational-system.py',
    'sector_momentum': 'sector-momentum-rotational-system.py',
    'paired_switching': 'paired-switching.py',
    'january_barometer': 'january-barometer.py',
    'asset_trend': 'asset-class-trend-following.py',
}
ASSETS = ('SPY', 'EFA', 'IEF', 'VNQ', 'GSG')
SECTORS = ('VNQ', 'XLK', 'XLE', 'XLV', 'XLF', 'XLI', 'XLB', 'XLY', 'XLP', 'XLU')
BENCHMARKS = ('spy_hold', 'asset_equal', 'stock_bond_60_40')


@dataclass
class Panel:
    dates: list[date]
    opens: dict[str, list[float]]
    closes: dict[str, list[float]]


def targets(panel: Panel, strategy: str) -> list[dict[str, float] | None]:
    """Original universes/parameters; prior close information, next session open.

    A None means no rebalance; an empty dict means liquidate to cash.
    asset_trend follows the archived code's 210-day SMA and fully allocated
    survivors, not its header's 10-month SMA / fixed 20-percent sleeves.
    Its minute-level entry is explicitly approximated by next-open execution.
    """
    if strategy not in (*SOURCES, *BENCHMARKS):
        raise ValueError(strategy)
    signals = [None] * len(panel.dates)
    january_start = None
    for i in range(1, len(panel.dates)):
        day, previous = panel.dates[i], panel.dates[i-1]
        if strategy == 'spy_hold':
            if i == 1:
                signals[i] = {'SPY': 1.0}
            continue
        if (day.year, day.month) == (previous.year, previous.month):
            continue
        if strategy in ('asset_momentum', 'sector_momentum') and i >= 253:
            universe = ASSETS if strategy == 'asset_momentum' else SECTORS
            ranked = sorted(universe, key=lambda s: panel.closes[s][i-1]/panel.closes[s][i-253]-1, reverse=True)
            signals[i] = {s: 1/3 for s in ranked[:3]}
        elif strategy == 'asset_trend' and i >= 210:
            selected = [s for s in ASSETS if panel.closes[s][i-1] > statistics.fmean(panel.closes[s][i-210:i])]
            signals[i] = {s: 1/len(selected) for s in selected}
        elif strategy == 'paired_switching' and day.month % 3 == 0:
            first = bisect.bisect_left(panel.dates, day - timedelta(days=90))
            if first < i-1 and panel.dates[0] <= day-timedelta(days=90):
                performance = {s: (panel.closes[s][i-1]-panel.closes[s][first])/panel.closes[s][i-1] for s in ('SPY','AGG')}
                signals[i] = {'SPY' if performance['SPY'] > performance['AGG'] else 'AGG': 1.0}
        elif strategy == 'january_barometer':
            if day.month == 1:
                january_start = panel.closes['SPY'][i-1]
                signals[i] = {'SPY': 1.0}
            elif day.month == 2 and january_start is not None:
                signals[i] = {'SPY' if panel.closes['SPY'][i-1] > january_start else 'BIL': 1.0}
        elif strategy == 'asset_equal':
            signals[i] = {s: 1/len(ASSETS) for s in ASSETS}
        elif strategy == 'stock_bond_60_40':
            signals[i] = {'SPY': .6, 'AGG': .4}
    return signals


def simulate(panel: Panel, signals, start: int, end: int, cost: float):
    """Fractional total-return units, self-financing rebalances and liquidation.

    Cost is a proportional per-side fee plus slippage allowance. No leverage,
    lending, funding or interest on idle cash. Each interval starts fresh and
    establishes the latest known target, instead of importing prior profits.
    """
    if not 0 <= start < end <= len(panel.dates) or not 0 <= cost < 1:
        raise ValueError('Invalid replay bounds/cost')
    cash, shares, curve, fees, orders, min_cash = 10000., {}, [10000.], 0., 0, 10000.
    initial = next((s for s in reversed(signals[:start+1]) if s is not None), {})
    for i in range(start, end):
        target = initial if i == start else signals[i]
        if target is not None:
            if any(not math.isfinite(w) or w < 0 for w in target.values()) or sum(target.values()) > 1+1e-12:
                raise ValueError('Target must be a nonnegative single capital budget')
            current = {s:q*panel.opens[s][i] for s,q in shares.items()}
            equity = cash + sum(current.values())
            symbols = current.keys() | target.keys()
            # Solve NAV after costs before sizing, so fees never borrow cash.
            low, high = 0., equity
            for _ in range(60):
                net = (low+high)/2
                paid = cost * sum(abs(net*target.get(s,0)-current.get(s,0)) for s in symbols)
                if net+paid > equity:
                    high = net
                else:
                    low = net
            net = low
            paid = cost * sum(abs(net*target.get(s,0)-current.get(s,0)) for s in symbols)
            orders += sum(abs(net*target.get(s,0)-current.get(s,0)) > 1e-6 for s in symbols)
            shares = {s:net*w/panel.opens[s][i] for s,w in target.items() if w > 0}
            cash = equity-paid-net*sum(target.values())
            fees += paid
            min_cash = min(min_cash, cash)
        value = sum(q*panel.closes[s][i] for s,q in shares.items())
        if i == end-1:
            fees += cost*value
            orders += len(shares)
            value *= 1-cost
        curve.append(cash+value)
    return {'curve':curve, 'costs':fees, 'orders':orders, 'min_cash':min_cash, **metrics(curve)}


def metrics(curve):
    peak, drawdown = curve[0], 0.
    for value in curve:
        peak = max(peak, value)
        drawdown = max(drawdown, 1-value/peak)
    changes = [b/a-1 for a,b in zip(curve,curve[1:])]
    sd = statistics.pstdev(changes) if len(changes) > 1 else 0
    return {'return':curve[-1]/curve[0]-1, 'drawdown':drawdown,
            'cagr':(curve[-1]/curve[0])**(252/max(1,len(changes)))-1,
            'sharpe_zero_rf':statistics.fmean(changes)/sd*math.sqrt(252) if sd else 0.}


def combine(curves, weights):
    return [sum(weights.get(s,0)*curve[i] for s,curve in curves.items())
            for i in range(len(next(iter(curves.values()))))]


def choose_weights(curves, drawdown_limit):
    """Select 25-percent fixed initial sleeves on validation data only."""
    names = list(curves)
    candidates = []
    for units in itertools.product(range(5), repeat=len(names)):
        if sum(units) != 4:
            continue
        weights = {s:u/4 for s,u in zip(names, units) if u}
        result = metrics(combine(curves,weights))
        candidates.append({'weights':weights, **result})
    eligible = [c for c in candidates if c['drawdown'] <= drawdown_limit+1e-12]
    if not eligible:
        raise ValueError('No source-strategy portfolio meets the comparator drawdown')
    return {**max(eligible,key=lambda c:(c['return'],-c['drawdown'])), 'candidates':len(candidates)}


def load_panel(path: Path) -> tuple[Panel, dict]:
    raw = json.loads(path.read_text(encoding='utf-8'))
    dates = [date.fromisoformat(d) for d in raw['dates']]
    if not dates or dates != sorted(set(dates)):
        raise ValueError('Dates must be unique, chronological and nonempty')
    required = set(ASSETS+SECTORS+('AGG','BIL'))
    for key in ('opens','closes'):
        if not required <= raw[key].keys():
            raise ValueError('Original strategy universe is incomplete')
        for values in raw[key].values():
            if len(values) != len(dates) or any(not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError('Invalid or missing daily prices')
    return Panel(dates,raw['opens'],raw['closes']), raw['provenance']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--panel',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args = parser.parse_args()
    if args.panel.resolve().is_relative_to(args.output_dir.resolve()):
        raise ValueError('Outputs must be separate from source input')
    panel, provenance = load_panel(args.panel)
    intervals = {'development':('2009-01-01','2018-01-01'),
                 'validation':('2018-01-01','2022-01-01'),
                 'holdout':('2022-01-01','2026-09-12')}
    all_signals = {s:targets(panel,s) for s in (*SOURCES,*BENCHMARKS)}
    results = {}
    for phase,(start,end) in intervals.items():
        lo,hi = (bisect.bisect_left(panel.dates,date.fromisoformat(d)) for d in (start,end))
        if hi-lo < 200 or lo < 253:
            raise ValueError(f'Insufficient original-universe history: {phase}')
        results[phase] = {'dates':[d.isoformat() for d in panel.dates[lo:hi]],
            'base':{s:simulate(panel,signal,lo,hi,.0005) for s,signal in all_signals.items()},
            'stress':{s:simulate(panel,signal,lo,hi,.0015) for s,signal in all_signals.items()}}
    validation = results['validation']['base']
    chosen = choose_weights({s:validation[s]['curve'] for s in SOURCES},validation['spy_hold']['drawdown'])
    for result in results.values():
        for scenario in ('base','stress'):
            curves = {s:result[scenario][s]['curve'] for s in SOURCES}
            for name, weights in (('selected',chosen['weights']),('source_equal',{s:.2 for s in SOURCES})):
                curve = combine(curves,weights)
                result[scenario][name] = {'curve':curve, **metrics(curve)}
    report = {'mode':'source_rule_reproduction_research_only', 'source_commit':SOURCE_COMMIT,
              'sources':{s:SOURCE_ROOT+f for s,f in SOURCES.items()},
              'input_sha256':hashlib.sha256(args.panel.read_bytes()).hexdigest(),
              'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'data':provenance, 'selection':chosen, 'results':results,
              'execution':'Previous completed daily close -> next session adjusted open; original universes and lookbacks; asset_trend changes minute signal/execution timing to daily.',
              'costs':'5 bps per side base; 15 bps stress, each including proportional slippage allowance.',
              'limitations':['Original ETF universes, NOT MU or OKX performance.',
                  'Yahoo adjusted prices are a total-return proxy; not original QuantConnect feed/fills.',
                  'Daily asset_trend changes intraday signal/execution timing; listed separately from four daily sources.',
                  'Post-selection retrospective holdout, not prospective unseen live evidence.',
                  'Fixed initial sleeves drift; no inter-sleeve rebalancing; idle cash earns zero.',
                  'Close-to-close NAV drawdown excludes intraday lows.']}
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'source-results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    lines = ['# 指定仓库策略：原 ETF 资产池复跑','',f"验证期选出的权重：`{chosen['weights']}`；比较 {chosen['candidates']} 个资金分配。此权重不自动成为推荐，请同时检查后段和等权基准。",'',
             '原参数保留；统一使用已收盘信号、下一交易日开盘成交。资产趋势一项是分钟源码的日线执行适配。收益不是 MU 收益。','',
             '| 策略 | 2009–2017收益 | 2018–2021收益 | 2022至今收益 | 2022至今回撤 | 2022至今年化 | 压力成本收益 |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for s in (*SOURCES,*BENCHMARKS,'source_equal','selected'):
        values = [results[p]['base'][s]['return'] for p in intervals]
        h = results['holdout']['base'][s]
        values += [h['drawdown'],h['cagr'],results['holdout']['stress'][s]['return']]
        lines.append('| '+s+' | '+' | '.join(f'{v:.2%}' for v in values)+' |')
    lines += ['', '## 原源码', '']+[f'- [{s}]({SOURCE_ROOT+f})' for s,f in SOURCES.items()]
    lines += ['', '## 解释范围', '']+['- '+s for s in report['limitations']]
    (args.output_dir/'source-results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # Self-contained overview; detailed numerical evidence stays in JSON/Markdown.
    labels = dict(zip((*SOURCES,*BENCHMARKS,'source_equal','selected'),
                     ('跨资产动量','行业动量轮动','股债配对切换','一月效应','跨资产趋势（日线适配）',
                      'SPY 买入持有','五资产月度等权','股债 60/40','五策略初始等权','验证期选出的权重')))
    rendered = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>指定仓库策略复跑</title><style>body{max-width:1180px;margin:35px auto;padding:0 20px;font:16px system-ui;background:#f5f7fa;color:#183047;line-height:1.65}svg,table{background:white;width:100%;border:1px solid #ddd}table{border-collapse:collapse;font-size:14px}th,td{padding:12px;text-align:right;border-bottom:1px solid #e4e9ee}td:first-child,th:first-child{text-align:left}a{color:#086eaa}.note{background:#e7eff7;padding:18px;border-radius:10px}</style><h1>指定仓库策略：原资产池复跑</h1><p class="note">原 ETF 标的与源码参数；2007–2026 数据。以下收益不是 MU 收益。资产趋势保留原参数，但分钟执行适配为日线。</p>'
    names = ('selected','spy_hold','source_equal')
    colors = ('#098577','#dd653d','#5360b7')
    curves = [results['holdout']['base'][s]['curve'] for s in names]
    floor, ceiling = min(map(min,curves)),max(map(max,curves))
    rendered += '<h2>2022-01 至 2026-09 权益</h2><p>同起始本金 10,000；'+ ' / '.join(f'<span style="color:{c}">{labels[s]}</span>' for s,c in zip(names,colors))+'</p><svg viewBox="0 0 1100 350" role="img" aria-label="最终检验区间权益曲线">'
    rendered += f'<text x="20" y="18" font-size="13">{ceiling:,.0f}</text><text x="20" y="320" font-size="13">{floor:,.0f}</text><text x="20" y="342" font-size="13">2022-01</text><text x="1010" y="342" font-size="13">2026-09</text>'
    for curve,color in zip(curves,colors):
        points = ' '.join(f'{20+1060*i/(len(curve)-1):.2f},{300-270*(v-floor)/(ceiling-floor or 1):.2f}' for i,v in enumerate(curve))
        rendered += f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/>'
    rendered += '</svg><h2>后段表现与成本</h2><p>基础每侧 5 bps，压力每侧 15 bps；回撤为日末净权益。</p><table><tr><th>策略</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th><th>压力成本收益</th></tr>'
    for s,label in labels.items():
        base = results['holdout']['base'][s]
        values = (base['return'],base['cagr'],base['drawdown'],results['holdout']['stress'][s]['return'])
        rendered += '<tr><td>'+label+'</td>'+''.join(f'<td>{v:.2%}</td>' for v in values)+'</tr>'
    rendered += '</table><h2>权重选择怎么解读</h2><p>仅用 2018–2021 年比较 70 个资金分配，选出的权重为 '+html.escape(str(chosen['weights']))+'。后段用于检验，不用于重新挑赢家。等权组合也必须与 SPY、60/40 比较，不能把低回撤直接说成更高收益。</p><h2>原策略源码</h2><ul>'
    rendered += ''.join(f'<li><a href="{SOURCE_ROOT+f}">{labels[s]}</a></li>' for s,f in SOURCES.items())
    rendered += '</ul><p>原始配置、所有区间、输入指纹和执行差异见 <a href="source-results.json">JSON 证据</a>；完整表格见 <a href="source-results.md">Markdown 报告</a>。</p></html>'
    (args.output_dir/'source-results.html').write_text(rendered,encoding='utf-8')
    print(json.dumps({'selection':chosen,'holdout':{s:{k:v for k,v in r.items() if k!='curve'} for s,r in results['holdout']['base'].items()}},indent=2))


if __name__ == '__main__':
    main()
