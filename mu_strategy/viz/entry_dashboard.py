from __future__ import annotations

import html
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REFRESH_SECONDS = 30

EXIT_OBSERVATION_TIER_WARNING = "exit_warning"
EXIT_OBSERVATION_TIER_UNKNOWN = "position_unknown"
EXIT_OBSERVATION_TIER_UNAVAILABLE = "position_unavailable"
EXIT_OBSERVATION_TIER_NONE = "no_position_or_source"


def classify_exit_observation(observation: Any) -> str:
    """Return the stable four-tier meaning shared by dashboard consumers."""

    if not isinstance(observation, dict):
        return EXIT_OBSERVATION_TIER_NONE
    state_quality = str(observation.get("state_quality") or "").lower()
    if state_quality != "degraded":
        return EXIT_OBSERVATION_TIER_UNAVAILABLE
    evaluation = observation.get("assumption_evaluation")
    if isinstance(evaluation, dict) and evaluation.get("exit_triggered") is True:
        if evaluation.get("exit_reason") in {"stop", "non_session_liquidation_risk"}:
            return EXIT_OBSERVATION_TIER_WARNING
        return EXIT_OBSERVATION_TIER_UNAVAILABLE
    return EXIT_OBSERVATION_TIER_UNKNOWN


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
    order_groups = _order_groups(orders)
    planned_orders = order_groups["planned"]
    submitted_orders = order_groups["submitted"]
    blocked_orders = order_groups["blocked"]
    execution_orders = submitted_orders + blocked_orders
    data_errors = _list(payload.get("data_errors"))
    expired_orders = _list(payload.get("expired_orders"))
    exit_observations_value = payload.get("exit_observations")
    exit_observations_are_list = isinstance(exit_observations_value, list)
    exit_observations = _list(exit_observations_value)
    exit_observation_status = payload.get("exit_observation_status")
    if not isinstance(exit_observation_status, dict):
        exit_observation_status = {}
    exit_warning_count = sum(
        classify_exit_observation(observation) == EXIT_OBSERVATION_TIER_WARNING
        for observation in exit_observations
    )
    mode = str(payload.get("mode") or "-")
    mode_label = _mode_label(mode)
    scope_label = _scope_label(universe, scans)
    failed = mode in {"blocked", "cycle_failed"} or str(payload.get("reason") or "") in {"runner_failed", "account_context_error"}

    if failed:
        headline = "扫描失败"
        headline_detail = "不可下单"
        state_class = "state-bad"
        headline_badge_class = "failed"
    elif exit_warning_count:
        headline = f"发现 {exit_warning_count} 个持仓出场警示"
        headline_detail = "降级估计，请先核对持仓"
        state_class = "state-bad"
        headline_badge_class = "failed"
    elif planned_orders:
        headline = f"发现 {len(planned_orders)} 个可人工复核机会"
        headline_detail = "只用于人工复核，不代表已经下单"
        state_class = "state-good"
        headline_badge_class = "planned"
    elif submitted_orders:
        headline = f"本轮已提交 {len(submitted_orders)} 个 demo 订单"
        headline_detail = "核对订单状态，必要时按撤单目标处理"
        state_class = "state-good"
        headline_badge_class = "planned"
    elif blocked_orders:
        headline = "本轮无可挂单建议，存在阻塞订单结果"
        headline_detail = "未下单，查看阻塞原因"
        state_class = "state-wait"
        headline_badge_class = "wait"
    else:
        headline = "当前无进场机会"
        headline_detail = "等待下一轮扫描"
        state_class = "state-wait"
        headline_badge_class = "wait"

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
        <span class="badge {headline_badge_class}">{_e(headline_detail)}</span>
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
  {_exit_observations_section(exit_observations, exit_observation_status, observations_are_list=exit_observations_are_list)}
  {_orders_section(planned_orders, scans, mode, show_empty_notice=not orders)}
  {_order_results_section(execution_orders, scans, mode)}
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


def _orders_section(
    planned_orders: list[dict[str, Any]],
    scans: list[dict[str, Any]],
    mode: str,
    *,
    show_empty_notice: bool = True,
) -> str:
    if not planned_orders:
        if not show_empty_notice:
            return ""
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


