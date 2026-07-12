import inspect
import json
import math
import unittest
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mu_strategy.commands.promote_strategy_release import (
    GitHubCliScmReviewProvider,
    LiveScmCommit,
    LiveScmReview,
    LiveScmPullRequest,
    ScmReviewVerificationError,
    approval_statement,
    capture_verified_approval,
    promote_strategy_release,
)
from mu_strategy.canonical import canonical_json, canonical_sha256

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
    StrategyReleaseResolutionError,
    StrategyReleaseSchemaError,
    StrictStrategyReleaseResolver,
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
        self.assertEqual(group.rule, strategy_rule_descriptor("second_pullback_limit_8"))

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
        fee_config = StrategyConfigPayloadV1.from_config(
            replace(
                baseline_strategy_group("MU-USDT-SWAP").config,
                fee_profile="limit",
                fee_rate=0.0002,
            )
        )
        changed = (
            _candidate(evaluated_code_commit_sha="b" * 40),
            _candidate(
                config=StrategyConfigPayloadV1.from_config(
                    replace(baseline_strategy_group("MU-USDT-SWAP").config, fib_lookback=33)
                )
            ),
            _candidate(dataset=_dataset(run_id="b" * 32)),
            _candidate(windows=_windows(offset_ms=900_000)),
            _candidate(
                config=fee_config,
                assumptions=replace(_assumptions(), fee_profile="limit", fee_rate="0.0002"),
            ),
            _candidate(results=_results(ending_equity="10001")),
            _candidate(selection_reason=SelectionReasonCode.REVALIDATED_BASELINE),
        )

        for candidate in changed:
            with self.subTest(candidate=candidate.candidate_fingerprint):
                self.assertNotEqual(base.candidate_fingerprint, candidate.candidate_fingerprint)

    def test_candidate_rejects_a_config_symbol_that_differs_from_the_dataset(self):
        mismatched = StrategyConfigPayloadV1.from_config(
            baseline_strategy_group("BTC-USDT-SWAP").config
        )
        with self.assertRaisesRegex(ValueError, "config symbol"):
            _candidate(config=mismatched)

    def test_readdressed_candidate_rejects_protocol_cross_field_mismatches(self):
        candidate = _candidate()

        def mismatched_fee_profile(wire):
            wire["assumptions"]["fee_profile"] = "limit"

        def mismatched_fee_rate(wire):
            wire["assumptions"]["fee_rate"] = "0.0002"

        def nonzero_slippage(wire):
            wire["assumptions"]["slippage_bps"] = "1"

        def mismatched_starting_equity(wire):
            wire["assumptions"]["starting_equity"] = "9000"

        def missing_required_interval(wire):
            wire["dataset"]["requested_intervals"] = ["5m", "15m"]
            wire["dataset"]["effective_intervals"] = ["5m", "15m"]
            wire["dataset"]["content_sha256_by_interval"].pop("1h")

        def missing_schema_v3_base_interval(wire):
            wire["dataset"]["requested_intervals"] = ["15m", "1h"]
            wire["dataset"]["effective_intervals"] = ["15m", "1h"]
            wire["dataset"]["content_sha256_by_interval"].pop("5m")

        def unsupported_fee_profile(wire):
            wire["strategy_config"]["fields"]["fee_profile"] = "attacker_defined"
            wire["assumptions"]["fee_profile"] = "attacker_defined"

        def boolean_config_schema_version(wire):
            wire["strategy_config"]["schema_version"] = True

        invalid_candidates = (
            ("fee", mismatched_fee_profile),
            ("fee", mismatched_fee_rate),
            ("slippage", nonzero_slippage),
            ("starting equity", mismatched_starting_equity),
            ("required intervals", missing_required_interval),
            ("effective_intervals", missing_schema_v3_base_interval),
            ("fee_profile", unsupported_fee_profile),
            ("schema_version", boolean_config_schema_version),
        )
        for expected_error, mutate in invalid_candidates:
            with self.subTest(mutation=mutate.__name__):
                wire = candidate.to_dict()
                mutate(wire)
                _readdress_candidate_wire(wire)

                with self.assertRaisesRegex(StrategyReleaseSchemaError, expected_error):
                    StrategyReleaseCandidateV1.from_dict(wire)

    def test_readdressed_candidate_rejects_result_arithmetic_mismatches(self):
        candidate = _candidate()

        def impossible_return(wire):
            wire["results"][0]["total_return_pct"] = "-2"

        def impossible_net_pnl(wire):
            wire["results"][0]["ending_equity"] = "10001"
            wire["results"][0]["total_return_pct"] = "0.0001"

        def impossible_zero_trade_summary(wire):
            wire["results"][0]["trade_count"] = 0

        invalid_results = (
            ("total return", impossible_return),
            ("gross profit", impossible_net_pnl),
            ("zero-trade", impossible_zero_trade_summary),
        )
        for expected_error, mutate in invalid_results:
            with self.subTest(mutation=mutate.__name__):
                wire = candidate.to_dict()
                mutate(wire)
                _readdress_candidate_wire(wire)

                with self.assertRaisesRegex(StrategyReleaseSchemaError, expected_error):
                    StrategyReleaseCandidateV1.from_dict(wire)

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


