"""Thin Resend send wrapper. One function: send(to, subject, html, text=None).

Sandbox note: with RESEND_API_KEY only (no verified domain), Resend rejects
recipients other than your Resend account email. The wrapper surfaces that as
a friendly error so the caller can act on it instead of guessing.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx


DEFAULT_FROM = os.getenv("RESEND_FROM", "onboarding@resend.dev")
RESEND_API = "https://api.resend.com/emails"


def send(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    from_addr: Optional[str] = None,
) -> tuple[bool, str | None]:
    """Send a single email via Resend. Returns (ok, error_message)."""
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        return False, "RESEND_API_KEY not set in .env"

    payload: dict = {
        "from": from_addr or DEFAULT_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                RESEND_API,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code in (200, 202):
            return True, None
        # Resend's 403 message for sandbox-locked recipients is helpful — surface it
        try:
            body = r.json()
            msg = body.get("message") or body.get("error") or r.text
        except Exception:
            msg = r.text
        return False, f"Resend {r.status_code}: {msg}"
    except Exception as e:
        return False, f"Resend send exception: {e}"
