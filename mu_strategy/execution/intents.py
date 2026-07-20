from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from enum import Enum, unique
from typing import Any

from mu_strategy.canonical import canonical_json, canonical_sha256
from mu_strategy.execution.instruments import OKXInstrumentSpec
from mu_strategy.models import EntryDecisionCode, EntryDisposition, entry_decision_metadata
from mu_strategy.observations import (
    STAGE0_TRUST_POLICY_NAME,
    STAGE0_TRUST_POLICY_VERSION,
    ObservationOutcome,
    ObservationSchemaError,
    Stage0Observation,
    canonical_payload_sha256,
)
from mu_strategy.research.strategy_releases import (
    STRATEGY_RELEASE_V1_RULE_ID,
    STRATEGY_RELEASE_V1_SYMBOL,
    StrategyReleaseResolutionError,
    StrategyReleaseV1,
    StrictStrategyReleaseResolver,
)


ORDER_INTENT_SCHEMA_VERSION = 1
ORDER_INTENT_REQUIRED_INTERVALS = frozenset({"5m", "15m", "1h"})
ORDER_INTENT_FINGERPRINT_FIELDS = (
    "schema_version",
    "signal_lineage_id",
    "business_action_id",
    "environment",
    "symbol",
    "side",
    "order_type",
    "size",
    "limit_price",
    "td_mode",
    "pos_side",
    "leverage",
    "initial_stop",
    "source_observation_id",
    "signal_time_ms",
    "decision_code",
    "trusted_run_id",
    "observed_at_ms",
    "data_content_sha256",
    "strategy_release_id",
    "strategy_name",
    "strategy_config_sha256",
    "second_pullback_wait_bars",
    "expires_at_ms",
)
_SCANNER_INTERVAL_MS = 15 * 60 * 1_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_RELEASE_ID_PATTERN = re.compile(r"^sr1_[0-9a-f]{64}$")
_SIGNAL_LINEAGE_ID_PATTERN = re.compile(r"^sig1_[0-9a-f]{64}$")
_BUSINESS_ACTION_ID_PATTERN = re.compile(r"^ba1_[0-9a-f]{64}$")
_INTENT_ID_PATTERN = re.compile(r"^oi1_[0-9a-f]{64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")
_ALLOWED_DECISIONS = frozenset(
    {
        EntryDecisionCode.SIGNAL_CONFIRMED,
        EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY,
    }
)


class OrderIntentSchemaError(ValueError):
    pass


class OrderIntentIneligibleError(ValueError):
    pass


class OrderIntentRevisionError(ValueError):
    pass


@unique
class ExecutionEnvironment(str, Enum):
    DEMO = "demo"
    PRODUCTION = "production"


