import unittest
from dataclasses import replace

from mu_strategy.research.historical_data import TrustedExperimentDatasetV1
from mu_strategy.research.strategy_releases import (
    StrategyConfigPayloadV1,
    StrategyConfigPayloadV2,
    StrategyReleaseSchemaError,
    parse_strategy_config_payload,
)
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.strategy import StrategyConfig


class StrategyConfigPayloadTests(unittest.TestCase):
    def test_v2_round_trips_strategy_config_and_changes_digest_on_edit(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        payload = StrategyConfigPayloadV2.from_config(config)
        restored = parse_strategy_config_payload(payload.to_dict())

        self.assertIsInstance(restored, StrategyConfigPayloadV2)
        self.assertEqual(payload, restored)
        self.assertEqual(config, restored.to_strategy_config())

        modified = StrategyConfigPayloadV2.from_config(replace(config, fib_lookback=config.fib_lookback + 1))
        self.assertNotEqual(payload.strategy_config_sha256, modified.strategy_config_sha256)

    def test_v2_rejects_unknown_missing_and_non_canonical_fields(self):
        wire = StrategyConfigPayloadV2.from_config(StrategyConfig()).to_dict()

        unknown = {**wire, "fields": {**wire["fields"], "unexpected": "x"}}
        with self.assertRaisesRegex(StrategyReleaseSchemaError, "unknown"):
            StrategyConfigPayloadV2.from_dict(unknown)

        missing_fields = dict(wire["fields"])
        missing_fields.pop("fib_lookback")
        with self.assertRaisesRegex(StrategyReleaseSchemaError, "missing"):
            StrategyConfigPayloadV2.from_dict({**wire, "fields": missing_fields})

        noncanonical = {**wire, "fields": {**wire["fields"], "leverage": "5.00"}}
        with self.assertRaisesRegex(StrategyReleaseSchemaError, "canonical decimal"):
            StrategyConfigPayloadV2.from_dict(noncanonical)

        for bad_version in (True, 0, 3, "2"):
            with self.assertRaises(StrategyReleaseSchemaError):
                parse_strategy_config_payload({**wire, "schema_version": bad_version})

    def test_v1_rejects_non_legacy_calendar_and_round_trips_legacy(self):
        legacy = StrategyConfig(symbol="MU-USDT-SWAP", entry_execution="second_pullback", fib_lookback=8)
        payload = StrategyConfigPayloadV1.from_config(legacy)
        self.assertEqual(payload, parse_strategy_config_payload(payload.to_dict()))
        self.assertEqual(legacy.symbol, payload.to_strategy_config().symbol)

        modern = baseline_strategy_group("MU-USDT-SWAP").config
        with self.assertRaises(StrategyReleaseSchemaError):
            StrategyConfigPayloadV1.from_config(modern)

    def test_v2_decodes_pre_risk_limit_payloads_and_preserves_original_digest(self):
        config = baseline_strategy_group("MU-USDT-SWAP").config
        payload = StrategyConfigPayloadV2.from_config(config)
        wire = payload.to_dict()
        # Default StrategyConfig omits unset optional risk limits to preserve base V2 hash identity.
        self.assertNotIn("max_account_drawdown_pct", wire["fields"])
        self.assertNotIn("daily_loss_limit_pct", wire["fields"])

        restored = parse_strategy_config_payload(wire)
        self.assertEqual(payload.strategy_config_sha256, restored.strategy_config_sha256)
        self.assertIsNone(restored.to_strategy_config().max_account_drawdown_pct)
        self.assertIsNone(restored.to_strategy_config().daily_loss_limit_pct)

        # Explicit null risk-limit fields are also accepted and preserve their exact wire representation.
        explicit_null_wire = {
            **wire,
            "fields": {**wire["fields"], "max_account_drawdown_pct": None, "daily_loss_limit_pct": None},
        }
        restored_nulls = parse_strategy_config_payload(explicit_null_wire)
        self.assertEqual(explicit_null_wire, restored_nulls.to_dict())
        self.assertEqual(config, restored_nulls.to_strategy_config())

        # Non-null risk limits are serialized, change digest, and round-trip cleanly.
        bounded = replace(config, max_account_drawdown_pct=0.15, daily_loss_limit_pct=0.05)
        bounded_payload = StrategyConfigPayloadV2.from_config(bounded)
        self.assertEqual("0.15", bounded_payload.to_dict()["fields"]["max_account_drawdown_pct"])
        self.assertEqual("0.05", bounded_payload.to_dict()["fields"]["daily_loss_limit_pct"])
        self.assertNotEqual(payload.strategy_config_sha256, bounded_payload.strategy_config_sha256)
        self.assertEqual(bounded, parse_strategy_config_payload(bounded_payload.to_dict()).to_strategy_config())

    def test_v1_rejects_risk_limits_and_non_default_bjt_windows(self):
        for risk_kwargs in ({"daily_loss_limit_pct": 0.05}, {"max_account_drawdown_pct": 0.15}):
            with self.subTest(risk_kwargs=risk_kwargs):
                with self.assertRaisesRegex(StrategyReleaseSchemaError, "V2"):
                    StrategyConfigPayloadV1.from_config(StrategyConfig(**risk_kwargs))

        legacy_wire = StrategyConfigPayloadV1.from_config(StrategyConfig()).to_dict()
        custom_bjt = {
            **legacy_wire,
            "fields": {**legacy_wire["fields"], "trading_windows_bjt": [["22:00", "23:00"]]},
        }
        with self.assertRaisesRegex(StrategyReleaseSchemaError, "trading_windows_bjt"):
            parse_strategy_config_payload(custom_bjt)

    def test_trusted_experiment_dataset_validation(self):
        digest = "a" * 64
        dataset = TrustedExperimentDatasetV1(
            run_id="b" * 32,
            symbol="MU-USDT-SWAP",
            manifest_schema_version=4,
            requested_intervals=("15m", "1h"),
            effective_intervals=("5m", "15m", "1h"),
            content_sha256_by_interval=(("15m", digest), ("1h", digest), ("5m", digest)),
        )
        self.assertEqual("MU-USDT-SWAP", dataset.to_dict()["symbol"])
        with self.assertRaises(ValueError):
            TrustedExperimentDatasetV1(
                run_id="invalid",
                symbol="MU-USDT-SWAP",
                manifest_schema_version=4,
                requested_intervals=("15m",),
                effective_intervals=("5m", "15m"),
                content_sha256_by_interval=(("15m", digest), ("5m", digest)),
            )


if __name__ == "__main__":
    unittest.main()
