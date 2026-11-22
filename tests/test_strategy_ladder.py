import json
import unittest
from contextlib import ExitStack, chdir, contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.backtest import run_backtest
from mu_strategy.execution.instruments import OKXInstrumentSpec
from mu_strategy.experiments.strategy_ladder import (
    CandidateDefinition,
    DEFAULT_MU_INSTRUMENT,
    DEFAULT_REPORT_PATH,
    FEE_GRID_BPS,
    MOMENTUM_HISTORY_HOURS,
    MOMENTUM_LOOKBACK_HOURS,
    SLIPPAGE_GRID_TICKS,
    StrategyLadderDataError,
    StrategyLadderOutputError,
    StrategyLadderResult,
    apply_adverse_tick_slippage,
    build_cli_instrument,
    build_cli_output_paths,
    build_conclusion_index,
    candidate_definitions,
    evaluate_strategy_ladder,
    momentum_target_long,
    overnight_target_long,
    render_strategy_ladder_html,
    render_strategy_ladder_report,
    run_long_only_candidate,
    run_strategy_ladder,
    summarize_windows,
)
from mu_strategy.experiments.walk_forward import (
    render_strategy_group_report,
    run_evaluator_walk_forward_backtests,
    run_strategy_group_walk_forward_backtests,
    run_walk_forward_backtests,
)
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import BacktestResult, Candle, Fill, Trade
from mu_strategy.strategies.registry import baseline_strategy_group
from tests.factories.trusted_publication import (
    write_generation_manifest_and_caches,
    write_generation_publication,
)


DAY_MS = 86_400_000
HOUR_MS = 3_600_000
QUARTER_HOUR_MS = 900_000