@dataclass(frozen=True)
class OrderIntent:
    schema_version: int
    signal_lineage_id: str
    business_action_id: str
    intent_id: str
    audit_correlation_id: str
    environment: ExecutionEnvironment
    symbol: str
    side: str
    order_type: str
    size: str
    limit_price: str
    td_mode: str
    pos_side: str
    leverage: int
    initial_stop: str
    source_observation_id: str
    signal_time_ms: int
    decision_code: EntryDecisionCode
    trusted_run_id: str
    observed_at_ms: int
    data_content_sha256: tuple[tuple[str, str], ...]
    strategy_release_id: str
    strategy_name: str
    strategy_config_sha256: str
    second_pullback_wait_bars: int
    created_at_ms: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        try:
            self._validate()
        except OrderIntentSchemaError:
            raise
        except (TypeError, ValueError) as exc:
            raise OrderIntentSchemaError(str(exc)) from exc

    @property
    def intent_fingerprint(self) -> str:
        return canonical_sha256(self._fingerprint_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal_lineage_id": self.signal_lineage_id,
            "business_action_id": self.business_action_id,
            "intent_id": self.intent_id,
            "audit_correlation_id": self.audit_correlation_id,
            "environment": self.environment.value,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "size": self.size,
            "limit_price": self.limit_price,
            "td_mode": self.td_mode,
            "pos_side": self.pos_side,
            "leverage": self.leverage,
            "initial_stop": self.initial_stop,
            "source_observation_id": self.source_observation_id,
            "signal_time_ms": self.signal_time_ms,
            "decision_code": self.decision_code.value,
            "trusted_run_id": self.trusted_run_id,
            "observed_at_ms": self.observed_at_ms,
            "data_content_sha256": [list(item) for item in self.data_content_sha256],
            "strategy_release_id": self.strategy_release_id,
            "strategy_name": self.strategy_name,
            "strategy_config_sha256": self.strategy_config_sha256,
            "second_pullback_wait_bars": self.second_pullback_wait_bars,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "OrderIntent":
        if not isinstance(value, str):
            raise OrderIntentSchemaError("intent JSON must be text")
        try:
            return cls.from_dict(json.loads(value))
        except json.JSONDecodeError as exc:
            raise OrderIntentSchemaError(f"invalid intent JSON: {exc}") from exc

    @classmethod
    def from_dict(cls, payload: Any) -> "OrderIntent":
        if not isinstance(payload, dict):
            raise OrderIntentSchemaError("intent must be an object")
        expected = {field.name for field in fields(cls)}
        actual = set(payload)
        if unknown := actual - expected:
            raise OrderIntentSchemaError(f"intent has unknown fields: {sorted(unknown)}")
        if missing := expected - actual:
            raise OrderIntentSchemaError(f"intent is missing fields: {sorted(missing)}")
        try:
            hashes = payload["data_content_sha256"]
            if not isinstance(hashes, list):
                raise OrderIntentSchemaError("data_content_sha256 must be a list")
            normalized_hashes: list[tuple[str, str]] = []
            for item in hashes:
                if not isinstance(item, list) or len(item) != 2:
                    raise OrderIntentSchemaError("data_content_sha256 items must be two-item lists")
                normalized_hashes.append((item[0], item[1]))
            return cls(
                schema_version=_required_int(payload, "schema_version"),
                signal_lineage_id=_required_text(payload, "signal_lineage_id"),
                business_action_id=_required_text(payload, "business_action_id"),
                intent_id=_required_text(payload, "intent_id"),
                audit_correlation_id=_required_text(payload, "audit_correlation_id"),
                environment=ExecutionEnvironment(payload["environment"]),
                symbol=_required_text(payload, "symbol"),
                side=_required_text(payload, "side"),
                order_type=_required_text(payload, "order_type"),
                size=_required_text(payload, "size"),
                limit_price=_required_text(payload, "limit_price"),
                td_mode=_required_text(payload, "td_mode"),
                pos_side=_required_text(payload, "pos_side"),
                leverage=_required_int(payload, "leverage"),
                initial_stop=_required_text(payload, "initial_stop"),
                source_observation_id=_required_text(payload, "source_observation_id"),
                signal_time_ms=_required_int(payload, "signal_time_ms"),
                decision_code=EntryDecisionCode(payload["decision_code"]),
                trusted_run_id=_required_text(payload, "trusted_run_id"),
                observed_at_ms=_required_int(payload, "observed_at_ms"),
                data_content_sha256=tuple(normalized_hashes),
                strategy_release_id=_required_text(payload, "strategy_release_id"),
                strategy_name=_required_text(payload, "strategy_name"),
                strategy_config_sha256=_required_text(payload, "strategy_config_sha256"),
                second_pullback_wait_bars=_required_int(payload, "second_pullback_wait_bars"),
                created_at_ms=_required_int(payload, "created_at_ms"),
                expires_at_ms=_required_int(payload, "expires_at_ms"),
            )
        except OrderIntentSchemaError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise OrderIntentSchemaError(f"invalid intent: {exc}") from exc

    def _validate(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != ORDER_INTENT_SCHEMA_VERSION:
            raise OrderIntentSchemaError(f"unsupported intent schema_version: {self.schema_version}")
        if not isinstance(self.environment, ExecutionEnvironment):
            raise OrderIntentSchemaError("environment must be an ExecutionEnvironment")
        if self.environment is not ExecutionEnvironment.DEMO:
            raise OrderIntentSchemaError("OrderIntent v1 permits DEMO environment only")
        if not isinstance(self.symbol, str) or not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise OrderIntentSchemaError("symbol has invalid format")
        for field_name, expected in (
            ("side", "buy"),
            ("order_type", "limit"),
            ("td_mode", "isolated"),
            ("pos_side", "long"),
        ):
            if getattr(self, field_name) != expected:
                raise OrderIntentSchemaError(f"{field_name} must be {expected}")
        size = _canonical_positive_decimal(self.size, "size")
        limit_price = _canonical_positive_decimal(self.limit_price, "limit_price")
        initial_stop = _canonical_positive_decimal(self.initial_stop, "initial_stop")
        if initial_stop >= limit_price:
            raise OrderIntentSchemaError("initial_stop must be below limit_price for a long entry")
        if isinstance(self.leverage, bool) or not isinstance(self.leverage, int) or self.leverage <= 0:
            raise OrderIntentSchemaError("leverage must be a positive integer")
        if size <= 0:
            raise OrderIntentSchemaError("size must be positive")
        for field_name in ("signal_time_ms", "observed_at_ms", "created_at_ms", "expires_at_ms"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise OrderIntentSchemaError(f"{field_name} must be an integer")
            if value < 0:
                raise OrderIntentSchemaError(f"{field_name} must be non-negative")
        if self.signal_time_ms > self.observed_at_ms:
            raise OrderIntentSchemaError("signal_time_ms must not be after observed_at_ms")
        if self.observed_at_ms > self.created_at_ms:
            raise OrderIntentSchemaError("observed_at_ms must not be after created_at_ms")
        if self.created_at_ms >= self.expires_at_ms:
            raise OrderIntentSchemaError("intent must be created before its exclusive expiry")
        if (
            isinstance(self.second_pullback_wait_bars, bool)
            or not isinstance(self.second_pullback_wait_bars, int)
            or self.second_pullback_wait_bars <= 0
        ):
            raise OrderIntentSchemaError("second_pullback_wait_bars must be a positive integer")
        expected_expiry = self.signal_time_ms + self.second_pullback_wait_bars * _SCANNER_INTERVAL_MS
        if self.expires_at_ms != expected_expiry:
            raise OrderIntentSchemaError("expires_at_ms does not match the scanner lifecycle policy")
        if not isinstance(self.source_observation_id, str) or not self.source_observation_id:
            raise OrderIntentSchemaError("source_observation_id is required")
        if not isinstance(self.decision_code, EntryDecisionCode) or self.decision_code not in _ALLOWED_DECISIONS:
            raise OrderIntentSchemaError("decision_code is not allowed for OrderIntent v1")
        if entry_decision_metadata(self.decision_code).disposition is not EntryDisposition.READY:
            raise OrderIntentSchemaError("decision_code must derive READY")
        if not isinstance(self.trusted_run_id, str) or not _RUN_ID_PATTERN.fullmatch(self.trusted_run_id):
            raise OrderIntentSchemaError("trusted_run_id must be a lowercase 32-character generation ID")
        _validate_content_hashes(self.data_content_sha256)
        if not isinstance(self.strategy_release_id, str) or not _RELEASE_ID_PATTERN.fullmatch(self.strategy_release_id):
            raise OrderIntentSchemaError("strategy_release_id has invalid format")
        if not isinstance(self.strategy_name, str) or not self.strategy_name:
            raise OrderIntentSchemaError("strategy_name is required")
        if not isinstance(self.strategy_config_sha256, str) or not _SHA256_PATTERN.fullmatch(
            self.strategy_config_sha256
        ):
            raise OrderIntentSchemaError("strategy_config_sha256 must be lowercase SHA-256")
        if not isinstance(self.signal_lineage_id, str) or not _SIGNAL_LINEAGE_ID_PATTERN.fullmatch(
            self.signal_lineage_id
        ):
            raise OrderIntentSchemaError("signal_lineage_id has invalid format")
        expected_signal_lineage = _signal_lineage_id(
            strategy_config_sha256=self.strategy_config_sha256,
            symbol=self.symbol,
            signal_time_ms=self.signal_time_ms,
        )
        if self.signal_lineage_id != expected_signal_lineage:
            raise OrderIntentSchemaError("signal_lineage_id does not match canonical signal evidence")
        if not isinstance(self.business_action_id, str) or not _BUSINESS_ACTION_ID_PATTERN.fullmatch(
            self.business_action_id
        ):
            raise OrderIntentSchemaError("business_action_id has invalid format")
        expected_business_action = _business_action_id(self.environment, self.signal_lineage_id)
        if self.business_action_id != expected_business_action:
            raise OrderIntentSchemaError("business_action_id does not match Demo signal lineage")
        if self.audit_correlation_id != self.business_action_id:
            raise OrderIntentSchemaError("audit_correlation_id must equal business_action_id in v1")
        if not isinstance(self.intent_id, str) or not _INTENT_ID_PATTERN.fullmatch(self.intent_id):
            raise OrderIntentSchemaError("intent_id has invalid format")
        if self.intent_id != f"oi1_{self.intent_fingerprint}":
            raise OrderIntentSchemaError("intent_id does not match canonical fingerprint")

    def _fingerprint_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal_lineage_id": self.signal_lineage_id,
            "business_action_id": self.business_action_id,
            "environment": self.environment.value
            if isinstance(self.environment, ExecutionEnvironment)
            else str(self.environment),
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "size": self.size,
            "limit_price": self.limit_price,
            "td_mode": self.td_mode,
            "pos_side": self.pos_side,
            "leverage": self.leverage,
            "initial_stop": self.initial_stop,
            "source_observation_id": self.source_observation_id,
            "signal_time_ms": self.signal_time_ms,
            "decision_code": self.decision_code.value
            if isinstance(self.decision_code, EntryDecisionCode)
            else str(self.decision_code),
            "trusted_run_id": self.trusted_run_id,
            "observed_at_ms": self.observed_at_ms,
            "data_content_sha256": [list(item) for item in self.data_content_sha256],
            "strategy_release_id": self.strategy_release_id,
            "strategy_name": self.strategy_name,
            "strategy_config_sha256": self.strategy_config_sha256,
            "second_pullback_wait_bars": self.second_pullback_wait_bars,
            "expires_at_ms": self.expires_at_ms,
        }


class OrderIntentFactory:
    def __init__(self, resolver: StrictStrategyReleaseResolver) -> None:
        if not callable(getattr(resolver, "resolve", None)):
            raise TypeError("resolver must provide the strict exact-ID resolve contract")
        self._resolver = resolver

    def create_demo_intent(
        self,
        *,
        observation: Stage0Observation,
        strategy_release_id: str,
        instrument_spec: OKXInstrumentSpec,
        notional_usdt: str,
        created_at_ms: int,
    ) -> OrderIntent:
        canonical_observation = _canonical_eligible_observation(observation)
        if canonical_observation.symbol != STRATEGY_RELEASE_V1_SYMBOL:
            raise OrderIntentIneligibleError(
                f"OrderIntent v1 requires exact symbol {STRATEGY_RELEASE_V1_SYMBOL}"
            )
        if isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int) or created_at_ms < 0:
            raise OrderIntentIneligibleError("created_at_ms must be a non-negative integer")
        if created_at_ms < canonical_observation.created_at_ms:
            raise OrderIntentIneligibleError("intent cannot predate its source observation")
        if not isinstance(instrument_spec, OKXInstrumentSpec):
            raise OrderIntentIneligibleError("complete canonical instrument sizing is required")
        if instrument_spec.inst_id != canonical_observation.symbol:
            raise OrderIntentIneligibleError("instrument symbol does not match observation")
        notional = _canonical_input_decimal(notional_usdt, "notional_usdt")

        try:
            release = self._resolver.resolve(
                strategy_release_id,
                expected_rule_id=STRATEGY_RELEASE_V1_RULE_ID,
                expected_symbol=canonical_observation.symbol,
            )
        except StrategyReleaseResolutionError as exc:
            raise OrderIntentIneligibleError(f"strategy release resolution failed: {exc}") from exc
        if not isinstance(release, StrategyReleaseV1):
            raise OrderIntentIneligibleError("strict resolver returned an invalid strategy release")
        if release.strategy_release_id != strategy_release_id:
            raise OrderIntentIneligibleError("resolved strategy release identity does not match request")
        candidate = release.candidate
        if candidate.strategy_rule_id != STRATEGY_RELEASE_V1_RULE_ID:
            raise OrderIntentIneligibleError("strategy release rule does not match MU v1")
        if candidate.strategy_name != canonical_observation.strategy_name:
            raise OrderIntentIneligibleError("strategy release name does not match observation")
        observation_config_sha256 = canonical_payload_sha256(candidate.strategy_config.to_strategy_config())
        if observation_config_sha256 != canonical_observation.strategy_config_fingerprint:
            raise OrderIntentIneligibleError("strategy release config does not match Stage 0 config evidence")

        config_values = candidate.strategy_config.values
        wait_bars = config_values.get("second_pullback_wait_bars")
        if isinstance(wait_bars, bool) or not isinstance(wait_bars, int) or wait_bars <= 0:
            raise OrderIntentIneligibleError("strategy release has invalid scanner lifecycle policy")
        leverage = _integral_leverage(config_values.get("leverage"))
        scan = canonical_observation.scan_result
        assert scan is not None
        trigger_price = _positive_source_decimal(scan.trigger_price, "trigger_price")
        initial_stop = _positive_source_decimal(scan.initial_stop, "initial_stop")
        if scan.signal_time_ms is None:
            raise OrderIntentIneligibleError("eligible observation requires signal_time_ms")
        expires_at_ms = scan.signal_time_ms + wait_bars * _SCANNER_INTERVAL_MS
        if created_at_ms >= expires_at_ms:
            raise OrderIntentIneligibleError("source signal is expired at the exclusive scanner lifecycle boundary")

        try:
            limit_price = instrument_spec.price_to_string(trigger_price)
            initial_stop_text = instrument_spec.price_to_string(initial_stop)
            size = instrument_spec.size_for_notional(notional, price=limit_price)
        except ValueError as exc:
            raise OrderIntentIneligibleError(f"canonical instrument sizing failed: {exc}") from exc
        if Decimal(initial_stop_text) >= Decimal(limit_price):
            raise OrderIntentIneligibleError("initial_stop must be below the long-entry limit price")

        hashes = tuple(sorted(canonical_observation.content_sha256_by_interval.items()))
        signal_lineage_id = _signal_lineage_id(
            strategy_config_sha256=candidate.strategy_config_sha256,
            symbol=canonical_observation.symbol,
            signal_time_ms=scan.signal_time_ms,
        )
        environment = ExecutionEnvironment.DEMO
        business_action_id = _business_action_id(environment, signal_lineage_id)
        values: dict[str, Any] = {
            "schema_version": ORDER_INTENT_SCHEMA_VERSION,
            "signal_lineage_id": signal_lineage_id,
            "business_action_id": business_action_id,
            "audit_correlation_id": business_action_id,
            "environment": environment,
            "symbol": canonical_observation.symbol,
            "side": "buy",
            "order_type": "limit",
            "size": size,
            "limit_price": limit_price,
            "td_mode": "isolated",
            "pos_side": "long",
            "leverage": leverage,
            "initial_stop": initial_stop_text,
            "source_observation_id": canonical_observation.observation_id,
            "signal_time_ms": scan.signal_time_ms,
            "decision_code": canonical_observation.decision_code,
            "trusted_run_id": canonical_observation.trusted_run_id,
            "observed_at_ms": canonical_observation.observed_at_ms,
            "data_content_sha256": hashes,
            "strategy_release_id": release.strategy_release_id,
            "strategy_name": candidate.strategy_name,
            "strategy_config_sha256": candidate.strategy_config_sha256,
            "second_pullback_wait_bars": wait_bars,
            "created_at_ms": created_at_ms,
            "expires_at_ms": expires_at_ms,
        }
        fingerprint = canonical_sha256(_fingerprint_payload_from_values(values))
        return OrderIntent(intent_id=f"oi1_{fingerprint}", **values)


@unique
class IntentRevisionAction(str, Enum):
    CREATE = "create"
    REUSE = "reuse"
    SUPERSEDE = "supersede"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class IntentRevisionPlan:
    action: IntentRevisionAction
    intent: OrderIntent
    supersedes_intent_id: str | None = None


def classify_intent_revision(
    existing: OrderIntent | None,
    candidate: OrderIntent,
    *,
    mutation_reserved: bool,
) -> IntentRevisionPlan:
    if type(mutation_reserved) is not bool:
        raise TypeError("mutation_reserved must be a boolean")
    if not isinstance(candidate, OrderIntent):
        raise TypeError("candidate must be an OrderIntent")
    _validate_revision_intent(candidate, "candidate")
    if mutation_reserved and existing is None:
        raise OrderIntentRevisionError("mutation reservation cannot exist without an existing intent")
    if existing is None:
        return IntentRevisionPlan(IntentRevisionAction.CREATE, candidate)
    if not isinstance(existing, OrderIntent):
        raise TypeError("existing must be an OrderIntent or None")
    _validate_revision_intent(existing, "existing")
    if existing.environment is not candidate.environment:
        raise OrderIntentRevisionError("cross-environment intent revision is forbidden")
    if existing.business_action_id != candidate.business_action_id:
        raise OrderIntentRevisionError("intents do not belong to the same business action")
    if existing.intent_fingerprint == candidate.intent_fingerprint:
        return IntentRevisionPlan(IntentRevisionAction.REUSE, existing)
    if mutation_reserved:
        return IntentRevisionPlan(IntentRevisionAction.CONFLICT, existing)
    return IntentRevisionPlan(
        IntentRevisionAction.SUPERSEDE,
        candidate,
        supersedes_intent_id=existing.intent_id,
    )


def render_order_intent_review(intent: OrderIntent) -> str:
    if not isinstance(intent, OrderIntent):
        raise TypeError("review renderer requires a valid Demo OrderIntent")
    intent = OrderIntent.from_dict(intent.to_dict())
    hash_lines = "\n".join(
        f"- {interval}: {digest}" for interval, digest in intent.data_content_sha256
    )
    return (
        "# Demo OrderIntent human review\n\n"
        "WARNING: initial_stop is review provenance only and is NOT broker-side protection.\n\n"
        "## Identity\n"
        f"- intent_id: {intent.intent_id}\n"
        f"- signal_lineage_id: {intent.signal_lineage_id}\n"
        f"- business_action_id: {intent.business_action_id}\n"
        f"- audit_correlation_id: {intent.audit_correlation_id}\n"
        f"- environment: {intent.environment.value}\n\n"
        "## Exact order fields\n"
        f"- symbol: {intent.symbol}\n"
        f"- side: {intent.side}\n"
        f"- order_type: {intent.order_type}\n"
        f"- size: {intent.size}\n"
        f"- limit_price: {intent.limit_price}\n"
        f"- td_mode: {intent.td_mode}\n"
        f"- pos_side: {intent.pos_side}\n"
        f"- leverage: {intent.leverage}\n"
        f"- initial_stop: {intent.initial_stop}\n\n"
        "## Evidence\n"
        f"- source_observation_id: {intent.source_observation_id}\n"
        f"- decision_code: {intent.decision_code.value}\n"
        f"- signal_time_ms: {intent.signal_time_ms}\n"
        f"- observed_at_ms: {intent.observed_at_ms}\n"
        f"- trusted_run_id: {intent.trusted_run_id}\n"
        f"- strategy_release_id: {intent.strategy_release_id}\n"
        f"- strategy_name: {intent.strategy_name}\n"
        f"- strategy_config_sha256: {intent.strategy_config_sha256}\n"
        f"- second_pullback_wait_bars: {intent.second_pullback_wait_bars}\n"
        f"- expires_at_ms (exclusive): {intent.expires_at_ms}\n\n"
        "## Trusted data hashes\n"
        f"{hash_lines}\n"
    )


def _canonical_eligible_observation(observation: Stage0Observation) -> Stage0Observation:
    if not isinstance(observation, Stage0Observation):
        raise OrderIntentIneligibleError("source must be a Stage0Observation")
    try:
        canonical_observation = Stage0Observation.from_dict(observation.to_dict())
    except (ObservationSchemaError, TypeError, ValueError) as exc:
        raise OrderIntentIneligibleError(f"source observation is not canonical: {exc}") from exc
    if (
        canonical_observation.trust_policy_name != STAGE0_TRUST_POLICY_NAME
        or canonical_observation.trust_policy_version != STAGE0_TRUST_POLICY_VERSION
        or not canonical_observation.trust_allowed
    ):
        raise OrderIntentIneligibleError("source observation did not pass the strict trusted-data policy")
    if canonical_observation.outcome is not ObservationOutcome.READY_FOR_REVIEW:
        raise OrderIntentIneligibleError("source observation is not ready for review")
    if canonical_observation.failure_code is not None:
        raise OrderIntentIneligibleError("failed observation cannot create an intent")
    if canonical_observation.decision_code not in _ALLOWED_DECISIONS:
        raise OrderIntentIneligibleError("typed decision is not allowed for OrderIntent v1")
    if entry_decision_metadata(canonical_observation.decision_code).disposition is not EntryDisposition.READY:
        raise OrderIntentIneligibleError("typed decision does not derive READY")
    if canonical_observation.scan_result is None:
        raise OrderIntentIneligibleError("eligible observation requires a scan result")
    if canonical_observation.scan_result.symbol != canonical_observation.symbol:
        raise OrderIntentIneligibleError("scan result symbol does not match observation")
    if not canonical_observation.trusted_run_id or not _RUN_ID_PATTERN.fullmatch(
        canonical_observation.trusted_run_id
    ):
        raise OrderIntentIneligibleError("trusted evidence requires an exact generation run ID")
    hashes = canonical_observation.content_sha256_by_interval
    missing = ORDER_INTENT_REQUIRED_INTERVALS - set(hashes)
    if missing:
        raise OrderIntentIneligibleError(f"trusted evidence is missing required hashes: {sorted(missing)}")
    if set(canonical_observation.effective_intervals) - set(hashes):
        raise OrderIntentIneligibleError("trusted evidence does not cover every effective interval")
    for interval, digest in hashes.items():
        if not interval or not _SHA256_PATTERN.fullmatch(digest):
            raise OrderIntentIneligibleError("trusted evidence contains an invalid content hash")
    scan = canonical_observation.scan_result
    if scan.trigger_price is None or scan.initial_stop is None or scan.signal_time_ms is None:
        raise OrderIntentIneligibleError("eligible observation requires trigger, stop, and signal time")
    if scan.signal_time_ms > canonical_observation.observed_at_ms:
        raise OrderIntentIneligibleError("signal time cannot be after observation time")
    return canonical_observation


def _validate_revision_intent(intent: OrderIntent, label: str) -> None:
    try:
        OrderIntent.from_dict(intent.to_dict())
    except (OrderIntentSchemaError, TypeError, ValueError) as exc:
        raise OrderIntentRevisionError(f"{label} intent is invalid: {exc}") from exc


def _signal_lineage_id(*, strategy_config_sha256: str, symbol: str, signal_time_ms: int) -> str:
    return "sig1_" + canonical_sha256(
        {
            "strategy_rule_id": STRATEGY_RELEASE_V1_RULE_ID,
            "strategy_config_sha256": strategy_config_sha256,
            "symbol": symbol,
            "signal_time_ms": signal_time_ms,
        }
    )


def _business_action_id(environment: ExecutionEnvironment, signal_lineage_id: str) -> str:
    return "ba1_" + canonical_sha256(
        {
            "environment": environment.value,
            "action": "submit_entry",
            "signal_lineage_id": signal_lineage_id,
        }
    )


def _fingerprint_payload_from_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": values["schema_version"],
        "signal_lineage_id": values["signal_lineage_id"],
        "business_action_id": values["business_action_id"],
        "environment": values["environment"].value,
        "symbol": values["symbol"],
        "side": values["side"],
        "order_type": values["order_type"],
        "size": values["size"],
        "limit_price": values["limit_price"],
        "td_mode": values["td_mode"],
        "pos_side": values["pos_side"],
        "leverage": values["leverage"],
        "initial_stop": values["initial_stop"],
        "source_observation_id": values["source_observation_id"],
        "signal_time_ms": values["signal_time_ms"],
        "decision_code": values["decision_code"].value,
        "trusted_run_id": values["trusted_run_id"],
        "observed_at_ms": values["observed_at_ms"],
        "data_content_sha256": [list(item) for item in values["data_content_sha256"]],
        "strategy_release_id": values["strategy_release_id"],
        "strategy_name": values["strategy_name"],
        "strategy_config_sha256": values["strategy_config_sha256"],
        "second_pullback_wait_bars": values["second_pullback_wait_bars"],
        "expires_at_ms": values["expires_at_ms"],
    }


