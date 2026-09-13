import unittest

from mu_strategy.experiments.strategy_ladder import (
    CandidateDefinition, DEFAULT_MU_INSTRUMENT, run_long_only_candidate,
)
from mu_strategy.models import Candle
from mu_strategy.models import BacktestResult, Fill, Trade
from mu_strategy.experiments.mu_overnight_validation import attribute_trades, exact_targets

STEP = 900000


class ExactOvernightTests(unittest.TestCase):
    def run_case(self, bars, targets):
        return run_long_only_candidate(
            bars, definition=CandidateDefinition('test', 'custom', 'test', 'test'),
            fee_bps_per_side=0, slippage_ticks=0, instrument=DEFAULT_MU_INSTRUMENT,
            target_long_by_open_time=targets, bar_duration_ms=STEP,
            execution_start_time_ms=0, execution_end_time_ms=4*STEP,
        )

    def test_quarter_hour_exit_uses_0930_open_not_next_hour(self):
        bars = [Candle(i*STEP, p, p, p, p, 1) for i,p in enumerate([100,110,120,80])]
        result = self.run_case(bars, {i*STEP:i<2 for i in range(4)})
        self.assertEqual(2*STEP,result.trades[0].exit_time_ms)
        self.assertEqual(120,result.trades[0].exit_price)
        self.assertEqual(12000,result.ending_equity)
        self.assertEqual(4*STEP,result.equity_curve[-1][0])

    def test_quarter_hour_missing_bar_is_not_rounded_forward(self):
        bars = [Candle(i*STEP,100,100,100,100,1) for i in [0,1,3]]
        with self.assertRaisesRegex(ValueError, 'contiguous'):
            self.run_case(bars,{b.open_time_ms:b.open_time_ms<2*STEP for b in bars})

    def test_terminal_settlement_uses_quarter_close(self):
        bars = [Candle(i*STEP,100,101,100,101,1) for i in range(4)]
        result = self.run_case(bars,{b.open_time_ms:True for b in bars})
        self.assertEqual(4*STEP,result.trades[0].exit_time_ms)
        self.assertEqual('end_of_data',result.trades[0].exit_reason)

    def test_attribution_sums_price_legs_without_double_counting_costs(self):
        bars = [Candle(i*STEP,p,p,p,p,1) for i,p in enumerate([100,110,105])]
        fill = Fill(0,100.1,1,200.2,2,.2002)
        trade = Trade(0,2*STEP,100.1,104.9,[fill],9.19,.41,.0459,1,'signal_flat')
        result = BacktestResult(10000,10009.19,[trade],[])
        att = attribute_trades(result,bars,{2*STEP:STEP})
        totals = att['totals']
        self.assertAlmostEqual(20,totals['overnight_gross_pnl'])
        self.assertAlmostEqual(-10,totals['spill_gross_pnl'])
        self.assertAlmostEqual(.4,totals['slippage_cost'])
        self.assertAlmostEqual(.41,totals['fees'])
        self.assertAlmostEqual(9.19,totals['net_pnl'])
        self.assertEqual('unknown',att['funding'])
        with self.assertRaisesRegex(ValueError,'Missing attribution'):
            attribute_trades(result,[bars[0],bars[-1]],{2*STEP:STEP})

    def test_forced_tail_is_separate_and_still_reconciles_account(self):
        trade = Trade(0,4*STEP,100,101,[],49,1,.049,1,'end_of_data')
        att = attribute_trades(BacktestResult(1000,1049,[trade],[]),[],{})
        self.assertEqual(0,att['paired_count'])
        self.assertEqual(1,att['unpaired_count'])
        self.assertEqual(49,att['totals']['unpaired_net_pnl'])

    def test_missing_scheduled_open_fails_even_if_later_bar_exists(self):
        bars = [Candle(i*STEP,100,100,100,100,1) for i in [0,1,3]]
        with self.assertRaisesRegex(ValueError,'Missing exact execution'):
            exact_targets(bars,[(0,True),(2*STEP,False)],0,4*STEP)

    def test_quarter_hour_cannot_reinterpret_builtin_hourly_signals(self):
        bars = [Candle(i*STEP,100,100,100,100,1) for i in range(4)]
        with self.assertRaisesRegex(ValueError,'requires explicit targets'):
            run_long_only_candidate(bars,
                definition=CandidateDefinition('test','overnight_seasonality','test','test'),
                fee_bps_per_side=0,slippage_ticks=0,instrument=DEFAULT_MU_INSTRUMENT,
                bar_duration_ms=STEP)


if __name__ == '__main__':
    unittest.main()
