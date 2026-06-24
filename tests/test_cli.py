import io
import json
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
                    "5m": _status("MU-USDT-SWAP", "5m", Path("data/live/5m.csv")),
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

    def test_cli_rejects_trusted_refresh_before_loading_or_backtesting(self):
        from mu_strategy import cli

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            report_path = Path(tmp) / "report.md"
            manifest_path = data_dir / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("canonical manifest", encoding="utf-8")
            manifest_before = manifest_path.read_bytes()
            argv = [
                "mu_strategy.cli",
                "--trusted-data",
                "--refresh",
                "--data-dir",
                str(data_dir),
                "--report",
                str(report_path),
            ]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.refresh_trusted_candle_bundle", side_effect=AssertionError("trusted loader")):
                    with patch("mu_strategy.cli.run_backtest", side_effect=AssertionError("backtest")):
                        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                            with self.assertRaises(SystemExit) as raised:
                                cli.main()

            self.assertNotEqual(0, raised.exception.code)
            self.assertIn("refresh_market_data", stderr.getvalue())
            self.assertEqual(manifest_before, manifest_path.read_bytes())
            self.assertFalse(report_path.exists())

    def test_cli_trusted_data_rejects_invalid_base_5m_status(self):
        from mu_strategy import cli

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            return SimpleNamespace(
                candles_by_interval={"15m": [_candle(0, 100)], "1h": [_candle(0, 100)]},
                files_by_interval={"15m": Path("data/live/15m.csv"), "1h": Path("data/live/1h.csv")},
                statuses_by_interval={
                    "5m": _status(
                        "MU-USDT-SWAP",
                        "5m",
                        Path("data/live/5m.csv"),
                        is_valid=False,
                        reason="cache_read_failed",
                    ),
                    "15m": _status("MU-USDT-SWAP", "15m", Path("data/live/15m.csv")),
                    "1h": _status("MU-USDT-SWAP", "1h", Path("data/live/1h.csv")),
                },
            )

        with TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.md"
            argv = ["mu_strategy.cli", "--trusted-data", "--report", str(report_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            cli.main()

        self.assertNotEqual(0, raised.exception.code)

    def test_cli_trusted_data_uses_trust_decision_gate(self):
        from mu_strategy import cli
        from mu_strategy.market_data.trusted_data.contracts import HealthReason, TrustDecision

        def fake_refresh_trusted_candle_bundle(symbol, **kwargs):
            return SimpleNamespace(
                candles_by_interval={"15m": [_candle(0, 100)], "1h": [_candle(0, 100)]},
                files_by_interval={"15m": Path("data/live/15m.csv"), "1h": Path("data/live/1h.csv")},
                statuses_by_interval={
                    "5m": _status("MU-USDT-SWAP", "5m", Path("data/live/5m.csv")),
                    "15m": _status("MU-USDT-SWAP", "15m", Path("data/live/15m.csv")),
                    "1h": _status("MU-USDT-SWAP", "1h", Path("data/live/1h.csv")),
                },
                trust_decision=TrustDecision(False, HealthReason.MALFORMED_MANIFEST),
            )

        with TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.md"
            argv = ["mu_strategy.cli", "--trusted-data", "--report", str(report_path)]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.refresh_trusted_candle_bundle", side_effect=fake_refresh_trusted_candle_bundle):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            cli.main()

        self.assertNotEqual(0, raised.exception.code)

    def test_cli_trusted_data_rejects_failed_manifest_with_valid_csv(self):
        from mu_strategy import cli

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            report_path = Path(tmp) / "report.md"
            _write_failed_manifest_with_valid_csv(data_dir)
            argv = [
                "mu_strategy.cli",
                "--trusted-data",
                "--data-dir",
                str(data_dir),
                "--report",
                str(report_path),
            ]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.run_backtest", return_value=BacktestResult(10_000, 10_000, [], [])):
                    with patch("mu_strategy.cli.render_markdown_report", return_value="# report"):
                        with patch("sys.stderr", new_callable=io.StringIO):
                            with self.assertRaises(SystemExit) as raised:
                                cli.main()

            self.assertFalse(report_path.exists())

        self.assertNotEqual(0, raised.exception.code)

    def test_cli_trusted_data_rejects_unpublished_symbol_before_backtest(self):
        from mu_strategy import cli

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            report_path = Path(tmp) / "report.md"
            _write_manifest_with_valid_csv(data_dir, symbol="BTC-USDT-SWAP", outcome="success", status="ok")
            _write_csv_only(data_dir, symbol="MU-USDT-SWAP")
            argv = [
                "mu_strategy.cli",
                "--trusted-data",
                "--data-dir",
                str(data_dir),
                "--report",
                str(report_path),
            ]
            with patch("sys.argv", argv):
                with patch("mu_strategy.cli.run_backtest", side_effect=AssertionError("backtest must not run")):
                    with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        with self.assertRaises(SystemExit) as raised:
                            cli.main()

            self.assertFalse(report_path.exists())

        self.assertNotEqual(0, raised.exception.code)
        self.assertIn("not_published", stderr.getvalue())

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


def _status(symbol: str, interval: str, path: Path, *, is_valid: bool = True, is_stale: bool = False, reason: str = "ok"):
    from mu_strategy.market_data.trusted import DataStatus

    return DataStatus(
        symbol=symbol,
        interval=interval,
        rows=1,
        first_timestamp_ms=0,
        last_timestamp_ms=0,
        updated_at_ms=0,
        source_file=path,
        is_valid=is_valid,
        is_stale=is_stale,
        reason=reason,
    )


def _write_failed_manifest_with_valid_csv(data_dir: Path) -> None:
    _write_manifest_with_valid_csv(data_dir, symbol="MU-USDT-SWAP", outcome="failed", status="invalid")


def _write_manifest_with_valid_csv(data_dir: Path, *, symbol: str, outcome: str, status: str) -> None:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles
    from mu_strategy.market_data.utils import DAY_MS

    store = TrustedDataStore(data_dir=data_dir)
    five = [_candle(index * 300_000, 100 + index) for index in range(DAY_MS // 300_000)]
    by_interval = {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }
    symbols = {symbol: {"intervals": {}}}
    for interval, candles in by_interval.items():
        path = store.cache_path(symbol, interval)
        store.write_csv(candles, path)
        symbols[symbol]["intervals"][interval] = {
            "symbol": symbol,
            "interval": interval,
            "availability": "available",
            "integrity": "valid",
            "freshness": "fresh",
            "reasons": ["ok"],
            "rows": len(candles),
            "first_timestamp_ms": candles[0].open_time_ms,
            "last_timestamp_ms": candles[-1].open_time_ms,
            "updated_at_ms": 86_400_000,
            "source_file": str(path),
            "content_sha256": candles_content_sha256(candles),
            "validation": {"ok": True, "reason": "ok"},
        }
    store.write_manifest(
        {
            "schema_version": 2,
            "run_id": f"{outcome}-run",
            "outcome": outcome,
            "status": status,
            "started_at_ms": 0,
            "completed_at_ms": 86_400_000,
            "requested_intervals": ["5m", "15m", "1h"],
            "effective_intervals": ["5m", "15m", "1h"],
            "universes": {"crypto_top": [], "stock_token_top": []},
            "symbols": symbols,
            "warnings": [],
            "cycle_error": {"error_type": "TimeoutError", "message": "blocked"},
        }
    )


def _write_csv_only(data_dir: Path, *, symbol: str) -> None:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles
    from mu_strategy.market_data.utils import DAY_MS

    store = TrustedDataStore(data_dir=data_dir)
    five = [_candle(index * 300_000, 100 + index) for index in range(DAY_MS // 300_000)]
    by_interval = {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }
    for interval, candles in by_interval.items():
        store.write_csv(candles, store.cache_path(symbol, interval))


if __name__ == "__main__":
    unittest.main()
