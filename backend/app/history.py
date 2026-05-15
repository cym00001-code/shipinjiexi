"""极简 SQLite 解析历史记录."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import List, Optional

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     TEXT NOT NULL,
    type        TEXT NOT NULL,
    title       TEXT,
    author      TEXT,
    cover       TEXT,
    source_url  TEXT,
    payload     TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_note ON history(note_id);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def record(note: dict) -> int:
    payload = json.dumps(note, ensure_ascii=False)
    now = int(time.time())
    with _conn() as c:
        # 同一 note_id 去重: 删旧的, 插新的, 始终保留最新一次解析
        c.execute("DELETE FROM history WHERE note_id = ?", (note.get("note_id"),))
        cur = c.execute(
            "INSERT INTO history (note_id, type, title, author, cover, source_url, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (note.get("note_id"), note.get("type"), note.get("title"),
             note.get("author"), note.get("cover"), note.get("source_url"),
             payload, now),
        )
        return cur.lastrowid


def list_recent(limit: int = 50) -> List[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, note_id, type, title, author, cover, source_url, payload, created_at "
            "FROM history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["platform"] = (json.loads(item.pop("payload")).get("platform") or "xhs")
            except Exception:
                item.pop("payload", None)
                item["platform"] = "xhs"
            items.append(item)
        return items


def get_full(item_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT payload FROM history WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])


def delete(item_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM history WHERE id = ?", (item_id,))
        return cur.rowcount > 0


def clear_all() -> int:
    with _conn() as c:
        cur = c.execute("DELETE FROM history")
        return cur.rowcount
