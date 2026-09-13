import unittest

from mu_strategy.experiments.mu_short_term import macd_targets, frequency, vix_publications, fresh_window_targets, overnight_events
from mu_strategy.experiments.mu_source_adaptation import StockBar
from mu_strategy.models import Candle, BacktestResult, Trade


class MUShortTermTests(unittest.TestCase):
    def test_overnight_uses_previous_twenty_observations_and_closed_snapshot(self):
        day = 86400000
        minute = 60000
        calendar = [StockBar(i*day+810*minute,1,1,1,1) for i in range(23)]
        prices = [100]+[1]*19+[2,10,10]
        quarter = []
        for i,price in enumerate(prices):
            # 15:15 ET candle closes at 15:30; 15:30 closes after decision.
            quarter += [Candle(i*day+1155*minute,price,price,price,price,1),
                        Candle(i*day+1170*minute,10000,10000,10000,10000,1)]
        vix = [(i*day,20 if i<20 else 10) for i in range(23)]
        events = overnight_events(quarter,calendar,vix)
        price_events = dict(events['overnight_price20'])
        self.assertFalse(price_events[20*day+1200*minute])
        self.assertTrue(price_events[21*day+1200*minute])
        self.assertFalse(price_events[22*day+810*minute])
        self.assertTrue(dict(events['overnight_vix20'])[20*day+1200*minute])
        prefix = overnight_events(quarter[:42],calendar[:22],vix[:21])
        self.assertEqual(prefix['overnight_price20'],
                         events['overnight_price20'][:len(prefix['overnight_price20'])])

    def test_new_account_waits_for_new_entry_not_inherited_overnight_position(self):
        target={0:False,1:True,2:True,3:False,4:True,5:False}
        result=fresh_window_targets(target,2)
        self.assertFalse(result[2])
        self.assertFalse(result[3])
        self.assertTrue(result[4])
        self.assertTrue(fresh_window_targets(target,1)[1])

    def test_vix_quote_timestamp_is_not_daily_close_availability(self):
        from datetime import datetime,timezone,date
        stamp = int(datetime(2026,9,10,7,tzinfo=timezone.utc).timestamp())
        raw = {'timestamp':[stamp],'indicators':{'quote':[{'close':[20.]}]}}
        result = vix_publications(raw,{date(2026,9,10)})
        expected = int(datetime(2026,9,11,4,tzinfo=timezone.utc).timestamp()*1000)
        self.assertEqual([(expected,20.)],result)
        raw['indicators']['quote'][0]['close'] = [None]
        with self.assertRaises(ValueError):
            vix_publications(raw,{date(2026,9,10)})
        self.assertEqual([],vix_publications(raw,set()))

    def bars(self,n=1200):
        import math
        return [Candle(i*3600000,100+.03*i+4*math.sin(i/9),102+.03*i+4*math.sin(i/9),
                       98+.03*i+4*math.sin(i/9),100+.03*i+4*math.sin(i/9+.12),1) for i in range(n)]

    def test_current_or_future_hour_cannot_change_current_target(self):
        from dataclasses import replace
        bars = self.bars()
        for mode in ('h1_cross','d1_h1_cross','d1_h1_one_bar','d1_h1_red_exit'):
            before = macd_targets(bars[:1000],mode)
            after = macd_targets(bars,mode)
            self.assertEqual(before,{t:after[t] for t in before})
            changed = bars[:1000]
            changed[-1] = replace(changed[-1],close=100000)
            self.assertEqual(before,macd_targets(changed,mode))

    def test_filtered_rules_wait_for_completed_daily_warmup(self):
        bars = self.bars()
        target = macd_targets(bars,'d1_h1_red_exit')
        self.assertFalse(any(v for t,v in target.items() if t<34*86400000))
        self.assertTrue(any(target.values()))

    def test_one_bar_source_exit_is_exactly_one_hour(self):
        target = list(macd_targets(self.bars(),'d1_h1_one_bar').values())
        self.assertTrue(any(target))
        self.assertFalse(any(a and b for a,b in zip(target,target[1:])))

    def test_frequency_counts_round_trips_not_sides_or_forced_settlement(self):
        trades = [Trade(0,3600000,100,101,[],10,1,.01,1,'signal_flat'),
                  Trade(7*86400000,14*86400000,100,101,[],10,1,.01,1,'end_of_data')]
        result = BacktestResult(10000,10020,trades,[])
        stats = frequency(result,0,14*86400000)
        self.assertEqual(1,stats['completed_trades'])
        self.assertEqual(.5,stats['completed_per_week'])
        self.assertFalse(stats['meets_weekly_minimum'])


if __name__ == '__main__':
    unittest.main()
