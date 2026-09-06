"""Chinese review presentation shared by offline exports and the local viewer."""
from __future__ import annotations

import html
import hashlib
import json
import os
import shlex
from datetime import datetime

from mu_strategy.signal_review import BEIJING


OUTCOMES = {
    "ready_for_review": ("待复核记录", "accent"),
    "normal_no_action": ("正常等待", "neutral"),
    "data_gate_blocked": ("数据阻断", "warning"),
    "scan_failed": ("扫描失败", "danger"),
}
DELIVERY = {
    "pending": ("待发送", "neutral"), "confirmed": ("SMTP 已接受 / 已核实接受", "good"),
    "failed": ("确定失败", "danger"), "unknown": ("结果不明 · 需核查", "warning"),
}
KINDS = {"entry_review": "入场复核", "signal_invalidated": "原提醒失效",
         "service_fault": "服务故障", "service_recovered": "服务恢复"}
REASONS = {
    "current_bar_outside_trading_window": "交易时段外，正常等待",
    "waiting_second_pullback": "等待第二次回踩", "regime_blocked": "1h 市场结构暂未允许入场",
    "second_pullback_limit_ready": "回踩条件满足，曾进入人工复核",
    "no_candles": "缺少 K 线", "insufficient_history": "历史样本不足",
    "no_confirmed_fib_retest": "尚无确认的 Fibonacci 回踩",
    "no_recent_confirmed_fib_retest": "近期没有确认的回踩",
    "unknown": "原记录没有明确判断", "market_data_unavailable": "行情数据暂不可用",
    "rsi_below_floor": "RSI 未达到确认条件", "macd_weakening": "MACD 动能转弱，继续等待",
    "signal_confirmed": "信号已确认，等待后续入场条件", "price_away_from_fib": "价格距离回踩位过远",
    "next_candle_required": "等待下一根 K 线", "next_fill_outside_trading_window": "后续入场时点不在交易窗口",
    "next_candle_did_not_break_signal_high": "下一根 K 线未突破信号高点",
    "execution_price_unavailable": "尚无可用的入场评估价格", "signal_candle_too_wide": "信号 K 线波幅过大",
    "entry_too_far_above_fib": "入场价格高于回踩位过多",
    "entry_too_far_above_signal_close": "入场价格高于信号收盘价过多",
    "reverse_fib_resistance": "遇到反向 Fibonacci 阻力", "execution_accepted": "通过入场条件评估（仅记录）",
    "review_expired": "人工复核期限已过", "decision_changed": "后续扫描决定已变化",
    "source_unavailable": "来源不可用，原提醒失去采信依据", "signal_replaced": "已被新的信号替代",
    "historical_event": "历史记录，已抑制发送", "ready": "扫描当时满足复核条件",
    "health_event": "服务健康事件", "runtime_changed": "服务运行状态变化",
}
RUNTIME = {"running": "运行中", "starting": "启动中", "stopped": "已停止", "interrupted": "运行中断",
           "unresponsive": "未及时响应", "not_started": "尚未启动", "unavailable": "无法核实"}


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _time(value, *, day_only=False) -> str:
    if value is None:
        return "unknown"
    try:
        stamp = datetime.fromtimestamp(value / 1000, BEIJING)
        return stamp.strftime("%Y-%m-%d" if day_only else "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "时间不可显示（原始值见证据）"


def _badge(value, catalog) -> str:
    text, tone = catalog.get(value, (value or "unknown", "neutral"))
    return f'<span class="badge {tone}">{_e(text)}</span>'


def _evidence(value, title="查看来源证据", *, identifier="") -> str:
    identity = f' id="{_e(identifier)}"' if identifier else ""
    return f'<details class="evidence"{identity}><summary>{_e(title)}</summary><pre>{_e(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))}</pre></details>'


def _command(data_dir: str, module: str, extra="") -> str:
    quoted = "'" + data_dir.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(data_dir)
    return f"python -B -m mu_strategy.commands.{module} {extra} --data-dir {quoted}".strip()


def _copy_command(command, identifier) -> str:
    return f'<div class="command"><code id="{_e(identifier)}">{_e(command)}</code><button id="{_e(identifier)}-copy" type="button" class="copy" data-copy="{_e(identifier)}">复制命令</button></div>'


def _scan_reason(item) -> str:
    code = item.get("decision_code")
    reason = REASONS.get(code, item.get("compatibility_reason") or item.get("trust_reason") or "原因见证据")
    if item["outcome"] == "scan_failed":
        reason = "扫描失败，本轮没有可采信结论"
    elif item["outcome"] == "data_gate_blocked":
        reason = "可信数据校验未通过，本轮停止产生信号"
    return reason


def _scan_identity(item) -> str:
    return hashlib.sha256(json.dumps([item["cycle_id"], item["observation_id"]]).encode()).hexdigest()


def group_scan_records(records: list[dict]) -> list[list[dict]]:
    """Compact only nearby, same-day normal waits; keep every source observation."""
    groups, last_by_symbol = [], {}
    fields = ("symbol", "strategy_name", "strategy_config_fingerprint", "outcome", "decision_code",
              "compatibility_reason", "compatibility_source", "trust_reason", "provenance", "trust_policy_name",
              "trust_policy_version", "requested_intervals", "effective_intervals")
    for index, item in enumerate(records):
        setup = item.get("scan_result") or {}
        signature = json.dumps([{key: item.get(key) for key in fields},
                                {key: setup.get(key) for key in ("signal_time_ms", "trigger_price", "initial_stop")}], sort_keys=True)
        previous = last_by_symbol.get(item["symbol"])
        merge = False
        if previous is not None and item["outcome"] == "normal_no_action":
            last = previous[1][-1]
            merge = (previous[2] == signature and
                     _time(item["created_at_ms"], day_only=True) == _time(last["created_at_ms"], day_only=True) and
                     0 <= item["created_at_ms"] - last["created_at_ms"] <= 600_000 and
                     0 <= item["observed_at_ms"] - last["observed_at_ms"] <= 600_000)
        if merge:
            previous[0] = index
            previous[1].append(item)
        else:
            group = [index, [item], signature]
            groups.append(group)
            last_by_symbol[item["symbol"]] = group
    return [group[1] for group in sorted(groups, key=lambda group: group[0])]


def _scan_row(item, *, raw=False) -> str:
    reason = _scan_reason(item)
    result = item.get("scan_result") or {}
    identifier = f'{item["cycle_id"]}/{item["observation_id"]}'
    evidence = {"source": "observations.jsonl", "strategy_code_version": "unknown",
                "source_service_run_id": "unknown", "source_attempt_id": "unknown", **item}
    return f'''<article class="scan-row filter-row" data-kind="{'raw-scan' if raw else 'scan'}" data-count="1" data-date="{_e(_time(item['created_at_ms'], day_only=True))}"
        data-symbol="{_e(item['symbol'])}" data-status="{_e(item['outcome'])}">
      <div class="row-time"><time>{_e(_time(item['observed_at_ms']))}</time><span>{_e(item['symbol'])}</span></div>
      <div class="row-main">{_badge(item['outcome'], OUTCOMES)}<strong>{_e(reason)}</strong>
        <span class="secondary">策略 {_e(item['strategy_name'])} · 观察价格 {_e(result.get('last_close') if result.get('last_close') is not None else 'unknown')}</span>
        {_evidence(evidence, identifier='scan-evidence-' + _scan_identity(item))}</div>
      <div class="row-reference"><span>数据快照</span><code>{_e(item.get('trusted_run_id') or 'unknown')}</code>
        <span class="sr-only">{_e(identifier)}</span></div>
    </article>'''


def _scan_group(items) -> str:
    if len(items) == 1:
        return _scan_row(items[0])
    first, last = items[0], items[-1]
    price = (last.get("scan_result") or {}).get("last_close")
    return f'''<article class="scan-row filter-row scan-group" data-kind="scan" data-count="{len(items)}"
        data-date="{_e(_time(last['created_at_ms'], day_only=True))}" data-symbol="{_e(last['symbol'])}" data-status="normal_no_action">
      <div class="row-time"><time>{_e(_time(last['observed_at_ms']))}</time><span>{_e(last['symbol'])}</span></div>
      <div class="row-main">{_badge('normal_no_action', OUTCOMES)}<strong>{_e(_scan_reason(last))}</strong>
        <span class="secondary">{_e(_time(first['observed_at_ms']))} — {_e(_time(last['observed_at_ms']))} · 相同状态 {len(items)} 次</span>
        <span class="secondary">最新观察价格 {_e(price if price is not None else 'unknown')}</span>
        <details class="group-details" id="scan-group-{_scan_identity(first)}"><summary>展开 {len(items)} 次扫描记录</summary>
          {''.join(_scan_row(item, raw=True) for item in reversed(items))}</details></div></article>'''


def _alert_row(record, data_dir, index, known_event_ids) -> str:
    event = record["event"]
    observation = event.get("observation") or {}
    related = event.get("related_event_id")
    relation = ""
    if related:
        relation = (f'<a href="#event-{_e(related)}">查看关联入场记录</a>' if related in known_event_ids else
                    '<span>关联入场不在本报告明细中；可按 event ID 查询。</span>')
        relation += f'<code class="related-id">{_e(related)}</code>'
    suppression = f'<p class="suppression">已抑制：{_e(REASONS.get(record["suppressed_reason"], record["suppressed_reason"]))}</p>' if record["suppressed_reason"] else ""
    evidence = {**record, "strategy_code_version": "unknown", "source_service_run_id": "unknown",
                "source_attempt_id": "unknown", "actual_trade": "尚无人工成交记录"}
    query = _copy_command(_command(data_dir, "email_alerts", "show " + record["event_id"]), f"query-{record['event_id']}")
    return f'''<article id="event-{_e(record['event_id'])}" class="alert-row filter-row" data-kind="alert"
        data-date="{_e(_time(event['occurred_at_ms'], day_only=True))}" data-symbol="{_e(observation.get('symbol', ''))}" data-status="{_e(record['state'])}">
      <div class="row-time"><time>{_e(_time(event['occurred_at_ms']))}</time><span>{_e(observation.get('symbol') or '全局服务')}</span></div>
      <div class="row-main"><strong>{_e(KINDS.get(event['kind'], event['kind']))}</strong>{_badge(record['state'], DELIVERY)}
        <span>{_e(REASONS.get(event['reason'], event['reason']))}</span>{suppression}
        <span class="secondary">发送尝试 {record['attempts']} 次 · 人工复核截止 {_e(_time(event['review_until_ms'])) if event['review_until_ms'] is not None else '不适用'}</span>
        <div class="relation">{relation}</div>{_evidence(evidence, '查看事件与送达历史', identifier='event-evidence-' + record['event_id'])}
        <details class="query" id="event-query-{record['event_id']}"><summary>只读查询命令</summary>{query}</details></div>
    </article>'''


def render_signal_review(report: dict, *, live=False) -> str:
    sources = report["sources"]
    scans, notifications, service = (sources[key] for key in ("observations", "notifications", "service"))
    window = report["window"]
    view = service.get("view", {})
    generated = _time(report["generated_at_ms"])
    notices = [item["message"] for item in sources.values() if item["state"] != "ok"]
    if view and not view.get("healthy"):
        notices.append("生成时服务未处于健康状态。请先查看健康详情；历史正常扫描不证明服务现在正常。")
    if notifications.get("all_counts", {}).get("unknown", 0):
        notices.append("通知库存在结果不明的邮件。先核查 SMTP 接受证据，不能直接重发。")
    if notifications.get("all_counts", {}).get("failed", 0):
        notices.append("通知库存在确定失败的邮件。查看事件的失败原因和尝试历史后处理。")
    issues = ''.join(f'<li>{_e(item)}</li>' for item in notices)
    notice_html = f'<aside class="notice" aria-label="需要核查"><strong>需要核查</strong><ul>{issues}</ul></aside>' if notices else ''
    scan_records = scans.get("records", [])
    alert_records = notifications.get("records", [])
    symbols = sorted({item["symbol"] for item in scan_records} |
                     {item["event"]["observation"]["symbol"] for item in alert_records if item["event"].get("observation")})
    symbol_options = ''.join(f'<option value="{_e(symbol)}">{_e(symbol)}</option>' for symbol in symbols)
    scan_rows = ''.join(_scan_group(items) for items in reversed(group_scan_records(scan_records)))
    known_event_ids = {record["event_id"] for record in alert_records}
    alert_rows = ''.join(_alert_row(item, report["data_dir"], index, known_event_ids) for index, item in enumerate(reversed(alert_records)))
    totals = scans.get("counts", {})
    summary = ''.join(f'<div class="metric"><span>{_e(label)}</span><strong>{totals.get(key, 0) if "counts" in scans else "—"}</strong></div>'
                      for key, (label, _) in OUTCOMES.items())
    latest_rows = ''.join(f'<li><strong>{_e(item["symbol"])}</strong><span>{_e(_scan_reason(item))}</span><time>{_e(_time(item["observed_at_ms"]))}</time></li>'
                          for item in scans.get("latest", []))
    runtime_label = RUNTIME.get(view.get("runtime", "unavailable"), "无法核实")
    health_label = "生成时正常" if view.get("healthy") else runtime_label
    data = view.get("data_at_last_scan", {})
    data_label = {"allowed": "上次扫描通过", "blocked": "上次扫描阻断", "unknown": "尚无校验记录"}.get(data.get("status"), "无法核实")
    state_counts = ' · '.join(f'{DELIVERY[key][0]} {value}' for key, value in notifications.get("all_counts", {}).items()) or '尚无提醒记录'
    if notifications["state"] != "ok":
        state_counts = "通知来源不可用"
    scan_note = (f'明细只保留最后 {scans.get("display_limit")} 条观察；上方统计仍覆盖完整窗口，筛选仅作用于这些明细。' if scans.get("display_truncated") else
                 '每条代表一个标的一次扫描；重复轮询得到的 READY 不等于新的交易机会。')
    if scans.get("state") == "incomplete":
        scan_note = "读取已截断：统计与明细都不是完整窗口。缩短观察数据源或处理容量后重新生成。"
    alert_note = (f'明细只保留最后 {notifications.get("display_limit")} 条事件；窗口统计没有使用最近 50 条摘要推算。' if notifications.get("display_truncated") else
                  '按事件发生日期筛选，送达状态截至采集时刻。标的筛选会保留全局服务事件。')
    service_evidence = _evidence(service, "健康详情", identifier="health-evidence")
    source_rows = ''.join(f'<tr><th scope="row">{_e(name)}</th><td>{_e(item["state"])}</td><td>{_e(_time(item["read_at_ms"]))}</td><td>{_e(item["message"])}</td></tr>'
                          for name, item in zip(("服务健康", "扫描日志", "通知库"), (service, scans, notifications)))
    window_total = notifications.get("total", "—")
    notification_counts = ' · '.join(f'{DELIVERY[key][0]} {value}' for key, value in notifications.get("counts", {}).items()) or '无窗口内提醒 / 来源见下方'
    latest_html = latest_rows or '<li class="empty">所选窗口内没有可验证的扫描记录。</li>'
    snapshot_html = (f'<strong id="refresh-state">自动更新 · 每 30 秒</strong><time id="report-updated">更新于 {_e(generated)} 北京时间</time>'
                     '<div class="live-actions"><button id="refresh-now" type="button">立即更新</button><button id="pause-refresh" type="button">暂停自动更新</button></div>'
                     '<span id="connection-note" role="status" aria-live="polite">已连接本地只读查看器</span>' if live else
                     f'<strong>导出快照 · 不自动更新</strong><time>生成于 {_e(generated)} 北京时间</time><span>日常查看请使用本地实时入口；此文件保留导出时的记录。</span>')
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><link rel="icon" href="data:,"><title>MU · 每日信号复盘</title><style>{_STYLE}</style></head>
<body data-live="{'true' if live else 'false'}" data-generated-at="{report['generated_at_ms']}" data-sources-verified="{'true' if report['sources_verified'] else 'false'}"><a class="skip" href="#main">跳到内容</a><header class="masthead"><div class="brand">MU<span>研究与观察</span></div>
<nav aria-label="页面导航"><a href="#overview">概览</a><a href="#scans">扫描记录</a><a href="#alerts">邮件记录</a><a href="#sources">来源与边界</a></nav></header>
<main id="main"><section class="intro"><div><p class="eyebrow">SIGNAL JOURNAL / 只读复盘</p><h1>每日信号复盘</h1>
<p class="lede">扫描状态与提醒记录</p></div><div class="snapshot">{snapshot_html}</div></section>
<div id="review-content">{notice_html}<section id="overview" aria-label="生成时状态"><div class="status-strip">
<div><span class="eyebrow">服务 / 生成时</span><strong>{_e(health_label)}</strong><small>{_e(runtime_label)} · 连续失败 {_e(view.get('consecutive_failures', 'unknown'))}</small></div>
<div><span class="eyebrow">行情 / 上次扫描时</span><strong>{_e(data_label)}</strong><small>校验于 {_e(_time(data.get('checked_at_ms')))}</small></div>
<div><span class="eyebrow">通知 / 库内现状</span><strong>{_e(state_counts)}</strong><small>最近消费 {_e(_time(notifications.get('last_collection_ms')))}；不证明进程存活</small></div></div>
{service_evidence}<div class="section-heading"><h2>观察窗口</h2><span>{_e(window['from_date'])} 至 {_e(window['to_date'])} · 北京时间自然日</span></div>
<p class="scope">{'已完整读取现存日志' if scans.get('complete') else '尚未验证完整性'}：{_e(scans.get('total_cycles', '—'))} 个扫描轮次 · {_e(scans.get('total_observations', '—'))} 条标的观察 · 忽略 {_e(scans.get('duplicate_cycles', '—'))} 个同内容重复轮次</p>
<p class="secondary">实际记录：{_e(_time(scans.get('first_at_ms')))} 至 {_e(_time(scans.get('last_at_ms')))}</p>
<div class="metrics">{summary}</div><h3>窗口内最后追加的扫描</h3><ul class="latest">{latest_html}</ul>
<p class="secondary">历史“待复核”不代表现在仍有效或已成交。</p></section>
<section class="filters" aria-label="筛选报告明细"><div class="section-heading"><h2>查找记录</h2><button id="reset" type="button">重置筛选</button></div>
<div class="filter-fields"><label>起始日期<input id="from-date" type="date" min="{_e(window['from_date'])}" max="{_e(window['to_date'])}" value="{_e(window['from_date'])}"></label>
<label>结束日期<input id="to-date" type="date" min="{_e(window['from_date'])}" max="{_e(window['to_date'])}" value="{_e(window['to_date'])}"></label>
<label>标的<select id="symbol"><option value="">全部标的</option>{symbol_options}</select></label></div>
<p id="filter-error" class="error" role="status" aria-live="polite"></p><p class="secondary">筛选只影响下方明细，不改变上方窗口统计和生成时状态。需要其他日期请重新生成报告。</p></section>
<section id="scans"><div class="section-heading"><h2>扫描记录 <span class="section-number">01</span></h2>
<label class="inline-filter">扫描结果<select id="scan-status"><option value="">全部结果</option>{''.join(f'<option value="{key}">{_e(label)}</option>' for key, (label, _) in OUTCOMES.items())}</select></label></div>
<p class="secondary">相邻同状态的正常等待已合并，其余逐条显示。{_e(scan_note) if scans.get('display_truncated') or scans.get('state') == 'incomplete' else '展开可核对每次扫描。'}</p><p id="scan-count" class="result-count" aria-live="polite">加载了 {len(scan_records)} 条明细</p>
<div class="record-list">{scan_rows}</div><p id="scan-empty" class="empty" {'hidden' if scan_records else ''}>没有符合筛选的可验证扫描；这不代表服务健康或没有交易机会。</p></section>
<section id="alerts"><div class="section-heading"><h2>邮件记录 <span class="section-number">02</span></h2>
<label class="inline-filter">送达状态<select id="delivery-status"><option value="">全部状态</option>{''.join(f'<option value="{key}">{_e(label)}</option>' for key, (label, _) in DELIVERY.items())}</select></label></div>
<p class="scope">窗口内 {_e(window_total)} 个事件 · 已抑制 {_e(notifications.get('suppressed', '—'))} 个 · {_e(notification_counts)}</p>
<p class="secondary">{_e(alert_note)} SMTP 接受不证明收件箱到达、已读或实际成交。抑制状态与送达状态分别展示。</p>
<p id="alert-count" class="result-count" aria-live="polite">加载了 {len(alert_records)} 条明细</p><div class="record-list">{alert_rows}</div>
<p id="alert-empty" class="empty" {'hidden' if alert_records else ''}>没有符合筛选的提醒记录；请同时查看通知来源状态。</p></section>
<section id="sources"><details id="source-details"><summary class="source-summary">来源、口径与排查命令</summary>
<p>三类来源分别读取，采集时点可能不同；本页不宣称跨文件的原子快照。健康源仅保留最近 100 个事件，不能由它证明完整故障历史。</p>
<div class="table-wrap"><table><thead><tr><th>来源</th><th>读取状态</th><th>采集时间 / 北京时间</th><th>说明</th></tr></thead><tbody>{source_rows}</tbody></table></div>
<dl class="boundaries"><div><dt>策略代码版本、源运行与尝试身份</dt><dd>unknown。原日志尚未提供这些历史字段，报告生成版本不能替代。</dd></div>
<div><dt>人工反馈与实际成交</dt><dd>尚无记录；不能从未记录推断未成交。</dd></div><div><dt>持仓管理提醒</dt><dd>尚未接入可信持仓（#85）；不判断空仓、加仓或确定退出。</dd></div>
<div><dt>运行与策略验收</dt><dd>扫描轮数和记录天数不等于连续交易日验收，也不证明策略盈利。</dd></div></dl>
<details class="query" id="source-queries"><summary>数据目录与只读排查命令</summary><p><code>{_e(report['data_dir'])}</code></p>
{_copy_command(_command(report['data_dir'], 'signal_service', 'status'), 'service-query')}
{_copy_command(_command(report['data_dir'], 'email_alerts', 'status'), 'email-query')}</details></details></section></div>
<footer>MU / SIGNAL JOURNAL<span>来源可查 · 状态分开 · 不执行交易</span></footer></main><script>{_SCRIPT}</script></body></html>'''


_STYLE = r'''
:root{color-scheme:light dark;--paper:#f4f3ed;--surface:#fffefa;--ink:#202b28;--muted:#58645e;--line:#d5d9cf;--accent:#175c4d;--soft:#e4eee6;--warning:#7a4800;--warn-bg:#fff0d3;--danger:#a02d25;--danger-bg:#ffebe6;--good:#246048;--good-bg:#e7f1e6;--focus:#0879a0;--code:#eaede6}
@media(prefers-color-scheme:dark){:root{--paper:#171e1c;--surface:#202a25;--ink:#eef1e8;--muted:#b7c1b8;--line:#465149;--accent:#a3dcc6;--soft:#2b4439;--warning:#ffd38e;--warn-bg:#44361f;--danger:#ffb4a9;--danger-bg:#442d29;--good:#b5deb6;--good-bg:#294131;--focus:#80d6fa;--code:#303c33}}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:24px}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.7 "Microsoft YaHei UI","PingFang SC",sans-serif}button,input,select{font:inherit}button,a,input,select,summary{touch-action:manipulation}a{color:var(--accent);text-underline-offset:4px}button{color:var(--accent);background:var(--surface);border:1px solid var(--line);padding:7px 13px;border-radius:4px;cursor:pointer}button:hover{background:var(--soft)}:focus-visible{outline:3px solid var(--focus);outline-offset:3px}main{max-width:1240px;margin:auto;padding:0 36px}.masthead{max-width:1240px;margin:auto;padding:22px 36px;display:flex;justify-content:space-between;gap:24px;border-bottom:1px solid var(--line)}.brand{font:bold 26px/1.2 Georgia,serif;letter-spacing:2px}.brand span{font:12px "Microsoft YaHei UI",sans-serif;margin-left:16px;letter-spacing:1px;color:var(--muted)}nav{display:flex;flex-wrap:wrap;gap:24px;align-items:center}nav a{text-decoration:none;font-size:13px}.intro{display:flex;justify-content:space-between;align-items:flex-end;gap:30px;padding:48px 0 32px}.eyebrow{font-size:11px;font-weight:700;letter-spacing:1.8px;color:var(--accent);margin:0 0 10px}h1{font-size:38px;line-height:1.3;letter-spacing:2px;margin:0 0 12px}h2{font-size:23px;margin:0;line-height:1.4}h3{font-size:15px;font-weight:600;margin:28px 0 10px}.lede{font-size:16px;color:var(--muted);margin:0}.snapshot{max-width:330px;display:grid;gap:5px;font-size:12px;color:var(--muted);border-left:3px solid var(--accent);padding-left:16px}.snapshot strong{color:var(--ink);font-size:14px}.status-strip{display:grid;grid-template-columns:1fr 1fr 1.4fr;border:1px solid var(--line);background:var(--surface)}.status-strip>div{display:grid;align-content:start;gap:8px;padding:22px;border-left:1px solid var(--line)}.status-strip>div:first-child{border-left:0}.status-strip strong{font-size:18px;line-height:1.5}.status-strip small{color:var(--muted);font-size:12px}.status-strip .eyebrow{margin:0}section{margin-bottom:36px}.section-heading{display:flex;justify-content:space-between;gap:20px;align-items:center;margin:28px 0 12px}.section-heading>span{font-size:13px;color:var(--muted)}.section-number{font:italic 18px Georgia,serif;color:var(--muted);margin-left:12px}.scope{font-size:13px;color:var(--muted)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);border-top:2px solid var(--accent);border-bottom:1px solid var(--line)}.metric{padding:18px 24px;display:flex;align-items:center;justify-content:space-between;border-left:1px solid var(--line)}.metric:first-child{border-left:0}.metric span{font-size:13px;color:var(--muted)}.metric strong{font:34px/1.2 Georgia,serif;font-variant-numeric:tabular-nums}.latest{list-style:none;padding:0;margin:0}.latest li{display:grid;grid-template-columns:180px 1fr auto;gap:16px;border-bottom:1px solid var(--line);padding:12px 0}.latest time{font-size:12px;color:var(--muted)}.secondary{color:var(--muted);font-size:12px}.filters{border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:0 0 18px}.filter-fields{display:flex;gap:16px;flex-wrap:wrap}label{display:grid;gap:5px;font-size:12px;color:var(--muted)}input,select{min-height:40px;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:4px;padding:6px 10px;max-width:100%}.filter-fields label{min-width:180px}.inline-filter{display:flex;align-items:center;gap:10px}.record-list{border-top:1px solid var(--line)}.scan-row,.alert-row{display:grid;gap:22px;padding:20px 0;border-bottom:1px solid var(--line);grid-template-columns:160px minmax(0,1fr) 190px}.alert-row{grid-template-columns:160px minmax(0,1fr)}.row-time{font-size:12px;display:flex;flex-direction:column;gap:5px;color:var(--muted)}.row-time span{font-weight:600;color:var(--ink)}.row-main{display:flex;align-items:flex-start;flex-direction:column;gap:8px;min-width:0}.row-main>strong{font-size:15px}.badge{display:inline-block;font-size:11px;line-height:1.7;font-weight:700;padding:3px 9px;border-radius:3px;background:var(--code);color:var(--muted)}.accent{background:var(--soft);color:var(--accent)}.warning{background:var(--warn-bg);color:var(--warning)}.danger{background:var(--danger-bg);color:var(--danger)}.good{background:var(--good-bg);color:var(--good)}.row-reference{display:grid;gap:5px;align-content:start;color:var(--muted);font-size:11px}code,pre{font-family:Consolas,"SFMono-Regular",monospace;font-size:12px;overflow-wrap:anywhere;white-space:pre-wrap}.row-reference code{font-size:11px}.evidence,.query{width:100%;font-size:12px}.evidence>summary,.query>summary{color:var(--accent);cursor:pointer;padding:5px 0}.evidence pre{max-height:380px;overflow:auto;background:var(--code);padding:16px;border:1px solid var(--line);border-radius:4px;line-height:1.6;margin:8px 0}.result-count{font-size:12px;font-weight:700;color:var(--muted)}.empty{padding:24px;color:var(--muted);border:1px dashed var(--line);font-size:13px}.notice{padding:16px 20px;background:var(--warn-bg);color:var(--warning);border-left:3px solid var(--warning);margin-bottom:24px}.notice ul{margin:6px 0;padding-left:22px;font-size:13px}.error{color:var(--danger);min-height:1em;font-size:12px;margin-bottom:0}.suppression{margin:0;color:var(--warning);font-size:12px}.relation{font-size:12px;max-width:100%}.related-id{display:block;margin-top:4px}.command{display:flex;gap:12px;align-items:center;padding:12px;background:var(--code);margin:8px 0;border-radius:4px;max-width:100%}.command code{flex:1;min-width:0}.copy{flex-shrink:0;font-size:11px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px;text-align:left}td,th{border-bottom:1px solid var(--line);padding:12px;vertical-align:top}th{white-space:nowrap}.boundaries{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:24px}.boundaries div{border-left:2px solid var(--line);padding-left:16px}.boundaries dt{font-size:13px;font-weight:600}.boundaries dd{margin:6px 0 0;font-size:12px;color:var(--muted)}footer{display:flex;justify-content:space-between;gap:18px;font-size:11px;letter-spacing:1px;padding:28px 0;border-top:1px solid var(--line);color:var(--muted)}[hidden]{display:none!important}.sr-only,.skip{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}.skip:focus{position:fixed;top:8px;left:8px;width:auto;height:auto;clip-path:none;background:var(--surface);padding:10px;z-index:5}
@media(max-width:780px){main{padding:0 20px}.masthead{padding:18px 20px;flex-direction:column;gap:16px}nav{gap:18px}.intro{padding:30px 0 24px;align-items:flex-start;flex-direction:column;gap:20px}h1{font-size:30px}.snapshot{max-width:none}.status-strip{grid-template-columns:1fr}.status-strip>div{border-left:0;border-top:1px solid var(--line);padding:16px}.status-strip>div:first-child{border-top:0}.metrics{grid-template-columns:1fr 1fr}.metric{padding:14px 16px;border-bottom:1px solid var(--line)}.metric:nth-child(3){border-left:0}.metric strong{font-size:28px}.latest li{grid-template-columns:1fr;gap:4px}.section-heading{align-items:flex-start;flex-direction:column;gap:12px}.filter-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}.filter-fields label{min-width:0}.filter-fields label:last-child{grid-column:1/-1}.scan-row,.alert-row{grid-template-columns:1fr;gap:12px}.row-time{flex-direction:row;flex-wrap:wrap;justify-content:space-between}.row-reference{display:none}.boundaries{grid-template-columns:1fr}.command{flex-wrap:wrap}.command code{flex-basis:100%}footer{flex-direction:column}.inline-filter select{max-width:220px}h2{font-size:21px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}@media print{:root{color-scheme:light;--paper:white;--surface:white;--ink:black;--muted:#444;--line:#aaa;--accent:#154e40;--soft:#eef3ef;--code:#f4f4f4;--warning:#713f00;--warn-bg:#fff8ee;--danger:#89251c;--danger-bg:#fff0eb;--good:#255336;--good-bg:#f1f6ef}nav,.filters,.copy,.query{display:none}main{max-width:none;padding:0}.masthead{padding:12px 0}.intro{padding:20px 0}.row-main pre{max-height:none}section{break-inside:avoid}a{color:inherit}}
'''


_SCRIPT = r'''

(() => {
  const byId = id => document.getElementById(id);
  const controlIds = ['from-date', 'to-date', 'symbol', 'scan-status', 'delivery-status'];
  function resetDates() {
    for (const id of ['from-date', 'to-date']) byId(id).value = byId(id).defaultValue;
  }
  function filter() {
    const from = byId('from-date'), to = byId('to-date'), symbol = byId('symbol');
    const invalid = !from.value || !to.value || from.value > to.value || from.value < from.min || to.value > to.max;
    byId('filter-error').textContent = invalid ? '日期必须位于本报告观察窗口内，且起始日期不晚于结束日期。' : '';
    for (const kind of ['scan', 'alert']) {
      const rows = Array.from(document.querySelectorAll('[data-kind="' + kind + '"]'));
      let count = 0, observations = 0;
      const status = byId(kind === 'scan' ? 'scan-status' : 'delivery-status').value;
      for (const row of rows) {
        const match = !invalid && row.dataset.date >= from.value && row.dataset.date <= to.value
          && (!symbol.value || row.dataset.symbol === symbol.value || (kind === 'alert' && !row.dataset.symbol))
          && (!status || row.dataset.status === status);
        row.hidden = !match;
        if (match) { count++; observations += Number(row.dataset.count || 1); }
      }
      byId(kind + '-count').textContent = kind === 'scan'
        ? '当前筛选 ' + count + ' 组 · ' + observations + ' 次扫描'
        : '当前筛选 ' + count + ' 条 / 本页已加载 ' + rows.length + ' 条明细';
      byId(kind + '-empty').hidden = count > 0;
    }
  }
  document.addEventListener('change', event => { if (controlIds.includes(event.target.id)) filter(); });
  document.addEventListener('click', async event => {
    if (event.target.closest('#reset')) {
      resetDates(); for (const id of ['symbol', 'scan-status', 'delivery-status']) byId(id).value = ''; filter();
    }
    const relation = event.target.closest('.relation a');
    if (relation) {
      resetDates(); byId('symbol').value = ''; byId('delivery-status').value = ''; filter();
      const target = byId(relation.getAttribute('href').slice(1));
      if (target) target.querySelector('details').open = true;
    }
    const button = event.target.closest('[data-copy]');
    if (button) {
      const source = byId(button.dataset.copy);
      try { await navigator.clipboard.writeText(source.textContent); button.textContent = '已复制'; }
      catch { const range = document.createRange(); range.selectNodeContents(source); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); button.textContent = '已选中，请复制'; }
    }
  });
  filter();
  if (document.body.dataset.live !== 'true') return;
  let paused = false, refreshing = false, editingUntil = 0;
  for (const type of ['focusin', 'input', 'change', 'keydown', 'pointerdown']) {
    document.addEventListener(type, event => {
      if (event.target.matches('input, select')) editingUntil = Date.now() + 2000;
    });
  }
  async function refresh(manual = false) {
    if (refreshing || (!manual && (paused || document.visibilityState !== 'visible'))) return;
    if (!manual && (Date.now() < editingUntil || !window.getSelection().isCollapsed)) {
      byId('refresh-state').textContent = Date.now() < editingUntil ? '编辑筛选 · 自动更新暂缓' : '选中文本 · 自动更新暂缓';
      return;
    }
    refreshing = true; byId('refresh-now').disabled = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/report', {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('unavailable');
      const incoming = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (incoming.body.dataset.live !== 'true' || !incoming.getElementById('review-content') || !incoming.getElementById('report-updated')) throw new Error('invalid report');
      // Read UI state at application time, so a choice made during fetch survives.
      const values = controlIds.map(id => ({id, value: byId(id).value, defaultValue: byId(id).defaultValue}));
      const opened = Array.from(document.querySelectorAll('details[open][id]'), node => node.id);
      const groupAnchors = Array.from(document.querySelectorAll('.group-details[open] .evidence[id]'), node => node.id);
      const focus = document.activeElement.id, scroll = [window.scrollX, window.scrollY];
      byId('review-content').replaceWith(incoming.getElementById('review-content'));
      for (const saved of values) {
        const control = byId(saved.id);
        if (saved.id.endsWith('-date') && saved.value === saved.defaultValue) continue;
        if (control.tagName !== 'SELECT' || Array.from(control.options).some(option => option.value === saved.value)) control.value = saved.value;
      }
      for (const id of groupAnchors) { const group = byId(id)?.closest('.group-details'); if (group) group.open = true; }
      for (const id of opened) {
        let detail = byId(id);
        while (detail) { detail.open = true; detail = detail.parentElement?.closest('details'); }
      }
      document.body.dataset.generatedAt = incoming.body.dataset.generatedAt;
      document.body.dataset.sourcesVerified = incoming.body.dataset.sourcesVerified;
      document.body.classList.remove('connection-lost');
      byId('report-updated').textContent = incoming.getElementById('report-updated').textContent;
      byId('refresh-state').textContent = paused ? '自动更新已暂停' : '自动更新 · 每 30 秒';
      byId('connection-note').textContent = incoming.body.dataset.sourcesVerified === 'true' ? '已连接本地只读查看器' : '已更新；部分来源待核查，见下方提示';
      filter();
      if (focus && byId(focus)) byId(focus).focus({preventScroll:true});
      const behavior = document.documentElement.style.scrollBehavior;
      document.documentElement.style.scrollBehavior = 'auto'; window.scrollTo(...scroll); document.documentElement.style.scrollBehavior = behavior;
    } catch {
      document.body.classList.add('connection-lost');
      byId('refresh-state').textContent = '连接中断 · 显示上次快照';
      byId('connection-note').textContent = '未能读取最新记录。下方保留上次快照，不代表当前服务状态；可点击立即更新重试。';
    } finally {
      clearTimeout(timeout); refreshing = false; byId('refresh-now').disabled = false;
    }
  }
  byId('refresh-now').addEventListener('click', () => refresh(true));
  byId('pause-refresh').addEventListener('click', () => {
    paused = !paused;
    byId('pause-refresh').textContent = paused ? '恢复自动更新' : '暂停自动更新';
    byId('refresh-state').textContent = paused ? '自动更新已暂停' : '自动更新 · 每 30 秒';
    if (!paused) refresh();
  });
  setInterval(() => refresh(), 30000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refresh(); });
})();

'''

_STYLE += r'''

.scan-group{grid-template-columns:160px minmax(0,1fr)}.group-details{width:100%;font-size:12px}.group-details>summary{color:var(--accent);cursor:pointer;padding:5px 0}.group-details .scan-row{grid-template-columns:140px minmax(0,1fr);padding:16px 0}.group-details .row-reference{display:none}.group-details .row-main>strong{font-size:13px}.source-summary{font-size:15px;font-weight:600;color:var(--accent);cursor:pointer;padding:12px 0}.live-actions{display:flex;gap:8px;flex-wrap:wrap}.live-actions button{font-size:12px;padding:5px 9px}.live-actions button:disabled{opacity:.65;cursor:wait}.connection-lost .snapshot{border-color:var(--danger)}.connection-lost #refresh-state,.connection-lost #connection-note{color:var(--danger)}.connection-lost .status-strip{opacity:.65}@media(max-width:780px){.scan-group,.group-details .scan-row{grid-template-columns:1fr}.group-details .row-time{font-size:11px}}@media print{.live-actions{display:none}}

'''
