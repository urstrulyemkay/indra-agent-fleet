"""SQLite-backed email signup store with double-opt-in tokens.

Same DB file as status_bus (./data/status.db by default). One table:
`email_signups(id, email, token, status, source, created_at, confirmed_at)`.

Status values: 'pending' (DOI email sent, not yet confirmed) | 'confirmed' | 'failed_send'.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


DB_PATH = Path(os.getenv("FLEET_DB_PATH", "./data/status.db"))
_lock = threading.Lock()

# Conservative email regex — good enough to catch obvious junk; we let Resend
# do the real validation on send.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


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
            CREATE TABLE IF NOT EXISTS email_signups (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT NOT NULL UNIQUE COLLATE NOCASE,
                token        TEXT NOT NULL UNIQUE,
                status       TEXT NOT NULL,
                source       TEXT,
                created_at   TEXT NOT NULL,
                confirmed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_signups_status ON email_signups(status);
            CREATE INDEX IF NOT EXISTS idx_signups_token  ON email_signups(token);
            """
        )


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip())) and len(email) <= 254


def create_or_get_pending(email: str, source: str = "") -> tuple[str, bool, str]:
    """Insert a pending signup (or return the existing one).

    Returns (token, is_new, status):
      - token:   the DOI confirmation token to embed in the email link
      - is_new:  True if this email had no row before this call
      - status:  current row status ('pending' | 'confirmed')

    Already-confirmed emails return their existing row with is_new=False so
    callers can short-circuit without re-sending the DOI mail.
    """
    email = (email or "").strip()
    if not is_valid_email(email):
        raise ValueError("invalid email")
    init_table()
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT token, status FROM email_signups WHERE email = ?",
            (email,),
        ).fetchone()
        if row:
            return row["token"], False, row["status"]
        token = secrets.token_urlsafe(24)
        c.execute(
            """INSERT INTO email_signups (email, token, status, source, created_at)
               VALUES (?, ?, 'pending', ?, ?)""",
            (email, token, source or None, _now_iso()),
        )
        return token, True, "pending"


def confirm(token: str) -> Optional[dict]:
    """Mark a signup as confirmed by token. Returns the row or None if no match.

    Idempotent: confirming an already-confirmed token just returns the row.
    """
    if not token:
        return None
    init_table()
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM email_signups WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["status"] != "confirmed":
            c.execute(
                "UPDATE email_signups SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
                (_now_iso(), row["id"]),
            )
            row = c.execute(
                "SELECT * FROM email_signups WHERE id = ?",
                (row["id"],),
            ).fetchone()
        return dict(row)


def get_by_token(token: str) -> Optional[dict]:
    """Return a signup row without changing its subscription state."""
    if not token:
        return None
    init_table()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM email_signups WHERE token = ?",
            (token,),
        ).fetchone()
        return dict(row) if row else None


def confirm_via_labs(email: str, source: str) -> str:
    """Newsletter opt-in path from /labs/ assessment completion.

    Form checkbox = explicit consent, so we skip the DOI step and write the
    row directly as confirmed. Returns the row's token (for use in the
    unsubscribe link). Idempotent:
      - email missing  → insert as confirmed, return new token
      - email pending  → flip to confirmed (their form consent overrides), reuse token
      - email confirmed → no change, reuse token
      - email unsubscribed → DO NOT silently re-subscribe; return token but keep status.
        Caller should detect and surface, but signup is effectively a no-op.
    """
    email = (email or "").strip()
    if not is_valid_email(email):
        raise ValueError("invalid email")
    init_table()
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT id, token, status FROM email_signups WHERE email = ?",
            (email,),
        ).fetchone()
        now = _now_iso()
        if row:
            if row["status"] == "unsubscribed":
                return row["token"]
            if row["status"] != "confirmed":
                c.execute(
                    """UPDATE email_signups
                       SET status = 'confirmed', confirmed_at = COALESCE(confirmed_at, ?),
                           source = COALESCE(source, ?)
                       WHERE id = ?""",
                    (now, source[:80] if source else None, row["id"]),
                )
            return row["token"]
        token = secrets.token_urlsafe(24)
        c.execute(
            """INSERT INTO email_signups (email, token, status, source, created_at, confirmed_at)
               VALUES (?, ?, 'confirmed', ?, ?, ?)""",
            (email, token, (source or "labs")[:80], now, now),
        )
        return token


def unsubscribe_by_token(token: str) -> Optional[dict]:
    """Flip status to 'unsubscribed'. Idempotent: re-clicking the link is fine.
    Returns the updated row or None if the token doesn't exist."""
    if not token:
        return None
    init_table()
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM email_signups WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["status"] != "unsubscribed":
            c.execute(
                "UPDATE email_signups SET status = 'unsubscribed' WHERE id = ?",
                (row["id"],),
            )
            row = c.execute(
                "SELECT * FROM email_signups WHERE id = ?",
                (row["id"],),
            ).fetchone()
        return dict(row)


def mark_send_failed(email: str, error: str) -> None:
    """Record that the DOI email send failed so we can show it in admin."""
    init_table()
    with _conn() as c:
        c.execute(
            """UPDATE email_signups
               SET status = 'failed_send', source = COALESCE(source, '') || ' | err:' || substr(?, 1, 180)
               WHERE email = ? AND status = 'pending'""",
            (error or "(no error)", email),
        )


def list_recent(limit: int = 100) -> list[dict]:
    init_table()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM email_signups ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def counts() -> dict:
    init_table()
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM email_signups GROUP BY status"
        ).fetchall()
        out = {"pending": 0, "confirmed": 0, "failed_send": 0, "total": 0}
        for r in rows:
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
        return out
