import importlib
import unittest


class ArchitecturePackageTests(unittest.TestCase):
    def test_domain_packages_are_importable(self):
        for name in [
            "mu_strategy.market_data",
            "mu_strategy.market_data.providers",
            "mu_strategy.core",
            "mu_strategy.strategies",
            "mu_strategy.research",
            "mu_strategy.experiments",
            "mu_strategy.selection",
            "mu_strategy.execution",
            "mu_strategy.viz",
            "mu_strategy.commands",
        ]:
            importlib.import_module(name)
