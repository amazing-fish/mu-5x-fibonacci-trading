"""One frozen MU 09:30 exit replay and matched-trade overnight/spill attribution."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.experiments.mu_short_term import (
    DAY, HOUR, OVERNIGHT_SOURCE, fresh_window_targets, overnight_events,
    summarize, vix_publications,
)
from mu_strategy.experiments.mu_source_adaptation import events_to_targets, load_stock
from mu_strategy.experiments.strategy_ladder import (
    CandidateDefinition, DEFAULT_MU_INSTRUMENT, run_long_only_candidate,
)
from mu_strategy.research.historical_data import load_historical_window, validate_replay_outputs
from mu_strategy.strategies.registry import baseline_strategy_group

STEP = 900_000
NAMES = ('overnight_price20', 'overnight_unfiltered')


def exact_targets(bars, events, start, end):
    """Scheduled executions must exist; never round a missing price forward."""
    available = {b.open_time_ms for b in bars}
    missing = [t for t,_ in events if start <= t < end and t not in available]
    if missing:
        raise ValueError(f'Missing exact execution prices: {missing[:5]}')
    return fresh_window_targets(events_to_targets(bars, events), start)


def attribute_trades(result, bars, cut_by_exit):
    """Use old-account quantities for an additive price/fee/slippage bridge."""
    prices = {b.open_time_ms:b.open for b in bars}
    rows, unpaired = [], []
    for trade in result.trades:
        if trade.exit_reason == 'end_of_data':
            unpaired.append({'entry':trade.entry_time_iso, 'exit':trade.exit_time_iso,
                             'reason':'terminal_settlement', 'net_pnl':trade.pnl, 'fees':trade.fees})
            continue
        cut = cut_by_exit.get(trade.exit_time_ms)
        if cut is None or any(t not in prices for t in (trade.entry_time_ms, cut, trade.exit_time_ms)):
            raise ValueError('Missing attribution price or scheduled session-open cut')
        if not trade.entry_time_ms < cut <= trade.exit_time_ms:
            raise ValueError('Invalid attribution chronology')
        if len(trade.fills) != 1:
            raise ValueError('Attribution expects one-entry source trades')
        units = trade.fills[0].units
        p0, p1, p2 = (prices[t] for t in (trade.entry_time_ms, cut, trade.exit_time_ms))
        overnight, spill = units*(p1-p0), units*(p2-p1)
        slippage = units*((trade.entry_price-p0)+(p2-trade.exit_price))
        residual = overnight+spill-slippage-trade.fees-trade.pnl
        if not math.isclose(residual, 0, abs_tol=1e-7):
            raise ValueError(f'Trade attribution does not reconcile: {residual}')
        rows.append({'entry':trade.entry_time_iso, 'cut':datetime.fromtimestamp(cut/1000,timezone.utc).isoformat(),
            'exit':trade.exit_time_iso, 'units':units, 'entry_open':p0, 'cut_open':p1, 'exit_open':p2,
            'overnight_gross_pnl':overnight, 'spill_gross_pnl':spill, 'slippage_cost':slippage,
            'fees':trade.fees, 'net_pnl':trade.pnl, 'reconciliation_error':residual})
    keys = ('overnight_gross_pnl','spill_gross_pnl','slippage_cost','fees','net_pnl')
    totals = {k:sum(row[k] for row in rows) for k in keys}
    totals['unpaired_net_pnl'] = sum(row['net_pnl'] for row in unpaired)
    if not math.isclose(totals['net_pnl']+totals['unpaired_net_pnl'],
                        result.ending_equity-result.starting_equity, abs_tol=1e-7):
        raise ValueError('Account attribution does not reconcile')
    signs = {leg:{'positive':sum(row[leg]>0 for row in rows), 'negative':sum(row[leg]<0 for row in rows),
                  'zero':sum(row[leg]==0 for row in rows)} for leg in ('overnight_gross_pnl','spill_gross_pnl')}
    return {'funding':'unknown', 'paired_count':len(rows), 'unpaired_count':len(unpaired),
            'totals':totals, 'sign_counts':signs, 'trades':rows, 'unpaired':unpaired}


def check_old_anchor(result, prior):
    """The 15m old-exit replay must reproduce the preceding hourly trade path."""
    if not math.isclose(result.total_return_pct, prior['return'], abs_tol=1e-10):
        raise ValueError('Old 10:00 account differs from previous report')
    if len(result.trades) != len(prior['trades']):
        raise ValueError('Old 10:00 trade count differs from previous report')
    for actual, expected in zip(result.trades, prior['trades']):
        if (actual.entry_time_iso, actual.exit_time_iso, actual.exit_reason) != (
            expected['entry'], expected['exit'], expected['reason']
        ) or any(not math.isclose(getattr(actual,k),expected[k],abs_tol=1e-8) for k in ('pnl','fees','entry_price','exit_price')):
            raise ValueError('Old 10:00 trade differs from previous report')


def describe(result, start, end, offset):
    row = summarize(result,start,end,offset)
    # Existing baseline can emit several event samples per quarter-hour label;
    # choose only the last value at each label for comparable sampled drawdown.
    samples = {}
    for t,value in result.equity_curve:
        samples[t+offset] = value
    peak, drawdown = result.starting_equity, 0.
    for t in sorted(samples):
        peak = max(peak,samples[t])
        drawdown = max(drawdown,1-samples[t]/peak)
    row.update(funding='unknown', drawdown_15m_sampled=drawdown,
               ending_equity=result.ending_equity, equity_15m=sorted(samples.items()))
    for trade, record in zip(result.trades,row['trades']):
        record['units'] = sum(fill.units for fill in trade.fills)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('data-dir','stock-calendar','vix','previous-report','output-dir'):
        parser.add_argument('--'+arg,type=Path,required=True)
    parser.add_argument('--generation-id',required=True)
    args = parser.parse_args()
    validate_replay_outputs(args.data_dir,args.output_dir)
    window = load_historical_window(data_dir=args.data_dir,generation_id=args.generation_id,
                                    symbol='MU-USDT-SWAP',days=179)
    bars, hourly = list(window.candles_by_interval['15m']),list(window.candles_by_interval['1h'])
    calendar = load_stock(args.stock_calendar)
    raw = json.loads(args.vix.read_text(encoding='utf-8'))['chart']['result'][0]
    if raw['meta']['symbol'] != '^VIX':
        raise ValueError('Expected VIX source')
    vix = vix_publications(raw,{datetime.fromtimestamp(b.open_ms/1000,timezone.utc).date() for b in calendar})
    schedules = overnight_events(bars,calendar,vix)
    prior = json.loads(args.previous_report.read_text(encoding='utf-8'))
    start, end = window.start_ms+35*DAY, window.end_ms
    periods = {'first':(start,start+48*DAY),'validation':(start+48*DAY,start+96*DAY),
               'final':(start+96*DAY,end),'full':(start,end)}
    if prior['periods'] != {k:list(v) for k,v in periods.items()}:
        raise ValueError('Previous report window mismatch')
    protocol = {'source':OVERNIGHT_SOURCE,'symbol':'MU-USDT-SWAP','funding':'unknown',
        'comparison':'frozen 09:30 vs 10:00 next regular session exits; no parameter search',
        'timezone':'America/New_York','base_fee_bps_per_side':5,'stress_fee_bps_per_side':10,
        'source_stress_slippage_ticks':1,'baseline_stress_slippage_ticks':0,
        'baseline_stress_limitation':'fees doubled only; original engine has no equivalent execution slippage',
        'bar_duration_ms':STEP,'prior_source_report_sha256':hashlib.sha256(args.previous_report.read_bytes()).hexdigest()}
    report = {'protocol':protocol,'periods':periods,'results':{},'attribution':{},'old_anchor_checks':[]}
    context = build_hourly_context(bars,hourly)
    for phase,(lo,hi) in periods.items():
        report['results'][phase], report['attribution'][phase] = {}, {}
        for scenario,fee,slip in (('base',5,0),('stress',10,1)):
            rows, attribution = {}, {}
            for name in NAMES:
                events = schedules[name]
                rounded = [((t+HOUR-1)//HOUR*HOUR,value) for t,value in events]
                cuts = {(t+HOUR-1)//HOUR*HOUR:t for t,value in events if not value}
                for exit_name,event_list in (('0930',events),('1000',rounded)):
                    result = run_long_only_candidate(bars,
                        definition=CandidateDefinition(name,'custom',name,OVERNIGHT_SOURCE),
                        fee_bps_per_side=fee,slippage_ticks=slip,instrument=DEFAULT_MU_INSTRUMENT,
                        execution_start_time_ms=lo,execution_end_time_ms=hi,bar_duration_ms=STEP,
                        target_long_by_open_time=exact_targets(bars,event_list,lo,hi))
                    rows[name+'_'+exit_name] = describe(result,lo,hi,0)
                    if exit_name == '1000':
                        check_old_anchor(result,prior['results'][phase][scenario][name])
                        report['old_anchor_checks'].append(f'{phase}/{scenario}/{name}')
                        attribution[name] = attribute_trades(result,bars,cuts)
            for leverage in (1,5):
                config = replace(baseline_strategy_group().config,leverage=leverage,fee_rate=fee/10000)
                result = run_backtest([b for b in bars if lo<=b.open_time_ms<hi],context,
                                      config=config,starting_equity=10000)
                check_old_anchor(result,prior['results'][phase][scenario][f'baseline_{leverage}x'])
                report['old_anchor_checks'].append(f'{phase}/{scenario}/baseline_{leverage}x')
                rows[f'baseline_{leverage}x'] = describe(result,lo,hi,STEP)
            report['results'][phase][scenario] = rows
            report['attribution'][phase][scenario] = attribution
    report['full_account_phase_pnl'] = {}
    for scenario, rows in report['results']['full'].items():
        report['full_account_phase_pnl'][scenario] = {}
        for name,row in rows.items():
            curve = row['curve']
            report['full_account_phase_pnl'][scenario][name] = {
                phase:curve[right]-curve[left]
                for phase,left,right in (('first',0,48),('validation',48,96),('final',96,144))}
    report['decision'] = {'default_configuration_changed':False,'new_direction_opened':False,
        'experiment':'completed','promotion':'not_established_funding_unknown_and_previously_examined_history',
        'price20_passes_frozen_frequency_and_stress':
            report['results']['full']['base']['overnight_price20_0930']['meets_weekly_minimum'] and
            report['results']['full']['stress']['overnight_price20_0930']['return']>0}
    report['provenance'] = json.loads(window.provenance(protocol))
    report['external_input_sha256'] = {str(p):hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in (args.stock_calendar,args.vix,args.previous_report)}
    lines = ['# MU 09:30退出与隔夜/溢出归因','',
        '144天同窗；所有结果funding=unknown。基础单边5bps，来源策略压力10bps+1tick；原策略压力仅10bps。',
        '旧10:00及baseline逐笔、收益复现检查全部通过。回撤为日末或15分钟末采样，不是连续盘中最大回撤。','',
        '|策略|全期基础收益|压力收益|日末回撤|15m采样回撤|自然笔数/周|中位持仓小时|末48天基础收益|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,row in report['results']['full']['base'].items():
        stress = report['results']['full']['stress'][name]
        final = report['results']['final']['base'][name]
        lines.append(f"|{name}|{row['return']:.2%}|{stress['return']:.2%}|{row['drawdown']:.2%}|"
                     f"{row['drawdown_15m_sampled']:.2%}|{row['completed_trades']}/{row['completed_per_week']:.2f}|"
                     f"{row['median_holding_hours']}|{final['return']:.2%}|")
    lines += ['', '## 同数量逐笔归因（旧10:00账户；基础成本；初始10000）','',
        '|策略|配对/尾部未配对|隔夜毛PNL|半小时毛PNL|配对手续费|配对净PNL|尾部净PNL|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for name,att in report['attribution']['full']['base'].items():
        t = att['totals']
        lines.append(f"|{name}|{att['paired_count']}/{att['unpaired_count']}|{t['overnight_gross_pnl']:.2f}|"
            f"{t['spill_gross_pnl']:.2f}|{t['fees']:.2f}|{t['net_pnl']:.2f}|{t['unpaired_net_pnl']:.2f}|")
    lines += ['', '配对净PNL=隔夜毛PNL+半小时毛PNL−滑点−手续费；尾部强制结算单列。',
        '新09:30曲线独立复利；不能将两条最终收益率之差当作纯半小时贡献。',
        '本轮完成即收口，未更改默认配置、未注册策略或开启组合方向。',
        '原SPY价格分支迁移MU，不是完整三因子复现。只验证已选规则，未重新选择参数。']
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    (args.output_dir/'validation.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
