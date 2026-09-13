import unittest
from datetime import datetime, timezone

from mu_strategy.experiments.mu_source_adaptation import StockBar, events_to_targets, stock_events, payday_events, sector_events, session_date
from mu_strategy.experiments.source_strategies import Panel, SECTORS, metrics
from mu_strategy.experiments.strategy_ladder import CandidateDefinition, DEFAULT_MU_INSTRUMENT, run_long_only_candidate
from mu_strategy.models import Candle


class MUSourceAdaptationTests(unittest.TestCase):
    def test_precomputed_signal_can_execute_on_first_real_bar(self):
        bars = [Candle(0,100,101,99,100,1),Candle(3600000,110,111,109,110,1)]
        result = run_long_only_candidate(bars,definition=CandidateDefinition('known','custom','known','source'),
            fee_bps_per_side=5,slippage_ticks=0,instrument=DEFAULT_MU_INSTRUMENT,
            target_long_by_open_time={0:True,3600000:True})
        self.assertEqual(0,result.trades[0].entry_time_ms)
        self.assertEqual(100,result.trades[0].entry_price)

    def test_stock_signal_is_unavailable_until_publication(self):
        bars = [Candle(t,100,101,99,100,1) for t in (0,3600000,7200000)]
        self.assertEqual({0:False,3600000:False,7200000:True},events_to_targets(bars,[(4500000,True)]))

    def test_monthly_sma_uses_previous_session_not_current_close(self):
        bars = [StockBar(i*86400000,100,101,99,100) for i in range(210)]
        # 1970-08-01 is a month boundary; test artificial calendar intentionally.
        bars += [StockBar(i*86400000,200,201,199,200) for i in range(210,213)]
        before = stock_events(bars,'sma210')
        bars[-1] = StockBar(bars[-1].open_ms,1,2,1,1)
        self.assertEqual(before,stock_events(bars,'sma210'))
        self.assertTrue(before[-1][1])

    def test_payday_weekend_moves_to_friday_then_next_session(self):
        def stamp(day):
            return int(datetime(2026,8,day,13,30,tzinfo=timezone.utc).timestamp()*1000)
        bars = [StockBar(stamp(day),100,101,99,100) for day in (13,14,17,18)]
        events = payday_events(bars)
        close_minus_minute = (6*60+29)*60000
        self.assertEqual([(stamp(14)+close_minus_minute,True),(stamp(17)+close_minus_minute,False)],events)

    def test_atr_exit_needs_completed_close_and_does_not_use_future(self):
        bars = [StockBar(i*86400000,100+i,102+i,99+i,100+i) for i in range(2550)]
        before = stock_events(bars,'ath_atr10')
        later = bars+[StockBar(2550*86400000,1,2,1,1)]
        self.assertEqual(before,stock_events(later,'ath_atr10')[:len(before)])
        self.assertTrue(before[-1][1])
        self.assertFalse(stock_events(later,'ath_atr10')[-1][1])

    def test_sector_rank_uses_closed_252_day_return(self):
        bars = [StockBar(i*86400000,100,101,99,100) for i in range(366)]
        dates = [session_date(b) for b in bars]
        closes = {s:[100+i*(k+1) for i in range(366)] for k,s in enumerate(SECTORS)}
        panel = Panel(dates,closes,closes)
        events = sector_events(panel,{session_date(b):b for b in bars})
        self.assertFalse(events[-1][1])  # XLK has slower growth than at least 3 sectors.
        closes['XLK'][-1] = 1000000
        self.assertEqual(events,sector_events(panel,{session_date(b):b for b in bars}))

    def test_crypto_daily_annualization_uses_365_days(self):
        curve = [10000]+[11000]*365
        self.assertAlmostEqual(.1,metrics(curve,365)['cagr'])


if __name__ == '__main__':
    unittest.main()
