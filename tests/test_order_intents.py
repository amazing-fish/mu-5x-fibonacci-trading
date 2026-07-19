import json
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.execution import (
    ORDER_INTENT_FINGERPRINT_FIELDS,
    ExecutionEnvironment,
    IntentRevisionAction,
    OKXInstrumentSpec,
    OrderIntent,
    OrderIntentFactory,
    OrderIntentIneligibleError,
    OrderIntentRevisionError,
    OrderIntentSchemaError,
    classify_intent_revision,
    render_order_intent_review,
)
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import EntryDecisionCode
from mu_strategy.observations import (
    STAGE0_TRUST_POLICY_NAME,
    STAGE0_TRUST_POLICY_VERSION,
    TrustedObservationReference,
    build_stage0_observation,
    canonical_payload_sha256,
)
from mu_strategy.research.strategy_releases import (
    EXPERIMENT_PROTOCOL_ID,
    STRATEGY_RELEASE_V1_RULE_ID,
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
    StrategyReleaseResolutionError,
    StrategyReleaseV1,
    StrictStrategyReleaseResolver,
    TrustedExperimentDatasetV1,
    strategy_release_approval_statement,
)
from mu_strategy.strategies.registry import baseline_strategy_group


SIGNAL_TIME_MS = 1_700_000_000_000
OBSERVED_AT_MS = SIGNAL_TIME_MS + 60_000
CREATED_AT_MS = OBSERVED_AT_MS + 1_000
REQUIRED_HASHES = (
    ("15m", "b" * 64),
    ("1h", "c" * 64),
    ("5m", "a" * 64),
)


