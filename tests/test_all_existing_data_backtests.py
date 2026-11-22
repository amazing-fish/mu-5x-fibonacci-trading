import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mu_strategy.commands import all_existing_data_backtests
from mu_strategy.commands.all_existing_data_backtests import (
    LocalDataset,
    audit_candles,
    discover_datasets,
    duration_label,
    render_summary_html,
    render_summary_markdown,
    run_all_existing_data_backtests,
    true_duration_ms,
)
from mu_strategy.models import BacktestResult, Candle


class AllExistingDataBacktestsTest(unittest.TestCase):
    def test_true_duration_uses_last_candle_close_not_filename_days(self):
        candles = [
            Candle(0, 100, 101, 99, 100, 1),
            Candle(15 * 60 * 1000, 100, 101, 99, 100, 1),
            Candle(30 * 60 * 1000, 100, 101, 99, 100, 1),
        ]

        self.assertEqual(45 * 60 * 1000, true_duration_ms(candles, "15m"))
        self.assertEqual("0d 0h 45m", duration_label(true_duration_ms(candles, "15m")))

    def test_audit_flags_large_15m_open_close_move_without_marking_ohlc_invalid(self):
        candles = [
            Candle(0, 100, 108, 99, 106, 1),
            Candle(15 * 60 * 1000, 106, 107, 105, 106, 1),
        ]

        audit = audit_candles(candles, "15m", open_close_warning_pct=0.05)

        self.assertEqual(0, audit.invalid_ohlc)
        self.assertEqual(0, audit.duplicate_timestamps)
        self.assertEqual(0, audit.gaps)
        self.assertEqual(1, audit.open_close_warnings)
        self.assertEqual("1970-01-01T00:00:00+00:00", audit.top_open_close_moves[0].open_time_iso)

    def test_summary_markdown_displays_true_duration_and_data_quality_columns(self):
        row = {
            "source": "okx",
            "symbol": "MU-USDT-SWAP",
            "nominal_days": 180,
            "bars_15m": 3,
            "bars_1h": 1,
            "duration_15m_label": "0d 0h 45m",
            "duration_1h_label": "0d 1h 0m",
            "start_15m": "1970-01-01T00:00:00+00:00",
            "end_15m": "1970-01-01T00:30:00+00:00",
            "ending_equity": 10000.0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "audited_events": 0,
            "range_anomalies": 0,
            "invalid_ohlc": 0,
            "duplicate_timestamps": 0,
            "gaps_15m": 0,
            "open_close_warnings": 1,
            "high_low_warnings": 1,
            "prev_close_open_gap_warnings": 0,
            "max_open_close_move_pct": 0.06,
            "max_high_low_range_pct": 0.09,
            "max_prev_close_open_gap_pct": 0.01,
            "top_open_close_moves": [],
            "top_high_low_ranges": [],
            "top_prev_close_open_gaps": [],
            "report_path": Path("reports/example.md"),
            "chart_path": Path("reports/example.html"),
        }

        markdown = render_summary_markdown(
            [row],
            generated_at="1970-01-01T00:00:00+00:00",
            strategy="optimized_v2",
            open_close_warning_pct=0.025,
            high_low_warning_pct=0.075,
            prev_close_open_warning_pct=0.015,
        )
        html = render_summary_html(
            [row],
            generated_at="1970-01-01T00:00:00+00:00",
            strategy="optimized_v2",
            open_close_warning_pct=0.025,
            high_low_warning_pct=0.075,
            prev_close_open_warning_pct=0.015,
        )

        self.assertIn("strategy: optimized_v2", markdown)
        self.assertIn("2.50%", markdown)
        self.assertIn("7.50%", markdown)
        self.assertIn("1.50%", markdown)
        self.assertIn("true 15m duration", markdown)
        self.assertIn("0d 0h 45m", markdown)
        self.assertIn("open-close warnings", markdown)
        self.assertIn("high-low warnings", markdown)
        self.assertIn("6.00%", markdown)
        self.assertIn("optimized_v2", html)
        self.assertIn("2.50%", html)

    def test_per_dataset_markdown_report_includes_robustness_benchmark(self):
        dataset = LocalDataset(
            source="cached",
            symbol="MU-USDT-SWAP",
            nominal_days=1,
            file_15m=Path("data/MU-USDT-SWAP_15m_1d.csv"),
            file_1h=Path("data/MU-USDT-SWAP_1h_1d.csv"),
        )
        candles_15m = [
            Candle(0, 100, 101, 99, 100, 1_000),
            Candle(900_000, 100, 111, 99, 110, 1_000),
        ]
        candles_1h = [
            Candle(0, 100, 101, 99, 100, 1_000),
            Candle(3_600_000, 100, 111, 99, 110, 1_000),
        ]

        with TemporaryDirectory() as temp_dir:
            with patch(
                "mu_strategy.commands.all_existing_data_backtests.discover_datasets", return_value=[dataset]
            ), patch(
                "mu_strategy.commands.all_existing_data_backtests.read_csv",
                side_effect=[candles_15m, candles_1h],
            ), patch(
                "mu_strategy.commands.all_existing_data_backtests.run_backtest",
                return_value=BacktestResult(10_000, 10_000, [], []),
            ), patch(
                "mu_strategy.commands.all_existing_data_backtests.render_html_visualization",
                return_value="<html></html>",
            ):
                rows = run_all_existing_data_backtests(
                    data_dir=Path(temp_dir) / "data",
                    report_dir=Path(temp_dir) / "reports",
                    strategy="baseline",
                )

            report = rows[0]["report_path"].read_text(encoding="utf-8")

        self.assertIn("## Robustness", report)
        self.assertIn("- buy-and-hold benchmark (1x): 10.00%", report)

    def test_discovers_trusted_generations_in_data_live_and_parent_data_dir(self):
        root = Path(__file__).resolve().parents[1]
        from_live = discover_datasets(root / "data/live")
        from_data = discover_datasets(root / "data")

        self.assertGreaterEqual(len(from_live), 2)
        self.assertIn("0114f8334f2e40d6ba5b5030faf6007b", {item.generation_id for item in from_live})
        self.assertIn("e702be27d2de4b2d92b12bf01c70d02d", {item.generation_id for item in from_live})
        self.assertTrue(all(item.symbol == "MU-USDT-SWAP" for item in from_live))
        self.assertIn("0114f8334f2e40d6ba5b5030faf6007b", {item.generation_id for item in from_data})

    def test_zero_datasets_fails_loudly_and_never_emits_vacuous_pass(self):
        empty_markdown = render_summary_markdown([], generated_at="1970-01-01T00:00:00+00:00")
        self.assertIn("FAIL: no datasets audited", empty_markdown)
        self.assertNotIn("PASS:", empty_markdown)

        with TemporaryDirectory() as tmp:
            empty_data = Path(tmp) / "empty_data"
            empty_data.mkdir()
            report_dir = Path(tmp) / "reports"
            with self.assertRaises(FileNotFoundError):
                run_all_existing_data_backtests(
                    data_dir=empty_data,
                    report_dir=report_dir,
                    strategy="baseline",
                )

            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                with self.assertRaises(SystemExit) as raised:
                    all_existing_data_backtests.main([
                        "--data-dir", str(empty_data),
                        "--report-dir", str(report_dir),
                    ])
            self.assertEqual(2, raised.exception.code)
            self.assertIn("no local datasets found", stderr.getvalue())
            self.assertFalse((report_dir / "mu_all_existing_data_backtests.md").exists())


if __name__ == "__main__":
    unittest.main()
