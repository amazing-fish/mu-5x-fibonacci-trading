"""Original-source rules adapted to MU stock signals and MU swap executions."""
import argparse
import bisect
import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.experiments.portfolio_research import daily_equity
from mu_strategy.experiments.source_strategies import SOURCE_ROOT, SECTORS, choose_weights, combine, metrics, load_panel
from mu_strategy.experiments.strategy_ladder import CandidateDefinition, DEFAULT_MU_INSTRUMENT, run_long_only_candidate
from mu_strategy.research.historical_data import load_historical_window, validate_replay_outputs
from mu_strategy.strategies.registry import baseline_strategy_group

DAY_MS = 86400000
SOURCE_FILES = {
    'sma210':'asset-class-trend-following.py',
    'momentum252':'time-series-momentum-effect.py',
    'ath_atr10':'trend-following-effect-in-stocks.py',
    'sector_gate':'sector-momentum-rotational-system.py',
    'january':'january-barometer.py',
    'payday':'payday-anomaly.py',
    'turn_month':'turn-of-the-month-in-equity-indexes.py',
    'btc_clock':'intraday-seasonality-in-bitcoin.py',
}


@dataclass(frozen=True)
class StockBar:
    open_ms: int
    open: float
    high: float
    low: float
    close: float


def events_to_targets(candles, events):
    times = [t for t,_ in events]
    if times != sorted(set(times)):
        raise ValueError('Events must have unique ascending publication times')
    result = {}
    for candle in candles:
        index = bisect.bisect_right(times,candle.open_time_ms)-1
        result[candle.open_time_ms] = bool(events[index][1]) if index >= 0 else False
    return result


def session_date(bar):
    # US session opens are on the same UTC calendar date as the exchange date.
    return datetime.fromtimestamp(bar.open_ms/1000,timezone.utc).date()


def stock_events(bars, strategy):
    if strategy not in ('sma210','momentum252','january','ath_atr10'):
        raise ValueError(strategy)
    closes = [b.close for b in bars]
    events, tr_values = [], []
    january_start, atr, peak, stop, held = None, None, 0., 0., False
    for i,bar in enumerate(bars):
        previous = closes[i-1] if i else bar.close
        tr_values.append(max(bar.high-bar.low,abs(bar.high-previous),abs(bar.low-previous)))
        if i == 9:
            atr = statistics.fmean(tr_values)
        elif i > 9:
            atr = (atr*9+tr_values[-1])/10
        if strategy == 'ath_atr10':
            # Seed the original 10*12*21 session history, then retain the high.
            if i < 2520:
                peak = max(peak,bar.close)
                continue
            if held and bar.close <= stop:
                held = False
            elif not held and bar.close >= peak:
                held, stop = True, bar.close-atr
            if held:
                stop = max(stop,bar.close-atr)
            peak = max(peak,bar.close)
            # EOD exit confirmation is a disclosed change from stop-market.
            events.append((bar.open_ms+405*60000,held))
            continue
        if i == 0 or session_date(bar).month == session_date(bars[i-1]).month:
            continue
        month = session_date(bar).month
        if strategy == 'sma210' and i >= 210:
            events.append((bar.open_ms,previous > statistics.fmean(closes[i-210:i])))
        elif strategy == 'momentum252' and i >= 252:
            # Archived time-series source uses 252 prices (251 return intervals).
            events.append((bar.open_ms,previous > closes[i-252]))
        elif strategy == 'january':
            if month == 1:
                january_start = previous
                events.append((bar.open_ms,True))
            elif month == 2 and january_start is not None:
                events.append((bar.open_ms,previous > january_start))
    return events


def payday_events(bars):
    events, held = [], False
    for bar in bars:
        day = session_date(bar)
        payday = day.replace(day=15)
        if payday.weekday() >= 5:
            payday -= timedelta(days=payday.weekday()-4)
        if day == payday:
            held = True
            events.append((bar.open_ms+389*60000,True))
        elif held:
            events.append((bar.open_ms+389*60000,False))
            held = False
    return events


def month_events(bars):
    grouped = defaultdict(list)
    for bar in bars:
        d = session_date(bar)
        grouped[(d.year,d.month)].append(bar)
    events = []
    for key,group in grouped.items():
        if len(group) >= 3:
            events.append((group[2].open_ms+390*60000,False))
        next_key = (key[0]+(key[1]==12),key[1]%12+1)
        # Do not treat the last partial month as a known complete month.
        if next_key in grouped:
            events.append((group[-1].open_ms,True))
    return sorted(events)


def sector_events(panel,bars):
    events = []
    for i,day in enumerate(panel.dates):
        if i < 253 or day.month == panel.dates[i-1].month:
            continue
        ranked = sorted(SECTORS,key=lambda s:panel.closes[s][i-1]/panel.closes[s][i-253],reverse=True)
        mu_bar = bars.get(day)
        if mu_bar is None:
            raise ValueError(f'Missing MU exchange session {day}')
        events.append((mu_bar.open_ms,'XLK' in ranked[:3]))
    return events


