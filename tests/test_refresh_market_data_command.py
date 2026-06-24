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
                "outcome": "success",
                "status": "ok",
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

            manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("failed", manifest["outcome"])
        self.assertEqual("invalid", manifest["status"])
        self.assertEqual("failed", run_log[-1]["outcome"])
        self.assertTrue(html_exists)
        self.assertEqual("failed", output["outcome"])
        self.assertEqual("invalid", output["status"])
        self.assertFalse(output["usable"])
        self.assertEqual(0, output["symbols"])
        self.assertEqual("TimeoutError", output["cycle_error"]["error_type"])

    def test_partial_one_shot_exits_non_zero_after_writing_artifacts(self):
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

            manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("partial", output["outcome"])
        self.assertEqual("invalid", output["status"])
        self.assertFalse(output["usable"])
        self.assertEqual("partial", manifest["outcome"])
        self.assertEqual("partial", run_log[-1]["outcome"])
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

            manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
            run_log = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]
            html_exists = html_path.exists()

        output = json.loads(stdout.getvalue())
        self.assertNotEqual(0, exit_code)
        self.assertEqual("success", output["outcome"])
        self.assertEqual("stale", output["status"])
        self.assertFalse(output["usable"])
        self.assertEqual("success", manifest["outcome"])
        self.assertEqual("stale", manifest["status"])
        self.assertEqual("stale", run_log[-1]["status"])
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
            {"run_id": "run-ok", "outcome": "success", "status": "ok", "usable": True, "symbols": 1},
            rows[1],
        )
        write_dashboard.assert_called_once()

    def test_loop_reports_failed_partial_and_stale_domain_results_and_continues(self):
        from mu_strategy.commands.refresh_market_data import main
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState, RefreshRunOutcome

        runs = [
            _run("run-failed", outcome=RefreshRunOutcome.FAILED, cycle_error={"error_type": "TimeoutError", "message": "ticker timeout"}),
            _run("run-partial", outcome=RefreshRunOutcome.PARTIAL),
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
        self.assertEqual(["failed", "partial", "success", "success"], [row["outcome"] for row in rows])
        self.assertEqual(["invalid", "invalid", "stale", "ok"], [row["status"] for row in rows])
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
    outcome=None,
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
        RefreshRun,
        RefreshRunOutcome,
        UniverseSnapshot,
    )

    outcome = outcome or RefreshRunOutcome.SUCCESS
    freshness = freshness or FreshnessState.FRESH
    reason = HealthReason.STALE_BY_CLOCK if freshness == FreshnessState.STALE else HealthReason.OK
    integrity = IntegrityState.VALID
    if outcome == RefreshRunOutcome.PARTIAL:
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
    return RefreshRun(
        run_id=run_id,
        outcome=outcome,
        started_at_ms=0,
        completed_at_ms=0,
        requested_intervals=("5m",),
        effective_intervals=("5m",),
        universe_snapshot=UniverseSnapshot(
            crypto_top=({"inst_id": "BTC-USDT-SWAP", "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"},)
        ),
        datasets={("BTC-USDT-SWAP", "5m"): health},
        cycle_error=cycle_error,
    )


def _candles(interval: str) -> list[Candle]:
    candles = [Candle(index * 300_000, 100 + index, 101 + index, 99 + index, 100 + index, 10.0) for index in range(12)]
    if interval == "5m":
        return candles
    raise AssertionError(f"unexpected interval: {interval}")


def _fetch_history(symbol: str, interval: str, *, days: int) -> list[Candle]:
    return _candles(interval)


if __name__ == "__main__":
    unittest.main()