class StrategyLadderSignalTests(unittest.TestCase):
    def test_cli_instrument_canonicalizes_aliases_and_requires_non_mu_tick_size(self):
        mu = build_cli_instrument("MU", None)
        btc = build_cli_instrument("BTCUSDT", Decimal("0.01"))

        self.assertEqual("MU-USDT-SWAP", mu.inst_id)
        self.assertEqual(DEFAULT_MU_INSTRUMENT.tick_size, mu.tick_size)
        self.assertEqual("BTC-USDT-SWAP", btc.inst_id)
        self.assertEqual(Decimal("0.01"), btc.tick_size)
        with self.assertRaisesRegex(ValueError, "--tick-size is required"):
            build_cli_instrument("BTCUSDT", None)

    def test_cli_output_defaults_are_symbol_specific_without_changing_mu_paths(self):
        mu_paths = build_cli_output_paths(
            "MU",
            report_path=None,
            html_report_path=None,
            conclusion_path=None,
        )
        btc_paths = build_cli_output_paths(
            "BTCUSDT",
            report_path=None,
            html_report_path=None,
            conclusion_path=None,
        )

        self.assertEqual(
            (
                Path("reports/live/mu_okx_strategy_ladder.md"),
                Path("reports/live/mu_okx_strategy_ladder.html"),
                Path("reports/live/mu_okx_strategy_ladder_conclusions.json"),
            ),
            mu_paths,
        )
        self.assertEqual(
            (
                Path("reports/live/btc_usdt_swap_strategy_ladder.md"),
                Path("reports/live/btc_usdt_swap_strategy_ladder.html"),
                Path("reports/live/btc_usdt_swap_strategy_ladder_conclusions.json"),
            ),
            btc_paths,
        )
        self.assertTrue(set(mu_paths).isdisjoint(btc_paths))

    def test_direct_non_mu_run_uses_symbol_specific_default_artifacts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "live"
            now_ms = 1_780_300_800_000 + 10 * DAY_MS
            write_generation_publication(
                data_dir,
                symbol="BTC-USDT-SWAP",
                start_ms=now_ms - (9 * DAY_MS),
                end_ms=now_ms,
            )
            instrument = OKXInstrumentSpec("BTC-USDT-SWAP", Decimal("0.01"), Decimal("1"), Decimal("1"))

            with chdir(root):
                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=now_ms):
                    result = run_strategy_ladder(
                        symbol="BTCUSDT",
                        data_dir=data_dir,
                        window_days=1,
                        windows=1,
                        instrument=instrument,
                    )

            self.assertEqual("BTC-USDT-SWAP", result.symbol)
            self.assertTrue((root / "reports/live/btc_usdt_swap_strategy_ladder.md").exists())
            self.assertTrue((root / "reports/live/btc_usdt_swap_strategy_ladder.html").exists())
            self.assertTrue((root / "reports/live/btc_usdt_swap_strategy_ladder_conclusions.json").exists())
            self.assertFalse((root / DEFAULT_REPORT_PATH).exists())

    def test_candidate_definitions_are_deterministic_and_long_only_families(self):
        first = candidate_definitions()
        second = candidate_definitions()

        self.assertEqual(first, second)
        self.assertEqual(
            [
                "overnight_seasonality",
                "time_series_momentum_24h",
                "time_series_momentum_96h",
                "time_series_momentum_168h",
                "baseline",
            ],
            [item.candidate_id for item in first],
        )
        self.assertEqual(MOMENTUM_LOOKBACK_HOURS, tuple(item.lookback_hours for item in first[1:4]))

    def test_overnight_signal_enters_at_22_and_exits_at_midnight_utc(self):
        closed = [_hourly_candle(21, 100)]

        self.assertTrue(overnight_target_long(closed, 22 * HOUR_MS))
        self.assertTrue(overnight_target_long(closed, 23 * HOUR_MS))
        self.assertFalse(overnight_target_long(closed, DAY_MS))
        self.assertFalse(overnight_target_long(closed, DAY_MS + HOUR_MS))

    def test_overnight_signal_does_not_read_the_entry_bar(self):
        closed = [_hourly_candle(21, 100)]
        calm_entry_bar = _hourly_candle(22, 100)
        crashing_entry_bar = Candle(22 * HOUR_MS, 100, 1_000, 1, 2, 999_999)

        calm_signal = overnight_target_long(closed, calm_entry_bar.open_time_ms)
        crashing_signal = overnight_target_long(closed, crashing_entry_bar.open_time_ms)

        self.assertEqual(calm_signal, crashing_signal)
        self.assertTrue(calm_signal)

    def test_momentum_signal_sweeps_lookbacks_and_returns_only_long_or_flat(self):
        for lookback in MOMENTUM_LOOKBACK_HOURS:
            with self.subTest(lookback=lookback):
                rising = [_hourly_candle(index, 100 + index) for index in range(lookback + 1)]
                flat = [_hourly_candle(index, 100) for index in range(lookback + 1)]
                falling = [_hourly_candle(index, 500 - index) for index in range(lookback + 1)]

                self.assertIs(momentum_target_long(rising, lookback), True)
                self.assertIs(momentum_target_long(flat, lookback), False)
                self.assertIs(momentum_target_long(falling, lookback), False)
                self.assertFalse(momentum_target_long(rising[:-1], lookback))

    def test_end_index_matches_prefix_slice_without_copying(self):
        candles = [_hourly_candle(index, 100 + index) for index in range(30)]
        candles.append(Candle(30 * HOUR_MS, 130, 1_000, 1, 1, 999_999))

        for end_index in (1, 24, 25, 30):
            self.assertEqual(
                overnight_target_long(candles[:end_index], candles[end_index].open_time_ms),
                overnight_target_long(candles, candles[end_index].open_time_ms, end_index=end_index),
            )
            self.assertEqual(
                momentum_target_long(candles[:end_index], 24),
                momentum_target_long(candles, 24, end_index=end_index),
            )

        with self.assertRaisesRegex(ValueError, "not closed"):
            overnight_target_long(candles, candles[5].open_time_ms, end_index=6)

    def test_run_long_only_candidate_passes_end_index_without_prefix_slicing(self):
        definition = CandidateDefinition(
            "time_series_momentum_24h",
            "time_series_momentum",
            "momentum",
            "test",
            24,
        )
        candles = [_hourly_candle(index, 100 + index) for index in range(30)]
        observed_lengths: list[tuple[int, int]] = []

        def spy(series, lookback_hours, *, end_index=None):
            observed_lengths.append((len(series), end_index))
            return momentum_target_long(series, lookback_hours, end_index=end_index)

        with patch("mu_strategy.experiments.strategy_ladder.momentum_target_long", side_effect=spy):
            run_long_only_candidate(
                candles,
                definition=definition,
                fee_bps_per_side=0,
                slippage_ticks=0,
                instrument=DEFAULT_MU_INSTRUMENT,
            )

        self.assertTrue(observed_lengths)
        self.assertTrue(all(length == len(candles) and idx is not None for length, idx in observed_lengths))

    def test_momentum_signal_does_not_read_the_bar_it_trades(self):
        closed = [_hourly_candle(index, 100 + index) for index in range(25)]
        calm_trade_bar = _hourly_candle(25, 125)
        crashing_trade_bar = Candle(25 * HOUR_MS, 125, 1_000, 1, 2, 999_999)

        before_calm = momentum_target_long(closed, 24)
        before_crash = momentum_target_long(closed, 24)

        self.assertEqual(before_calm, before_crash)
        self.assertTrue(before_calm)
        self.assertEqual(calm_trade_bar.open_time_ms, crashing_trade_bar.open_time_ms)

    def test_momentum_uses_closed_pre_window_history_at_window_boundary(self):
        candles_15m = [
            Candle(index * QUARTER_HOUR_MS, 100, 101, 99, 100, 1_000)
            for index in range(96)
        ]
        candles_1h = [
            _hourly_candle(hour, 500 + hour)
            for hour in range(-MOMENTUM_HISTORY_HOURS, 24)
        ]
        definition = CandidateDefinition(
            "time_series_momentum_24h",
            "time_series_momentum",
            "momentum",
            "test",
            24,
        )

        windows = run_evaluator_walk_forward_backtests(
            candles_15m,
            candles_1h,
            evaluator=lambda segment_15m, hourly, _context: run_long_only_candidate(
                hourly,
                definition=definition,
                fee_bps_per_side=0,
                slippage_ticks=0,
                instrument=DEFAULT_MU_INSTRUMENT,
                execution_start_time_ms=segment_15m[0].open_time_ms,
                execution_end_time_ms=segment_15m[-1].open_time_ms + QUARTER_HOUR_MS,
            ),
            window_days=1,
            windows=1,
            history_hours=MOMENTUM_HISTORY_HOURS,
        )

        self.assertEqual(1, windows[0].result.trade_count)
        self.assertEqual(0, windows[0].result.trades[0].entry_time_ms)

    def test_local_candidate_emits_positive_units_and_never_a_short_state(self):
        definition = CandidateDefinition(
            "overnight_seasonality",
            "overnight_seasonality",
            "overnight",
            "test",
        )
        candles = [
            _hourly_candle(21, 100),
            _hourly_candle(22, 100),
            _hourly_candle(23, 110),
            _hourly_candle(24, 110),
        ]

        result = run_long_only_candidate(
            candles,
            definition=definition,
            fee_bps_per_side=0,
            slippage_ticks=0,
            instrument=DEFAULT_MU_INSTRUMENT,
        )

        self.assertEqual(1, result.trade_count)
        self.assertTrue(all(fill.units > 0 for trade in result.trades for fill in trade.fills))
        self.assertTrue(all(trade.max_stage == 1 for trade in result.trades))

    def test_forced_exit_does_not_use_hourly_close_beyond_window_boundary(self):
        definition = CandidateDefinition(
            "overnight_seasonality",
            "overnight_seasonality",
            "overnight",
            "test",
        )
        boundary_ms = (23 * HOUR_MS) + QUARTER_HOUR_MS
        calm = [_hourly_candle(21, 100), _hourly_candle(22, 100), _hourly_candle(23, 100)]
        future_spike = [*calm[:2], Candle(23 * HOUR_MS, 100, 10_000, 1, 9_000, 1_000)]

        results = [
            run_long_only_candidate(
                candles,
                definition=definition,
                fee_bps_per_side=0,
                slippage_ticks=0,
                instrument=DEFAULT_MU_INSTRUMENT,
                execution_start_time_ms=22 * HOUR_MS,
                execution_end_time_ms=boundary_ms,
            )
            for candles in (calm, future_spike)
        ]

        self.assertEqual(results[0], results[1])
        self.assertEqual(23 * HOUR_MS, results[0].trades[0].exit_time_ms)
        self.assertLessEqual(results[0].trades[0].exit_time_ms, boundary_ms)

    def test_insolvent_candidate_stays_flat_instead_of_opening_non_positive_units(self):
        definition = CandidateDefinition(
            "overnight_seasonality",
            "overnight_seasonality",
            "overnight",
            "test",
        )
        candles = [
            _hourly_candle(21, 100),
            _hourly_candle(22, 100),
            _hourly_candle(24, 0.1),
            _hourly_candle(45, 100),
            _hourly_candle(46, 100),
            _hourly_candle(48, 100),
        ]

        result = run_long_only_candidate(
            candles,
            definition=definition,
            fee_bps_per_side=10,
            slippage_ticks=0,
            instrument=DEFAULT_MU_INSTRUMENT,
        )

        self.assertLessEqual(result.ending_equity, 0)
        self.assertEqual(1, result.trade_count)
        self.assertTrue(all(fill.notional > 0 and fill.units > 0 and fill.fee >= 0 for fill in result.trades[0].fills))