class GitHubCliScmReviewProviderTests(unittest.TestCase):
    def test_review_last_edited_at_is_required_nullable_and_parsed(self):
        provider = GitHubCliScmReviewProvider()
        cases = (
            (None, None),
            ("2023-11-16T02:00:00Z", 1_700_100_000_000),
        )

        for wire_value, expected_ms in cases:
            with self.subTest(last_edited_at=wire_value), patch.object(
                provider, "_gh_json", return_value={"node_id": "PRR_1"}
            ), patch.object(
                provider,
                "_gh_graphql_review",
                return_value=self._review_payload(lastEditedAt=wire_value),
            ):
                review = provider.fetch_review(
                    repository="amazing-fish/mu-5x-fibonacci-trading",
                    pull_request_number=46,
                    review_record_id="123",
                )

            self.assertIsNotNone(review)
            self.assertEqual(expected_ms, review.last_edited_at_ms)

    def test_review_rejects_missing_or_malformed_last_edited_at(self):
        provider = GitHubCliScmReviewProvider()
        invalid_payloads = (
            self._review_payload(include_last_edited_at=False),
            self._review_payload(lastEditedAt=123),
            self._review_payload(lastEditedAt="not-a-timestamp"),
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch.object(
                provider, "_gh_json", return_value={"node_id": "PRR_1"}
            ), patch.object(provider, "_gh_graphql_review", return_value=payload):
                with self.assertRaisesRegex(ScmReviewVerificationError, "lastEditedAt"):
                    provider.fetch_review(
                        repository="amazing-fish/mu-5x-fibonacci-trading",
                        pull_request_number=46,
                        review_record_id="123",
                    )

    def test_graphql_review_rejects_nonempty_errors_even_with_a_node(self):
        completed = Mock(
            stdout=json.dumps(
                {
                    "data": {"node": self._review_payload(lastEditedAt=None)},
                    "errors": [{"message": "field resolution failed"}],
                }
            )
        )
        with patch(
            "mu_strategy.commands.promote_strategy_release.subprocess.run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(ScmReviewVerificationError, "GraphQL errors"):
                GitHubCliScmReviewProvider._gh_graphql_review("PRR_1")

    @staticmethod
    def _review_payload(*, include_last_edited_at=True, **overrides):
        payload = {
            "databaseId": 123,
            "body": "approval",
            "state": "APPROVED",
            "submittedAt": "2023-11-16T02:00:00Z",
            "url": "https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/46#pullrequestreview-123",
            "includesCreatedEdit": False,
            "author": {"login": "independent-reviewer"},
        }
        if include_last_edited_at:
            payload["lastEditedAt"] = None
        payload.update(overrides)
        return payload


class PromotionVerificationTests(unittest.TestCase):
    def test_live_independent_review_is_captured_with_reproducible_snapshot(self):
        candidate = _candidate()
        provider = Mock()
        live = _live_review(candidate)
        pull_request = _live_pull_request(candidate)
        provider.fetch_pull_request.return_value = pull_request
        provider.fetch_review.return_value = live

        approval = capture_verified_approval(
            candidate,
            repository=live.repository,
            pull_request_number=live.pull_request_number,
            review_record_id=live.review_record_id,
            provider=provider,
        )

        self.assertEqual(ReleaseDecision.APPROVED, approval.decision)
        self.assertEqual(candidate.candidate_fingerprint, approval.candidate_fingerprint)
        self.assertEqual(live.reviewer_id, approval.review_snapshot.reviewer_id)
        self.assertEqual(
            pull_request.commits[0].author_id,
            approval.review_snapshot.author_id,
        )
        self.assertEqual(
            approval.review_snapshot,
            ScmReviewSnapshotV1.from_dict(approval.review_snapshot.to_dict()),
        )
        provider.fetch_review.assert_called_once_with(
            repository=live.repository,
            pull_request_number=live.pull_request_number,
            review_record_id=live.review_record_id,
        )
        provider.fetch_pull_request.assert_called_once_with(
            repository=live.repository,
            pull_request_number=live.pull_request_number,
        )

    def test_missing_self_or_mismatched_live_review_is_rejected(self):
        candidate = _candidate()
        valid = _live_review(candidate)
        pull_request = _live_pull_request(candidate)
        invalid_records = (
            None,
            replace(valid, scm_provider="gitlab"),
            replace(valid, reviewer_id=pull_request.author_id),
            replace(valid, reviewer_id=pull_request.author_id.upper()),
            replace(valid, reviewer_id=pull_request.commits[0].author_id),
            replace(valid, reviewer_id=pull_request.commits[0].author_id.upper()),
            replace(valid, reviewer_id=pull_request.commits[0].committer_id),
            replace(valid, reviewer_id=pull_request.commits[0].committer_id.upper()),
            replace(valid, includes_created_edit=True),
            replace(valid, last_edited_at_ms=1_700_100_000_001),
            replace(valid, statement=approval_statement(_candidate(evaluated_code_commit_sha="b" * 40))),
            replace(valid, decision=ReleaseDecision.REJECTED),
            replace(valid, repository="other/repository"),
            replace(valid, review_url="https://attacker.invalid/review/1"),
        )

        for record in invalid_records:
            with self.subTest(record=record):
                provider = Mock()
                provider.fetch_pull_request.return_value = pull_request
                provider.fetch_review.return_value = record
                with self.assertRaises(ScmReviewVerificationError):
                    capture_verified_approval(
                        candidate,
                        repository=valid.repository,
                        pull_request_number=valid.pull_request_number,
                        review_record_id=valid.review_record_id,
                        provider=provider,
                    )

        provider = Mock()
        provider.fetch_pull_request.return_value = replace(
            pull_request,
            commits=(LiveScmCommit("b" * 40, "other-author", "other-committer"),),
        )
        provider.fetch_review.return_value = valid
        with self.assertRaisesRegex(ScmReviewVerificationError, "evaluated commit"):
            capture_verified_approval(
                candidate,
                repository=valid.repository,
                pull_request_number=valid.pull_request_number,
                review_record_id=valid.review_record_id,
                provider=provider,
            )

        provider = Mock()
        with self.assertRaisesRegex(ScmReviewVerificationError, "trusted repository"):
            capture_verified_approval(
                candidate,
                repository="attacker/fork",
                pull_request_number=valid.pull_request_number,
                review_record_id=valid.review_record_id,
                provider=provider,
            )
        provider.fetch_pull_request.assert_not_called()
        provider.fetch_review.assert_not_called()

        for missing_identity in (
            LiveScmCommit(candidate.evaluated_code_commit_sha, None, "release-committer"),
            LiveScmCommit(candidate.evaluated_code_commit_sha, "release-author", None),
        ):
            with self.subTest(missing_identity=missing_identity):
                provider = Mock()
                provider.fetch_pull_request.return_value = replace(
                    pull_request,
                    commits=(missing_identity,),
                )
                provider.fetch_review.return_value = valid
                with self.assertRaisesRegex(ScmReviewVerificationError, "identity"):
                    capture_verified_approval(
                        candidate,
                        repository=valid.repository,
                        pull_request_number=valid.pull_request_number,
                        review_record_id=valid.review_record_id,
                        provider=provider,
                    )

    def test_promotion_writes_only_the_canonical_content_addressed_release(self):
        candidate = _candidate()
        live = _live_review(candidate)
        provider = Mock()
        provider.fetch_pull_request.return_value = _live_pull_request(candidate)
        provider.fetch_review.return_value = live

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_path = root / "candidate.json"
            candidate_path.write_text(canonical_json(candidate.to_dict()), encoding="utf-8")
            release, output_path = promote_strategy_release(
                candidate_path,
                release_dir=root / "releases",
                repository=live.repository,
                pull_request_number=live.pull_request_number,
                review_record_id=live.review_record_id,
                provider=provider,
            )

            self.assertEqual(root / "releases" / f"{release.strategy_release_id}.json", output_path)
            self.assertEqual(canonical_json(release.to_dict()), output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                release,
                StrategyReleaseV1.from_dict(json.loads(output_path.read_text(encoding="utf-8"))),
            )


class StrictStrategyReleaseResolverTests(unittest.TestCase):
    def test_exact_release_resolves_without_git_registry_or_scm_lookup(self):
        release = StrategyReleaseV1.create(candidate=_candidate(), approval=_approval(_candidate()))
        with TemporaryDirectory() as tmp:
            release_dir = Path(tmp)
            path = release_dir / f"{release.strategy_release_id}.json"
            path.write_text(json.dumps(release.to_dict()), encoding="utf-8")
            resolver = StrictStrategyReleaseResolver(release_dir)

            with patch("mu_strategy.strategies.registry.strategy_rule_descriptor") as registry, patch(
                "subprocess.run"
            ) as git_or_scm:
                restored = resolver.resolve(
                    release.strategy_release_id,
                    expected_rule_id="mu.baseline.second_pullback.long_limit.v1",
                    expected_symbol="MU-USDT-SWAP",
                )

        self.assertEqual(release, restored)
        registry.assert_not_called()
        git_or_scm.assert_not_called()
        parameters = inspect.signature(StrictStrategyReleaseResolver.resolve).parameters
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameters["expected_rule_id"].kind)
        self.assertEqual(inspect.Parameter.empty, parameters["expected_rule_id"].default)
        self.assertEqual(inspect.Parameter.empty, parameters["expected_symbol"].default)
        self.assertNotIn("latest", dir(resolver))
        self.assertNotIn("by_name", dir(resolver))

    def test_self_consistent_untrusted_approval_snapshots_fail_closed(self):
        candidate = _candidate()
        release = StrategyReleaseV1.create(candidate=candidate, approval=_approval(candidate))
        invalid_snapshots = (
            ("provider", {"scm_provider": "gitlab"}),
            ("repository", {"repository": "attacker/fork"}),
            ("independent", {"reviewer_id": "RELEASE-AUTHOR"}),
            ("record", {"review_record_id": "not-numeric"}),
            ("record", {"review_record_id": "0"}),
            ("statement", {"statement": "self-authored approval"}),
            ("URL", {"review_url": "https://attacker.invalid/review/1"}),
            (
                "URL",
                {
                    "review_url": "https://github.com/amazing-fish/mu-5x-fibonacci-trading/"
                    "pull/46#pullrequestreview-1"
                },
            ),
            (
                "URL",
                {
                    "review_url": "https://github.com/amazing-fish/mu-5x-fibonacci-trading/"
                    "pull/45#pullrequestreview-2"
                },
            ),
        )

        with TemporaryDirectory() as tmp:
            release_dir = Path(tmp)
            resolver = StrictStrategyReleaseResolver(release_dir)
            for expected_error, snapshot_updates in invalid_snapshots:
                with self.subTest(snapshot_updates=snapshot_updates):
                    wire = _readdress_release_with_snapshot_updates(release, snapshot_updates)
                    release_id = wire["strategy_release_id"]
                    (release_dir / f"{release_id}.json").write_text(
                        canonical_json(wire),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(StrategyReleaseResolutionError, expected_error):
                        resolver.resolve(
                            release_id,
                            expected_rule_id=candidate.strategy_rule_id,
                            expected_symbol=candidate.dataset.symbol,
                        )

    def test_invalid_id_mismatch_missing_corrupt_and_path_binding_fail_closed(self):
        candidate = _candidate()
        release = StrategyReleaseV1.create(candidate=candidate, approval=_approval(candidate))
        with TemporaryDirectory() as tmp:
            release_dir = Path(tmp)
            resolver = StrictStrategyReleaseResolver(release_dir)
            with self.assertRaisesRegex(StrategyReleaseResolutionError, "strategy_release_id"):
                resolver.resolve(
                    "../latest",
                    expected_rule_id=candidate.strategy_rule_id,
                    expected_symbol=candidate.dataset.symbol,
                )
            with self.assertRaisesRegex(StrategyReleaseResolutionError, "not found"):
                resolver.resolve(
                    "sr1_" + "0" * 64,
                    expected_rule_id=candidate.strategy_rule_id,
                    expected_symbol=candidate.dataset.symbol,
                )

            path = release_dir / f"{release.strategy_release_id}.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(StrategyReleaseResolutionError):
                resolver.resolve(
                    release.strategy_release_id,
                    expected_rule_id=candidate.strategy_rule_id,
                    expected_symbol=candidate.dataset.symbol,
                )

            path.write_text(json.dumps(release.to_dict()), encoding="utf-8")
            with self.assertRaisesRegex(StrategyReleaseResolutionError, "rule"):
                resolver.resolve(
                    release.strategy_release_id,
                    expected_rule_id="mu.other.long_limit.v1",
                    expected_symbol=candidate.dataset.symbol,
                )
            with self.assertRaisesRegex(StrategyReleaseResolutionError, "symbol"):
                resolver.resolve(
                    release.strategy_release_id,
                    expected_rule_id=candidate.strategy_rule_id,
                    expected_symbol="BTC-USDT-SWAP",
                )

            alias_id = "sr1_" + "f" * 64
            (release_dir / f"{alias_id}.json").write_text(json.dumps(release.to_dict()), encoding="utf-8")
            with self.assertRaisesRegex(StrategyReleaseResolutionError, "path"):
                resolver.resolve(
                    alias_id,
                    expected_rule_id=candidate.strategy_rule_id,
                    expected_symbol=candidate.dataset.symbol,
                )


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
    changed = ending_equity != "10000"
    return tuple(
        ExperimentWindowResultV1.create(
            role=role,
            trade_count=2,
            starting_equity="10000",
            ending_equity=ending_equity,
            gross_profit="11" if changed else "10",
            gross_loss="10",
            total_return_pct="0.0001" if changed else "0",
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
        review_record_id="1",
        reviewer_id="independent-reviewer",
        author_id="release-author",
        reviewed_at_ms=1_700_100_000_000,
        decision=decision,
        candidate_fingerprint=candidate.candidate_fingerprint,
        evaluated_code_commit_sha=candidate.evaluated_code_commit_sha,
        statement=approval_statement(candidate),
        review_url="https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/45#pullrequestreview-1",
    )
    return StrategyReleaseApprovalV1.create(review_snapshot=snapshot)


def _readdress_release_with_snapshot_updates(
    release: StrategyReleaseV1,
    snapshot_updates: dict,
) -> dict:
    wire = release.to_dict()
    snapshot = wire["approval"]["review_snapshot"]
    snapshot.update(snapshot_updates)
    snapshot["snapshot_sha256"] = canonical_sha256(
        {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    )
    wire["approval"]["approval_snapshot_sha256"] = snapshot["snapshot_sha256"]
    wire["strategy_release_id"] = "sr1_" + canonical_sha256(
        {"candidate": wire["candidate"], "approval": wire["approval"]}
    )
    return wire


def _readdress_candidate_wire(wire: dict) -> None:
    wire["strategy_config_sha256"] = canonical_sha256(wire["strategy_config"])
    for result in wire["results"]:
        result["result_fingerprint"] = canonical_sha256(
            {key: value for key, value in result.items() if key != "result_fingerprint"}
        )
    wire["result_fingerprint"] = canonical_sha256(
        {
            "experiment_protocol_id": wire["experiment_protocol_id"],
            "windows": wire["windows"],
            "assumptions": wire["assumptions"],
            "results": wire["results"],
        }
    )
    wire["candidate_fingerprint"] = canonical_sha256(
        {key: value for key, value in wire.items() if key != "candidate_fingerprint"}
    )


def _live_review(candidate: StrategyReleaseCandidateV1) -> LiveScmReview:
    return LiveScmReview(
        scm_provider="github",
        repository="amazing-fish/mu-5x-fibonacci-trading",
        pull_request_number=45,
        review_record_id="1",
        reviewer_id="independent-reviewer",
        reviewed_at_ms=1_700_100_000_000,
        decision=ReleaseDecision.APPROVED,
        statement=approval_statement(candidate),
        review_url="https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/45#pullrequestreview-1",
        includes_created_edit=False,
        last_edited_at_ms=None,
    )


def _live_pull_request(candidate: StrategyReleaseCandidateV1) -> LiveScmPullRequest:
    return LiveScmPullRequest(
        repository="amazing-fish/mu-5x-fibonacci-trading",
        pull_request_number=45,
        author_id="pr-author",
        commits=(
            LiveScmCommit(candidate.evaluated_code_commit_sha, "release-author", "release-committer"),
            LiveScmCommit("c" * 40, "docs-author", "docs-committer"),
        ),
    )


if __name__ == "__main__":
    unittest.main()
