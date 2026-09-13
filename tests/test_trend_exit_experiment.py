import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.experiments.trend_exit import candidate_configs, retention_failures, run_experiment, split_periods
from mu_strategy.models import Candle, EntryDecisionCode
from mu_strategy.research.historical_data import load_historical_window
from mu_strategy.strategy import should_enter_long


class TrendExitExperimentTests(unittest.TestCase):
    def test_green_filter_blocks_yellow_without_changing_baseline(self):
        configs = candidate_configs("MU-USDT-SWAP")
        candle = Candle(1, 101, 103, 98.8, 100.8, 1000)
        for regime in ("yellow", "green"):
            baseline = should_enter_long(candle, 100, regime, 51, .2, .1, configs["baseline"])
            filtered = should_enter_long(candle, 100, regime, 51, .2, .1, configs["green_only_baseline"])
            self.assertTrue(baseline.allowed)
            self.assertEqual(regime == "green", filtered.allowed)
            if regime == "yellow":
                self.assertEqual(EntryDecisionCode.REGIME_BLOCKED, filtered.decision_code)

    def test_rsi_hypothesis_is_independent_of_green_filter(self):
        configs = candidate_configs("MU-USDT-SWAP")
        candle = Candle(1, 101, 103, 98.8, 100.8, 1000)
        original = configs["baseline_delayed_tighten_smooth"]
        candidate = configs["smooth_rsi50"]
        self.assertEqual(original, replace(candidate, rsi_floor=45))
        self.assertTrue(should_enter_long(candle, 100, "yellow", 49, .2, .1, original).allowed)
        self.assertFalse(should_enter_long(candle, 100, "yellow", 49, .2, .1, candidate).allowed)
        self.assertTrue(should_enter_long(candle, 100, "yellow", 50, .2, .1, candidate).allowed)

    def test_periods_cover_every_day_once_with_exclusive_end(self):
        day = 86_400_000
        self.assertEqual([(0, 60*day), (60*day, 120*day), (120*day, 179*day)], split_periods(0, 179*day))
        with self.assertRaises(ValueError):
            split_periods(0, 2*day)
        for days in range(3, 183):
            periods = split_periods(day, (days + 1)*day)
            self.assertEqual(day, periods[0][0])
            self.assertEqual((days + 1)*day, periods[-1][1])
            self.assertTrue(all(start < end for start, end in periods))
            self.assertEqual([end for _, end in periods[:-1]], [start for start, _ in periods[1:]])

    def test_replay_matches_existing_engine_and_explicit_cost_rerun(self):
        root = Path(__file__).resolve().parents[1]
        with patch("socket.socket", side_effect=AssertionError("research must stay offline")):
            window = load_historical_window(data_dir=root / "data/live", generation_id="e702be27d2de4b2d92b12bf01c70d02d", symbol="MU-USDT-SWAP", days=3)
            report = run_experiment(window)
        candles = list(window.candles_by_interval["15m"])
        context = build_hourly_context(candles, list(window.candles_by_interval["1h"]))
        config = candidate_configs("MU-USDT-SWAP")["baseline"]
        direct = run_backtest(candles, context, config=config)
        stress = run_backtest(candles, context, config=replace(config, fee_rate=.001))
        baseline = report["strategies"][0]
        self.assertEqual(8, len(report["strategies"]))
        self.assertEqual(direct.equity_curve, baseline["equity_curve"])
        self.assertEqual(direct.total_return_pct, baseline["full"]["return_pct"])
        self.assertEqual(stress.total_return_pct, baseline["double_fee"]["return_pct"])
        self.assertEqual(window.start_ms, report["periods"][0]["start_ms"])
        self.assertEqual(window.end_ms, report["periods"][-1]["end_ms"])
        self.assertFalse(baseline["retained_for_forward_observation"])

    def test_higher_total_return_does_not_hide_late_period_regression(self):
        baseline = self.row(1.4, -.25, [.9, .08, .10])
        candidate = self.row(2.2, -.24, [1., .4, .09])
        self.assertIn("period_3_return", retention_failures(candidate, baseline))
        candidate["periods"][2]["return_pct"] = .11
        self.assertEqual([], retention_failures(candidate, baseline))

    def test_retention_requires_net_gain_drawdown_trade_count_and_cost_survival(self):
        baseline = self.row(1.4, -.25, [.9, .08, .10])
        candidate = self.row(1.4, -.30, [1., .4, .11])
        candidate["full"]["trade_count"] = 19
        candidate["double_fee"]["return_pct"] = 0
        self.assertEqual(
            ["full_return", "drawdown", "trade_count", "double_fee_return"],
            retention_failures(candidate, baseline),
        )

    @staticmethod
    def row(profit, drawdown, periods):
        return {"full": {"return_pct": profit, "max_drawdown_pct": drawdown, "trade_count": 30},
                "double_fee": {"return_pct": .2},
                "periods": [{"return_pct": value} for value in periods]}
