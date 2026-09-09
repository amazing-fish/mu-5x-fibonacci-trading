"""Post-#122 characterizations: real engine, only signal/indicator inputs fixed.

The frozen hashes cover complete Trade/Fill and ordered curve fields. Hand
assertions explain the path differences; the ledger audit checks fee conservation
without inspecting engine frames or duplicating entry/risk/position rules.
"""

from dataclasses import asdict, replace
import unittest
from unittest.mock import patch

import mu_strategy.backtest as engine
from mu_strategy.strategies.registry import selected_strategy_groups
from replay_net_equity import audit_result, digest
from test_backtest_equity import bar


def cases():
    signal = bar(1, 100, 100, 99, 100)
    paths = {
        "initial_stop": [bar(0), signal, bar(2, 100, 103, 97, 102), bar(3)],
        "existing_stop": [bar(0), signal, bar(2), bar(3, 100, 103, 97, 102)],
        "entry_management": [bar(0), signal, bar(2, 100, 103, 99, 102),
                             bar(3, 103, 104, 99, 103)],
        "non_session_gap": [bar(0), signal, bar(2), bar(27, 70, 106, 69, 105)],
    }
    for name in ("baseline", "direct_next_open", "legacy_break_high"):
        for fee in (0, .0005):
            config = replace(selected_strategy_groups("MU-USDT-SWAP", [name])[0].config, fee_rate=fee)
            for path, bars in paths.items():
                yield f"{name}/{fee}/{path}", bars, config


def replay(bars, config):
    with (
        patch("mu_strategy.backtest.nearest_fib_retest_level",
              side_effect=lambda candles, index, cfg: 100 if index == 1 else None),
        patch("mu_strategy.backtest.rsi", return_value=[55] * len(bars)),
        patch("mu_strategy.backtest.macd", return_value=([], [], [1] * len(bars))),
    ):
        return engine.run_backtest(bars, {b.open_time_ms: "green" for b in bars}, config=config)


def characterization_results():
    return {name: asdict(replay(bars, config)) for name, bars, config in cases()}


