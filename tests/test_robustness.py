import copy
import unittest
from dataclasses import FrozenInstanceError

from mu_strategy.models import Candle, Trade
from mu_strategy.research.robustness import (
    StageDistribution,
    TradeConcentration,
    buy_and_hold_return_pct,
    stage_distribution,
    trade_concentration,
)


def trade(pnl: float, *, max_stage: int = 1, index: int = 0, fees: float = 3.0) -> Trade:
    return Trade(
        entry_time_ms=index * 1_000,
        exit_time_ms=(index + 1) * 1_000,
        entry_price=100.0,
        exit_price=100.0 + pnl,
        fills=[],
        pnl=pnl,
        fees=fees,
        return_pct=pnl / 10_000,
        max_stage=max_stage,
        exit_reason="test",
    )


def candle(index: int, *, open_: float, close: float) -> Candle:
    return Candle(
        open_time_ms=index * 900_000,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1_000.0,
    )


class TradeConcentrationTests(unittest.TestCase):
    def test_top_five_share_above_one_is_preserved(self):
        trades = [
            trade(100, index=0),
            trade(90, index=1),
            trade(80, index=2),
            trade(70, index=3),
            trade(60, index=4),
            trade(10, index=5),
            trade(-20, index=6),
            trade(-30, index=7),
        ]

        result = trade_concentration(trades)

        self.assertEqual(360.0, result.net_pnl)
        self.assertEqual(6, result.winner_count)
        self.assertEqual(2, result.loser_count)
        self.assertAlmostEqual(400 / 360, result.top_n_share_of_net_pnl)
        self.assertGreater(result.top_n_share_of_net_pnl, 1.0)
        self.assertEqual(-40.0, result.net_pnl_excluding_top_n)

    def test_non_positive_net_pnl_has_no_concentration_ratio(self):
        cases = (
            ([trade(10), trade(-10, index=1)], 0.0, -10.0),
            ([trade(5), trade(-10, index=1)], -5.0, -10.0),
        )
        for trades, expected_net, expected_excluding in cases:
            with self.subTest(expected_net=expected_net):
                result = trade_concentration(trades, top_n=1)
                self.assertEqual(expected_net, result.net_pnl)
                self.assertIsNone(result.top_n_share_of_net_pnl)
                self.assertEqual(expected_excluding, result.net_pnl_excluding_top_n)

    def test_empty_trades_return_documented_empty_values(self):
        self.assertEqual(
            TradeConcentration(
                net_pnl=0.0,
                winner_count=0,
                loser_count=0,
                top_n_share_of_net_pnl=None,
                net_pnl_excluding_top_n=0.0,
            ),
            trade_concentration([]),
        )
        self.assertEqual(StageDistribution(stages=()), stage_distribution([]))

    def test_top_n_larger_than_winner_count_uses_all_winners(self):
        result = trade_concentration([trade(5), trade(2, index=1), trade(-1, index=2)], top_n=10)

        self.assertEqual(6.0, result.net_pnl)
        self.assertAlmostEqual(7 / 6, result.top_n_share_of_net_pnl)
        self.assertEqual(-1.0, result.net_pnl_excluding_top_n)

    def test_all_winners_and_all_losers(self):
        winners = trade_concentration([trade(5), trade(2, index=1)])
        self.assertEqual(2, winners.winner_count)
        self.assertEqual(0, winners.loser_count)
        self.assertEqual(1.0, winners.top_n_share_of_net_pnl)
        self.assertEqual(0.0, winners.net_pnl_excluding_top_n)

        losers = trade_concentration([trade(-5), trade(-2, index=1)])
        self.assertEqual(0, losers.winner_count)
        self.assertEqual(2, losers.loser_count)
        self.assertIsNone(losers.top_n_share_of_net_pnl)
        self.assertEqual(-7.0, losers.net_pnl_excluding_top_n)

    def test_negative_top_n_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "top_n must be non-negative"):
            trade_concentration([], top_n=-1)

    def test_tied_winners_are_deterministic(self):
        trades = [trade(10, index=3), trade(10, index=1), trade(10, index=2), trade(-5, index=4)]

        first = trade_concentration(trades, top_n=2)
        second = trade_concentration(trades, top_n=2)

        self.assertEqual(first, second)
        self.assertEqual(20 / 25, first.top_n_share_of_net_pnl)


