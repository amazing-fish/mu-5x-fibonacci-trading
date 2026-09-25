import io
import json
import math
import random
import statistics
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mu_strategy.models import BacktestResult, Candle, Fill, Trade

START_MS = 1780272000000


def walk(seed, *, bars=192, drift=0.0, sigma=0.002):
    rng = random.Random(seed)
    price = 100.0
    rows = []
    for index in range(bars):
        opened = price
        price *= math.exp(drift + rng.gauss(0, sigma))
        rows.append(Candle(START_MS + index * 900_000, opened, max(opened, price), min(opened, price), price, 1.0))
    return rows


def test_engine(candles, context, *, config):
    # Test rule: buy the first 15m bar of the day if yesterday's last bar
    # was positive. Sampling whole days destroys this cross-day relation.
    cash = 10_000.0
    trades = []
    curve = [(candles[0].open_time_ms, cash)]
    for prev, bar in zip(candles, candles[1:]):
        if prev.open_time_ms // 86_400_000 != bar.open_time_ms // 86_400_000 and prev.close > prev.open:
            change = bar.close / bar.open - 1
            cash *= 1 + change - 0.001
        curve.append((bar.open_time_ms, cash))
    return BacktestResult(10_000.0, cash, trades, curve)


class EdgeGateTests(unittest.TestCase):
    def test_synthetic_path_aligns_complete_days_by_weekday_stratum(self):
        from mu_strategy.research.edge_gate import synthetic_path

        price = 100.0
        bars = []
        for index in range(24, 7 * 96 + 72):
            day, slot = divmod(index, 96)
            if day == 2 and slot == 40:
                continue
            opened = price
            price *= math.exp((day + 1) * 0.00001 + (slot + 1) * 0.000000001)
            bars.append(Candle(START_MS + index * 900_000, opened,
                               max(opened, price), min(opened, price), price, 1))
        synthetic = synthetic_path(bars, random.Random(7))
        self.assertEqual([bar.open_time_ms for bar in bars],
                         [bar.open_time_ms for bar in synthetic])
        drift = statistics.fmean(math.log(right.close / left.close)
                                 for left, right in zip(bars, bars[1:]))
        actual_returns = [math.log(right.close / left.close)
                          for left, right in zip(bars, bars[1:])]
        simulated_returns = [math.log(right.close / left.close) + drift
                             for left, right in zip(synthetic, synthetic[1:])]
        by_day = {}
        for i, bar in enumerate(bars):
            by_day.setdefault(bar.open_time_ms // 86_400_000, []).append(i)
        full_days = {day: indexes for day, indexes in by_day.items()
                     if len(indexes) == 96 and bars[indexes[0]].open_time_ms % 86_400_000 == 0
                     and all(bars[right].open_time_ms - bars[left].open_time_ms == 900_000
                             for left, right in zip(indexes, indexes[1:]))}
        self.assertGreaterEqual(len(full_days), 4)
        for day, indexes in by_day.items():
            if day not in full_days:
                for i in indexes:
                    if i:
                        self.assertAlmostEqual(actual_returns[i - 1], simulated_returns[i - 1], places=11)
                continue
            weekday = datetime.fromtimestamp(bars[indexes[0]].open_time_ms / 1000, timezone.utc).weekday() < 5
            candidates = []
            for source_day, source_indexes in full_days.items():
                source_weekday = datetime.fromtimestamp(
                    bars[source_indexes[0]].open_time_ms / 1000, timezone.utc).weekday() < 5
                if source_weekday == weekday:
                    candidates.append([actual_returns[i - 1] for i in source_indexes])
            observed = [simulated_returns[i - 1] for i in indexes]
            self.assertTrue(any(all(abs(a - b) < 1e-11 for a, b in zip(observed, candidate))
                                for candidate in candidates), f"misaligned day {day}")

    def test_synthetic_path_rejects_missing_complete_weekend_stratum(self):
        from mu_strategy.research.edge_gate import synthetic_path

        bars = walk(5, bars=5 * 96 + 20)
        with self.assertRaisesRegex(ValueError, 'weekend'):
            synthetic_path(bars, random.Random(1))

    def test_default_universe_uses_every_published_stock_perpetual(self):
        from mu_strategy.research.edge_gate import stock_symbols

        manifest = SimpleNamespace(
            universe_snapshot=SimpleNamespace(stock_token_top=(
                {'inst_id': 'B-USDT-SWAP'}, {'inst_id': 'A-USDT-SWAP'})),
            datasets={('A-USDT-SWAP', '15m'): object(), ('A-USDT-SWAP', '1h'): object(),
                      ('B-USDT-SWAP', '15m'): object(), ('B-USDT-SWAP', '1h'): object()},
        )
        self.assertEqual(['A-USDT-SWAP', 'B-USDT-SWAP'],
                         stock_symbols(SimpleNamespace(manifest=manifest)))

    def test_zero_drift_baseline_fails_with_percentile_near_fifty(self):
        from mu_strategy.research.edge_gate import assess_symbol, summarize_gate
        from mu_strategy.strategies.registry import selected_strategy_groups

        rows = []
        for index in range(4):
            bars = walk(index, bars=96 * 28)
            config = selected_strategy_groups('MU-USDT-SWAP', ['baseline'])[0].config
            rows.append(assess_symbol(f'S{index}-USDT-SWAP', bars, config, simulations=30,
                                      seed=index, funding_annual=0.08))
        self.assertTrue(all(row.trade_count > 0 for row in rows))
        verdict = summarize_gate(rows, z_threshold=2)
        self.assertEqual('FAIL', verdict['verdict'])
        self.assertLessEqual(abs(verdict['mean_percentile'] - 50), 3 * verdict['standard_error'])

    def test_positive_drift_random_walk_baseline_is_unbiased_and_fails(self):
        from mu_strategy.research.edge_gate import assess_symbol, summarize_gate
        from mu_strategy.strategies.registry import selected_strategy_groups
        from mu_strategy.strategy import with_fee_profile

        config = with_fee_profile(selected_strategy_groups('MU-USDT-SWAP', ['baseline'])[0].config, 'market')
        drift = math.log(2) / (180 * 96)
        started = time.perf_counter()
        rows = [assess_symbol(f'S{seed}-USDT-SWAP', walk(seed, bars=96 * 45, drift=drift, sigma=.006),
                              config, simulations=20, seed=seed, funding_annual=0.08)
                for seed in range(101, 107)]
        verdict = summarize_gate(rows, z_threshold=2)
        self.assertTrue(all(row.trade_count >= 5 for row in rows))
        self.assertLessEqual(abs(verdict['mean_percentile'] - 50), 3 * verdict['standard_error'])
        self.assertEqual('FAIL', verdict['verdict'])
        self.assertLess(time.perf_counter() - started, 40)

    def test_trusted_hourly_mismatch_fails_and_matching_input_is_neutral(self):
        from mu_strategy.research.edge_gate import assess_symbol
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles
        from mu_strategy.strategies.registry import selected_strategy_groups

        bars = walk(12, bars=192)
        hourly = aggregate_candles(bars, interval='1h', base_interval='15m')
        config = selected_strategy_groups('MU-USDT-SWAP', ['baseline'])[0].config
        without = assess_symbol('MU-USDT-SWAP', bars, config, simulations=3, seed=9, funding_annual=.08)
        matching = assess_symbol('MU-USDT-SWAP', bars, config, simulations=3, seed=9,
                                 funding_annual=.08, hourly_candles=hourly)
        self.assertEqual(without, matching)
        tampered = [*hourly]
        tampered[2] = replace(tampered[2], close=tampered[2].close * 1.001)
        with self.assertRaisesRegex(ValueError, '1h'):
            assess_symbol('MU-USDT-SWAP', bars, config, simulations=1, seed=9,
                          funding_annual=.08, hourly_candles=tampered)
        extra = [*hourly, replace(hourly[-1], open_time_ms=hourly[-1].open_time_ms + 3_600_000)]
        with self.assertRaisesRegex(ValueError, '1h'):
            assess_symbol('MU-USDT-SWAP', bars, config, simulations=1, seed=9,
                          funding_annual=.08, hourly_candles=extra)

    def test_funding_rejects_fill_or_exit_outside_candle_times(self):
        from mu_strategy.research.edge_gate import funding_cost

        bars = [Candle(i * 900_000, 100, 100, 100, 100, 1) for i in range(5)]
        fill = Fill(1, 100, 1, 200, 2, 0)
        trade = Trade(0, 3 * 900_000, 100, 100, [fill], 0, 0, 0, 1, 'end')
        with self.assertRaisesRegex(ValueError, 'fill'):
            funding_cost(BacktestResult(10000, 10000, [trade], []), bars, 0)
        trade = replace(trade, fills=[replace(fill, time_ms=0)], exit_time_ms=3 * 900_000 + 1)
        with self.assertRaisesRegex(ValueError, 'exit'):
            funding_cost(BacktestResult(10000, 10000, [trade], []), bars, .08)


    def test_predictable_structure_passes_with_test_strategy(self):
        from mu_strategy.research.edge_gate import assess_symbol, summarize_gate

        rows = []
        for index in range(5):
            bars = walk(index + 50, bars=96 * 24)
            signs = [1 if random.Random(index * 100 + day).random() > 0.5 else -1 for day in range(24)]
            prices = [100.0]
            for j in range(96 * 24):
                day, slot = divmod(j, 96)
                change = (0.04 if signs[day - 1] > 0 else -0.02) if slot == 0 and day else 0.0
                if slot == 95:
                    change = 0.001 * signs[day]
                prices.append(prices[-1] * (1 + change))
            bars = [Candle(bar.open_time_ms, prices[j], max(prices[j], prices[j + 1]),
                           min(prices[j], prices[j + 1]), prices[j + 1], 1.0)
                    for j, bar in enumerate(bars)]
            rows.append(assess_symbol(f'S{index}-USDT-SWAP', bars, object(), simulations=40,
                                      seed=index, funding_annual=0, backtest_fn=test_engine))
        verdict = summarize_gate(rows, z_threshold=2)
        self.assertEqual('PASS', verdict['verdict'])

    def test_missing_and_stale_data_exit_without_network_or_cache_write(self):
        from mu_strategy.commands.edge_gate import main
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data' / 'live'
            output = Path(tmp) / 'report'
            with patch('socket.socket.connect', side_effect=AssertionError('network')) as network:
                with patch.object(TrustedDataStore, 'write_segmented_dataset', side_effect=AssertionError('write')):
                    with self.assertRaises(SystemExit) as caught:
                        main(['--data-dir', str(data), '--output-dir', str(output), '--simulations', '2'])
            network.assert_not_called()
            self.assertNotEqual(0, caught.exception.code)
            self.assertFalse(data.exists())
            self.assertFalse(output.exists())

    def test_stale_trusted_bundle_exits_before_writing_report(self):
        from mu_strategy.commands.edge_gate import main
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, TrustDecision

        with TemporaryDirectory() as tmp:
            output = Path(tmp) / 'report'
            context = SimpleNamespace(generation_id='g')
            bundle = SimpleNamespace(trust_decision=TrustDecision(False, HealthReason.STALE_BY_CLOCK),
                                     statuses_by_interval={}, candles_by_interval={})
            with patch('mu_strategy.commands.edge_gate.LoadTrustedBundle.open_context', return_value=context):
                with patch('mu_strategy.commands.edge_gate.stock_symbols', return_value=['MU-USDT-SWAP']):
                    with patch('mu_strategy.commands.edge_gate.load_trusted_candle_bundle', return_value=bundle) as loader:
                        with self.assertRaises(SystemExit) as caught:
                            main(['--data-dir', str(Path(tmp) / 'data' / 'live'),
                                  '--output-dir', str(output), '--simulations', '2'])
            self.assertNotEqual(0, caught.exception.code)
            self.assertFalse(output.exists())

    def test_same_seed_report_is_byte_identical(self):
        from mu_strategy.commands.edge_gate import main
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, TrustDecision
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles

        bars = walk(1)
        hourly = aggregate_candles(bars, interval='1h', base_interval='15m')
        context = SimpleNamespace(generation_id='generation')
        bundle = SimpleNamespace(trust_decision=TrustDecision(True, HealthReason.OK),
                                 candles_by_interval={'15m': bars, '1h': hourly})
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / 'report'
            argv = ['--data-dir', str(Path(tmp) / 'data' / 'live'), '--output-dir', str(output),
                    '--simulations', '8', '--seed', '7']
            with patch('mu_strategy.commands.edge_gate.LoadTrustedBundle.open_context', return_value=context):
                with patch('mu_strategy.commands.edge_gate.stock_symbols', return_value=['MU-USDT-SWAP']):
                    with patch('mu_strategy.commands.edge_gate.load_trusted_candle_bundle', return_value=bundle) as loader:
                        with patch('mu_strategy.commands.edge_gate._commit', return_value='abc'):
                            with patch('sys.stdout', new_callable=io.StringIO) as stdout:
                                main(argv)
                                first = ((output / 'edge_gate.json').read_bytes(),
                                         (output / 'edge_gate.md').read_bytes())
                                stdout.truncate(0)
                                stdout.seek(0)
                                main(argv)
                                second = ((output / 'edge_gate.json').read_bytes(),
                                          (output / 'edge_gate.md').read_bytes())
        self.assertEqual(180, loader.call_args.kwargs['days'])
        self.assertEqual(first, second)
        self.assertEqual(2, len(stdout.getvalue().splitlines()))
        self.assertEqual('generation', json.loads(first[0])['generation_id'])

    def test_funding_matches_exposure_time_rate(self):
        from mu_strategy.research.edge_gate import assess_symbol, funding_cost

        bars = [Candle(i * 900_000, 100, 100, 100, 100, 1) for i in range(192)]
        trade = Trade(0, 3 * 900_000, 100, 100,
                      [Fill(0, 100, 1, 200, 2, 0)], 0, 0, 0, 1, 'end')
        result = BacktestResult(10000, 10000, [trade], [])
        self.assertEqual(0, funding_cost(result, bars, 0))
        self.assertAlmostEqual(200 * (3 * 900_000 / (365 * 24 * 3600 * 1000)) * 0.08,
                               funding_cost(result, bars, 0.08))
        def engine(candles, context, *, config):
            fill = Fill(candles[0].open_time_ms, 100, 1, 200, 2, 0)
            trade = Trade(fill.time_ms, candles[3].open_time_ms, 100, 100,
                          [fill], 0, 0, 0, 1, 'end')
            return BacktestResult(10000, 10000, [trade],
                                  [(bar.open_time_ms, 10000) for bar in candles])
        zero = assess_symbol('TEST', bars, object(), simulations=1, seed=1,
                             funding_annual=0, backtest_fn=engine)
        paid = assess_symbol('TEST', bars, object(), simulations=1, seed=1,
                             funding_annual=0.08, backtest_fn=engine)
        self.assertAlmostEqual(funding_cost(result, bars, 0.08) / 10000,
                               zero.actual_return - paid.actual_return)


if __name__ == '__main__':
    unittest.main()
