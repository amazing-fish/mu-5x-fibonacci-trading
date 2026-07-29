import copy
import unittest
from dataclasses import asdict

from mu_strategy.backtest import run_backtest
from mu_strategy.models import Candle
from mu_strategy.strategies.position_rules import (
    PositionFillSnapshot,
    PositionStateSnapshot,
    apply_stop_transition_curve,
    decide_pyramid_add,
    resolved_stop_tightening,
    stop_transition_progress,
    tighten_stop,
)
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


def position_snapshot(
    prices,
    *,
    stop_price=98,
    initial_stop_price=98,
    max_stage=None,
    stop_transition_fill_count=0,
    stop_transition_start=0,
):
    fills = tuple(
        PositionFillSnapshot(
            time_ms=index * 900_000,
            price=price,
            units=1,
        )
        for index, price in enumerate(prices)
    )
    return PositionStateSnapshot(
        fills=fills,
        stop_price=stop_price,
        entry_anchor=prices[0],
        initial_stop_price=initial_stop_price,
        max_stage=max_stage if max_stage is not None else len(prices),
        stop_transition_fill_count=stop_transition_fill_count,
        stop_transition_start=stop_transition_start,
    )


class BacktestTradeSnapshotTests(unittest.TestCase):
    def test_fixed_synthetic_series_preserves_every_trade_and_fill_field(self):
        config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("00:00", "23:59"),),
        )
        candles = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 101.5, 99.5, 100.8),
            candle(2, 100.8, 103.2, 100.7, 102.6),
            candle(3, 102.6, 105.4, 102.2, 104.6),
            candle(4, 104.6, 107.5, 104.1, 106.8),
            candle(5, 106.8, 108.0, 105.5, 107.2),
        ]
        hourly_context = {bar.open_time_ms: "green" for bar in candles}

        result = run_backtest(candles, hourly_context, config=config, starting_equity=10_000)

        self.assertEqual(10_559.58936943889, result.ending_equity)
        self.assertEqual(
            [
                {
                    "entry_time_ms": 2_700_000,
                    "exit_time_ms": 4_500_000,
                    "entry_price": 105.2370161497052,
                    "exit_price": 107.2,
                    "fills": [
                        {
                            "time_ms": 2_700_000,
                            "price": 103.2,
                            "margin_fraction": 0.2,
                            "notional": 10_000.0,
                            "units": 96.89922480620154,
                            "fee": 0.0,
                        },
                        {
                            "time_ms": 2_700_000,
                            "price": 105.26400000000001,
                            "margin_fraction": 0.2,
                            "notional": 10_000.0,
                            "units": 94.99924000607994,
                            "fee": 0.0,
                        },
                        {
                            "time_ms": 3_600_000,
                            "price": 107.328,
                            "margin_fraction": 0.2,
                            "notional": 10_000.0,
                            "units": 93.17233154442457,
                            "fee": 0.0,
                        },
                    ],
                    "pnl": 559.5893694388899,
                    "fees": 0.0,
                    "return_pct": 0.05595893694388899,
                    "max_stage": 3,
                    "exit_reason": "end_of_data",
                }
            ],
            [asdict(trade) for trade in result.trades],
        )


