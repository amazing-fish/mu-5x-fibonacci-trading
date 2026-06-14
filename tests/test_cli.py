import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import BacktestResult


class CliTests(unittest.TestCase):
    def test_cli_defaults_to_okx_mu_source_and_accepts_strategy(self):
        from mu_strategy import cli

        cached_calls = []

        def fake_cached_historical(symbol, interval, **kwargs):
            cached_calls.append((symbol, interval, kwargs))
            return [], Path(f"data/{symbol}_{interval}.csv")

        with TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.md"
            argv = ["mu_strategy.cli", "--days", "180", "--strategy", "baseline", "--report", str(report_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.cached_historical", side_effect=fake_cached_historical):
                    with patch("mu_strategy.cli.run_backtest", return_value=BacktestResult(10_000, 10_000, [], [])):
                        with patch("mu_strategy.cli.render_markdown_report", return_value="# report"):
                            with patch("sys.stdout", new_callable=io.StringIO):
                                cli.main()

        self.assertEqual("MU-USDT-SWAP", cached_calls[0][0])
        self.assertEqual("okx", cached_calls[0][2]["source"])
        self.assertEqual(180, cached_calls[0][2]["days"])


if __name__ == "__main__":
    unittest.main()
