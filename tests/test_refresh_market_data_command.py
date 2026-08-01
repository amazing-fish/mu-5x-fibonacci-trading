from contextlib import redirect_stderr
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle
from tests.factories.trusted_publication import manifest_path, range_candles


class RefreshMarketDataCommandTests(unittest.TestCase):
    def test_success_ok_one_shot_returns_zero_with_structured_output(self):
        from mu_strategy.commands.refresh_market_data import main

        captured = {}
        run = _run("run-ok")

        def fake_refresh(args, *, intervals):
            captured["args"] = args
            captured["intervals"] = intervals
            return run

        with TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            html_path = Path(tmp) / "health.html"
            with patch("mu_strategy.commands.refresh_market_data._refresh_once", side_effect=fake_refresh):
                with patch("mu_strategy.commands.refresh_market_data.write_data_health_dashboard") as write_dashboard:
                    exit_code = main(["--interval", "5m", "--html-output", str(html_path)], stdout=stdout)

        self.assertEqual(0, exit_code)
        self.assertEqual(("5m",), captured["intervals"])
        self.assertEqual(2, captured["args"].max_concurrency)
        self.assertEqual(
            {
                "run_id": "run-ok",
                "attempt_status": "success",
                "snapshot_usability": "usable",
                "usable": True,
                "symbols": 1,
                "fetch_mode": "top_universe",
            },
            json.loads(stdout.getvalue()),
        )
        write_dashboard.assert_called_once()

    def test_explicit_symbol_subset_skips_universe_fetch_and_reports_mode(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.trusted_data.validation import aggregate_candles

        fetch_calls = []

        def fetch_history(symbol: str, interval: str, *, days: int):
            fetch_calls.append((symbol, interval, days))
            five = range_candles(0, 86_100_000)
            if interval == "5m":
                return five
            return aggregate_candles(five, interval=interval)

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            html_path = Path(tmp) / "health.html"
            stdout = io.StringIO()
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers",
                side_effect=AssertionError("explicit refresh must not fetch ticker universe"),
            ) as fetch_tickers:
                with patch(
                    "mu_strategy.market_data.trusted_data.refresh.load_stock_token_inst_ids",
                    side_effect=AssertionError("explicit refresh must not load stock token config"),
                ) as load_config:
                    with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical", side_effect=fetch_history):
                        with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                            exit_code = main(
                                [
                                    "--symbol",
                                    "MU",
                                    "--symbol",
                                    "MU-USDT-SWAP",
                                    "--limit",
                                    "99",
                                    "--days",
                                    "1",
                                    "--max-concurrency",
                                    "1",
                                    "--interval",
                                    "15m",
                                    "--data-dir",
                                    str(data_dir),
                                    "--html-output",
                                    str(html_path),
                                ],
                                stdout=stdout,
                            )

            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            html = html_path.read_text(encoding="utf-8")
            output = json.loads(stdout.getvalue())

        fetch_tickers.assert_not_called()
        load_config.assert_not_called()
        self.assertEqual(0, exit_code)
        self.assertEqual(
            [("MU-USDT-SWAP", "5m", 33), ("MU-USDT-SWAP", "15m", 33)],
            fetch_calls,
        )
        self.assertEqual("explicit_symbols", output["fetch_mode"])
        self.assertEqual(["MU-USDT-SWAP"], output["requested_symbols"])
        self.assertEqual(1, output["symbols"])
        self.assertIn("refresh_segments", output)
        self.assertEqual(2, len(output["refresh_segments"]))
        self.assertEqual(["15m", "5m"], [segment["interval"] for segment in output["slowest_segments"]])
        for segment in output["refresh_segments"]:
            self.assertEqual("MU-USDT-SWAP", segment["symbol"])
            self.assertEqual("full_history", segment["fetch_mode"])
            self.assertEqual(0, segment["existing_rows"])
            self.assertGreater(segment["fetched_rows"], 0)
            self.assertEqual(segment["fetched_rows"], segment["output_rows"])
            self.assertFalse(segment["had_existing"])
            self.assertFalse(segment["reused_prior_generation"])
            self.assertEqual("ok", segment["health_reason"])
        self.assertNotIn("failed_segments", output)
        self.assertNotIn("blocking_symbols", output)
        self.assertEqual(["5m", "15m"], manifest["effective_intervals"])
        self.assertEqual(output["refresh_segments"], manifest["diagnostics"]["refresh_segments"])
        self.assertEqual(["MU-USDT-SWAP"], list(manifest["symbols"]))
        self.assertEqual(
            [{"inst_id": "MU-USDT-SWAP", "last": 0.0, "volume_ccy_24h": 0.0, "source": "explicit"}],
            manifest["universes"]["crypto_top"],
        )
        self.assertEqual([], manifest["universes"]["stock_token_top"])
        self.assertEqual("explicit", manifest["symbols"]["MU-USDT-SWAP"]["source"])
        self.assertNotIn("stock_token_top_count_below_limit", ",".join(manifest["warnings"]))
        self.assertIn("explicit", html)

    def test_max_concurrency_cli_rejects_zero_and_negative_values(self):
        from mu_strategy.commands.refresh_market_data import main

        for value in (0, -1):
            with self.subTest(max_concurrency=value):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(["--max-concurrency", str(value)], stdout=io.StringIO())
                self.assertEqual(2, raised.exception.code)

    def test_dashboard_failure_after_successful_refresh_warns_without_cycle_error(self):
        from mu_strategy.commands.refresh_market_data import main

        run = _run("run-ok")

        with TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            html_path = Path(tmp) / "health.html"
            with patch("mu_strategy.commands.refresh_market_data._refresh_once", return_value=run):
                with patch(
                    "mu_strategy.commands.refresh_market_data.write_data_health_dashboard",
                    side_effect=OSError("dashboard offline"),
                ):
                    exit_code = main(["--interval", "5m", "--html-output", str(html_path)], stdout=stdout)

        output = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("run-ok", output["run_id"])
        self.assertEqual("success", output["attempt_status"])
        self.assertEqual("usable", output["snapshot_usability"])
        self.assertTrue(output["usable"])
        self.assertEqual(1, output["symbols"])
        self.assertIn("dashboard_write_failed: dashboard offline", output["warnings"])
        self.assertNotIn("status", output)
        self.assertNotIn("cycle_error", output)

    def test_dashboard_failure_after_degraded_usable_refresh_preserves_exit_code(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability

        run = _run(
            "run-degraded-usable",
            attempt_status=RefreshAttemptStatus.DEGRADED,
            snapshot_usability=SnapshotUsability.USABLE,
        )

        with TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            html_path = Path(tmp) / "health.html"
            with patch("mu_strategy.commands.refresh_market_data._refresh_once", return_value=run):
                with patch(
                    "mu_strategy.commands.refresh_market_data.write_data_health_dashboard",
                    side_effect=OSError("dashboard offline"),
                ):
                    exit_code = main(["--interval", "5m", "--html-output", str(html_path)], stdout=stdout)

        output = json.loads(stdout.getvalue())
        self.assertEqual(2, exit_code)
        self.assertEqual("run-degraded-usable", output["run_id"])
        self.assertEqual("degraded", output["attempt_status"])
        self.assertEqual("usable", output["snapshot_usability"])
        self.assertTrue(output["usable"])
        self.assertEqual(1, output["symbols"])
        self.assertIn("dashboard_write_failed: dashboard offline", output["warnings"])
        self.assertNotIn("status", output)
        self.assertNotIn("cycle_error", output)

    def test_failed_one_shot_exits_non_zero_after_writing_artifacts(self):
        from mu_strategy.commands.refresh_market_data import main

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            html_path = Path(tmp) / "health.html"
            stdout = io.StringIO()
            with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers", side_effect=TimeoutError("ticker timeout")):
                exit_code = main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--html-output",
                        str(html_path),
                        "--limit",
                        "1",
                        "--interval",
                        "5m",
                    ],
                    stdout=stdout,
                )

            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("failed", run_log[-1]["attempt_status"])
        self.assertTrue(html_exists)
        self.assertEqual("failed", output["attempt_status"])
        self.assertEqual("invalid", output["snapshot_usability"])
        self.assertFalse(output["usable"])
        self.assertEqual(0, output["symbols"])
        self.assertEqual("TimeoutError", output["cycle_error"]["error_type"])

    def test_limit_zero_one_shot_exits_non_zero_without_provider_config_or_symbol_csv(self):
        from mu_strategy.commands.refresh_market_data import main

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            html_path = Path(tmp) / "health.html"
            stock_config = Path(tmp) / "missing-stock-tokens.json"
            stdout = io.StringIO()
            with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers", side_effect=AssertionError("ticker fetch")) as fetch_tickers:
                with patch("mu_strategy.market_data.trusted_data.refresh.load_stock_token_inst_ids", side_effect=AssertionError("stock config")) as load_config:
                    exit_code = main(
                        [
                            "--data-dir",
                            str(data_dir),
                            "--html-output",
                            str(html_path),
                            "--stock-token-config",
                            str(stock_config),
                            "--limit",
                            "0",
                            "--days",
                            "1",
                            "--interval",
                            "5m",
                        ],
                        stdout=stdout,
                    )

            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            csv_paths = list(data_dir.rglob("*.csv"))
            html_exists = html_path.exists()

        fetch_tickers.assert_not_called()
        load_config.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("failed", output["attempt_status"])
        self.assertEqual("invalid", output["snapshot_usability"])
        self.assertFalse(output["usable"])
        self.assertEqual(0, output["symbols"])
        self.assertEqual("publication_not_fully_healthy", output["reason"])
        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual({"crypto_top": [], "stock_token_top": []}, manifest["universes"])
        self.assertEqual({}, manifest["symbols"])
        self.assertEqual("failed", run_log[-1]["attempt_status"])
        self.assertEqual("invalid", run_log[-1]["snapshot_usability"])
        self.assertEqual(0, run_log[-1]["symbol_count"])
        self.assertEqual([], csv_paths)
        self.assertTrue(html_exists)

    def test_degraded_invalid_one_shot_exits_non_zero_after_writing_artifacts(self):
        from mu_strategy.commands.refresh_market_data import main

        def fetch_history(symbol: str, interval: str, *, days: int):
            if symbol == "BTC-USDT-SWAP":
                raise TimeoutError("btc blocked")
            return _candles(interval)

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            html_path = Path(tmp) / "health.html"
            stock_config = Path(tmp) / "stock_tokens.json"
            stock_config.write_text("[]", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers",
                return_value=[
                    {"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"},
                    {"instId": "ETH-USDT-SWAP", "last": "90", "volCcy24h": "9"},
                ],
            ):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical", side_effect=fetch_history):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=86_400_000):
                        exit_code = main(
                            [
                                "--data-dir",
                                str(data_dir),
                                "--html-output",
                                str(html_path),
                                "--stock-token-config",
                                str(stock_config),
                                "--limit",
                                "2",
                                "--days",
                                "1",
                                "--interval",
                                "5m",
                            ],
                            stdout=stdout,
                        )

            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("degraded", output["attempt_status"])
        self.assertEqual("invalid", output["snapshot_usability"])
        self.assertFalse(output["usable"])
        self.assertEqual("degraded", manifest["attempt_status"])
        self.assertEqual("degraded", run_log[-1]["attempt_status"])
        self.assertEqual("refresh_failed", output["failed_segments"][0]["fetch_mode"])
        self.assertEqual("BTC-USDT-SWAP", output["failed_segments"][0]["symbol"])
        self.assertEqual(
            [{"interval": "5m", "reason": "refresh_failed"}],
            [
                {"interval": item["interval"], "reason": item["reason"]}
                for item in output["blocking_symbols"]["BTC-USDT-SWAP"]
            ],
        )
        self.assertTrue(html_exists)

    def test_success_stale_one_shot_exits_non_zero_after_writing_artifacts(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.utils import DAY_MS

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            html_path = Path(tmp) / "health.html"
            stock_config = Path(tmp) / "stock_tokens.json"
            stock_config.write_text("[]", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers",
                return_value=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            ):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical", side_effect=_fetch_history):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=10 * DAY_MS):
                        exit_code = main(
                            [
                                "--data-dir",
                                str(data_dir),
                                "--html-output",
                                str(html_path),
                                "--stock-token-config",
                                str(stock_config),
                                "--limit",
                                "1",
                                "--days",
                                "1",
                                "--interval",
                                "5m",
                            ],
                            stdout=stdout,
                        )

            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("failed", output["attempt_status"])
        self.assertEqual("invalid", output["snapshot_usability"])
        self.assertFalse(output["usable"])
        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("failed", run_log[-1]["attempt_status"])
        self.assertEqual("invalid", run_log[-1]["snapshot_usability"])
        self.assertTrue(html_exists)

    def test_validation_only_zero_usable_one_shot_exits_failed_invalid_after_writing_artifacts(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.utils import DAY_MS

        def fetch_history(symbol: str, interval: str, *, days: int):
            self.assertEqual("BTC-USDT-SWAP", symbol)
            self.assertEqual("5m", interval)
            return [Candle(0, 100.0, 99.0, 101.0, 100.0, 1000.0)]

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            html_path = Path(tmp) / "health.html"
            stock_config = Path(tmp) / "stock_tokens.json"
            stock_config.write_text("[]", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers",
                return_value=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            ):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical", side_effect=fetch_history):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=DAY_MS):
                        exit_code = main(
                            [
                                "--data-dir",
                                str(data_dir),
                                "--html-output",
                                str(html_path),
                                "--stock-token-config",
                                str(stock_config),
                                "--limit",
                                "1",
                                "--days",
                                "1",
                                "--interval",
                                "5m",
                            ],
                            stdout=stdout,
                        )

            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]

        output = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertEqual("failed", output["attempt_status"])
        self.assertEqual("invalid", output["snapshot_usability"])
        self.assertFalse(output["usable"])
        self.assertEqual("failed", manifest["attempt_status"])
        self.assertEqual("invalid", manifest["snapshot_usability"])
        self.assertEqual("ohlcv_invalid", manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["5m"]["reason"])
        self.assertEqual(
            [{"interval": "5m", "reason": "ohlcv_invalid"}],
            [
                {"interval": item["interval"], "reason": item["reason"]}
                for item in output["blocking_symbols"]["BTC-USDT-SWAP"]
            ],
        )
        self.assertEqual("failed", run_log[-1]["attempt_status"])
        self.assertEqual("invalid", run_log[-1]["snapshot_usability"])

    def test_success_partial_history_one_shot_exits_zero_after_writing_artifacts(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.utils import DAY_MS

        end_ms = 20 * DAY_MS

        def fetch_history(symbol: str, interval: str, *, days: int):
            self.assertEqual("BTC-USDT-SWAP", symbol)
            self.assertEqual("5m", interval)
            self.assertEqual(46, days)
            return range_candles(end_ms - DAY_MS, end_ms)

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            html_path = Path(tmp) / "health.html"
            stock_config = Path(tmp) / "stock_tokens.json"
            stock_config.write_text("[]", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.fetch_okx_swap_tickers",
                return_value=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}],
            ):
                with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_historical", side_effect=fetch_history):
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=end_ms):
                        exit_code = main(
                            [
                                "--data-dir",
                                str(data_dir),
                                "--html-output",
                                str(html_path),
                                "--stock-token-config",
                                str(stock_config),
                                "--limit",
                                "1",
                                "--days",
                                "14",
                                "--interval",
                                "5m",
                            ],
                            stdout=stdout,
                        )

            manifest = json.loads(manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("success", output["attempt_status"])
        self.assertEqual("usable", output["snapshot_usability"])
        self.assertTrue(output["usable"])
        self.assertNotIn("reason", output)
        self.assertEqual("success", manifest["attempt_status"])
        self.assertEqual("usable", manifest["snapshot_usability"])
        status = manifest["symbols"]["BTC-USDT-SWAP"]["intervals"]["5m"]
        self.assertEqual("ok", status["reason"])
        self.assertEqual("valid", status["integrity"])
        self.assertEqual(14, status["requested_days"])
        self.assertEqual("partial_available_history", status["coverage_state"])
        self.assertEqual("success", run_log[-1]["attempt_status"])
        self.assertEqual("usable", run_log[-1]["snapshot_usability"])
        self.assertTrue(html_exists)

    def test_loop_reports_cycle_failure_and_continues(self):
        from mu_strategy.commands.refresh_market_data import main

        calls = []
        success = _run("run-ok")

        def fake_refresh(args, *, intervals):
            calls.append((args, intervals))
            if len(calls) == 1:
                raise TimeoutError("ticker timeout")
            return success

        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise KeyboardInterrupt

        stdout = io.StringIO()
        with TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "health.html"
            with patch("mu_strategy.commands.refresh_market_data._refresh_once", side_effect=fake_refresh):
                with patch("mu_strategy.commands.refresh_market_data.write_data_health_dashboard") as write_dashboard:
                    with patch("mu_strategy.commands.refresh_market_data.time.sleep", side_effect=fake_sleep):
                        with self.assertRaises(KeyboardInterrupt):
                            main(["--loop", "--interval-seconds", "1", "--html-output", str(html_path)], stdout=stdout)

        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(2, len(calls))
        self.assertEqual([1, 1], sleeps)
        self.assertEqual(
            {"status": "error", "error_type": "TimeoutError", "message": "ticker timeout", "symbols": 0},
            rows[0],
        )
        self.assertEqual(
            {
                "run_id": "run-ok",
                "attempt_status": "success",
                "snapshot_usability": "usable",
                "usable": True,
                "symbols": 1,
                "fetch_mode": "top_universe",
            },
            rows[1],
        )
        write_dashboard.assert_called_once()

    def test_loop_reports_failed_partial_and_stale_domain_results_and_continues(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, RefreshAttemptStatus

        runs = [
            _run("run-failed", attempt_status=RefreshAttemptStatus.FAILED, cycle_error={"error_type": "TimeoutError", "message": "ticker timeout"}),
            _run("run-degraded", attempt_status=RefreshAttemptStatus.DEGRADED),
            _run("run-stale", freshness=FreshnessState.STALE),
            _run("run-ok"),
        ]
        calls = []

        def fake_refresh(args, *, intervals):
            calls.append((args, intervals))
            return runs[len(calls) - 1]

        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= len(runs):
                raise KeyboardInterrupt

        stdout = io.StringIO()
        with TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "health.html"
            with patch("mu_strategy.commands.refresh_market_data._refresh_once", side_effect=fake_refresh):
                with patch("mu_strategy.commands.refresh_market_data.write_data_health_dashboard") as write_dashboard:
                    with patch("mu_strategy.commands.refresh_market_data.time.sleep", side_effect=fake_sleep):
                        with self.assertRaises(KeyboardInterrupt):
                            main(["--loop", "--interval-seconds", "1", "--html-output", str(html_path)], stdout=stdout)

        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(4, len(calls))
        self.assertEqual(["failed", "degraded", "success", "success"], [row["attempt_status"] for row in rows])
        self.assertEqual(["invalid", "invalid", "invalid", "usable"], [row["snapshot_usability"] for row in rows])
        self.assertEqual([False, False, False, True], [row["usable"] for row in rows])
        self.assertEqual([1, 1, 1, 1], sleeps)
        self.assertEqual(4, write_dashboard.call_count)

    def test_one_shot_unexpected_exception_outputs_error_and_exits_non_zero(self):
        from mu_strategy.commands.refresh_market_data import main

        stdout = io.StringIO()
        with patch("mu_strategy.commands.refresh_market_data._refresh_once", side_effect=RuntimeError("disk offline")):
            exit_code = main([], stdout=stdout)

        self.assertNotEqual(0, exit_code)
        self.assertEqual(
            {"status": "error", "error_type": "RuntimeError", "message": "disk offline", "symbols": 0},
            json.loads(stdout.getvalue()),
        )


def _run(
    run_id: str,
    *,
    attempt_status=None,
    freshness=None,
    cycle_error=None,
    snapshot_usability=None,
):
    from mu_strategy.market_data.trusted_data.contracts import (
        AvailabilityState,
        DatasetHealth,
        DatasetKey,
        DatasetStorage,
        FreshnessState,
        HealthReason,
        IntegrityState,
        RefreshAttemptStatus,
        RefreshRun,
        SegmentReference,
        SnapshotUsability,
        TrustedStorageLayout,
        UniverseSnapshot,
        derive_snapshot_usability,
    )

    attempt_status = attempt_status or RefreshAttemptStatus.SUCCESS
    freshness = freshness or FreshnessState.FRESH
    reason = HealthReason.STALE_BY_CLOCK if freshness == FreshnessState.STALE else HealthReason.OK
    integrity = IntegrityState.VALID
    if attempt_status in {RefreshAttemptStatus.DEGRADED, RefreshAttemptStatus.FAILED}:
        integrity = IntegrityState.INVALID
        reason = HealthReason.REFRESH_FAILED
    health = DatasetHealth(
        key=DatasetKey("BTC-USDT-SWAP", "5m"),
        availability=AvailabilityState.AVAILABLE,
        integrity=integrity,
        freshness=freshness,
        reasons=(reason,),
        rows=1,
        first_timestamp_ms=0,
        last_timestamp_ms=0,
        updated_at_ms=0,
        source_file=Path("data/live/okx/BTC-USDT-SWAP/5m.csv"),
    )
    datasets = {("BTC-USDT-SWAP", "5m"): health}
    storage_by_dataset = {
        ("BTC-USDT-SWAP", "5m"): DatasetStorage(
            layout=TrustedStorageLayout.SEGMENTED_CSV_V1,
            source_root=Path("segments/okx/BTC-USDT-SWAP/5m"),
            segments=(
                SegmentReference(
                    segment_id="1970-01",
                    source_file=Path("segments/okx/BTC-USDT-SWAP/5m/1970-01.csv"),
                    start_row=0,
                    rows=1,
                    first_timestamp_ms=0,
                    last_timestamp_ms=0,
                    content_sha256="0" * 64,
                    closed=False,
                ),
            ),
        )
    }
    return RefreshRun(
        run_id=run_id,
        attempt_status=attempt_status,
        snapshot_usability=snapshot_usability or derive_snapshot_usability(datasets),
        started_at_ms=0,
        completed_at_ms=0,
        requested_intervals=("5m",),
        effective_intervals=("5m",),
        universe_snapshot=UniverseSnapshot(
            crypto_top=({"inst_id": "BTC-USDT-SWAP", "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"},)
        ),
        datasets=datasets,
        storage_by_dataset=storage_by_dataset,
        cycle_error=cycle_error,
    )


def _candles(interval: str) -> list[Candle]:
    if interval == "5m":
        return range_candles(0, 86_400_000)
    raise AssertionError(f"unexpected interval: {interval}")


def _fetch_history(symbol: str, interval: str, *, days: int) -> list[Candle]:
    return _candles(interval)


if __name__ == "__main__":
    unittest.main()