def _order_results_section(orders: list[dict[str, Any]], scans: list[dict[str, Any]], mode: str) -> str:
    if not orders:
        return ""
    scans_by_symbol = {str(scan.get("symbol")): scan for scan in scans if scan.get("symbol")}
    rows = "\n".join(_execution_order_rows(order, scans_by_symbol.get(str(order.get("symbol"))), mode) for order in orders)
    return f"""
  <section>
    <h2>订单执行结果</h2>
    <table>
      <thead>
        <tr>
          <th>状态</th>
          <th>symbol</th>
          <th>order_id</th>
          <th>client_order_id</th>
          <th>挂单价</th>
          <th>挂单量</th>
          <th>初始止损点</th>
          <th>reason</th>
          <th>response</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </section>"""


def _execution_order_rows(order: dict[str, Any], scan: dict[str, Any] | None, mode: str) -> str:
    status = _order_status(order)
    order_id = _order_id(order)
    client_order_id = _client_order_id(order)
    if status == "submitted":
        status_text = "已提交 demo 订单"
        status_class = "planned"
        instruction = _submitted_cancel_instruction(order, order_id, client_order_id)
    else:
        status_text = "已阻塞，未下单"
        status_class = "bad"
        instruction = _blocked_order_instruction(order)
    return f"""
        <tr>
          <td><span class="badge {status_class}">{_e(status_text)}</span></td>
          <td>{_e(order.get("symbol"))}</td>
          <td class="mono">{_e(order_id)}</td>
          <td class="mono">{_e(client_order_id)}</td>
          <td class="num">{_e(order.get("limit_price"))}</td>
          <td class="num">{_e(order.get("size"))}</td>
          <td class="num">{_fmt_number(order.get("initial_stop"))}</td>
          <td>{_e(order.get("reason"))}</td>
          <td class="mono">{_e(_response_summary(order))}</td>
        </tr>
        <tr>
          <td colspan="9">{instruction}</td>
        </tr>"""


def _submitted_cancel_instruction(order: dict[str, Any], order_id: str, client_order_id: str) -> str:
    symbol = _fmt(order.get("symbol"))
    limit_price = _fmt(order.get("limit_price"))
    size = _fmt(order.get("size")) if order.get("size") not in {None, ""} else "未换算"
    lines = [
        f"撤单目标：{symbol} / {order_id} / {client_order_id} / {limit_price} / {size}",
        "已提交 demo 订单；如需撤单，应使用上面的 order_id/client_order_id 核对 OKX demo 挂单。",
    ]
    return '<div class="instruction">' + "".join(f"<div>{_e(line)}</div>" for line in lines) + "</div>"


def _blocked_order_instruction(order: dict[str, Any]) -> str:
    reason = _fmt(order.get("reason"))
    line = f"未下单，无撤单目标。reason={reason}"
    return f'<div class="instruction"><div>{_e(line)}</div></div>'


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


def _exit_observations_section(
    observations: list[dict[str, Any]],
    status: dict[str, Any],
    *,
    observations_are_list: bool,
) -> str:
    cards = "\n".join(_exit_observation_card(observation) for observation in observations)
    if not cards:
        cards = _empty_exit_observation_status(
            status,
            observations_are_list=observations_are_list,
            rendered_observation_count=len(observations),
        )
    return f"""
  <section>
    <h2>持仓出场观测 · 降级估计</h2>
    <div class="notice">
      交易所只返回持仓均价和数量，不返回真实止损、成交明细和加仓阶段。这里没有权威的实时出场判断，下面所有数值都是基于假设推算的估计值，仅供你自己去核对，不构成“该平仓”的结论。
    </div>
    <div class="instruction">{cards}</div>
  </section>"""


def _empty_exit_observation_status(
    status: dict[str, Any],
    *,
    observations_are_list: bool,
    rendered_observation_count: int,
) -> str:
    reason = str(status.get("reason") or "")
    state = str(status.get("status") or "")
    if reason == "dry_run_has_no_position_source":
        message = "Dry-run 未读取持仓，无法确认当前是否有持仓。"
    elif reason:
        message = f"持仓观测不可用，无法评估。状态码：{reason}"
    elif (
        observations_are_list
        and state == "available"
        and _safe_count(status.get("position_count")) == 0
        and _safe_count(status.get("observation_count")) == rendered_observation_count
    ):
        message = "已读取交易所持仓：本轮返回 0 个持仓。"
    else:
        message = "本轮没有可展示的持仓观测，无法确认当前是否有持仓。"
    return f"""
    <div class="status state-wait">
      <div class="headline">
        <strong>无持仓观测</strong>
        <span class="badge wait">无持仓 / 无观测源</span>
      </div>
      <div class="subtle">{_e(message)}</div>
    </div>"""


