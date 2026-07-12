import math
import unittest
from dataclasses import fields, replace

from mu_strategy.research.strategy_releases import (
    EXPERIMENT_PROTOCOL_ID,
    BacktestAssumptionsV1,
    ExperimentWindow,
    ExperimentWindowResultV1,
    ExperimentWindowRole,
    FillModel,
    PartialFillModel,
    ReleaseDecision,
    ScmReviewSnapshotV1,
    SelectionReasonCode,
    StrategyConfigPayloadV1,
    StrategyReleaseApprovalV1,
    StrategyReleaseCandidateV1,
    StrategyReleaseV1,
    StrategyReleaseSchemaError,
    TrustedExperimentDatasetV1,
)
from mu_strategy.strategies.registry import (
    StrategyRuleDescriptor,
    baseline_strategy_group,
    default_strategy_groups,
    strategy_rule_descriptor,
    validate_strategy_rule_descriptors,
)
from mu_strategy.strategy import StrategyConfig


class StrategyRuleIdentityTests(unittest.TestCase):
    def test_baseline_rule_identity_is_owned_by_registry(self):
        group = baseline_strategy_group("MU-USDT-SWAP")

        self.assertEqual("mu.baseline.second_pullback.long_limit.v1", group.rule.strategy_rule_id)
        self.assertEqual(group.rule, strategy_rule_descriptor("baseline"))
        self.assertEqual("baseline", group.rule.strategy_name)
        self.assertEqual(1, group.rule.semantic_version)
        self.assertEqual("buy", group.rule.side)
        self.assertEqual("limit", group.rule.order_type)

    def test_rule_catalog_covers_default_groups_and_rejects_duplicate_ids(self):
        groups = default_strategy_groups("MU-USDT-SWAP")
        descriptors = tuple(group.rule for group in groups)

        validate_strategy_rule_descriptors(descriptors)
        self.assertEqual(len(descriptors), len({descriptor.strategy_rule_id for descriptor in descriptors}))

        duplicate = replace(descriptors[0], strategy_name="duplicate_name")
        with self.assertRaisesRegex(ValueError, "strategy_rule_id"):
            validate_strategy_rule_descriptors((*descriptors, duplicate))

    def test_rule_descriptor_rejects_unversioned_or_non_entry_identity(self):
        with self.assertRaises(ValueError):
            StrategyRuleDescriptor("baseline", "baseline", 1, "buy", "limit")
        with self.assertRaises(ValueError):
            StrategyRuleDescriptor("mu.baseline.v1", "baseline", 0, "buy", "limit")
        with self.assertRaises(ValueError):
            StrategyRuleDescriptor("mu.baseline.v1", "baseline", 1, "sell", "limit")