class StageDistributionTests(unittest.TestCase):
    def test_stage_four_only_winners_are_visible(self):
        trades = [
            trade(-1, max_stage=1, index=0),
            trade(-2, max_stage=2, index=1),
            trade(-3, max_stage=3, index=2),
            trade(4, max_stage=4, index=3),
            trade(6, max_stage=4, index=4),
        ]

        result = stage_distribution(trades)
        by_stage = {stage.max_stage: stage for stage in result.stages}

        self.assertEqual([1, 2, 3, 4], list(by_stage))
        self.assertEqual(0.0, by_stage[1].win_rate)
        self.assertEqual(0.0, by_stage[2].win_rate)
        self.assertEqual(0.0, by_stage[3].win_rate)
        self.assertEqual(1.0, by_stage[4].win_rate)
        self.assertEqual(2, by_stage[4].trade_count)
        self.assertEqual(2, by_stage[4].win_count)
        self.assertEqual(10.0, by_stage[4].net_pnl)

    def test_only_observed_stages_are_reported_including_beyond_four(self):
        result = stage_distribution([trade(-2, max_stage=2), trade(3, max_stage=6, index=1)])

        self.assertEqual([2, 6], [stage.max_stage for stage in result.stages])

    def test_stage_rows_reconcile_to_trade_totals(self):
        trades = [
            trade(-2, max_stage=1),
            trade(3, max_stage=4, index=1),
            trade(5, max_stage=4, index=2),
        ]

        result = stage_distribution(trades)

        self.assertEqual(len(trades), sum(stage.trade_count for stage in result.stages))
        self.assertEqual(2, sum(stage.win_count for stage in result.stages))
        self.assertEqual(sum(item.pnl for item in trades), sum(stage.net_pnl for stage in result.stages))


class BuyAndHoldReturnTests(unittest.TestCase):
    def test_normal_and_levered_reference_cases(self):
        candles = [candle(0, open_=100, close=105), candle(1, open_=105, close=121)]

        self.assertAlmostEqual(0.21, buy_and_hold_return_pct(candles))
        self.assertAlmostEqual(0.42, buy_and_hold_return_pct(candles, leverage=2.0))

    def test_empty_single_flat_zero_open_and_declining_series(self):
        cases = (
            ([], 0.0),
            ([candle(0, open_=100, close=110)], 0.0),
            ([candle(0, open_=100, close=100), candle(1, open_=100, close=100)], 0.0),
            ([candle(0, open_=0, close=1), candle(1, open_=1, close=2)], 0.0),
            ([candle(0, open_=100, close=95), candle(1, open_=95, close=80)], -0.2),
        )
        for candles, expected in cases:
            with self.subTest(expected=expected):
                self.assertAlmostEqual(expected, buy_and_hold_return_pct(candles))


class RobustnessPurityTests(unittest.TestCase):
    def test_functions_are_repeatable_and_do_not_mutate_inputs(self):
        trades = [trade(10, max_stage=4, index=0), trade(-4, max_stage=2, index=1)]
        candles = [candle(0, open_=100, close=101), candle(1, open_=101, close=110)]
        trades_before = copy.deepcopy(trades)
        candles_before = copy.deepcopy(candles)
        trade_ids_before = [id(item) for item in trades]

        self.assertEqual(trade_concentration(trades), trade_concentration(trades))
        self.assertEqual(stage_distribution(trades), stage_distribution(trades))
        self.assertEqual(buy_and_hold_return_pct(candles), buy_and_hold_return_pct(candles))

        self.assertEqual(trades_before, trades)
        self.assertEqual(candles_before, candles)
        self.assertEqual(trade_ids_before, [id(item) for item in trades])

    def test_result_dataclasses_are_frozen(self):
        concentration = trade_concentration([trade(1)])
        stage = stage_distribution([trade(1)]).stages[0]

        with self.assertRaises(FrozenInstanceError):
            concentration.net_pnl = 2
        with self.assertRaises(FrozenInstanceError):
            stage.win_rate = 0.0


if __name__ == "__main__":
    unittest.main()
