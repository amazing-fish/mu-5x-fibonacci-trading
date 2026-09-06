"""Manual position cards and a plain form editor without background refresh."""
from __future__ import annotations

import html
import json
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from mu_strategy.manual_positions import BEIJING, UNITS


def _e(value):
    return html.escape(str(value), quote=True)


def _time(value, *, input_value=False):
    if value is None:
        return "" if input_value else "未知"
    return datetime.fromtimestamp(value / 1000, BEIJING).strftime("%Y-%m-%dT%H:%M:%S" if input_value else "%Y-%m-%d %H:%M:%S")


def _state_summary(position, *, editable):
    state = position["current_state"]
    labels = {"unconfirmed": ("当前状态尚未确认", "warning"), "confirmed": ("已人工确认", "good"),
              "needs_review": ("成交记录已变化，待重新核对", "warning"), "not_open": ("当前无可确认的持仓状态", "neutral")}
    label, style = labels[state["status"]]
    values = (f'<p>已达到的策略阶段 {_e(state["stage"] or "未知")} · 当前手记止损 / USDT {_e(state["stop_price"] or "未知")}</p>'
              if state["status"] != "not_open" else '')
    at = f'<span class="secondary">上次确认 {_time(state["confirmed_at_ms"])} 北京时间</span>' if state["confirmed_at_ms"] else ''
    link = f'<a href="/position-state?position_id={position["position_id"]}#position-form">更新持仓状态</a>' if editable and position["status"] == "open" else ''
    return f'<div class="current-position-state"><span class="badge {style}">{label}</span>{values}{at} {link}</div>'


def render_position_cards(view, *, editable=False):
    if not view["available"]:
        return '<p class="notice">成交台账暂不可用，无法判断已记录持仓。请检查台账后刷新。</p>'
    if not view["positions"]:
        return '<p class="empty">尚无成交记录。这不代表账户空仓。</p>'
    cards = []
    for position in sorted(view["positions"], key=lambda item: item["status"] != "open"):
        identity = position["position_id"]
        status = {"open": "已记录持仓", "closed": "已按记录全部卖出", "empty": "无有效成交"}[position["status"]]
        average = position["average_entry_price"]
        average = format(Decimal(average), ".12f").rstrip("0").rstrip(".") if average is not None else "—"
        actions = (f'<a href="/positions?position_id={identity}#position-form">补录成交</a>' if position["status"] != "closed" else "") if editable else ""
        rows = []
        for fill in reversed(position["fills"]):
            edit = f'<a href="/positions?position_id={identity}&amp;fill_id={fill["fill_id"]}#position-form">更正 / 作废</a>' if editable else ""
            action = "作废" if fill["voided"] else "买入" if fill["action"] == "buy" else "卖出"
            rows.append(f'<tr><td>{_time(fill["time_ms"])}</td><td>{action}</td><td>{_e(fill["quantity"])}</td>'
                        f'<td>{_e(fill["price"])}</td><td>{_e(fill["note"])}</td><td>{edit}</td></tr>')
        history = ''.join(f'<tr><td>{_time(item["recorded_at_ms"])}</td><td>{item["fill_id"][:8]} · v{item["revision"]}</td>'
                          f'<td>{"作废" if item["voided"] else "买入" if item["action"] == "buy" else "卖出"} '
                          f'{_e(item["quantity"])} @ {_e(item["price"])}</td><td>{_time(item["time_ms"])}</td>'
                          f'<td>{_e(item["note"])}</td></tr>' for item in reversed(position["history"]))
        state_rows = ''.join(f'<tr><td>{_time(item["confirmed_at_ms"])}</td><td>v{item["revision"]}</td>'
                             f'<td>{_e(item["stage"] or "未知")}</td><td>{_e(item["stop_price"] or "未知")}</td>'
                             f'<td>{_e(item["note"])}</td></tr>' for item in reversed(position["state_history"]))
        state_history = (f'<h4>持仓状态确认历史</h4><div class="table-wrap"><table><thead><tr><th>确认时间 / 北京时间</th><th>版本</th>'
                         f'<th>已达到的阶段</th><th>手记止损 / USDT</th><th>说明</th></tr></thead><tbody>{state_rows}</tbody></table></div>') if state_rows else ''
        source = json.dumps(position["signal_source"], ensure_ascii=False, indent=2) if position["signal_source"] else "未关联信号；配置版本未知。"
        cards.append(f'''<article class="position-card" id="position-{identity}">
          <div class="section-heading"><h3>{_e(position['symbol'])} · 多头</h3><span>{status} · {_e(position['label'] or identity[:8])}</span></div>
          <div class="position-numbers"><strong>{_e(position['recorded_quantity'])} <small>{_e(UNITS[position['unit']])}</small></strong>
          <span>剩余成本均价 / USDT <b>{_e(average)}</b></span></div>
          <p class="secondary">最近成交 {_time(position['last_fill_at_ms'])} 北京时间 · {actions}</p>
          {_state_summary(position, editable=editable)}
          <details id="fills-{identity}"><summary>成交明细 · {len(position['fills'])} 笔</summary>
            <p class="secondary">最近成交时手记（历史信息）：stage {_e(position['recorded_stage'] or '未知')} · 止损 {_e(position['recorded_stop_price'] or '未知')}</p><div class="table-wrap"><table>
            <thead><tr><th>成交时间 / 北京时间</th><th>动作</th><th>数量</th><th>价格 / USDT</th><th>备注</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>
          <details class="evidence" id="position-source-{identity}"><summary>记录来源与更正历史</summary>
            <p>来源：人工确认，未经交易所核对。关联信号配置不代表完整、可恢复的持仓配置。</p><pre>{_e(source)}</pre>
            <div class="table-wrap"><table><thead><tr><th>录入时间 / 北京时间</th><th>成交编号 / 版本</th><th>记录值</th><th>实际成交时间</th><th>备注 / 更正原因</th></tr></thead><tbody>{history}</tbody></table></div>{state_history}</details></article>''')
    return ''.join(cards)


