"""Small personal annotations, separate from signal and delivery evidence."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path


FEEDBACK_STATUSES = {
    "unreviewed": "未处理", "seen": "已查看", "traded": "已手动交易", "skipped": "已跳过",
}


class SignalFeedbackStore:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir).resolve()
        self.path = data_dir.parent / f"{data_dir.name}-signal-review" / "feedback.sqlite3"

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return {row["event_id"]: dict(row) for row in db.execute("SELECT * FROM feedback")}

    def save(self, event_id: str, status: str, note: str, *, now_ms: int) -> dict:
        if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{64}", event_id):
            raise ValueError("invalid event id")
        if not isinstance(status, str) or status not in FEEDBACK_STATUSES:
            raise ValueError("invalid feedback status")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("note must contain at most 2000 characters")
        record = {"event_id": event_id, "status": status, "note": note.strip(), "updated_at_ms": now_ms}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS feedback (event_id TEXT PRIMARY KEY, status TEXT NOT NULL, note TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)")
            db.execute("INSERT INTO feedback VALUES (:event_id, :status, :note, :updated_at_ms) "
                       "ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,note=excluded.note,updated_at_ms=excluded.updated_at_ms", record)
        return record
