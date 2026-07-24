import ast
import unittest
from pathlib import Path

from mu_strategy.strategies import registry
from mu_strategy.strategies.presets.mu import DEFAULT_MU_STRATEGY_NAMES


REPO_ROOT = Path(__file__).resolve().parents[1]

REGISTERED_NAMES = (
    "legacy_break_high",
    "baseline",
    "direct_next_open",
    "baseline_half_protect",
    "baseline_green_wide",
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

PUBLIC_FACTORIES = {
    "legacy_break_high": "legacy_break_high_strategy_group",
    "baseline": "baseline_strategy_group",
    "direct_next_open": "direct_next_open_strategy_group",
    "baseline_half_protect": "baseline_half_protect_strategy_group",
    "baseline_green_wide": "baseline_green_wide_strategy_group",
    "baseline_yellow_wide": "baseline_yellow_wide_strategy_group",
    "baseline_yellow_green_wide": "baseline_yellow_green_wide_strategy_group",
    "baseline_half_green_wide": "baseline_half_green_wide_strategy_group",
    "baseline_delayed_tighten": "baseline_delayed_tighten_strategy_group",
    "baseline_delayed_tighten_slow_start": "baseline_delayed_tighten_slow_start_strategy_group",
    "baseline_delayed_tighten_fast_start": "baseline_delayed_tighten_fast_start_strategy_group",
    "baseline_delayed_tighten_smooth": "baseline_delayed_tighten_smooth_strategy_group",
    "optimized_v2": "optimized_strategy_group",
}


class StrategyGroupRegistryTests(unittest.TestCase):
    def test_registration_catalog_owns_names_rules_construction_selection_and_defaults(self):
        registrations = registry.strategy_group_registrations()

        self.assertEqual(REGISTERED_NAMES, tuple(registration.name for registration in registrations))
        self.assertEqual(DEFAULT_NAMES, registry.default_strategy_names())
        self.assertEqual(DEFAULT_NAMES, DEFAULT_MU_STRATEGY_NAMES)
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
                public_factory = getattr(registry, PUBLIC_FACTORIES[registration.name])

                self.assertTrue(registration.selectable)
                self.assertEqual(registration.name, group.name)
                self.assertEqual(registration.descriptor, group.rule)
                self.assertEqual(group, public_factory("MU-USDT-SWAP"))
                self.assertEqual([group], selected)

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
            "second_pullback_limit_8",
            registry.second_pullback_strategy_group("MU-USDT-SWAP").name,
        )
        with self.assertRaisesRegex(ValueError, r"unknown strategy group\(s\): missing, also_missing"):
            registry.selected_strategy_groups(
                "MU-USDT-SWAP",
                ["missing,also_missing"],
            )

    def test_strategy_module_remains_a_shallow_compatibility_reexport(self):
        from mu_strategy import strategy

        self.assertIs(registry.StrategyGroup, strategy.StrategyGroup)
        self.assertIs(registry.default_strategy_groups, strategy.default_strategy_groups)
        self.assertIs(registry.selected_strategy_groups, strategy.selected_strategy_groups)

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