class ExitRuleTests(unittest.TestCase):
    def test_all_stop_modes_follow_their_stage_advance_sequences(self):
        candles = [
            candle(0, 111, 112, 110, 111),
            candle(1, 112, 113, 111, 112),
            candle(2, 113, 114, 112, 113),
            candle(3, 114, 115, 113, 114),
            candle(4, 115, 116, 114, 115),
        ]
        expected = {
            "baseline": [98, 100, 102, 110 * (1 - 0.0005)],
            "half_protect": [98, 99, 100, 110 * (1 - 0.0005)],
            "wide": [98, 99, 100, 110 * (1 - 0.01)],
            "delayed_baseline": [98, 100, 102, 110 * (1 - 0.0005)],
        }

        for mode, expected_sequence in expected.items():
            with self.subTest(mode=mode):
                config = StrategyConfig(
                    fee_rate=0,
                    stop_tightening=mode,
                    stop_transition_bars=0,
                )
                stop_price = 98
                actual_sequence = []
                for stage in range(1, 5):
                    state = position_snapshot(
                        (100, 102, 104, 106)[:stage],
                        stop_price=stop_price,
                        max_stage=stage,
                    )
                    next_stop = tighten_stop(
                        state,
                        candles[4],
                        index=4,
                        candles=candles,
                        regime="green",
                        config=config,
                    )
                    self.assertGreaterEqual(next_stop, stop_price)
                    actual_sequence.append(next_stop)
                    stop_price = next_stop

                self.assertEqual(expected_sequence, actual_sequence)

    def test_regime_dispatch_and_explicit_overrides(self):
        cases = (
            (StrategyConfig(stop_tightening="green_wide"), "yellow", "baseline"),
            (StrategyConfig(stop_tightening="green_wide"), "green", "wide"),
            (
                StrategyConfig(stop_tightening="half_protect_green_wide"),
                "yellow",
                "half_protect",
            ),
            (
                StrategyConfig(stop_tightening="half_protect_green_wide"),
                "green",
                "wide",
            ),
            (
                StrategyConfig(
                    stop_tightening="baseline",
                    yellow_stop_tightening="wide",
                ),
                "yellow",
                "wide",
            ),
            (
                StrategyConfig(
                    stop_tightening="wide",
                    green_stop_tightening="baseline",
                ),
                "green",
                "baseline",
            ),
        )

        for config, regime, expected in cases:
            with self.subTest(regime=regime, expected=expected):
                self.assertEqual(expected, resolved_stop_tightening(regime, config))

    def test_transition_curves_and_non_positive_bar_short_circuit(self):
        self.assertEqual(0.5, apply_stop_transition_curve(0.5, "linear"))
        self.assertEqual(0.25, apply_stop_transition_curve(0.5, "slow_start"))
        self.assertEqual(0.75, apply_stop_transition_curve(0.5, "fast_start"))
        self.assertEqual(0.5, apply_stop_transition_curve(0.5, "smooth"))

        state = position_snapshot((100, 102), max_stage=2)
        for bars in (0, -1):
            with self.subTest(bars=bars):
                self.assertEqual(
                    1.0,
                    stop_transition_progress(
                        state,
                        index=1,
                        candles=[],
                        config=StrategyConfig(stop_transition_bars=bars),
                    ),
                )

    def test_exit_and_add_rules_do_not_mutate_snapshot_or_nested_fills(self):
        state = position_snapshot((100, 102), max_stage=2)
        original = copy.deepcopy(state)
        current = candle(2, 102, 105, 101, 104)
        config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("00:00", "23:59"),),
        )

        tighten_stop(
            state,
            current,
            index=2,
            candles=[candle(0, 100, 101, 99, 100), candle(1, 101, 103, 100, 102), current],
            regime="green",
            config=config,
        )
        decide_pyramid_add(
            state,
            current,
            rsi_value=55,
            macd_hist=0.2,
            previous_macd_hist=0.1,
            regime="green",
            config=config,
        )

        self.assertEqual(original, state)
        self.assertEqual(original.fills, state.fills)

    def test_unknown_stop_mode_and_transition_curve_still_fail_closed(self):
        state = position_snapshot((100,), max_stage=1)
        current = candle(1, 100, 101, 99, 100)

        with self.assertRaisesRegex(ValueError, "unsupported stop_tightening"):
            tighten_stop(
                state,
                current,
                index=0,
                candles=[current],
                regime="green",
                config=StrategyConfig(stop_tightening="future_mode"),
            )
        with self.assertRaisesRegex(ValueError, "unsupported stop_transition_curve"):
            apply_stop_transition_curve(0.5, "future_curve")


class PyramidAddRuleTests(unittest.TestCase):
    def setUp(self):
        self.config = StrategyConfig(
            fee_rate=0,
            trading_windows_et=(("00:00", "23:59"),),
        )
        self.current = candle(1, 105, 106, 104, 105)

    def decide(self, state, **overrides):
        values = {
            "rsi_value": 55,
            "macd_hist": 0.2,
            "previous_macd_hist": 0.1,
            "regime": "green",
            "config": self.config,
        }
        values.update(overrides)
        return decide_pyramid_add(state, self.current, **values)

    def test_add_decision_returns_stage_gap_fill_and_margin_without_mutation(self):
        state = position_snapshot((100,), max_stage=1)

        decision = self.decide(state)

        self.assertTrue(decision.should_add)
        self.assertEqual(2, decision.stage)
        self.assertEqual(self.current.open, decision.fill_price)
        self.assertEqual(self.config.margin_steps[1], decision.margin_fraction)
        self.assertEqual(1, state.max_stage)
        self.assertEqual(1, len(state.fills))

    def test_stage_three_requires_full_size_regime(self):
        state = position_snapshot((100, 102), max_stage=2)

        self.assertFalse(self.decide(state, regime="yellow").should_add)
        self.assertTrue(self.decide(state, regime=self.config.full_size_regime).should_add)

    def test_indicator_margin_and_session_gates_reject_adds(self):
        stage_one = position_snapshot((100,), max_stage=1)
        cases = (
            ("rsi", stage_one, {"rsi_value": self.config.rsi_add_floor - 1}),
            ("macd", stage_one, {"macd_hist": 0.09}),
            ("margin_steps", position_snapshot((100, 102, 104, 106), max_stage=4), {}),
            (
                "outside_session",
                stage_one,
                {"config": StrategyConfig(fee_rate=0, trading_windows_et=(("09:45", "11:30"),))},
            ),
        )

        for name, state, overrides in cases:
            with self.subTest(gate=name):
                self.assertFalse(self.decide(state, **overrides).should_add)

    def test_shared_contract_has_no_backtest_open_position_dependency(self):
        state = PositionStateSnapshot(
            fills=(PositionFillSnapshot(time_ms=0, price=100, units=1),),
            stop_price=98,
            entry_anchor=100,
            initial_stop_price=98,
            max_stage=1,
        )

        next_stop = tighten_stop(
            state,
            self.current,
            index=1,
            candles=[candle(0, 100, 101, 99, 100), self.current],
            regime="green",
            config=self.config,
        )
        decision = self.decide(state)

        self.assertEqual(98, next_stop)
        self.assertTrue(decision.should_add)
        self.assertEqual("mu_strategy.strategies.position_rules", type(state).__module__)


if __name__ == "__main__":
    unittest.main()
