"""MAPC Report Delivery agent.

Sends the MAPC Exam Prep PDF download link to one or more email addresses
via Resend. Generates a single-use HMAC-signed download token per recipient
so each link works exactly once.

Realm: Engagement (Devi) — subscriber fulfilment.
Astra: चन्द्र (Chandra) — the moon that illuminates study paths.

CLI:
  python -m agents.mapc_delivery --email student@example.com
  python -m agents.mapc_delivery --list ./emails.txt
  python -m agents.mapc_delivery --resend-to student@example.com  (re-issue fresh link)

The live website trigger goes directly through the n8n → Resend path (always-on).
This agent is for:
  - Manual / bulk sends
  - Re-issuing expired links
  - Dashboard visibility & delivery log
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


SITE_BASE = os.getenv("SITE_BASE_URL", "https://manikumarjami.com")
ASSESS_SECRET = os.getenv("ASSESS_SECRET", "")
TOKEN_TTL_HOURS = 48


def _sign_download_token(email: str, ts: int) -> str:
    """HMAC-SHA256 token: email|timestamp signed with ASSESS_SECRET."""
    payload = f"{email}|{ts}|mapc"
    return hmac.new(
        ASSESS_SECRET.encode() or b"dev-secret",
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def build_download_url(email: str) -> str:
    ts = int(time.time())
    sig = _sign_download_token(email, ts)
    return f"{SITE_BASE}/api/mapc-download?email={email}&ts={ts}&sig={sig}"


class MacpDeliveryAgent(BaseAgent):
    name = "mapc_delivery"
    description = (
        "Delivers the MAPC Exam Prep study guide to subscribers via Resend. "
        "Issues a unique single-use download link per recipient. "
        "Use for manual sends, bulk delivery, and re-issue of expired links."
    )
    workflow_steps = [
        WorkflowStep(
            key="validate",
            label="Validate inputs",
            description="Check email list, verify RESEND_API_KEY and ASSESS_SECRET are set.",
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

    def _run(self, task: str, **kwargs) -> str:  # noqa: ARG002
        emails: list[str] = kwargs.get("emails", [])
        if not emails:
            raise ValueError("No email addresses provided. Pass emails=[...] or use CLI.")

        self.set_status("running", current_task=f"Delivering to {len(emails)} recipient(s)")

        # ── Step 1: Validate ──────────────────────────────────────────────
        self.step("validate")
        if not os.getenv("RESEND_API_KEY"):
            self.log("RESEND_API_KEY not set in .env", level="error")
            raise RuntimeError("RESEND_API_KEY not set")
        if not ASSESS_SECRET:
            self.log("ASSESS_SECRET not set — tokens will use dev fallback", level="warning")

        from_addr = os.getenv(
            "RESEND_FROM_EMAIL",
            "Mani Kumar Jami <hello@manikumarjami.com>"
        )

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
                subject="Your MAPC Exam Prep Guide is ready ↓",
                html=html,
                text=text,
                from_addr=from_addr,
            )
            if ok:
                d["status"] = "sent"
                sent += 1
                self.log(f"✓ Sent to {d['email']}")
            else:
                d["status"] = f"failed: {err}"
                failed += 1
                self.log(f"✗ Failed for {d['email']}: {err}", level="error")

        # ── Step 4: Report ────────────────────────────────────────────────
        self.step("report")
        lines = [
            f"# MAPC Delivery Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"\n**Sent:** {sent}  **Failed:** {failed}  **Total:** {len(deliveries)}\n",
            "| Email | Status |",
            "|-------|--------|",
        ]
        for d in deliveries:
            lines.append(f"| {d['email']} | {d['status']} |")

        report = "\n".join(lines)

        out_dir = Path(os.getenv("OUTPUTS_DIR", "./outputs")) / "mapc_deliveries"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"{ts}-delivery.md"
        out_path.write_text(report)

        self.record_artifact("delivery_report", f"MAPC delivery {ts}", str(out_path))
        self.set_status("idle")

        summary = f"Delivered {sent}/{len(deliveries)} emails."
        self.log(summary)
        return report
