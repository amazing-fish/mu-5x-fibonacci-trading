"""Short-term MU research using the repository-linked D1/H1 MACD study."""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.experiments.portfolio_research import daily_equity
from mu_strategy.experiments.mu_source_adaptation import events_to_targets, load_stock
from mu_strategy.experiments.source_strategies import metrics
from mu_strategy.experiments.strategy_ladder import CandidateDefinition, DEFAULT_MU_INSTRUMENT, run_long_only_candidate
from mu_strategy.indicators import macd
from mu_strategy.research.historical_data import load_historical_window, validate_replay_outputs
from mu_strategy.strategies.registry import baseline_strategy_group

HOUR = 3600000
DAY = 24*HOUR
MODES = ('h1_cross','d1_h1_cross','d1_h1_one_bar','d1_h1_red_exit')
SOURCE = 'https://quantpedia.com/how-to-design-a-simple-multi-timeframe-trend-strategy-on-bitcoin/'
REPO = 'https://github.com/paperswithbacktest/awesome-systematic-trading/blob/4e23dd84c9ff746ddfcbc856316bcbcef0855b81/README.md'
OVERNIGHT_SOURCE = 'https://github.com/paperswithbacktest/awesome-systematic-trading/blob/4e23dd84c9ff746ddfcbc856316bcbcef0855b81/static/strategies/market-sentiment-and-an-overnight-anomaly.py'
BTC_SOURCE = OVERNIGHT_SOURCE.rsplit('/',1)[0]+'/intraday-seasonality-in-bitcoin.py'


def fresh_window_targets(target,start):
    """A new account waits for a fresh entry if the signal was already held."""
    result = dict(target)
    prior = False
    for timestamp in sorted(target):
        if timestamp < start:
            prior = target[timestamp]
        else:
            break
    armed = not prior
    for timestamp in sorted(target):
        if timestamp < start:
            continue
        if not target[timestamp]:
            armed = True
        result[timestamp] = bool(target[timestamp] and armed)
    return result


def vix_publications(raw,session_dates):
    publications = []
    for timestamp,value in zip(raw['timestamp'],raw['indicators']['quote'][0]['close']):
        day = datetime.fromtimestamp(timestamp,timezone.utc).date()
        if value is None:
            if day in session_dates:
                raise ValueError(f'Missing VIX on equity session {day}')
            continue  # Provider includes null rows for verified equity holidays.
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid VIX close')
        # Yahoo VIX timestamps denote the start of its extended session, not
        # publication of its close. Only expose that close the following day.
        available = datetime.combine(day+timedelta(days=1),time.min,ZoneInfo('America/New_York'))
        publications.append((int(available.timestamp()*1000),float(value)))
    if [t for t,_ in publications] != sorted({t for t,_ in publications}):
        raise ValueError('VIX publications must be unique and ordered')
    return publications


def overnight_events(quarter,calendar,vix):
    """Two observable source legs; BMS is not invented or silently imputed.

    MU latest completed 15m replaces source SPY minute quote at 15:44 ET.
    Original source compares each current observation to its previous20 samples,
    then appends it. Entry is regular close; exit next regular session open.
    """
    close_times = [b.open_time_ms+900000 for b in quarter]
    vix_times = [t for t,_ in vix]
    histories = {'overnight_price20':[],'overnight_vix20':[]}
    events = {s:[] for s in histories}
    events['overnight_unfiltered'] = []
    for bar,next_bar in zip(calendar,calendar[1:]):
        close_time = bar.open_ms+390*60000
        observed_at = close_time-16*60000
        j = bisect.bisect_right(close_times,observed_at)-1
        k = bisect.bisect_right(vix_times,observed_at)-1
        if j < 0 or observed_at-close_times[j] > 900000:
            continue
        if k < 0 or (datetime.fromtimestamp(observed_at/1000,timezone.utc).date()-
                     datetime.fromtimestamp(vix_times[k]/1000,timezone.utc).date()).days > 3:
            raise ValueError('VIX unavailable for source overnight observation')
        values = {'overnight_price20':quarter[j].close,'overnight_vix20':vix[k][1]}
        for name,value in values.items():
            history = histories[name]
            ready = len(history) >= 20
            signal = ready and (value > statistics.fmean(history[-20:]) if name=='overnight_price20'
                                else value < statistics.fmean(history[-20:]))
            events[name] += [(close_time,signal),(next_bar.open_ms,False)]
            history.append(value)
        events['overnight_unfiltered'] += [(close_time,True),(next_bar.open_ms,False)]
    return {s:sorted(e) for s,e in events.items()}


