import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle


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
        self.assertEqual(
            {
                "run_id": "run-ok",
                "attempt_status": "success",
                "snapshot_usability": "usable",
                "usable": True,
                "symbols": 1,
            },
            json.loads(stdout.getvalue()),
        )
        write_dashboard.assert_called_once()

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

            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
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

            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
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
                    with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=3_600_000):
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

            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("degraded", output["attempt_status"])
        self.assertEqual("invalid", output["snapshot_usability"])
        self.assertFalse(output["usable"])
        self.assertEqual("degraded", manifest["attempt_status"])
        self.assertEqual("degraded", run_log[-1]["attempt_status"])
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

            manifest = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("success", output["attempt_status"])
        self.assertEqual("stale", output["snapshot_usability"])
        self.assertFalse(output["usable"])
        self.assertEqual("success", manifest["attempt_status"])
        self.assertEqual("stale", manifest["snapshot_usability"])
        self.assertEqual("stale", run_log[-1]["snapshot_usability"])
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
            {"run_id": "run-ok", "attempt_status": "success", "snapshot_usability": "usable", "usable": True, "symbols": 1},
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
        self.assertEqual(["invalid", "invalid", "stale", "usable"], [row["snapshot_usability"] for row in rows])
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
):
    from mu_strategy.market_data.trusted_data.contracts import (
        AvailabilityState,
        DatasetHealth,
        DatasetKey,
        FreshnessState,
        HealthReason,
        IntegrityState,
        RefreshAttemptStatus,
        RefreshRun,
        SnapshotUsability,
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
    return RefreshRun(
        run_id=run_id,
        attempt_status=attempt_status,
        snapshot_usability=derive_snapshot_usability(datasets),
        started_at_ms=0,
        completed_at_ms=0,
        requested_intervals=("5m",),
        effective_intervals=("5m",),
        universe_snapshot=UniverseSnapshot(
            crypto_top=({"inst_id": "BTC-USDT-SWAP", "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"},)
        ),
        datasets=datasets,
        cycle_error=cycle_error,
    )


def _candles(interval: str) -> list[Candle]:
    candles = [Candle(index * 300_000, 100 + index, 101 + index, 99 + index, 100 + index, 10.0) for index in range(12)]
    if interval == "5m":
        return candles
    raise AssertionError(f"unexpected interval: {interval}")


def _fetch_history(symbol: str, interval: str, *, days: int) -> list[Candle]:
    return _candles(interval)


def _manifest_path(data_dir: Path) -> Path:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore

    current = data_dir / "current.json"
    if current.exists():
        return data_dir / json.loads(current.read_text(encoding="utf-8"))["manifest"]
    return TrustedDataStore(data_dir=data_dir).flat_manifest_path


if __name__ == "__main__":
    unittest.main()
