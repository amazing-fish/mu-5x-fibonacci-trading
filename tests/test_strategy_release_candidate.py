import json
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mu_strategy.commands.build_strategy_release_candidate import (
    CandidateGenerationRequest,
    GitState,
    build_strategy_release_candidate,
)

from mu_strategy.experiments.release_candidate import (
    HistoricalTrustedGeneration,
    HistoricalGenerationError,
    HistoricalTrustedGenerationReader,
    run_release_experiment,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.models import BacktestResult, Candle, Trade
from mu_strategy.research.strategy_releases import (
    BacktestAssumptionsV1,
    ExperimentWindow,
    ExperimentWindowRole,
    FillModel,
    PartialFillModel,
    StrategyConfigPayloadV1,
    StrategyReleaseCandidateV1,
    TrustedExperimentDatasetV1,
)
from mu_strategy.strategies.registry import baseline_strategy_group


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TRACKED_DATA_DIR = REPOSITORY_ROOT / "data" / "live"
TRACKED_RUN_ID = "e702be27d2de4b2d92b12bf01c70d02d"


class HistoricalTrustedGenerationReaderTests(unittest.TestCase):
    def test_reads_explicit_tracked_generation_without_current_pointer_or_refresh(self):
        reader = HistoricalTrustedGenerationReader(data_dir=TRACKED_DATA_DIR)

        with patch.object(TrustedDataStore, "read_manifest", side_effect=AssertionError("current pointer used")):
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.refresh_with_okx_provider",
                side_effect=AssertionError("refresh used"),
            ) as refresh:
                generation = reader.read(run_id=TRACKED_RUN_ID, symbol="MU-USDT-SWAP")

        self.assertEqual(TRACKED_RUN_ID, generation.reference.run_id)
        self.assertEqual(("5m", "15m", "1h"), generation.reference.effective_intervals)
        self.assertEqual(35_257, len(generation.candles_by_interval["5m"]))
        self.assertEqual(11_752, len(generation.candles_by_interval["15m"]))
        self.assertEqual(2_938, len(generation.candles_by_interval["1h"]))
        self.assertEqual("fresh", generation.published_freshness_by_interval["15m"])
        refresh.assert_not_called()

    def test_reader_is_independent_from_current_pointer_contents(self):
        with TemporaryDirectory() as tmp:
            data_dir = _copy_generation(Path(tmp))
            (data_dir / "current.json").write_text('{"schema_version":1,"generation_id":"different"}', encoding="utf-8")

            generation = HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                run_id=TRACKED_RUN_ID,
                symbol="MU-USDT-SWAP",
            )

        self.assertEqual(TRACKED_RUN_ID, generation.reference.run_id)

    def test_reader_rejects_content_hash_mismatch(self):
        with TemporaryDirectory() as tmp:
            data_dir = _copy_generation(Path(tmp))
            csv_path = data_dir / "generations" / TRACKED_RUN_ID / "okx" / "MU-USDT-SWAP" / "15m.csv"
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            header = lines[0].split(",")
            first_row = lines[1].split(",")
            close_index = header.index("close")
            first_row[close_index] = str(float(first_row[close_index]) + 1)
            lines[1] = ",".join(first_row)
            csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(HistoricalGenerationError, "content SHA-256"):
                HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                    run_id=TRACKED_RUN_ID,
                    symbol="MU-USDT-SWAP",
                )

    def test_reader_rejects_manifest_identity_schema_and_source_path_mismatch(self):
        cases = (
            ("run_id", "b" * 32, "run_id"),
            ("schema_version", 2, "schema_version"),
            ("source_file", "../../escape.csv", "source_file"),
        )
        for field_name, value, message in cases:
            with self.subTest(field=field_name):
                with TemporaryDirectory() as tmp:
                    data_dir = _copy_generation(Path(tmp))
                    manifest_path = data_dir / "generations" / TRACKED_RUN_ID / "manifest.json"
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if field_name == "source_file":
                        payload["symbols"]["MU-USDT-SWAP"]["intervals"]["15m"][field_name] = value
                    else:
                        payload[field_name] = value
                    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

                    with self.assertRaisesRegex(HistoricalGenerationError, message):
                        HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                            run_id=TRACKED_RUN_ID,
                            symbol="MU-USDT-SWAP",
                        )


