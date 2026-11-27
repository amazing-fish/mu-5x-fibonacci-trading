import ast
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "mu_strategy"
SCANNED_PACKAGES = ("core", "engine", "entry", "execution", "strategies")
FORBIDDEN_IMPORTS = (
    "mu_strategy.cli",
    "mu_strategy.commands",
    "mu_strategy.viz",
    "mu_strategy.live",
    "mu_strategy.demo_trading",
)
ENGINE_FORBIDDEN_IMPORTS = FORBIDDEN_IMPORTS + (
    "mu_strategy.notifications",
    "smtplib",
    "mu_strategy.market_data.service",
    "mu_strategy.market_data.trusted_data.refresh",
    "mu_strategy.backtest",
    "mu_strategy.experiments",
    "mu_strategy.lab",
    "mu_strategy.entry",
    "mu_strategy.execution",
)
STRATEGY_PROTOCOL_FORBIDDEN_IMPORTS = ENGINE_FORBIDDEN_IMPORTS + (
    "mu_strategy.engine",
)
LOW_LEVEL_DEPENDENCY_RULES = (
    (Path("models.py"), ("mu_strategy",)),
    (Path("strategy.py"), ("mu_strategy.entry", "mu_strategy.execution")),
)


class ArchitectureDependencyTests(unittest.TestCase):
    def test_scan_cycle_and_observation_writer_do_not_import_broker_or_application_adapters(self):
        violations = []
        for relative_path in ("scan_cycle.py", "stage0.py", "readonly_scan.py", "market_data/trusted_data/load.py"):
            for line, statement in _forbidden_import_statements(
                (PACKAGE_ROOT / relative_path).read_text(encoding="utf-8"),
                package="mu_strategy",
                forbidden_imports=FORBIDDEN_IMPORTS + (
                    "mu_strategy.market_data.service",
                    "mu_strategy.market_data.trusted_data.refresh",
                    "mu_strategy.notifications",
                    "smtplib",
                ),
            ):
                violations.append(f"{relative_path}:{line}: {statement}")
        self.assertEqual([], violations)

    def test_service_scan_cannot_reach_demo_broker_smtp_or_refresh_writer_indirectly(self):
        # A fresh interpreter catches transitive imports through a new wrapper.
        # Scope this guard to the service's real scan path, not the whole package.
        code = '''
import importlib.abc
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

class NoApplicationAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = ("mu_strategy.demo_trading", "mu_strategy.live", "mu_strategy.notifications",
                     "mu_strategy.market_data.service", "mu_strategy.market_data.trusted_data.refresh", "smtplib")
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            raise AssertionError("read-only scan imported " + fullname)

sys.meta_path.insert(0, NoApplicationAdapters())
from mu_strategy.signal_service import ServiceConfig, scan_once
from mu_strategy.service_health import StepStatus
with TemporaryDirectory() as directory, patch("socket.create_connection", side_effect=AssertionError("network")) as network:
    scan = scan_once(ServiceConfig(data_dir=Path(directory), symbols=("BTC", "ETH")))
    assert scan.status is StepStatus.SUCCEEDED, scan
    assert scan.persistence is StepStatus.SUCCEEDED, scan
    assert len(scan.cycle.observations) == 2, scan
    network.assert_not_called()
'''
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], _forbidden_import_statements(
            (PACKAGE_ROOT / "signal_service.py").read_text(encoding="utf-8"), package="mu_strategy",
            forbidden_imports=("mu_strategy.demo_trading", "mu_strategy.live", "mu_strategy.notifications", "smtplib"),
        ))

    def test_domain_packages_do_not_import_application_layers(self):
        violations: list[str] = []
        for package_name in SCANNED_PACKAGES:
            for path in sorted((PACKAGE_ROOT / package_name).rglob("*.py")):
                source = path.read_text(encoding="utf-8")
                package = ".".join(path.relative_to(REPO_ROOT).parts[:-1])
                for line_number, statement in _forbidden_import_statements(source, package=package):
                    relative_path = path.relative_to(REPO_ROOT).as_posix()
                    violations.append(f"{relative_path}:{line_number}: {statement}")

        self.assertEqual(
            [],
            violations,
            "Forbidden application-layer imports:\n" + "\n".join(violations),
        )

    def test_import_scanner_covers_import_and_import_from(self):
        source = "import mu_strategy.cli\nfrom mu_strategy import viz\nfrom .. import live\n"

        violations = _forbidden_import_statements(source, package="mu_strategy.entry")

        self.assertEqual(
            [
                (1, "import mu_strategy.cli"),
                (2, "from mu_strategy import viz"),
                (3, "from .. import live"),
            ],
            violations,
        )

    def test_shared_entry_contract_dependencies_remain_low_level(self):
        violations: list[str] = []
        for relative_path, forbidden_imports in LOW_LEVEL_DEPENDENCY_RULES:
            path = PACKAGE_ROOT / relative_path
            source = path.read_text(encoding="utf-8")
            for line_number, statement in _forbidden_import_statements(
                source,
                package="mu_strategy",
                forbidden_imports=forbidden_imports,
            ):
                violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line_number}: {statement}")

        for path in sorted((PACKAGE_ROOT / "core").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            package = ".".join(path.relative_to(REPO_ROOT).parts[:-1])
            for line_number, statement in _forbidden_import_statements(
                source,
                package=package,
                forbidden_imports=("mu_strategy.entry", "mu_strategy.execution"),
            ):
                violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line_number}: {statement}")

        self.assertEqual(
            [],
            violations,
            "Forbidden low-level entry-contract imports:\n" + "\n".join(violations),
        )

    def test_engine_and_strategy_protocol_layers_respect_layer_boundaries(self):
        violations: list[str] = []

        # Layer 3: mu_strategy/engine/
        engine_dir = PACKAGE_ROOT / "engine"
        self.assertTrue(engine_dir.is_dir(), "mu_strategy/engine package must exist")
        for path in sorted(engine_dir.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            package = ".".join(path.relative_to(REPO_ROOT).parts[:-1])
            for line_number, statement in _forbidden_import_statements(
                source,
                package=package,
                forbidden_imports=ENGINE_FORBIDDEN_IMPORTS,
            ):
                violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line_number}: {statement}")

        # Layer 2: protocol.py, features.py, and strategies/families/
        layer2_paths = [
            PACKAGE_ROOT / "strategies" / "protocol.py",
            PACKAGE_ROOT / "strategies" / "features.py",
        ]
        families_dir = PACKAGE_ROOT / "strategies" / "families"
        self.assertTrue(families_dir.is_dir(), "mu_strategy/strategies/families package must exist")
        layer2_paths.extend(sorted(families_dir.rglob("*.py")))

        for path in layer2_paths:
            self.assertTrue(path.is_file(), f"expected Layer 2 module {path}")
            source = path.read_text(encoding="utf-8")
            package = ".".join(path.relative_to(REPO_ROOT).parts[:-1])
            for line_number, statement in _forbidden_import_statements(
                source,
                package=package,
                forbidden_imports=STRATEGY_PROTOCOL_FORBIDDEN_IMPORTS,
            ):
                violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line_number}: {statement}")

        # Layer 1: core/instrument.py must stay pure domain (no engine/strategies/market_data/adapters)
        instrument_path = PACKAGE_ROOT / "core" / "instrument.py"
        if instrument_path.is_file():
            source = instrument_path.read_text(encoding="utf-8")
            for line_number, statement in _forbidden_import_statements(
                source,
                package="mu_strategy.core",
                forbidden_imports=STRATEGY_PROTOCOL_FORBIDDEN_IMPORTS + (
                    "mu_strategy.strategies",
                    "mu_strategy.market_data",
                ),
            ):
                violations.append(f"{instrument_path.relative_to(REPO_ROOT).as_posix()}:{line_number}: {statement}")

        self.assertEqual(
            [],
            violations,
            "Forbidden Layer 1/2/3 boundary imports:\n" + "\n".join(violations),
        )

    def test_engine_and_protocol_imports_cannot_reach_brokers_or_refresh_transitively(self):
        code = '''
import importlib.abc
import sys

class NoApplicationAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = (
            "mu_strategy.cli",
            "mu_strategy.commands",
            "mu_strategy.viz",
            "mu_strategy.live",
            "mu_strategy.demo_trading",
            "mu_strategy.notifications",
            "mu_strategy.market_data.service",
            "mu_strategy.market_data.trusted_data.refresh",
            "mu_strategy.lab",
            "smtplib",
        )
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            raise AssertionError("Layer 1-3 imported forbidden module " + fullname)

sys.meta_path.insert(0, NoApplicationAdapters())
import mu_strategy.core.instrument
import mu_strategy.market_data.funding
import mu_strategy.market_data.bar_series
import mu_strategy.strategies.protocol
import mu_strategy.strategies.features
import mu_strategy.strategies.families
import mu_strategy.engine
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_lower_layers_never_import_lab(self):
        violations: list[str] = []
        for package_name in ("core", "market_data", "strategies", "engine"):
            package_dir = PACKAGE_ROOT / package_name
            self.assertTrue(package_dir.is_dir(), f"mu_strategy/{package_name} package must exist")
            for path in sorted(package_dir.rglob("*.py")):
                source = path.read_text(encoding="utf-8")
                package = ".".join(path.relative_to(REPO_ROOT).parts[:-1])
                for line_number, statement in _forbidden_import_statements(
                    source,
                    package=package,
                    forbidden_imports=("mu_strategy.lab",),
                ):
                    violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line_number}: {statement}")

        self.assertEqual(
            [],
            violations,
            "Forbidden mu_strategy.lab imports in lower layers:\n" + "\n".join(violations),
        )


def _forbidden_import_statements(
    source: str,
    *,
    package: str,
    forbidden_imports: tuple[str, ...] = FORBIDDEN_IMPORTS,
) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if any(
            _is_forbidden_import(target, forbidden_imports=forbidden_imports)
            for target in _import_targets(node, package=package)
        ):
            statement = ast.get_source_segment(source, node) or ast.dump(node)
            violations.append((node.lineno, statement))
    return sorted(violations)


def _import_targets(node: ast.Import | ast.ImportFrom, *, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]

    if node.level:
        relative_name = "." * node.level + (node.module or "")
        module = importlib.util.resolve_name(relative_name, package)
    else:
        module = node.module or ""
    targets = [module] if module else []
    targets.extend(f"{module}.{alias.name}" for alias in node.names if module and alias.name != "*")
    return targets


def _is_forbidden_import(target: str, *, forbidden_imports: tuple[str, ...] = FORBIDDEN_IMPORTS) -> bool:
    return any(target == forbidden or target.startswith(f"{forbidden}.") for forbidden in forbidden_imports)


if __name__ == "__main__":
    unittest.main()