class OrderIntentContractTests(unittest.TestCase):
    def setUp(self):
        self.release = _release()
        self.observation = _observation()
        self.instrument = OKXInstrumentSpec(
            inst_id="MU-USDT-SWAP",
            tick_size=Decimal("0.1"),
            lot_size=Decimal("0.01"),
            contract_value=Decimal("1"),
        )
        self.factory, self.resolver = _factory_for(self.release)

    def test_factory_builds_frozen_closed_round_trippable_demo_intent(self):
        intent = self._create()

        self.assertEqual(1, intent.schema_version)
        self.assertIs(ExecutionEnvironment.DEMO, intent.environment)
        self.assertEqual("MU-USDT-SWAP", intent.symbol)
        self.assertEqual("buy", intent.side)
        self.assertEqual("limit", intent.order_type)
        self.assertEqual("0.09", intent.size)
        self.assertEqual("101.2", intent.limit_price)
        self.assertEqual("isolated", intent.td_mode)
        self.assertEqual("long", intent.pos_side)
        self.assertEqual(5, intent.leverage)
        self.assertEqual("99.9", intent.initial_stop)
        self.assertEqual(self.observation.observation_id, intent.source_observation_id)
        self.assertEqual(REQUIRED_HASHES, intent.data_content_sha256)
        self.assertEqual(self.release.strategy_release_id, intent.strategy_release_id)
        self.assertEqual(self.release.candidate.strategy_config_sha256, intent.strategy_config_sha256)
        self.assertNotEqual(
            self.observation.strategy_config_fingerprint,
            intent.strategy_config_sha256,
            "Stage 0 and release config hashes intentionally use different canonical schemas",
        )
        self.assertEqual(8, intent.second_pullback_wait_bars)
        self.assertEqual(SIGNAL_TIME_MS + 8 * 15 * 60 * 1_000, intent.expires_at_ms)
        self.assertEqual(intent.business_action_id, intent.audit_correlation_id)
        self.assertRegex(intent.signal_lineage_id, r"^sig1_[0-9a-f]{64}$")
        self.assertRegex(intent.business_action_id, r"^ba1_[0-9a-f]{64}$")
        self.assertRegex(intent.intent_id, r"^oi1_[0-9a-f]{64}$")

        restored = OrderIntent.from_json(intent.to_json())
        self.assertEqual(intent, restored)
        self.assertEqual(intent.to_dict(), restored.to_dict())
        with self.assertRaises(FrozenInstanceError):
            intent.size = "1"

        self.resolver.resolve.assert_called_once_with(
            self.release.strategy_release_id,
            expected_rule_id=STRATEGY_RELEASE_V1_RULE_ID,
            expected_symbol="MU-USDT-SWAP",
        )

    def test_factory_consumes_a_real_exact_id_strict_resolver_without_fallback(self):
        with TemporaryDirectory() as directory:
            release_dir = Path(directory)
            release_path = release_dir / f"{self.release.strategy_release_id}.json"
            release_path.write_text(json.dumps(self.release.to_dict()), encoding="utf-8")
            factory = OrderIntentFactory(StrictStrategyReleaseResolver(release_dir))

            intent = factory.create_demo_intent(
                observation=self.observation,
                strategy_release_id=self.release.strategy_release_id,
                instrument_spec=self.instrument,
                notional_usdt="10",
                created_at_ms=CREATED_AT_MS,
            )

            self.assertEqual(self.release.strategy_release_id, intent.strategy_release_id)
            with self.assertRaisesRegex(OrderIntentIneligibleError, "strategy release"):
                factory.create_demo_intent(
                    observation=self.observation,
                    strategy_release_id="sr1_" + "0" * 64,
                    instrument_spec=self.instrument,
                    notional_usdt="10",
                    created_at_ms=CREATED_AT_MS,
                )

    def test_closed_schema_rejects_unknown_missing_version_bool_and_noncanonical_values(self):
        wire = self._create().to_dict()

        malformed = dict(wire)
        malformed["future"] = True
        with self.assertRaisesRegex(OrderIntentSchemaError, "unknown"):
            OrderIntent.from_dict(malformed)

        malformed = dict(wire)
        malformed.pop("strategy_release_id")
        with self.assertRaisesRegex(OrderIntentSchemaError, "missing"):
            OrderIntent.from_dict(malformed)

        for field_name, value, pattern in (
            ("schema_version", 2, "schema_version"),
            ("created_at_ms", True, "integer"),
            ("size", "0.090", "canonical"),
            ("limit_price", "NaN", "finite"),
            ("initial_stop", "0", "positive"),
        ):
            with self.subTest(field=field_name):
                malformed = {**wire, field_name: value}
                with self.assertRaisesRegex(OrderIntentSchemaError, pattern):
                    OrderIntent.from_dict(malformed)

    def test_reader_rejects_readdressed_intent_with_arbitrary_expiry(self):
        intent = self._create()
        changed = _unchecked_replace(intent, expires_at_ms=intent.expires_at_ms + 1)
        readdressed = _unchecked_replace(changed, intent_id=f"oi1_{changed.intent_fingerprint}")

        with self.assertRaisesRegex(OrderIntentSchemaError, "scanner lifecycle"):
            OrderIntent.from_dict(readdressed.to_dict())

    def test_fingerprint_matrix_binds_every_control_field_and_excludes_only_derived_or_creation_fields(self):
        intent = self._create()
        mutations = {
            "schema_version": 2,
            "signal_lineage_id": "sig1_" + "e" * 64,
            "business_action_id": "ba1_" + "e" * 64,
            "environment": ExecutionEnvironment.PRODUCTION,
            "symbol": "BTC-USDT-SWAP",
            "side": "sell",
            "order_type": "market",
            "size": "0.1",
            "limit_price": "101.3",
            "td_mode": "cross",
            "pos_side": "short",
            "leverage": 4,
            "initial_stop": "99.8",
            "source_observation_id": "obs-2",
            "signal_time_ms": intent.signal_time_ms + 1,
            "decision_code": EntryDecisionCode.SIGNAL_CONFIRMED,
            "trusted_run_id": "e" * 32,
            "observed_at_ms": intent.observed_at_ms + 1,
            "data_content_sha256": (
                ("15m", "d" * 64),
                ("1h", "c" * 64),
                ("5m", "a" * 64),
            ),
            "strategy_release_id": "sr1_" + "e" * 64,
            "strategy_name": "baseline-v2",
            "strategy_config_sha256": "e" * 64,
            "second_pullback_wait_bars": 9,
            "expires_at_ms": intent.expires_at_ms + 1,
        }

        self.assertEqual(set(ORDER_INTENT_FINGERPRINT_FIELDS), set(mutations))
        for field_name, value in mutations.items():
            with self.subTest(field=field_name):
                mutant = _unchecked_replace(intent, **{field_name: value})
                self.assertNotEqual(intent.intent_fingerprint, mutant.intent_fingerprint)

        for field_name, value in (
            ("intent_id", "oi1_" + "e" * 64),
            ("audit_correlation_id", "ba1_" + "e" * 64),
            ("created_at_ms", intent.created_at_ms + 1),
        ):
            with self.subTest(excluded=field_name):
                mutant = _unchecked_replace(intent, **{field_name: value})
                self.assertEqual(intent.intent_fingerprint, mutant.intent_fingerprint)

    def test_lineage_and_demo_action_are_stable_across_data_and_release_revisions(self):
        second_release = _release(evaluated_code_commit_sha="b" * 40, review_record_id="2")
        second_observation = _observation(
            observation_id="obs-2",
            run_id="e" * 32,
            hashes=(("15m", "d" * 64), ("1h", "e" * 64), ("5m", "f" * 64)),
            observed_at_ms=OBSERVED_AT_MS + 1,
        )
        resolver = Mock(spec=StrictStrategyReleaseResolver)
        releases = {
            self.release.strategy_release_id: self.release,
            second_release.strategy_release_id: second_release,
        }
        resolver.resolve.side_effect = lambda release_id, **_: releases[release_id]
        factory = OrderIntentFactory(resolver)

        first = factory.create_demo_intent(
            observation=self.observation,
            strategy_release_id=self.release.strategy_release_id,
            instrument_spec=self.instrument,
            notional_usdt="10",
            created_at_ms=CREATED_AT_MS,
        )
        second = factory.create_demo_intent(
            observation=second_observation,
            strategy_release_id=second_release.strategy_release_id,
            instrument_spec=self.instrument,
            notional_usdt="10",
            created_at_ms=CREATED_AT_MS + 1,
        )

        self.assertEqual(first.signal_lineage_id, second.signal_lineage_id)
        self.assertEqual(first.business_action_id, second.business_action_id)
        self.assertNotEqual(first.intent_id, second.intent_id)

    def test_factory_accepts_both_typed_ready_codes_and_ignores_free_text_action(self):
        for decision_code in (
            EntryDecisionCode.SIGNAL_CONFIRMED,
            EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY,
        ):
            with self.subTest(decision_code=decision_code):
                observation = replace(
                    _observation(decision_code=decision_code),
                    compatibility_action="do-not-authorize-from-this-text",
                )
                intent = self.factory.create_demo_intent(
                    observation=observation,
                    strategy_release_id=self.release.strategy_release_id,
                    instrument_spec=self.instrument,
                    notional_usdt="10",
                    created_at_ms=CREATED_AT_MS,
                )
                self.assertIs(decision_code, intent.decision_code)

    def test_factory_rejects_non_ready_missing_scan_fields_and_noncanonical_trust(self):
        cases = {
            "wait_with_enter_text": replace(
                _observation(decision_code=EntryDecisionCode.WAITING_SECOND_PULLBACK),
                compatibility_action="enter",
            ),
            "unknown": _unchecked_dataclass_replace(
                _observation(),
                decision_code=EntryDecisionCode.UNKNOWN,
            ),
            "missing_trigger": _observation(trigger_price=None),
            "missing_stop": _observation(initial_stop=None),
            "missing_signal_time": _observation(signal_time_ms=None),
            "missing_5m": _observation(
                hashes=(("15m", "b" * 64), ("1h", "c" * 64)),
                effective_intervals=("15m", "1h"),
            ),
            "legacy_run_id": _observation(run_id="legacy-flat-data"),
        }
        for label, observation in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(OrderIntentIneligibleError):
                    self.factory.create_demo_intent(
                        observation=observation,
                        strategy_release_id=self.release.strategy_release_id,
                        instrument_spec=self.instrument,
                        notional_usdt="10",
                        created_at_ms=CREATED_AT_MS,
                    )

    def test_factory_rejects_release_config_strategy_symbol_sizing_and_resolution_failures(self):
        mismatched_config = replace(baseline_strategy_group("MU-USDT-SWAP").config, fib_lookback=33)
        cases = (
            (
                _observation(config=mismatched_config),
                self.instrument,
                "10",
            ),
            (
                _observation(strategy_name="other"),
                self.instrument,
                "10",
            ),
            (
                self.observation,
                OKXInstrumentSpec("BTC-USDT-SWAP", Decimal("0.1"), Decimal("0.01"), Decimal("1")),
                "10",
            ),
            (self.observation, self.instrument, "0"),
            (self.observation, self.instrument, "10.0"),
        )
        for observation, instrument, notional in cases:
            with self.subTest(observation=observation.observation_id, instrument=instrument.inst_id, notional=notional):
                with self.assertRaises(OrderIntentIneligibleError):
                    self.factory.create_demo_intent(
                        observation=observation,
                        strategy_release_id=self.release.strategy_release_id,
                        instrument_spec=instrument,
                        notional_usdt=notional,
                        created_at_ms=CREATED_AT_MS,
                    )

        resolver = Mock(spec=StrictStrategyReleaseResolver)
        resolver.resolve.side_effect = StrategyReleaseResolutionError("missing or unapproved")
        with self.assertRaisesRegex(OrderIntentIneligibleError, "strategy release"):
            OrderIntentFactory(resolver).create_demo_intent(
                observation=self.observation,
                strategy_release_id="sr1_" + "0" * 64,
                instrument_spec=self.instrument,
                notional_usdt="10",
                created_at_ms=CREATED_AT_MS,
            )

    def test_factory_rejects_non_mu_observation_before_release_resolution(self):
        observation = _observation(symbol="BTC-USDT-SWAP")
        instrument = OKXInstrumentSpec(
            "BTC-USDT-SWAP",
            Decimal("0.1"),
            Decimal("0.01"),
            Decimal("1"),
        )
        self.resolver.reset_mock()

        with self.assertRaisesRegex(OrderIntentIneligibleError, "MU-USDT-SWAP"):
            self.factory.create_demo_intent(
                observation=observation,
                strategy_release_id=self.release.strategy_release_id,
                instrument_spec=instrument,
                notional_usdt="10",
                created_at_ms=CREATED_AT_MS,
            )

        self.resolver.resolve.assert_not_called()

    def test_expiry_uses_release_scanner_lifecycle_and_is_exclusive_at_boundary(self):
        expiry = SIGNAL_TIME_MS + 8 * 15 * 60 * 1_000
        accepted = self.factory.create_demo_intent(
            observation=self.observation,
            strategy_release_id=self.release.strategy_release_id,
            instrument_spec=self.instrument,
            notional_usdt="10",
            created_at_ms=expiry - 1,
        )
        self.assertEqual(expiry, accepted.expires_at_ms)

        with self.assertRaisesRegex(OrderIntentIneligibleError, "expired"):
            self.factory.create_demo_intent(
                observation=self.observation,
                strategy_release_id=self.release.strategy_release_id,
                instrument_spec=self.instrument,
                notional_usdt="10",
                created_at_ms=expiry,
            )

    def test_instrument_sizing_is_canonical_complete_and_fail_closed(self):
        spec = OKXInstrumentSpec.from_row(
            {"instId": "MU-USDT-SWAP", "tickSz": "0.1", "lotSz": "0.01", "ctVal": "1"}
        )
        self.assertEqual("101.2", spec.price_to_string("101.239"))
        self.assertEqual("0.09", spec.size_for_notional("10", price="101.2"))

        with self.assertRaisesRegex(ValueError, "ctVal"):
            OKXInstrumentSpec.from_row(
                {"instId": "MU-USDT-SWAP", "tickSz": "0.1", "lotSz": "0.01"}
            )

        for kwargs in (
            {"inst_id": "", "tick_size": Decimal("0.1"), "lot_size": Decimal("0.01"), "contract_value": Decimal("1")},
            {"inst_id": "MU-USDT-SWAP", "tick_size": Decimal("0"), "lot_size": Decimal("0.01"), "contract_value": Decimal("1")},
            {"inst_id": "MU-USDT-SWAP", "tick_size": Decimal("NaN"), "lot_size": Decimal("0.01"), "contract_value": Decimal("1")},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    OKXInstrumentSpec(**kwargs)

        with self.assertRaises(ValueError):
            spec.size_for_notional("0", price="101.2")
        with self.assertRaises(ValueError):
            spec.size_for_notional("0.001", price="101.2")

    def test_review_renderer_is_deterministic_complete_and_not_a_broker_request(self):
        intent = self._create()

        first = render_order_intent_review(intent)
        second = render_order_intent_review(intent)

        self.assertEqual(first, second)
        for required in (
            intent.intent_id,
            intent.business_action_id,
            intent.source_observation_id,
            intent.strategy_release_id,
            intent.trusted_run_id,
            intent.limit_price,
            intent.size,
            intent.initial_stop,
            "NOT broker-side protection",
            "human review",
        ):
            self.assertIn(required, first)
        for forbidden in ("clOrdId", '"instId"', '"tdMode"', '"ordType"', '"sz"', '"px"', "api_key", "authorization"):
            self.assertNotIn(forbidden, first)

        corrupted = _unchecked_replace(intent, intent_id="oi1_" + "0" * 64)
        with self.assertRaises(OrderIntentSchemaError):
            render_order_intent_review(corrupted)

    def test_execution_intent_modules_have_no_application_or_mutation_reachability(self):
        root = Path(__file__).resolve().parents[1]
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (root / "mu_strategy" / "execution").glob("*.py")
        )
        for forbidden in (
            "mu_strategy.live",
            "mu_strategy.demo_trading",
            "OKXCredentials",
            ".submit_order(",
            ".cancel_order(",
            ".set_leverage(",
            "trusted_refresh",
            "clOrdId",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def _create(self, **overrides):
        kwargs = {
            "observation": self.observation,
            "strategy_release_id": self.release.strategy_release_id,
            "instrument_spec": self.instrument,
            "notional_usdt": "10",
            "created_at_ms": CREATED_AT_MS,
        }
        kwargs.update(overrides)
        return self.factory.create_demo_intent(**kwargs)


class IntentRevisionClassifierTests(unittest.TestCase):
    def setUp(self):
        release = _release()
        self.factory, _ = _factory_for(release)
        self.release_id = release.strategy_release_id
        self.instrument = OKXInstrumentSpec("MU-USDT-SWAP", Decimal("0.1"), Decimal("0.01"), Decimal("1"))
        self.observation = _observation()
        self.first = self._create(created_at_ms=CREATED_AT_MS)

    def test_create_reuse_supersede_and_reserved_conflict_are_pure_and_deterministic(self):
        created = classify_intent_revision(None, self.first, mutation_reserved=False)
        self.assertIs(IntentRevisionAction.CREATE, created.action)
        self.assertIs(self.first, created.intent)

        duplicate = self._create(created_at_ms=CREATED_AT_MS + 1)
        reused = classify_intent_revision(self.first, duplicate, mutation_reserved=False)
        self.assertIs(IntentRevisionAction.REUSE, reused.action)
        self.assertIs(self.first, reused.intent)
        self.assertEqual(CREATED_AT_MS, reused.intent.created_at_ms)

        revised = self._create(created_at_ms=CREATED_AT_MS + 1, notional_usdt="20")
        superseded = classify_intent_revision(self.first, revised, mutation_reserved=False)
        self.assertIs(IntentRevisionAction.SUPERSEDE, superseded.action)
        self.assertIs(revised, superseded.intent)
        self.assertEqual(self.first.intent_id, superseded.supersedes_intent_id)

        conflict = classify_intent_revision(self.first, revised, mutation_reserved=True)
        self.assertIs(IntentRevisionAction.CONFLICT, conflict.action)
        self.assertIs(self.first, conflict.intent)

    def test_classifier_rejects_cross_action_cross_environment_and_non_boolean_reservation_state(self):
        other_signal = _observation(
            signal_time_ms=SIGNAL_TIME_MS + 900_000,
            observed_at_ms=OBSERVED_AT_MS + 900_000,
            observation_id="obs-other",
        )
        other_action = self._create(observation=other_signal, created_at_ms=CREATED_AT_MS + 900_000)
        with self.assertRaisesRegex(OrderIntentRevisionError, "business action"):
            classify_intent_revision(self.first, other_action, mutation_reserved=False)

        production = _unchecked_replace(self.first, environment=ExecutionEnvironment.PRODUCTION)
        with self.assertRaisesRegex(OrderIntentRevisionError, "environment"):
            classify_intent_revision(self.first, production, mutation_reserved=False)

        with self.assertRaisesRegex(TypeError, "mutation_reserved"):
            classify_intent_revision(self.first, self.first, mutation_reserved=1)

        with self.assertRaisesRegex(OrderIntentRevisionError, "reservation"):
            classify_intent_revision(None, self.first, mutation_reserved=True)

        corrupted = _unchecked_replace(self.first, size="not-canonical")
        with self.assertRaises(OrderIntentRevisionError):
            classify_intent_revision(self.first, corrupted, mutation_reserved=False)

    def _create(self, **overrides):
        kwargs = {
            "observation": self.observation,
            "strategy_release_id": self.release_id,
            "instrument_spec": self.instrument,
            "notional_usdt": "10",
            "created_at_ms": CREATED_AT_MS,
        }
        kwargs.update(overrides)
        return self.factory.create_demo_intent(**kwargs)


def _factory_for(release):
    resolver = Mock(spec=StrictStrategyReleaseResolver)
    resolver.resolve.return_value = release
    return OrderIntentFactory(resolver), resolver


def _observation(
    *,
    observation_id="obs-1",
    decision_code=EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY,
    run_id="d" * 32,
    hashes=REQUIRED_HASHES,
    effective_intervals=("5m", "15m", "1h"),
    trigger_price=101.239,
    initial_stop=99.987,
    signal_time_ms=SIGNAL_TIME_MS,
    observed_at_ms=OBSERVED_AT_MS,
    config=None,
    strategy_name="baseline",
    symbol="MU-USDT-SWAP",
):
    config = config or baseline_strategy_group(symbol).config
    trusted = TrustedObservationReference(
        run_id=run_id,
        requested_intervals=tuple(interval for interval in ("15m", "1h") if interval in effective_intervals),
        effective_intervals=effective_intervals,
        content_sha256_by_interval=hashes,
        policy_name=STAGE0_TRUST_POLICY_NAME,
        policy_version=STAGE0_TRUST_POLICY_VERSION,
        allowed=True,
        reason=HealthReason.OK,
    )
    result = EntryScanResult(
        symbol=symbol,
        action="presentation-only",
        reason="presentation-only",
        last_close=101.5,
        regime_1h="green",
        rsi14=55.0,
        macd_hist=1.0,
        macd_hist_prev=0.5,
        fib_level=trigger_price,
        fib_distance_pct=0.001,
        trigger_price=trigger_price,
        initial_stop=initial_stop,
        signal_time_ms=signal_time_ms,
        decision_code=decision_code,
    )
    return build_stage0_observation(
        observation_id=observation_id,
        cycle_id="cycle-1",
        symbol=symbol,
        created_at_ms=observed_at_ms,
        observed_at_ms=observed_at_ms,
        trusted=trusted,
        strategy_name=strategy_name,
        strategy_config_fingerprint=canonical_payload_sha256(config),
        result=result,
        compatibility_source="scanner",
        provenance="strict trusted scanner",
    )


def _release(*, evaluated_code_commit_sha="a" * 40, review_record_id="1"):
    config = StrategyConfigPayloadV1.from_config(baseline_strategy_group("MU-USDT-SWAP").config)
    candidate = StrategyReleaseCandidateV1.create(
        strategy_rule_id=STRATEGY_RELEASE_V1_RULE_ID,
        strategy_name="baseline",
        supported_symbols=("MU-USDT-SWAP",),
        strategy_config=config,
        evaluated_code_commit_sha=evaluated_code_commit_sha,
        dataset=TrustedExperimentDatasetV1(
            run_id="a" * 32,
            symbol="MU-USDT-SWAP",
            manifest_schema_version=3,
            requested_intervals=("5m", "15m", "1h"),
            effective_intervals=("5m", "15m", "1h"),
            content_sha256_by_interval=REQUIRED_HASHES,
        ),
        windows=_windows(),
        experiment_protocol_id=EXPERIMENT_PROTOCOL_ID,
        assumptions=BacktestAssumptionsV1(
            starting_equity="10000",
            fee_profile="market",
            fee_rate="0.0005",
            fill_model=FillModel.DETERMINISTIC_OHLC,
            slippage_bps="0",
            partial_fill_model=PartialFillModel.NONE,
        ),
        results=tuple(
            ExperimentWindowResultV1.create(
                role=role,
                trade_count=2,
                starting_equity="10000",
                ending_equity="10000",
                gross_profit="10",
                gross_loss="10",
                total_return_pct="0",
                max_drawdown_pct="-0.01",
            )
            for role in ExperimentWindowRole
        ),
        selection_reason=SelectionReasonCode.BASELINE_CONTINUITY,
    )
    snapshot = ScmReviewSnapshotV1.create(
        scm_provider="github",
        repository="amazing-fish/mu-5x-fibonacci-trading",
        pull_request_number=45,
        review_record_id=review_record_id,
        reviewer_id="independent-reviewer",
        author_id="release-author",
        reviewed_at_ms=1_700_100_000_000,
        decision=ReleaseDecision.APPROVED,
        candidate_fingerprint=candidate.candidate_fingerprint,
        evaluated_code_commit_sha=candidate.evaluated_code_commit_sha,
        statement=strategy_release_approval_statement(
            candidate.candidate_fingerprint,
            candidate.evaluated_code_commit_sha,
        ),
        review_url=(
            "https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/45"
            f"#pullrequestreview-{review_record_id}"
        ),
    )
    approval = StrategyReleaseApprovalV1.create(review_snapshot=snapshot)
    return StrategyReleaseV1.create(candidate=candidate, approval=approval)


def _windows():
    width = 10_000_000
    return (
        ExperimentWindow(ExperimentWindowRole.TRAIN, SIGNAL_TIME_MS, SIGNAL_TIME_MS, SIGNAL_TIME_MS + width),
        ExperimentWindow(
            ExperimentWindowRole.VALIDATION,
            SIGNAL_TIME_MS + width,
            SIGNAL_TIME_MS + width,
            SIGNAL_TIME_MS + 2 * width,
        ),
        ExperimentWindow(
            ExperimentWindowRole.OUT_OF_SAMPLE,
            SIGNAL_TIME_MS + 2 * width,
            SIGNAL_TIME_MS + 2 * width,
            SIGNAL_TIME_MS + 3 * width,
        ),
    )


def _unchecked_replace(intent, **updates):
    mutant = object.__new__(OrderIntent)
    for field in fields(intent):
        object.__setattr__(mutant, field.name, updates.get(field.name, getattr(intent, field.name)))
    return mutant


def _unchecked_dataclass_replace(value, **updates):
    mutant = object.__new__(type(value))
    for field in fields(value):
        object.__setattr__(mutant, field.name, updates.get(field.name, getattr(value, field.name)))
    return mutant


if __name__ == "__main__":
    unittest.main()
