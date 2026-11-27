"""Unit and integration tests for Phase 2 offline HTML workbench and read-only CLI."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
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
                "stress_scenarios": [
                    {"scenario_id": "slippage_10bp", "label": "Slippage +10bp", "available": True, "passed": True, "total_return": 0.19, "liquidations": 0, "detail": "ok"},
                    {"scenario_id": "funding_0pct_apr", "label": "Funding 0% APR", "available": True, "passed": True, "total_return": 0.25, "liquidations": 0, "detail": "ok"},
                    {"scenario_id": "funding_15pct_apr", "label": "Funding +15% APR", "available": True, "passed": True, "total_return": 0.20, "liquidations": 0, "detail": "ok"},
                    {"scenario_id": "funding_30pct_apr", "label": "Funding +30% APR", "available": True, "passed": False, "total_return": -0.02, "liquidations": 0, "detail": "negative"},
                ],
                "curve": [[1775000000000, 10000.0], [1780000000000, 11100.0], [1785000000000, 12400.0]],
            }
        ],
    }


def _run_workbench_dom_probe(html_doc: str, *, expand_first: bool = False, collapse_after: bool = False) -> dict:
    """Execute the standalone workbench inline script in a lightweight Node DOM harness."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node executable not available for DOM probe")
    harness = r"""
const fs = require("fs");
const html = fs.readFileSync(0, "utf8");
const opts = JSON.parse(process.argv[1]);
const dataMatch = html.match(/<script id="discovery-data" type="application\/json">([\s\S]*?)<\/script>/);
if (!dataMatch) throw new Error("missing discovery-data");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length !== 1) throw new Error("expected 1 inline script");

const elements = new Map();
function makeEl(id, tag = "div", attrs = {}) {
  const listeners = {};
  const classSet = new Set();
  const el = {
    id, tag, dataset: { ...attrs }, _html: "", _cache: {}, textContent: "", value: "", style: {}, focused: false,
    classList: {
      add: (c) => classSet.add(c),
      remove: (c) => classSet.delete(c),
      toggle: (c, f) => (f === undefined ? (classSet.has(c) ? classSet.delete(c) : classSet.add(c)) : (f ? classSet.add(c) : classSet.delete(c))),
    },
    set innerHTML(v) { this._html = String(v); this._cache = {}; },
    get innerHTML() { return this._html; },
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k]; },
    focus() { this.focused = true; },
    addEventListener(ev, fn) { (listeners[ev] ||= []).push(fn); },
    dispatch(ev, obj = {}) { (listeners[ev] || []).forEach((fn) => fn({ target: el, stopPropagation() {}, ...obj })); },
    querySelectorAll(sel) {
      if (this._cache[sel]) return this._cache[sel];
      if (sel === ".leg-item") {
        this._cache[sel] = [...this._html.matchAll(/data-key="([^"]+)"/g)].map((m) => makeEl("", "button", { key: m[1] }));
        return this._cache[sel];
      }
      if (sel === "button.row-toggle") {
        const out = [];
        for (const m of this._html.matchAll(/<button[^>]*class="row-toggle"[^>]*data-id="([^"]+)"[^>]*aria-expanded="([^"]+)"[^>]*>/g)) {
          const b = makeEl("", "button", { id: m[1] });
          b.setAttribute("aria-expanded", m[2]);
          out.push(b);
        }
        this._cache[sel] = out;
        return out;
      }
      if (sel === "tr.cand-row") {
        this._cache[sel] = [...this._html.matchAll(/<tr class="cand-row[^"]*" data-id="([^"]+)"/g)].map((m) => makeEl("", "tr", { id: m[1] }));
        return this._cache[sel];
      }
      return [];
    },
    getBoundingClientRect() { return { left: 0, width: 1120, top: 0, height: 270 }; },
  };
  if (id) elements.set(id, el);
  return el;
}
["headerSub","kpiGrid","themeBtn","scaleLinearBtn","scaleLogBtn","resetLegendBtn","chartLegend","chartWrap","equitySvg","ddSvg","chartTip","candCount","searchBox","matrixBody","attrTarget","attrPanel","copyRecipeBtn","recipeBanner","recipeBox","eqCross","ddCross","eqHit","ddHit"].forEach((id) => makeEl(id));
elements.set("discovery-data", { textContent: dataMatch[1] });
const filterBtns = ["ALL","GO","CONDITIONAL","NO_GO"].map((f) => makeEl("", "button", { filter: f }));
const sortThs = ["rank","name","total_return","max_drawdown","sharpe","dsr","calmar","trades","liquidations","verdict"].map((s) => makeEl("", "th", { sort: s }));
const document = {
  documentElement: { setAttribute(k, v) { this[k] = v; } },
  getElementById: (id) => elements.get(id) || makeEl(id),
  querySelectorAll: (sel) => (sel === "[data-filter]" ? filterBtns : (sel === "th[data-sort]" ? sortThs : [])),
  createElement: (tag) => makeEl("", tag),
  body: { appendChild() {}, removeChild() {} },
};
const navigator = { clipboard: { writeText: () => Promise.resolve() } };
new Function("document", "navigator", scripts[0])(document, navigator);

const matrixBody = elements.get("matrixBody");
const beforeHtml = matrixBody.innerHTML;
let expandedHtml = null;
let collapsedHtml = null;
const initialBtns = matrixBody.querySelectorAll("button.row-toggle");
if (opts.expand_first && initialBtns.length > 0) {
  initialBtns[0].dispatch("click");
  expandedHtml = matrixBody.innerHTML;
  const expandedBtns = matrixBody.querySelectorAll("button.row-toggle");
  if (opts.collapse_after && expandedBtns.length > 0) {
    expandedBtns[0].dispatch("click");
    collapsedHtml = matrixBody.innerHTML;
  }
}
console.log(JSON.stringify({
  beforeHtml,
  expandedHtml,
  collapsedHtml,
  recipeBannerHtml: elements.get("recipeBanner").innerHTML,
  recipeBoxText: elements.get("recipeBox").textContent,
}));
"""
    proc = subprocess.run(
        [node, "-e", harness, json.dumps({"expand_first": expand_first, "collapse_after": collapse_after})],
        input=html_doc,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return json.loads(proc.stdout)


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

    def test_workbench_disclosure_button_stress_matrix_and_fail_closed_verdict(self):
        # P2R1-10 & P2R1-11 (perp all 4 scenarios + keyboard disclosure button + aria-expanded)
        perp_html = render_workbench_html(_sample_report_dict())
        perp_dom = _run_workbench_dom_probe(perp_html, expand_first=True, collapse_after=True)
        self.assertIn('class="row-toggle"', perp_dom["beforeHtml"])
        self.assertIn('aria-expanded="false"', perp_dom["beforeHtml"])
        self.assertIn('aria-expanded="true"', perp_dom["expandedHtml"])
        for label in ("Stress Matrix", "Slippage +10bp", "Funding 0% APR", "Funding +15% APR", "Funding +30% APR"):
            self.assertIn(label, perp_dom["expandedHtml"])
        self.assertIn('aria-expanded="false"', perp_dom["collapsedHtml"])
        self.assertNotIn("detail-row", perp_dom["collapsedHtml"])

        # P2R1-11 (spot unavailable funding scenarios explicitly rendered as N/A + empty stress graceful)
        spot_payload = _sample_report_dict()
        spot_payload["candidates"][0]["stress_scenarios"] = [
            {"scenario_id": "slippage_10bp", "label": "Slippage +10bp", "available": True, "passed": True, "total_return": 0.15, "liquidations": 0, "detail": "ok"},
            {"scenario_id": "funding_0pct_apr", "label": "Funding 0% APR", "available": False, "passed": None, "total_return": None, "liquidations": None, "detail": "N/A — spot instrument"},
            {"scenario_id": "funding_15pct_apr", "label": "Funding +15% APR", "available": False, "passed": None, "total_return": None, "liquidations": None, "detail": "N/A — spot instrument"},
            {"scenario_id": "funding_30pct_apr", "label": "Funding +30% APR", "available": False, "passed": None, "total_return": None, "liquidations": None, "detail": "N/A — spot instrument"},
        ]
        spot_dom = _run_workbench_dom_probe(render_workbench_html(spot_payload), expand_first=True)
        self.assertEqual(3, spot_dom["expandedHtml"].count(">N/A<"))
        self.assertIn("N/A — spot instrument", spot_dom["expandedHtml"])
        self.assertNotIn("NaN", spot_dom["expandedHtml"])

        missing_stress = _sample_report_dict()
        missing_stress["candidates"][0].pop("stress_scenarios", None)
        missing_dom = _run_workbench_dom_probe(render_workbench_html(missing_stress), expand_first=True)
        self.assertIn("No stress scenario data.", missing_dom["expandedHtml"])

        # P2R1-12: recipe={} / unknown verdict MUST fail closed (never render green GO banner)
        empty_report = {**_sample_report_dict(), "candidates": []}
        for bad_recipe in ({}, {"verdict": "UNKNOWN"}):
            with self.subTest(recipe=bad_recipe):
                dom = _run_workbench_dom_probe(render_workbench_html(empty_report, recipe=bad_recipe))
                self.assertIn("UNVERIFIED VERDICT", dom["recipeBannerHtml"])
                self.assertNotIn("Candidate passed all 6 gates", dom["recipeBannerHtml"])

        go_dom = _run_workbench_dom_probe(render_workbench_html(_sample_report_dict(), recipe={"verdict": "GO"}))
        self.assertIn("GO: Candidate passed all 6 gates", go_dom["recipeBannerHtml"])


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

    def test_days_trailing_window_uses_final_close_boundary_exact_24_bars(self):
        # P2R1-01: 48 hourly bars trimmed to --days 1 must retain exactly 24 bars (not 25)
        with TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "candles_48h.csv"
            candles = _synthetic_candles(48)
            write_csv(candles, csv_path)
            series_1d = lab_cli._load_bar_series_readonly(
                symbol="MU-USDT-SWAP",
                interval="1h",
                data_dir=Path(tmp) / "unused_live",
                candles_file=csv_path,
                generation_id=None,
                days=1,
            )
            self.assertEqual(24, len(series_1d))
            self.assertEqual(candles[24].open_time_ms, series_1d.open_times[0])
            self.assertEqual(candles[47].open_time_ms, series_1d.open_times[-1])

            series_2d = lab_cli._load_bar_series_readonly(
                symbol="MU-USDT-SWAP",
                interval="1h",
                data_dir=Path(tmp) / "unused_live",
                candles_file=csv_path,
                generation_id=None,
                days=2,
            )
            self.assertEqual(48, len(series_2d))

    def test_cli_guards_data_dir_with_candles_file_and_rejects_input_overwrites(self):
        # P2R1-02 & P2R1-03
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "live"
            data_dir.mkdir()
            pointer = data_dir / "current.json"
            pointer.write_text('{"schema_version":1}', encoding="utf-8")

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

            data_before = _snapshot_tree(data_dir)
            csv_before = csv_path.read_bytes()
            funding_before = funding_path.read_bytes()

            blocked_cases = (
                ["discover", "--data-dir", str(data_dir), "--candles-file", str(csv_path), "--output-json", str(pointer)],
                ["discover", "--data-dir", str(data_dir), "--candles-file", str(csv_path), "--output-html", str(data_dir / "x.html")],
                ["discover", "--data-dir", str(data_dir), "--candles-file", str(csv_path), "--recipe-out", str(data_dir / "r.json")],
                ["discover", "--data-dir", str(data_dir), "--candles-file", str(csv_path), "--output-json", str(csv_path)],
                ["discover", "--data-dir", str(data_dir), "--candles-file", str(csv_path), "--output-html", str(csv_path)],
                ["discover", "--data-dir", str(data_dir), "--candles-file", str(csv_path), "--funding-file", str(funding_path), "--output-json", str(funding_path)],
                ["discover", "--data-dir", str(data_dir), "--candles-file", str(csv_path), "--funding-file", str(funding_path), "--recipe-out", str(funding_path)],
            )
            for argv in blocked_cases:
                with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exc:
                        lab_cli.main(argv)
                    self.assertEqual(2, exc.exception.code)

            self.assertEqual(data_before, _snapshot_tree(data_dir))
            self.assertEqual(csv_before, csv_path.read_bytes())
            self.assertEqual(funding_before, funding_path.read_bytes())

            # External output path + provenance/effective_config check (P2R1-02 + P2R1-04)
            out_json = root / "out" / "report.json"
            with redirect_stdout(io.StringIO()):
                rc = lab_cli.main(
                    [
                        "discover",
                        "--data-dir",
                        str(data_dir),
                        "--candles-file",
                        str(csv_path),
                        "--funding-file",
                        str(funding_path),
                        "--families",
                        "ts_momentum",
                        "--overlays",
                        "none",
                        "--folds",
                        "2",
                        "--purge-bars",
                        "6",
                        "--fee-profile",
                        "limit",
                        "--output-json",
                        str(out_json),
                    ]
                )
            self.assertEqual(0, rc)
            self.assertEqual(data_before, _snapshot_tree(data_dir))
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            prov = payload["provenance"]
            self.assertEqual(hashlib.sha256(csv_before).hexdigest(), prov["dataset_sha256"])
            self.assertEqual(hashlib.sha256(funding_before).hexdigest(), prov["funding_sha256"])
            self.assertTrue(prov["code_revision"] and prov["code_revision"] != "unknown")
            self.assertEqual("limit", prov["fee_profile"])
            self.assertEqual("candles_file", prov["data_source"])
            eff = payload["effective_config"]
            self.assertEqual(6, eff["purge_bars"])
            self.assertEqual(2.0, eff["cost_model"]["taker_fee_bps"])
            self.assertEqual(
                ["slippage_10bp", "funding_0pct_apr", "funding_15pct_apr", "funding_30pct_apr"],
                eff["stress_scenarios"],
            )

    def test_cli_reads_candles_and_funding_once_and_pins_provenance_hash_to_parsed_snapshot(self):
        # P2R2-01: single snapshot capture + hash eliminates TOCTOU between hashing and parsing
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

            expected_csv_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
            expected_funding_sha = hashlib.sha256(funding_path.read_bytes()).hexdigest()

            real_snapshot = lab_cli._read_file_snapshot
            reads: list[Path] = []

            def snapshot_then_corrupt_disk(path: Path) -> tuple[bytes, str]:
                resolved = path.resolve()
                reads.append(resolved)
                data, digest = real_snapshot(path)
                # Mutate the on-disk file immediately after the single snapshot read:
                # if discovery re-read the file later, parsing would crash on corrupted bytes!
                path.write_text("CORRUPTED_AFTER_SNAPSHOT", encoding="utf-8")
                return data, digest

            out_json = root / "out" / "single_read.json"
            with (
                patch.object(lab_cli, "_read_file_snapshot", side_effect=snapshot_then_corrupt_disk),
                patch.object(FundingSeries, "from_json_file", side_effect=AssertionError("must use from_json_text")),
                redirect_stdout(io.StringIO()),
            ):
                rc = lab_cli.main(
                    [
                        "discover",
                        "--data-dir",
                        str(root / "unused_live"),
                        "--candles-file",
                        str(csv_path),
                        "--funding-file",
                        str(funding_path),
                        "--families",
                        "ts_momentum",
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
            self.assertEqual([csv_path.resolve(), funding_path.resolve()], reads)
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(expected_csv_sha, payload["provenance"]["dataset_sha256"])
            self.assertEqual(expected_funding_sha, payload["provenance"]["funding_sha256"])
            self.assertTrue(payload["funding_verified"])

            # Verify invalid UTF-8 bytes in input fail cleanly with exit code 2
            bad_csv = root / "bad_utf8.csv"
            bad_csv.write_bytes(b"\xff\xfe\x00invalid")
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    lab_cli.main(["discover", "--data-dir", str(root / "unused_live"), "--candles-file", str(bad_csv)])
            self.assertEqual(2, exc.exception.code)


if __name__ == "__main__":
    unittest.main()