class StrategyConfigPayloadTests(unittest.TestCase):
    def test_payload_owns_every_strategy_config_field_and_round_trips(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config

        payload = StrategyConfigPayloadV1.from_config(config)
        wire = payload.to_dict()
        restored = StrategyConfigPayloadV1.from_dict(wire)

        self.assertEqual({field.name for field in fields(StrategyConfig)}, set(wire["fields"]))
        self.assertEqual(payload, restored)
        self.assertEqual(payload.strategy_config_sha256, restored.strategy_config_sha256)
        self.assertEqual(config, restored.to_strategy_config())
        self.assertIsInstance(wire["fields"]["leverage"], str)
        self.assertEqual(["0.2", "0.2", "0.2", "0.4"], wire["fields"]["margin_steps"])

    def test_payload_rejects_unknown_missing_noncanonical_and_nonfinite_fields(self):
        wire = StrategyConfigPayloadV1.from_config(StrategyConfig()).to_dict()

        unknown = {**wire, "fields": {**wire["fields"], "future_field": "value"}}
        with self.assertRaisesRegex(StrategyReleaseSchemaError, "unknown"):
            StrategyConfigPayloadV1.from_dict(unknown)

        missing_fields = dict(wire["fields"])
        missing_fields.pop("leverage")
        with self.assertRaisesRegex(StrategyReleaseSchemaError, "missing"):
            StrategyConfigPayloadV1.from_dict({**wire, "fields": missing_fields})

        noncanonical = {**wire, "fields": {**wire["fields"], "leverage": "5.00"}}
        with self.assertRaisesRegex(StrategyReleaseSchemaError, "canonical decimal"):
            StrategyConfigPayloadV1.from_dict(noncanonical)

        for invalid in (True, math.nan, math.inf):
            with self.subTest(value=invalid):
                malformed = {**wire, "fields": {**wire["fields"], "leverage": invalid}}
                with self.assertRaises(StrategyReleaseSchemaError):
                    StrategyConfigPayloadV1.from_dict(malformed)

    def test_payload_hash_changes_with_any_executable_config_change(self):
        first = StrategyConfigPayloadV1.from_config(StrategyConfig())
        second = StrategyConfigPayloadV1.from_config(replace(StrategyConfig(), fib_lookback=33))

        self.assertNotEqual(first.strategy_config_sha256, second.strategy_config_sha256)


class StrategyReleaseContractTests(unittest.TestCase):
    def test_candidate_approval_release_round_trip_is_content_addressed(self):
        candidate = _candidate()
        approval = _approval(candidate)
        release = StrategyReleaseV1.create(candidate=candidate, approval=approval)

        restored_candidate = StrategyReleaseCandidateV1.from_dict(candidate.to_dict())
        restored_release = StrategyReleaseV1.from_dict(release.to_dict())

        self.assertEqual(candidate, restored_candidate)
        self.assertEqual(release, restored_release)
        self.assertRegex(candidate.candidate_fingerprint, r"^[0-9a-f]{64}$")
        self.assertRegex(release.strategy_release_id, r"^sr1_[0-9a-f]{64}$")
        self.assertEqual(approval.review_snapshot.snapshot_sha256, approval.approval_snapshot_sha256)

    def test_candidate_identity_changes_for_each_control_dimension(self):
        base = _candidate()
        changed = (
            _candidate(evaluated_code_commit_sha="b" * 40),
            _candidate(config=StrategyConfigPayloadV1.from_config(replace(StrategyConfig(), fib_lookback=33))),
            _candidate(dataset=_dataset(run_id="b" * 32)),
            _candidate(windows=_windows(offset_ms=900_000)),
            _candidate(assumptions=replace(_assumptions(), fee_rate="0.0002")),
            _candidate(results=_results(ending_equity="10001")),
            _candidate(selection_reason=SelectionReasonCode.REVALIDATED_BASELINE),
        )

        for candidate in changed:
            with self.subTest(candidate=candidate.candidate_fingerprint):
                self.assertNotEqual(base.candidate_fingerprint, candidate.candidate_fingerprint)

    def test_windows_are_exactly_ordered_contiguous_and_end_exclusive(self):
        valid = _windows()
        self.assertEqual(ExperimentWindowRole.TRAIN, valid[0].role)

        invalid_cases = (
            (valid[1], valid[0], valid[2]),
            (valid[0], replace(valid[1], start_ms=valid[1].start_ms + 1, input_start_ms=valid[1].start_ms + 1), valid[2]),
        )
        for windows in invalid_cases:
            with self.subTest(windows=windows):
                with self.assertRaises(ValueError):
                    _candidate(windows=windows)

        with self.assertRaisesRegex(ValueError, "non-empty"):
            ExperimentWindow(
                ExperimentWindowRole.OUT_OF_SAMPLE,
                valid[2].start_ms,
                valid[2].start_ms,
                valid[2].start_ms,
            )

    def test_result_rejects_noncanonical_nonfinite_and_bool_values(self):
        wire = _results()[0].to_dict()
        for invalid in ("1.00", float("nan"), float("inf"), True):
            with self.subTest(value=invalid):
                malformed = {**wire, "ending_equity": invalid}
                with self.assertRaises(StrategyReleaseSchemaError):
                    ExperimentWindowResultV1.from_dict(malformed)

    def test_release_rejects_rejected_or_mismatched_approval(self):
        candidate = _candidate()
        rejected = _approval(candidate, decision=ReleaseDecision.REJECTED)
        with self.assertRaisesRegex(ValueError, "APPROVED"):
            StrategyReleaseV1.create(candidate=candidate, approval=rejected)

        other = _candidate(evaluated_code_commit_sha="b" * 40)
        with self.assertRaisesRegex(ValueError, "candidate"):
            StrategyReleaseV1.create(candidate=candidate, approval=_approval(other))

    def test_strict_readers_reject_unknown_fields_and_fingerprint_tampering(self):
        candidate = _candidate()
        unknown = {**candidate.to_dict(), "future": True}
        with self.assertRaisesRegex(StrategyReleaseSchemaError, "unknown"):
            StrategyReleaseCandidateV1.from_dict(unknown)

        tampered = candidate.to_dict()
        tampered["evaluated_code_commit_sha"] = "b" * 40
        with self.assertRaisesRegex((StrategyReleaseSchemaError, ValueError), "fingerprint"):
            StrategyReleaseCandidateV1.from_dict(tampered)


def _dataset(*, run_id: str = "a" * 32) -> TrustedExperimentDatasetV1:
    return TrustedExperimentDatasetV1(
        run_id=run_id,
        symbol="MU-USDT-SWAP",
        manifest_schema_version=3,
        requested_intervals=("5m", "15m", "1h"),
        effective_intervals=("5m", "15m", "1h"),
        content_sha256_by_interval=(("15m", "b" * 64), ("1h", "c" * 64), ("5m", "a" * 64)),
    )


def _windows(*, offset_ms: int = 0) -> tuple[ExperimentWindow, ...]:
    start = 1_700_000_000_000 + offset_ms
    width = 10_000_000
    return (
        ExperimentWindow(ExperimentWindowRole.TRAIN, start, start, start + width),
        ExperimentWindow(ExperimentWindowRole.VALIDATION, start + width, start + width, start + 2 * width),
        ExperimentWindow(ExperimentWindowRole.OUT_OF_SAMPLE, start + 2 * width, start + 2 * width, start + 3 * width),
    )


def _assumptions() -> BacktestAssumptionsV1:
    return BacktestAssumptionsV1(
        starting_equity="10000",
        fee_profile="market",
        fee_rate="0.0005",
        fill_model=FillModel.DETERMINISTIC_OHLC,
        slippage_bps="0",
        partial_fill_model=PartialFillModel.NONE,
    )


def _results(*, ending_equity: str = "10000") -> tuple[ExperimentWindowResultV1, ...]:
    return tuple(
        ExperimentWindowResultV1.create(
            role=role,
            trade_count=1,
            starting_equity="10000",
            ending_equity=ending_equity,
            gross_profit="10",
            gross_loss="10",
            total_return_pct="0",
            max_drawdown_pct="-0.01",
        )
        for role in ExperimentWindowRole
    )


def _candidate(
    *,
    evaluated_code_commit_sha: str = "a" * 40,
    config: StrategyConfigPayloadV1 | None = None,
    dataset: TrustedExperimentDatasetV1 | None = None,
    windows: tuple[ExperimentWindow, ...] | None = None,
    assumptions: BacktestAssumptionsV1 | None = None,
    results: tuple[ExperimentWindowResultV1, ...] | None = None,
    selection_reason: SelectionReasonCode = SelectionReasonCode.BASELINE_CONTINUITY,
) -> StrategyReleaseCandidateV1:
    config = config or StrategyConfigPayloadV1.from_config(baseline_strategy_group("MU-USDT-SWAP").config)
    return StrategyReleaseCandidateV1.create(
        strategy_rule_id="mu.baseline.second_pullback.long_limit.v1",
        strategy_name="baseline",
        supported_symbols=("MU-USDT-SWAP",),
        strategy_config=config,
        evaluated_code_commit_sha=evaluated_code_commit_sha,
        dataset=dataset or _dataset(),
        windows=windows or _windows(),
        experiment_protocol_id=EXPERIMENT_PROTOCOL_ID,
        assumptions=assumptions or _assumptions(),
        results=results or _results(),
        selection_reason=selection_reason,
    )


def _approval(
    candidate: StrategyReleaseCandidateV1,
    *,
    decision: ReleaseDecision = ReleaseDecision.APPROVED,
) -> StrategyReleaseApprovalV1:
    snapshot = ScmReviewSnapshotV1.create(
        scm_provider="github",
        repository="amazing-fish/mu-5x-fibonacci-trading",
        pull_request_number=45,
        review_record_id="PRR_review_1",
        reviewer_id="independent-reviewer",
        author_id="release-author",
        reviewed_at_ms=1_700_100_000_000,
        decision=decision,
        candidate_fingerprint=candidate.candidate_fingerprint,
        evaluated_code_commit_sha=candidate.evaluated_code_commit_sha,
        statement="APPROVE exact candidate and evaluated implementation",
        review_url="https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/45#pullrequestreview-1",
    )
    return StrategyReleaseApprovalV1.create(review_snapshot=snapshot)


if __name__ == "__main__":
    unittest.main()
