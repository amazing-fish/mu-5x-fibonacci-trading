import unittest
from datetime import date, timedelta

from mu_strategy.experiments.source_strategies import Panel, targets, simulate, choose_weights


class SourceStrategyTests(unittest.TestCase):
    def panel(self):
        dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(450)]
        symbols = ('SPY', 'EFA', 'IEF', 'VNQ', 'GSG', 'AGG', 'BIL',
                   'XLK', 'XLE', 'XLV', 'XLF', 'XLI', 'XLB', 'XLY', 'XLP', 'XLU')
        closes = {s: [100 + j * (k + 1) / 100 for j in range(450)] for k, s in enumerate(symbols)}
        return Panel(dates, closes, closes)

    def test_original_252_day_top_three_and_no_current_price_leak(self):
        p = self.panel()
        index = next(i for i, d in enumerate(p.dates) if d == date(2021, 1, 1))
        signal = targets(p, 'asset_momentum')
        self.assertEqual({'GSG': 1/3, 'VNQ': 1/3, 'IEF': 1/3}, signal[index])
        p.closes['SPY'][index] = 99999
        self.assertEqual(signal[index], targets(p, 'asset_momentum')[index])
        early = Panel(p.dates[:index+1], {s:x[:index+1] for s,x in p.opens.items()},
                      {s:x[:index+1] for s,x in p.closes.items()})
        self.assertEqual(signal[:index+1], targets(early, 'asset_momentum'))

    def test_paired_switches_only_march_june_september_december(self):
        p = self.panel()
        signal = targets(p, 'paired_switching')
        self.assertEqual({'AGG': 1.0}, signal[p.dates.index(date(2021, 3, 1))])
        self.assertIsNone(signal[p.dates.index(date(2021, 2, 1))])

    def test_january_negative_selects_bills_and_later_months_hold(self):
        p = self.panel()
        feb = p.dates.index(date(2021, 2, 1))
        p.closes['SPY'][feb-1] = 1
        signal = targets(p, 'january_barometer')
        self.assertEqual({'SPY': 1.0}, signal[p.dates.index(date(2021, 1, 1))])
        self.assertEqual({'BIL': 1.0}, signal[feb])
        self.assertIsNone(signal[feb+1])

    def test_cost_accounting_no_borrowing_and_final_liquidation(self):
        p = Panel([date(2020,1,i) for i in (1,2,3)], {'A':[100.,110.,120.]}, {'A':[100.,110.,120.]})
        result = simulate(p, [{'A':1.0},None,None], 0,3,.01)
        expected = 10000/1.01*1.2*.99
        self.assertAlmostEqual(expected, result['curve'][-1])
        self.assertGreaterEqual(result['min_cash'], -1e-8)
        self.assertEqual(2, result['orders'])

    def test_selection_maximizes_return_under_comparator_drawdown(self):
        curves = {'a':[10000,12000,11000], 'b':[10000,10000,11500]}
        best = choose_weights(curves, .05)
        self.assertEqual({'b':1.0}, best['weights'])


if __name__ == '__main__':
    unittest.main()
