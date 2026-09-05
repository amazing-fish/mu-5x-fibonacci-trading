import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class RepositoryArtifactPolicyTests(unittest.TestCase):
    def test_trusted_generation_baseline_files_are_visible_to_git(self):
        for path in (
            "data/live/generations/new-run/manifest.json",
            "data/live/generations/new-run/okx/MU-USDT-SWAP/15m.csv",
            "data/live/generations/new-run/okx/MU-USDT-SWAP/1h.csv",
            "data/live/generations/new-run/okx/MU-USDT-SWAP/5m.csv",
        ):
            with self.subTest(path=path):
                self.assertFalse(_is_git_ignored(path), path)

    def test_trusted_live_runtime_artifacts_remain_ignored(self):
        for path in (
            "data/live/refresh_runs.jsonl",
            "data/live-signal-service/health.json",
            "data/live-signal-service/observations.jsonl",
            "data/live-signal-service/service.lock",
            "data/live-signal-service/supervisor.lock",
            "data/live-signal-service/email.sqlite3",
            "data/live-signal-service/email.sqlite3-journal",
            "data/live/generations/new-run/tmp.json",
            "data/live/generations/new-run/okx/MU-USDT-SWAP/15m.tmp",
        ):
            with self.subTest(path=path):
                self.assertTrue(_is_git_ignored(path), path)

    def test_documented_demo_dry_run_keeps_mu_watchlist_coverage(self):
        for path in (REPO_ROOT / "README.md", REPO_ROOT / "SKILL.md"):
            for command in _documented_okx_demo_commands(path):
                with self.subTest(path=path.name, command=command):
                    disables_default_watchlist = "--no-default-watchlist" in command
                    pins_mu_watchlist = "--watchlist-symbol MU-USDT-SWAP" in command
                    self.assertFalse(disables_default_watchlist and not pins_mu_watchlist, command)

def _is_git_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", path],
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise AssertionError(f"git check-ignore failed for {path!r} with exit {result.returncode}")


def _documented_okx_demo_commands(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if "python -m mu_strategy.commands.okx_demo_loop" in line
    ]


if __name__ == "__main__":
    unittest.main()
