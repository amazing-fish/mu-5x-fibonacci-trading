from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def render_data_health_dashboard(manifest: dict[str, Any]) -> str:
    rows = []
    blocking = _blocking_issue_summary(manifest)
    partial = _partial_coverage_summary(manifest)
    segments = _segment_diagnostics_table(manifest)
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
    .issue-list {{ margin: 0; padding-left: 18px; }}
    .issue-list li {{ margin: 4px 0; }}
    .hint {{ color: #63707a; font-size: 12px; }}
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
    <h2>Blocking issues</h2>
    {blocking}
  </section>
  <section>
    <h2>Partial coverage</h2>
    {partial}
  </section>
  <section>
    <h2>Segment diagnostics</h2>
    {segments}
  </section>
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
    availability = status.get("availability")
    integrity = status.get("integrity")
    freshness = status.get("freshness")
    if availability or integrity or freshness:
        if availability == "missing" or integrity == "invalid":
            return "invalid"
        if freshness == "stale":
            return "stale"
        if freshness == "fresh" and availability == "available" and integrity == "valid":
            return "fresh"
        return "invalid"
    if status.get("is_stale"):
        return "stale"
    if not status.get("is_valid"):
        return "invalid"
    return "fresh"


def _blocking_issue_summary(manifest: dict[str, Any]) -> str:
    grouped: dict[str, dict[str, list[str]]] = {}
    for symbol, payload in sorted((manifest.get("symbols") or {}).items()):
        for interval, status in sorted((payload.get("intervals") or {}).items()):
            if not _is_blocking_status(status):
                continue
            reason = str(status.get("reason") or _reasons_text(status))
            grouped.setdefault(symbol, {}).setdefault(reason, []).append(str(interval))
    if not grouped:
        return "<p>无 blocking symbols</p>"
    summary = f"<p>{len(grouped)} blocking symbol{'s' if len(grouped) != 1 else ''}</p>"
    items: list[str] = []
    for symbol, reasons in grouped.items():
        reason_text = "; ".join(
            f"{'/'.join(intervals)}: {reason}"
            for reason, intervals in sorted(reasons.items())
        )
        hint = _blocking_hint(reasons)
        hint_html = f'<div class="hint">{_e(hint)}</div>' if hint else ""
        items.append(f"<li><strong>{_e(symbol)}</strong> {_e(reason_text)}{hint_html}</li>")
    return summary + '<ul class="issue-list">' + "".join(items) + "</ul>"


def _segment_diagnostics_table(manifest: dict[str, Any]) -> str:
    diagnostics = manifest.get("diagnostics") or {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    segments = diagnostics.get("refresh_segments") or manifest.get("refresh_segments") or []
    if not isinstance(segments, list) or not segments:
        return "<p>No segment diagnostics</p>"
    rows: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        reason = segment.get("fetch_reason") or segment.get("health_reason") or "-"
        rows.append(
            f"""
        <tr>
          <td>{_e(segment.get("symbol"))}</td>
          <td>{_e(segment.get("interval"))}</td>
          <td>{_e(segment.get("fetch_mode"))}</td>
          <td class="num">{_e(segment.get("elapsed_ms"))}</td>
          <td class="num">{_e(segment.get("existing_rows"))}</td>
          <td class="num">{_e(segment.get("fetched_rows"))}</td>
          <td class="num">{_e(segment.get("output_rows"))}</td>
          <td>{_e(reason)}</td>
          <td>{_e(segment.get("error_type"))}</td>
          <td class="mono">{_e(segment.get("message"))}</td>
        </tr>"""
        )
    body = "\n".join(rows) if rows else '<tr><td colspan="10">No segment diagnostics</td></tr>'
    return f"""
    <table>
      <thead><tr><th>symbol</th><th>interval</th><th>fetch_mode</th><th>elapsed_ms</th><th>existing_rows</th><th>fetched_rows</th><th>output_rows</th><th>reason</th><th>error_type</th><th>message</th></tr></thead>
      <tbody>{body}</tbody>
    </table>"""


def _partial_coverage_summary(manifest: dict[str, Any]) -> str:
    items: list[str] = []
    for symbol, payload in sorted((manifest.get("symbols") or {}).items()):
        for interval, status in sorted((payload.get("intervals") or {}).items()):
            if status.get("coverage_state") != "partial_available_history":
                continue
            requested = _format_days(status.get("requested_days"))
            effective = _format_days(status.get("effective_days"))
            first = _format_time_ms(status.get("first_timestamp_ms"))
            latest = _format_time_ms(status.get("last_timestamp_ms"))
            items.append(
                f"<li><strong>{_e(symbol)}</strong> {_e(interval)}: "
                f"requested {_e(requested)}, effective {_e(effective)}, "
                f"{_e(first)} to {_e(latest)}</li>"
            )
    if not items:
        return "<p>无</p>"
    return '<ul class="issue-list">' + "".join(items) + "</ul>"


def _is_blocking_status(status: dict[str, Any]) -> bool:
    availability = status.get("availability")
    integrity = status.get("integrity")
    freshness = status.get("freshness")
    return availability == "missing" or integrity == "invalid" or freshness in {"stale", "unknown"}


def _blocking_hint(reasons: dict[str, list[str]]) -> str | None:
    if "ohlcv_mismatch" in reasons:
        return "likely cause: zero-volume child candle OHLC policy mismatch"
    return None


def _format_days(value: Any) -> str:
    try:
        if value is None:
            return "-"
        number = float(value)
        if number.is_integer():
            return f"{int(number)}d"
        return f"{number:.2f}d"
    except Exception:
        return str(value)


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
