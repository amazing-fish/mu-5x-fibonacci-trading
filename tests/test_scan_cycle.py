import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mu_strategy.demo_trading import DemoTradingConfig, run_once
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.market_data.universe import OKXSwapTicker
from mu_strategy.models import EntryDecisionCode
from mu_strategy.observations import ObservationCycleInvalidError, ObservationFailureCode, ObservationOutcome
from mu_strategy.scan_cycle import ScanCycle, ScanDataFailure
from mu_strategy.strategies.registry import baseline_strategy_group
from tests.factories.scan_cycle import scan_result, trusted_scan_bundle


class ScanCycleTests(unittest.TestCase):
    def test_persistence_switch_preserves_every_typed_outcome_payload_and_scan_count(self):
        ready = scan_result(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY)
        bundle = trusted_scan_bundle()
        missing_hash = replace(bundle.load_context.manifest, datasets={})
        cases = [
            ("ready", bundle, ready, ObservationOutcome.READY_FOR_REVIEW, None, 1),
            ("wait", bundle, scan_result(EntryDecisionCode.WAITING_SECOND_PULLBACK),
             ObservationOutcome.NORMAL_NO_ACTION, None, 1),
            ("signal_block", bundle, scan_result(EntryDecisionCode.REGIME_BLOCKED),
             ObservationOutcome.NORMAL_NO_ACTION, None, 1),
            ("input_block", bundle, scan_result(EntryDecisionCode.MARKET_DATA_UNAVAILABLE),
             ObservationOutcome.DATA_GATE_BLOCKED, None, 1),
            ("denied", trusted_scan_bundle(allowed=False, reason=HealthReason.STALE_BY_CLOCK), ready,
             ObservationOutcome.DATA_GATE_BLOCKED, ObservationFailureCode.TRUSTED_DATA_BLOCKED, 0),
            ("load_failure", OSError("cache unavailable"), ready, ObservationOutcome.DATA_GATE_BLOCKED,
             ObservationFailureCode.TRUSTED_DATA_LOAD_FAILED, 0),
            ("missing_candles", replace(bundle, candles_by_interval={}), ready,
             ObservationOutcome.DATA_GATE_BLOCKED, ObservationFailureCode.TRUSTED_DATA_BLOCKED, 0),
            ("missing_context", replace(bundle, load_context=None), ready, ObservationOutcome.DATA_GATE_BLOCKED,
             ObservationFailureCode.TRUSTED_PROVENANCE_INCOMPLETE, 0),
            ("wrong_generation", replace(bundle, run_id="another-run"), ready, ObservationOutcome.DATA_GATE_BLOCKED,
             ObservationFailureCode.TRUSTED_PROVENANCE_INCOMPLETE, 0),
            ("missing_hash", replace(bundle, load_context=replace(bundle.load_context, manifest=missing_hash)), ready,
             ObservationOutcome.DATA_GATE_BLOCKED, ObservationFailureCode.TRUSTED_PROVENANCE_INCOMPLETE, 0),
            ("exception", bundle, RuntimeError("api_key=secret-marker scanner failed"), ObservationOutcome.SCAN_FAILED,
             ObservationFailureCode.SCANNER_EXCEPTION, 1),
        ]
        invalid = [None, {}, replace(ready, decision_code=EntryDecisionCode.UNKNOWN),
                   replace(ready, symbol="ETH-USDT-SWAP"), replace(ready, last_close="100"),
                   replace(ready, last_close=True), replace(ready, last_close=float("inf")),
                   replace(ready, last_close=10**1000), replace(ready, signal_time_ms=-1)]
        cases.extend((f"invalid_{index}", bundle, value, ObservationOutcome.SCAN_FAILED,
                      ObservationFailureCode.SCANNER_RESULT_INVALID, 1) for index, value in enumerate(invalid))

        for name, data, result, expected, failure, expected_calls in cases:
            with self.subTest(case=name):
                outputs = []
                cycles = []
                for persist in (False, True):
                    repository = Mock() if persist else None
                    payload, cycle, scanner, broker = _run(data, result, repository)
                    self.assertEqual(expected_calls, scanner.call_count)
                    observation = cycle.observations[0]
                    self.assertIs(expected, observation.outcome)
                    self.assertIs(failure, observation.failure_code)
                    self.assertEqual(1, len(cycle.observations))
                    self.assertLessEqual({call[0] for call in broker.method_calls}, {"get_instruments"})
                    if repository is not None:
                        repository.append_cycle.assert_called_once_with(cycle)
                        self.assertIs(cycle, repository.append_cycle.call_args.args[0])
                    self.assertNotIn("secret-marker", str(payload))
                    self.assertNotIn("secret-marker", cycle.to_json())
                    outputs.append(payload)
                    cycles.append(cycle.to_json())
                self.assertEqual(outputs[0], outputs[1])
                self.assertEqual(cycles[0], cycles[1])

    def test_fresh_legacy_inputs_are_blocked_with_or_without_persistence(self):
        bundle = replace(trusted_scan_bundle(), load_context=None, trust_decision=None, observed_at_ms=None)
        plain = SimpleNamespace(**bundle.__dict__)
        del plain.trust_decision
        for data in (bundle, plain):
            with self.subTest(type=type(data).__name__):
                outputs = []
                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=1_000):
                    for persist in (False, True):
                        payload, cycle, scanner, broker = _run(
                            data, scan_result(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY), Mock() if persist else None,
                        )
                        scanner.assert_not_called()
                        self.assertEqual([], payload["orders"])
                        self.assertIs(ObservationFailureCode.TRUSTED_PROVENANCE_INCOMPLETE,
                                      cycle.observations[0].failure_code)
                        outputs.append(payload)
                self.assertEqual(outputs[0], outputs[1])

    def test_failure_classification_uses_typed_cause_even_when_diagnostic_text_changes(self):
        cycle = ScanCycle(clock=Mock(now_ms=lambda: 1_000), id_factory=iter(("cycle", "observation")).__next__)
        scanner = Mock()
        group = baseline_strategy_group("BTC-USDT-SWAP")
        outcome = cycle.scan_symbol(
            symbol="BTC-USDT-SWAP", source="top", bundle=trusted_scan_bundle(), requested_intervals=("15m", "1h"),
            strategy_name=group.name, strategy_config=group.config, scanner=scanner,
            data_failure=ScanDataFailure(
                ObservationFailureCode.TRUSTED_DATA_BLOCKED, HealthReason.CACHE_MISSING,
                {"reason": "scanner_failed", "status_reason": "unrelated presentation text"},
            ),
        )
        scanner.assert_not_called()
        self.assertIs(ObservationOutcome.DATA_GATE_BLOCKED, outcome.observation.outcome)
        self.assertIs(ObservationFailureCode.TRUSTED_DATA_BLOCKED, outcome.observation.failure_code)
        self.assertIs(HealthReason.CACHE_MISSING, outcome.observation.trust_reason)

    def test_failed_write_preserves_ready_decision_without_rescan_or_order_planning(self):
        bundle = trusted_scan_bundle()
        result = scan_result(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY)
        _, expected, _, _ = _run(bundle, result, None)
        repository = Mock()
        repository.append_cycle.side_effect = OSError("disk full")
        scanner = Mock(return_value=result)
        broker = _broker()
        with self.assertRaisesRegex(ObservationCycleInvalidError, "persistence failed") as caught:
            _run(bundle, result, repository, scanner=scanner, broker=broker)
        scanner.assert_called_once()
        broker.get_instruments.assert_not_called()
        self.assertIsInstance(caught.exception.__cause__, OSError)
        repository.append_cycle.assert_called_once()
        actual = repository.append_cycle.call_args.args[0]
        self.assertEqual(expected, actual)
        self.assertIs(ObservationOutcome.READY_FOR_REVIEW, actual.observations[0].outcome)
        self.assertIsNone(actual.observations[0].failure_code)

    def test_each_distinct_symbol_is_scanned_once_despite_watchlist_overlap(self):
        for persist in (False, True):
            with self.subTest(persist=persist):
                scanner = Mock(side_effect=lambda symbol, *args, **kwargs: scan_result(
                    EntryDecisionCode.WAITING_SECOND_PULLBACK, symbol=symbol,
                ))
                loader = Mock(side_effect=lambda symbol, **kwargs: trusted_scan_bundle(symbol=symbol))
                repository = Mock() if persist else None
                payload = run_once(
                    DemoTradingConfig(universe_limit=2, watchlist_symbols=("BTC-USDT-SWAP",)), broker=None,
                    universe_provider=lambda limit: [OKXSwapTicker(symbol, 100, 1000)
                                                     for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")],
                    candle_loader=loader, scanner=scanner, observation_repository=repository,
                )
                self.assertEqual(["BTC-USDT-SWAP", "ETH-USDT-SWAP"], [call.args[0] for call in scanner.call_args_list])
                self.assertEqual(2, loader.call_count)
                self.assertEqual(2, len(payload["scans"]))
                if repository is not None:
                    self.assertEqual(2, len(repository.append_cycle.call_args.args[0].observations))


def _broker():
    broker = Mock(spec=("get_positions", "get_open_orders", "get_balance", "get_instruments",
                        "set_leverage", "place_limit_buy", "cancel_order"))
    broker.get_instruments.return_value = {
        "code": "0", "data": [{"instId": "BTC-USDT-SWAP", "tickSz": "0.1", "lotSz": "0.01", "ctVal": "0.01"}],
    }
    return broker


def _run(data, result, repository, *, scanner=None, broker=None):
    captured = []
    snapshot = ScanCycle.observations

    def capture(cycle):
        captured.append(snapshot(cycle))
        return captured[-1]

    loader = Mock(side_effect=data) if isinstance(data, Exception) else Mock(return_value=data)
    scanner = scanner if scanner is not None else (
        Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    )
    broker = broker if broker is not None else _broker()
    with patch.object(ScanCycle, "observations", autospec=True, side_effect=capture), patch(
        "mu_strategy.live.okx.OKXCredentials.from_env", side_effect=AssertionError("no private credentials"),
    ):
        payload = run_once(
            DemoTradingConfig(universe_limit=1, watchlist_symbols=()), broker=broker,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 100, 1000)],
            candle_loader=loader, scanner=scanner, observation_repository=repository,
            observation_clock=Mock(now_ms=lambda: 1_000),
            observation_id_factory=iter(("cycle", "observation")).__next__,
        )
    return payload, captured[0], scanner, broker