def macd_targets(bars, mode):
    if mode not in MODES:
        raise ValueError(mode)
    if not bars or any(b.open_time_ms%HOUR for b in bars) or any(b.open_time_ms-a.open_time_ms != HOUR for a,b in zip(bars,bars[1:])):
        raise ValueError('Contiguous whole UTC hours required')
    groups = defaultdict(list)
    for bar in bars:
        groups[bar.open_time_ms//DAY*DAY].append(bar)
    daily = [(day+DAY,group[-1].close) for day,group in sorted(groups.items()) if len(group)==24]
    closes_at = [t for t,_ in daily]
    _,_,daily_hist = macd([v for _,v in daily],12,26,9)
    _,_,hourly_hist = macd([b.close for b in bars],12,26,9)
    held, entry_index, targets = False, -1, {}
    for i,bar in enumerate(bars):
        j = i-1  # Only the previous fully closed hourly candle is observable.
        if j < 33:
            targets[bar.open_time_ms] = False
            continue
        d = bisect.bisect_right(closes_at,bar.open_time_ms)-1
        daily_ok = d >= 33 and daily_hist[d] > 0
        cross_up = hourly_hist[j] > 0 and hourly_hist[j-1] <= 0
        cross_down = hourly_hist[j] < 0 and hourly_hist[j-1] >= 0
        if held:
            if mode in ('h1_cross','d1_h1_cross') and cross_down:
                held = False
            elif mode == 'd1_h1_one_bar' and i > entry_index:
                held = False
            elif mode == 'd1_h1_red_exit' and bars[j].close < bars[j].open:
                held = False
        elif cross_up and (mode == 'h1_cross' or daily_ok):
            held,entry_index = True,i
        targets[bar.open_time_ms] = held
    return targets


def frequency(result,start,end):
    completed = [t for t in result.trades if t.exit_reason != 'end_of_data']
    weeks = (end-start)/(7*DAY)
    holds = [(t.exit_time_ms-t.entry_time_ms)/HOUR for t in completed]
    # Count activity on calendar weeks separately from the average rate.
    def week(t):
        d=datetime.fromtimestamp(t/1000,timezone.utc).isocalendar()
        return f'{d.year}-W{d.week:02d}'
    activity = defaultdict(int)
    for t in result.trades:
        activity[week(t.entry_time_ms)] += 1
        if t.exit_reason != 'end_of_data':
            activity[week(t.exit_time_ms)] += 1
    present = {week(t) for t in range(start,end,DAY)}
    entries = [t.entry_time_ms for t in result.trades]
    gaps = [(b-a)/DAY for a,b in zip([start]+entries,entries+[end])]
    return {'completed_trades':len(completed),'completed_per_week':len(completed)/weeks,
            'meets_weekly_minimum':len(completed)/weeks >= 1,
            'median_holding_hours':statistics.median(holds) if holds else None,
            'max_holding_hours':max(holds,default=0),'longest_entry_gap_days':max(gaps,default=(end-start)/DAY),
            'active_calendar_weeks':len(present & activity.keys()),'calendar_weeks':len(present),
            'weekly_operations':{w:activity.get(w,0) for w in sorted(present)}}


def summarize(result,start,end,label_offset):
    curve = daily_equity(result,start,end,label_offset)
    return {**metrics(curve,365),**frequency(result,start,end),'curve':curve,
            'trades':[{'entry':t.entry_time_iso,'exit':t.exit_time_iso,'entry_price':t.entry_price,
                       'exit_price':t.exit_price,'pnl':t.pnl,'fees':t.fees,'reason':t.exit_reason} for t in result.trades]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--generation-id',required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--stock-calendar',type=Path,required=True)
    parser.add_argument('--vix',type=Path,required=True)
    args = parser.parse_args()
    validate_replay_outputs(args.data_dir,args.output_dir)
    window = load_historical_window(data_dir=args.data_dir,generation_id=args.generation_id,symbol='MU-USDT-SWAP',days=179)
    hourly,quarter = list(window.candles_by_interval['1h']),list(window.candles_by_interval['15m'])
    targets = {mode:macd_targets(hourly,mode) for mode in MODES}
    calendar = load_stock(args.stock_calendar)
    vix_raw = json.loads(args.vix.read_text(encoding='utf-8'))['chart']['result'][0]
    if vix_raw['meta']['symbol'] != '^VIX':
        raise ValueError('Expected VIX source')
    vix = vix_publications(vix_raw,{datetime.fromtimestamp(b.open_ms/1000,timezone.utc).date() for b in calendar})
    targets.update({name:events_to_targets(hourly,events) for name,events in overnight_events(quarter,calendar,vix).items()})
    targets['btc_clock'] = {b.open_time_ms:datetime.fromtimestamp(b.open_time_ms/1000,timezone.utc).hour>=22 for b in hourly}
    start,end = window.start_ms+35*DAY,window.end_ms
    periods = {'first':(start,start+48*DAY),'validation':(start+48*DAY,start+96*DAY),
               'final':(start+96*DAY,end),'full':(start,end)}
    context = build_hourly_context(quarter,hourly)
    report = {'source_repository':REPO,'source_rules':SOURCE,'overnight_source':OVERNIGHT_SOURCE,
        'protocol':{'execution_symbol':'MU-USDT-SWAP','warmup_days':35,'macd':[12,26,9],
            'daily_boundary':'UTC midnight, completed daily candle only','frequency_min_completed_per_week':1,
            'primary_objective':'net validation return among source candidates positive after stress in first+validation and meeting weekly frequency in both',
            'parameter_search':'none','fee_bps_per_side':5,'stress_fee_bps_per_side':10,'stress_slippage_ticks':1},
        'periods':periods,'results':{},
        'limitations':['BTC source transferred to MU; not original BTC replication.',
            'Article first describes crossover exit, later one-hour holding. Both interpretations are separately reported.',
            'Red-candle exit executes next hour open rather than assuming the already-known close is executable.',
            'EMA seeded at first observation; 35 daily bars preheat, not source engine bitwise equivalence.',
            'Overnight price20 and VIX20 are isolated source legs at1x, not full three-factor strategy. Public BMS ends2021; full BMS strategy not tested.',
            'Overnight uses MU15:30 ET snapshot for15:44 decision, regular16:00 ET entry and next09:30 ET exit rounded forward to whole UTC hour. Calendar is original MU stock sessions.',
            'Source candidates1x; current baseline5x is separately labelled. No funding, spread, queue, partial fills.',
            'Historical window was previously examined; split is research evidence, not unseen prospective performance.']}
    for phase,(lo,hi) in periods.items():
        rows = {}
        for scenario,fee,slip in (('base',5,0),('stress',10,1)):
            results = {}
            for mode,target in targets.items():
                source = OVERNIGHT_SOURCE if mode.startswith('overnight_') else BTC_SOURCE if mode=='btc_clock' else SOURCE
                result = run_long_only_candidate(hourly,definition=CandidateDefinition(mode,'custom',mode,source),
                    fee_bps_per_side=fee,slippage_ticks=slip,instrument=DEFAULT_MU_INSTRUMENT,
                    execution_start_time_ms=lo,execution_end_time_ms=hi,target_long_by_open_time=fresh_window_targets(target,lo))
                results[mode] = summarize(result,lo,hi,0)
            candles = [b for b in quarter if lo<=b.open_time_ms<hi]
            for leverage in (1,5):
                config = replace(baseline_strategy_group().config,leverage=leverage,fee_rate=fee/10000)
                result = run_backtest(candles,context,config=config,starting_equity=10000)
                results[f'baseline_{leverage}x'] = summarize(result,lo,hi,900000)
            rows[scenario] = results
        report['results'][phase] = rows
    eligible = [s for s in targets if s != 'overnight_unfiltered' and all(report['results'][p]['base'][s]['meets_weekly_minimum'] and
        report['results'][p]['stress'][s]['return']>0 for p in ('first','validation'))]
    chosen = max(eligible,key=lambda s:report['results']['validation']['base'][s]['return']) if eligible else None
    report['selection'] = {'eligible':eligible,'chosen':chosen,'final_used_for_selection':False}
    report['provenance'] = json.loads(window.provenance(report['protocol']))
    import hashlib
    report['external_input_sha256'] = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.stock_calendar,args.vix)}
    candidate = {'strategy_id':chosen,'status':'research_only','execution_symbol':'MU-USDT-SWAP',
        'notional_to_entry_equity':1,'selection':report['selection'],'protocol':report['protocol'],
        'rule_source':OVERNIGHT_SOURCE if chosen and chosen.startswith('overnight_') else BTC_SOURCE if chosen=='btc_clock' else SOURCE,
        'rules':{'h1_cross':{'entry':'completed hourly MACD12/26/9 upward cross, execute next open','exit':'downward cross, execute next open'},
            'd1_h1_cross':{'entry':'h1_cross with previous completed UTC daily MACD histogram positive','exit':'hourly downward cross, execute next open'},
            'd1_h1_one_bar':{'entry':'h1_cross with previous completed UTC daily MACD histogram positive','exit':'one hour after entry'},
            'd1_h1_red_exit':{'entry':'h1_cross with previous completed UTC daily MACD histogram positive','exit':'first held hourly close below open, execute next open'},
            'btc_clock':{'entry':'22:00 UTC','exit':'00:00 UTC'},
            'overnight_vix20':{'signal':'previous published VIX close < mean of previous20 regular-session observations',
                'entry':'16:00 America/New_York','exit':'next regular session09:30 ET rounded to10:00 ET',
                'source_adaptation':'isolated VIX leg applied to MU at1x; original weight1/3'},
            'overnight_price20':{'signal':'latest closed MU 15m price at15:44 ET > mean of previous20 regular-session observations',
            'entry':'16:00 America/New_York regular equity session close',
            'exit':'next regular equity session09:30 ET, replay rounds forward to10:00 ET',
            'weekend_holding':True,'new_account':'wait for next fresh entry signal',
            'source_adaptation':'isolated SPY-price leg applied to MU at1x; original three legs are each1/3'}}.get(chosen),
        'evaluation':{p:{scenario:{k:v for k,v in report['results'][p][scenario][chosen].items() if k not in ('curve','trades')}
            for scenario in ('base','stress')} for p in periods} if chosen else None}
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'candidate.json').write_text(json.dumps(candidate,indent=2),encoding='utf-8')
    (args.output_dir/'short-term.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    lines = ['# MU 短线来源策略：每周至少一笔完整交易','',f'候选选择：{chosen}；35天预热后144天，48/48/48天分段。', '',
        '|策略|144天收益|日末回撤|完成笔数|平均每周|持仓中位小时|首段|中段|末段|末段压力|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for s,row in report['results']['full']['base'].items():
        parts = [report['results'][p]['base'][s]['return'] for p in ('first','validation','final')]
        parts.append(report['results']['final']['stress'][s]['return'])
        lines.append(f"| {s} | {row['return']:.2%} | {row['drawdown']:.2%} | {row['completed_trades']} | {row['completed_per_week']:.2f} | {row['median_holding_hours']} | "+' | '.join(f'{v:.2%}' for v in parts)+' |')
    lines += ['', '一次买入再卖出才计一笔；数据末端强制结算不计自然完成次数。','',*['- '+s for s in report['limitations']]]
    (args.output_dir/'short-term.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('\n'.join(lines[:15]))


if __name__ == '__main__':
    main()