def render_position_editor(view, *, stylesheet, position_id=None, fill_id=None, source=None, draft=None, error=None, saved=False):
    positions = {item["position_id"]: item for item in view["positions"]}
    position = positions.get(position_id)
    fill = next((item for item in position["fills"] if item["fill_id"] == fill_id), None) if position else None
    values = {"request_id": uuid4().hex, "position_id": position_id or uuid4().hex,
              "command": "revise" if fill else "append" if position else "create", "action": "buy",
              "symbol": source["symbol"] if source else "", "event_id": source["event_id"] if source else "",
              "unit": "", "label": "", "quantity": "", "price": "", "executed_at": "",
              "note": "", "stage": "", "stop_price": "", "confirmed": "", "voided": ""}
    if fill:
        values.update(fill_id=fill_id, expected_revision=str(fill["revision"]), action=fill["action"],
                      quantity=fill["quantity"], price=fill["price"], executed_at=_time(fill["time_ms"], input_value=True),
                      stage=str(fill["stage"] or ""), stop_price=fill["stop_price"] or "", voided="yes" if fill["voided"] else "")
    if draft is not None:
        values.update(draft)
    creating, revising = values["command"] == "create", values["command"] == "revise"

    def input_field(name, label, *, kind="text", required=False, extra=""):
        return (f'<label>{label}<input name="{name}" type="{kind}" value="{_e(values.get(name, ""))}" '
                f'{"required" if required else ""} {extra}></label>')

    def select_field(name, label, options):
        return f'<label>{label}<select name="{name}" required>' + ''.join(
            f'<option value="{key}" {"selected" if values.get(name) == key else ""}>{_e(text)}</option>' for key, text in options.items()) + '</select></label>'

    identity_fields = ''.join(f'<input type="hidden" name="{name}" value="{_e(values.get(name, ""))}">' for name in
                              ("request_id", "position_id", "command", "fill_id", "expected_revision", "event_id"))
    if not creating:
        identity_fields += f'<input type="hidden" name="unit" value="{_e(position["unit"] if position else values.get("unit", ""))}">'
    create_fields = (input_field("symbol", "标的 / USDT 永续", required=True, extra='placeholder="MU-USDT-SWAP" maxlength="40"') +
                     select_field("unit", "数量单位（后续成交沿用）", {"": "请选择单位", **UNITS}) +
                     input_field("label", "持仓标签 / 可选", extra='maxlength="80" placeholder="用于区分同标的不同持仓"')) if creating else ""
    action_field = ('<input type="hidden" name="action" value="buy"><p class="secondary">新建多头持仓 · 记录实际买入</p>' if creating else
                    select_field("action", "实际动作", {"buy": "买入 / 增加记录", "sell": "卖出 / 减少记录"}))
    source_note = (f'关联信号：{_e(source["symbol"])} · {_e(source["strategy_name"])}。只带入标的与来源，实际成交请自行填写。' if source else
                   "未关联入场信号；配置版本保持未知。") if creating else f'此笔属于 {_e(position["symbol"])} · 数量单位：{_e(UNITS[position["unit"]])}' if position else "请核对目标持仓。"
    void_field = f'<label class="confirmation"><input type="checkbox" name="voided" value="yes" {"checked" if values.get("voided") == "yes" else ""}>作废这笔成交记录（保留历史）</label>' if revising else ""
    title = "更正成交" if revising else "补录成交" if position else "记录实际成交"
    message = "已保存当前持仓状态。" if saved == "state" else "已保存实际成交记录。"
    notice = f'<p class="notice" role="alert">{_e(error)} 输入仍保留，可修改后重新提交。</p>' if error else f'<p class="save-result" role="status">{message}</p>' if saved else ''
    form = f'''<section id="position-form"><div class="section-heading"><h2>{title}</h2><a href="/positions">另建持仓</a></div>
      <p class="secondary">{source_note}</p>{notice}
      <form method="post" action="/positions">{identity_fields}<div class="position-fields">{create_fields}{action_field}
        {input_field('quantity', '实际数量', required=True, extra='inputmode="decimal" maxlength="31"')}
        {input_field('price', '实际成交价格 / USDT', required=True, extra='inputmode="decimal" maxlength="31"')}
        {input_field('executed_at', '实际成交时间 / 北京时间', kind='datetime-local', required=True, extra='step="1"')}
      </div><details><summary>可选手记：stage 与止损</summary><p class="secondary">填成交时实际掌握的状态，不知道就留空。止损手记不证明交易所已设置保护单。</p><div class="position-fields">
        {input_field('stage', '策略 stage / 非成交笔数', extra='inputmode="numeric" maxlength="2"')}
        {input_field('stop_price', '成交时手记止损 / USDT', extra='inputmode="decimal" maxlength="31"')}
      </div></details><label>{'更正 / 作废原因（必填）' if revising else '成交备注 / 可选'}<textarea name="note" rows="2" maxlength="2000" {'required' if revising else ''}>{_e(values.get('note', ''))}</textarea></label>
      {void_field}<label class="confirmation"><input type="checkbox" name="confirmed" value="yes" required {"checked" if values.get('confirmed') == 'yes' else ''}>我确认填写的是已发生的实际成交，数量单位正确。</label>
      <button type="submit">{'保存更正' if revising else '保存实际成交'}</button></form></section>''' if view["available"] or draft is not None else ''
    return _render_position_page(view, form, stylesheet=stylesheet)


