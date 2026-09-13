import unittest
from dataclasses import replace

from mu_strategy.experiments.portfolio_research import (
    SignalSpec, build_targets, daily_equity, combine_equity, select_portfolio,
)
from mu_strategy.experiments.strategy_ladder import CandidateDefinition, DEFAULT_MU_INSTRUMENT, run_long_only_candidate
from mu_strategy.models import BacktestResult, Candle


class PortfolioResearchTests(unittest.TestCase):
    def candles(self, count=220):
        return [Candle(i*3600000, 100+i%19, 102+i%19, 98+i%19, 101+i%19, 100) for i in range(count)]

    def test_targets_do_not_change_when_future_bars_are_appended(self):
        candles = self.candles()
        for family, period, trend in (("momentum",24,False),("ema",12,False),("breakout",24,False),("zscore",24,False),("rsi",7,True)):
            spec = SignalSpec(family, family, period, 48, trend)
            early = build_targets(candles[:190], spec)
            late = build_targets(candles, spec)
            self.assertEqual(early, {time: late[time] for time in early})
            # The candle to execute may not affect its own signal.
            mutated = candles[:190]
            mutated[-1] = replace(mutated[-1], close=1, high=100000)
            self.assertEqual(early, build_targets(mutated, spec))

    def test_explicit_targets_fill_at_next_open_with_costs(self):
        candles = self.candles(4)
        result = run_long_only_candidate(candles, definition=CandidateDefinition("test","custom","test","test"),
            fee_bps_per_side=5, slippage_ticks=1, instrument=DEFAULT_MU_INSTRUMENT,
            target_long_by_open_time={bar.open_time_ms: index == 1 for index,bar in enumerate(candles)})
        self.assertEqual(1, result.trade_count)
        trade = result.trades[0]
        self.assertEqual(candles[1].open+.1, trade.entry_price)
        self.assertEqual(candles[2].open-.1, trade.exit_price)
        self.assertAlmostEqual(10000+trade.pnl, result.ending_equity)

    def test_distinct_mechanisms_have_the_declared_signal_behavior(self):
        rising = [Candle(i*3600000,100+3*i,101+3*i,99+3*i,100+3*i,1) for i in range(60)]
        for spec,index in ((SignalSpec("m","momentum",24),25),(SignalSpec("e","ema",12,48),48),
                           (SignalSpec("b","breakout",24),25)):
            self.assertTrue(build_targets(rising,spec)[index*3600000])
        prices = [100.]*170+[90.,80.,110.,110.]
        dip = [Candle(i*3600000,p,p+1,p-1,p,1) for i,p in enumerate(prices)]
        for family,period in (("zscore",24),("rsi",2)):
            plain = build_targets(dip,SignalSpec("plain",family,period))
            filtered = build_targets(dip,SignalSpec("filtered",family,period,168,True))
            self.assertTrue(plain[171*3600000])
            self.assertFalse(filtered[171*3600000])
            self.assertFalse(plain[173*3600000])

    def test_daily_alignment_keeps_final_settlement_and_bar_close_timing(self):
        day = 86400000
        result = BacktestResult(10000, 11000, [], [(0,10000),(day-900000,11200),(day-900000,11000)])
        self.assertEqual([10000,11000],daily_equity(result,0,day,900000))

    def test_combination_allocates_one_capital_budget(self):
        curves = {"a":[10000,12000,11000],"b":[10000,9000,13000],"cash":[10000]*3}
        for actual, expected in zip(combine_equity(curves,{"a":.5,"b":.5}),[10000,10500,12000]):
            self.assertAlmostEqual(expected,actual)
        for actual, expected in zip(combine_equity(curves,{"a":.25,"cash":.75}),[10000,10500,10250]):
            self.assertAlmostEqual(expected,actual)
        with self.assertRaises(ValueError):
            combine_equity(curves,{"a":1,"b":1})

    def test_final_segment_does_not_select_parameters_or_weights(self):
        rows = [self.row("baseline","fib",[10000,10200,10400],[10000,10300,10400]),
                self.row("trend","trend",[10000,10300,10500],[10000,10500,11000])]
        selected = select_portfolio(rows)
        for row in rows:
            row["periods"][2]["daily_equity"] = [10000,1,100000000]
        self.assertEqual(selected,select_portfolio(rows))

    def row(self,name,family,train,validation):
        return {"name":name,"family":family,"periods":[{"daily_equity":train,"trade_count":4},
            {"daily_equity":validation,"trade_count":4},{"daily_equity":[10000,10000,10000],"trade_count":4}]}
