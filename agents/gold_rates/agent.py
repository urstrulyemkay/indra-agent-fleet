"""Gold & Silver Rates agent.

Reads the daily gold/silver rates snapshot written by the Vercel cron
(`api/gold-rates-cron.js` on manikumarjami.com) from the public price-data
Gist, checks subscriber count on the "Gold & Silver Alerts" Brevo list, and
writes a markdown report artifact. Use to verify the daily cron ran and
rates are fresh.

Realm: Engagement (Devi) — subscriber-facing rates + alerts.
Astra: कलश (Kalash) — the wealth/abundance vessel.

CLI:
  python -m agents.gold_rates
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from core.base_agent import BaseAgent, WorkflowStep


GH_API = "https://api.github.com/gists"
BREVO_API = "https://api.brevo.com/v3"


class GoldRatesAgent(BaseAgent):
    name = "gold_rates"
    description = (
        "Reads today's gold & silver rates snapshot from the public price-data Gist, "
        "checks the Gold & Silver Alerts subscriber count via Brevo, and writes a "
        "markdown report. Use to verify the daily Vercel cron ran and rates are fresh."
    )
    workflow_steps = [
        WorkflowStep(
            key="fetch_snapshot",
            label="Fetch rates snapshot",
            description="Read gold_rates_latest.json from the public price-data Gist.",
        ),
        WorkflowStep(
            key="check_subscribers",
            label="Check subscribers",
            description="Query the Gold & Silver Alerts Brevo list for the current subscriber count.",
        ),
        WorkflowStep(
            key="report",
            label="Write report",
            description="Write a markdown summary: today's rates, change %, subscriber count.",
        ),
    ]

    def _run(self, task: str, **kwargs) -> str:  # noqa: ARG002
        self.set_status("running", current_task="Checking gold/silver rates + subscribers")

        # ── Step 1: Fetch snapshot ───────────────────────────────────────
        self.step("fetch_snapshot")
        gist_id = os.getenv("GOLD_RATES_GIST", "")
        snapshot: dict = {}
        if gist_id:
            try:
                headers = {"Accept": "application/vnd.github.v3+json"}
                token = os.getenv("GITHUB_TOKEN", "")
                if token:
                    headers["Authorization"] = f"token {token}"
                with httpx.Client(timeout=10.0) as c:
                    r = c.get(f"{GH_API}/{gist_id}", headers=headers)
                if r.status_code == 200:
                    raw = r.json().get("files", {}).get("gold_rates_latest.json", {}).get("content", "{}")
                    snapshot = json.loads(raw)
            except Exception as exc:
                self.log(f"Snapshot fetch failed: {exc}", level="error")
        else:
            self.log("GOLD_RATES_GIST not set", level="warning")

        if snapshot:
            self.log(f"Snapshot date: {snapshot.get('date')}")
        else:
            self.log("No rates snapshot available", level="warning")

        # ── Step 2: Check subscribers ─────────────────────────────────────
        self.step("check_subscribers")
        subscriber_count: int | None = None
        brevo_key = os.getenv("BREVO_API_KEY", "")
        list_id = os.getenv("BREVO_GOLD_LIST_ID", "")
        if brevo_key and list_id:
            try:
                with httpx.Client(timeout=10.0) as c:
                    r = c.get(f"{BREVO_API}/contacts/lists/{list_id}", headers={"api-key": brevo_key})
                if r.status_code == 200:
                    subscriber_count = r.json().get("totalSubscribers")
            except Exception as exc:
                self.log(f"Brevo list fetch failed: {exc}", level="error")
        else:
            self.log("BREVO_GOLD_LIST_ID not set — skipping subscriber check", level="warning")

        if subscriber_count is not None:
            self.log(f"Gold & Silver Alerts subscribers: {subscriber_count}")

        # ── Step 3: Report ─────────────────────────────────────────────────
        self.step("report")
        chg = snapshot.get("change_pct", {})
        lines = [f"# Gold & Silver Rates Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]

        if snapshot:
            nat = snapshot.get("national", {})
            intl = snapshot.get("international", {})
            lines += [
                f"**Date:** {snapshot.get('date')}  **Updated:** {snapshot.get('updated_at')}",
                "",
                "## National (India)",
                f"- 24K gold: ₹{nat.get('gold', {}).get('24k_per_10g')} / 10g ({chg.get('gold', 0):+.2f}% vs yesterday)",
                f"- 22K gold: ₹{nat.get('gold', {}).get('22k_per_10g')} / 10g",
                f"- 18K gold: ₹{nat.get('gold', {}).get('18k_per_10g')} / 10g",
                f"- Silver: ₹{nat.get('silver', {}).get('per_kg')} / kg ({chg.get('silver', 0):+.2f}% vs yesterday)",
                "",
                "## International",
                f"- XAU: ${intl.get('xau_usd_per_oz')} / oz",
                f"- XAG: ${intl.get('xag_usd_per_oz')} / oz",
                f"- USD/INR: {intl.get('usd_inr')}",
                "",
                f"## Cities tracked: {len(snapshot.get('cities', {}))}",
            ]
        else:
            lines.append("No rates snapshot available — check `GOLD_RATES_GIST` / cron status.")

        lines += ["", "## Subscribers"]
        if subscriber_count is not None:
            lines.append(f"Gold & Silver Alerts list: **{subscriber_count}** subscribers")
        else:
            lines.append("Subscriber count unavailable — check `BREVO_API_KEY` / `BREVO_GOLD_LIST_ID`")

        report = "\n".join(lines)

        out_dir = Path(os.getenv("OUTPUTS_DIR", "./outputs")) / "gold_rates"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"{ts}-report.md"
        out_path.write_text(report)

        self.record_artifact("rates_report", f"Gold & silver rates {ts}", str(out_path))
        self.set_status("idle")

        gold_chg = f"{chg.get('gold', 0):+.2f}%" if snapshot else "n/a"
        silver_chg = f"{chg.get('silver', 0):+.2f}%" if snapshot else "n/a"
        sub_str = subscriber_count if subscriber_count is not None else "n/a"
        summary = f"Gold {gold_chg}, silver {silver_chg}, subscribers {sub_str}"
        self.log(summary)
        return report