def render_position_state_editor(view, *, stylesheet, position_id, draft=None, error=None):
    position = next((item for item in view["positions"] if item["position_id"] == position_id), None)
    latest = position["state_history"][-1] if position and position["state_history"] else {}
    values = {"request_id": uuid4().hex, "position_id": position_id,
              "expected_fill_sequence": str(position["fill_sequence"]) if position else "",
              "expected_state_revision": str(position["current_state"]["revision"]) if position else "",
              "stage": latest.get("stage") or "", "stop_price": latest.get("stop_price") or "", "note": ""}
    if draft is not None:
        values.update(draft)
    identity = ''.join(f'<input type="hidden" name="{name}" value="{_e(values.get(name, ""))}">' for name in
                       ("request_id", "position_id", "expected_fill_sequence", "expected_state_revision"))
    notice = f'<p class="notice" role="alert">{_e(error)} 本页输入仍保留；若记录已变化，请重新打开最新持仓核对。</p>' if error else ''
    context = f'{_e(position["symbol"])} · 已记录 {_e(position["recorded_quantity"])} {_e(UNITS[position["unit"]])}' if position else '持仓信息暂不可用'
    form = f'''<section id="position-form"><div class="section-heading"><h2>更新持仓状态</h2><a href="/position-state?position_id={_e(position_id)}#position-form">重新打开最新持仓</a></div>
      <p>{context}</p><p class="secondary">无需新增成交。填本次核对掌握的状态，不知道的字段请留空；清空表示未知。已有值来自上次确认，须按当前成交重新核对。</p>{notice}
      <form method="post" action="/position-state">{identity}<div class="position-fields">
        <label>已达到的策略阶段 / 非成交笔数<input name="stage" inputmode="numeric" maxlength="2" value="{_e(values['stage'])}" placeholder="不知道则留空"></label>
        <label>当前手记止损 / USDT<input name="stop_price" inputmode="decimal" maxlength="31" value="{_e(values['stop_price'])}" placeholder="不知道则留空"></label>
      </div><label>本次核对说明 / 可选<textarea name="note" rows="2" maxlength="2000">{_e(values['note'])}</textarea></label>
      <label class="confirmation"><input type="checkbox" name="confirmed" value="yes" required>我已核对当前成交记录，以上是我目前掌握的持仓状态。</label>
      <p class="secondary">止损手记不证明交易所已设置保护单；本页不生成策略建议或执行操作。</p><button type="submit">保存当前状态</button></form></section>''' if (position and position["status"] == "open") or draft is not None else ''
    return _render_position_page(view, form, stylesheet=stylesheet, form_label="更新状态")


