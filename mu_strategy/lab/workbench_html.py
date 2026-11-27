"""Standalone zero-CDN offline HTML workbench renderer for Phase 2 strategy discovery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import html
import json
import math
from pathlib import Path
from typing import Any


def _sanitize_json(value: Any) -> Any:
    """Recursively replace non-finite floats with None and normalize containers for strict JSON."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {str(k): _sanitize_json(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_sanitize_json(v) for v in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _sanitize_json(value.to_dict())
    return str(value)


def _safe_json_for_script(obj: Any) -> str:
    """Serialize JSON safely for inline HTML <script> tags (prevents </script> and XSS breakout)."""
    raw = json.dumps(_sanitize_json(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _extract_report_dict(report: Any) -> dict[str, Any]:
    """Accept a DiscoveryReport instance or a pre-serialized Mapping."""
    if hasattr(report, "to_dict") and callable(report.to_dict):
        raw = report.to_dict()
    elif isinstance(report, Mapping):
        raw = dict(report)
    else:
        raise TypeError(f"report must be a DiscoveryReport or Mapping, got {type(report).__name__}")
    sanitized = _sanitize_json(raw)
    if not isinstance(sanitized, dict):
        raise ValueError("serialized report must be a JSON object")
    return sanitized


def _fallback_winner_recipe(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a dry-run-only recipe from the report payload when none is explicitly supplied."""
    existing = payload.get("winner_recipe")
    if isinstance(existing, Mapping):
        recipe = dict(existing)
        recipe["dry_run_only"] = True
        recipe["live_orders_allowed"] = False
        return recipe

    candidates = payload.get("candidates")
    cand_list = candidates if isinstance(candidates, list) else []
    selected: Mapping[str, Any] | None = None
    for verdict_target in ("GO", "CONDITIONAL"):
        for item in cand_list:
            if isinstance(item, Mapping) and _candidate_verdict(item) == verdict_target:
                selected = item
                break
        if selected is not None:
            break
    if selected is None and cand_list and isinstance(cand_list[0], Mapping):
        selected = cand_list[0]

    verdict = _candidate_verdict(selected) if selected is not None else "NO_GO"
    warning = None if verdict == "GO" else (
        "CONDITIONAL candidate: review WARN gates before paper-trading promotion."
        if verdict == "CONDITIONAL"
        else "NO_GO: no candidate passed all 6 Phase 2 discovery gates. Research reference only."
    )
    return {
        "schema_version": str(payload.get("schema_version") or "phase2.discovery.v1"),
        "dry_run_only": True,
        "live_orders_allowed": False,
        "symbol": payload.get("symbol"),
        "interval": payload.get("interval"),
        "verdict": verdict,
        "warning": warning,
        "candidate_id": selected.get("candidate_id") if selected else None,
        "family_id": selected.get("family_id") if selected else None,
        "display_name": selected.get("display_name") if selected else None,
        "overlay_preset": selected.get("overlay_preset") if selected else None,
        "params": dict(selected.get("params", {})) if selected and isinstance(selected.get("params"), Mapping) else {},
    }


def _candidate_verdict(candidate: Mapping[str, Any]) -> str:
    gates = candidate.get("gates")
    if isinstance(gates, Mapping) and gates.get("verdict"):
        return str(gates["verdict"])
    if candidate.get("verdict"):
        return str(candidate["verdict"])
    return "NO_GO"


def _escape_html(text: str) -> str:
    return html.escape(text, quote=True).replace("\u2028", "&#8232;").replace("\u2029", "&#8233;")


def render_workbench_html(
    report: Any,
    *,
    recipe: Mapping[str, Any] | None = None,
) -> str:
    """Render a standalone zero-CDN interactive HTML Strategy Discovery Workbench."""
    payload = _extract_report_dict(report)
    resolved_recipe = _sanitize_json(dict(recipe)) if recipe is not None else _fallback_winner_recipe(payload)
    if isinstance(resolved_recipe, dict):
        resolved_recipe["dry_run_only"] = True
        resolved_recipe["live_orders_allowed"] = False
    payload["winner_recipe"] = resolved_recipe

    symbol = _escape_html(str(payload.get("symbol") or "UNKNOWN"))
    interval = _escape_html(str(payload.get("interval") or "1h"))
    schema = _escape_html(str(payload.get("schema_version") or "phase2.discovery.v1"))
    data_json = _safe_json_for_script(payload)

    return _HTML_TEMPLATE.replace("__TITLE_SYMBOL__", symbol).replace("__TITLE_INTERVAL__", interval).replace(
        "__SCHEMA_VERSION__", schema
    ).replace("__REPORT_JSON__", data_json)


def write_workbench_html(
    report: Any,
    output_path: Path | str,
    *,
    recipe: Mapping[str, Any] | None = None,
) -> Path:
    """Render and write the standalone offline HTML workbench to ``output_path``."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_workbench_html(report, recipe=recipe), encoding="utf-8")
    return target


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Phase 2 Strategy Discovery Workbench — __TITLE_SYMBOL__ (__TITLE_INTERVAL__)</title>
<style>
  :root[data-theme="dark"] {
    --bg: #0b0f19; --card: #111827; --card-alt: #161f33; --hover: #1e293b;
    --text: #f1f5f9; --muted: #94a3b8; --dim: #64748b; --border: #1e293b; --border-hi: #334155;
    --accent: #38bdf8; --pos: #10b981; --pos-bg: rgba(16,185,129,.15);
    --warn: #f59e0b; --warn-bg: rgba(245,158,11,.15); --neg: #f43f5e; --neg-bg: rgba(244,63,94,.15);
    --grid: rgba(148,163,184,.14); --tip: rgba(15,23,42,.96); --code-bg: #0a101d;
  }
  :root[data-theme="light"] {
    --bg: #f8fafc; --card: #ffffff; --card-alt: #f1f5f9; --hover: #e2e8f0;
    --text: #0f172a; --muted: #475569; --dim: #64748b; --border: #e2e8f0; --border-hi: #cbd5e1;
    --accent: #0284c7; --pos: #059669; --pos-bg: rgba(5,150,105,.12);
    --warn: #d97706; --warn-bg: rgba(217,119,6,.12); --neg: #e11d48; --neg-bg: rgba(225,29,72,.12);
    --grid: rgba(100,116,139,.18); --tip: rgba(255,255,255,.97); --code-bg: #f1f5f9;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font: 13.5px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 20px 24px 40px; max-width: 1520px; margin: 0 auto; }
  .header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 14px; padding-bottom: 16px; border-bottom: 1px solid var(--border); margin-bottom: 18px; }
  .header h1 { font-size: 21px; font-weight: 700; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .sub { color: var(--muted); font-size: 12.5px; margin-top: 4px; }
  .pill { display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: .02em; border: 1px solid transparent; }
  .pill-accent { background: rgba(56,189,248,.14); color: var(--accent); border-color: rgba(56,189,248,.35); }
  .pill-pos, .st-PASS, .vd-GO { background: var(--pos-bg); color: var(--pos); border-color: rgba(16,185,129,.35); }
  .pill-warn, .st-WARN, .vd-CONDITIONAL { background: var(--warn-bg); color: var(--warn); border-color: rgba(245,158,11,.35); }
  .pill-neg, .st-FAIL, .vd-NO_GO { background: var(--neg-bg); color: var(--neg); border-color: rgba(244,63,94,.35); }
  .btn { background: var(--card); color: var(--text); border: 1px solid var(--border-hi); padding: 6px 12px; border-radius: 7px; font-size: 12px; font-weight: 600; cursor: pointer; }
  .btn:hover, .btn.active { border-color: var(--accent); color: var(--accent); background: var(--hover); }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin-bottom: 18px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .kpi-label { color: var(--muted); font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 4px; }
  .kpi-val { font-size: 19px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .kpi-sub { color: var(--muted); font-size: 12px; margin-top: 3px; font-variant-numeric: tabular-nums; }
  .section { margin-bottom: 18px; }
  .sec-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 10px; }
  .sec-title { font-size: 15px; font-weight: 700; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .leg-item { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 6px; font-size: 11.5px; cursor: pointer; background: var(--card-alt); border: 1px solid var(--border); color: var(--text); user-select: none; }
  .leg-item.off { opacity: .4; text-decoration: line-through; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
  .chart-wrap { position: relative; width: 100%; }
  svg.chart { width: 100%; display: block; overflow: visible; }
  .tooltip { position: absolute; pointer-events: none; background: var(--tip); border: 1px solid var(--border-hi); border-radius: 8px; padding: 8px 10px; font-size: 11.5px; min-width: 210px; z-index: 20; display: none; box-shadow: 0 8px 20px rgba(0,0,0,.35); }
  .tip-row { display: flex; justify-content: space-between; gap: 12px; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; font-variant-numeric: tabular-nums; }
  th, td { padding: 8px 9px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; cursor: pointer; user-select: none; background: var(--card-alt); position: sticky; top: 0; }
  th:hover { color: var(--accent); }
  tr.cand-row { cursor: pointer; }
  tr.cand-row:hover { background: var(--hover); }
  tr.cand-row.selected { background: rgba(56,189,248,.10); }
  tr.detail-row td { background: var(--card-alt); white-space: normal; padding: 12px 14px; }
  .detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
  .gate-list { list-style: none; display: flex; flex-direction: column; gap: 5px; }
  .gate-item { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
  .num { text-align: right; }
  .pos { color: var(--pos); } .neg { color: var(--neg); } .muted { color: var(--muted); }
  .search-input { background: var(--card-alt); color: var(--text); border: 1px solid var(--border-hi); border-radius: 7px; padding: 5px 10px; font-size: 12px; min-width: 190px; }
  .bottom-grid { display: grid; grid-template-columns: 1.1fr 1fr; gap: 16px; }
  @media (max-width: 960px) { .bottom-grid { grid-template-columns: 1fr; } }
  .attr-row { display: grid; grid-template-columns: 130px 1fr 105px; align-items: center; gap: 10px; margin: 8px 0; font-size: 12.5px; }
  .bar-track { position: relative; height: 18px; background: var(--card-alt); border-radius: 4px; overflow: hidden; border: 1px solid var(--border); }
  .bar-zero { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: var(--border-hi); }
  .bar-fill { position: absolute; top: 2px; bottom: 2px; border-radius: 3px; }
  pre.recipe { background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-x: auto; max-height: 310px; color: var(--text); }
  .warn-banner { margin-bottom: 10px; padding: 8px 10px; border-radius: 7px; font-size: 12px; border: 1px solid rgba(245,158,11,.35); background: var(--warn-bg); color: var(--warn); }
  .warn-banner.nogo { border-color: rgba(244,63,94,.35); background: var(--neg-bg); color: var(--neg); }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>
      <span>Phase 2 Strategy Discovery Workbench</span>
      <span class="pill pill-accent">__TITLE_SYMBOL__ · __TITLE_INTERVAL__</span>
      <span class="pill pill-warn">DRY-RUN ONLY</span>
    </h1>
    <div class="sub" id="headerSub">Schema __SCHEMA_VERSION__ · Standalone Zero-CDN Offline Report</div>
  </div>
  <div class="controls">
    <button class="btn" id="themeBtn" type="button">Light Theme</button>
  </div>
</div>

<div class="kpi-grid" id="kpiGrid"></div>

<div class="card section">
  <div class="sec-head">
    <div class="sec-title">OOS Stitched Equity Curve &amp; Synchronized Underwater Drawdown</div>
    <div class="controls">
      <button class="btn active" id="scaleLinearBtn" type="button">Linear</button>
      <button class="btn" id="scaleLogBtn" type="button">Log</button>
      <button class="btn" id="resetLegendBtn" type="button">Reset Series</button>
    </div>
  </div>
  <div class="legend" id="chartLegend"></div>
  <div class="chart-wrap" id="chartWrap">
    <svg class="chart" id="equitySvg" viewBox="0 0 1120 270"></svg>
    <svg class="chart" id="ddSvg" viewBox="0 0 1120 135"></svg>
    <div class="tooltip" id="chartTip"></div>
  </div>
</div>

<div class="card section">
  <div class="sec-head">
    <div class="sec-title">6-Gate Go/No-Go Matrix <span class="muted" id="candCount"></span></div>
    <div class="controls">
      <button class="btn active" data-filter="ALL" type="button">All</button>
      <button class="btn" data-filter="GO" type="button">GO</button>
      <button class="btn" data-filter="CONDITIONAL" type="button">CONDITIONAL</button>
      <button class="btn" data-filter="NO_GO" type="button">NO_GO</button>
      <input class="search-input" id="searchBox" type="search" placeholder="Filter family / overlay / params..." />
    </div>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th data-sort="rank">#</th>
          <th data-sort="name">Strategy / Overlay</th>
          <th class="num" data-sort="total_return">OOS Ret</th>
          <th class="num" data-sort="max_drawdown">MaxDD</th>
          <th class="num" data-sort="sharpe">Sharpe</th>
          <th class="num" data-sort="dsr">DSR</th>
          <th class="num" data-sort="calmar">Calmar</th>
          <th class="num" data-sort="trades">Trades</th>
          <th class="num" data-sort="liquidations">Liq</th>
          <th>G1</th><th>G2</th><th>G3</th><th>G4</th><th>G5</th><th>G6</th>
          <th data-sort="verdict">Verdict</th>
        </tr>
      </thead>
      <tbody id="matrixBody"></tbody>
    </table>
  </div>
</div>

<div class="bottom-grid">
  <div class="card">
    <div class="sec-head">
      <div class="sec-title">PnL &amp; Cost Attribution <span class="muted" id="attrTarget"></span></div>
    </div>
    <div id="attrPanel"></div>
  </div>
  <div class="card">
    <div class="sec-head">
      <div class="sec-title">Dry-Run Winner Recipe JSON</div>
      <button class="btn" id="copyRecipeBtn" type="button">Copy JSON</button>
    </div>
    <div id="recipeBanner"></div>
    <pre class="recipe" id="recipeBox"></pre>
  </div>
</div>

<script id="discovery-data" type="application/json">__REPORT_JSON__</script>
<script>
(function () {
  "use strict";
  const REPORT = JSON.parse(document.getElementById("discovery-data").textContent || "{}");
  const PALETTE = ["#38bdf8", "#10b981", "#f59e0b", "#a78bfa", "#f43f5e", "#2dd4bf", "#fb923c", "#60a5fa", "#e879f9", "#84cc16"];
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtPct = (v, signed) => { const n = num(v); if (n === null) return "—"; return (signed && n > 0 ? "+" : "") + (n * 100).toFixed(2) + "%"; };
  const fmtNum = (v, d = 2) => { const n = num(v); return n === null ? "—" : n.toFixed(d); };
  const fmtUsd = (v, signed) => { const n = num(v); if (n === null) return "—"; const s = signed && n > 0 ? "+" : (n < 0 ? "-" : ""); return s + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
  const fmtInt = (v) => { const n = num(v); return n === null ? "—" : Math.round(n).toString(); };
  const fmtDate = (ms) => { const n = num(ms); if (n === null) return "—"; const d = new Date(n); return Number.isNaN(d.getTime()) ? "—" : d.toISOString().slice(0, 10); };
  const pick = (obj, keys) => { if (!obj || typeof obj !== "object") return null; for (const k of keys) { if (obj[k] !== undefined && obj[k] !== null) return obj[k]; } return null; };

  function normCurve(raw) {
    if (!Array.isArray(raw)) return [];
    const pts = [];
    for (const p of raw) {
      let t = null, eq = null;
      if (Array.isArray(p) && p.length >= 2) { t = num(p[0]); eq = num(p[1]); }
      else if (p && typeof p === "object") { t = num(pick(p, ["ts_ms", "timestamp_ms", "t", "ts"])); eq = num(pick(p, ["equity", "eq", "value", "v"])); }
      if (t !== null && eq !== null) pts.push({ t, eq });
    }
    pts.sort((a, b) => a.t - b.t);
    let peak = -Infinity;
    for (const pt of pts) {
      if (pt.eq > peak) peak = pt.eq;
      pt.dd = peak > 0 ? Math.min(0, (pt.eq - peak) / peak) : 0;
    }
    return pts;
  }

  function normCandidate(raw, idx) {
    const m = (raw && typeof raw.metrics === "object" && raw.metrics) ? raw.metrics : (raw || {});
    const g = (raw && typeof raw.gates === "object" && raw.gates) ? raw.gates : {};
    const a = (raw && typeof raw.attribution === "object" && raw.attribution) ? raw.attribution : {};
    const checks = Array.isArray(g.checks) ? g.checks : [];
    const checkMap = {};
    for (const c of checks) { if (c && c.gate_id) checkMap[String(c.gate_id)] = c; }
    const family = String(raw.family_id || raw.family || "unknown");
    const overlay = String(raw.overlay_preset || raw.overlay || "none");
    return {
      rank: num(raw.rank) !== null ? raw.rank : idx + 1,
      id: String(raw.candidate_id || (family + ":" + overlay + ":" + (idx + 1))),
      family, display: String(raw.display_name || family), overlay,
      params: (raw.params && typeof raw.params === "object") ? raw.params : {},
      total_return: num(pick(m, ["total_return", "oos_total_return", "return"])),
      max_drawdown: num(pick(m, ["max_drawdown", "oos_max_drawdown", "max_dd"])),
      sharpe: num(pick(m, ["sharpe", "oos_sharpe", "sharpe_ratio"])),
      calmar: num(pick(m, ["calmar", "oos_calmar", "calmar_ratio"])),
      trades: num(pick(m, ["trades", "trade_count", "oos_trades", "n_trades"])),
      liquidations: num(pick(m, ["liquidations", "liquidation_count", "n_liquidations"])) ?? 0,
      dsr: num(pick(g, ["dsr"])) ?? num(pick(m, ["dsr"])),
      verdict: String(g.verdict || raw.verdict || "NO_GO"),
      passed_count: num(g.passed_count) ?? checks.filter((c) => c.status === "PASS").length,
      failed_gates: Array.isArray(g.failed_gates) ? g.failed_gates : [],
      checks, checkMap,
      folds: Array.isArray(raw.folds) ? raw.folds : [],
      attribution: {
        price_pnl: num(pick(a, ["gross_pnl", "price_pnl", "gross_price_pnl", "trading_pnl"])) ?? 0,
        funding_pnl: num(pick(a, ["funding_pnl", "net_funding_pnl", "real_funding_pnl"])) ?? 0,
        fees: -Math.abs(num(pick(a, ["fees", "fee_cost", "fees_paid", "total_fees"])) ?? 0),
        slippage: -Math.abs(num(pick(a, ["slippage", "slippage_cost", "total_slippage"])) ?? 0),
        net_pnl: num(pick(a, ["net_pnl", "total_pnl"])),
      },
      curve: normCurve(raw.curve),
    };
  }

  const candidates = (Array.isArray(REPORT.candidates) ? REPORT.candidates : []).map(normCandidate);
  const benchmarks = (REPORT.benchmarks && typeof REPORT.benchmarks === "object") ? REPORT.benchmarks : {};
  const spotBench = benchmarks.spot_1x || {};
  const perpBench = benchmarks.perp_1x || {};
  const spotMetrics = (spotBench.metrics && typeof spotBench.metrics === "object") ? spotBench.metrics : spotBench;

  const state = {
    theme: "dark",
    logScale: false,
    filter: "ALL",
    search: "",
    sortKey: "rank",
    sortDir: 1,
    expandedId: null,
    selectedId: candidates[0] ? candidates[0].id : null,
    series: [],
  };

  function initSeries() {
    const out = [];
    const topWithCurve = candidates.filter((c) => c.curve.length >= 2).slice(0, 10);
    topWithCurve.forEach((c, i) => {
      out.push({
        key: "cand:" + c.id,
        label: "#" + c.rank + " " + c.display + " (" + c.overlay + ")",
        color: PALETTE[i % PALETTE.length],
        dash: "",
        visible: i < 5,
        pts: c.curve,
      });
    });
    const spotPts = normCurve(spotBench.curve);
    if (spotPts.length >= 2) {
      out.push({ key: "bench:spot_1x", label: "Spot 1x Benchmark", color: "#eab308", dash: "6,4", visible: true, pts: spotPts });
    }
    const perpPts = normCurve(perpBench.curve);
    if (perpPts.length >= 2) {
      out.push({ key: "bench:perp_1x", label: "Perp 1x Benchmark", color: "#94a3b8", dash: "2,4", visible: true, pts: perpPts });
    }
    state.series = out;
  }

  function renderHeaderAndKpis() {
    const goCount = candidates.filter((c) => c.verdict === "GO").length;
    const condCount = candidates.filter((c) => c.verdict === "CONDITIONAL").length;
    const nogoCount = candidates.filter((c) => c.verdict === "NO_GO").length;
    const best = candidates[0] || null;
    const spotRet = num(pick(spotMetrics, ["total_return", "oos_total_return", "return"]));
    const spotDd = num(pick(spotMetrics, ["max_drawdown", "oos_max_drawdown", "max_dd"]));
    const oosWindow = (num(REPORT.oos_start_ms) && num(REPORT.oos_end_ms))
      ? (fmtDate(REPORT.oos_start_ms) + " → " + fmtDate(REPORT.oos_end_ms))
      : "Out-of-Sample Window";
    document.getElementById("headerSub").textContent =
      "Schema " + (REPORT.schema_version || "phase2.discovery.v1") + " · " + oosWindow + " · Zero-CDN Offline Workbench";

    const rtMs = num(REPORT.runtime_ms);
    const rtText = rtMs === null ? "—" : (rtMs >= 1000 ? (rtMs / 1000).toFixed(2) + " s" : rtMs.toFixed(0) + " ms");
    const kpis = [
      { label: "Instrument", val: String(REPORT.symbol || "—"), sub: "Interval: " + String(REPORT.interval || "—") },
      { label: "OOS Window & Folds", val: fmtNum(REPORT.oos_days, 1) + "d", sub: fmtInt(REPORT.n_folds) + " purged WF folds" },
      { label: "Total Trials", val: fmtInt(REPORT.n_trials ?? candidates.length), sub: candidates.length + " candidates ranked" },
      { label: "6-Gate Verdicts", val: goCount + " GO · " + condCount + " COND", sub: nogoCount + " NO_GO rejected" },
      { label: "Best vs Spot 1x", val: best ? fmtPct(best.total_return, true) : "—", sub: "Spot 1x: " + fmtPct(spotRet, true) + " (DD " + fmtPct(spotDd, false) + ")" },
      { label: "Discovery Runtime", val: rtText, sub: best ? ("Top: " + best.display + " [" + best.verdict + "]") : "No candidates" },
    ];
    document.getElementById("kpiGrid").innerHTML = kpis.map((k) =>
      '<div class="card"><div class="kpi-label">' + esc(k.label) + '</div><div class="kpi-val">' + esc(k.val) + '</div><div class="kpi-sub">' + esc(k.sub) + "</div></div>"
    ).join("");
  }

  function renderLegend() {
    const el = document.getElementById("chartLegend");
    if (!state.series.length) { el.innerHTML = '<span class="muted">No curve series available.</span>'; return; }
    el.innerHTML = state.series.map((s) =>
      '<button type="button" class="leg-item' + (s.visible ? "" : " off") + '" data-key="' + esc(s.key) + '">' +
      '<span class="swatch" style="background:' + esc(s.color) + '"></span><span>' + esc(s.label) + "</span></button>"
    ).join("");
    el.querySelectorAll(".leg-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const s = state.series.find((x) => x.key === btn.dataset.key);
        if (s) { s.visible = !s.visible; renderLegend(); renderCharts(); }
      });
    });
  }

  function renderCharts() {
    const eqSvg = document.getElementById("equitySvg");
    const ddSvg = document.getElementById("ddSvg");
    const visible = state.series.filter((s) => s.visible && s.pts.length >= 2);
    if (!visible.length) {
      eqSvg.innerHTML = '<text x="560" y="135" text-anchor="middle" fill="#94a3b8" font-size="13">Select at least one series with equity curve data</text>';
      ddSvg.innerHTML = "";
      return;
    }
    const W = 1120, H1 = 270, H2 = 135, L = 66, R = 18, T1 = 16, B1 = 24, T2 = 10, B2 = 26;
    let tMin = Infinity, tMax = -Infinity, yMin = Infinity, yMax = -Infinity, ddMin = -0.05;
    for (const s of visible) {
      for (const p of s.pts) {
        if (p.t < tMin) tMin = p.t;
        if (p.t > tMax) tMax = p.t;
        const yv = state.logScale ? Math.log10(Math.max(p.eq, 1e-6)) : p.eq;
        if (yv < yMin) yMin = yv;
        if (yv > yMax) yMax = yv;
        if (p.dd < ddMin) ddMin = p.dd;
      }
    }
    if (tMax <= tMin) tMax = tMin + 86400000;
    if (yMax <= yMin) { yMax += 1; yMin -= 1; }
    const pad = (yMax - yMin) * 0.06;
    yMin -= pad; yMax += pad;
    const xOf = (t) => L + ((t - tMin) / (tMax - tMin)) * (W - L - R);
    const yEqOf = (eq) => {
      const v = state.logScale ? Math.log10(Math.max(eq, 1e-6)) : eq;
      return T1 + ((yMax - v) / (yMax - yMin)) * (H1 - T1 - B1);
    };
    const yDdOf = (dd) => T2 + ((0 - dd) / Math.max(0.01, -ddMin)) * (H2 - T2 - B2);

    let eqHtml = "";
    for (let i = 0; i <= 4; i++) {
      const frac = i / 4, y = T1 + frac * (H1 - T1 - B1);
      const rawV = yMax - frac * (yMax - yMin);
      const disp = state.logScale ? Math.pow(10, rawV) : rawV;
      eqHtml += '<line x1="' + L + '" y1="' + y.toFixed(1) + '" x2="' + (W - R) + '" y2="' + y.toFixed(1) + '" stroke="var(--grid)" />';
      eqHtml += '<text x="' + (L - 8) + '" y="' + (y + 4).toFixed(1) + '" text-anchor="end" fill="var(--muted)" font-size="10.5">' + esc(fmtUsd(disp, false)) + "</text>";
    }
    for (const s of visible) {
      const d = s.pts.map((p, i) => (i ? "L" : "M") + xOf(p.t).toFixed(1) + "," + yEqOf(p.eq).toFixed(1)).join(" ");
      eqHtml += '<path d="' + d + '" fill="none" stroke="' + esc(s.color) + '" stroke-width="' + (s.dash ? 1.8 : 2.1) + '"' + (s.dash ? ' stroke-dasharray="' + s.dash + '"' : "") + " />";
    }
    eqHtml += '<line id="eqCross" x1="0" y1="' + T1 + '" x2="0" y2="' + (H1 - B1) + '" stroke="var(--accent)" stroke-dasharray="3,3" visibility="hidden" />';
    eqHtml += '<rect id="eqHit" x="' + L + '" y="' + T1 + '" width="' + (W - L - R) + '" height="' + (H1 - T1 - B1) + '" fill="transparent" />';
    eqSvg.innerHTML = eqHtml;

    let ddHtml = "";
    for (let i = 0; i <= 3; i++) {
      const frac = i / 3, y = T2 + frac * (H2 - T2 - B2), dv = frac * ddMin;
      ddHtml += '<line x1="' + L + '" y1="' + y.toFixed(1) + '" x2="' + (W - R) + '" y2="' + y.toFixed(1) + '" stroke="var(--grid)" />';
      ddHtml += '<text x="' + (L - 8) + '" y="' + (y + 4).toFixed(1) + '" text-anchor="end" fill="var(--muted)" font-size="10.5">' + esc((dv * 100).toFixed(1) + "%") + "</text>";
    }
    for (let i = 0; i <= 5; i++) {
      const t = tMin + (i / 5) * (tMax - tMin), x = xOf(t);
      ddHtml += '<text x="' + x.toFixed(1) + '" y="' + (H2 - 6) + '" text-anchor="middle" fill="var(--muted)" font-size="10.5">' + esc(fmtDate(t)) + "</text>";
    }
    for (const s of visible) {
      const d = s.pts.map((p, i) => (i ? "L" : "M") + xOf(p.t).toFixed(1) + "," + yDdOf(p.dd).toFixed(1)).join(" ");
      ddHtml += '<path d="' + d + '" fill="none" stroke="' + esc(s.color) + '" stroke-width="1.6"' + (s.dash ? ' stroke-dasharray="' + s.dash + '"' : "") + " />";
    }
    ddHtml += '<line id="ddCross" x1="0" y1="' + T2 + '" x2="0" y2="' + (H2 - B2) + '" stroke="var(--accent)" stroke-dasharray="3,3" visibility="hidden" />';
    ddHtml += '<rect id="ddHit" x="' + L + '" y="' + T2 + '" width="' + (W - L - R) + '" height="' + (H2 - T2 - B2) + '" fill="transparent" />';
    ddSvg.innerHTML = ddHtml;

    const tip = document.getElementById("chartTip");
    const eqCross = document.getElementById("eqCross");
    const ddCross = document.getElementById("ddCross");
    const nearest = (pts, targetT) => {
      let lo = 0, hi = pts.length - 1;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (pts[mid].t < targetT) lo = mid + 1; else hi = mid; }
      return (lo > 0 && Math.abs(pts[lo - 1].t - targetT) < Math.abs(pts[lo].t - targetT)) ? pts[lo - 1] : pts[lo];
    };
    function onMove(ev, svg) {
      const rect = svg.getBoundingClientRect();
      const sx = ((ev.clientX - rect.left) / Math.max(rect.width, 1)) * W;
      const clamped = Math.max(L, Math.min(W - R, sx));
      const targetT = tMin + ((clamped - L) / (W - L - R)) * (tMax - tMin);
      eqCross.setAttribute("x1", clamped.toFixed(1)); eqCross.setAttribute("x2", clamped.toFixed(1)); eqCross.setAttribute("visibility", "visible");
      ddCross.setAttribute("x1", clamped.toFixed(1)); ddCross.setAttribute("x2", clamped.toFixed(1)); ddCross.setAttribute("visibility", "visible");
      let rows = '<div style="font-weight:700;margin-bottom:4px">' + esc(fmtDate(targetT)) + "</div>";
      for (const s of visible) {
        const pt = nearest(s.pts, targetT);
        const base = s.pts[0].eq;
        const ret = base > 0 ? (pt.eq - base) / base : null;
        rows += '<div class="tip-row"><span style="color:' + esc(s.color) + '">' + esc(s.label) + "</span><span>" + esc(fmtUsd(pt.eq, false)) + " (" + esc(fmtPct(ret, true)) + " · DD " + esc(fmtPct(pt.dd, false)) + ")</span></div>";
      }
      tip.innerHTML = rows;
      tip.style.display = "block";
      const wrapRect = document.getElementById("chartWrap").getBoundingClientRect();
      const leftPx = Math.min(wrapRect.width - 260, Math.max(12, ev.clientX - wrapRect.left + 14));
      tip.style.left = leftPx + "px";
      tip.style.top = "12px";
    }
    function onLeave() {
      eqCross.setAttribute("visibility", "hidden"); ddCross.setAttribute("visibility", "hidden"); tip.style.display = "none";
    }
    ["eqHit", "ddHit"].forEach((id) => {
      const hit = document.getElementById(id);
      const svg = id === "eqHit" ? eqSvg : ddSvg;
      hit.addEventListener("mousemove", (ev) => onMove(ev, svg));
      hit.addEventListener("mouseleave", onLeave);
    });
  }

  function renderMatrix() {
    const q = state.search.trim().toLowerCase();
    const rows = candidates.filter((c) => {
      if (state.filter !== "ALL" && c.verdict !== state.filter) return false;
      if (!q) return true;
      return (c.display + " " + c.family + " " + c.overlay + " " + JSON.stringify(c.params)).toLowerCase().includes(q);
    });
    const order = { GO: 0, CONDITIONAL: 1, NO_GO: 2 };
    rows.sort((a, b) => {
      const k = state.sortKey;
      let va = k === "name" ? (a.display + a.overlay) : (k === "verdict" ? (order[a.verdict] ?? 9) : a[k]);
      let vb = k === "name" ? (b.display + b.overlay) : (k === "verdict" ? (order[b.verdict] ?? 9) : b[k]);
      if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * state.sortDir;
      va = num(va) ?? -1e18; vb = num(vb) ?? -1e18;
      return (va - vb) * state.sortDir;
    });
    document.getElementById("candCount").textContent = "(" + rows.length + " / " + candidates.length + ")";
    const body = document.getElementById("matrixBody");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="16" class="muted" style="text-align:center;padding:18px">No matching candidates.</td></tr>';
      return;
    }
    const gateIds = ["G1", "G2", "G3", "G4", "G5", "G6"];
    let htmlStr = "";
    for (const c of rows) {
      const gatesHtml = gateIds.map((gid) => {
        const chk = c.checkMap[gid];
        const st = chk ? String(chk.status || "FAIL") : "FAIL";
        const title = chk ? (chk.name + ": " + chk.detail) : "Not evaluated";
        return '<td><span class="pill st-' + esc(st) + '" title="' + esc(title) + '">' + esc(st) + "</span></td>";
      }).join("");
      const retCls = (c.total_return ?? 0) >= 0 ? "pos" : "neg";
      htmlStr += '<tr class="cand-row' + (state.selectedId === c.id ? " selected" : "") + '" data-id="' + esc(c.id) + '">' +
        "<td>" + esc(fmtInt(c.rank)) + "</td>" +
        "<td><strong>" + esc(c.display) + '</strong> <span class="muted">· ' + esc(c.overlay) + "</span></td>" +
        '<td class="num ' + retCls + '">' + esc(fmtPct(c.total_return, true)) + "</td>" +
        '<td class="num neg">' + esc(fmtPct(c.max_drawdown, false)) + "</td>" +
        '<td class="num">' + esc(fmtNum(c.sharpe, 2)) + "</td>" +
        '<td class="num">' + esc(fmtNum(c.dsr, 3)) + "</td>" +
        '<td class="num">' + esc(fmtNum(c.calmar, 2)) + "</td>" +
        '<td class="num">' + esc(fmtInt(c.trades)) + "</td>" +
        '<td class="num' + (c.liquidations > 0 ? " neg" : "") + '">' + esc(fmtInt(c.liquidations)) + "</td>" +
        gatesHtml +
        '<td><span class="pill vd-' + esc(c.verdict) + '">' + esc(c.verdict) + "</span></td></tr>";
      if (state.expandedId === c.id) {
        const checksHtml = c.checks.map((chk) =>
          '<li class="gate-item"><span class="pill st-' + esc(chk.status) + '">' + esc(chk.gate_id) + " " + esc(chk.status) + "</span>" +
          "<span><strong>" + esc(chk.name) + ":</strong> " + esc(chk.detail) + "</span></li>"
        ).join("");
        const foldsHtml = c.folds.length ? c.folds.map((f, i) =>
          "<tr><td>F" + (num(f.fold_index) !== null ? f.fold_index + 1 : i + 1) + "</td>" +
          '<td class="num">' + esc(fmtPct(pick(f, ["oos_total_return", "total_return"]), true)) + "</td>" +
          '<td class="num">' + esc(fmtNum(pick(f, ["oos_sharpe", "sharpe"]), 2)) + "</td>" +
          '<td class="num">' + esc(fmtPct(pick(f, ["oos_max_drawdown", "max_drawdown"]), false)) + "</td>" +
          '<td class="num">' + esc(fmtInt(pick(f, ["oos_trades", "trades", "trade_count"]))) + "</td></tr>"
        ).join("") : '<tr><td colspan="5" class="muted">No fold details.</td></tr>';
        htmlStr += '<tr class="detail-row"><td colspan="16"><div class="detail-grid">' +
          '<div><div class="kpi-label">6-Gate Breakdown</div><ul class="gate-list">' + (checksHtml || '<li class="muted">No gate details.</li>') + "</ul></div>" +
          '<div><div class="kpi-label">Parameters &amp; Overlay</div><pre class="recipe" style="max-height:140px">' + esc(JSON.stringify({ family_id: c.family, overlay_preset: c.overlay, params: c.params }, null, 2)) + "</pre></div>" +
          '<div><div class="kpi-label">Walk-Forward OOS Folds</div><table><thead><tr><th>Fold</th><th class="num">OOS Ret</th><th class="num">Sharpe</th><th class="num">MaxDD</th><th class="num">Trades</th></tr></thead><tbody>' + foldsHtml + "</tbody></table></div>" +
          "</div></td></tr>";
      }
    }
    body.innerHTML = htmlStr;
    body.querySelectorAll("tr.cand-row").forEach((tr) => {
      tr.addEventListener("click", () => {
        const id = tr.dataset.id;
        state.selectedId = id;
        state.expandedId = state.expandedId === id ? null : id;
        renderMatrix();
        renderAttribution();
      });
    });
  }

  function renderAttribution() {
    const c = candidates.find((x) => x.id === state.selectedId) || candidates[0] || null;
    const panel = document.getElementById("attrPanel");
    const target = document.getElementById("attrTarget");
    if (!c) { target.textContent = ""; panel.innerHTML = '<div class="muted">No candidate selected.</div>'; return; }
    target.textContent = "— #" + c.rank + " " + c.display + " (" + c.overlay + ")";
    const a = c.attribution;
    const net = a.net_pnl !== null ? a.net_pnl : (a.price_pnl + a.funding_pnl + a.fees + a.slippage);
    const items = [
      { label: "Price PnL", val: a.price_pnl },
      { label: "Real 8h Funding", val: a.funding_pnl },
      { label: "Trading Fees", val: a.fees },
      { label: "Slippage & Spread", val: a.slippage },
      { label: "Net OOS PnL", val: net, bold: true },
    ];
    const maxAbs = Math.max(1, ...items.map((x) => Math.abs(x.val || 0)));
    panel.innerHTML = items.map((it) => {
      const pctW = Math.min(50, (Math.abs(it.val || 0) / maxAbs) * 48);
      const isPos = (it.val || 0) >= 0;
      const left = isPos ? 50 : (50 - pctW);
      const color = isPos ? "var(--pos)" : "var(--neg)";
      const labelHtml = it.bold ? ("<strong>" + esc(it.label) + "</strong>") : esc(it.label);
      return '<div class="attr-row"><div>' + labelHtml + '</div><div class="bar-track"><div class="bar-zero"></div>' +
        '<div class="bar-fill" style="left:' + left.toFixed(1) + "%;width:" + pctW.toFixed(1) + "%;background:" + color + '"></div></div>' +
        '<div class="num ' + (isPos ? "pos" : "neg") + '">' + esc(fmtUsd(it.val, true)) + "</div></div>";
    }).join("");
  }

  function renderRecipe() {
    const recipe = REPORT.winner_recipe || { dry_run_only: true, live_orders_allowed: false, verdict: "NO_GO" };
    const text = JSON.stringify(recipe, null, 2);
    document.getElementById("recipeBox").textContent = text;
    const banner = document.getElementById("recipeBanner");
    if (recipe.verdict === "NO_GO") {
      banner.innerHTML = '<div class="warn-banner nogo">NO_GO: No candidate cleared all 6 discovery gates. Dry-run research reference only — never deploy live.</div>';
    } else if (recipe.verdict === "CONDITIONAL") {
      banner.innerHTML = '<div class="warn-banner">CONDITIONAL: Hard gates passed; inspect WARN gates before paper-trading promotion.</div>';
    } else {
      banner.innerHTML = '<div class="warn-banner" style="background:var(--pos-bg);color:var(--pos);border-color:rgba(16,185,129,.35)">GO: Candidate passed all 6 gates. Dry-run recipe only (live_orders_allowed=false).</div>';
    }
    const btn = document.getElementById("copyRecipeBtn");
    btn.addEventListener("click", () => {
      const done = () => { btn.textContent = "Copied!"; setTimeout(() => { btn.textContent = "Copy JSON"; }, 1400); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
      } else { fallbackCopy(text, done); }
    });
  }

  function fallbackCopy(text, cb) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (_err) { /* noop */ }
    document.body.removeChild(ta); cb();
  }

  document.getElementById("themeBtn").addEventListener("click", (ev) => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", state.theme);
    ev.target.textContent = state.theme === "dark" ? "Light Theme" : "Dark Theme";
  });
  document.getElementById("scaleLinearBtn").addEventListener("click", () => {
    state.logScale = false;
    document.getElementById("scaleLinearBtn").classList.add("active");
    document.getElementById("scaleLogBtn").classList.remove("active");
    renderCharts();
  });
  document.getElementById("scaleLogBtn").addEventListener("click", () => {
    state.logScale = true;
    document.getElementById("scaleLogBtn").classList.add("active");
    document.getElementById("scaleLinearBtn").classList.remove("active");
    renderCharts();
  });
  document.getElementById("resetLegendBtn").addEventListener("click", () => { initSeries(); renderLegend(); renderCharts(); });
  document.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.filter = btn.dataset.filter;
      document.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("active", b === btn));
      renderMatrix();
    });
  });
  document.getElementById("searchBox").addEventListener("input", (ev) => { state.search = ev.target.value || ""; renderMatrix(); });
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (state.sortKey === k) state.sortDir *= -1; else { state.sortKey = k; state.sortDir = k === "rank" || k === "name" || k === "verdict" ? 1 : -1; }
      renderMatrix();
    });
  });

  initSeries();
  renderHeaderAndKpis();
  renderLegend();
  renderCharts();
  renderMatrix();
  renderAttribution();
  renderRecipe();
})();
</script>
</body>
</html>
"""


__all__ = [
    "render_workbench_html",
    "write_workbench_html",
]
