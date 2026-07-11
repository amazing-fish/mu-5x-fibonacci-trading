import ast
import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "mu_strategy"
SCANNED_PACKAGES = ("core", "entry", "execution", "strategies")
FORBIDDEN_IMPORTS = (
    "mu_strategy.cli",
    "mu_strategy.commands",
    "mu_strategy.viz",
    "mu_strategy.live",
    "mu_strategy.demo_trading",
)


class ArchitectureDependencyTests(unittest.TestCase):
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


def _forbidden_import_statements(source: str, *, package: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if any(_is_forbidden_import(target) for target in _import_targets(node, package=package)):
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


def _is_forbidden_import(target: str) -> bool:
    return any(target == forbidden or target.startswith(f"{forbidden}.") for forbidden in FORBIDDEN_IMPORTS)


if __name__ == "__main__":
    unittest.main()