def _validate_content_hashes(value: Any) -> None:
    if not isinstance(value, tuple):
        raise OrderIntentSchemaError("data_content_sha256 must be an immutable tuple")
    if any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not isinstance(item[0], str)
        or not item[0]
        or not isinstance(item[1], str)
        or not _SHA256_PATTERN.fullmatch(item[1])
        for item in value
    ):
        raise OrderIntentSchemaError("data_content_sha256 contains an invalid interval/hash pair")
    if tuple(sorted(value)) != value or len({item[0] for item in value}) != len(value):
        raise OrderIntentSchemaError("data_content_sha256 must be uniquely sorted by interval")
    missing = ORDER_INTENT_REQUIRED_INTERVALS - {item[0] for item in value}
    if missing:
        raise OrderIntentSchemaError(f"data_content_sha256 is missing required intervals: {sorted(missing)}")


def _canonical_positive_decimal(value: Any, field_name: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise OrderIntentSchemaError(f"{field_name} must be a canonical decimal string")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        raise OrderIntentSchemaError(f"{field_name} must be a finite canonical decimal string") from exc
    if not decimal_value.is_finite():
        raise OrderIntentSchemaError(f"{field_name} must be finite")
    if decimal_value <= 0:
        raise OrderIntentSchemaError(f"{field_name} must be positive")
    canonical = "0" if decimal_value == 0 else format(decimal_value.normalize(), "f")
    if canonical != value:
        raise OrderIntentSchemaError(f"{field_name} must be canonical")
    return decimal_value


def _canonical_input_decimal(value: Any, field_name: str) -> Decimal:
    try:
        decimal_value = _canonical_positive_decimal(value, field_name)
    except OrderIntentSchemaError as exc:
        raise OrderIntentIneligibleError(str(exc)) from exc
    return decimal_value


def _positive_source_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise OrderIntentIneligibleError(f"{field_name} must be finite and positive")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OrderIntentIneligibleError(f"{field_name} must be finite and positive") from exc
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise OrderIntentIneligibleError(f"{field_name} must be finite and positive")
    return decimal_value


def _integral_leverage(value: Any) -> int:
    decimal_value = _positive_source_decimal(value, "leverage")
    integral = decimal_value.to_integral_value()
    if decimal_value != integral:
        raise OrderIntentIneligibleError("strategy release leverage must be an integer")
    return int(integral)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise OrderIntentSchemaError(f"{key} must be a non-empty string")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrderIntentSchemaError(f"{key} must be an integer")
    return value
