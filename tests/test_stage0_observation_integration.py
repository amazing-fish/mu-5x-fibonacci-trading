import io
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.demo_trading import DemoTradingConfig, run_once
from mu_strategy.market_data.trusted_data.contracts import (
    HealthReason,
    RefreshAttemptStatus,
    UniverseSnapshot,
)
from mu_strategy.market_data.universe import OKXSwapTicker
from mu_strategy.models import Candle, EntryDecisionCode
from mu_strategy.observations import (
    DEFAULT_STAGE0_OBSERVATION_LOG,
    JsonlObservationRepository,
    ObservationCycleInvalidError,
    ObservationFailureCode,
    ObservationOutcome,
    ObservationWriteError,
)
from tests.factories.scan_cycle import scan_result as _scan, trusted_scan_bundle as _bundle
from tests.factories.trusted_publication import write_generation_publication


class Stage0ObservationIntegrationTests(unittest.TestCase):
    def test_trusted_data_block_persists_input_outcome_without_calling_scanner(self):
        calls = []
        repository = RecordingRepository()

        result = _run(
            repository=repository,
            bundle=_bundle(allowed=False, reason=HealthReason.MANIFEST_INVALID),
            scanner=lambda *args, **kwargs: calls.append("scanner"),
        )

        self.assertEqual([], calls)
        self.assertEqual([], result["orders"])
        observation = repository.cycles[0].observations[0]
        self.assertIs(ObservationOutcome.DATA_GATE_BLOCKED, observation.outcome)
        self.assertIs(EntryDecisionCode.MARKET_DATA_UNAVAILABLE, observation.decision_code)
        self.assertFalse(observation.trust_allowed)

    def test_scanner_exception_is_sanitized_and_persisted_as_scan_failed(self):
        repository = RecordingRepository()

        def failing_scanner(*args, **kwargs):
            raise RuntimeError("api_key=private-value scanner exploded")

        result = _run(repository=repository, scanner=failing_scanner)

        self.assertEqual([], result["orders"])
        observation = repository.cycles[0].observations[0]
        self.assertIs(ObservationOutcome.SCAN_FAILED, observation.outcome)
        self.assertIs(ObservationFailureCode.SCANNER_EXCEPTION, observation.failure_code)
        self.assertEqual("RuntimeError", observation.error_type)
        self.assertNotIn("private-value", observation.error_message)

    def test_unknown_typed_result_fails_closed_without_action_inference(self):
        repository = RecordingRepository()

        result = _run(
            repository=repository,
            scanner=lambda *args, **kwargs: _scan(EntryDecisionCode.UNKNOWN, action="enter", reason="looks ready"),
        )

        self.assertEqual([], result["orders"])
        observation = repository.cycles[0].observations[0]
        self.assertIs(ObservationOutcome.SCAN_FAILED, observation.outcome)
        self.assertIs(ObservationFailureCode.SCANNER_RESULT_INVALID, observation.failure_code)

    def test_structurally_invalid_typed_result_is_persisted_as_scan_failed(self):
        repository = RecordingRepository()

        result = _run(
            repository=repository,
            scanner=lambda *args, **kwargs: _scan(
                EntryDecisionCode.NO_CONFIRMED_FIB_RETEST,
                symbol="ETH-USDT-SWAP",
            ),
        )

        self.assertEqual([], result["orders"])
        observation = repository.cycles[0].observations[0]
        self.assertIs(ObservationOutcome.SCAN_FAILED, observation.outcome)
        self.assertIs(ObservationFailureCode.SCANNER_RESULT_INVALID, observation.failure_code)
        self.assertIn("symbol", observation.error_message)

    def test_non_numeric_scan_field_is_persisted_as_scan_failed(self):
        repository = RecordingRepository()

        result = _run(
            repository=repository,
            scanner=lambda *args, **kwargs: replace(
                _scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST),
                last_close="not-a-number",
            ),
        )

        self.assertEqual([], result["orders"])
        observation = repository.cycles[0].observations[0]
        self.assertIs(ObservationOutcome.SCAN_FAILED, observation.outcome)
        self.assertIs(ObservationFailureCode.SCANNER_RESULT_INVALID, observation.failure_code)
        self.assertIn("last_close", observation.error_message)

    def test_overflowing_scan_field_is_persisted_as_scan_failed(self):
        repository = RecordingRepository()

        result = _run(
            repository=repository,
            scanner=lambda *args, **kwargs: replace(
                _scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST),
                last_close=10**10000,
            ),
        )

        self.assertEqual([], result["orders"])
        observation = repository.cycles[0].observations[0]
        self.assertIs(ObservationOutcome.SCAN_FAILED, observation.outcome)
        self.assertIs(ObservationFailureCode.SCANNER_RESULT_INVALID, observation.failure_code)
        self.assertIn("last_close", observation.error_message)

    def test_wait_block_and_ready_use_typed_disposition_not_free_text(self):
        cases = (
            (EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, ObservationOutcome.NORMAL_NO_ACTION),
            (EntryDecisionCode.WAITING_SECOND_PULLBACK, ObservationOutcome.NORMAL_NO_ACTION),
            (EntryDecisionCode.REGIME_BLOCKED, ObservationOutcome.NORMAL_NO_ACTION),
            (EntryDecisionCode.SIGNAL_CANDLE_TOO_WIDE, ObservationOutcome.NORMAL_NO_ACTION),
            (EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, ObservationOutcome.READY_FOR_REVIEW),
        )
        for decision_code, expected in cases:
            with self.subTest(decision_code=decision_code):
                repository = RecordingRepository()
                _run(
                    repository=repository,
                    scanner=lambda *args, code=decision_code, **kwargs: _scan(
                        code,
                        action="skip",
                        reason="presentation text must not control classification",
                    ),
                )
                self.assertIs(expected, repository.cycles[0].observations[0].outcome)

    def test_observation_sidecar_keeps_legacy_payload_exact_and_calls_no_mutation(self):
        baseline = _run(
            repository=None,
            broker=RecordingBroker(),
            scanner=lambda *args, **kwargs: _scan(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY),
        )
        repository = RecordingRepository()
        broker = RecordingBroker()

        observed = _run(
            repository=repository,
            broker=broker,
            scanner=lambda *args, **kwargs: _scan(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY),
        )

        self.assertEqual(baseline, observed)
        self.assertEqual(
            {
                "symbol",
                "action",
                "reason",
                "last_close",
                "regime_1h",
                "rsi14",
                "macd_hist",
                "macd_hist_prev",
                "fib_level",
                "fib_distance_pct",
                "trigger_price",
                "initial_stop",
                "signal_time_ms",
                "source",
                "run_id",
                "observed_at_ms",
                "second_pullback_wait_bars",
                "data_files",
            },
            set(observed["scans"][0]),
        )
        mutation_names = {"set_leverage", "place_limit_buy", "cancel_order"}
        self.assertTrue(mutation_names.isdisjoint(call[0] for call in broker.calls))

    def test_jsonl_path_constructs_repository_and_is_restart_readable(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "stage0.jsonl"
            _run(
                repository=None,
                observation_log_path=path,
                scanner=lambda *args, **kwargs: _scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST),
            )

            cycles = JsonlObservationRepository(path).read_cycles()

        self.assertEqual(1, len(cycles))
        self.assertIs(ObservationOutcome.NORMAL_NO_ACTION, cycles[0].observations[0].outcome)

    def test_cli_dry_run_configures_versioned_observation_log_without_changing_stdout_shape(self):
        from mu_strategy.commands.okx_demo_loop import main

        captured = {}

        def runner(config, broker):
            captured["path"] = config.observation_log_path
            return {"mode": "dry_run", "scans": [], "orders": [], "expired_orders": []}

        stdout = io.StringIO()
        exit_code = main(["--once", "--dry-run"], stdout=stdout, runner=runner)

        self.assertEqual(0, exit_code)
        self.assertEqual(DEFAULT_STAGE0_OBSERVATION_LOG, captured["path"])
        self.assertNotIn("observation", stdout.getvalue())

    def test_default_strict_loader_persists_canonical_hashes_without_network_refresh(self):
        now_ms = int(time.time() * 1000)
        end_ms = (now_ms // 300_000) * 300_000 - 300_000
        start_ms = end_ms - (2 * 86_400_000)
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "trusted"
            observation_path = Path(tmp) / "observations.jsonl"
            write_generation_publication(
                data_dir,
                symbol="BTC-USDT-SWAP",
                start_ms=start_ms,
                end_ms=end_ms,
                run_id="strict-run",
            )
            with patch("mu_strategy.market_data.service.cached_historical") as legacy_fetch:
                with patch("mu_strategy.market_data.trusted_data.refresh.refresh_with_okx_provider") as refresh:
                    payload = run_once(
                        DemoTradingConfig(
                            universe_limit=0,
                            days=1,
                            data_dir=data_dir,
                            dry_run=True,
                            watchlist_symbols=("BTC-USDT-SWAP",),
                            observation_log_path=observation_path,
                        ),
                        broker=None,
                        scanner=lambda *args, **kwargs: _scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST),
                    )

            cycles = JsonlObservationRepository(observation_path).read_cycles()

        legacy_fetch.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual("strict-run", payload["run_id"])
        observation = cycles[0].observations[0]
        self.assertTrue(observation.trust_allowed)
        self.assertEqual({"5m", "15m", "1h"}, set(observation.content_sha256_by_interval))

    def test_two_symbols_commit_one_cycle_with_exactly_one_observation_each(self):
        repository = RecordingRepository()
        ids = iter(("cycle-1", "observation-1", "observation-2"))
        symbols = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")

        run_once(
            DemoTradingConfig(universe_limit=2, dry_run=True, watchlist_symbols=()),
            broker=None,
            universe_provider=lambda limit: [OKXSwapTicker(symbol, 100.0, 1000.0) for symbol in symbols],
            candle_loader=lambda symbol, **kwargs: _bundle(symbol=symbol),
            scanner=lambda symbol, *args, **kwargs: _scan(
                EntryDecisionCode.NO_CONFIRMED_FIB_RETEST,
                symbol=symbol,
            ),
            observation_repository=repository,
            observation_clock=FixedClock(),
            observation_id_factory=lambda: next(ids),
        )

        self.assertEqual(1, len(repository.cycles))
        observations = repository.cycles[0].observations
        self.assertEqual(symbols, tuple(observation.symbol for observation in observations))
        self.assertEqual(2, len({observation.observation_id for observation in observations}))

    def test_persistence_failure_invalidates_cycle_and_returns_no_payload(self):
        class FailingRepository:
            def append_cycle(self, cycle):
                raise ObservationWriteError("disk full")

        with self.assertRaisesRegex(ObservationCycleInvalidError, "not promotion evidence"):
            _run(repository=FailingRepository())


