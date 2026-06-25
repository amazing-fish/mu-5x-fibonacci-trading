from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def render_data_health_dashboard(manifest: dict[str, Any]) -> str:
    rows = []
    for symbol, payload in sorted((manifest.get("symbols") or {}).items()):
        source = payload.get("source")
        for interval, status in sorted((payload.get("intervals") or {}).items()):
            state_label = _state_label(status)
            state = "ok" if state_label == "fresh" else "bad"
            rows.append(
                f"""
        <tr>
          <td>{_e(symbol)}</td>
          <td>{_e(source)}</td>
          <td>{_e(interval)}</td>
          <td><span class="badge {state}">{_e(state_label)}</span></td>
          <td>{_e(status.get("availability") or "-")}</td>
          <td>{_e(status.get("integrity") or "-")}</td>
          <td class="num">{_e(status.get("rows"))}</td>
          <td>{_e(_format_time_ms(status.get("last_timestamp_ms")))}</td>
          <td>{_e(status.get("reason") or _reasons_text(status))}</td>
          <td class="mono">{_e(status.get("source_file"))}</td>
        </tr>"""
            )
    body = "\n".join(rows) if rows else '<tr><td colspan="10">暂无数据</td></tr>'
    universe = manifest.get("universes") or {}
    crypto_count = len(universe.get("crypto_top") or [])
    stock_count = len(universe.get("stock_token_top") or [])
    warnings = manifest.get("warnings") or []
    warning_html = "".join(f"<li>{_e(item)}</li>" for item in warnings) or "<li>无</li>"
    generated_at = _format_time_ms(manifest.get("updated_at_ms"))
    requested = ", ".join(str(item) for item in manifest.get("requested_intervals") or manifest.get("intervals") or [])
    effective = ", ".join(str(item) for item in manifest.get("effective_intervals") or manifest.get("intervals") or [])
    cycle_error = manifest.get("cycle_error")
    provider_failures = manifest.get("provider_failures") or []
    provider_failure_html = "".join(f"<li>{_e(item)}</li>" for item in provider_failures) or "<li>无</li>"
    cycle_error_html = (
        f"<pre>{_e(cycle_error)}</pre>"
        if cycle_error
        else "<p>无</p>"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OKX 数据健康看板</title>
  <style>
    body {{ margin: 0; background: #f6f7f4; color: #17202a; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 22px; }}
    h1 {{ margin: 0 0 8px; font-size: 26px; }}
    .summary {{ display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 10px; margin: 14px 0; }}
    .metric, section {{ background: #fff; border: 1px solid #d8ddd7; border-radius: 8px; padding: 12px; }}
    .metric span {{ display: block; color: #63707a; font-size: 13px; }}
    .metric strong {{ display: block; font-size: 22px; }}
    table {{ width: 100%; min-width: 980px; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 8px 9px; border-bottom: 1px solid #d8ddd7; text-align: left; vertical-align: top; }}
    th {{ background: #edf0e8; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .mono {{ font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }}
    .badge {{ display: inline-flex; padding: 2px 8px; border-radius: 999px; font-weight: 650; }}
    .badge.ok {{ color: #0b7a5a; background: #dff3ea; }}
    .badge.bad {{ color: #b42318; background: #ffe2de; }}
    section {{ overflow-x: auto; margin-top: 14px; }}
  </style>
</head>
<body>
<main>
  <h1>OKX 数据健康看板</h1>
  <div>生成时间：{_e(generated_at)} · 数据源：OKX public market data · 存储：CSV + JSON manifest + JSONL run log</div>
  <div class="summary">
    {_metric("attempt", manifest.get("attempt_status"))}
    {_metric("snapshot", manifest.get("snapshot_usability"))}
    {_metric("run_id", manifest.get("run_id") or "-")}
    {_metric("symbols", len(manifest.get("symbols") or {}))}
    {_metric("crypto_top", crypto_count)}
    {_metric("stock_token_top", stock_count)}
    {_metric("requested", requested or "-")}
    {_metric("effective", effective or "-")}
  </div>
  <section>
    <h2>Warnings</h2>
    <ul>{warning_html}</ul>
  </section>
  <section>
    <h2>Provider failures</h2>
    <ul>{provider_failure_html}</ul>
  </section>
  <section>
    <h2>Cycle error</h2>
    {cycle_error_html}
  </section>
  <section>
    <h2>Intervals</h2>
    <table>
      <thead><tr><th>symbol</th><th>universe</th><th>interval</th><th>freshness</th><th>availability</th><th>integrity</th><th>rows</th><th>latest</th><th>reason</th><th>source_file</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
  </section>
</main>
</body>
</html>"""


def write_data_health_dashboard(manifest: dict[str, Any], output_path: Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_data_health_dashboard(manifest), encoding="utf-8")
    return output_path


def _state_label(status: dict[str, Any]) -> str:
    if status.get("freshness"):
        return str(status.get("freshness"))
    if status.get("is_stale"):
        return "stale"
    if not status.get("is_valid"):
        return "invalid"
    return "fresh"


def _reasons_text(status: dict[str, Any]) -> str:
    return ", ".join(str(item) for item in status.get("reasons") or []) or "-"


def _metric(label: str, value: Any) -> str:
    return f'<div class="metric"><span>{_e(label)}</span><strong>{_e(value)}</strong></div>'


def _format_time_ms(value: Any) -> str:
    try:
        if value is None:
            return "-"
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


def _e(value: Any) -> str:
    if value is None:
        return "-"
    return html.escape(str(value), quote=True)
