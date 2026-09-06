"""On-demand, read-only baseline review of explicitly confirmed manual inputs."""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from pathlib import Path

from mu_strategy.canonical import canonical_sha256
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.indicators import macd, rsi
from mu_strategy.live_exit import evaluate_exit
from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.research.strategy_releases import StrategyConfigPayloadV1
from mu_strategy.strategies.position_rules import PositionFillSnapshot, PositionStateSnapshot, decide_pyramid_add
from mu_strategy.strategies.registry import baseline_strategy_group


BAR_MS = 900_000
DAY_MS = 86_400_000
MAX_MAPPED_FILLS = 32


def baseline_configuration(symbol):
    """The selectable template, not a reconstruction of the entry signal."""
    group = baseline_strategy_group(symbol)
    payload = StrategyConfigPayloadV1.from_config(group.config)
    return {"strategy_name": group.name, "strategy_rule_id": group.rule.strategy_rule_id,
            "configuration": payload.to_dict(), "configuration_sha256": payload.strategy_config_sha256}


def project_rule_fills(position, inputs):
    """Aggregate explicitly mapped buys by strategy stage; never create ledger fills."""
    buys = [row for row in position["fills"] if not row["voided"] and row["action"] == "buy"]
    stages = inputs["fill_stages"]
    if set(stages) != {row["fill_id"] for row in buys}:
        raise ValueError("请逐笔核对全部有效买入所属的策略阶段。")
    if inputs["fill_revisions"] != {row["fill_id"]: row["revision"] for row in buys}:
        raise ValueError("成交修订已变化，请重新核对阶段映射。")
    sequence = [stages[row["fill_id"]] for row in buys]
    if not sequence or any(type(stage) is not int for stage in sequence):
        raise ValueError("买入所属阶段尚未全部确认；成交笔数不会自动转换为阶段。")
    if sequence != sorted(sequence) or set(sequence) != set(range(1, max(sequence) + 1)):
        raise ValueError("阶段须按成交时间从 1 连续推进；同一阶段可以包含多笔买入。")
    if max(sequence) != position["current_state"]["stage"]:
        raise ValueError("买入阶段映射与已确认的当前阶段不一致。")
    projected = []
    with localcontext() as context:
        context.prec = 60
        for stage in range(1, max(sequence) + 1):
            rows = [row for row in buys if stages[row["fill_id"]] == stage]
            units = sum(Decimal(row["quantity"]) for row in rows)
            price = sum(Decimal(row["quantity"]) * Decimal(row["price"]) for row in rows) / units
            projected.append({"stage": stage, "time_ms": rows[-1]["time_ms"], "price": float(price),
                              "units": float(units),
                              "sources": [{"fill_id": row["fill_id"], "revision": row["revision"]} for row in rows]})
    return projected


def management_checks(position):
    """Explain the supported input subset without reading market data."""
    current = position["current_state"]
    management = position["management_inputs"]
    inputs = management["latest"]
    active = [row for row in position["fills"] if not row["voided"]]
    checks = [
        {"key": "position", "label": "仍有已记录的多头持仓", "ok": position["status"] == "open"},
        {"key": "state", "label": "当前阶段与手记止损已确认", "ok":
         current["status"] == "confirmed" and current["stage"] is not None and current["stop_price"] is not None},
        {"key": "buys_only", "label": "当前支持有效买入，卖出后的规则映射尚未接入", "ok":
         bool(active) and all(row["action"] == "buy" for row in active)},
        {"key": "management", "label": "本次管理输入与成交、状态版本一致", "ok": management["status"] == "confirmed"},
        {"key": "configuration", "label": "已明确选用并冻结受支持的 baseline 配置", "ok": False},
        {"key": "anchors", "label": "加仓基准价与初始止损已人工确认", "ok":
         inputs is not None and inputs["entry_anchor"] is not None and inputs["initial_stop_price"] is not None},
        {"key": "leverage", "label": "实际杠杆已人工确认（不从配置默认值推定）", "ok":
         inputs is not None and inputs["actual_leverage"] is not None},
        {"key": "mapping", "label": "全部有效买入已明确映射到连续策略阶段", "ok": False},
    ]
    if inputs is not None:
        config = StrategyConfigPayloadV1.from_dict(inputs["configuration"]).to_strategy_config()
        group = baseline_strategy_group(position["symbol"])
        checks[4]["ok"] = (
            inputs["strategy_name"] == "baseline" and inputs["strategy_rule_id"] == group.rule.strategy_rule_id
            and config.symbol == position["symbol"] and config.stop_tightening == "baseline"
            and config.yellow_stop_tightening is None and config.green_stop_tightening is None
        )
        if current["stage"] is not None and not 1 <= current["stage"] <= len(config.margin_steps):
            checks[4].update(ok=False, label="当前阶段超出已选 baseline 的阶段范围，暂不能复核。")
        try:
            project_rule_fills(position, inputs)
        except ValueError as exc:
            checks[-1]["label"] = str(exc)
        else:
            checks[-1]["ok"] = True
    return checks


