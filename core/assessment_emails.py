"""SQLite-backed log of assessment result emails sent via the labs flow.

Distinct from email_signups: this is a per-send audit trail with idempotency
keyed on (email, test_id, run_id). The labs site generates a run_id when the
visitor completes a test; re-submitting the same run is a no-op.

Same DB file as status_bus + email_signups (./data/status.db).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


DB_PATH = Path(os.getenv("FLEET_DB_PATH", "./data/status.db"))
_lock = threading.Lock()


# Tests the labs site ships. Keep in sync with /labs/data/<id>.json filenames.
KNOWN_TESTS = {
    "big-five", "attachment-style", "career-interest",
    "cognitive", "student-stress", "wellbeing",
}

# Wellbeing screener — PHQ-9 + GAD-7 — must never gate the report on email.
# This module doesn't enforce; the labs UI does. But we record the flag so
# downstream queries (admin views) can distinguish ethical-carveout sends.
WELLBEING_TESTS = {"wellbeing"}


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


def init_table() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS assessment_emails (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email           TEXT NOT NULL COLLATE NOCASE,
                test_id         TEXT NOT NULL,
                run_id          TEXT NOT NULL,
                name            TEXT,
                results_url     TEXT,
                summary_text    TEXT,
                newsletter_opt  INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL,
                error           TEXT,
                created_at      TEXT NOT NULL,
                sent_at         TEXT,
                UNIQUE(email, test_id, run_id) ON CONFLICT IGNORE
            );
            CREATE INDEX IF NOT EXISTS idx_aem_email ON assessment_emails(email);
            CREATE INDEX IF NOT EXISTS idx_aem_test  ON assessment_emails(test_id);
            CREATE INDEX IF NOT EXISTS idx_aem_status ON assessment_emails(status);
            """
        )


def find_existing(email: str, test_id: str, run_id: str) -> Optional[dict]:
    """Return the row for this (email, test, run) triple if it exists."""
    init_table()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM assessment_emails WHERE email = ? AND test_id = ? AND run_id = ?",
            (email.lower().strip(), test_id, run_id),
        ).fetchone()
        return dict(row) if row else None


def record_pending(
    email: str,
    test_id: str,
    run_id: str,
    name: Optional[str],
    results_url: Optional[str],
    summary_text: Optional[str],
    newsletter_opt: bool,
) -> int:
    """Insert a pending row before attempting the Resend send. Returns the row id.
    Caller is responsible for calling mark_sent or mark_failed afterwards."""
    init_table()
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO assessment_emails
               (email, test_id, run_id, name, results_url, summary_text,
                newsletter_opt, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (email.lower().strip(), test_id, run_id, (name or None),
             results_url, summary_text, 1 if newsletter_opt else 0, _now_iso()),
        )
        return cur.lastrowid


def mark_sent(row_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE assessment_emails SET status = 'sent', sent_at = ? WHERE id = ?",
            (_now_iso(), row_id),
        )


def mark_failed(row_id: int, error: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE assessment_emails SET status = 'failed', error = ? WHERE id = ?",
            ((error or "")[:500], row_id),
        )


def counts_by_test() -> list[dict]:
    init_table()
    with _conn() as c:
        rows = c.execute(
            """SELECT test_id, status, COUNT(*) AS n
               FROM assessment_emails GROUP BY test_id, status"""
        ).fetchall()
        return [dict(r) for r in rows]


def list_recent(limit: int = 100) -> list[dict]:
    init_table()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM assessment_emails ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