class RiskPathTests(unittest.TestCase):
    def test_complete_post_122_results_and_independent_accounting(self):
        # Captured on unmodified main 1fc366c before production refactoring.
        expected = {
            'baseline/0/initial_stop': 'abc9716aacb298540c0c55f6630b0335a776978ec6c160099881ea1bfd27c86e',
            'baseline/0/existing_stop': 'ae5de01001574937d9f4563ea02d906aa6e1c696ca399928e396e3edc74362f2',
            'baseline/0/entry_management': '1f28dd3e9e72bea7df77628ced0840b2f47e77afcdc0b01922081870e0278ba6',
            'baseline/0/non_session_gap': 'd322f60b3c9229f4dc428583b05ee1c9a4e6160a86b1ea8bbf465d90879f52d2',
            'baseline/0.0005/initial_stop': '64e3011f7f9b7d99d9124b5d0f75126fb71c5d8927a3514fcbb250d739d4cc35',
            'baseline/0.0005/existing_stop': '391f7f0fab886deb0ec7f25475c7dea701e1b3ad36794f453a86e0d51c06573f',
            'baseline/0.0005/entry_management': '0f4e87fb202d8396ed2521379f9783bcb5efdfca29dfb659ea5218341c91ff76',
            'baseline/0.0005/non_session_gap': '2fdf5be4da2c06e6b5f8e9ac6df5ee23c43913e665128c4628528e669ef34857',
            'direct_next_open/0/initial_stop': 'abc9716aacb298540c0c55f6630b0335a776978ec6c160099881ea1bfd27c86e',
            'direct_next_open/0/existing_stop': 'ae5de01001574937d9f4563ea02d906aa6e1c696ca399928e396e3edc74362f2',
            'direct_next_open/0/entry_management': '2761720a0a2cc4a1b9d389729591108431b0215762ee00c2bddef2efce9b4e02',
            'direct_next_open/0/non_session_gap': 'd322f60b3c9229f4dc428583b05ee1c9a4e6160a86b1ea8bbf465d90879f52d2',
            'direct_next_open/0.0005/initial_stop': '64e3011f7f9b7d99d9124b5d0f75126fb71c5d8927a3514fcbb250d739d4cc35',
            'direct_next_open/0.0005/existing_stop': '391f7f0fab886deb0ec7f25475c7dea701e1b3ad36794f453a86e0d51c06573f',
            'direct_next_open/0.0005/entry_management': '512e2ff7ca4718ca651111b1863d6b20d2149ff0d95b2446d6826506905446e5',
            'direct_next_open/0.0005/non_session_gap': '2fdf5be4da2c06e6b5f8e9ac6df5ee23c43913e665128c4628528e669ef34857',
            'legacy_break_high/0/initial_stop': 'abc9716aacb298540c0c55f6630b0335a776978ec6c160099881ea1bfd27c86e',
            'legacy_break_high/0/existing_stop': 'ae5de01001574937d9f4563ea02d906aa6e1c696ca399928e396e3edc74362f2',
            'legacy_break_high/0/entry_management': '2761720a0a2cc4a1b9d389729591108431b0215762ee00c2bddef2efce9b4e02',
            'legacy_break_high/0/non_session_gap': 'd322f60b3c9229f4dc428583b05ee1c9a4e6160a86b1ea8bbf465d90879f52d2',
            'legacy_break_high/0.0005/initial_stop': '64e3011f7f9b7d99d9124b5d0f75126fb71c5d8927a3514fcbb250d739d4cc35',
            'legacy_break_high/0.0005/existing_stop': '391f7f0fab886deb0ec7f25475c7dea701e1b3ad36794f453a86e0d51c06573f',
            'legacy_break_high/0.0005/entry_management': '512e2ff7ca4718ca651111b1863d6b20d2149ff0d95b2446d6826506905446e5',
            'legacy_break_high/0.0005/non_session_gap': '2fdf5be4da2c06e6b5f8e9ac6df5ee23c43913e665128c4628528e669ef34857',
        }
        for name, bars, config in cases():
            with self.subTest(case=name):
                result = replay(bars, config)
                self.assertEqual(expected[name], digest(asdict(result)))
                samples = audit_result(bars, result, config.fee_rate)
                self.assertEqual(len(result.equity_curve), len(samples))

    def test_initial_and_existing_stop_retain_distinct_reasons_and_sample_order(self):
        for name, bars, config in cases():
            path = name.rsplit("/", 1)[-1]
            if path not in ("initial_stop", "existing_stop"):
                continue
            with self.subTest(case=name):
                result = replay(bars, config)
                trade = result.trades[0]
                self.assertEqual("initial_stop" if path == "initial_stop" else "stop", trade.exit_reason)
                self.assertEqual(98, trade.exit_price)
                self.assertEqual(1, len(trade.fills))  # Risk wins over add trigger.
                stopped = bars[2 if path == "initial_stop" else 3]
                values = [v for t, v in result.equity_curve if t == stopped.open_time_ms]
                ending = 9800 - 19800 * config.fee_rate
                expected = ([10000 - 10000 * config.fee_rate] if path == "initial_stop" else [])
                self.assertEqual(expected + [ending, ending], values)

    def test_second_pullback_continues_but_next_bar_entry_plans_at_its_close(self):
        for name, bars, config in cases():
            if not name.endswith("/entry_management"):
                continue
            with self.subTest(case=name):
                result = replay(bars, config)
                trade = result.trades[0]
                prices = [100] if config.entry_execution == "second_pullback" else [100, 103]
                self.assertEqual(prices, [f.price for f in trade.fills])
                self.assertEqual("end_of_data", trade.exit_reason)
                self.assertEqual(len(prices), trade.max_stage)
                # Terminal low=99 predates any new raised stop; no retroactive exit.
                terminal = [s for s in audit_result(bars, result, config.fee_rate)
                            if s["time_ms"] == bars[-1].open_time_ms]
                kinds = (["fill"] if len(prices) == 2 else []) + ["close", "settlement"]
                self.assertEqual(kinds, [s["kind"] for s in terminal])

    def test_next_bar_initial_risk_is_checked_once_then_management_runs(self):
        for name, bars, config in cases():
            if not name.endswith("/entry_management"):
                continue
            with self.subTest(case=name), patch.object(
                engine, "_has_non_session_liquidation_risk",
                wraps=engine._has_non_session_liquidation_risk,
            ) as risk:
                replay(bars, config)
                self.assertEqual([bars[2], bars[3]], [call.args[0] for call in risk.call_args_list])

    def test_accounting_audit_rejects_missing_duplicate_reordered_or_double_charged_samples(self):
        name, bars, config = next(case for case in cases()
                                  if case[0] == "baseline/0.0005/initial_stop")
        result = replay(bars, config)
        curve = result.equity_curve
        corruptions = (
            curve[:3] + curve[4:],  # Remove a same-label exit point.
            curve[:3] + [curve[3]] + curve[3:],
            curve[:2] + [curve[3], curve[2]] + curve[4:],
            [(t, value - 5 if i == 2 else value) for i, (t, value) in enumerate(curve)],
        )
        for changed in corruptions:
            with self.subTest(curve=changed), self.assertRaises(AssertionError):
                audit_result(bars, replace(result, equity_curve=changed), config.fee_rate)


if __name__ == "__main__":
    unittest.main()
