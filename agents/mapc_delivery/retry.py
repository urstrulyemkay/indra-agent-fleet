"""MAPC email retry processor.

Reads mapc_pending.json from GitHub (written by Vercel when Resend is rate-limited),
calls the live /api/mapc-gate endpoint for each queued email, then clears sent entries.

Design: Indra does NOT need ASSESS_SECRET — it delegates to the Vercel function
which has the correct signing key. This keeps secrets in one place.

Run manually:  python -m agents.mapc_delivery.retry
Run via Indra: POST /api/mapc-retry
Resend free tier resets at midnight UTC → run at 00:05 UTC.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone

import httpx

GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
MAPC_QUEUE_GIST = os.getenv("MAPC_QUEUE_GIST", "")

# The live Vercel endpoint — handles ASSESS_SECRET signing internally
MAPC_GATE_URL = "https://manikumarjami.com/api/mapc-gate"

# Test email bypass — rate limit is skipped for this address
TEST_EMAIL = "manikumarjami1@gmail.com"


def _fetch_pending() -> tuple[list[dict], None]:
    """Fetch pending queue from private GitHub Gist. Returns (entries, None)."""
    if not GITHUB_TOKEN or not MAPC_QUEUE_GIST:
        return [], None
    hdrs = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    r = httpx.get(f"https://api.github.com/gists/{MAPC_QUEUE_GIST}", headers=hdrs, timeout=10)
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    raw = r.json().get("files", {}).get("mapc_pending.json", {}).get("content", "[]")
    return json.loads(raw), None


def _write_queue(entries: list[dict], _sha: None, message: str) -> None:
    """Write entries back to private GitHub Gist (overwrites content — no git history)."""
    if not GITHUB_TOKEN or not MAPC_QUEUE_GIST:
        return
    hdrs = {"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json"}
    httpx.patch(
        f"https://api.github.com/gists/{MAPC_QUEUE_GIST}",
        headers=hdrs,
        json={"description": message,
              "files": {"mapc_pending.json": {"content": json.dumps(entries, indent=2)}}},
        timeout=10,
    )


def _resend_via_gate(email: str, specialisation: str) -> bool:
    """Call the live mapc-gate Vercel endpoint. Returns True on success."""
    try:
        r = httpx.post(
            MAPC_GATE_URL,
            json={"email": email, "specialisation": specialisation},
            headers={"Content-Type": "application/json",
                     "Origin": "https://manikumarjami.com"},
            timeout=20,
        )
        data = r.json() if r.status_code < 500 else {}
        # ok:true means sent, queued:true means queued again (still rate limited)
        return r.status_code == 200 and data.get("ok") and not data.get("queued")
    except Exception as exc:
        print(f"  [retry] gate call failed: {exc}")
        return False


def process_queue(dry_run: bool = False) -> dict:
    """Fetch pending queue, retry via Vercel gate, update queue. Returns summary."""
    pending, sha = _fetch_pending()
    if not pending:
        return {"pending": 0, "sent": 0, "failed": 0, "still_queued": 0,
                "message": "Queue empty — nothing to retry"}

    print(f"[mapc-retry] Processing {len(pending)} queued emails")
    sent, failed, still_queued = [], [], []

    for entry in pending:
        email = entry["email"]
        spec  = entry.get("specialisation", "counselling")

        if dry_run:
            print(f"  [dry-run] would retry {email} ({spec})")
            sent.append(email)
            continue

        ok = _resend_via_gate(email, spec)
        if ok:
            sent.append(email)
            print(f"  ✓ {email} → sent")
        else:
            # Still rate-limited or failed — keep in queue for next cycle
            still_queued.append(entry)
            print(f"  ✗ {email} → still queued (rate limit may persist)")

        time.sleep(0.5)  # 500ms between sends

    # Update queue: remove sent, keep still_queued
    if not dry_run:
        msg = (f"Retry {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — "
               f"sent {len(sent)}, still pending {len(still_queued)}")
        _write_queue(still_queued, sha, msg)

    return {
        "pending": len(pending),
        "sent": len(sent),
        "failed": len(failed),
        "still_queued": len(still_queued),
        "sent_emails": sent,
    }


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if "--status" in sys.argv:
        pending, _ = _fetch_pending()
        print(f"Queued emails: {len(pending)}")
        for e in pending:
            print(f"  {e['email']} ({e.get('specialisation')}) — queued at {e.get('queued_at','?')}")
    else:
        result = process_queue(dry_run=dry)
        print(f"\nResult: {result}")
