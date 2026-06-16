"""Delivery queue retry processor.

Reads a pending queue stored as JSON in a GitHub Gist (written by your server
when the email provider is rate-limited), retries each entry via your delivery
gate endpoint, and clears successfully sent entries from the queue.

Design: this module delegates to your server's gate endpoint so ASSESS_SECRET
stays server-side. Indra only needs GITHUB_TOKEN + DELIVERY_QUEUE_GIST.

Configure via .env:
  GITHUB_TOKEN           — GitHub PAT with gist:write scope
  DELIVERY_QUEUE_GIST    — Gist ID that stores the pending queue
  DELIVERY_QUEUE_FILE    — filename in the Gist (default: delivery_pending.json)
  DELIVERY_GATE_URL      — your server's delivery gate endpoint
  DELIVERY_SITE_ORIGIN   — Origin header sent to the gate

Run manually:  python -m agents.digital_delivery.retry
Run via Indra: POST /api/delivery-retry
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import httpx


GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN", "")
QUEUE_GIST          = os.getenv("DELIVERY_QUEUE_GIST", "")
QUEUE_FILE          = os.getenv("DELIVERY_QUEUE_FILE", "delivery_pending.json")
GATE_URL            = os.getenv("DELIVERY_GATE_URL", "")
SITE_ORIGIN         = os.getenv("DELIVERY_SITE_ORIGIN", os.getenv("SITE_BASE_URL", ""))


def _fetch_pending() -> tuple[list[dict], None]:
    """Fetch pending queue from GitHub Gist. Returns (entries, None)."""
    if not GITHUB_TOKEN or not QUEUE_GIST:
        return [], None
    hdrs = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    r = httpx.get(f"https://api.github.com/gists/{QUEUE_GIST}", headers=hdrs, timeout=10)
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    raw = r.json().get("files", {}).get(QUEUE_FILE, {}).get("content", "[]")
    return json.loads(raw), None


def _write_queue(entries: list[dict], _sha: None, message: str) -> None:
    """Overwrite the queue Gist with updated entries."""
    if not GITHUB_TOKEN or not QUEUE_GIST:
        return
    hdrs = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github.v3+json",
    }
    httpx.patch(
        f"https://api.github.com/gists/{QUEUE_GIST}",
        headers=hdrs,
        json={"description": message, "files": {QUEUE_FILE: {"content": json.dumps(entries, indent=2)}}},
        timeout=10,
    )


def _resend_via_gate(email: str, extra: dict) -> bool:
    """Call the delivery gate endpoint. Returns True on success."""
    if not GATE_URL:
        print("  [retry] DELIVERY_GATE_URL not set — skipping")
        return False
    try:
        r = httpx.post(
            GATE_URL,
            json={"email": email, **extra},
            headers={"Content-Type": "application/json", "Origin": SITE_ORIGIN},
            timeout=20,
        )
        data = r.json() if r.status_code < 500 else {}
        return r.status_code == 200 and data.get("ok") and not data.get("queued")
    except Exception as exc:
        print(f"  [retry] gate call failed: {exc}")
        return False


def process_queue(dry_run: bool = False) -> dict:
    """Fetch pending queue, retry via gate, update Gist. Returns summary dict."""
    pending, sha = _fetch_pending()
    if not pending:
        return {
            "pending": 0, "sent": 0, "failed": 0, "still_queued": 0,
            "message": "Queue empty — nothing to retry",
        }

    print(f"[delivery-retry] Processing {len(pending)} queued emails")
    sent, still_queued = [], []

    for entry in pending:
        email = entry.get("email", "")
        extra = {k: v for k, v in entry.items() if k not in ("email", "queued_at")}

        if dry_run:
            print(f"  [dry-run] would retry {email}")
            sent.append(email)
            continue

        ok = _resend_via_gate(email, extra)
        if ok:
            sent.append(email)
            print(f"  ✓ {email} → sent")
        else:
            still_queued.append(entry)
            print(f"  ✗ {email} → still queued")

        time.sleep(0.5)

    if not dry_run:
        msg = (
            f"Retry {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — "
            f"sent {len(sent)}, still pending {len(still_queued)}"
        )
        _write_queue(still_queued, sha, msg)

    return {
        "pending": len(pending),
        "sent": len(sent),
        "failed": 0,
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
            print(f"  {e.get('email')} — queued at {e.get('queued_at', '?')}")
    else:
        result = process_queue(dry_run=dry)
        print(f"\nResult: {result}")
