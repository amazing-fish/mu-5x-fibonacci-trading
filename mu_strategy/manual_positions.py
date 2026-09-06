"""Manual fill ledger; no broker, strategy evaluation or account-position claims."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from mu_strategy.notifications.events import AlertKind
from mu_strategy.notifications.store import NotificationStore


BEIJING = timezone(timedelta(hours=8))
UNITS = {"contracts": "合约张数", "base": "标的数量"}


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("记录标识无效，请重新打开录入页。")
    return value


def _decimal(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,17})(?:\.\d{1,12})?", value):
        raise ValueError(f"{label}须为正数，最多 18 位整数、12 位小数。")
    result = Decimal(value)
    if result <= 0:
        raise ValueError(f"{label}须大于零。")
    return result


def _number(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fill(payload, *, now_ms):
    action = payload.get("action")
    if not isinstance(action, str) or action not in {"buy", "sell"}:
        raise ValueError("请选择买入或卖出。")
    quantity = _number(_decimal(payload.get("quantity"), "实际数量"))
    price = _number(_decimal(payload.get("price"), "实际价格"))
    try:
        raw_time = payload.get("executed_at", "")
        if not isinstance(raw_time, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", raw_time):
            raise ValueError()
        at = datetime.fromisoformat(raw_time)
        time_ms = int(at.replace(tzinfo=BEIJING).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("请填写有效的北京时间成交时间。") from exc
    if not 0 < time_ms <= now_ms:
        raise ValueError("成交时间须在 1970 年之后，且不能晚于当前时间。")
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError("备注最多 2000 字。")
    stage = payload.get("stage", "")
    if not isinstance(stage, str) or (stage and not re.fullmatch(r"[1-9]\d?", stage)):
        raise ValueError("stage 须为 1–99 的整数；不知道时留空。")
    stop = payload.get("stop_price", "")
    stop = _number(_decimal(stop, "手记止损")) if stop else None
    voided = payload.get("voided", "")
    if not isinstance(voided, str) or voided not in {"", "yes"}:
        raise ValueError("作废标记无效。")
    return {"action": action, "quantity": quantity, "price": price, "time_ms": time_ms,
            "stage": int(stage) if stage else None, "stop_price": stop,
            "note": note.strip(), "voided": voided == "yes"}


def _project(position, history):
    """Replay latest fill revisions by execution time, preserving submission order on ties."""
    latest, first_sequence = {}, {}
    for revision in history:
        latest[revision["fill_id"]] = revision
        first_sequence.setdefault(revision["fill_id"], revision["sequence"])
    fills = sorted(latest.values(), key=lambda row: (row["time_ms"], first_sequence[row["fill_id"]]))
    quantity, average, last = Decimal(0), Decimal(0), None
    with localcontext() as context:
        context.prec = 60
        for fill in fills:
            if fill["voided"]:
                continue
            units = _decimal(fill["quantity"], "实际数量")
            price = _decimal(fill["price"], "实际价格")
            if fill["action"] == "buy":
                if last is not None and quantity == 0:
                    raise ValueError("该笔持仓已按记录全部卖出；再次开仓请建立新持仓。")
                average = (average * quantity + price * units) / (quantity + units)
                quantity += units
            elif fill["action"] == "sell":
                if units > quantity:
                    raise ValueError("卖出数量超过当时已记录持仓；请先补录或更正此前成交。")
                quantity -= units
            else:
                raise ValueError("成交动作无效。")
            last = fill
    return {**position, "fills": fills, "history": history, "recorded_quantity": _number(quantity),
            "average_entry_price": _number(average) if quantity else None,
            "status": "open" if quantity else "closed" if last else "empty",
            "last_fill_at_ms": last["time_ms"] if last else None,
            "recorded_stage": last["stage"] if last else None,
            "recorded_stop_price": last["stop_price"] if last else None,
            "transition_state": None, "management_status": "unknown"}


class ManualPositionLedger:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir.parent / f"{self.data_dir.name}-signal-review" / "positions.sqlite3"

    def entry_source(self, event_id: str) -> dict:
        if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{64}", event_id):
            raise ValueError("关联入场事件无效。")
        store = NotificationStore(self.data_dir)
        with store.connection(readonly=True) as db:
            record = store.record(db, event_id)
        if record is None or record.event.kind is not AlertKind.ENTRY_REVIEW:
            raise ValueError("只能关联已存在的入场提醒。")
        observation = record.event.observation
        return {"event_id": event_id, "observation_id": observation.observation_id,
                "symbol": observation.symbol, "strategy_name": observation.strategy_name,
                "signal_config_fingerprint": observation.strategy_config_fingerprint,
                "trusted_run_id": observation.trusted_run_id}

    @staticmethod
    def _read(db):
        positions = [json.loads(row[0]) for row in db.execute("SELECT payload FROM positions ORDER BY rowid DESC")]
        histories = {position["position_id"]: [] for position in positions}
        for seq, position_id, fill_id, revision, recorded_at_ms, raw in db.execute(
                "SELECT sequence, position_id, fill_id, revision, recorded_at_ms, payload FROM fill_revisions ORDER BY sequence"):
            histories[position_id].append({**json.loads(raw), "sequence": seq, "fill_id": fill_id,
                                           "revision": revision, "recorded_at_ms": recorded_at_ms})
        return [_project(position, histories[position["position_id"]]) for position in positions]

    def read(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            return self._read(db)

    def view(self):
        try:
            return {"available": True, "positions": self.read(), "path": str(self.path)}
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            return {"available": False, "positions": [], "path": str(self.path)}

    def save(self, payload: dict, *, now_ms: int) -> str:
        """Commit one manual fact revision atomically; a retried request is counted once."""
        if not isinstance(payload, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in payload.items()):
            raise ValueError("录入内容无效。")
        request_id = _identifier(payload.get("request_id"))
        position_id = _identifier(payload.get("position_id"))
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.path.exists():
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                previous = db.execute("SELECT request_hash, position_id FROM fill_revisions WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError("同次提交内容已变化，请重新打开录入页后记录。")
                return previous[1]
        command = payload.get("command")
        if not isinstance(command, str) or command not in {"create", "append", "revise"}:
            raise ValueError("录入动作无效。")
        fill = _fill(payload, now_ms=now_ms)
        if payload.get("confirmed") != "yes":
            raise ValueError("请确认填写的是已经发生的实际成交。")
        if command != "revise" and fill["voided"]:
            raise ValueError("只能作废已记录成交。")
        if command == "revise" and not fill["note"]:
            raise ValueError("更正或作废须填写原因。")
        if command != "create" and not self.path.exists():
            raise ValueError("持仓记录不存在。")
        if command == "create":
            if fill["action"] != "buy":
                raise ValueError("新持仓须由一笔实际买入建立。")
            symbol, unit, label = payload.get("symbol"), payload.get("unit"), payload.get("label", "")
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,20}-USDT-SWAP", symbol):
                raise ValueError("请填写 USDT 永续标的，例如 MU-USDT-SWAP。")
            if not isinstance(unit, str) or unit not in UNITS:
                raise ValueError("请明确数量单位。")
            if not isinstance(label, str) or len(label) > 80:
                raise ValueError("持仓标签最多 80 字。")
            source = self.entry_source(payload["event_id"]) if payload.get("event_id") else None
            if source and source["symbol"] != symbol:
                raise ValueError("成交标的与关联信号不一致。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS positions (position_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS fill_revisions (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "request_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, position_id TEXT NOT NULL, "
                       "fill_id TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at_ms INTEGER NOT NULL, "
                       "payload TEXT NOT NULL, UNIQUE(fill_id, revision))")
            previous = db.execute("SELECT request_hash, position_id FROM fill_revisions WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError("同次提交内容已变化，请重新打开录入页后记录。")
                return previous[1]
            positions = {row["position_id"]: row for row in self._read(db)}
            if command == "create":
                if position_id in positions:
                    raise ValueError("该持仓已存在，请从卡片补录成交。")
                position = {"position_id": position_id, "symbol": symbol, "unit": unit, "direction": "long",
                            "label": label.strip(), "source": "manual_confirmation", "signal_source": source,
                            "created_at_ms": now_ms}
                db.execute("INSERT INTO positions VALUES (?, ?)", (position_id, json.dumps(position)))
                history = []
            else:
                if position_id not in positions:
                    raise ValueError("持仓记录不存在。")
                position = positions[position_id]
                history = position["history"]
                if payload.get("unit") != position["unit"]:
                    raise ValueError("数量单位须与此笔持仓一致，请重新打开录入页核对。")
            fill_id, revision = request_id, 1
            if command == "revise":
                fill_id = _identifier(payload.get("fill_id"))
                previous_fill = next((row for row in position["fills"] if row["fill_id"] == fill_id), None)
                if previous_fill is None:
                    raise ValueError("成交不属于此持仓。")
                if payload.get("expected_revision") != str(previous_fill["revision"]):
                    raise ValueError("这笔成交已被更正，请重新打开后核对。")
                revision = previous_fill["revision"] + 1
            candidate = {**fill, "fill_id": fill_id, "revision": revision,
                         "sequence": max((row["sequence"] for row in history), default=0) + 1,
                         "recorded_at_ms": now_ms}
            _project(position, [*history, candidate])
            db.execute("INSERT INTO fill_revisions (request_id, request_hash, position_id, fill_id, revision, recorded_at_ms, payload) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (request_id, digest, position_id, fill_id, revision, now_ms, json.dumps(fill)))
        return position_id
