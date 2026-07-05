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
            "data/live/generations/new-run/tmp.json",
            "data/live/generations/new-run/okx/MU-USDT-SWAP/15m.tmp",
        ):
            with self.subTest(path=path):
                self.assertTrue(_is_git_ignored(path), path)


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


if __name__ == "__main__":
    unittest.main()
