"""SQLite storage: conversations (API messages), what the UI shows, and usage counts."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    catalog TEXT NOT NULL,          -- master-data snapshot frozen into the system prompt
    messages TEXT NOT NULL,         -- Anthropic API messages, append-only
    pending TEXT,                   -- tool calls waiting for the user
    notes TEXT NOT NULL DEFAULT '[]'  -- things to tell the model on the next turn
);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations (user, updated_at);
CREATE TABLE IF NOT EXISTS display (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    item TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS display_conversation ON display (conversation_id, id);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    user TEXT NOT NULL,
    model TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user, model)
);
"""


@dataclass
class Conversation:
    id: str
    user: str
    catalog: str
    messages: list[dict] = field(default_factory=list)
    pending: dict | None = None
    notes: list[str] = field(default_factory=list)
    updated_at: float = 0.0


class DB:
    def __init__(self, path=None):
        path = path or config.DB_PATH
        if str(path) != ":memory:":
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------ conversations

    def create(self, user: str, catalog: str) -> Conversation:
        conv = Conversation(id=uuid.uuid4().hex, user=user, catalog=catalog, updated_at=time.time())
        self.conn.execute(
            "INSERT INTO conversations (id, user, created_at, updated_at, catalog, messages) VALUES (?,?,?,?,?,?)",
            (conv.id, user, conv.updated_at, conv.updated_at, catalog, "[]"),
        )
        return conv

    def _load(self, row: sqlite3.Row | None) -> Conversation | None:
        if not row:
            return None
        return Conversation(
            id=row["id"],
            user=row["user"],
            catalog=row["catalog"],
            messages=json.loads(row["messages"]),
            pending=json.loads(row["pending"]) if row["pending"] else None,
            notes=json.loads(row["notes"]),
            updated_at=row["updated_at"],
        )

    def get(self, conversation_id: str, user: str) -> Conversation | None:
        return self._load(
            self.conn.execute("SELECT * FROM conversations WHERE id=? AND user=?", (conversation_id, user)).fetchone()
        )

    def latest(self, user: str, max_idle_seconds: float) -> Conversation | None:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE user=? AND updated_at>? ORDER BY updated_at DESC LIMIT 1",
            (user, time.time() - max_idle_seconds),
        ).fetchone()
        return self._load(row)

    def save(self, conv: Conversation) -> None:
        conv.updated_at = time.time()
        self.conn.execute(
            "UPDATE conversations SET updated_at=?, messages=?, pending=?, notes=? WHERE id=?",
            (
                conv.updated_at,
                json.dumps(conv.messages, ensure_ascii=False),
                json.dumps(conv.pending, ensure_ascii=False) if conv.pending else None,
                json.dumps(conv.notes, ensure_ascii=False),
                conv.id,
            ),
        )

    # ------------------------------------------------------------ display

    def show(self, conversation_id: str, item: dict) -> dict:
        cur = self.conn.execute(
            "INSERT INTO display (conversation_id, created_at, item) VALUES (?,?,?)",
            (conversation_id, time.time(), json.dumps(item, ensure_ascii=False)),
        )
        return {**item, "id": cur.lastrowid}

    def update_shown(self, display_id: int, item: dict) -> None:
        item = {k: v for k, v in item.items() if k != "id"}
        self.conn.execute("UPDATE display SET item=? WHERE id=?", (json.dumps(item, ensure_ascii=False), display_id))

    def shown(self, conversation_id: str, display_id: int | None = None) -> list[dict]:
        sql, args = "SELECT id, item FROM display WHERE conversation_id=?", [conversation_id]
        if display_id is not None:
            sql, args = sql + " AND id=?", args + [display_id]
        rows = self.conn.execute(sql + " ORDER BY id", args).fetchall()
        return [{**json.loads(r["item"]), "id": r["id"]} for r in rows]

    # -------------------------------------------------------------- usage

    def record_usage(self, user: str, model: str, usage: Any, new_turn: bool) -> None:
        day = time.strftime("%Y-%m-%d")
        self.conn.execute("INSERT OR IGNORE INTO usage (day, user, model) VALUES (?,?,?)", (day, user, model))
        self.conn.execute(
            "UPDATE usage SET turns=turns+?, requests=requests+1, input_tokens=input_tokens+?, "
            "output_tokens=output_tokens+?, cache_read_tokens=cache_read_tokens+?, "
            "cache_write_tokens=cache_write_tokens+? WHERE day=? AND user=? AND model=?",
            (
                1 if new_turn else 0,
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
                getattr(usage, "cache_read_input_tokens", 0) or 0,
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
                day, user, model,
            ),
        )

    def turns_today(self, user: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(turns),0) AS n FROM usage WHERE day=? AND user=?", (time.strftime("%Y-%m-%d"), user)
        ).fetchone()
        return int(row["n"])

    def usage_summary(self, days: int = 30) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM usage ORDER BY day DESC, user LIMIT ?", (days * 20,)).fetchall()
        return [dict(r) for r in rows]
