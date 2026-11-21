import ast
import unittest
from dataclasses import replace
from pathlib import Path

from mu_strategy.strategies import registry


REPO_ROOT = Path(__file__).resolve().parents[1]

REGISTERED_NAMES = (
    "legacy_break_high",
    "baseline",
    "direct_next_open",
    "baseline_half_protect",
    "baseline_green_wide",
    "baseline_green_only_wide",
    "baseline_yellow_wide",
    "baseline_yellow_green_wide",
    "baseline_half_green_wide",
    "baseline_delayed_tighten",
    "baseline_delayed_tighten_slow_start",
    "baseline_delayed_tighten_fast_start",
    "baseline_delayed_tighten_smooth",
    "optimized_v2",
)

DEFAULT_NAMES = (
    "legacy_break_high",
    "baseline",
    "direct_next_open",
    "baseline_half_protect",
    "baseline_green_wide",
    "baseline_yellow_wide",
    "baseline_yellow_green_wide",
    "baseline_half_green_wide",
    "baseline_delayed_tighten_fast_start",
    "optimized_v2",
)


class StrategyGroupRegistryTests(unittest.TestCase):
    def test_green_only_wide_is_opt_in_and_matches_the_measured_configuration(self):
        from mu_strategy.models import Candle, EntryDecisionCode
        from mu_strategy.strategy import should_enter_long

        group = registry.selected_strategy_groups("MU-USDT-SWAP", ["baseline_green_only_wide"])[0]
        reference = registry.strategy_group("baseline_green_wide", "MU-USDT-SWAP")
        self.assertEqual(replace(reference.config, allowed_regimes=("green",)), group.config)
        self.assertNotIn(group.name, registry.default_strategy_names())
        candle = Candle(1, 101, 103, 98.8, 100.8, 1000)
        self.assertTrue(should_enter_long(candle, 100, "green", 51, 0.2, 0.1, group.config).allowed)
        self.assertEqual(
            EntryDecisionCode.REGIME_BLOCKED,
            should_enter_long(candle, 100, "yellow", 51, 0.2, 0.1, group.config).decision_code,
        )

    def test_registration_catalog_owns_names_rules_construction_selection_and_defaults(self):
        registrations = registry.strategy_group_registrations()

        self.assertEqual(REGISTERED_NAMES, tuple(registration.name for registration in registrations))
        self.assertEqual(DEFAULT_NAMES, registry.default_strategy_names())
        self.assertEqual(
            DEFAULT_NAMES,
            tuple(group.name for group in registry.default_strategy_groups("MU-USDT-SWAP")),
        )

        for registration in registrations:
            with self.subTest(strategy=registration.name):
                group = registration.build("MU-USDT-SWAP")
                selected = registry.selected_strategy_groups(
                    "MU-USDT-SWAP",
                    [registration.name],
                )
                built = registry.strategy_group(registration.name, "MU-USDT-SWAP")

                self.assertTrue(registration.selectable)
                self.assertEqual(registration.name, group.name)
                self.assertEqual(registration.descriptor, group.rule)
                self.assertEqual(group, built)
                self.assertEqual([group], selected)

        self.assertEqual(
            registry.strategy_group("baseline", "MU-USDT-SWAP"),
            registry.baseline_strategy_group("MU-USDT-SWAP"),
        )

    def test_rule_descriptors_report_honest_order_types_and_validate_values(self):
        self.assertEqual("limit", registry.strategy_rule_descriptor("baseline").order_type)
        self.assertEqual("stop", registry.strategy_rule_descriptor("legacy_break_high").order_type)
        self.assertEqual("stop", registry.strategy_rule_descriptor("optimized_v2").order_type)
        self.assertEqual("market", registry.strategy_rule_descriptor("direct_next_open").order_type)

        with self.assertRaises(ValueError):
            registry.StrategyRuleDescriptor("mu.bad.long.v1", "bad", 1, "buy", "post_only")
        with self.assertRaises(ValueError):
            registry.StrategyRuleDescriptor("mu.bad.short.v1", "bad", 1, "sell", "limit")

    def test_non_default_delayed_variants_are_intentionally_selectable(self):
        names = (
            "baseline_delayed_tighten",
            "baseline_delayed_tighten_slow_start",
            "baseline_delayed_tighten_smooth",
        )

        groups = registry.selected_strategy_groups("MU-USDT-SWAP", list(names))

        self.assertEqual(names, tuple(group.name for group in groups))
        self.assertEqual(
            ("linear", "slow_start", "smooth"),
            tuple(group.config.stop_transition_curve for group in groups),
        )
        self.assertTrue(all(group.config.stop_tightening == "delayed_baseline" for group in groups))
        self.assertTrue(set(names).isdisjoint(registry.default_strategy_names()))

    def test_alias_and_unknown_name_behavior_are_derived_from_the_catalog(self):
        baseline = next(
            registration
            for registration in registry.strategy_group_registrations()
            if registration.name == "baseline"
        )

        self.assertEqual(("second_pullback_limit_8",), baseline.aliases)
        self.assertEqual(
            registry.strategy_rule_descriptor("baseline"),
            registry.strategy_rule_descriptor("second_pullback_limit_8"),
        )
        self.assertEqual(
            ["baseline"],
            [
                group.name
                for group in registry.selected_strategy_groups(
                    "MU-USDT-SWAP",
                    ["second_pullback_limit_8"],
                )
            ],
        )
        self.assertEqual(
            registry.baseline_strategy_group("MU-USDT-SWAP"),
            registry.strategy_group("second_pullback_limit_8", "MU-USDT-SWAP"),
        )
        with self.assertRaisesRegex(ValueError, r"unknown strategy group\(s\): missing, also_missing"):
            registry.selected_strategy_groups(
                "MU-USDT-SWAP",
                ["missing,also_missing"],
            )
        with self.assertRaisesRegex(ValueError, r"unknown strategy group: missing"):
            registry.strategy_group("missing", "MU-USDT-SWAP")

    def test_strategy_module_has_no_circular_import_of_registry(self):
        source = (REPO_ROOT / "mu_strategy" / "strategy.py").read_text(encoding="utf-8")
        imported_modules = {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("mu_strategy.strategies.registry", imported_modules)

    def test_cli_imports_strategy_selection_from_the_owning_registry(self):
        source = (REPO_ROOT / "mu_strategy" / "cli.py").read_text(encoding="utf-8")
        imports = {
            node.module: {alias.name for alias in node.names}
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        }

        self.assertIn(
            "selected_strategy_groups",
            imports.get("mu_strategy.strategies.registry", set()),
        )
        self.assertNotIn(
            "selected_strategy_groups",
            imports.get("mu_strategy.strategy", set()),
        )


if __name__ == "__main__":
    unittest.main()
