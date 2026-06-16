"""Catalog of every external tool the Indra fleet talks to.

Used by the dashboard /integrations view so you always know what's wired,
what's planned, what costs money, and which agents depend on what.

Status semantics:
- live      : currently used by at least one live agent and verified working
- optional  : code supports it, no API key required, or graceful fallback
- planned   : roadmap agent depends on this; not yet wired
"""

from __future__ import annotations

import os
import time as _t
from dataclasses import dataclass
from typing import Literal

from core import langfuse_integration as lf


Status = Literal["live", "optional", "planned"]
Kind = Literal[
    "llm", "observability", "data_source", "scraper", "email",
    "social", "scheduler", "image_render", "automation",
]


@dataclass(frozen=True)
class Integration:
    name: str
    kind: Kind
    purpose: str                          # one-line
    status: Status
    cost: str                              # "free", "free tier", "paid", or specific
    env_vars: tuple[str, ...] = ()         # required env vars
    used_by: tuple[str, ...] = ()          # agent names that depend on this
    docs_url: str = ""
    notes: str = ""


INTEGRATIONS: tuple[Integration, ...] = (
    Integration(
        name="Anthropic API",
        kind="llm",
        purpose="Local Claude SDK auth — only needed if an agent runs locally and calls Claude directly.",
        status="optional",
        cost="paid (per token, if used)",
        env_vars=("ANTHROPIC_API_KEY", "CLAUDE_MODEL"),
        used_by=(),
        docs_url="https://docs.anthropic.com",
        notes="Not required for the current fleet. daily_ai_brief + thought_mechanic run as Claude Cloud Routines (auth via your claude.ai account). The IG auto-responder uses no Claude at all (template rotation in n8n). Add this key only if you wire a future agent that hits the SDK locally.",
    ),
    Integration(
        name="Claude Cloud Routines",
        kind="automation",
        purpose="Scheduled-job runtime that fires the brief-generating agents on a daily cadence.",
        status="live",
        cost="free (counted against Claude usage)",
        used_by=("daily_ai_brief", "thought_mechanic"),
        docs_url="https://claude.ai/routines",
        notes="Configured at claude.ai/routines — not via env vars. daily_ai_brief fires ~03:36 UTC; thought_mechanic conversion in progress (see docs/THOUGHT-MECHANIC-ROUTINE.md).",
    ),
    Integration(
        name="Langfuse Cloud",
        kind="observability",
        purpose="Prompt management for n8n template rotation + trace observability for Claude calls.",
        status="live",
        cost="free tier (50K observations/month)",
        env_vars=("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"),
        used_by=("comment_dm_responder", "daily_ai_brief", "thought_mechanic"),
        docs_url="https://cloud.langfuse.com",
        notes="Edit IG response templates in Langfuse UI → next n8n execution uses them. Code fallback keeps the fleet working if Langfuse is down.",
    ),
    Integration(
        name="n8n Cloud",
        kind="automation",
        purpose="Workflow runtime for the Instagram auto-comment + DM auto-responder.",
        status="live",
        cost="free tier",
        env_vars=("N8N_API_BASE_URL", "N8N_API_KEY", "INDRA_N8N_WORKFLOW_ID", "INDRA_N8N_SHARED_SECRET", "N8N_SEND_WEBHOOK_URL"),
        used_by=("comment_dm_responder",),
        docs_url=os.getenv("N8N_API_BASE_URL", "https://app.n8n.cloud").replace("/api/v1", ""),
        notes="Workflow runs entirely on n8n Cloud and is healthy regardless of these env vars. Vars are only needed for the Indra dashboard to fetch execution stats + relay approval webhooks. HMAC shared secret protects inbound webhooks from n8n.",
    ),
    Integration(
        name="Meta Graph API (Instagram)",
        kind="social",
        purpose="Reads IG comments/DMs (via n8n webhook subscription) and posts replies/DMs to the configured IG Business account.",
        status="live",
        cost="free (requires FB Page + IG Business account)",
        env_vars=("META_APP_SECRET", "META_PAGE_ACCESS_TOKEN", "IG_BUSINESS_ACCOUNT_ID", "META_VERIFY_TOKEN", "IG_USER_ID"),
        used_by=("comment_dm_responder",),
        docs_url="https://developers.facebook.com/docs/instagram-platform",
        notes="Tokens shared with the n8n workflow. Page access token must be long-lived. Never used for cold outreach — only for replies to inbound IG events on configured posts.",
    ),
    Integration(
        name="GitHub API",
        kind="data_source",
        purpose="Reads briefs from `urstrulyemkay/emkayjami` for the dashboard; the Cloud Routine agents write back to the same repo.",
        status="live",
        cost="free (5K req/hr authenticated)",
        env_vars=("GITHUB_TOKEN",),
        used_by=("daily_ai_brief", "thought_mechanic"),
        docs_url="https://docs.github.com/rest",
        notes="Dashboard reads `daily-brief/` and `thought-mechanic/` paths. PAT scopes needed: `repo` (read+write). Indra never pushes — that's the Cloud Routines' job.",
    ),
    Integration(
        name="Resend",
        kind="email",
        purpose="Transactional email — fleet notifications. Never cold email.",
        status="live",
        cost="free tier (3K emails/month, 100/day)",
        env_vars=("RESEND_API_KEY",),
        used_by=(),
        docs_url="https://resend.com/docs",
        notes="Plumbing wired 2026-05-17. Live ping verifies the key on each /integrations page load (5-min cache). Cold-email use violates AUP — never wire to outbound.",
    ),
    Integration(
        name="Apify",
        kind="scraper",
        purpose="LinkedIn jobs scraping for the job_hunter agent — feeds raw postings to Claude for ranking + per-JD CV tailoring.",
        status="optional",
        cost="free tier ($5/month platform credit; curious_coder LinkedIn jobs actor is pay-per-result at $0.001/job ≈ 5000 jobs/month free)",
        env_vars=("APIFY_TOKEN", "APIFY_LINKEDIN_ACTOR_ID"),
        used_by=("job_hunter",),
        docs_url="https://apify.com/curious_coder/linkedin-jobs-scraper",
        notes=(
            "Sign up at apify.com → Settings → Integrations → API tokens. Free tier includes "
            "$5/month platform credits. Default actor `curious_coder~linkedin-jobs-scraper` "
            "is pay-per-result ($0.001/job) and does NOT charge a monthly rental — at 25 "
            "jobs/run that's $0.025/run, fits inside the free tier comfortably. AVOID "
            "`bebity~linkedin-jobs-scraper` (FLAT_PRICE_PER_MONTH $15/mo = violates the "
            "no-paid-services rule). Input shape: {urls: [LinkedIn search URL], count, "
            "scrapeCompany}."
        ),
    ),
    Integration(
        name="gold-api.com",
        kind="data_source",
        purpose="Live XAU/XAG (gold/silver) spot prices in USD for the gold-rates page.",
        status="live",
        cost="free, keyless",
        used_by=("gold_rates",),
        docs_url="https://gold-api.com",
        notes="Called by the daily Vercel Cron (api/gold-rates-cron.js) on manikumarjami.com, not by this fleet directly. No API key or auth needed.",
    ),
    Integration(
        name="frankfurter.dev",
        kind="data_source",
        purpose="USD-INR exchange rate used to convert international gold/silver spot prices into India rates.",
        status="live",
        cost="free, keyless",
        used_by=("gold_rates",),
        docs_url="https://frankfurter.dev",
        notes="Called by the daily Vercel Cron (api/gold-rates-cron.js) on manikumarjami.com, not by this fleet directly. No API key required.",
    ),
    Integration(
        name="Brevo",
        kind="email",
        purpose="Gold & Silver Alerts subscriber list + >5% move alert emails for the gold-rates page.",
        status="optional",
        cost="free tier (300 emails/day)",
        env_vars=("BREVO_API_KEY", "BREVO_GOLD_LIST_ID"),
        used_by=("gold_rates",),
        docs_url="https://developers.brevo.com",
        notes="Alert sends + list signups happen from manikumarjami.com's Vercel functions; this dashboard only reads the subscriber count for /agent/gold_rates. Both sides no-op gracefully if unset.",
    ),
    Integration(
        name="Postiz",
        kind="scheduler",
        purpose="Self-hosted multi-platform scheduler — Instagram, YouTube Shorts, LinkedIn from one calendar.",
        status="planned",
        cost="free, OSS, self-hosted",
        docs_url="https://github.com/gitroomhq/postiz-app",
        notes="Wire when ready to publish across platforms automatically. Until then, manual posting from the thought_mechanic brief.",
    ),
)


