"""Terminal events through the real engine, with deterministic indicator inputs.

Configurations come from the current registry (including its MU calendar). Only
fees and, in expiry cases, the waiting period are overridden. Fibonacci candidate
availability and RSI/MACD inputs isolate signal timing without mocking signal
validation, execution, risk, position management, fees or settlement. Synthetic
bars are inside the June 11, 2026 US cash window; indicator warmup is controlled.
"""

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from mu_strategy.backtest import run_backtest
from mu_strategy.models import Candle
from mu_strategy.strategies.registry import selected_strategy_groups
from mu_strategy.strategy import is_preferred_us_cash_window


START = int(datetime(2026, 6, 11, 13, 45, tzinfo=timezone.utc).timestamp() * 1000)
STEP = 900_000


def bar(index, open_=100, high=101, low=99, close=100):
    return Candle(START + index * STEP, open_, high, low, close, 1000)


class TerminalBacktestTests(unittest.TestCase):
    def config(self, name="baseline", **overrides):
        config = selected_strategy_groups("MU-USDT-SWAP", [name])[0].config
        return replace(config, **{"fee_rate": 0, **overrides})

    def replay(self, bars, *, signals=(1,), config=None):
        config = config or self.config()
        signal_times = {START + index * STEP for index in signals}
        with (
            patch("mu_strategy.backtest.nearest_fib_retest_level",
                  side_effect=lambda candles, index, cfg:
                  100 if candles[index].open_time_ms in signal_times else None),
            patch("mu_strategy.backtest.rsi", return_value=[55] * len(bars)),
            patch("mu_strategy.backtest.macd",
                  return_value=([0] * len(bars), [0] * len(bars), [1] * len(bars))),
        ):
            return run_backtest(bars, {b.open_time_ms: "green" for b in bars},
                                config=config, starting_equity=10_000)

    def assert_single_exit(self, result, *, entry_index, exit_bar, price, reason, fee_rate=0):
        self.assertEqual(1, result.trade_count)
        trade = result.trades[0]
        self.assertEqual(1, len(trade.fills))
        fill = trade.fills[0]
        self.assertEqual(START + entry_index * STEP, fill.time_ms)
        self.assertEqual(fill.time_ms, trade.entry_time_ms)
        self.assertEqual(100, fill.price)
        self.assertEqual(100, fill.units)
        self.assertEqual(10_000, fill.notional)
        self.assertEqual(exit_bar.open_time_ms, trade.exit_time_ms)
        self.assertEqual(price, trade.exit_price)
        self.assertEqual(reason, trade.exit_reason)
        fees = (10_000 + price * 100) * fee_rate
        self.assertAlmostEqual(10_000 * fee_rate, fill.fee)
        self.assertAlmostEqual(fees, trade.fees)
        self.assertAlmostEqual((price - 100) * 100 - fees, trade.pnl)
        self.assertAlmostEqual(10_000 + trade.pnl, result.ending_equity)
        self.assertEqual(result.ending_equity, result.equity_curve[-1][1])

    def test_existing_terminal_stop_and_its_historical_prefix(self):
        config = self.config()
        self.assertEqual("second_pullback", config.entry_execution)
        self.assertEqual("us_equities_2025_2028_v1", config.trading_calendar_id)
        self.assertTrue(config.trading_calendar_sha256)
        terminal = bar(3, 100, 106, 90, 105)
        self.assertTrue(is_preferred_us_cash_window(terminal.open_time_ms, config))
        self.assertGreater(terminal.low, 100 * (1 - 1 / config.leverage))
        bars = [bar(0), bar(1), bar(2), terminal]
        short = self.replay(bars, config=config)
        extended = self.replay(bars + [bar(4, 105, 106, 104, 105)], config=config)
        for result in (short, extended):
            self.assert_single_exit(result, entry_index=2, exit_bar=terminal, price=98, reason="stop")
        # Only historical exits are invariant; end_of_data and final returns are not.
        self.assertEqual(short.trades, extended.trades)

    def test_terminal_gap_uses_first_available_open(self):
        terminal = bar(3, 95, 106, 90, 105)
        for fee in (0, 0.0005):
            with self.subTest(fee=fee):
                result = self.replay([bar(0), bar(1), bar(2), terminal], config=self.config(fee_rate=fee))
                self.assert_single_exit(result, entry_index=2, exit_bar=terminal,
                                        price=95, reason="stop", fee_rate=fee)

    def test_terminal_stop_charges_entry_and_exit_fee_once(self):
        terminal = bar(3, 100, 106, 90, 105)
        result = self.replay([bar(0), bar(1), bar(2), terminal], config=self.config(fee_rate=0.0005))
        self.assert_single_exit(result, entry_index=2, exit_bar=terminal,
                                price=98, reason="stop", fee_rate=0.0005)

    def test_survivor_receives_one_end_of_data_settlement(self):
        terminal = bar(3, 100, 101, 99, 101)
        result = self.replay([bar(0), bar(1), bar(2), terminal], config=self.config(fee_rate=0.0005))
        self.assert_single_exit(result, entry_index=2, exit_bar=terminal,
                                price=101, reason="end_of_data", fee_rate=0.0005)

    def test_exit_before_terminal_is_not_repeated(self):
        stopped = bar(3, 100, 106, 90, 105)
        result = self.replay([bar(0), bar(1), bar(2), stopped, bar(4)],
                             config=self.config(fee_rate=0.0005))
        self.assert_single_exit(result, entry_index=2, exit_bar=stopped,
                                price=98, reason="stop", fee_rate=0.0005)

    def test_terminal_non_session_risk_retains_priority_over_stop(self):
        terminal = replace(bar(3, 100, 106, 70, 105), open_time_ms=START + 27 * STEP)
        config = self.config()
        self.assertFalse(is_preferred_us_cash_window(terminal.open_time_ms, config))
        result = self.replay([bar(0), bar(1), bar(2), terminal], config=config)
        self.assert_single_exit(result, entry_index=2, exit_bar=terminal,
                                price=80, reason="non_session_liquidation_risk")

    def test_entry_on_terminal_checks_risk_once_for_each_execution_mode(self):
        for name in ("baseline", "direct_next_open", "legacy_break_high"):
            for fee in (0, 0.0005):
                with self.subTest(strategy=name, fee=fee):
                    terminal = bar(3, 100, 106, 90, 105)
                    bars = [bar(0), bar(1), bar(2, 100, 100, 99, 100), terminal]
                    config = self.config(name, fee_rate=fee)
                    short = self.replay(bars, signals=(2,), config=config)
                    extended = self.replay(bars + [bar(4)], signals=(2,), config=config)
                    for result in (short, extended):
                        self.assert_single_exit(result, entry_index=3, exit_bar=terminal,
                                                price=98, reason="initial_stop", fee_rate=fee)
                    self.assertEqual(short.trades, extended.trades)

    def test_terminal_entry_survivor_settles_once_for_each_execution_mode(self):
        for name in ("baseline", "direct_next_open", "legacy_break_high"):
            with self.subTest(strategy=name):
                terminal = bar(3, 100, 101, 99, 101)
                bars = [bar(0), bar(1), bar(2, 100, 100, 99, 100), terminal]
                result = self.replay(bars, signals=(2,), config=self.config(name, fee_rate=0.0005))
                self.assert_single_exit(result, entry_index=3, exit_bar=terminal,
                                        price=101, reason="end_of_data", fee_rate=0.0005)

    def test_pending_is_fillable_on_inclusive_expiry_at_terminal(self):
        bars = [bar(0), bar(1), bar(2, 101, 102, 100.5, 101),
                bar(3, 101, 102, 100.5, 101), bar(4)]
        for wait in (3, 8):
            with self.subTest(wait=wait):
                result = self.replay(bars, config=self.config(second_pullback_wait_bars=wait))
                self.assert_single_exit(result, entry_index=4, exit_bar=bars[-1],
                                        price=100, reason="end_of_data")

    def test_pending_expired_before_terminal_cannot_fill(self):
        bars = [bar(0), bar(1), bar(2, 101, 102, 100.5, 101),
                bar(3, 101, 102, 100.5, 101), bar(4)]
        result = self.replay(bars, config=self.config(second_pullback_wait_bars=2))
        self.assertEqual([], result.trades)
        self.assertEqual(10_000, result.ending_equity)

    def test_pending_terminal_touch_still_requires_limit_and_session(self):
        for terminal in (bar(3, 101, 102, 100.1, 101),
                         replace(bar(3), open_time_ms=START + 27 * STEP)):
            with self.subTest(terminal=terminal):
                bars = [bar(0), bar(1), bar(2, 101, 102, 100.5, 101), terminal]
                result = self.replay(bars)
                self.assertEqual([], result.trades)
                self.assertEqual(10_000, result.ending_equity)

    def test_final_signal_requires_a_real_later_candle(self):
        for name in ("baseline", "direct_next_open", "legacy_break_high"):
            with self.subTest(strategy=name):
                bars = [bar(0), bar(1), bar(2), bar(3, 100, 100, 99, 100)]
                config = self.config(name)
                short = self.replay(bars, signals=(3,), config=config)
                self.assertEqual([], short.trades)
                self.assertEqual(10_000, short.ending_equity)
                terminal = bar(4, 100, 106, 90, 105)
                extended = self.replay(bars + [terminal], signals=(3,), config=config)
                self.assert_single_exit(extended, entry_index=4, exit_bar=terminal,
                                        price=98, reason="initial_stop")

    def test_flat_and_short_inputs_have_no_phantom_trades(self):
        for length in range(6):
            with self.subTest(length=length):
                result = self.replay([bar(i) for i in range(length)], signals=())
                self.assertEqual([], result.trades)
                self.assertEqual(10_000, result.ending_equity)
                if length < 4:
                    self.assertEqual([], result.equity_curve)

    def test_terminal_add_and_tightening_do_not_recheck_the_old_low(self):
        terminal = bar(3, 100, 103, 99, 102)
        bars = [bar(0), bar(1), bar(2), terminal]
        result = self.replay(bars)
        self.assertEqual(1, result.trade_count)
        trade = result.trades[0]
        self.assertEqual("end_of_data", trade.exit_reason)
        self.assertEqual([100, 102], [f.price for f in trade.fills])
        self.assertEqual([START + 2 * STEP, terminal.open_time_ms], [f.time_ms for f in trade.fills])
        self.assertEqual(2, trade.max_stage)
        # The raised stop applies on the following bar, not retroactively to low=99.
        extended = self.replay(bars + [bar(4, 102, 103, 99, 101)])
        self.assertEqual(1, extended.trade_count)
        self.assertEqual(trade.fills, extended.trades[0].fills)
        self.assertEqual("stop", extended.trades[0].exit_reason)
        self.assertEqual(100, extended.trades[0].exit_price)


if __name__ == "__main__":
    unittest.main()
