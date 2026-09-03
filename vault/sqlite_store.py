"""Shell Cracked SQLite layer — agent state, heartbeats, session events."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from bhive import evaluate_agent, minute_slot, validate_agent

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    last_seen   REAL NOT NULL,
    last_slot   INTEGER NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    ts          REAL NOT NULL,
    slot        INTEGER NOT NULL,
    source      TEXT NOT NULL,
    FOREIGN KEY (agent) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_hb_agent_ts ON heartbeats(agent, ts DESC);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    agent       TEXT,
    body        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    context     TEXT NOT NULL
);
"""


class SqliteVault:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_heartbeat(
        self,
        agent: str,
        ts: float | None = None,
        slot: int | None = None,
        source: str = "agent",
    ) -> dict[str, Any]:
        name = validate_agent(agent)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        now = time.time() if ts is None else float(ts)
        if now < 0:
            raise ValueError("ts must be >= 0")
        computed_slot = minute_slot(now)
        use_slot = computed_slot if slot is None else int(slot)
        if abs(use_slot - computed_slot) > 1:
            raise ValueError("bhive_slot is more than one minute off the clock")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agents (id, status, last_seen, last_slot, updated_at)
                VALUES (?, 'running', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status='running', last_seen=excluded.last_seen,
                    last_slot=excluded.last_slot, updated_at=excluded.updated_at
                """,
                (name, now, use_slot, now),
            )
            self._conn.execute(
                "INSERT INTO heartbeats (agent, ts, slot, source) VALUES (?, ?, ?, ?)",
                (name, now, use_slot, source.strip()),
            )
            self._conn.commit()
        return {"agent": name, "ts": now, "slot": use_slot, "status": "running"}

    def log_event(self, kind: str, body: dict[str, Any], agent: str | None = None) -> int:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("kind must be a non-empty string")
        if not isinstance(body, dict):
            raise TypeError("body must be a dict")
        agent_id = validate_agent(agent) if agent is not None else None
        payload = json.dumps(body, default=str)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, kind, agent, body) VALUES (?, ?, ?, ?)",
                (time.time(), kind.strip(), agent_id, payload),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def upsert_session(self, session_id: str, context: dict[str, Any]) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(context, dict):
            raise TypeError("context must be a dict")
        now = time.time()
        blob = json.dumps(context, default=str)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (id, started_at, context) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET context=excluded.context
                """,
                (session_id.strip(), now, blob),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        with self._lock:
            row = self._conn.execute(
                "SELECT id, started_at, context FROM sessions WHERE id = ?",
                (session_id.strip(),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "context": json.loads(row["context"]),
        }

    def list_agents(self, now_seconds: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now_seconds is None else float(now_seconds)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, status, last_seen, last_slot, updated_at FROM agents"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            status = evaluate_agent(row["id"], row["last_seen"], row["last_slot"], now)
            out.append({
                "id": row["id"],
                "stored_status": row["status"],
                **status.as_dict(),
                "updated_at": row["updated_at"],
            })
        return out

    def recent_heartbeats(self, agent: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or limit < 1 or limit > 500:
            raise ValueError("limit must be an int in 1..500")
        with self._lock:
            if agent is None:
                rows = self._conn.execute(
                    "SELECT agent, ts, slot, source FROM heartbeats ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                name = validate_agent(agent)
                rows = self._conn.execute(
                    "SELECT agent, ts, slot, source FROM heartbeats WHERE agent = ? ORDER BY ts DESC LIMIT ?",
                    (name, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or limit < 1 or limit > 500:
            raise ValueError("limit must be an int in 1..500")
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, kind, agent, body FROM events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "ts": r["ts"],
                "kind": r["kind"],
                "agent": r["agent"],
                "body": json.loads(r["body"]),
            })
        return out
