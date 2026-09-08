"""Net-equity measurements through real registry strategies and run_backtest.

Only signal locations and indicators are controlled. Fees, sizing, fills,
position rules and settlement are real. Curve timestamps label execution bars;
within a bar the list retains fill/exit events, then the observed close, then
terminal settlement. They do not assert an OHLC intrabar path.
"""

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from mu_strategy.backtest import run_backtest
from mu_strategy.models import Candle
from mu_strategy.strategies.registry import selected_strategy_groups


START = int(datetime(2026, 6, 11, 13, 45, tzinfo=timezone.utc).timestamp() * 1000)
STEP = 900_000


def bar(index, open_=100, high=101, low=99, close=100):
    return Candle(START + index * STEP, open_, high, low, close, 1000)


class NetEquityTests(unittest.TestCase):
    def replay(self, bars, *, name="baseline", signals=(1,), regimes=None, **overrides):
        config = replace(selected_strategy_groups("MU-USDT-SWAP", [name])[0].config, **overrides)
        with (
            patch("mu_strategy.backtest.nearest_fib_retest_level",
                  side_effect=lambda candles, index, cfg: 100 if index in signals else None),
            patch("mu_strategy.backtest.rsi", return_value=[55] * len(bars)),
            patch("mu_strategy.backtest.macd", return_value=([], [], [1] * len(bars))),
        ):
            return run_backtest(bars, regimes or {b.open_time_ms: "green" for b in bars}, config=config)

    def values(self, result, index):
        return [value for time, value in result.equity_curve if time == START + index * STEP]

    def assert_values(self, actual, expected):
        self.assertEqual(len(expected), len(actual), actual)
        for value, target in zip(actual, expected):
            self.assertAlmostEqual(target, value, delta=1e-9)

    def test_flat_price_entry_and_exit_charge_five_each(self):
        result = self.replay([bar(i) for i in range(4)])
        self.assert_values(self.values(result, 1), [10000])  # candidate only
        self.assert_values(self.values(result, 2), [9995, 9995])  # fill, close
        self.assert_values(self.values(result, 3), [9995, 9990])  # close, settlement
        fill = result.trades[0].fills[0]
        self.assertEqual((100, 100, 10000, 5), (fill.price, fill.units, fill.notional, fill.fee))
        self.assertAlmostEqual(9990, result.ending_equity)
        self.assertAlmostEqual(-10, result.trades[0].pnl)
        self.assertAlmostEqual(-.001, result.max_drawdown_pct)

    def test_profit_is_net_once_and_margin_is_not_a_cost(self):
        result = self.replay([bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102)])
        trade = result.trades[0]
        self.assert_values(self.values(result, 3), [10195, 10189.9])
        self.assertAlmostEqual(10.1, trade.fees)
        self.assertAlmostEqual(189.9, trade.pnl)
        self.assertAlmostEqual(189.9 / 2000, trade.return_pct)
        self.assertAlmostEqual(10189.9, result.ending_equity)

    def test_next_candle_fees_never_leak_into_signal_bar(self):
        for name in ("baseline", "direct_next_open", "legacy_break_high"):
            with self.subTest(name=name):
                bars = [bar(0), bar(1, 100, 100, 99, 100), bar(2), bar(3)]
                result = self.replay(bars, name=name)
                self.assert_values(self.values(result, 1), [10000])
                self.assert_values(self.values(result, 2), [9995, 9995])
                self.assertEqual(sorted(t for t, _ in result.equity_curve),
                                 [t for t, _ in result.equity_curve])
                self.assertEqual(START + 2 * STEP, result.trades[0].fills[0].time_ms)

    def test_same_bar_entry_stop_keeps_both_events_and_flat_close(self):
        for name in ("baseline", "direct_next_open", "legacy_break_high"):
            with self.subTest(name=name):
                result = self.replay([bar(0), bar(1, 100, 100, 99, 100),
                                      bar(2, 100, 101, 97, 100), bar(3)], name=name)
                self.assert_values(self.values(result, 1), [10000])
                self.assert_values(self.values(result, 2), [9995, 9790.1, 9790.1])
                self.assert_values(self.values(result, 3), [9790.1])
                self.assertEqual("initial_stop", result.trades[0].exit_reason)

    def test_terminal_entry_has_close_before_settlement_in_all_modes(self):
        for name in ("baseline", "direct_next_open", "legacy_break_high"):
            with self.subTest(name=name):
                result = self.replay([bar(0), bar(1), bar(2, 100, 100, 99, 100),
                                      bar(3, 100, 101, 99, 101)], name=name, signals=(2,))
                self.assert_values(self.values(result, 2), [10000])
                self.assert_values(self.values(result, 3), [9995, 10095, 10089.95])
                self.assertEqual(1, result.trade_count)

    def test_gap_and_normal_stop_then_flat_bars(self):
        for final, exit_price, ending in ((bar(3, 95, 100, 94, 99), 95, 9490.25),
                                           (bar(3, 100, 101, 97, 100), 98, 9790.1)):
            with self.subTest(exit_price=exit_price):
                result = self.replay([bar(0), bar(1), bar(2), final, bar(4)])
                self.assert_values(self.values(result, 3), [ending, ending])
                self.assert_values(self.values(result, 4), [ending])
                self.assertEqual(exit_price, result.trades[0].exit_price)
                self.assertEqual("stop", result.trades[0].exit_reason)

    def test_hand_calculated_peak_trough_and_drawdown(self):
        result = self.replay([bar(0), bar(1), bar(2, 100, 101, 99, 101),
                              bar(3), bar(4, 101, 103, 100, 102)])
        self.assert_values(self.values(result, 2), [9995, 10095])
        self.assert_values(self.values(result, 3), [9995])
        self.assertAlmostEqual(9995 / 10095 - 1, result.max_drawdown_pct, delta=1e-12)

    def test_add_fees_accumulate_without_reducing_sizing_budget(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102),
                bar(4, 103, 105, 102, 104), bar(5, 105, 106, 104, 105)]
        result = self.replay(bars)
        trade = result.trades[0]
        self.assertEqual([100, 103, 105], [f.price for f in trade.fills])
        self.assertEqual([10000] * 3, [f.notional for f in trade.fills])
        self.assertEqual([5] * 3, [f.fee for f in trade.fills])
        self.assert_values(self.values(result, 3), [10195])  # plan, no add fee
        self.assert_values(self.values(result, 4), [10290, 10390 + 10000 / 103])
        marked = 10000 + 500 + 2 * 10000 / 103 - 15
        self.assert_values(self.values(result, 5), [marked, marked, result.ending_equity])
        self.assertAlmostEqual(marked - 105 * sum(f.units for f in trade.fills) * .0005,
                               result.ending_equity, delta=1e-9)

    def test_missed_or_invalid_add_plan_has_no_fee(self):
        for execution in (bar(4), bar(8, 105, 106, 104, 105)):
            with self.subTest(execution=execution):
                bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102), execution]
                result = self.replay(bars)
                self.assertEqual(1, len(result.trades[0].fills))
                self.assertAlmostEqual(10000 + (execution.close - 100) * 100 - 5,
                                       result.equity_curve[-2][1])

    def test_fourth_stage_uses_original_forty_percent_budget(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102),
                bar(4, 103, 105, 102, 104), bar(5, 105, 107, 104, 106),
                bar(6, 107, 108, 106, 107)]
        result = self.replay(bars)
        trade = result.trades[0]
        self.assertEqual([10000, 10000, 10000, 20000], [f.notional for f in trade.fills])
        self.assertEqual([5, 5, 5, 10], [f.fee for f in trade.fills])
        marked = 10000 + 700 + 4 * 10000 / 103 + 2 * 10000 / 105 - 25
        self.assert_values(self.values(result, 6), [marked, marked, result.ending_equity])

    def test_new_regime_invalidates_add_without_charging_candidate_fee(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102),
                bar(4, 103, 105, 102, 104), bar(5, 105, 106, 104, 105)]
        regimes = {b.open_time_ms: "green" for b in bars}
        regimes[bars[5].open_time_ms] = "yellow"
        result = self.replay(bars, regimes=regimes)
        self.assertEqual([5, 5], [f.fee for f in result.trades[0].fills])
        marked = 10000 + 500 + 2 * 10000 / 103 - 10
        self.assert_values(self.values(result, 5), [marked, result.ending_equity])

    def test_non_session_risk_precedes_stop_and_samples_net_exit(self):
        result = self.replay([bar(0), bar(1), bar(2), bar(27, 100, 105, 70, 104)])
        self.assertEqual("non_session_liquidation_risk", result.trades[0].exit_reason)
        self.assert_values(self.values(result, 27), [7991, 7991])

    def test_untouched_next_bar_break_high_candidate_has_no_fee(self):
        bars = [bar(0), bar(1), bar(2), bar(3)]
        result = self.replay(bars, name="legacy_break_high")
        self.assertEqual([], result.trades)
        self.assertEqual([(b.open_time_ms, 10000) for b in bars], result.equity_curve)

    def test_unfilled_expired_or_closed_session_entry_is_free(self):
        for terminal in (bar(4), bar(27)):
            with self.subTest(terminal=terminal):
                bars = [bar(0), bar(1), bar(2, 101, 102, 100.5, 101),
                        bar(3, 101, 102, 100.5, 101), terminal]
                result = self.replay(bars, second_pullback_wait_bars=2)
                self.assertEqual([], result.trades)
                self.assertEqual([b.open_time_ms for b in bars],
                                 [t for t, _ in result.equity_curve])
                self.assertTrue(all(value == 10000 for _, value in result.equity_curve))

    def test_no_trade_samples_every_bar_and_short_input_contract_remains(self):
        for count in range(6):
            with self.subTest(count=count):
                bars = [bar(i) for i in range(count)]
                result = self.replay(bars, signals=())
                self.assertEqual([], result.trades)
                self.assertEqual([(b.open_time_ms, 10000) for b in bars] if count >= 4 else [],
                                 result.equity_curve)
                self.assertEqual(0, result.max_drawdown_pct)

    def test_zero_fee_measurement_and_next_trade_compounding(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 100, 101, 97, 100), bar(4), bar(5), bar(6)]
        zero = self.replay(bars, signals=(1, 4), fee_rate=0)
        paid = self.replay(bars, signals=(1, 4))
        self.assert_values(self.values(zero, 2), [10000, 10000])
        self.assertEqual([10000, 9800], [t.fills[0].notional for t in zero.trades])
        self.assert_values([t.fills[0].notional for t in paid.trades], [10000, 9790.1])
        self.assertAlmostEqual(10000 + sum(t.pnl for t in paid.trades), paid.ending_equity)


if __name__ == "__main__":
    unittest.main()