class StrategyLadderCostTests(unittest.TestCase):
    def test_fee_slippage_grid_has_all_nine_cells_in_stable_order(self):
        candles_15m, candles_1h = _walk_forward_candles(days=2)

        evaluations = evaluate_strategy_ladder(
            candles_15m,
            candles_1h,
            symbol="MU-USDT-SWAP",
            instrument=DEFAULT_MU_INSTRUMENT,
            window_days=1,
            windows=1,
        )

        expected = [(fee, ticks) for fee in FEE_GRID_BPS for ticks in SLIPPAGE_GRID_TICKS]
        self.assertEqual(5, len(evaluations))
        for evaluation in evaluations:
            with self.subTest(candidate=evaluation.definition.candidate_id):
                self.assertEqual(expected, [(cell.fee_bps_per_side, cell.slippage_ticks) for cell in evaluation.stress_grid])

    def test_fee_and_tick_slippage_arithmetic_charges_both_sides(self):
        definition = CandidateDefinition(
            "overnight_seasonality",
            "overnight_seasonality",
            "overnight",
            "test",
        )
        candles = [
            _hourly_candle(21, 100),
            _hourly_candle(22, 100),
            _hourly_candle(23, 110),
            _hourly_candle(24, 110),
        ]
        instrument = OKXInstrumentSpec("MU-USDT-SWAP", Decimal("0.1"), Decimal("1"), Decimal("1"))

        result = run_long_only_candidate(
            candles,
            definition=definition,
            fee_bps_per_side=5,
            slippage_ticks=1,
            instrument=instrument,
        )

        entry_price = Decimal("100.1")
        exit_price = Decimal("109.9")
        units = Decimal("10000") / entry_price
        expected_fee = Decimal("10000") * Decimal("0.0005") + exit_price * units * Decimal("0.0005")
        expected_pnl = (exit_price - entry_price) * units - expected_fee
        self.assertAlmostEqual(float(expected_pnl), result.trades[0].pnl, places=9)
        self.assertAlmostEqual(10_000 + float(expected_pnl), result.ending_equity, places=9)

    def test_default_five_bp_zero_tick_baseline_matches_existing_market_profile(self):
        candles_15m, candles_1h = _walk_forward_candles(days=2)
        baseline = baseline_strategy_group("MU-USDT-SWAP")

        existing = run_walk_forward_backtests(
            candles_15m,
            candles_1h,
            config=baseline.config,
            window_days=1,
            windows=1,
        )
        evaluations = evaluate_strategy_ladder(
            candles_15m,
            candles_1h,
            symbol="MU-USDT-SWAP",
            instrument=DEFAULT_MU_INSTRUMENT,
            window_days=1,
            windows=1,
        )
        ladder_baseline = next(item for item in evaluations if item.definition.candidate_id == "baseline")

        self.assertAlmostEqual(
            summarize_windows(tuple(existing)).ending_equity,
            ladder_baseline.default_cell.summary.ending_equity,
            delta=Decimal("0.00000001"),
        )
        self.assertEqual(
            [window.result.trade_count for window in existing],
            [window.result.trade_count for window in ladder_baseline.default_cell.windows],
        )

    def test_reports_disclose_evaluated_leverage_and_account_return_basis(self):
        candles_15m, candles_1h = _walk_forward_candles(days=2)
        registered = baseline_strategy_group("MU-USDT-SWAP")
        evaluated = replace(registered, config=replace(registered.config, leverage=3.0))
        with patch("mu_strategy.experiments.strategy_ladder.baseline_strategy_group", return_value=evaluated):
            evaluations = evaluate_strategy_ladder(
                candles_15m,
                candles_1h,
                symbol="MU-USDT-SWAP",
                instrument=DEFAULT_MU_INSTRUMENT,
                window_days=1,
                windows=1,
            )
        index = build_conclusion_index(
            evaluations,
            DEFAULT_MU_INSTRUMENT,
            trusted_generation="report-generation",
            window_days=1,
            windows=1,
        )
        result = StrategyLadderResult(
            "MU-USDT-SWAP", "report-generation", (), DEFAULT_MU_INSTRUMENT, evaluations, index,
        )
        original_index = json.dumps(index, sort_keys=True)
        with patch(
            "mu_strategy.experiments.strategy_ladder.baseline_strategy_group",
            side_effect=AssertionError("rendering must use evaluated configuration"),
        ):
            markdown = render_strategy_ladder_report(result)
            dashboard = render_strategy_ladder_html(result)

        for output in (markdown, dashboard):
            self.assertIn("Configured leverage", output)
            self.assertIn("Account return", output)
            self.assertIn("sum(window ending equity) / sum(window starting equity) - 1", output)
            self.assertIn("not compounded across windows", output)
            self.assertIn("not leverage- or risk-normalized", output)
            self.assertIn("baseline uses staged position sizing", output)
        for evaluation in evaluations:
            candidate_id = evaluation.definition.candidate_id
            family = evaluation.definition.family
            leverage = "3x" if family == "baseline" else "1x"
            self.assertIn(f"| {candidate_id} | {family} | {leverage} |", markdown)
            self.assertIn(
                f'<td>{candidate_id}</td><td>{family}</td><td class="num">{leverage}</td>',
                dashboard,
            )
        self.assertEqual(original_index, json.dumps(index, sort_keys=True))
        self.assertEqual(5.0, registered.config.leverage)

    def test_default_report_discloses_five_x_baseline_and_one_x_local_candidates(self):
        candles_15m, candles_1h = _walk_forward_candles(days=2)
        evaluations = evaluate_strategy_ladder(
            candles_15m,
            candles_1h,
            symbol="MU-USDT-SWAP",
            instrument=DEFAULT_MU_INSTRUMENT,
            window_days=1,
            windows=1,
        )
        self.assertEqual(
            [1.0, 1.0, 1.0, 1.0, 5.0],
            [evaluation.configured_leverage for evaluation in evaluations],
        )

    def test_baseline_slippage_overlay_preserves_zero_tick_result_and_debits_two_sides(self):
        fill = Fill(0, 100, 1.0, 10_000, 100, 0)
        trade = Trade(0, HOUR_MS, 100, 110, [fill], 1_000, 0, 0.1, 1, "signal_flat")
        result = BacktestResult(10_000, 11_000, [trade], [(0, 10_000), (HOUR_MS, 11_000)])

        self.assertIs(result, apply_adverse_tick_slippage(result, Decimal("0.1"), 0))
        adjusted = apply_adverse_tick_slippage(result, Decimal("0.1"), 2)

        self.assertAlmostEqual(960, adjusted.trades[0].pnl)
        self.assertAlmostEqual(10_960, adjusted.ending_equity)
        self.assertAlmostEqual(100.2, adjusted.trades[0].entry_price)
        self.assertAlmostEqual(109.8, adjusted.trades[0].exit_price)
        self.assertEqual([(0, 9_980), (HOUR_MS, 10_960)], adjusted.equity_curve)

    def test_baseline_slippage_preserves_leveraged_return_semantics_for_multiple_fills(self):
        fills = [
            Fill(0, 100, 0.1, 10_000, 100, 2),
            Fill(HOUR_MS, 110, 0.1, 5_500, 50, 1),
        ]
        trade = Trade(0, 2 * HOUR_MS, 103.333333, 120, fills, 1_000, 3, 1_000 / 3_100, 2, "signal_flat")
        result = BacktestResult(10_000, 11_000, [trade], [(0, 10_000), (HOUR_MS, 10_500), (2 * HOUR_MS, 11_000)])

        adjusted = apply_adverse_tick_slippage(result, Decimal("0.1"), 2, leverage=5)

        self.assertAlmostEqual(940, adjusted.trades[0].pnl)
        self.assertAlmostEqual(940 / 3_100, adjusted.trades[0].return_pct)
        self.assertEqual(3, adjusted.trades[0].fees)
        self.assertAlmostEqual(10_940, adjusted.ending_equity)
        self.assertEqual([(0, 9_980), (HOUR_MS, 10_470), (2 * HOUR_MS, 10_940)], adjusted.equity_curve)