def _exit_observation_card(observation: dict[str, Any]) -> str:
    tier = classify_exit_observation(observation)
    if tier == EXIT_OBSERVATION_TIER_UNAVAILABLE:
        return _unavailable_exit_observation_card(observation)

    evaluation_value = observation.get("assumption_evaluation")
    evaluation = evaluation_value if isinstance(evaluation_value, dict) else None
    warning = tier == EXIT_OBSERVATION_TIER_WARNING
    title = "⚠️ 出场警示（降级估计）" if warning else "持仓可见但状态未知"
    state_class = "state-bad" if warning else "state-wait"
    badge_class = "bad" if warning else "wait"
    badge_text = "出场警示" if warning else "状态未知"
    if evaluation is None:
        evaluation_html = '<div class="notice">假设评估不可用，当前没有可展示的出场数值。</div>'
    else:
        evaluation_html = _exit_evaluation_details(observation, evaluation)
    return f"""
    <div class="status {state_class}">
      <div class="headline">
        <strong>{title}</strong>
        <span class="badge {badge_class}">{badge_text}</span>
      </div>
      <div class="subtle">symbol={_e(observation.get("symbol"))} · 持仓数量={_e(_fmt_number(observation.get("position_size")))} · 持仓均价={_e(_fmt_number(observation.get("average_entry_price")))}</div>
      <div class="instruction">
        <strong>这是降级估计，不是权威判断</strong>
        <span>decision_status=unknown：缺少真实止损、成交明细和加仓阶段，因此没有权威的实时出场判断。</span>
      </div>
      {evaluation_html}
      {_stop_bias_notice(observation, evaluation)}
    </div>"""


def _unavailable_exit_observation_card(observation: dict[str, Any]) -> str:
    unavailable_reason = observation.get("unavailable_reason")
    if not unavailable_reason:
        evaluation = observation.get("assumption_evaluation")
        if isinstance(evaluation, dict) and evaluation.get("exit_triggered") is True:
            exit_reason = _fmt(evaluation.get("exit_reason"))
            trigger_basis = _fmt(evaluation.get("trigger_basis"))
            unavailable_reason = (
                "assumption_evaluation 字段矛盾：exit_triggered=true，"
                f"但 exit_reason={exit_reason} 不是受支持的触发原因"
                f"（trigger_basis={trigger_basis}），无法评估。"
            )
        else:
            unavailable_reason = "未提供不可评估原因。"
    return f"""
    <div class="status state-bad">
      <div class="headline">
        <strong>持仓不可评估</strong>
        <span class="badge bad">不可评估</span>
      </div>
      <div class="subtle">symbol={_e(observation.get("symbol"))} · 持仓数量={_e(_fmt_number(observation.get("position_size")))} · 持仓均价={_e(_fmt_number(observation.get("average_entry_price")))}</div>
      <div class="notice">无法生成假设评估：{_e(unavailable_reason)}</div>
    </div>"""


def _exit_evaluation_details(observation: dict[str, Any], evaluation: dict[str, Any]) -> str:
    reason = evaluation.get("exit_reason") or "未触发"
    trigger_basis = evaluation.get("trigger_basis") or "none"
    return f"""
      <div class="instruction">
        <strong>依据：{_e(reason)} / {_e(trigger_basis)}</strong>
        <span>stop_before_candle（本根触发线）= {_e(_fmt_number(evaluation.get("stop_before_candle")))} ← 判据：最低价 ≤ 此值</span>
        <span>最新收盘 = {_e(_fmt_number(evaluation.get("latest_close")))}</span>
        <span>stop_after_candle_if_open（若存续，下一根携带值）= {_e(_fmt_number(evaluation.get("stop_after_candle_if_open")))} ← 不是本根的触发线</span>
        <span>latest_close_at_or_below_tightened_stop = {_diagnostic_bool(evaluation.get("latest_close_at_or_below_tightened_stop"))}</span>
        <span>仅供诊断，不是本根的出场判断。本根判断只看最低价与本根触发线。</span>
      </div>
      {_liquidation_risk_notice(observation)}"""


