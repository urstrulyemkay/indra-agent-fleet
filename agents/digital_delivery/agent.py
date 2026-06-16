"""Digital Delivery agent.

Sends a signed single-use download link for any digital product (PDF, ebook,
template, report) to one or more email addresses via Resend.

Pattern:
  subscriber confirms → this agent issues an HMAC-signed token → sends an
  email with a link valid for TOKEN_TTL_HOURS → the receiving server verifies
  the token before serving the file.

Configure via .env:
  PRODUCT_NAME          — display name in emails (e.g. "Python Crash Course PDF")
  PRODUCT_SLUG          — short ID used in token signing (e.g. "python-course")
  SITE_BASE_URL         — base of your site (e.g. https://yoursite.com)
  DELIVERY_GATE_PATH    — path on your server that validates tokens (e.g. /api/download)
  ASSESS_SECRET         — HMAC signing key (keep secret, rotate regularly)
  RESEND_API_KEY        — Resend transactional email key
  RESEND_FROM_EMAIL     — verified sender address (e.g. "You <hello@yoursite.com>")
  DELIVERY_SUBJECT      — email subject line
  TOKEN_TTL_HOURS       — hours before link expires (default: 48)

CLI:
  python -m agents.digital_delivery --email student@example.com
  python -m agents.digital_delivery --list ./emails.txt
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from core import resend_client
from core import status_bus
from core.base_agent import BaseAgent, WorkflowStep

from .prompts import build_delivery_email_html, build_delivery_email_text


SITE_BASE        = os.getenv("SITE_BASE_URL", "https://yoursite.com")
GATE_PATH        = os.getenv("DELIVERY_GATE_PATH", "/api/download")
PRODUCT_NAME     = os.getenv("PRODUCT_NAME", "Your Digital Product")
PRODUCT_SLUG     = os.getenv("PRODUCT_SLUG", "product")
ASSESS_SECRET    = os.getenv("ASSESS_SECRET", "")
TOKEN_TTL_HOURS  = int(os.getenv("TOKEN_TTL_HOURS", "48"))


def _sign_download_token(email: str, ts: int) -> str:
    """HMAC-SHA256 token: email|timestamp|slug signed with ASSESS_SECRET."""
    payload = f"{email}|{ts}|{PRODUCT_SLUG}"
    return hmac.new(
        ASSESS_SECRET.encode() or b"dev-secret",
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def build_download_url(email: str) -> str:
    ts  = int(time.time())
    sig = _sign_download_token(email, ts)
    return f"{SITE_BASE}{GATE_PATH}?email={email}&ts={ts}&sig={sig}"


class DigitalDeliveryAgent(BaseAgent):
    name = "digital_delivery"
    description = (
        "Delivers a signed single-use download link for any digital product to "
        "one or more subscribers via Resend. Handles manual sends, bulk delivery, "
        "and re-issue of expired links. Configure PRODUCT_NAME + SITE_BASE_URL in .env."
    )
    workflow_steps = [
        WorkflowStep(
            key="validate",
            label="Validate inputs",
            description="Check email list; verify RESEND_API_KEY and ASSESS_SECRET are set.",
        ),
        WorkflowStep(
            key="tokenise",
            label="Generate download tokens",
            description="Create HMAC-signed single-use download URLs per recipient.",
        ),
        WorkflowStep(
            key="send",
            label="Send emails",
            description="Dispatch delivery emails via Resend with personalised download links.",
        ),
        WorkflowStep(
            key="report",
            label="Delivery report",
            description="Log success/failure per recipient. Write markdown artifact.",
        ),
    ]

    def _run(self, task: str, **kwargs) -> str:
        emails: list[str] = kwargs.get("emails", [])
        if not emails:
            raise ValueError("No email addresses provided. Pass emails=[...] or use CLI.")

        self.set_status("running", current_task=f"Delivering {PRODUCT_NAME} to {len(emails)} recipient(s)")

        # ── Step 1: Validate ──────────────────────────────────────────────
        self.step("validate")
        if not os.getenv("RESEND_API_KEY"):
            self.log("RESEND_API_KEY not set in .env", level="error")
            raise RuntimeError("RESEND_API_KEY not set")
        if not ASSESS_SECRET:
            self.log("ASSESS_SECRET not set — tokens will use dev fallback (not safe in production)", level="warning")

        from_addr = os.getenv("RESEND_FROM_EMAIL", "")
        if not from_addr:
            self.log("RESEND_FROM_EMAIL not set in .env", level="error")
            raise RuntimeError("RESEND_FROM_EMAIL not set — add it to .env")

        subject = os.getenv("DELIVERY_SUBJECT", f"Your {PRODUCT_NAME} is ready ↓")

        # ── Step 2: Tokenise ──────────────────────────────────────────────
        self.step("tokenise")
        deliveries: list[dict] = []
        for email in emails:
            url = build_download_url(email.strip().lower())
            deliveries.append({"email": email, "url": url, "status": "pending"})
            self.log(f"Token generated for {email[:20]}…")

        # ── Step 3: Send ──────────────────────────────────────────────────
        self.step("send")
        sent, failed = 0, 0
        for d in deliveries:
            html = build_delivery_email_html(d["email"], d["url"])
            text = build_delivery_email_text(d["email"], d["url"])
            ok, err = resend_client.send(
                to=d["email"],
                subject=subject,
                html=html,
                text=text,
                from_addr=from_addr,
            )
            if ok:
                d["status"] = "sent"
                sent += 1
                self.log(f"Sent to {d['email']}")
            else:
                d["status"] = f"failed: {err}"
                failed += 1
                self.log(f"Failed for {d['email']}: {err}", level="error")

        # ── Step 4: Report ────────────────────────────────────────────────
        self.step("report")
        lines = [
            f"# {PRODUCT_NAME} — Delivery Report",
            f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"\n**Sent:** {sent}  **Failed:** {failed}  **Total:** {len(deliveries)}\n",
            "| Email | Status |",
            "|-------|--------|",
        ]
        for d in deliveries:
            lines.append(f"| {d['email']} | {d['status']} |")

        report = "\n".join(lines)

        out_dir = Path(os.getenv("OUTPUTS_DIR", "./outputs")) / "digital_delivery"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"{ts}-delivery.md"
        out_path.write_text(report)

        self.record_artifact("delivery_report", f"Delivery {ts} — {PRODUCT_NAME}", str(out_path))
        self.set_status("idle")

        self.log(f"Delivered {sent}/{len(deliveries)} emails.")
        return report
