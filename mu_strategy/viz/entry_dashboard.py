from __future__ import annotations

import html
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REFRESH_SECONDS = 30


def render_entry_dashboard(
    payload: dict[str, Any],
    *,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    generated_at: str | None = None,
) -> str:
    refresh_seconds = max(1, int(refresh_seconds))
    generated_at = generated_at or _local_timestamp()
    universe = _list(payload.get("universe"))
    scans = _list(payload.get("scans"))
    orders = _list(payload.get("orders"))
    planned_orders = [order for order in orders if str(order.get("status") or "") == "planned"]
    data_errors = _list(payload.get("data_errors"))
    expired_orders = _list(payload.get("expired_orders"))
    mode = str(payload.get("mode") or "-")
    mode_label = _mode_label(mode)
    scope_label = _scope_label(universe, scans)
    failed = mode in {"blocked", "cycle_failed"} or str(payload.get("reason") or "") in {"runner_failed", "account_context_error"}

    if failed:
        headline = "扫描失败"
        headline_detail = "不可下单"
        state_class = "state-bad"
    elif planned_orders:
        headline = f"发现 {len(planned_orders)} 个可人工复核机会"
        headline_detail = "只用于人工复核，不代表已经下单"
        state_class = "state-good"
    else:
        headline = "当前无进场机会"
        headline_detail = "等待下一轮扫描"
        state_class = "state-wait"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <title>OKX 入场扫描看板</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f6f1;
      --ink: #182027;
      --muted: #66717c;
      --line: #d8ddd7;
      --surface: #ffffff;
      --surface-strong: #edf0e8;
      --good: #0b7a5a;
      --good-bg: #dff3ea;
      --wait: #8a5b04;
      --wait-bg: #fff0c2;
      --bad: #b42318;
      --bad-bg: #ffe2de;
      --accent: #2454a6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Aptos", "Segoe UI", "Microsoft YaHei", sans-serif;
      letter-spacing: 0;
    }}
    main {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 22px;
    }}
    header {{
      display: grid;
      grid-template-columns: minmax(280px, 1.4fr) repeat(5, minmax(120px, 0.6fr));
      gap: 10px;
      align-items: stretch;
      margin-bottom: 14px;
    }}
    h1 {{
      margin: 0;
      font-size: 25px;
      line-height: 1.2;
    }}
    h2 {{
      margin: 0 0 10px;
      font-size: 17px;
    }}
    .status, .metric, section {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .status {{
      padding: 14px 16px;
      border-left-width: 6px;
    }}
    .status.state-good {{ border-left-color: var(--good); }}
    .status.state-wait {{ border-left-color: var(--wait); }}
    .status.state-bad {{ border-left-color: var(--bad); }}
    .headline {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .subtle, .metric span, td small {{
      color: var(--muted);
    }}
    .metric {{
      padding: 12px;
      min-width: 0;
    }}
    .metric strong {{
      display: block;
      font-size: 20px;
      line-height: 1.1;
      font-variant-numeric: tabular-nums;
      overflow-wrap: anywhere;
    }}
    section {{
      margin-top: 14px;
      padding: 14px;
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      min-width: 1050px;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 8px 9px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: var(--surface-strong);
      color: #303941;
      white-space: nowrap;
      font-weight: 650;
    }}
    td.num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }}
    .badge.enter, .badge.planned, .badge.good {{ color: var(--good); background: var(--good-bg); }}
    .badge.wait {{ color: var(--wait); background: var(--wait-bg); }}
    .badge.skip, .badge.failed, .badge.bad {{ color: var(--bad); background: var(--bad-bg); }}
    .badge.other {{ color: var(--accent); background: #dfe9ff; }}
    .notice {{
      padding: 10px 12px;
      background: #f8faf7;
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
    }}
    .rules {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .rules li {{
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcf9;
    }}
    .mono {{
      font-family: "Cascadia Mono", "Consolas", monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .instruction {{
      display: grid;
      gap: 5px;
      padding: 10px 0;
    }}
    @media (max-width: 980px) {{
      main {{ padding: 14px; }}
      header {{ grid-template-columns: 1fr 1fr; }}
      .status {{ grid-column: 1 / -1; }}
      table {{ min-width: 940px; }}
    }}
    @media (max-width: 640px) {{
      header {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 22px; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div class="status {state_class}">
      <div class="headline">
        <h1>{_e(headline)}</h1>
        <span class="badge {('failed' if failed else 'planned' if planned_orders else 'wait')}">{_e(headline_detail)}</span>
      </div>
      <div class="subtle">生成时间：{_e(generated_at)} · 扫描范围：{_e(scope_label)} · 页面刷新倒计时 <strong id="refresh-countdown">{refresh_seconds}</strong>s</div>
    </div>
    {_metric("模式", mode_label)}
    {_metric("扫描标的", len(scans))}
    {_metric("机会", len(planned_orders))}
    {_metric("数据错误", len(data_errors))}
    {_metric("撤单记录", len(expired_orders))}
  </header>

  {_failure_section(payload) if failed else ""}
  {_orders_section(planned_orders, scans, mode)}
  {_reason_summary_section(scans)}
  {_scan_table_section(scans)}
  {_account_open_orders_section(payload)}
  {_error_section(payload, data_errors)}
  {_expired_orders_section(expired_orders)}
</main>
<script>
(function () {{
  var seconds = {refresh_seconds};
  var target = document.getElementById("refresh-countdown");
  if (!target) return;
  target.textContent = String(seconds);
  window.setInterval(function () {{
    seconds -= 1;
    if (seconds <= 0) {{
      target.textContent = "0";
      window.location.reload();
      return;
    }}
    target.textContent = String(seconds);
  }}, 1000);
}}());
</script>
</body>
</html>"""


def write_entry_dashboard(
    payload: dict[str, Any],
    output_path: Path,
    *,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    generated_at: str | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_entry_dashboard(payload, refresh_seconds=refresh_seconds, generated_at=generated_at)
    tmp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(html_text, encoding="utf-8")
    tmp_path.replace(output_path)
    return output_path


def latest_payload_from_jsonl(log_path: Path) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            latest = value
    if latest is None:
        raise ValueError(f"no valid JSON object found in {log_path}")
    return latest


def _orders_section(planned_orders: list[dict[str, Any]], scans: list[dict[str, Any]], mode: str) -> str:
    if not planned_orders:
        return """
  <section>
    <h2>挂单建议单</h2>
    <div class="notice">当前无挂单建议，因此无撤单目标。</div>
  </section>"""
    scans_by_symbol = {str(scan.get("symbol")): scan for scan in scans if scan.get("symbol")}
    rows = "\n".join(_order_rows(order, scans_by_symbol.get(str(order.get("symbol"))), mode) for order in planned_orders)
    return f"""
  <section>
    <h2>挂单建议单</h2>
    <table>
      <thead>
        <tr>
          <th>操作</th>
          <th>状态</th>
          <th>symbol</th>
          <th>挂单价</th>
          <th>挂单量</th>
          <th>名义金额</th>
          <th>杠杆</th>
          <th>初始止损点</th>
          <th>client_order_id</th>
          <th>信号时间</th>
          <th>reason</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </section>"""


def _order_rows(order: dict[str, Any], scan: dict[str, Any] | None, mode: str) -> str:
    size = order.get("size")
    size_text = _fmt(size)
    status = "可人工复核"
    status_class = "planned"
    if size in {None, ""}:
        size_text = "挂单数量未完成换算，不建议直接挂单"
        status = "不可直接挂单"
        status_class = "bad"
    return f"""
        <tr>
          <td>限价买入</td>
          <td><span class="badge {status_class}">{_e(status)}</span></td>
          <td>{_e(order.get("symbol"))}</td>
          <td class="num">{_e(order.get("limit_price"))}</td>
          <td class="num">{_e(size_text)}</td>
          <td class="num">{_fmt_number(order.get("notional_usdt"))}</td>
          <td class="num">{_fmt(order.get("leverage", "-"))}</td>
          <td class="num">{_fmt_number(order.get("initial_stop"))}</td>
          <td class="mono">{_e(order.get("client_order_id"))}</td>
          <td>{_e(_format_time_ms(order.get("signal_time_ms")))}</td>
          <td>{_e(order.get("reason"))}</td>
        </tr>
        <tr>
          <td colspan="10">{_cancel_instruction(order, scan, mode)}</td>
        </tr>"""


def _cancel_instruction(order: dict[str, Any], scan: dict[str, Any] | None, mode: str) -> str:
    symbol = _fmt(order.get("symbol"))
    client_order_id = _fmt(order.get("client_order_id"))
    limit_price = _fmt(order.get("limit_price"))
    size = _fmt(order.get("size")) if order.get("size") not in {None, ""} else "未换算"
    wait_bars = _wait_bars(scan)
    signal_time_ms = order.get("signal_time_ms") or (scan or {}).get("signal_time_ms")
    mode_note = (
        "dry_run 下没有真实撤单操作；如果你手工挂了这张单，应撤这张单。"
        if mode == "dry_run"
        else "live_demo 下如已有真实 demo 挂单，应按实际 order_id/client_order_id 撤单结果核对。"
    )
    lines = [
        f"撤单目标：{symbol} / {client_order_id} / {limit_price} / {size}",
        f"撤单触发点：下一轮扫描不再出现同一个 client_order_id={client_order_id}",
        _expiry_instruction(signal_time_ms, wait_bars),
        "1h 失效：regime_1h == red",
        "RSI 失效：rsi14 < 45.00",
        "MACD 失效：macd_hist < macd_hist_prev 且 macd_hist < 0",
        "数据失效：market data stale/missing/load failed",
        mode_note,
    ]
    return '<div class="instruction">' + "".join(f"<div>{_e(line)}</div>" for line in lines) + "</div>"


def _reason_summary_section(scans: list[dict[str, Any]]) -> str:
    if not scans:
        return """
  <section>
    <h2>阻塞原因统计</h2>
    <div class="notice">本轮没有扫描明细。</div>
  </section>"""
    counts = Counter(str(scan.get("reason") or "-") for scan in scans)
    rows = "\n".join(
        f"<tr><td>{_e(reason)}</td><td class=\"num\">{count}</td></tr>" for reason, count in counts.most_common()
    )
    return f"""
  <section>
    <h2>阻塞原因统计</h2>
    <table>
      <thead><tr><th>reason</th><th>count</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </section>"""


def _scan_table_section(scans: list[dict[str, Any]]) -> str:
    if not scans:
        return """
  <section>
    <h2>全市场扫描</h2>
    <div class="notice">本轮没有扫描明细。</div>
  </section>"""
    ordered = sorted(scans, key=lambda row: (_action_rank(row.get("action")), str(row.get("symbol") or "")))
    rows = "\n".join(_scan_row(scan) for scan in ordered)
    return f"""
  <section>
    <h2>全市场扫描</h2>
    <table>
      <thead>
        <tr>
          <th>action</th>
          <th>symbol</th>
          <th>source</th>
          <th>reason</th>
          <th>last_close</th>
          <th>regime_1h</th>
          <th>rsi14</th>
          <th>macd_hist</th>
          <th>fib_level</th>
          <th>fib_distance_pct</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </section>"""


def _scan_row(scan: dict[str, Any]) -> str:
    action = str(scan.get("action") or "other")
    badge_class = action if action in {"enter", "wait", "skip"} else "other"
    return f"""
        <tr>
          <td><span class="badge {badge_class}">{_e(action)}</span></td>
          <td>{_e(scan.get("symbol"))}</td>
          <td>{_e(scan.get("source"))}</td>
          <td>{_e(scan.get("reason"))}</td>
          <td class="num">{_fmt_number(scan.get("last_close"))}</td>
          <td>{_e(scan.get("regime_1h"))}</td>
          <td class="num">{_fmt_number(scan.get("rsi14"))}</td>
          <td class="num">{_fmt_number(scan.get("macd_hist"))}</td>
          <td class="num">{_fmt_number(scan.get("fib_level"))}</td>
          <td class="num">{_fmt_pct(scan.get("fib_distance_pct"))}</td>
        </tr>"""


def _account_open_orders_section(payload: dict[str, Any]) -> str:
    account_context = payload.get("account_context")
    if not isinstance(account_context, dict):
        return ""
    open_orders = account_context.get("open_orders")
    if not isinstance(open_orders, dict):
        return ""
    rows = [row for row in open_orders.get("data") or [] if isinstance(row, dict)]
    if not rows:
        return ""
    body = "\n".join(
        f"""
        <tr>
          <td>{_e(row.get("instId"))}</td>
          <td class="mono">{_e(row.get("ordId"))}</td>
          <td class="mono">{_e(row.get("clOrdId"))}</td>
          <td>{_e(row.get("side"))}</td>
          <td>{_e(row.get("ordType"))}</td>
          <td>{_e(row.get("state"))}</td>
          <td class="num">{_e(row.get("px"))}</td>
          <td class="num">{_e(row.get("sz"))}</td>
        </tr>"""
        for row in rows
    )
    return f"""
  <section>
    <h2>当前 demo 挂单</h2>
    <table>
      <thead><tr><th>symbol</th><th>order_id</th><th>client_order_id</th><th>side</th><th>type</th><th>state</th><th>price</th><th>size</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
  </section>"""


def _failure_section(payload: dict[str, Any]) -> str:
    return f"""
  <section>
    <h2>扫描失败</h2>
    <div class="notice">
      不可下单。不可作为下单依据。reason={_e(payload.get("reason"))} · error_type={_e(payload.get("error_type"))} · message={_e(payload.get("message"))}
    </div>
  </section>"""


def _error_section(payload: dict[str, Any], data_errors: list[dict[str, Any]]) -> str:
    universe_error = payload.get("universe_error")
    if not data_errors and not universe_error:
        return ""
    rows = []
    if isinstance(universe_error, dict):
        rows.append(_error_row({"symbol": "universe", **universe_error}))
    rows.extend(_error_row(error) for error in data_errors)
    return f"""
  <section>
    <h2>数据/标的池错误</h2>
    <table>
      <thead><tr><th>symbol</th><th>reason</th><th>error_type</th><th>message</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </section>"""


def _error_row(error: dict[str, Any]) -> str:
    return f"""
        <tr>
          <td>{_e(error.get("symbol"))}</td>
          <td>{_e(error.get("reason"))}</td>
          <td>{_e(error.get("error_type"))}</td>
          <td>{_e(error.get("message"))}</td>
        </tr>"""


def _expired_orders_section(expired_orders: list[dict[str, Any]]) -> str:
    if not expired_orders:
        return ""
    rows = "\n".join(
        f"""
        <tr>
          <td>{_e(order.get("status"))}</td>
          <td>{_e(order.get("symbol"))}</td>
          <td>{_e(order.get("reason"))}</td>
          <td class="mono">{_e(order.get("client_order_id"))}</td>
          <td class="mono">{_e(order.get("order_id"))}</td>
        </tr>"""
        for order in expired_orders
    )
    return f"""
  <section>
    <h2>撤单记录</h2>
    <table>
      <thead><tr><th>status</th><th>symbol</th><th>reason</th><th>client_order_id</th><th>order_id</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </section>"""


def _mode_label(mode: str) -> str:
    labels = {
        "dry_run": "模拟扫描：不读取私有账户，不下单，不撤单",
        "live_demo": "OKX demo 模式：可能读取 demo 持仓/挂单，并按确认参数提交 demo 单",
        "blocked": "不可作为下单依据",
        "cycle_failed": "不可作为下单依据",
    }
    return labels.get(mode, mode or "-")


def _scope_label(universe: list[dict[str, Any]], scans: list[dict[str, Any]]) -> str:
    rows = universe if universe else scans
    if not rows:
        return "未取得扫描范围"
    counts = Counter(str(row.get("source") or "unknown") for row in rows)
    parts = [f"Top {counts.get('top', 0)}", f"固定关注 {counts.get('watchlist', 0)}"]
    extras = [
        f"{source} {count}"
        for source, count in sorted(counts.items())
        if source not in {"top", "watchlist"} and count
    ]
    return " + ".join(parts + extras)


def _wait_bars(scan: dict[str, Any] | None) -> int:
    try:
        return max(1, int((scan or {}).get("second_pullback_wait_bars") or 8))
    except Exception:
        return 8


def _expiry_instruction(signal_time_ms: Any, wait_bars: int) -> str:
    try:
        if signal_time_ms is None:
            raise ValueError
        expiry_ms = int(signal_time_ms) + wait_bars * 15 * 60 * 1000
    except Exception:
        return f"时间失效点：signal_time 缺失，无法计算 + {wait_bars} 根 15m K"
    return (
        f"时间失效点：{_format_time_ms(signal_time_ms)} + {wait_bars} 根 15m K = "
        f"{_format_time_ms(expiry_ms)}"
    )


def _metric(label: str, value: Any) -> str:
    return f"""
    <div class="metric">
      <span>{_e(label)}</span>
      <strong>{_e(value)}</strong>
    </div>"""


def _list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _action_rank(value: Any) -> int:
    return {"enter": 0, "wait": 1, "skip": 2}.get(str(value or ""), 3)


def _format_time_ms(value: Any) -> str:
    try:
        if value is None:
            return "-"
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


def _fmt_pct(value: Any) -> str:
    try:
        if value is None:
            return "-"
        return f"{float(value):.2%}"
    except Exception:
        return _fmt(value)


def _fmt_number(value: Any) -> str:
    try:
        if value is None:
            return "-"
        number = float(value)
    except Exception:
        return _fmt(value)
    if abs(number) >= 100:
        return f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    return str(value)


def _e(value: Any) -> str:
    return html.escape(_fmt(value), quote=True)


def _local_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