def _render_position_page(view, form, *, stylesheet, form_label="记录成交"):
    cards = render_position_cards(view, editable=True)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <link rel="icon" href="data:,"><title>MU · 成交与持仓</title><style>{stylesheet}</style></head><body>
      <header class="masthead"><div class="brand">MU<span>人工成交台账</span></div><nav><a href="/">每日复盘</a><a href="#positions">已记录持仓</a><a href="#position-form">{form_label}</a></nav></header>
      <main><section class="intro"><div><p class="eyebrow">MANUAL FILLS / 人工记录</p><h1>成交与持仓</h1><p class="lede">把实际成交留下来，随时核对。</p></div><div class="snapshot"><strong>仅记录 · 不执行交易</strong><span>此页不自动更新，填写内容不会被后台刷新覆盖。</span></div></section>
      {form}<section id="positions"><div class="section-heading"><h2>已记录持仓</h2><span>所有日期 · 人工确认记录</span></div>
      <p class="scope">剩余数量按买入减卖出计算；卖出沿用当时成本均价，新增买入按剩余数量加权。未计费用，合约张数不自动换算标的数量。</p>
      {cards}<p class="secondary">只表示本台账，未核对账户全量仓位。持仓规则尚未接入，transition、实际杠杆与完整配置未知；不生成加仓、止损或退出建议。</p></section>
      <details class="evidence"><summary>本地保存位置</summary><p><code>{_e(view['path'])}</code></p><p>备份该文件可保留持仓及全部成交更正记录。</p></details>
      <footer>MU / MANUAL FILLS<span>人工确认 · 原记录保留 · 不执行交易</span></footer></main></body></html>'''
