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
from mu_strategy.position_management import MAX_MAPPED_FILLS, baseline_configuration, project_rule_fills
from mu_strategy.research.strategy_releases import StrategyConfigPayloadV1


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


def _stage(value):
    if not isinstance(value, str) or (value and not re.fullmatch(r"[1-9]\d?", value)):
        raise ValueError("stage 须为 1–99 的整数；不知道时留空。")
    return int(value) if value else None


def _note(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("备注最多 2000 字。")
    return value.strip()


def _request_identity(payload):
    if not isinstance(payload, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in payload.items()):
        raise ValueError("录入内容无效。")
    return (_identifier(payload.get("request_id")), _identifier(payload.get("position_id")),
            hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest())


def _saved_request(db, table, request_id, digest):
    # The table name is supplied only by the ledger methods below.
    previous = db.execute(f"SELECT request_hash, position_id FROM {table} WHERE request_id=?", (request_id,)).fetchone()
    if previous and previous[0] != digest:
        raise ValueError("同次提交内容已变化，请重新打开录入页后记录。")
    return previous[1] if previous else None


def _has_state_table(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='position_state_revisions'").fetchone() is not None


def _has_management_table(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='position_management_revisions'").fetchone() is not None


def _management_record(record, position):
    """Validate the versioned manual input contract without selecting new defaults."""
    expected = {"schema_version", "strategy_name", "strategy_rule_id", "configuration", "configuration_sha256",
                "configuration_source", "entry_anchor", "initial_stop_price", "actual_leverage", "leverage_source",
                "fill_stages", "fill_revisions", "fill_sequence", "state_revision", "confirmed_at_ms", "note"}
    if not isinstance(record, dict) or set(record) != expected or type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("invalid management input record")
    configuration = StrategyConfigPayloadV1.from_dict(record["configuration"])
    if (configuration.strategy_config_sha256 != record["configuration_sha256"]
            or configuration.to_strategy_config().symbol != position["symbol"]
            or record["strategy_name"] != "baseline"
            or not isinstance(record["strategy_rule_id"], str)
            or not re.fullmatch(r"[a-z0-9]+(?:[._][a-z0-9]+)*\.v[1-9]\d*", record["strategy_rule_id"])
            or record["configuration_source"] != "manual_baseline_selection"):
        raise ValueError("invalid frozen management configuration")
    for name in ("entry_anchor", "initial_stop_price", "actual_leverage"):
        if record[name] is not None:
            _decimal(record[name], name)
    if record["leverage_source"] != ("manual_confirmation" if record["actual_leverage"] is not None else None):
        raise ValueError("invalid leverage source")
    for name, minimum in (("fill_sequence", 1), ("state_revision", 0), ("confirmed_at_ms", 1)):
        if type(record[name]) is not int or record[name] < minimum:
            raise ValueError("invalid management revision")
    _note(record["note"])
    stages, revisions = record["fill_stages"], record["fill_revisions"]
    if not isinstance(stages, dict) or not isinstance(revisions, dict) or set(stages) != set(revisions) or not 1 <= len(stages) <= MAX_MAPPED_FILLS:
        raise ValueError("invalid management fill mapping")
    for identity, stage in stages.items():
        _identifier(identity)
        if stage is not None and (type(stage) is not int or not 1 <= stage <= 99):
            raise ValueError("invalid mapped stage")
        if type(revisions[identity]) is not int or revisions[identity] < 1:
            raise ValueError("invalid mapped fill revision")
    return record


def _current_management(position, history):
    latest = history[-1] if history else None
    status = ("not_open" if position["status"] != "open" else "unconfigured" if latest is None else
              "needs_review" if (latest["fill_sequence"] != position["fill_sequence"]
                                 or latest["state_revision"] != position["current_state"]["revision"]) else "confirmed")
    return {"status": status, "revision": latest["revision"] if latest else 0, "latest": latest}


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
    note = _note(payload.get("note", ""))
    stage = _stage(payload.get("stage", ""))
    stop = payload.get("stop_price", "")
    stop = _number(_decimal(stop, "手记止损")) if stop else None
    voided = payload.get("voided", "")
    if not isinstance(voided, str) or voided not in {"", "yes"}:
        raise ValueError("作废标记无效。")
    return {"action": action, "quantity": quantity, "price": price, "time_ms": time_ms,
            "stage": stage, "stop_price": stop,
            "note": note, "voided": voided == "yes"}


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
            "fill_sequence": max((row["sequence"] for row in history), default=0),
            "average_entry_price": _number(average) if quantity else None,
            "status": "open" if quantity else "closed" if last else "empty",
            "last_fill_at_ms": last["time_ms"] if last else None,
            "recorded_stage": last["stage"] if last else None,
            "recorded_stop_price": last["stop_price"] if last else None,
            "transition_state": None, "management_status": "unknown"}


def _current_state(position, history):
    latest = history[-1] if history else None
    status = ("not_open" if position["status"] != "open" else "unconfirmed" if latest is None else
              "needs_review" if latest["fill_sequence"] != position["fill_sequence"] else "confirmed")
    return {"status": status, "revision": latest["revision"] if latest else 0,
            "stage": latest["stage"] if status == "confirmed" else None,
            "stop_price": latest["stop_price"] if status == "confirmed" else None,
            "confirmed_at_ms": latest["confirmed_at_ms"] if latest else None}


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
        states = {position["position_id"]: [] for position in positions}
        if _has_state_table(db):
            for position_id, revision, raw in db.execute("SELECT position_id, revision, payload FROM position_state_revisions ORDER BY revision"):
                state = json.loads(raw)
                if (not isinstance(state, dict) or set(state) != {"stage", "stop_price", "note", "fill_sequence", "confirmed_at_ms"}
                        or type(state["fill_sequence"]) is not int or state["fill_sequence"] <= 0
                        or type(state["confirmed_at_ms"]) is not int or state["confirmed_at_ms"] <= 0):
                    raise ValueError("invalid position state record")
                if state["stage"] is not None and (type(state["stage"]) is not int or not 1 <= state["stage"] <= 99):
                    raise ValueError("invalid position stage")
                if state["stop_price"] is not None:
                    _decimal(state["stop_price"], "当前手记止损")
                _note(state["note"])
                states[position_id].append({**state, "revision": revision})
        management = {position["position_id"]: [] for position in positions}
        if _has_management_table(db):
            identities = {position["position_id"]: position for position in positions}
            for position_id, revision, raw in db.execute("SELECT position_id, revision, payload FROM position_management_revisions ORDER BY revision"):
                record = _management_record(json.loads(raw), identities[position_id])
                if type(revision) is not int or revision != len(management[position_id]) + 1:
                    raise ValueError("invalid management history sequence")
                management[position_id].append({**record, "revision": revision})
        result = []
        for position in positions:
            projected = _project(position, histories[position["position_id"]])
            history = states[position["position_id"]]
            projected.update(current_state=_current_state(projected, history), state_history=history)
            management_history = management[position["position_id"]]
            result.append({**projected, "management_inputs": _current_management(projected, management_history),
                           "management_history": management_history})
        return result

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
        request_id, position_id, digest = _request_identity(payload)
        if self.path.exists():
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                previous = _saved_request(db, "fill_revisions", request_id, digest)
            if previous:
                return previous
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
            previous = _saved_request(db, "fill_revisions", request_id, digest)
            if previous:
                return previous
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

    def save_state(self, payload: dict, *, now_ms: int) -> str:
        """Record a manual state confirmation against the exact fill and state revisions."""
        request_id, position_id, digest = _request_identity(payload)
        if not self.path.exists():
            raise ValueError("持仓记录不存在。")
        stage = _stage(payload.get("stage", ""))
        stop = payload.get("stop_price", "")
        stop = _number(_decimal(stop, "当前手记止损")) if stop else None
        note = _note(payload.get("note", ""))
        if payload.get("confirmed") != "yes":
            raise ValueError("请确认这些信息对应当前已记录的持仓；不知道的字段请留空。")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if _has_state_table(db):
                previous = _saved_request(db, "position_state_revisions", request_id, digest)
                if previous:
                    return previous
            position = next((item for item in self._read(db) if item["position_id"] == position_id), None)
            if position is None or position["status"] != "open":
                raise ValueError("仅可确认仍有已记录数量的持仓。")
            if (payload.get("expected_fill_sequence") != str(position["fill_sequence"])
                    or payload.get("expected_state_revision") != str(position["current_state"]["revision"])):
                raise ValueError("成交或持仓状态已变化，请重新打开最新持仓核对后再保存。")
            state = {"stage": stage, "stop_price": stop, "note": note,
                     "fill_sequence": position["fill_sequence"], "confirmed_at_ms": now_ms}
            db.execute("CREATE TABLE IF NOT EXISTS position_state_revisions (position_id TEXT NOT NULL, "
                       "revision INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, "
                       "payload TEXT NOT NULL, PRIMARY KEY (position_id, revision))")
            db.execute("INSERT INTO position_state_revisions VALUES (?, ?, ?, ?, ?)",
                       (position_id, position["current_state"]["revision"] + 1, request_id, digest, json.dumps(state)))
        return position_id

    def save_management(self, payload: dict, *, now_ms: int) -> str:
        """Freeze explicit rule inputs against the current fill, state and input revisions."""
        request_id, position_id, digest = _request_identity(payload)
        if not self.path.exists():
            raise ValueError("持仓记录不存在。")
        if payload.get("confirmed") != "yes":
            raise ValueError("请确认本次选择的规则配置、已知参数与成交阶段归属。")
        parameters = {}
        for name, label in (("entry_anchor", "加仓基准价"), ("initial_stop_price", "初始止损"), ("actual_leverage", "实际杠杆")):
            value = payload.get(name, "")
            parameters[name] = _number(_decimal(value, label)) if value else None
        note = _note(payload.get("note", ""))
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if _has_management_table(db):
                previous = _saved_request(db, "position_management_revisions", request_id, digest)
                if previous:
                    return previous
            position = next((item for item in self._read(db) if item["position_id"] == position_id), None)
            if position is None or position["status"] != "open":
                raise ValueError("仅可为仍有已记录数量的持仓确认管理输入。")
            if any(payload.get(field) != str(value) for field, value in (
                    ("expected_fill_sequence", position["fill_sequence"]),
                    ("expected_state_revision", position["current_state"]["revision"]),
                    ("expected_management_revision", position["management_inputs"]["revision"]))):
                raise ValueError("成交、持仓状态或管理输入已变化，请重新打开最新持仓核对。")
            previous = position["management_inputs"]["latest"]
            template = previous or baseline_configuration(position["symbol"])
            if payload.get("configuration_sha256") != template["configuration_sha256"]:
                raise ValueError("本页规则配置已变化，请重新打开后核对完整参数。")
            buys = [row for row in position["fills"] if not row["voided"] and row["action"] == "buy"]
            if not 1 <= len(buys) <= MAX_MAPPED_FILLS:
                raise ValueError(f"当前规则复核最多支持 {MAX_MAPPED_FILLS} 笔有效买入的阶段映射，成交台账仍可正常使用。")
            supplied = {name.removeprefix("fill_stage_"): _stage(value) for name, value in payload.items() if name.startswith("fill_stage_")}
            if set(supplied) != {row["fill_id"] for row in buys}:
                raise ValueError("请逐笔核对当前全部有效买入的阶段；不知道时保留空白。")
            record = {"schema_version": 1,
                      **{name: template[name] for name in ("strategy_name", "strategy_rule_id", "configuration", "configuration_sha256")},
                      "configuration_source": "manual_baseline_selection", **parameters,
                      "leverage_source": "manual_confirmation" if parameters["actual_leverage"] is not None else None,
                      "fill_stages": supplied, "fill_revisions": {row["fill_id"]: row["revision"] for row in buys},
                      "fill_sequence": position["fill_sequence"], "state_revision": position["current_state"]["revision"],
                      "confirmed_at_ms": now_ms, "note": note}
            _management_record(record, position)
            if all(stage is not None for stage in supplied.values()) and position["current_state"]["stage"] is not None:
                project_rule_fills(position, record)
            db.execute("CREATE TABLE IF NOT EXISTS position_management_revisions (position_id TEXT NOT NULL, "
                       "revision INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, "
                       "payload TEXT NOT NULL, PRIMARY KEY (position_id, revision))")
            db.execute("INSERT INTO position_management_revisions VALUES (?, ?, ?, ?, ?)",
                       (position_id, position["management_inputs"]["revision"] + 1, request_id, digest, json.dumps(record)))
        return position_id
