"""Causal add regressions through the real engine and registry baseline.

Only Fibonacci availability and RSI/MACD are controlled; execution, sizing,
calendar, position rules, risk and settlement run unchanged.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mu_strategy.backtest import run_backtest
from mu_strategy.models import Candle
from mu_strategy.strategies.registry import selected_strategy_groups


START = int(datetime(2026, 6, 11, 13, 45, tzinfo=timezone.utc).timestamp() * 1000)
STEP = 900_000


def bar(index, open_=100, high=101, low=99, close=100):
    return Candle(START + index * STEP, open_, high, low, close, 1000)


class PyramidTimingTests(unittest.TestCase):
    def replay(self, bars, *, rsi_values=None, hist=None, regimes=None, config=None):
        config = config or selected_strategy_groups("MU-USDT-SWAP", ["baseline"])[0].config
        with (
            patch("mu_strategy.backtest.nearest_fib_retest_level",
                  side_effect=lambda candles, index, cfg: 100 if index == 1 else None),
            patch("mu_strategy.backtest.rsi", return_value=rsi_values or [55] * len(bars)),
            patch("mu_strategy.backtest.macd", return_value=([], [], hist or [1] * len(bars))),
        ):
            return run_backtest(bars, regimes or {b.open_time_ms: "green" for b in bars}, config=config)

    def test_close_information_cannot_backfill_signal_open_or_touch(self):
        for candidate in (bar(3, 105, 106, 104, 105), bar(3, 101, 103, 100, 102)):
            with self.subTest(candidate=candidate):
                bars = [bar(0), bar(1), bar(2), candidate, bar(4, 106, 107, 105, 106)]
                result = self.replay(bars, rsi_values=[55, 55, 49, 55, 40])
                fills = result.trades[0].fills
                self.assertEqual([bars[2].open_time_ms, bars[4].open_time_ms], [f.time_ms for f in fills])
                self.assertEqual([100, 106], [f.price for f in fills])

    def test_terminal_candidate_has_no_fill_until_real_next_bar(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102)]
        short = self.replay(bars)
        self.assertEqual(1, len(short.trades[0].fills))
        extended = self.replay(bars + [bar(4, 105, 106, 104, 105)])
        self.assertEqual([100, 105], [f.price for f in extended.trades[0].fills])

    def test_execution_gap_and_touch_use_next_bar_prices(self):
        for execution, expected in ((bar(4, 105, 106, 104, 105), 105),
                                    (bar(4, 100, 103, 99, 102), 102)):
            with self.subTest(expected=expected):
                bars = [bar(0), bar(1), bar(2), bar(3, 108, 109, 107, 108), execution]
                fills = self.replay(bars).trades[0].fills
                self.assertEqual([100, expected], [f.price for f in fills])
                self.assertEqual(execution.open_time_ms, fills[-1].time_ms)

    def test_untriggered_plan_expires_and_later_touch_needs_new_candidate(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102), bar(4),
                bar(5, 101, 103, 100, 102)]
        self.assertEqual(1, len(self.replay(bars).trades[0].fills))

    def test_execution_candle_close_cannot_veto_an_earlier_fill(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102), bar(4, 105, 106, 104, 105)]
        bad = self.replay(bars, rsi_values=[55, 55, 55, 55, 20], hist=[1, 1, 1, 1, -10])
        good = self.replay(bars)
        self.assertEqual(good.trades[0].fills, bad.trades[0].fills)
        self.assertEqual(bars[4].open_time_ms, bad.trades[0].fills[-1].time_ms)

    def test_previous_close_ineligible_cannot_be_rescued_by_execution_close(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102), bar(4, 105, 106, 104, 105)]
        self.assertEqual(1, len(self.replay(bars, rsi_values=[55, 55, 55, 49, 55]).trades[0].fills))

    def test_next_open_regime_invalidates_stage_three_plan(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102),
                bar(4, 103, 105, 102, 104), bar(5, 105, 106, 104, 105)]
        regimes = {b.open_time_ms: "green" for b in bars}
        regimes[bars[5].open_time_ms] = "yellow"
        fills = self.replay(bars, regimes=regimes).trades[0].fills
        self.assertEqual([100, 103], [f.price for f in fills])

    def test_closed_execution_session_or_missing_next_interval_expires_plan(self):
        for execution in (bar(8, 105, 106, 104, 105), bar(27, 105, 106, 104, 105)):
            with self.subTest(time=execution.open_time_ms):
                bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102), execution]
                self.assertEqual(1, len(self.replay(bars).trades[0].fills))
        # A contiguous candidate at the last eligible open cannot cross the session end.
        bars = [bar(0), bar(1), bar(2)] + [bar(i) for i in range(3, 7)]
        bars += [bar(7, 101, 103, 100, 102), bar(8, 105, 106, 104, 105)]
        self.assertEqual(1, len(self.replay(bars).trades[0].fills))

    def test_existing_stop_beats_pending_add_even_with_gap_above_trigger(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102), bar(4, 105, 106, 97, 105)]
        result = self.replay(bars)
        self.assertEqual(1, len(result.trades[0].fills))
        self.assertEqual("stop", result.trades[0].exit_reason)
        self.assertEqual(98, result.trades[0].exit_price)

    def test_add_tightening_applies_next_bar_without_rechecking_old_low(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 101, 103, 100, 102), bar(4, 101, 103, 99, 102)]
        short = self.replay(bars)
        self.assertEqual([100, 102], [f.price for f in short.trades[0].fills])
        self.assertEqual("end_of_data", short.trades[0].exit_reason)
        extended = self.replay(bars + [bar(5, 102, 103, 99, 101)])
        self.assertEqual(short.trades[0].fills, extended.trades[0].fills)
        self.assertEqual("stop", extended.trades[0].exit_reason)
        self.assertEqual(100, extended.trades[0].exit_price)

    def test_non_session_risk_still_precedes_stop_and_pending_add(self):
        bars = [bar(0), bar(1), bar(2)] + [bar(i) for i in range(3, 7)]
        bars += [bar(7, 101, 103, 100, 102), bar(8, 105, 106, 70, 105)]
        trade = self.replay(bars).trades[0]
        self.assertEqual(1, len(trade.fills))
        self.assertEqual("non_session_liquidation_risk", trade.exit_reason)
        self.assertEqual(80, trade.exit_price)


if __name__ == "__main__":
    unittest.main()
