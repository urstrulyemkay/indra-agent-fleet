"""SQLite-backed status bus.

Every agent writes heartbeats and log lines here. The dashboard reads from it
via Server-Sent Events. Single file, zero setup, fine for light cadence and
single-VPS deployment.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DB_PATH = Path(os.getenv("FLEET_DB_PATH", "./data/status.db"))
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _restrict_perms() -> None:
    """Lock down data dir + DB file permissions to owner-only."""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(DB_PATH.parent, 0o700)
    except Exception:
        pass
    if DB_PATH.exists():
        try:
            os.chmod(DB_PATH, 0o600)
        except Exception:
            pass


def init_db() -> None:
    _restrict_perms()
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                name TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                current_task TEXT,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                meta_json TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                data_json TEXT,
                ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL,
                meta_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_s REAL,
                artifact_id INTEGER,
                error TEXT,
                meta_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_name, started_at DESC);

            -- Individual approvable drafts (one row per public-reply or DM, etc.)
            -- so the /queue surface can show each as a discrete approve/reject item.
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artifact_id INTEGER,
                agent_name TEXT NOT NULL,
                kind TEXT NOT NULL,              -- public_reply / dm / email / etc.
                target_handle TEXT,              -- @somebody, or email, or anonymous
                source_text TEXT,                -- the original comment / DM / question that this responds to
                category TEXT,                   -- qualified_request / spam_or_skip / lead / etc.
                body TEXT NOT NULL,              -- the actual drafted content
                status TEXT NOT NULL,            -- pending / sent / rejected / failed
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                external_id TEXT,                -- Meta comment/message ID after send
                error TEXT,
                meta_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_drafts_agent ON drafts(agent_name, status, created_at DESC);

            -- Auto-send template library. Each row is one variant of a public_reply
            -- or DM. Active rows for a (category, niche, kind) are published as a
            -- JSON array to Langfuse so n8n can fetch + randomly pick at send time.
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,           -- qualified_request / pure_praise / etc.
                niche TEXT NOT NULL,              -- behavioral_psychology / technology / legal_tech
                kind TEXT NOT NULL,               -- public_reply | dm
                body TEXT NOT NULL,
                voice_note TEXT,
                status TEXT NOT NULL,             -- draft | active | inactive
                artifact_id INTEGER,              -- generation batch this came from
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_templates_active ON templates(category, niche, kind, status);

            -- MBT (Meet by Travel) creator outreach: local-only tracking of which
            -- discovered profiles the operator has manually contacted or skipped.
            -- Never written back to the source Google Sheet, never auto-sent.
            CREATE TABLE IF NOT EXISTS mbt_outreach_status (
                username TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',   -- pending | contacted | skipped
                updated_at TEXT NOT NULL
            );
            """
        )
    _restrict_perms()


def add_template(
    *,
    category: str,
    niche: str,
    kind: str,
    body: str,
    voice_note: str = "",
    artifact_id: int | None = None,
    status: str = "draft",
) -> int:
    with _lock, _conn() as c:
        now = _now_iso()
        cur = c.execute(
            """
            INSERT INTO templates (category, niche, kind, body, voice_note, status, artifact_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (category, niche, kind, body, voice_note, status, artifact_id, now, now),
        )
        return cur.lastrowid


def list_templates(
    category: str | None = None,
    niche: str | None = None,
    kind: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM templates WHERE 1=1"
    params: list = []
    for col, val in (("category", category), ("niche", niche), ("kind", kind), ("status", status)):
        if val is not None:
            sql += f" AND {col} = ?"
            params.append(val)
    sql += " ORDER BY category, niche, kind, status DESC, id DESC"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def get_template(template_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
        return dict(row) if row else None


def update_template_status(template_id: int, status: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE templates SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now_iso(), template_id),
        )


def delete_template(template_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM templates WHERE id = ?", (template_id,))


def active_template_bodies(category: str, niche: str, kind: str) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT body FROM templates WHERE category = ? AND niche = ? AND kind = ? AND status = 'active' ORDER BY id",
            (category, niche, kind),
        ).fetchall()
        return [r["body"] for r in rows]


def template_counts() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM templates GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def add_draft(
    agent_name: str,
    kind: str,
    body: str,
    *,
    artifact_id: int | None = None,
    target_handle: str = "",
    source_text: str = "",
    category: str = "",
    meta: dict[str, Any] | None = None,
) -> int:
    with _lock, _conn() as c:
        now = _now_iso()
        cur = c.execute(
            """
            INSERT INTO drafts (
                artifact_id, agent_name, kind, target_handle, source_text,
                category, body, status, created_at, updated_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                artifact_id, agent_name, kind, target_handle, source_text,
                category, body, now, now, json.dumps(meta or {}),
            ),
        )
        return cur.lastrowid