class ReleaseExperimentRunnerTests(unittest.TestCase):
    def test_runner_uses_exact_independent_windows_and_canonical_summaries(self):
        generation, windows = _synthetic_generation_and_windows()
        config = baseline_strategy_group("MU-USDT-SWAP").config
        results = (
            _backtest_result(10_010.0, pnl=10.0),
            _backtest_result(9_990.0, pnl=-10.0),
            _backtest_result(10_000.0, pnl=None),
        )

        with patch("mu_strategy.experiments.release_candidate.build_hourly_context", return_value={}) as context:
            with patch("mu_strategy.experiments.release_candidate.run_backtest", side_effect=results) as backtest:
                summaries = run_release_experiment(
                    generation,
                    config=config,
                    windows=windows,
                    assumptions=_assumptions(),
                )

        self.assertEqual(tuple(ExperimentWindowRole), tuple(item.role for item in summaries))
        self.assertEqual("10010", summaries[0].ending_equity)
        self.assertEqual("10", summaries[0].gross_profit)
        self.assertEqual("0", summaries[0].gross_loss)
        self.assertEqual("0.001", summaries[0].total_return_pct)
        self.assertEqual("10", summaries[1].gross_loss)
        self.assertNotIn("inf", json.dumps([item.to_dict() for item in summaries]).lower())
        self.assertEqual(3, backtest.call_count)
        self.assertEqual(3, context.call_count)
        for index, call in enumerate(backtest.call_args_list):
            segment = call.args[0]
            self.assertTrue(segment)
            self.assertTrue(all(windows[index].start_ms <= bar.open_time_ms < windows[index].end_ms for bar in segment))
            self.assertEqual(float(_assumptions().starting_equity), call.kwargs["starting_equity"])

    def test_runner_is_byte_deterministic_and_does_not_mutate_generation(self):
        generation, windows = _synthetic_generation_and_windows()
        config = baseline_strategy_group("MU-USDT-SWAP").config
        before = tuple(generation.candles_by_interval["15m"])

        first = run_release_experiment(generation, config=config, windows=windows, assumptions=_assumptions())
        second = run_release_experiment(generation, config=config, windows=windows, assumptions=_assumptions())

        self.assertEqual([item.to_dict() for item in first], [item.to_dict() for item in second])
        self.assertEqual(before, generation.candles_by_interval["15m"])

    def test_runner_rejects_assumption_or_window_mismatch(self):
        generation, windows = _synthetic_generation_and_windows()
        config = baseline_strategy_group("MU-USDT-SWAP").config

        with self.assertRaisesRegex(ValueError, "fee"):
            run_release_experiment(
                generation,
                config=config,
                windows=windows,
                assumptions=BacktestAssumptionsV1(
                    starting_equity="10000",
                    fee_profile="limit",
                    fee_rate="0.0002",
                    fill_model=FillModel.DETERMINISTIC_OHLC,
                    slippage_bps="0",
                    partial_fill_model=PartialFillModel.NONE,
                ),
            )

        shifted = (replace(windows[0], start_ms=windows[0].start_ms - 900_000, input_start_ms=windows[0].start_ms - 900_000), *windows[1:])
        with self.assertRaisesRegex(ValueError, "outside pinned data"):
            run_release_experiment(generation, config=config, windows=shifted, assumptions=_assumptions())