class StrategyLadderTrustedDataTests(unittest.TestCase):
    def test_missing_and_invalid_generations_fail_closed_with_typed_reason_and_no_outputs(self):
        expected_reasons = {
            "missing": HealthReason.MANIFEST_MISSING,
            "invalid": HealthReason.MANIFEST_INVALID,
        }
        for state, expected_reason in expected_reasons.items():
            with self.subTest(state=state), TemporaryDirectory() as tmp:
                root = Path(tmp)
                data_dir = root / "data" / "live"
                observed_at_ms = DAY_MS
                if state == "invalid":
                    write_generation_manifest_and_caches(
                        data_dir,
                        symbol="MU-USDT-SWAP",
                        days=1,
                        status="invalid",
                        integrity="invalid",
                    )
                paths = _artifact_paths(root)

                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=observed_at_ms):
                    with self.assertRaises(StrategyLadderDataError) as raised:
                        run_strategy_ladder(
                            data_dir=data_dir,
                            window_days=1,
                            windows=1,
                            report_path=paths[0],
                            html_report_path=paths[1],
                            conclusion_path=paths[2],
                        )

                self.assertEqual(expected_reason, raised.exception.reason)
                self.assertTrue(all(not path.exists() for path in paths))

    def test_cache_only_ladder_run_blocks_network_and_trusted_csv_writes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "live"
            now_ms = 1_780_300_800_000 + 10 * DAY_MS
            write_generation_publication(
                data_dir,
                symbol="MU-USDT-SWAP",
                start_ms=now_ms - (9 * DAY_MS),
                end_ms=now_ms,
            )
            paths = _artifact_paths(root)

            with _blocked_market_data_paths():
                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=now_ms):
                    result = run_strategy_ladder(
                        data_dir=data_dir,
                        window_days=1,
                        windows=1,
                        report_path=paths[0],
                        html_report_path=paths[1],
                        conclusion_path=paths[2],
                    )

            self.assertEqual("run-coverage", result.run_id)
            self.assertTrue(all(path.exists() for path in paths))
            self.assertEqual(
                ["overnight_seasonality", "time_series_momentum", "baseline"],
                [entry["family"] for entry in result.conclusion_index["entries"]],
            )

    def test_partial_available_history_fails_closed_before_outputs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "live"
            now_ms = 1_780_300_800_000 + 10 * DAY_MS
            write_generation_publication(
                data_dir,
                symbol="MU-USDT-SWAP",
                start_ms=now_ms - DAY_MS,
                end_ms=now_ms,
            )
            paths = _artifact_paths(root)

            with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=now_ms):
                with self.assertRaises(StrategyLadderDataError) as raised:
                    run_strategy_ladder(
                        data_dir=data_dir,
                        window_days=1,
                        windows=1,
                        report_path=paths[0],
                        html_report_path=paths[1],
                        conclusion_path=paths[2],
                    )

            self.assertEqual(HealthReason.INSUFFICIENT_COVERAGE, raised.exception.reason)
            self.assertTrue(all(not path.exists() for path in paths))

    def test_all_output_options_reject_trusted_data_store_targets_without_writes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "live"
            protected_targets = (
                ("report_path", data_dir / "current.json"),
                ("html_report_path", data_dir / "generations" / "run" / "15m.csv"),
                ("conclusion_path", data_dir / "conclusions.json"),
            )
            for option, protected in protected_targets:
                with self.subTest(option=option):
                    protected.parent.mkdir(parents=True, exist_ok=True)
                    protected.write_bytes(b"sentinel")
                    paths = _artifact_paths(root / option)
                    kwargs = dict(zip(("report_path", "html_report_path", "conclusion_path"), paths))
                    kwargs[option] = protected

                    with self.assertRaises(StrategyLadderOutputError):
                        run_strategy_ladder(data_dir=data_dir, window_days=1, windows=1, **kwargs)

                    self.assertEqual(b"sentinel", protected.read_bytes())
                    self.assertTrue(all(not path.exists() for path in paths))

    def test_output_paths_must_be_distinct_before_data_access(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            duplicate = root / "reports" / "ladder"

            with self.assertRaisesRegex(StrategyLadderOutputError, "must be distinct"):
                run_strategy_ladder(
                    data_dir=root / "missing-data",
                    window_days=1,
                    windows=1,
                    report_path=duplicate,
                    html_report_path=duplicate,
                    conclusion_path=root / "reports" / "conclusions.json",
                )

            self.assertFalse(duplicate.parent.exists())

    def test_same_trusted_generation_produces_byte_identical_outputs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "live"
            now_ms = 1_780_300_800_000 + 10 * DAY_MS
            write_generation_publication(
                data_dir,
                symbol="MU-USDT-SWAP",
                start_ms=now_ms - (9 * DAY_MS),
                end_ms=now_ms,
                run_id="deterministic-run",
            )
            first = _artifact_paths(root / "first")
            second = _artifact_paths(root / "second")

            with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=now_ms):
                run_strategy_ladder(
                    data_dir=data_dir,
                    window_days=1,
                    windows=1,
                    report_path=first[0],
                    html_report_path=first[1],
                    conclusion_path=first[2],
                )
                run_strategy_ladder(
                    data_dir=data_dir,
                    window_days=1,
                    windows=1,
                    report_path=second[0],
                    html_report_path=second[1],
                    conclusion_path=second[2],
                )

            self.assertEqual([path.read_bytes() for path in first], [path.read_bytes() for path in second])

    def test_shared_evaluator_seam_keeps_existing_walk_forward_report_bytes(self):
        candles_15m, candles_1h = _walk_forward_candles(days=2)
        group = baseline_strategy_group("MU-USDT-SWAP")

        legacy = run_strategy_group_walk_forward_backtests(
            candles_15m,
            candles_1h,
            groups=[group],
            window_days=1,
            windows=1,
        )
        via_seam = run_evaluator_walk_forward_backtests(
            candles_15m,
            candles_1h,
            evaluator=lambda segment, _hourly, context: run_backtest(segment, context, config=group.config),
            window_days=1,
            windows=1,
        )
        recreated = [replace(legacy[0], windows=via_seam)]

        self.assertEqual(
            render_strategy_group_report(legacy, symbol="MU-USDT-SWAP", data_files=[]).encode(),
            render_strategy_group_report(recreated, symbol="MU-USDT-SWAP", data_files=[]).encode(),
        )


