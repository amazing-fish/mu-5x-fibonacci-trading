import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import EntryDecisionCode
from mu_strategy.observations import (
    JsonlObservationRepository,
    ObservationCorruptionError,
    ObservationFailureCode,
    ObservationOutcome,
    ObservationSchemaError,
    ObservationWriteError,
    Stage0ObservationCycle,
    TrustedObservationReference,
    build_stage0_observation,
    sanitize_observation_text,
)


class Stage0ObservationContractTests(unittest.TestCase):
    def test_classifies_closed_outcomes_from_typed_control_fields(self):
        blocked = _observation(
            trusted=_trusted(allowed=False, reason=HealthReason.MANIFEST_INVALID, run_id=None, hashes=()),
            result=None,
            failure_code=ObservationFailureCode.TRUSTED_DATA_BLOCKED,
        )
        failed = _observation(
            result=None,
            failure_code=ObservationFailureCode.SCANNER_EXCEPTION,
            error_type="RuntimeError",
            error_message="scanner exploded",
        )
        waiting = _observation(result=_scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, reason="wait text"))
        ready = _observation(result=_scan(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, reason="ready text"))

        self.assertIs(ObservationOutcome.DATA_GATE_BLOCKED, blocked.outcome)
        self.assertIs(ObservationOutcome.SCAN_FAILED, failed.outcome)
        self.assertIs(ObservationOutcome.NORMAL_NO_ACTION, waiting.outcome)
        self.assertIs(ObservationOutcome.READY_FOR_REVIEW, ready.outcome)

    def test_signal_stage_block_is_normal_no_action_but_input_block_is_data_blocked(self):
        signal_block = _observation(result=_scan(EntryDecisionCode.REGIME_BLOCKED))
        input_block = _observation(result=_scan(EntryDecisionCode.MARKET_DATA_UNAVAILABLE))

        self.assertIs(ObservationOutcome.NORMAL_NO_ACTION, signal_block.outcome)
        self.assertIs(ObservationOutcome.DATA_GATE_BLOCKED, input_block.outcome)

    def test_unknown_decision_code_fails_closed_without_using_action_or_reason(self):
        result = _scan(EntryDecisionCode.UNKNOWN, action="enter", reason="looks ready")

        with self.assertRaisesRegex(ValueError, "decision_code"):
            _observation(result=result)

    def test_allowed_observation_requires_complete_generation_provenance(self):
        with self.assertRaisesRegex(ValueError, "trusted run_id"):
            _trusted(run_id=None, hashes=())

    def test_non_strict_or_inconsistent_trust_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "trading_strict"):
            TrustedObservationReference(
                run_id="trusted-run",
                requested_intervals=("15m", "1h"),
                effective_intervals=("5m", "15m", "1h"),
                content_sha256_by_interval=(("5m", "a" * 64), ("15m", "b" * 64), ("1h", "c" * 64)),
                policy_name="observe_only",
                policy_version=1,
                allowed=True,
                reason=HealthReason.OK,
            )
        with self.assertRaisesRegex(ValueError, "reason OK"):
            _trusted(allowed=True, reason=HealthReason.MANIFEST_INVALID)

    def test_fingerprint_is_stable_across_ids_timestamps_and_presentation_text(self):
        first = _observation(
            observation_id="obs-a",
            created_at_ms=100,
            observed_at_ms=90,
            result=_scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, reason="first wording"),
        )
        second = _observation(
            observation_id="obs-b",
            created_at_ms=200,
            observed_at_ms=190,
            result=_scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, reason="second wording"),
        )
        changed_control = _observation(result=_scan(EntryDecisionCode.REGIME_BLOCKED))

        self.assertEqual(first.result_fingerprint, second.result_fingerprint)
        self.assertNotEqual(first.result_fingerprint, changed_control.result_fingerprint)

    def test_sanitizes_secret_values_and_bounds_diagnostic_length(self):
        text = (
            'api_key=abc secret: def passphrase=ghi Authorization: Bearer bearer-token '
            'OKX_SECRET_KEY=okx-secret OKX_PASSPHRASE=okx-pass "OK-ACCESS-KEY": "header-key" '
            "'access_token': 'json-token' cookie=session-value "
            + ("x" * 1000)
        )

        sanitized = sanitize_observation_text(text)

        self.assertNotIn("abc", sanitized)
        self.assertNotIn("def", sanitized)
        self.assertNotIn("ghi", sanitized)
        self.assertNotIn("Bearer-token", sanitized)
        self.assertNotIn("bearer-token", sanitized)
        self.assertNotIn("okx-secret", sanitized)
        self.assertNotIn("okx-pass", sanitized)
        self.assertNotIn("header-key", sanitized)
        self.assertNotIn("json-token", sanitized)
        self.assertNotIn("session-value", sanitized)
        self.assertLessEqual(len(sanitized), 512)

    def test_cycle_round_trip_preserves_typed_fields_and_uses_one_jsonl_record(self):
        cycle = Stage0ObservationCycle(
            cycle_id="cycle-1",
            created_at_ms=123,
            observations=(
                _observation(observation_id="obs-wait"),
                _observation(
                    observation_id="obs-ready",
                    result=_scan(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY),
                ),
            ),
        )

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            JsonlObservationRepository(path).append_cycle(cycle)

            lines = path.read_text(encoding="utf-8").splitlines()
            restarted = JsonlObservationRepository(path).read_cycles()

        self.assertEqual(1, len(lines))
        self.assertEqual((cycle,), restarted)
        self.assertIs(ObservationOutcome.READY_FOR_REVIEW, restarted[0].observations[1].outcome)
        self.assertIs(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, restarted[0].observations[1].decision_code)
        self.assertEqual({"5m", "15m", "1h"}, set(restarted[0].observations[1].content_sha256_by_interval))

    def test_restart_reads_prior_complete_cycles(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            JsonlObservationRepository(path).append_cycle(_cycle("cycle-a", "obs-a"))
            JsonlObservationRepository(path).append_cycle(_cycle("cycle-b", "obs-b"))

            restarted = JsonlObservationRepository(path).read_cycles()

        self.assertEqual(["cycle-a", "cycle-b"], [cycle.cycle_id for cycle in restarted])

    def test_unknown_schema_is_rejected_without_downgrade(self):
        cycle = _cycle("cycle-a", "obs-a")
        payload = cycle.to_dict()
        payload["schema_version"] = 999

        with self.assertRaisesRegex(ObservationSchemaError, "schema_version"):
            Stage0ObservationCycle.from_dict(payload)

    def test_unknown_fields_are_rejected_instead_of_hiding_private_payloads(self):
        payload = _cycle("cycle-a", "obs-a").to_dict()
        payload["api_key"] = "must-not-be-accepted"

        with self.assertRaisesRegex(ObservationSchemaError, "unknown fields"):
            Stage0ObservationCycle.from_dict(payload)

    def test_reader_rejects_allowed_record_with_missing_run_id_even_if_schema_is_current(self):
        payload = _cycle("cycle-a", "obs-a").to_dict()
        payload["observations"][0]["trusted_run_id"] = None

        with self.assertRaisesRegex(ObservationSchemaError, "trusted run_id"):
            Stage0ObservationCycle.from_dict(payload)

    def test_corrupted_trailing_record_fails_closed(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            repository.append_cycle(_cycle("cycle-a", "obs-a"))
            with path.open("ab") as handle:
                handle.write(b'{"schema_version":1,"cycle_id":"partial"')

            with self.assertRaisesRegex(ObservationCorruptionError, "line 2"):
                JsonlObservationRepository(path).read_cycles()

    def test_reader_rechecks_failed_write_marker_after_reading_visible_cycle(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            repository.append_cycle(_cycle("cycle-a", "obs-a"))
            original_from_dict = Stage0ObservationCycle.from_dict

            def parse_then_expose_failed_writer(payload):
                cycle = original_from_dict(payload)
                repository.invalid_marker_path.write_text("failed-cycle", encoding="utf-8")
                return cycle

            with patch.object(Stage0ObservationCycle, "from_dict", side_effect=parse_then_expose_failed_writer):
                with self.assertRaisesRegex(ObservationCorruptionError, "failed-write marker"):
                    repository.read_cycles()

    def test_scan_result_rejects_non_numeric_and_non_finite_fields(self):
        from mu_strategy.observations import Stage0ScanResult

        payload = _observation().scan_result.to_dict()
        cases = (
            ("string", "not-a-number"),
            ("nan", float("nan")),
            ("infinity", float("inf")),
            ("bool", True),
            ("overflow", 10**10000),
        )
        for label, invalid in cases:
            with self.subTest(case=label):
                malformed = dict(payload, last_close=invalid)
                with self.assertRaises((TypeError, ValueError)):
                    Stage0ScanResult.from_dict(malformed)

    def test_fsync_failure_leaves_marker_that_prevents_consuming_visible_bytes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            with patch("mu_strategy.observations.os.fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(ObservationWriteError):
                    repository.append_cycle(_cycle("cycle-a", "obs-a"))

            self.assertTrue(repository.invalid_marker_path.exists())
            with self.assertRaisesRegex(ObservationCorruptionError, "failed-write marker"):
                JsonlObservationRepository(path).read_cycles()

    def test_cycle_rejects_duplicate_observation_ids(self):
        observation = _observation(observation_id="same")

        with self.assertRaisesRegex(ValueError, "observation_id"):
            Stage0ObservationCycle(
                cycle_id="cycle-1",
                created_at_ms=123,
                observations=(observation, observation),
            )


def _cycle(cycle_id: str, observation_id: str) -> Stage0ObservationCycle:
    return Stage0ObservationCycle(
        cycle_id=cycle_id,
        created_at_ms=123,
        observations=(_observation(cycle_id=cycle_id, observation_id=observation_id),),
    )


def _trusted(
    *,
    allowed: bool = True,
    reason: HealthReason = HealthReason.OK,
    run_id: str | None = "trusted-run",
    hashes=(
        ("5m", "a" * 64),
        ("15m", "b" * 64),
        ("1h", "c" * 64),
    ),
) -> TrustedObservationReference:
    return TrustedObservationReference(
        run_id=run_id,
        requested_intervals=("15m", "1h"),
        effective_intervals=("5m", "15m", "1h"),
        content_sha256_by_interval=tuple(hashes),
        policy_name="trading_strict",
        policy_version=1,
        allowed=allowed,
        reason=reason,
    )


def _scan(
    decision_code: EntryDecisionCode = EntryDecisionCode.NO_CONFIRMED_FIB_RETEST,
    *,
    action: str = "wait",
    reason: str = "no current setup",
) -> EntryScanResult:
    return EntryScanResult(
        symbol="BTC-USDT-SWAP",
        action=action,
        reason=reason,
        last_close=100.0,
        regime_1h="green",
        rsi14=55.0,
        macd_hist=0.2,
        macd_hist_prev=0.1,
        fib_level=99.0,
        fib_distance_pct=0.01,
        trigger_price=99.0,
        initial_stop=97.0,
        signal_time_ms=42,
        decision_code=decision_code,
    )


def _observation(
    *,
    observation_id: str = "obs-1",
    cycle_id: str = "cycle-1",
    created_at_ms: int = 123,
    observed_at_ms: int = 120,
    trusted: TrustedObservationReference | None = None,
    result: EntryScanResult | None = None,
    failure_code: ObservationFailureCode | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
):
    return build_stage0_observation(
        observation_id=observation_id,
        cycle_id=cycle_id,
        symbol="BTC-USDT-SWAP",
        created_at_ms=created_at_ms,
        observed_at_ms=observed_at_ms,
        trusted=trusted or _trusted(),
        strategy_name="baseline",
        strategy_config_fingerprint="d" * 64,
        result=result or (_scan() if failure_code is None else None),
        compatibility_source="watchlist",
        provenance="canonical_trusted_generation",
        failure_code=failure_code,
        error_type=error_type,
        error_message=error_message,
    )


if __name__ == "__main__":
    unittest.main()