class CandidateGenerationTests(unittest.TestCase):
    def test_generation_rejects_dirty_or_non_exact_git_state(self):
        generation, windows = _synthetic_generation_and_windows()
        exact_sha = "1" * 40

        for git_state, message in (
            (GitState(head_sha=exact_sha, is_clean=False), "clean"),
            (GitState(head_sha="2" * 40, is_clean=True), "exact"),
        ):
            with self.subTest(message=message), TemporaryDirectory() as tmp:
                reader = Mock()
                request = _candidate_request(Path(tmp), windows, exact_sha)
                with self.assertRaisesRegex(ValueError, message):
                    build_strategy_release_candidate(
                        request,
                        git_state_provider=lambda _root, state=git_state: state,
                        generation_reader=reader,
                    )
                reader.read.assert_not_called()

    def test_generation_uses_registry_identity_and_writes_canonical_ignored_output(self):
        generation, windows = _synthetic_generation_and_windows()
        exact_sha = "1" * 40
        reader = Mock()
        reader.read.return_value = generation

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = _candidate_request(root, windows, exact_sha)
            candidate, output_path = build_strategy_release_candidate(
                request,
                git_state_provider=lambda _root: GitState(head_sha=exact_sha, is_clean=True),
                generation_reader=reader,
            )

            group = baseline_strategy_group(request.symbol)
            self.assertEqual(group.rule.strategy_rule_id, candidate.strategy_rule_id)
            self.assertEqual(group.name, candidate.strategy_name)
            self.assertEqual(
                StrategyConfigPayloadV1.from_config(group.config).to_dict(),
                candidate.strategy_config.to_dict(),
            )
            self.assertEqual(
                root / "data" / "strategy-release-candidates" / f"{candidate.candidate_fingerprint}.json",
                output_path,
            )
            self.assertIn(
                "data/strategy-release-candidates/",
                (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines(),
            )
            reloaded = StrategyReleaseCandidateV1.from_dict(
                json.loads(output_path.read_text(encoding="utf-8"))
            )
            self.assertEqual(candidate.to_dict(), reloaded.to_dict())
            self.assertEqual(
                json.dumps(candidate.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                output_path.read_text(encoding="utf-8"),
            )
            reader.read.assert_called_once_with(request.run_id, request.symbol)

    def test_generation_has_no_current_refresh_provider_or_broker_side_effects(self):
        generation, windows = _synthetic_generation_and_windows()
        exact_sha = "1" * 40
        reader = Mock()
        reader.read.return_value = generation

        with TemporaryDirectory() as tmp, \
            patch("mu_strategy.market_data.trusted_data.store.TrustedDataStore._read_current_manifest") as current, \
            patch("mu_strategy.market_data.trusted_data.refresh.refresh_with_okx_provider") as refresh, \
            patch("mu_strategy.market_data.trusted_data.refresh.OKXMarketDataProvider.fetch_history") as fetch, \
            patch("mu_strategy.live.okx.OKXRestClient.get_balance") as private_read, \
            patch("mu_strategy.live.okx.OKXRestClient.set_leverage") as leverage, \
            patch("mu_strategy.live.okx.OKXRestClient.place_demo_order") as submit, \
            patch("mu_strategy.live.okx.OKXRestClient.cancel_order") as cancel:
            build_strategy_release_candidate(
                _candidate_request(Path(tmp), windows, exact_sha),
                git_state_provider=lambda _root: GitState(head_sha=exact_sha, is_clean=True),
                generation_reader=reader,
            )

        for prohibited in (current, refresh, fetch, private_read, leverage, submit, cancel):
            prohibited.assert_not_called()


def _copy_generation(root: Path) -> Path:
    data_dir = root / "data" / "live"
    target = data_dir / "generations" / TRACKED_RUN_ID
    target.parent.mkdir(parents=True)
    shutil.copytree(TRACKED_DATA_DIR / "generations" / TRACKED_RUN_ID, target)
    return data_dir


def _assumptions() -> BacktestAssumptionsV1:
    return BacktestAssumptionsV1(
        starting_equity="10000",
        fee_profile="market",
        fee_rate="0.0005",
        fill_model=FillModel.DETERMINISTIC_OHLC,
        slippage_bps="0",
        partial_fill_model=PartialFillModel.NONE,
    )


def _candidate_request(
    repository_root: Path,
    windows: tuple[ExperimentWindow, ...],
    evaluated_code_commit_sha: str,
) -> CandidateGenerationRequest:
    return CandidateGenerationRequest(
        repository_root=repository_root,
        data_dir=repository_root / "data" / "live",
        run_id="a" * 32,
        symbol="MU-USDT-SWAP",
        evaluated_code_commit_sha=evaluated_code_commit_sha,
        windows=windows,
    )


def _synthetic_generation_and_windows() -> tuple[HistoricalTrustedGeneration, tuple[ExperimentWindow, ...]]:
    start = 1_700_000_000_000
    interval_ms = 900_000
    candles_15m = tuple(
        Candle(start + index * interval_ms, 100 + index, 101 + index, 99 + index, 100.5 + index, 1000)
        for index in range(12)
    )
    candles_1h = tuple(
        Candle(start + index * 3_600_000, 100 + index, 101 + index, 99 + index, 100.5 + index, 1000)
        for index in range(3)
    )
    reference = TrustedExperimentDatasetV1(
        run_id="a" * 32,
        symbol="MU-USDT-SWAP",
        manifest_schema_version=3,
        requested_intervals=("15m", "1h"),
        effective_intervals=("15m", "1h"),
        content_sha256_by_interval=(("15m", "a" * 64), ("1h", "b" * 64)),
    )
    generation = HistoricalTrustedGeneration(
        reference=reference,
        candles_by_interval={"15m": candles_15m, "1h": candles_1h},
        published_freshness_by_interval={"15m": "fresh", "1h": "fresh"},
        completed_at_ms=start + 12 * interval_ms,
    )
    width = 4 * interval_ms
    windows = (
        ExperimentWindow(ExperimentWindowRole.TRAIN, start, start, start + width),
        ExperimentWindow(ExperimentWindowRole.VALIDATION, start + width, start + width, start + 2 * width),
        ExperimentWindow(ExperimentWindowRole.OUT_OF_SAMPLE, start + 2 * width, start + 2 * width, start + 3 * width),
    )
    return generation, windows


def _backtest_result(ending_equity: float, *, pnl: float | None) -> BacktestResult:
    trades = []
    if pnl is not None:
        trades.append(
            Trade(
                entry_time_ms=1,
                exit_time_ms=2,
                entry_price=100,
                exit_price=100 + pnl,
                fills=[],
                pnl=pnl,
                fees=0,
                return_pct=pnl / 10_000,
                max_stage=1,
                exit_reason="end_of_data",
            )
        )
    return BacktestResult(
        starting_equity=10_000,
        ending_equity=ending_equity,
        trades=trades,
        equity_curve=[(1, 10_000), (2, ending_equity)],
    )


if __name__ == "__main__":
    unittest.main()