def _stop_bias_notice(
    observation: dict[str, Any],
    evaluation: dict[str, Any] | None,
) -> str:
    initial_stop_pct = _assumed_initial_stop_pct(observation, evaluation)
    return f"""
      <div class="notice">
        <strong>为什么“没有警示”不等于安全</strong><br>
        估计假设你只建了首仓（max_stage=1），止损停在均价 ×（1 − {_e(initial_stop_pct)}）。如果你实际已加仓，真实止损会被收紧到远高于这个假设值——所以止损类警示会漏报：真实已该止损的持仓，这里可能显示无警示。<br>
        这条“只漏报、不误报”仅限于假设止损偏低这一项偏差。均价与实际加权成本有偏、持仓已平但交易所未更新、建仓时配置与当前不同，这些情况下警示仍可能误报。
      </div>"""


def _liquidation_risk_notice(observation: dict[str, Any]) -> str:
    leverage = _leverage_assumption(observation)
    if leverage is None:
        return '<div class="notice"><strong>杠杆未知，强平线无法估计</strong></div>'
    return f"""
      <div class="notice">
        <strong>强平风险已纳入估计</strong><br>
        非美股时段，若最低价跌破均价 ×（1 − 1/杠杆）即触发，与回测同序（该判断优先于普通止损）。<br>
        估计使用杠杆 {_e(leverage)}。若与你交易所该持仓的实际杠杆不符，上面这条强平线就是错的——杠杆越高，真实强平线越接近现价，请自行核对。<br>
        这条路径只依赖均价，不依赖加仓阶段，估计质量高于止损路径。
      </div>"""


def _leverage_assumption(observation: dict[str, Any]) -> str | None:
    assumptions = observation.get("assumptions")
    if not isinstance(assumptions, (list, tuple)):
        return None
    for assumption in assumptions:
        text = str(assumption)
        if not text.startswith("leverage="):
            continue
        value_text = text.removeprefix("leverage=").split(":", 1)[0].strip()
        try:
            value = float(value_text)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        return _fmt_number(value)
    return None


def _assumed_initial_stop_pct(
    observation: dict[str, Any],
    evaluation: dict[str, Any] | None,
) -> str:
    average_entry = _finite_number(observation.get("average_entry_price"))
    stop_before = _finite_number((evaluation or {}).get("stop_before_candle"))
    if average_entry is None or average_entry <= 0 or stop_before is None:
        return "策略初始止损比例"
    ratio = 1 - (stop_before / average_entry)
    if not math.isfinite(ratio) or ratio < 0 or ratio >= 1:
        return "策略初始止损比例"
    return f"{ratio:.2%}"


def _finite_number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_count(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _diagnostic_bool(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


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


def _order_groups(orders: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups = {"planned": [], "submitted": [], "blocked": [], "other": []}
    for order in orders:
        status = _order_status(order)
        if status in groups:
            groups[status].append(order)
        else:
            groups["other"].append(order)
    return groups


def _order_status(order: dict[str, Any]) -> str:
    return str(order.get("status") or "").lower()


def _order_id(order: dict[str, Any]) -> str:
    if order.get("order_id"):
        return _fmt(order.get("order_id"))
    response = order.get("response")
    if isinstance(response, dict):
        for row in response.get("data") or []:
            if isinstance(row, dict) and row.get("ordId"):
                return _fmt(row.get("ordId"))
    return "-"


def _client_order_id(order: dict[str, Any]) -> str:
    if order.get("client_order_id"):
        return _fmt(order.get("client_order_id"))
    response = order.get("response")
    if isinstance(response, dict):
        for row in response.get("data") or []:
            if isinstance(row, dict) and row.get("clOrdId"):
                return _fmt(row.get("clOrdId"))
    return "-"


def _response_summary(order: dict[str, Any]) -> str:
    response = order.get("response")
    if not isinstance(response, dict):
        return "-"
    code = response.get("code")
    msg = response.get("msg")
    data = response.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        row = data[0]
        parts = [
            f"code={_fmt(code)}",
            f"sCode={_fmt(row.get('sCode'))}",
            f"sMsg={_fmt(row.get('sMsg'))}",
        ]
    else:
        parts = [f"code={_fmt(code)}", f"msg={_fmt(msg)}"]
    return " ".join(part for part in parts if not part.endswith("=-"))


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