def _hourly_candle(hour: int, price: float) -> Candle:
    return Candle(hour * HOUR_MS, price, price, price, price, 1_000)


def _walk_forward_candles(*, days: int) -> tuple[list[Candle], list[Candle]]:
    start = 1_780_300_800_000  # 2026-06-01, within the pinned calendar.
    quarter_count = days * 24 * 4
    hourly_count = days * 24
    candles_15m = [
        Candle(start + index * QUARTER_HOUR_MS, 100 + index / 10, 101 + index / 10, 99 + index / 10, 100.5 + index / 10, 1_000)
        for index in range(quarter_count)
    ]
    candles_1h = [
        Candle(start + index * HOUR_MS, 100 + index / 2, 101 + index / 2, 99 + index / 2, 100.5 + index / 2, 4_000)
        for index in range(hourly_count)
    ]
    return candles_15m, candles_1h


def _artifact_paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "ladder.md", root / "ladder.html", root / "conclusions.json"


@contextmanager
def _blocked_market_data_paths():
    with ExitStack() as stack:
        for target in (
            "mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical",
            "mu_strategy.market_data.trusted_data.refresh.fetch_okx_incremental",
            "mu_strategy.market_data.universe.fetch_okx_swap_tickers",
        ):
            stack.enter_context(patch(target, side_effect=AssertionError("network must not be used")))
        stack.enter_context(
            patch(
                "mu_strategy.market_data.trusted_data.store.TrustedDataStore.write_csv",
                side_effect=AssertionError("trusted store write must not be used"),
            )
        )
        stack.enter_context(
            patch(
                "mu_strategy.market_data.trusted_data.store.TrustedDataStore.write_segmented_dataset",
                side_effect=AssertionError("trusted segmented store write must not be used"),
            )
        )
        yield


if __name__ == "__main__":
    unittest.main()
