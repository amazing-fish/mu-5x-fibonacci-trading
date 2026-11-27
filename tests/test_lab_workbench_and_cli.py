"""Unit and integration tests for Phase 2 offline HTML workbench and read-only CLI."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mu_strategy.core.instrument import CostModel, okx_swap_spec
from mu_strategy.data import write_csv
from mu_strategy.lab import cli as lab_cli
from mu_strategy.lab.discovery import DiscoveryConfig, default_strategy_catalog, discover_strategies
from mu_strategy.lab.workbench_html import render_workbench_html, write_workbench_html
from mu_strategy.market_data.bar_series import BarSeries
from mu_strategy.market_data.funding import FundingSeries
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.models import Candle
from tests.factories.trusted_publication import write_generation_publication


_HOUR_MS = 3_600_000


def _synthetic_candles(count: int, *, start_ms: int = 1_770_000_000_000) -> list[Candle]:
    candles: list[Candle] = []
    price = 100.0
    for i in range(count):
        wave = 0.006 * math.sin(i / 11.0) + 0.0012 * math.cos(i / 3.0) + 0.0004
        open_px = price
        close_px = max(10.0, open_px * (1.0 + wave))
        high_px = max(open_px, close_px) * 1.004
        low_px = min(open_px, close_px) * 0.996
        candles.append(
            Candle(
                open_time_ms=start_ms + i * _HOUR_MS,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                volume=1_000.0 + (i % 17) * 50.0,
            )
        )
        price = close_px
    return candles


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sample_report_dict() -> dict:
    return {
        "schema_version": "phase2.discovery.v1",
        "symbol": "MU-USDT-SWAP",
        "interval": "1h",
        "starting_equity": 10000.0,
        "oos_start_ms": 1775000000000,
        "oos_end_ms": 1785000000000,
        "oos_days": 115.74,
        "n_folds": 3,
        "n_trials": 4,
        "runtime_ms": 412.5,
        "funding_verified": True,
        "benchmarks": {
            "spot_1x": {
                "label": "spot_1x",
                "total_return": 0.12,
                "max_drawdown": 0.28,
                "sharpe": 0.62,
                "curve": [[1775000000000, 10000.0], [1780000000000, 10500.0], [1785000000000, 11200.0]],
            },
            "perp_1x": {
                "label": "perp_1x",
                "total_return": 0.09,
                "max_drawdown": 0.30,
                "sharpe": 0.48,
                "curve": [[1775000000000, 10000.0], [1780000000000, 10300.0], [1785000000000, 10900.0]],
            },
        },
        "candidates": [
            {
                "candidate_id": "ts_momentum:none:0",
                "family_id": "ts_momentum",
                "display_name": "Time-Series Momentum",
                "overlay_preset": "none",
                "params": {"lookback_bars": 96, "deadband_return": 0.015},
                "metrics": {
                    "oos_total_return": 0.24,
                    "oos_max_drawdown": 0.14,
                    "oos_sharpe": 1.42,
                    "oos_calmar": 1.71,
                    "oos_trades": 28,
                    "liquidations": 0,
                    "dsr": 0.962,
                },
                "attribution": {
                    "gross_pnl": 2800.0,
                    "funding_pnl": -120.0,
                    "fees": 210.0,
                    "slippage": 70.0,
                    "net_pnl": 2400.0,
                },
                "gates": {
                    "verdict": "GO",
                    "passed_count": 6,
                    "dsr": 0.962,
                    "failed_gates": [],
                    "checks": [
                        {"gate_id": f"G{i}", "name": f"Gate {i}", "status": "PASS", "value": 1.0, "threshold": 0.5, "detail": "ok"}
                        for i in range(1, 7)
                    ],
                },
                "folds": [
                    {"fold_index": 0, "oos_total_return": 0.08, "oos_sharpe": 1.3, "oos_max_drawdown": 0.09, "oos_trades": 9}
                ],
                "curve": [[1775000000000, 10000.0], [1780000000000, 11100.0], [1785000000000, 12400.0]],
            }
        ],
    }


class WorkbenchHtmlTests(unittest.TestCase):
    def test_render_workbench_html_is_offline_zero_cdn_and_has_core_controls(self):
        html_doc = render_workbench_html(_sample_report_dict())
        self.assertNotIn("http://", html_doc)
        self.assertNotIn("https://", html_doc)
        self.assertIsNone(re.search(r"<script[^>]+src=", html_doc, re.IGNORECASE))
        self.assertIsNone(re.search(r"<link[^>]+href=", html_doc, re.IGNORECASE))
        for token in (
            "Phase 2 Strategy Discovery Workbench",
            "DRY-RUN ONLY",
            'id="themeBtn"',
            'id="scaleLinearBtn"',
            'id="scaleLogBtn"',
            'id="equitySvg"',
            'id="ddSvg"',
            'id="matrixBody"',
            'id="attrPanel"',
            'id="recipeBox"',
            'id="copyRecipeBtn"',
            '"dry_run_only":true',
            '"live_orders_allowed":false',
        ):
            self.assertIn(token, html_doc)

    def test_render_workbench_html_escapes_script_breakout_xss_and_non_finite_floats(self):
        payload = _sample_report_dict()
        evil = "</script><img src=x onerror=alert(1)>&\u2028\u2029"
        payload["symbol"] = evil
        payload["candidates"][0]["display_name"] = evil
        payload["candidates"][0]["gates"]["checks"][0]["detail"] = evil
        payload["candidates"][0]["metrics"]["oos_sharpe"] = float("nan")
        payload["candidates"][0]["attribution"]["gross_pnl"] = float("inf")

        html_doc = render_workbench_html(payload)
        self.assertNotIn("</script><img", html_doc)
        self.assertNotIn("\u2028", html_doc)
        self.assertNotIn("\u2029", html_doc)

        match = re.search(r'<script id="discovery-data" type="application/json">(.*?)</script>', html_doc, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        embedded = match.group(1)
        self.assertNotIn("NaN", embedded)
        self.assertNotIn("Infinity", embedded)
        parsed = json.loads(embedded)
        self.assertEqual(evil, parsed["symbol"])
        self.assertIsNone(parsed["candidates"][0]["metrics"]["oos_sharpe"])
        self.assertIsNone(parsed["candidates"][0]["attribution"]["gross_pnl"])
        self.assertTrue(parsed["winner_recipe"]["dry_run_only"])
        self.assertFalse(parsed["winner_recipe"]["live_orders_allowed"])

    def test_write_workbench_html_handles_empty_candidates_and_real_discovery_report(self):
        empty = {**_sample_report_dict(), "candidates": []}
        with TemporaryDirectory() as tmp:
            out = write_workbench_html(empty, Path(tmp) / "nested" / "empty.html")
            self.assertTrue(out.is_file())
            text = out.read_text(encoding="utf-8")
            self.assertIn('"verdict":"NO_GO"', text)

        candles = _synthetic_candles(240)
        series = BarSeries.from_candles("MU-USDT-SWAP", "1h", candles)
        instrument = okx_swap_spec("MU-USDT-SWAP")
        catalog = tuple(e for e in default_strategy_catalog(symbol="MU-USDT-SWAP", interval="1h") if e.family_id == "ts_momentum")
        report = discover_strategies(
            series,
            instrument,
            CostModel.okx_default(),
            catalog=catalog,
            config=DiscoveryConfig(n_folds=2, purge_bars=6, overlay_names=("none",)),
        )
        rendered = render_workbench_html(report)
        self.assertIn("MU-USDT-SWAP", rendered)
        self.assertNotIn("http://", rendered)
        self.assertNotIn("https://", rendered)


class LabCliTests(unittest.TestCase):
    def test_help_renders_literal_percent_signs_for_root_and_discover(self):
        for argv in (["mu_strategy.lab.cli", "--help"], ["mu_strategy.lab.cli", "discover", "--help"]):
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                with patch("sys.argv", argv), redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as exc:
                        lab_cli.main()
                self.assertEqual(0, exc.exception.code)
                self.assertIn("0.0500%", stdout.getvalue())
                self.assertIn("0.0200%", stdout.getvalue())

    def test_cli_rejects_no_dry_run_and_outputs_inside_data_dir(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "live"
            data_dir.mkdir()
            sentinel = data_dir / "current.json"
            sentinel.write_text('{"schema_version":1}', encoding="utf-8")
            before = _snapshot_tree(data_dir)

            for argv in (
                ["discover", "--data-dir", str(data_dir), "--no-dry-run"],
                ["discover", "--data-dir", str(data_dir), "--output-html", str(sentinel)],
                ["discover", "--data-dir", str(data_dir), "--output-json", str(data_dir / "sub" / "out.json")],
                ["discover", "--data-dir", str(data_dir), "--recipe-out", str(data_dir / "recipe.json")],
                ["discover", "--data-dir", str(data_dir), "--families", "nonexistent_family"],
                ["discover", "--data-dir", str(data_dir), "--overlays", "nonexistent_overlay"],
            ):
                with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exc:
                        lab_cli.main(argv)
                    self.assertEqual(2, exc.exception.code)
            self.assertEqual(before, _snapshot_tree(data_dir))

    def test_cli_discover_from_trusted_store_is_readonly_with_zero_bar_cache_or_network(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "live"
            start_ms = 1_772_611_200_000
            end_ms = start_ms + 12 * 86_400_000
            write_generation_publication(data_dir, symbol="MU-USDT-SWAP", start_ms=start_ms, end_ms=end_ms)
            before = _snapshot_tree(data_dir)

            out_html = root / "out" / "workbench.html"
            out_json = root / "out" / "report.json"
            out_recipe = root / "out" / "recipe.json"
            stdout = io.StringIO()
            with (
                patch("urllib.request.urlopen", side_effect=AssertionError("network must not be used")),
                patch.object(TrustedDataStore, "write_csv", side_effect=AssertionError("write_csv")),
                patch.object(TrustedDataStore, "write_segmented_dataset", side_effect=AssertionError("write_segmented_dataset")),
                patch.object(TrustedDataStore, "write_generation_manifest", side_effect=AssertionError("write_generation_manifest")),
                patch.object(TrustedDataStore, "append_run_log", side_effect=AssertionError("append_run_log")),
                redirect_stdout(stdout),
            ):
                rc = lab_cli.main(
                    [
                        "discover",
                        "--data-dir",
                        str(data_dir),
                        "--symbol",
                        "MU-USDT-SWAP",
                        "--interval",
                        "1h",
                        "--families",
                        "ts_momentum",
                        "--overlays",
                        "none",
                        "--folds",
                        "2",
                        "--purge-bars",
                        "6",
                        "--output-html",
                        str(out_html),
                        "--output-json",
                        str(out_json),
                        "--recipe-out",
                        str(out_recipe),
                    ]
                )

            self.assertEqual(0, rc)
            self.assertEqual(before, _snapshot_tree(data_dir))
            self.assertFalse((data_dir / ".bar_cache").exists())
            self.assertIn("Phase 2 Strategy Discovery Summary", stdout.getvalue())
            self.assertIn("DRY-RUN ONLY", stdout.getvalue())

            report_payload = json.loads(out_json.read_text(encoding="utf-8"))
            recipe_payload = json.loads(out_recipe.read_text(encoding="utf-8"))
            html_text = out_html.read_text(encoding="utf-8")
            self.assertEqual("phase2.discovery.v1", report_payload["schema_version"])
            self.assertEqual("MU-USDT-SWAP", report_payload["symbol"])
            self.assertGreater(len(report_payload["candidates"]), 0)
            self.assertTrue(recipe_payload["dry_run_only"])
            self.assertFalse(recipe_payload["live_orders_allowed"])
            self.assertNotIn("http://", html_text)
            self.assertNotIn("https://", html_text)

    def test_cli_discover_from_candles_csv_and_funding_file_and_generation_id_replay(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = _synthetic_candles(240)
            csv_path = root / "candles_1h.csv"
            write_csv(candles, csv_path)
            funding = FundingSeries.synthetic_constant_apr(
                "okx:MU-USDT-SWAP",
                start_ms=candles[0].open_time_ms - _HOUR_MS,
                end_ms=candles[-1].open_time_ms + _HOUR_MS,
                interval_hours=8,
                apr=0.10,
            )
            funding_path = root / "funding.json"
            funding.to_json_file(funding_path)

            out_json = root / "csv_report.json"
            with redirect_stdout(io.StringIO()):
                rc = lab_cli.main(
                    [
                        "discover",
                        "--candles-file",
                        str(csv_path),
                        "--funding-file",
                        str(funding_path),
                        "--symbol",
                        "MU-USDT-SWAP",
                        "--interval",
                        "1h",
                        "--fee-profile",
                        "limit",
                        "--families",
                        "donchian_atr",
                        "--overlays",
                        "none",
                        "--folds",
                        "2",
                        "--purge-bars",
                        "6",
                        "--output-json",
                        str(out_json),
                    ]
                )
            self.assertEqual(0, rc)
            report = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertTrue(report["funding_verified"])

            # Verify explicit --generation-id replay ignores corrupted current.json
            data_dir = root / "live_replay"
            start_ms = 1_772_611_200_000
            end_ms = start_ms + 12 * 86_400_000
            manifest = write_generation_publication(data_dir, symbol="MU-USDT-SWAP", start_ms=start_ms, end_ms=end_ms)
            gen_id = manifest["run_id"]
            (data_dir / "current.json").write_text("corrupt-pointer", encoding="utf-8")
            with (
                patch.object(TrustedDataStore, "read_manifest", side_effect=AssertionError("current.json must not be read")),
                redirect_stdout(io.StringIO()),
            ):
                rc_replay = lab_cli.main(
                    [
                        "discover",
                        "--data-dir",
                        str(data_dir),
                        "--generation-id",
                        gen_id,
                        "--families",
                        "ts_momentum",
                        "--overlays",
                        "none",
                        "--folds",
                        "2",
                        "--purge-bars",
                        "6",
                    ]
                )
            self.assertEqual(0, rc_replay)


if __name__ == "__main__":
    unittest.main()
