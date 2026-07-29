import ast
import inspect
import textwrap
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mu_strategy.backtest import OpenPosition, _make_fill, _maybe_add, _tighten_stop, run_backtest
from mu_strategy.models import Candle
from mu_strategy.strategy import StrategyConfig


def candle(index, open_, high, low, close):
    return Candle(
        open_time_ms=index * 900_000,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


class BacktestTests(unittest.TestCase):
    def test_initial_stop_loss_is_limited_by_5x_20_percent_margin(self):
        config = StrategyConfig(fee_rate=0, trading_windows_et=(("00:00", "23:59"),))
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 100.8, 103, 100.7, 102.5),
            candle(3, 102.5, 103.5, 98.6, 100.1),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(result.trade_count, 1)
        self.assertAlmostEqual(result.trades[0].return_pct, -0.02, places=4)

    def test_fees_are_included_in_trade_return(self):
        config = StrategyConfig(fee_rate=0.001, trading_windows_et=(("00:00", "23:59"),))
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 100.8, 103.2, 100.7, 102.5),
            candle(3, 102.5, 103.5, 98.6, 100.1),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertLess(result.trades[0].return_pct, -0.02)

    def test_pyramid_adds_only_when_filters_are_green(self):
        config = StrategyConfig(fee_rate=0, trading_windows_et=(("00:00", "23:59"),))
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 100.8, 103.2, 100.7, 102.6),
            candle(3, 102.6, 105.4, 102.2, 104.6),
            candle(4, 104.6, 107.5, 104.1, 106.8),
            candle(5, 106.8, 108.0, 105.5, 107.2),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(result.trade_count, 1)
        self.assertGreaterEqual(result.trades[0].max_stage, 3)
        self.assertGreater(result.total_return_pct, 0)

    def test_second_pullback_entry_waits_for_fibonacci_retest(self):
        config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("00:00", "23:59"),),
            entry_execution="second_pullback",
            second_pullback_wait_bars=3,
        )
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 101.1, 101.4, 101.0, 101.2),
            candle(3, 101.0, 101.2, 100.7, 101.0),
            candle(4, 101.0, 103.5, 100.9, 103.0),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(candles_15m[3].open_time_ms, result.trades[0].entry_time_ms)

    def test_second_pullback_does_not_fill_when_tolerance_bar_never_trades_limit(self):
        config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("00:00", "23:59"),),
            entry_execution="second_pullback",
            second_pullback_wait_bars=2,
            fib_tolerance_pct=0.01,
        )
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 102, 99, 101),
            candle(2, 101, 102, 100.5, 101),
            candle(3, 101, 102, 100.5, 101),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        with patch("mu_strategy.backtest.nearest_fib_retest_level", return_value=100):
            with patch("mu_strategy.backtest.rsi", return_value=[55] * len(candles_15m)):
                with patch(
                    "mu_strategy.backtest.macd",
                    return_value=([0] * len(candles_15m), [0] * len(candles_15m), [0, 0.1, 0.2, 0.3]),
                ):
                    result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(0, result.trade_count)

    def test_gap_above_pyramid_add_threshold_fills_at_first_available_open(self):
        config = StrategyConfig(fee_rate=0, trading_windows_et=(("00:00", "23:59"),))
        first = _make_fill(0, 100, 0.2, 10_000, config)
        position = OpenPosition([first], stop_price=98, entry_anchor=100, initial_stop_price=98, max_stage=1)
        gap_above_threshold = candle(1, 105, 106, 104, 105)

        _maybe_add(
            position,
            gap_above_threshold,
            1,
            [candle(0, 100, 101, 99, 100), gap_above_threshold],
            {gap_above_threshold.open_time_ms: "green"},
            [0.1, 0.2],
            [55, 55],
            10_000,
            config,
        )

        self.assertEqual(2, position.max_stage)
        self.assertEqual(gap_above_threshold.open, position.fills[-1].price)

    def test_gap_below_stop_exits_at_first_available_open(self):
        config = StrategyConfig(fee_rate=0, trading_windows_et=(("00:00", "23:59"),), entry_execution="direct_next_open")
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 102, 99, 101),
            candle(2, 100, 101, 99, 100),
            candle(3, 95, 97, 94, 96),
            candle(4, 96, 97, 95, 96),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        with patch("mu_strategy.backtest.nearest_fib_retest_level", return_value=100):
            with patch("mu_strategy.backtest.rsi", return_value=[55] * len(candles_15m)):
                with patch(
                    "mu_strategy.backtest.macd",
                    return_value=([0] * len(candles_15m), [0] * len(candles_15m), [0, 0.1, 0.2, 0.3, 0.4]),
                ):
                    result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(1, result.trade_count)
        self.assertEqual(candles_15m[3].open_time_ms, result.trades[0].exit_time_ms)
        self.assertEqual(candles_15m[3].open, result.trades[0].exit_price)

    def test_existing_stop_exits_before_same_candle_pyramid_trigger(self):
        config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("00:00", "23:59"),),
            entry_execution="direct_next_open",
        )
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 102, 99, 101),
            candle(2, 100, 101, 99, 100),
            candle(3, 100, 103, 97, 102),
            candle(4, 102, 103, 101, 102),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        with patch("mu_strategy.backtest.nearest_fib_retest_level", return_value=100):
            with patch("mu_strategy.backtest.rsi", return_value=[55] * len(candles_15m)):
                with patch(
                    "mu_strategy.backtest.macd",
                    return_value=([0] * len(candles_15m), [0] * len(candles_15m), [0, 0.1, 0.2, 0.3, 0.4]),
                ):
                    result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(1, result.trade_count)
        self.assertEqual(candles_15m[3].open_time_ms, result.trades[0].exit_time_ms)
        self.assertEqual(98, result.trades[0].exit_price)
        self.assertEqual(1, result.trades[0].max_stage)

    def test_pyramid_add_precedes_same_candle_stop_tightening(self):
        config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("00:00", "23:59"),),
            entry_execution="direct_next_open",
        )
        candles_15m = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 102, 99, 101),
            candle(2, 100, 101, 99, 100),
            candle(3, 101, 103, 99, 102.5),
            candle(4, 101, 102, 99.5, 100),
            candle(5, 100, 101, 99, 100),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        with patch("mu_strategy.backtest.nearest_fib_retest_level", return_value=100):
            with patch("mu_strategy.backtest.rsi", return_value=[55] * len(candles_15m)):
                with patch(
                    "mu_strategy.backtest.macd",
                    return_value=([0] * len(candles_15m), [0] * len(candles_15m), [0, 0.1, 0.2, 0.3, 0.4, 0.5]),
                ):
                    result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(1, result.trade_count)
        self.assertEqual(2, result.trades[0].max_stage)
        self.assertEqual(candles_15m[4].open_time_ms, result.trades[0].exit_time_ms)
        self.assertEqual(100, result.trades[0].exit_price)

    def test_half_protect_green_wide_stop_does_not_jump_to_first_entry_cost(self):
        config = StrategyConfig(fee_rate=0, stop_tightening="half_protect_green_wide")
        first = _make_fill(0, 100, 0.2, 10_000, config)
        second = _make_fill(900_000, 102, 0.2, 10_000, config)
        position = OpenPosition([first, second], stop_price=98, entry_anchor=100, initial_stop_price=98, max_stage=2)
        current = candle(2, 102, 103, 101, 102.5)

        _tighten_stop(
            position,
            current,
            2,
            [candle(0, 100, 101, 99, 100), candle(1, 101, 103, 100, 102), current],
            "green",
            config,
        )

        self.assertAlmostEqual(99, position.stop_price)
        self.assertLess(position.stop_price, first.price)

    def test_regime_specific_wide_stop_can_apply_to_yellow_only(self):
        config = StrategyConfig(
            fee_rate=0,
            yellow_stop_tightening="wide",
            green_stop_tightening="baseline",
        )
        first = _make_fill(0, 100, 0.2, 10_000, config)
        second = _make_fill(900_000, 102, 0.2, 10_000, config)
        yellow_position = OpenPosition(
            [first, second],
            stop_price=98,
            entry_anchor=100,
            initial_stop_price=98,
            max_stage=2,
        )
        green_position = OpenPosition(
            [first, second],
            stop_price=98,
            entry_anchor=100,
            initial_stop_price=98,
            max_stage=2,
        )
        current = candle(2, 102, 103, 101, 102.5)
        candles = [candle(0, 100, 101, 99, 100), candle(1, 101, 103, 100, 102), current]

        _tighten_stop(yellow_position, current, 2, candles, "yellow", config)
        _tighten_stop(green_position, current, 2, candles, "green", config)

        self.assertAlmostEqual(99, yellow_position.stop_price)
        self.assertAlmostEqual(100, green_position.stop_price)

    def test_delayed_baseline_stop_tightening_raises_stop_over_transition_bars(self):
        config = StrategyConfig(
            fee_rate=0,
            stop_tightening="delayed_baseline",
            stop_transition_bars=4,
            stop_transition_curve="linear",
        )
        first = _make_fill(0, 100, 0.2, 10_000, config)
        second = _make_fill(900_000, 102, 0.2, 10_000, config)
        candles = [candle(index, 100 + index, 101 + index, 99 + index, 100 + index) for index in range(6)]
        position = OpenPosition([first, second], stop_price=98, entry_anchor=100, initial_stop_price=98, max_stage=2)

        _tighten_stop(position, candles[1], 1, candles, "green", config)
        self.assertAlmostEqual(98, position.stop_price)

        _tighten_stop(position, candles[3], 3, candles, "green", config)
        self.assertAlmostEqual(99, position.stop_price)

        _tighten_stop(position, candles[5], 5, candles, "green", config)
        self.assertAlmostEqual(100, position.stop_price)

    def test_stop_adapter_delegates_transition_state_machine_to_shared_entry_point(self):
        source = textwrap.dedent(inspect.getsource(_tighten_stop))
        tree = ast.parse(source)
        calls = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        mode_literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in {"baseline", "half_protect", "wide", "delayed_baseline"}
        }

        self.assertEqual(1, calls.count("tighten_stop"))
        self.assertNotIn("resolved_stop_tightening", calls)
        self.assertEqual(set(), mode_literals)
        self.assertFalse(any(isinstance(node, (ast.If, ast.Compare)) for node in ast.walk(tree)))

    def test_delayed_baseline_non_linear_curves_change_transition_speed(self):
        first_candle = candle(0, 100, 101, 99, 100)
        second_candle = candle(1, 101, 102, 100, 101)
        midpoint_candle = candle(3, 103, 104, 102, 103)
        candles = [first_candle, second_candle, candle(2, 102, 103, 101, 102), midpoint_candle]
        expected_stops = {
            "slow_start": 98.5,
            "fast_start": 99.5,
            "smooth": 99.0,
        }

        for curve, expected_stop in expected_stops.items():
            with self.subTest(curve=curve):
                config = StrategyConfig(
                    fee_rate=0,
                    stop_tightening="delayed_baseline",
                    stop_transition_bars=4,
                    stop_transition_curve=curve,
                )
                first = _make_fill(0, 100, 0.2, 10_000, config)
                second = _make_fill(900_000, 102, 0.2, 10_000, config)
                position = OpenPosition(
                    [first, second],
                    stop_price=98,
                    entry_anchor=100,
                    initial_stop_price=98,
                    max_stage=2,
                )

                _tighten_stop(position, midpoint_candle, 3, candles, "green", config)

                self.assertAlmostEqual(expected_stop, position.stop_price)

    def test_delayed_baseline_new_add_continues_from_current_stop(self):
        config = StrategyConfig(
            fee_rate=0,
            stop_tightening="delayed_baseline",
            stop_transition_bars=8,
            stop_transition_curve="fast_start",
        )
        first = _make_fill(0, 100, 0.2, 10_000, config)
        second = _make_fill(900_000, 102, 0.2, 10_000, config)
        third = _make_fill(1_800_000, 104, 0.2, 10_000, config)
        candles = [candle(index, 100 + index, 101 + index, 99 + index, 100 + index) for index in range(6)]
        position = OpenPosition([first, second], stop_price=98, entry_anchor=100, initial_stop_price=98, max_stage=2)

        _tighten_stop(position, candles[2], 2, candles, "green", config)
        self.assertAlmostEqual(98.46875, position.stop_price)

        position.fills.append(third)
        position.max_stage = 3
        _tighten_stop(position, candles[2], 2, candles, "green", config)

        self.assertAlmostEqual(98.46875, position.stop_price)

    def test_smooth_delay_curve_starts_slower_than_linear(self):
        config = StrategyConfig(
            fee_rate=0,
            stop_tightening="delayed_baseline",
            stop_transition_bars=4,
            stop_transition_curve="smooth",
        )
        first = _make_fill(0, 100, 0.2, 10_000, config)
        second = _make_fill(900_000, 102, 0.2, 10_000, config)
        candles = [candle(index, 100 + index, 101 + index, 99 + index, 100 + index) for index in range(6)]
        position = OpenPosition([first, second], stop_price=98, entry_anchor=100, initial_stop_price=98, max_stage=2)

        _tighten_stop(position, candles[2], 2, candles, "green", config)

        self.assertAlmostEqual(98.3125, position.stop_price)

    def test_non_session_liquidation_risk_is_not_hidden_by_cash_session_entry_filter(self):
        config = StrategyConfig(
            fee_rate=0,
            entry_execution="direct_next_open",
            trading_windows_et=(("09:45", "11:30"), ("14:30", "15:45")),
        )
        candles_15m = [
            utc_candle(2026, 6, 11, 13, 15, 100, 105, 99, 104),
            utc_candle(2026, 6, 11, 13, 30, 104, 110, 103, 109),
            utc_candle(2026, 6, 11, 13, 45, 109, 112, 102.5, 105),
            utc_candle(2026, 6, 11, 14, 0, 105, 108, 103, 107),
            utc_candle(2026, 6, 11, 20, 15, 107, 108, 80, 82),
            utc_candle(2026, 6, 11, 20, 30, 82, 83, 81, 82),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        with patch("mu_strategy.backtest.rsi", return_value=[55] * len(candles_15m)):
            with patch(
                "mu_strategy.backtest.macd",
                return_value=([0] * len(candles_15m), [0] * len(candles_15m), [0, 0.1, 0.2, 0.3, 0.4, 0.5]),
            ):
                result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(1, result.trade_count)
        self.assertEqual("non_session_liquidation_risk", result.trades[0].exit_reason)
        self.assertEqual(candles_15m[4].open_time_ms, result.trades[0].exit_time_ms)
        self.assertAlmostEqual(result.trades[0].entry_price * (1 - 1 / config.leverage), result.trades[0].exit_price)

    def test_next_open_entry_is_skipped_when_actual_fill_bar_is_outside_cash_session(self):
        config = StrategyConfig(
            fee_rate=0,
            entry_execution="direct_next_open",
            trading_windows_et=(("09:45", "11:30"), ("14:30", "15:45")),
        )
        candles_15m = [
            utc_candle(2026, 6, 11, 19, 0, 100, 105, 99, 104),
            utc_candle(2026, 6, 11, 19, 15, 104, 110, 103, 109),
            utc_candle(2026, 6, 11, 19, 30, 109, 112, 108, 111),
            utc_candle(2026, 6, 11, 19, 45, 111, 112, 102.5, 106),
            utc_candle(2026, 6, 11, 20, 0, 105, 108, 103, 107),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        with patch("mu_strategy.backtest.rsi", return_value=[55] * len(candles_15m)):
            with patch(
                "mu_strategy.backtest.macd",
                return_value=([0] * len(candles_15m), [0] * len(candles_15m), [0, 0.1, 0.2, 0.3, 0.4]),
            ):
                result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(0, result.trade_count)

    def test_second_pullback_fill_is_skipped_outside_cash_session(self):
        config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("09:45", "11:30"), ("14:30", "15:45")),
            entry_execution="second_pullback",
            second_pullback_wait_bars=3,
        )
        candles_15m = [
            utc_candle(2026, 6, 11, 19, 0, 100, 105, 99, 104),
            utc_candle(2026, 6, 11, 19, 15, 104, 110, 103, 109),
            utc_candle(2026, 6, 11, 19, 30, 109, 112, 108, 111),
            utc_candle(2026, 6, 11, 19, 45, 111, 112, 102.5, 106),
            utc_candle(2026, 6, 11, 20, 0, 105, 106, 103, 104),
            utc_candle(2026, 6, 11, 20, 15, 104, 106, 103, 105),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles_15m}

        with patch("mu_strategy.backtest.rsi", return_value=[55] * len(candles_15m)):
            with patch(
                "mu_strategy.backtest.macd",
                return_value=([0] * len(candles_15m), [0] * len(candles_15m), [0, 0.1, 0.2, 0.3, 0.4, 0.5]),
            ):
                result = run_backtest(candles_15m, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(0, result.trade_count)

    def test_pyramid_adds_are_skipped_outside_cash_session(self):
        config = StrategyConfig(fee_rate=0, trading_windows_et=(("09:45", "11:30"), ("14:30", "15:45")))
        first = _make_fill(0, 100, 0.2, 10_000, config)
        position = OpenPosition([first], stop_price=98, entry_anchor=100, initial_stop_price=98, max_stage=1)
        outside_session = utc_candle(2026, 6, 11, 20, 0, 102, 103, 101, 102.5)

        _maybe_add(
            position,
            outside_session,
            1,
            [outside_session],
            {outside_session.open_time_ms: "green"},
            [0.1, 0.2],
            [55, 55],
            10_000,
            config,
        )

        self.assertEqual(1, position.max_stage)
        self.assertEqual(1, len(position.fills))


def utc_candle(year, month, day, hour, minute, open_, high, low, close):
    timestamp = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return Candle(
        open_time_ms=int(timestamp.timestamp() * 1000),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


if __name__ == "__main__":
    unittest.main()