class FixedClock:
    def now_ms(self):
        return 1_000


class RecordingRepository:
    def __init__(self):
        self.cycles = []

    def append_cycle(self, cycle):
        self.cycles.append(cycle)


class RecordingBroker:
    def __init__(self):
        self.calls = []

    def get_instruments(self, *, inst_type, inst_id):
        self.calls.append(("get_instruments", inst_type, inst_id))
        return {"code": "0", "data": [{"instId": inst_id, "tickSz": "0.1", "lotSz": "0.01", "ctVal": "0.01"}]}

    def set_leverage(self, **kwargs):
        self.calls.append(("set_leverage", kwargs))
        raise AssertionError("dry-run observation must not set leverage")

    def place_limit_buy(self, **kwargs):
        self.calls.append(("place_limit_buy", kwargs))
        raise AssertionError("dry-run observation must not submit")

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel_order", kwargs))
        raise AssertionError("dry-run observation must not cancel")


def _run(
    *,
    repository,
    bundle=None,
    broker=None,
    scanner=None,
    observation_log_path=None,
):
    ids = iter(("cycle-1", "observation-1"))
    return run_once(
        DemoTradingConfig(
            universe_limit=1,
            dry_run=True,
            watchlist_symbols=(),
            observation_log_path=observation_log_path,
        ),
        broker=broker,
        universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 100.0, 1000.0)],
        candle_loader=lambda symbol, **kwargs: bundle or _bundle(),
        scanner=scanner or (lambda *args, **kwargs: _scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST)),
        observation_repository=repository,
        observation_clock=FixedClock(),
        observation_id_factory=lambda: next(ids),
    )


if __name__ == "__main__":
    unittest.main()
