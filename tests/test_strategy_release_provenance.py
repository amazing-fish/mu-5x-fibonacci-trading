import math
import unittest
from dataclasses import fields, replace

from mu_strategy.research.strategy_releases import (
    StrategyConfigPayloadV1,
    StrategyReleaseSchemaError,
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


if __name__ == "__main__":
    unittest.main()
