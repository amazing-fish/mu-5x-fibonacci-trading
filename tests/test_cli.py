import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mu_strategy.models import BacktestResult
from mu_strategy.models import Candle


class CliTests(unittest.TestCase):
    def test_fee_profile_help_renders_literal_percent_signs(self):
        from mu_strategy import cli
        from mu_strategy.commands import all_existing_data_backtests
        from mu_strategy.experiments import walk_forward
        from mu_strategy.viz import backtest as viz_backtest

        for module, program_name in (
            (cli, "mu_strategy.cli"),
            (viz_backtest, "mu_strategy.visualize"),
            (walk_forward, "mu_strategy.walk_forward"),
            (all_existing_data_backtests, "mu_strategy.all_existing_data_backtests"),
        ):
            with self.subTest(program_name=program_name):
                stdout = io.StringIO()
                with patch("sys.argv", [program_name, "--help"]):
                    with patch("sys.stdout", stdout):
                        with self.assertRaises(SystemExit) as exc:
                            module.main()

                self.assertEqual(0, exc.exception.code)
                self.assertIn("0.0500%", stdout.getvalue())
                self.assertIn("0.0200%", stdout.getvalue())

    def test_cli_defaults_to_okx_mu_source_and_accepts_strategy(self):
        from mu_strategy import cli

        bundle_calls = []
        configs = []

        def fake_refresh_candle_bundle(symbol, **kwargs):
            bundle_calls.append((symbol, kwargs))
            return SimpleNamespace(
                candles_by_interval={"15m": [], "1h": []},
                files_by_interval={"15m": Path("data/15m.csv"), "1h": Path("data/1h.csv")},
            )

        def fake_run_backtest(candles_15m, context, *, config):
            configs.append(config)
            return BacktestResult(10_000, 10_000, [], [])

        with TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.md"
            argv = ["mu_strategy.cli", "--days", "180", "--strategy", "baseline", "--report", str(report_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.refresh_candle_bundle", side_effect=fake_refresh_candle_bundle):
                    with patch("mu_strategy.cli.cached_historical", side_effect=AssertionError("CLI must use market_data.service"), create=True):
                        with patch("mu_strategy.cli.run_backtest", side_effect=fake_run_backtest):
                            with patch("mu_strategy.cli.render_markdown_report", return_value="# report"):
                                with patch("sys.stdout", new_callable=io.StringIO):
                                    cli.main()

        self.assertEqual("MU-USDT-SWAP", bundle_calls[0][0])
        self.assertEqual(("15m", "1h"), bundle_calls[0][1]["intervals"])
        self.assertEqual("okx", bundle_calls[0][1]["source"])
        self.assertEqual(180, bundle_calls[0][1]["days"])
        self.assertFalse(bundle_calls[0][1]["refresh"])
        self.assertEqual("market", configs[0].fee_profile)
        self.assertAlmostEqual(0.0005, configs[0].fee_rate)

    def test_cli_trusted_data_uses_trusted_bundle_and_status_gate(self):
        from mu_strategy import cli

        trusted_calls = []
        configs = []

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            trusted_calls.append((symbol, kwargs))
            return SimpleNamespace(
                candles_by_interval={"15m": [_candle(0, 100)], "1h": [_candle(0, 100)]},
                files_by_interval={"15m": Path("data/live/15m.csv"), "1h": Path("data/live/1h.csv")},
                statuses_by_interval={
                    "15m": _status("MU-USDT-SWAP", "15m", Path("data/live/15m.csv")),
                    "1h": _status("MU-USDT-SWAP", "1h", Path("data/live/1h.csv")),
                },
            )

        def fake_run_backtest(candles_15m, context, *, config):
            configs.append(config)
            return BacktestResult(10_000, 10_000, [], [])

        with TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.md"
            argv = ["mu_strategy.cli", "--trusted-data", "--report", str(report_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle):
                    with patch("mu_strategy.cli.run_backtest", side_effect=fake_run_backtest):
                        with patch("mu_strategy.cli.render_markdown_report", return_value="# report"):
                            with patch("sys.stdout", new_callable=io.StringIO):
                                cli.main()

        self.assertEqual("MU-USDT-SWAP", trusted_calls[0][0])
        self.assertEqual(Path("data/live"), trusted_calls[0][1]["data_dir"])
        self.assertFalse(trusted_calls[0][1]["refresh"])
        self.assertEqual("market", configs[0].fee_profile)

    def test_cli_accepts_limit_fee_profile_for_cost_sensitivity(self):
        from mu_strategy import cli

        configs = []

        def fake_refresh_candle_bundle(symbol, **kwargs):
            return SimpleNamespace(
                candles_by_interval={"15m": [], "1h": []},
                files_by_interval={"15m": Path("data/15m.csv"), "1h": Path("data/1h.csv")},
            )

        def fake_run_backtest(candles_15m, context, *, config):
            configs.append(config)
            return BacktestResult(10_000, 10_000, [], [])

        with TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.md"
            argv = [
                "mu_strategy.cli",
                "--days",
                "180",
                "--strategy",
                "baseline",
                "--fee-profile",
                "limit",
                "--report",
                str(report_path),
            ]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.refresh_candle_bundle", side_effect=fake_refresh_candle_bundle):
                    with patch("mu_strategy.cli.run_backtest", side_effect=fake_run_backtest):
                        with patch("mu_strategy.cli.render_markdown_report", return_value="# report"):
                            with patch("sys.stdout", new_callable=io.StringIO):
                                cli.main()

        self.assertEqual("limit", configs[0].fee_profile)
        self.assertAlmostEqual(0.0002, configs[0].fee_rate)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


def _status(symbol: str, interval: str, path: Path):
    from mu_strategy.market_data.trusted import DataStatus

    return DataStatus(
        symbol=symbol,
        interval=interval,
        rows=1,
        first_timestamp_ms=0,
        last_timestamp_ms=0,
        updated_at_ms=0,
        source_file=path,
    )


if __name__ == "__main__":
    unittest.main()