def list_drafts(
    status: str | None = "pending",
    agent_name: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM drafts WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if agent_name:
        sql += " AND agent_name = ?"
        params.append(agent_name)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_draft(draft_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None


def update_draft_status(
    draft_id: int,
    status: str,
    external_id: str | None = None,
    error: str | None = None,
) -> None:
    with _lock, _conn() as c:
        now = _now_iso()
        c.execute(
            """
            UPDATE drafts
               SET status = ?, updated_at = ?, sent_at = ?, external_id = COALESCE(?, external_id), error = ?
             WHERE id = ?
            """,
            (status, now, now if status == "sent" else None, external_id, error, draft_id),
        )


def draft_counts_by_status() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM drafts GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def set_mbt_status(username: str, status: str) -> None:
    with _lock, _conn() as c:
        now = _now_iso()
        c.execute(
            """
            INSERT INTO mbt_outreach_status (username, status, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (username.lower().strip(), status, now),
        )


def get_mbt_statuses() -> dict[str, str]:
    with _conn() as c:
        rows = c.execute("SELECT username, status FROM mbt_outreach_status").fetchall()
        return {r["username"]: r["status"] for r in rows}


def begin_run(agent_name: str, task: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO runs (agent_name, task, status, started_at) VALUES (?, ?, ?, ?)",
            (agent_name, task, "running", _now_iso()),
        )
        return cur.lastrowid


def finish_run(
    run_id: int,
    status: str,
    artifact_id: int | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    if not run_id:
        return
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT started_at FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return
        started = datetime.fromisoformat(row["started_at"])
        now = datetime.now(started.tzinfo or timezone.utc)
        duration_s = (now - started).total_seconds()
        c.execute(
            """
            UPDATE runs
               SET status = ?,
                   ended_at = ?,
                   duration_s = ?,
                   artifact_id = ?,
                   error = ?,
                   meta_json = ?
             WHERE id = ?
            """,
            (
                status,
                _now_iso(),
                duration_s,
                artifact_id,
                error,
                json.dumps(meta or {}),
                run_id,
            ),
        )


def recent_runs(agent_name: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
    with _conn() as c:
        if agent_name:
            rows = c.execute(
                "SELECT * FROM runs WHERE agent_name = ? ORDER BY id DESC LIMIT ?",
                (agent_name, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def set_agent_status(
    name: str,
    status: str,
    current_task: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    with _lock, _conn() as c:
        now = _now_iso()
        existing = c.execute(
            "SELECT started_at FROM agents WHERE name = ?", (name,)
        ).fetchone()
        if status == "running":
            started_at = (
                existing["started_at"] if existing and existing["started_at"] else now
            )
            if not existing or existing["started_at"] is None:
                started_at = now
        else:
            started_at = None
        c.execute(
            """
            INSERT INTO agents (name, status, current_task, started_at, updated_at, meta_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                status = excluded.status,
                current_task = excluded.current_task,
                started_at = excluded.started_at,
                updated_at = excluded.updated_at,
                meta_json = excluded.meta_json
            """,
            (name, status, current_task, started_at, now, json.dumps(meta or {})),
        )


def log_event(
    agent_name: str,
    message: str,
    level: str = "info",
    data: dict[str, Any] | None = None,
) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO events (agent_name, level, message, data_json, ts) VALUES (?, ?, ?, ?, ?)",
            (agent_name, level, message, json.dumps(data or {}), _now_iso()),
        )


def record_artifact(
    agent_name: str,
    kind: str,
    title: str,
    path: str,
    meta: dict[str, Any] | None = None,
) -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO artifacts (agent_name, kind, title, path, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_name, kind, title, path, json.dumps(meta or {}), _now_iso()),
        )
        return cur.lastrowid


def list_agents() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM agents ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def recent_events(limit: int = 50, since_id: int = 0) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?",
            (since_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_artifacts(limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM artifacts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def wait_for_new_events(last_id: int, timeout_s: float = 25.0) -> list[dict[str, Any]]:
    """Poll-based wait; cheap enough at light cadence. SSE handler calls this."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT 100",
                (last_id,),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        time.sleep(0.5)
    return []