def load_stock(path):
    data = json.loads(path.read_text(encoding='utf-8'))['chart']['result'][0]
    if data['meta']['symbol'] != 'MU':
        raise ValueError('Expected original MU stock data')
    quote,adjusted = data['indicators']['quote'][0],data['indicators']['adjclose'][0]['adjclose']
    bars = []
    for i,timestamp in enumerate(data['timestamp']):
        values = [quote[k][i] for k in ('open','high','low','close')]
        values.append(adjusted[i])
        if any(v is None or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError('Missing/invalid MU stock price')
        factor = adjusted[i]/quote['close'][i]
        bar = StockBar(timestamp*1000,*(v*factor for v in values[:4]))
        if bar.low > min(bar.open,bar.close) or bar.high < max(bar.open,bar.close):
            raise ValueError('Invalid MU OHLC ordering')
        bars.append(bar)
    if [b.open_ms for b in bars] != sorted({b.open_ms for b in bars}):
        raise ValueError('Duplicate/unordered MU stock sessions')
    return bars


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock',type=Path,required=True)
    parser.add_argument('--sector-panel',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--generation-id',required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args = parser.parse_args()
    validate_replay_outputs(args.data_dir,args.output_dir)
    if any(p.resolve().is_relative_to(args.output_dir.resolve()) for p in (args.stock,args.sector_panel)):
        raise ValueError('Research output overlaps external inputs')
    stock = load_stock(args.stock)
    panel,_ = load_panel(args.sector_panel)
    window = load_historical_window(data_dir=args.data_dir,generation_id=args.generation_id,symbol='MU-USDT-SWAP',days=179)
    hourly = list(window.candles_by_interval['1h'])
    event_sets = {s:stock_events(stock,s) for s in ('sma210','momentum252','january','ath_atr10')}
    event_sets.update(payday=payday_events(stock),turn_month=month_events(stock),
                      sector_gate=sector_events(panel,{session_date(b):b for b in stock}))
    targets = {s:events_to_targets(hourly,events) for s,events in event_sets.items()}
    targets['btc_clock'] = {b.open_time_ms:datetime.fromtimestamp(b.open_time_ms/1000,timezone.utc).hour >= 22 for b in hourly}
    targets['buy_hold'] = {b.open_time_ms:True for b in hourly}
    # One specified use of sector rotation for MU: gate its source trend entry.
    targets['ath_atr10_sector'] = {t:state and targets['sector_gate'][t] for t,state in targets['ath_atr10'].items()}
    start,end = window.start_ms,window.end_ms
    periods = {'first':(start,start+60*DAY_MS),'validation':(start+60*DAY_MS,start+120*DAY_MS),
               'final':(start+120*DAY_MS,end),'full':(start,end)}
    results = {}
    for phase,(lo,hi) in periods.items():
        results[phase] = {}
        for scenario,fee,slip in (('base',5,0),('stress',10,1)):
            rows = {}
            for s,target in targets.items():
                definition = CandidateDefinition(s,'custom',s,SOURCE_ROOT+SOURCE_FILES.get(s,'sector-momentum-rotational-system.py'))
                result = run_long_only_candidate(hourly,definition=definition,fee_bps_per_side=fee,slippage_ticks=slip,
                    instrument=DEFAULT_MU_INSTRUMENT,execution_start_time_ms=lo,execution_end_time_ms=hi,target_long_by_open_time=target)
                curve = daily_equity(result,lo,hi,0)
                rows[s] = {**metrics(curve,365),'curve':curve,'trades':result.trade_count,'hourly_drawdown':result.max_drawdown_pct,
                    'trade_ledger':[{'entry':t.entry_time_iso,'exit':t.exit_time_iso,'entry_price':t.entry_price,'exit_price':t.exit_price,
                                     'pnl':t.pnl,'fees':t.fees,'exit_reason':t.exit_reason} for t in result.trades]}
            candles = [b for b in window.candles_by_interval['15m'] if lo <= b.open_time_ms < hi]
            context = build_hourly_context(candles,hourly)
            config = replace(baseline_strategy_group().config,leverage=1,fee_rate=fee/10000)
            result = run_backtest(candles,context,config=config,starting_equity=10000)
            curve = daily_equity(result,lo,hi,900000)
            rows['baseline_1x'] = {**metrics(curve,365),'curve':curve,'trades':result.trade_count,'native_drawdown':result.max_drawdown_pct}
            anchor = run_backtest(candles,context,config=replace(config,leverage=5),starting_equity=10000)
            anchor_curve = daily_equity(anchor,lo,hi,900000)
            rows['baseline_current_5x'] = {**metrics(anchor_curve,365),'curve':anchor_curve,'trades':anchor.trade_count,
                                            'native_drawdown':anchor.max_drawdown_pct}
            rows['cash'] = {'return':0.,'drawdown':0.,'curve':[10000.]*(1+(hi-lo)//DAY_MS),'trades':0}
            results[phase][scenario] = rows
    # Source lookbacks stay fixed. Do not exclude slow monthly rules for low activity.
    # First-period return is only the deterministic tie-break order for identical
    # validation curves; the final segment never participates in selection.
    signatures, aliases = {}, {}
    for s in sorted((s for s in targets if s != 'buy_hold'),key=lambda s:results['first']['base'][s]['return'],reverse=True):
        signature = tuple(state for t,state in targets[s].items() if start <= t < start+60*DAY_MS)
        if signature in signatures:
            aliases[signatures[signature]].append(s)
        else:
            signatures[signature] = s
            aliases[s] = [s]
    candidates = list(aliases)+['cash']
    # Fixed documented objective: net validation return, DD no worse than buy/hold.
    selected = choose_weights({s:results['validation']['base'][s]['curve'] for s in candidates},results['validation']['base']['buy_hold']['drawdown'],365)
    if selected:
        for phase in periods:
            for scenario in ('base','stress'):
                curve = combine({s:results[phase][scenario][s]['curve'] for s in candidates},selected['weights'])
                results[phase][scenario]['selected'] = {**metrics(curve,365),'curve':curve}
                for exposure in (.25,.5,.75):
                    scaled = [10000+exposure*(v-10000) for v in curve]
                    results[phase][scenario][f'selected_{int(exposure*100)}pct'] = {**metrics(scaled,365),'curve':scaled}
    report = {'mode':'MU_source_adaptation_research_only','execution_symbol':'MU-USDT-SWAP',
              'sources':{s:SOURCE_ROOT+f for s,f in SOURCE_FILES.items()},'selected':selected,'eligible_after_first':candidates,'first_period_signal_aliases':aliases,
              'periods':periods,'results':results,'external_input_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.stock,args.sector_panel)},
              'provenance':json.loads(window.provenance({'source_files':SOURCE_FILES,'entry_exposure':1,'selection':'fixed source candidates plus cash; 25pct capital grid validation return with buyhold DD cap; tie first-period return'})),
              'adaptations':['MU stock/sector signals, swap executions; price series never stitched.',
                  'Monthly sources use previous completed US session; ATR daily events available regular close plus 15min, execute next whole UTC hour.',
                  'ATH/ATR10 uses close-confirmed exits, NOT source intraday stop-market. Full historical high seeded by 2520 sessions.',
                  'Sector top3 becomes XLK inclusion filter for MU; no ETF purchased. Negative time-series momentum and January state become cash, not short/BIL.',
                  'Time-series momentum transfers direction and 252-price horizon only; original multi-futures inverse-volatility weighting is not reproduced.',
                  'Calendar orders round forward to next full hour; known exchange session dates, not future prices.',
                  'Daily closing drawdown; fee stress10bps+1tick for source sleeves; baseline fee stress only; no funding/spreads/queue.',
                  'All179days were inspected in prior work; chronological research split is not unseen prospective evidence.']}
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'mu-source-results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    lines = ['# 来源策略用在 MU 合约上的结果','',f'选择：{selected}', '',
             '|策略|179天收益|日末回撤|交易数|首60天|中60天|末59天|末段压力收益|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for s,row in results['full']['base'].items():
        values = [row['return'],row['drawdown']]
        tail = [results[p]['base'][s]['return'] for p in ('first','validation','final')]+[results['final']['stress'][s]['return']]
        lines.append('| '+s+' | '+' | '.join(f'{v:.2%}' for v in values)+f" | {row.get('trades','组合')} | "+' | '.join(f'{v:.2%}' for v in tail)+' |')
    lines += ['', '所有订单模拟在 MU-USDT-SWAP；来源候选目标1倍本金，baseline_current_5x单列原5倍配置。原参数保留，移植差异如下：','']+['- '+x for x in report['adaptations']]
    (args.output_dir/'mu-source-results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if selected:
        chosen_config = {'mode':'research_candidate_not_live_release','symbol':'MU-USDT-SWAP',
            'weights':selected['weights'],'source_files':{s:SOURCE_ROOT+SOURCE_FILES[s] for s in selected['weights'] if s in SOURCE_FILES},
            'target_notional_per_equity_at_entry':1.0,'execution':'first whole UTC hour at/after signal event',
            'live_readiness':'external daily signal refresh and release integration are not implemented',
            'selected_by':'middle60d net return, no worse than buyhold DD; equal-score tie uses first60d only',
            'final_metrics':{k:v for k,v in results['final']['base']['selected'].items() if k!='curve'}}
        if selected['weights'] == {'sector_gate':1.0}:
            chosen_config['signal'] = {'universe':list(SECTORS),'lookback_trading_days':252,'top_count':3,
                'reference_sector':'XLK','rebalance':'first US trading session each month',
                'price_information':'previous completed session adjusted closes',
                'long_if':'XLK is in top 3; otherwise flat','when_already_long':'hold existing units',
                'exit':'first whole UTC hour after monthly rank excludes XLK',
                'position':'enter once at 1x current account equity; no Fibonacci entries, pyramiding or source-independent stops'}
        (args.output_dir/'mu-source-candidate.json').write_text(json.dumps(chosen_config,indent=2),encoding='utf-8')
    print('\n'.join(lines[:16]))


if __name__ == '__main__':
    main()