def review_position(position, data_dir: Path, *, now_ms: int, loader=None):
    """Review a single position against one pinned, strictly validated current generation.

    Each candle uses the same confirmed actual snapshot. Proposed additions and
    stop changes are never applied to that snapshot, even on a later candle.
    """
    checks = management_checks(position)
    result = {"status": "unknown", "checks": checks, "evaluated_at_ms": now_ms,
              "messages": [item["label"] for item in checks if not item["ok"]], "evaluation": None}
    if not all(item["ok"] for item in checks):
        if position["status"] != "open":
            result["status"] = "not_open"
        elif position["management_inputs"]["status"] == "needs_review" or position["current_state"]["status"] == "needs_review":
            result["status"] = "needs_review"
        elif not checks[2]["ok"] or (position["management_inputs"]["latest"] is not None and not checks[4]["ok"]):
            result["status"] = "unsupported"
        return result

    inputs = position["management_inputs"]["latest"]
    state = position["current_state"]
    confirmed_at = max(inputs["confirmed_at_ms"], state["confirmed_at_ms"])
    first_open = ((confirmed_at + BAR_MS - 1) // BAR_MS) * BAR_MS
    result.update(confirmed_at_ms=confirmed_at, first_eligible_open_ms=first_open)
    if now_ms < confirmed_at:
        return {**result, "status": "data_blocked", "messages": ["查询时间早于人工确认时间，暂不能复核。"]}
    # Include indicator context before the entire review interval. A bounded
    # request must not silently drop the beginning of a long-lived confirmation.
    days = max(7, (now_ms - first_open + DAY_MS - 1) // DAY_MS + 2)
    if days > 180:
        return {**result, "status": "data_blocked", "messages": ["本次确认后的区间超过复核上限，请核对历史记录后重新确认状态。"]}
    loader = loader or LoadTrustedBundle(TrustedDataStore(data_dir=Path(data_dir)))
    try:
        context = loader.open_context(now_ms=now_ms)
        bundle = loader.execute(LoadTrustedBundleQuery(position["symbol"], ("15m", "1h"), days, now_ms),
                                trading_strict_policy(), context=context)
        if not bundle.trust_decision.allowed:
            return {**result, "status": "data_blocked",
                    "messages": ["可信行情未通过当前交易严格校验，请检查行情来源后重新复核。"],
                    "data_reason": bundle.trust_decision.reason.value}
        if bundle.load_context != context or not bundle.run_id or bundle.run_id != context.manifest.run_id:
            raise ValueError("unpinned management input")
        candles = bundle.candles_by_interval["15m"]
        hourly = bundle.candles_by_interval["1h"]
        # The loader owns integrity validation. These checks enforce this
        # review's complete post-confirmation range and causal indicator input.
        for rows, interval_ms in ((candles, BAR_MS), (hourly, 3_600_000)):
            if not rows or rows[-1].open_time_ms + interval_ms > now_ms or any(
                    right.open_time_ms - left.open_time_ms != interval_ms for left, right in zip(rows, rows[1:])):
                raise ValueError("incomplete closed-candle sequence")
        first_index = next((index for index, candle in enumerate(candles) if candle.open_time_ms >= first_open), None)
        hashes = {interval: bundle.health_by_interval[interval].content_sha256 for interval in ("5m", "15m", "1h")}
        if not all(hashes.values()):
            raise ValueError("missing canonical content hashes")
        provenance = {"generation_id": context.generation_id, "run_id": bundle.run_id,
                      "content_sha256": hashes, "configuration_sha256": inputs["configuration_sha256"],
                      "strategy_rule_id": inputs["strategy_rule_id"], "fill_sequence": position["fill_sequence"],
                      "state_revision": state["revision"], "management_revision": inputs["revision"],
                      "requested_days": days, "indicator_start_ms": candles[0].open_time_ms,
                      "hourly_start_ms": hourly[0].open_time_ms,
                      "evaluated_at_ms": now_ms, "leverage_source": "manual_confirmation"}
        result["provenance"] = provenance
        if first_index is None:
            return {**result, "status": "waiting", "messages": ["等待确认后的第一根完整 15m K 线；不会把当前止损回投到确认前的行情。"]}
        if candles[first_index].open_time_ms != first_open or first_index < 35 or sum(
                candle.open_time_ms + 3_600_000 <= first_open for candle in hourly) < 35:
            raise ValueError("review range or indicator history is incomplete")
        config_payload = StrategyConfigPayloadV1.from_dict(inputs["configuration"])
        # Keep the selected complete baseline immutable. Only this risk input
        # uses the separately confirmed actual leverage, with separate identity.
        config = replace(config_payload.to_strategy_config(), leverage=float(inputs["actual_leverage"]))
        effective_payload = StrategyConfigPayloadV1.from_config(config)
        provenance["effective_configuration_sha256"] = effective_payload.strategy_config_sha256
        projected = project_rule_fills(position, inputs)
        snapshot = PositionStateSnapshot(
            fills=tuple(PositionFillSnapshot(item["time_ms"], item["price"], item["units"]) for item in projected),
            stop_price=float(state["stop_price"]), entry_anchor=float(inputs["entry_anchor"]),
            initial_stop_price=float(inputs["initial_stop_price"]), max_stage=state["stage"],
            # Baseline does not consume delayed-transition state. These zeros
            # are inert inputs, not a claim about actual transition history.
            stop_transition_fill_count=0, stop_transition_start=0.0,
        )
        hourly_context = build_hourly_context(candles, hourly)
        closes = [candle.close for candle in candles]
        rsi_values, hist = rsi(closes), macd(closes)[2]
        earliest_exit = None
        latest = None
        for index in range(first_index, len(candles)):
            latest = evaluate_exit(snapshot, candles[index], index=index, candles=candles,
                                   regime=hourly_context[candles[index].open_time_ms], config=config)
            if latest.exit_triggered and earliest_exit is None:
                earliest_exit = asdict(latest)
        last = candles[-1]
        addition = None
        if earliest_exit is None:
            addition = asdict(decide_pyramid_add(
                snapshot, last, rsi_value=rsi_values[-1], macd_hist=hist[-1], previous_macd_hist=hist[-2],
                regime=hourly_context[last.open_time_ms], config=config,
            ))
        evaluation = {
            "outcome": "exit_review" if earliest_exit else "add_candidate" if addition["should_add"] else
                       "stop_review" if latest.stop_after_candle_if_open > snapshot.stop_price else "no_action",
            "first_open_ms": first_open, "last_open_ms": last.open_time_ms, "checked_through_ms": last.open_time_ms + BAR_MS,
            "candle_count": len(candles) - first_index, "earliest_exit": earliest_exit,
            "confirmed_stop": snapshot.stop_price, "suggested_stop": latest.stop_after_candle_if_open if not earliest_exit else None,
            "close_below_suggested_stop": latest.latest_close_at_or_below_tightened_stop if not earliest_exit else None,
            "latest_close": last.close, "regime": hourly_context[last.open_time_ms], "addition": addition,
            "projected_fills": projected, "actual_leverage": inputs["actual_leverage"],
            "transition_state": "not_used_by_baseline",
        }
        provenance["review_identity"] = canonical_sha256({
            "position_id": position["position_id"], "provenance": {key: value for key, value in provenance.items() if key != "evaluated_at_ms"},
            "first_open_ms": first_open, "last_open_ms": last.open_time_ms,
        })
        return {**result, "status": "evaluated", "messages": [], "evaluation": evaluation}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return {**result, "status": "data_blocked", "messages": ["行情或规则输入不足以完整复核本次确认后的区间，请检查来源与覆盖范围。"]}