def get_status_for(env_var: str) -> bool:
    return bool(os.getenv(env_var, "").strip())


_PING_CACHE: dict[str, dict] = {}
_PING_TTL = 300.0  # 5 min — don't hammer integrations on every page load


def _cached_ping(key: str, fn) -> bool | None:
    now = _t.time()
    entry = _PING_CACHE.get(key)
    if entry and (now - entry["ts"]) < _PING_TTL:
        return entry["ok"]
    ok = fn()
    _PING_CACHE[key] = {"ts": now, "ok": ok}
    return ok


def _resend_ping() -> bool | None:
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        return None
    try:
        import httpx
        with httpx.Client(timeout=4.0) as c:
            r = c.get("https://api.resend.com/domains",
                      headers={"Authorization": f"Bearer {key}"})
            return r.status_code == 200
    except Exception:
        return False


def _github_ping() -> bool | None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return None
    try:
        import httpx
        with httpx.Client(timeout=4.0) as c:
            r = c.get("https://api.github.com/user",
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"})
            return r.status_code == 200
    except Exception:
        return False


def _meta_ping() -> bool | None:
    token = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    try:
        import httpx
        with httpx.Client(timeout=4.0) as c:
            r = c.get(f"https://graph.facebook.com/v18.0/me?access_token={token}")
            return r.status_code == 200
    except Exception:
        return False


def _n8n_ping() -> bool | None:
    base = os.getenv("N8N_API_BASE_URL", "").rstrip("/")
    key = os.getenv("N8N_API_KEY", "").strip()
    if not base or not key:
        # Vars not set yet → IG auto-responder still works (lives in n8n Cloud),
        # but we can't programmatically verify from here.
        return None
    try:
        import httpx
        with httpx.Client(timeout=4.0) as c:
            r = c.get(f"{base}/workflows?limit=1",
                      headers={"X-N8N-API-KEY": key})
            return r.status_code == 200
    except Exception:
        return False


def realtime_state(integration: Integration) -> dict:
    """Return runtime state: env vars set, connected, etc."""
    env_state = {v: get_status_for(v) for v in integration.env_vars}
    required_set = all(env_state.values()) if env_state else True

    live_check: bool | None = None
    name = integration.name
    if name == "Langfuse Cloud":
        live_check = lf.is_enabled()
    elif name == "Anthropic API":
        live_check = True if get_status_for("ANTHROPIC_API_KEY") else None
    elif name == "Resend":
        live_check = _cached_ping("resend", _resend_ping)
    elif name == "GitHub API":
        live_check = _cached_ping("github", _github_ping)
    elif name == "Meta Graph API (Instagram)":
        live_check = _cached_ping("meta", _meta_ping)
    elif name == "n8n Cloud":
        live_check = _cached_ping("n8n", _n8n_ping)
    elif name == "Claude Cloud Routines":
        # No API to ping. The dashboard verifies indirectly via "did today's
        # daily_ai_brief commit land in GitHub?" on the agent detail page.
        live_check = None

    return {
        "integration": integration,
        "env_state": env_state,
        "env_complete": required_set,
        "live_check": live_check,
    }


def all_states() -> list[dict]:
    return [realtime_state(i) for i in INTEGRATIONS]
