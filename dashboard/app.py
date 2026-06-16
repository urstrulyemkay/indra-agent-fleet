"""Indra — agent fleet admin dashboard.

Single-URL SPA-style admin UI built with FastAPI + HTMX + Tailwind.

Routes
======
Page routes (return full shell on direct hit, partial on HTMX navigation):
- GET  /                              → Overview
- GET  /activity                      → Activity feed (full)
- GET  /artifacts                     → All artifacts
- GET  /agent/{name}                  → Agent detail (workflow + run form + artifacts)

Partial routes (HTMX polls):
- GET  /api/sidebar                   → Sidebar agents grouped by category
- GET  /api/agent/{name}/pipeline     → Workflow pipeline (per-agent)
- GET  /api/agent/{name}/artifacts    → Artifacts list filtered by agent
- GET  /api/artifacts/partial         → All artifacts list
- GET  /api/events/stream             → SSE activity stream

Run routes (POST):
- POST /api/run/comment_dm_responder
- POST /api/run/job_hunter

Artifact viewer:
- GET  /api/artifacts/{id}/view       → raw markdown
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

# IMPORTANT: load_dotenv must happen before any module that touches
# LANGFUSE_* env vars (claude_client → langfuse_integration).
load_dotenv()

from agents.comment_dm_responder.agent import CommentDMResponderAgent
from agents.job_hunter.agent import JobHunterAgent, CV_BUILDER_DIR as JOB_HUNTER_CV_DIR
from agents.startup_lookup.agent import StartupLookupAgent, OUTPUTS_DIR as STARTUP_LOOKUP_DIR
from core import agent_metrics
from core import assessment_emails
from core import email_signups
from core import langfuse_integration as lf
from core import prompt_registry as pr
from core import resend_client
from core import status_bus
from core.fleet_manifest import FLEET, get_entry, grouped_by_category
from core.integrations_registry import all_states as integration_states
from core.niche_config import NICHES


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Indra")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# CORS — only allow the personalwebsite/labs origin (localhost dev + production).
# The labs site POSTs to /api/labs/results-email from the browser, so it needs an
# Access-Control-Allow-Origin header. We allow specific origins, NOT "*", because
# /api endpoints touch user-owned state (signups, sends).
_LABS_ORIGINS = [
    "http://localhost:8000",       # python -m http.server, per labs CLAUDE.md
    "http://127.0.0.1:8000",
    "https://manikumarjami.com",
    "https://www.manikumarjami.com",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_LABS_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


AGENT_CLASSES = {
    "comment_dm_responder": CommentDMResponderAgent,
    "job_hunter": JobHunterAgent,
    "startup_lookup": StartupLookupAgent,
}


# ---------- SECURITY ----------
#
# Three lines of defense:
#   1. HTTP Basic Auth — every dashboard surface requires user + password.
#   2. HMAC verification — every n8n webhook signed with shared secret +
#      timestamp (replay protection within a 5-minute window).
#   3. Defense in depth — Cloudflare Access on top is STRONGLY recommended.
#
# Webhook endpoints explicitly bypass Basic Auth (HMAC is their gate);
# the dashboard explicitly bypasses HMAC (Basic Auth + browser session is its).
# robots.txt is the only fully-public surface and exists to tell bots to leave.

REQUEST_MAX_BYTES = 256 * 1024            # 256 KB hard cap on any request body
WEBHOOK_MAX_AGE_SECONDS = 300              # 5 min window for replay protection
SEEN_NONCES: dict[str, float] = {}         # nonce → expiry timestamp
RATE_LIMIT_WINDOW = 60                     # seconds
RATE_LIMIT_MAX_RUNS = 12                   # agent runs per IP per minute
_rate_buckets: dict[str, list[float]] = defaultdict(list)

# Paths that bypass Basic Auth (HMAC-protected or fully public)
_NO_BASIC_AUTH_PATHS = {
    "/robots.txt",
    "/api/healthz",
}
_NO_BASIC_AUTH_PREFIXES = (
    "/api/webhooks/",   # HMAC-signed by n8n
    "/static/",
)


def _dashboard_creds() -> tuple[str, str] | None:
    user = os.getenv("INDRA_DASHBOARD_USER", "").strip()
    pwd = os.getenv("INDRA_DASHBOARD_PASSWORD", "").strip()
    if not user or not pwd:
        return None
    return user, pwd


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Combined security layer: Basic Auth + size limit + rate limit + noindex."""
    path = request.url.path

    # 1) Request size limit (defense against payload abuse)
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > REQUEST_MAX_BYTES:
        return JSONResponse(
            {"detail": "Request body too large"},
            status_code=413,
            headers={"X-Robots-Tag": "noindex, nofollow"},
        )

    # 2) HTTP Basic Auth — OPT-IN. Enforced only if INDRA_DASHBOARD_PASSWORD is
    #    set in .env. Default (no creds configured) is OPEN, which is safe when
    #    Indra binds to 127.0.0.1 only (the default). If you ever expose the
    #    dashboard publicly, set the password env var and auth turns back on.
    creds = _dashboard_creds()
    bypass_auth = (
        creds is None
        or path in _NO_BASIC_AUTH_PATHS
        or any(path.startswith(p) for p in _NO_BASIC_AUTH_PREFIXES)
    )
    if not bypass_auth:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Basic "):
            return Response(
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="Indra"',
                    "X-Robots-Tag": "noindex, nofollow",
                },
                content="Authentication required.",
                media_type="text/plain",
            )
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8", errors="strict")
            req_user, _, req_pwd = decoded.partition(":")
        except Exception:
            return Response(status_code=401, content="Invalid auth", media_type="text/plain")
        expected_user, expected_pwd = creds
        if not (
            hmac.compare_digest(req_user, expected_user)
            and hmac.compare_digest(req_pwd, expected_pwd)
        ):
            return Response(
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="Indra"',
                    "X-Robots-Tag": "noindex, nofollow",
                },
                content="Bad credentials.",
                media_type="text/plain",
            )

    # 3) Rate limit on the expensive agent run endpoints
    if path.startswith("/api/run/"):
        client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()
        now = time.time()
        bucket = _rate_buckets[client_ip]
        # Drop entries older than the window
        cutoff = now - RATE_LIMIT_WINDOW
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= RATE_LIMIT_MAX_RUNS:
            return JSONResponse(
                {"detail": f"Rate limit: max {RATE_LIMIT_MAX_RUNS} runs per {RATE_LIMIT_WINDOW}s"},
                status_code=429,
                headers={"Retry-After": str(RATE_LIMIT_WINDOW), "X-Robots-Tag": "noindex, nofollow"},
            )
        bucket.append(now)

    # 4) Execute the handler
    response = await call_next(request)

    # 5) Always-on response headers
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return PlainTextResponse(
        "User-agent: *\nDisallow: /\n",
        media_type="text/plain",
    )


def _prune_seen_nonces() -> None:
    now = time.time()
    for k in [k for k, exp in SEEN_NONCES.items() if exp < now]:
        SEEN_NONCES.pop(k, None)


# ---------- HMAC auth for n8n webhooks ----------


def _shared_secret() -> str:
    secret = os.getenv("INDRA_N8N_SHARED_SECRET", "").strip()
    return secret


def _verify_hmac(request: Request, body: bytes) -> None:
    """Verify the inbound webhook's signature + timestamp + nonce.

    Layers of defense:
    1. Shared secret HMAC-SHA256 of the raw body → only n8n (with secret) can sign
    2. Timestamp in X-Indra-Timestamp must be within ±5 minutes → blocks replays
    3. Nonce in X-Indra-Nonce must be unique → blocks duplicate-replay within window
    """
    secret = _shared_secret()
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="INDRA_N8N_SHARED_SECRET not configured. Set it in .env on both Indra and n8n.",
        )

    signature = request.headers.get("X-Indra-Signature", "").strip()
    timestamp = request.headers.get("X-Indra-Timestamp", "").strip()
    nonce = request.headers.get("X-Indra-Nonce", "").strip()

    if not signature:
        raise HTTPException(status_code=401, detail="Missing X-Indra-Signature header")
    if not timestamp or not nonce:
        # Timestamp + nonce are required for replay protection. n8n flow
        # template sets these. If you're calling Indra manually, set them too.
        raise HTTPException(
            status_code=401,
            detail="Missing X-Indra-Timestamp or X-Indra-Nonce header",
        )

    try:
        ts_int = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid X-Indra-Timestamp")
    now = int(time.time())
    if abs(now - ts_int) > WEBHOOK_MAX_AGE_SECONDS:
        raise HTTPException(
            status_code=401,
            detail=f"Stale request (age > {WEBHOOK_MAX_AGE_SECONDS}s)",
        )

    # Replay protection: each nonce can be used exactly once within the window.
    _prune_seen_nonces()
    if nonce in SEEN_NONCES:
        raise HTTPException(status_code=401, detail="Replay detected (nonce reused)")

    # HMAC over: timestamp + "." + nonce + "." + body
    signing_blob = f"{timestamp}.{nonce}.".encode() + body
    expected = hmac.new(secret.encode(), signing_blob, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Record the nonce so a replayer can't reuse it
    SEEN_NONCES[nonce] = float(now + WEBHOOK_MAX_AGE_SECONDS)


def _sign_payload(payload: bytes) -> tuple[str, str, str]:
    """Return (signature, timestamp, nonce) for outbound webhook calls.

    Use these in headers X-Indra-Signature / X-Indra-Timestamp / X-Indra-Nonce.
    The receiver (n8n in this direction) is expected to verify them the same way.
    """
    secret = _shared_secret()
    if not secret:
        return "", "", ""
    ts = str(int(time.time()))
    nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:32]
    signing_blob = f"{ts}.{nonce}.".encode() + payload
    sig = hmac.new(secret.encode(), signing_blob, hashlib.sha256).hexdigest()
    return sig, ts, nonce


@app.on_event("startup")
def _startup() -> None:
    status_bus.init_db()
    email_signups.init_table()
    assessment_emails.init_table()
    registered = {a["name"] for a in status_bus.list_agents()}
    for entry in FLEET:
        if entry.built and entry.name not in registered:
            status_bus.set_agent_status(entry.name, "idle")

    # Pre-warm the slow caches in a background thread so the first user-visible
    # load is served warm. These touch external services (GitHub, n8n) and take
    # 3-15s on cold boot; doing it in a daemon thread means uvicorn finishes
    # binding the socket immediately while the warmup happens behind the curtain.
    import threading

    def _warm() -> None:
        try:
            _brief_aggregate_stats()
        except Exception:
            pass
        try:
            _tm_aggregate_stats()
        except Exception:
            pass
        try:
            _ig_stats()
        except Exception:
            pass
        try:
            _labs_emailer_stats()
        except Exception:
            pass
        try:
            _mapc_delivery_stats()
        except Exception:
            pass
        try:
            _gold_rates_stats()
        except Exception:
            pass
        try:
            integration_states()  # warms Resend/GitHub/Meta/n8n pings
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True, name="indra-cache-prewarm").start()


# ---------- helpers ----------


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _agent_state(name: str) -> dict:
    entry = get_entry(name)
    agents = {a["name"]: a for a in status_bus.list_agents()}
    workflow_steps = []
    if entry.built and name in AGENT_CLASSES:
        workflow_steps = AGENT_CLASSES[name].workflow_steps

    a = agents.get(name)
    if a:
        try:
            meta = json.loads(a.get("meta_json") or "{}")
        except json.JSONDecodeError:
            meta = {}
        current_step_key = meta.get("step")
        current_idx = -1
        if current_step_key and workflow_steps:
            for i, s in enumerate(workflow_steps):
                if s.key == current_step_key:
                    current_idx = i
                    break
        return {
            "entry": entry,
            "workflow_steps": workflow_steps,
            "status": a["status"],
            "current_task": a["current_task"],
            "current_step_key": current_step_key,
            "current_step_idx": current_idx,
            "updated_at": a["updated_at"],
            "started_at": a["started_at"],
        }
    return {
        "entry": entry,
        "workflow_steps": workflow_steps,
        "status": "unregistered" if entry.built else "roadmap",
        "current_task": None,
        "current_step_key": None,
        "current_step_idx": -1,
        "updated_at": None,
        "started_at": None,
    }


def _grouped_states() -> list[tuple[str, dict, list[dict]]]:
    """[(cat_key, cat_meta, [agent_state...]), ...] for sidebar + overview."""
    out: list[tuple[str, dict, list[dict]]] = []
    for cat_key, cat_meta, entries in grouped_by_category():
        states = [_agent_state(e.name) for e in entries]
        out.append((cat_key, cat_meta, states))
    return out


def _stats() -> dict:
    built = sum(1 for e in FLEET if e.built)
    running = sum(1 for a in status_bus.list_agents() if a["status"] == "running")
    artifacts = len(status_bus.recent_artifacts(limit=1000))
    return {"built": built, "total": len(FLEET), "running": running, "artifacts": artifacts}


# ---------- n8n IG stats (live ops console) ----------

_IG_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_IG_STATS_TTL = 60.0  # 1 min — IG events are minutes apart at most; 60s stale is fine and saves a re-fetch on quick reloads


def _classify_exec(run_data: dict) -> tuple[str, str, str]:
    """Inspect an execution's runData and return (event_kind, outcome, sender).

    event_kind: 'comment' | 'quickreply' | 'other'
    outcome:    'link_sent' | 'gated' | 'regated' | 'dedup' | 'spam' | 'no_config' | 'own_reply' | 'error' | 'unknown'
    sender:     '@username' or sender_id
    """
    parse = (run_data.get("Parse webhook event") or [{}])[0]
    arr = ((parse.get("data") or {}).get("main") or [[]])[0]
    if not arr:
        return ("other", "unknown", "")
    j = arr[0].get("json", {})
    et = j.get("event_type", "other")
    sender = j.get("sender_username") or j.get("sender_id", "")
    if sender and not sender.startswith("@") and not sender.isdigit():
        sender = f"@{sender}"
    elif sender and sender.isdigit():
        sender = sender[-6:]  # short tail

    if et == "comment":
        if "Send DM (Private Replies)" in run_data:
            # link DM only if dm_decision=='send'
            cf = ((run_data.get("Check follower + dedup") or [{}])[0].get("data") or {}).get("main") or [[]]
            if cf and cf[0]:
                dm_dec = cf[0][0].get("json", {}).get("dm_decision", "")
                if dm_dec == "send":
                    return ("comment", "link_sent", sender)
                if dm_dec == "not_following":
                    return ("comment", "gated", sender)
                if dm_dec == "already_dmed":
                    return ("comment", "dedup", sender)
            return ("comment", "link_sent", sender)
        if "Audit: spam_skipped" in run_data:
            return ("comment", "spam", sender)
        if "Audit: no_config" in run_data:
            return ("comment", "no_config", sender)
        # routed straight to ACK = own reply or dedup
        if "Skip own replies + nested" in run_data:
            sr = ((run_data.get("Skip own replies + nested") or [{}])[0].get("data") or {}).get("main") or [[]]
            if len(sr) > 1 and sr[1]:
                return ("comment", "own_reply", sender)
        if "Should respond?" in run_data:
            sr = ((run_data.get("Should respond?") or [{}])[0].get("data") or {}).get("main") or [[]]
            if len(sr) > 1 and sr[1]:
                return ("comment", "dedup", sender)
        return ("comment", "unknown", sender)
    if et == "message_quickreply":
        if "QR: send link DM" in run_data:
            return ("quickreply", "link_sent", sender)
        if "QR: re-send gate DM" in run_data:
            return ("quickreply", "regated", sender)
        return ("quickreply", "unknown", sender)
    return ("other", "unknown", sender)


def _ig_stats() -> dict:
    """Pull recent n8n executions for the IG auto-responder workflow + aggregate.

    Returns a dict with: today_counts, totals, last_event, recent (list of events).
    Cached for _IG_STATS_TTL seconds.
    """
    import datetime as _dt
    now = time.time()
    if _IG_STATS_CACHE["data"] and (now - _IG_STATS_CACHE["ts"]) < _IG_STATS_TTL:
        return _IG_STATS_CACHE["data"]

    base = os.getenv("N8N_API_BASE_URL", "").rstrip("/")
    key = os.getenv("N8N_API_KEY", "")
    wf_id = os.getenv("INDRA_N8N_WORKFLOW_ID", "znewL0vNI0kxDdCz")
    out = {
        "configured": bool(base and key),
        "workflow_id": wf_id,
        "today": {"link_sent": 0, "gated": 0, "regated": 0, "dedup": 0, "spam": 0, "errors": 0, "total": 0},
        "yesterday": {"link_sent": 0, "gated": 0, "regated": 0, "dedup": 0, "spam": 0, "errors": 0, "total": 0},
        "last_7d": {"link_sent": 0, "gated": 0, "regated": 0, "dedup": 0, "spam": 0, "errors": 0, "total": 0},
        "all_time": {"link_sent": 0, "gated": 0, "regated": 0, "errors": 0, "total": 0},
        "conversion_rate": None,  # gated DMs that eventually became link sends (button taps converted)
        "by_post": {},  # post_id -> { link_sent, gated, total } for top posts
        "last_event_ts": None,
        "last_event_label": "",
        "recent": [],
        "posts": [],
        "active": False,
        "dmed_users": 0,
    }
    if not out["configured"]:
        _IG_STATS_CACHE["data"] = out
        _IG_STATS_CACHE["ts"] = now
        return out
    try:
        with httpx.Client(timeout=4.0) as c:
            r_wf = c.get(f"{base}/workflows/{wf_id}", headers={"X-N8N-API-KEY": key})
            r_wf.raise_for_status()
            wf = r_wf.json()
            out["active"] = bool(wf.get("active"))
            sd = (wf.get("staticData") or {}).get("global") or {}
            pm = sd.get("postMap") or {}
            dmed = sd.get("dmedUsers") or {}
            out["dmed_users"] = len(dmed)
            for pid, cfg in pm.items():
                out["posts"].append({
                    "post_id": pid,
                    "campaign": cfg.get("campaign", ""),
                    "niche": cfg.get("niche", ""),
                    "category": cfg.get("category", ""),
                    "payload": cfg.get("payload", ""),
                    "send_dm": cfg.get("send_dm", True),
                })

            r_ex = c.get(f"{base}/executions?workflowId={wf_id}&limit=60",
                         headers={"X-N8N-API-KEY": key})
            r_ex.raise_for_status()
            execs_list = (r_ex.json() or {}).get("data") or []
    except Exception as exc:
        out["error"] = str(exc)[:200]
        _IG_STATS_CACHE["data"] = out
        _IG_STATS_CACHE["ts"] = now
        return out

    # Two-pass: (1) cheap counting on ALL executions; (2) parallel per-execution
    # detail fetch capped at DETAIL_MAX (we only display ~12, and own_reply/spam
    # may further reduce the displayable set, so a small margin is enough).
    DETAIL_MAX = 10
    today = _dt.datetime.utcnow().date()
    exec_ids_to_detail: list[str] = []
    exec_meta_by_id: dict[str, tuple] = {}

    for e in execs_list:
        out["all_time"]["total"] += 1
        if e.get("status") == "error":
            out["all_time"]["errors"] += 1
        started = e.get("startedAt") or ""
        try:
            dt = _dt.datetime.fromisoformat(started.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
        is_today = dt.date() == today
        if is_today:
            out["today"]["total"] += 1
            if e.get("status") == "error":
                out["today"]["errors"] += 1
        if len(exec_ids_to_detail) < DETAIL_MAX:
            exec_ids_to_detail.append(e["id"])
            exec_meta_by_id[e["id"]] = (started, is_today, e.get("status"))

    # Pass 2: fan out per-execution detail fetches concurrently. Each n8n
    # call is ~200-500ms; 10 serial = 2-5s, 10 parallel = ~500ms.
    def _fetch_run_data(exec_id: str) -> tuple[str, dict]:
        try:
            with httpx.Client(timeout=4.0) as c:
                r_one = c.get(f"{base}/executions/{exec_id}?includeData=true",
                              headers={"X-N8N-API-KEY": key})
                return exec_id, ((r_one.json().get("data") or {}).get("resultData") or {}).get("runData") or {}
        except Exception:
            return exec_id, {}

    from concurrent.futures import ThreadPoolExecutor
    results_by_id: dict[str, dict] = {}
    if exec_ids_to_detail:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for eid, rd in pool.map(_fetch_run_data, exec_ids_to_detail):
                results_by_id[eid] = rd

    # Iterate in original (most-recent-first) order and build the recent list
    recent: list[dict] = []
    last_ts_iso = None
    last_label = ""
    for exec_id in exec_ids_to_detail:
        rd = results_by_id.get(exec_id)
        if not rd:
            continue
        kind, outcome, sender = _classify_exec(rd)
        if outcome in ("own_reply", "unknown") and kind != "quickreply":
            continue
        started, is_today, status = exec_meta_by_id[exec_id]
        recent.append({
            "ts": started,
            "kind": kind,
            "outcome": outcome,
            "sender": sender,
            "exec_id": exec_id,
            "status": status,
        })
        if is_today and outcome in out["today"]:
            out["today"][outcome] += 1
        if outcome in out["all_time"]:
            out["all_time"][outcome] += 1
        if not last_ts_iso:
            last_ts_iso = started
            last_label = f"{outcome.replace('_', ' ')}" + (f" → {sender}" if sender else "")

    out["recent"] = recent[:12]
    out["last_event_ts"] = last_ts_iso
    out["last_event_label"] = last_label
    _IG_STATS_CACHE["data"] = out
    _IG_STATS_CACHE["ts"] = now
    return out


def _shell_context(request: Request, main_template: str, **extra) -> dict:
    pending_drafts = status_bus.draft_counts_by_status().get("pending", 0)
    return {
        "request": request,
        "main_template": main_template,
        "langfuse_status": lf.get_status(),
        "niches": NICHES,
        "pending_drafts": pending_drafts,
        **extra,
    }


def _render(
    request: Request, main_template: str, *, active_view: str, active_agent: str = "", **ctx
) -> HTMLResponse:
    """Render a view. Return only the main partial for HTMX requests, or the
    full shell for direct page hits / refreshes."""
    base_ctx = _shell_context(
        request,
        main_template,
        active_view=active_view,
        active_agent=active_agent,
        **ctx,
    )
    if _is_htmx(request):
        return templates.TemplateResponse(main_template, base_ctx)
    return templates.TemplateResponse("shell.html", base_ctx)


# ---------- PAGE ROUTES ----------


@app.get("/", response_class=HTMLResponse)
def page_overview(request: Request):
    # Cheap shell render only. Each console panel fetches its own stats via
    # HTMX (hx-trigger="load") so the page chrome appears immediately and
    # slow GitHub/n8n aggregates don't block the shell.
    return _render(
        request,
        "_overview.html",
        active_view="overview",
        groups=_grouped_states(),
        stats=_stats(),
    )


@app.get("/api/ig-stats", response_class=HTMLResponse)
def api_ig_stats(request: Request):
    """HTMX target for the live IG ops console. Browser caches the response for
    the same duration as the server-side cache (8s) so navigating back to the
    overview page doesn't refetch."""
    resp = templates.TemplateResponse(
        "_ig_console.html",
        {"request": request, "ig": _ig_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=8"
    return resp


def _ig_activity_feed(limit: int = 60) -> list[dict]:
    """Unified activity stream for /activity page — pulls latest n8n executions for
    the IG auto-responder + GitHub commits for the brief agents. Sorted by ts desc."""
    import datetime as _dt
    feed: list[dict] = []

    # n8n executions (IG auto-responder)
    base = os.getenv("N8N_API_BASE_URL", "").rstrip("/")
    key = os.getenv("N8N_API_KEY", "")
    wf_id = os.getenv("INDRA_N8N_WORKFLOW_ID", "znewL0vNI0kxDdCz")
    if base and key:
        try:
            with httpx.Client(timeout=4.0) as c:
                r = c.get(f"{base}/executions?workflowId={wf_id}&limit={limit}",
                          headers={"X-N8N-API-KEY": key})
                if r.status_code == 200:
                    for e in (r.json() or {}).get("data") or []:
                        feed.append({
                            "ts": e.get("startedAt", ""),
                            "agent": "comment_dm_responder",
                            "agent_label": "Instagram auto-DM",
                            "kind": "n8n",
                            "level": "error" if e.get("status") == "error" else "info",
                            "title": f"webhook execution #{e['id']}",
                            "status": e.get("status"),
                            "link": f"https://umcrazy.app.n8n.cloud/workflow/{wf_id}/executions/{e['id']}",
                        })
        except Exception:
            pass

    # GitHub commits — daily-brief + thought-mechanic
    def _gh_commits(path: str, agent_name: str, agent_label: str, max_n: int = 15):
        items = []
        try:
            with httpx.Client(timeout=4.0) as c:
                r = c.get(
                    f"https://api.github.com/repos/{_BRIEF_REPO}/commits?path={path}&per_page={max_n}",
                    headers=_gh_headers(),
                )
                if r.status_code == 200:
                    for cm in r.json() or []:
                        commit = cm.get("commit", {}) or {}
                        msg = (commit.get("message") or "").split("\n", 1)[0][:90]
                        items.append({
                            "ts": (commit.get("author", {}) or {}).get("date", ""),
                            "agent": agent_name,
                            "agent_label": agent_label,
                            "kind": "github",
                            "level": "success",
                            "title": msg or "(no message)",
                            "status": "success",
                            "link": cm.get("html_url", ""),
                        })
        except Exception:
            pass
        return items

    feed.extend(_gh_commits("daily-brief", "daily_ai_brief", "Daily AI brief"))
    feed.extend(_gh_commits("thought-mechanic", "thought_mechanic", "Thought Mechanic"))

    feed.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return feed[:limit]


# ---------- Daily AI Brief (GitHub-backed cloud routine output) ----------

_BRIEF_REPO = "urstrulyemkay/emkayjami"
_BRIEF_PATH = "daily-brief"
_BRIEF_LIST_CACHE: dict = {"ts": 0.0, "data": None}
_BRIEF_LIST_TTL = 300.0  # 5 min — content only updates daily
_BRIEF_BODY_CACHE: dict[str, dict] = {}  # date_slug -> {ts, data}


def _parse_brief_frontmatter(md: str) -> tuple[dict, str]:
    """Strip a `---\\nkey: value\\n---` block from the top, return (meta, body)."""
    meta: dict = {}
    body = md
    if md.startswith("---"):
        end = md.find("---", 3)
        if end > 0:
            head = md[3:end].strip()
            body = md[end + 3:].lstrip("\n")
            for line in head.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if v.startswith("[") and v.endswith("]"):
                    try:
                        v = [t.strip().strip('"').strip("'") for t in v[1:-1].split(",") if t.strip()]
                    except Exception:
                        pass
                if k == "confidence":
                    try:
                        v = int(v)
                    except Exception:
                        pass
                meta[k] = v
    return meta, body


def _gh_headers() -> dict:
    tok = os.getenv("GITHUB_TOKEN", "") or ""
    h = {"Accept": "application/vnd.github+json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _brief_list() -> dict:
    """Return cached list of daily-brief markdown files from GitHub."""
    now = time.time()
    cached = _BRIEF_LIST_CACHE.get("data")
    if cached and (now - _BRIEF_LIST_CACHE["ts"]) < _BRIEF_LIST_TTL:
        return cached
    out: dict = {"briefs": [], "error": None, "fetched_at": None}
    try:
        with httpx.Client(timeout=6.0) as c:
            r = c.get(
                f"https://api.github.com/repos/{_BRIEF_REPO}/contents/{_BRIEF_PATH}",
                headers=_gh_headers(),
            )
            r.raise_for_status()
            items = r.json()
            for it in items:
                if it.get("type") != "file" or not it.get("name", "").endswith(".md"):
                    continue
                date_slug = it["name"].rsplit(".", 1)[0]
                out["briefs"].append({
                    "date": date_slug,
                    "name": it["name"],
                    "size": it.get("size", 0),
                    "sha": it.get("sha", ""),
                    "html_url": it.get("html_url", ""),
                })
            out["briefs"].sort(key=lambda b: b["date"], reverse=True)
        out["fetched_at"] = now
    except Exception as exc:
        out["error"] = str(exc)[:200]
    _BRIEF_LIST_CACHE["data"] = out
    _BRIEF_LIST_CACHE["ts"] = now
    return out


def _render_brief_md(md: str) -> str:
    """Render brief markdown to HTML with our extensions (tables, fenced code, autolink)."""
    try:
        import markdown as _md
        return _md.markdown(md, extensions=["fenced_code", "tables", "sane_lists", "nl2br"])
    except Exception:
        # Fallback: rudimentary line-break preservation if lib import fails
        from html import escape as _e
        return "<p>" + _e(md).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def _brief_body(date_slug: str) -> dict:
    """Return cached, parsed body of one daily brief."""
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_slug):
        return {"error": "Invalid date slug"}
    now = time.time()
    cached = _BRIEF_BODY_CACHE.get(date_slug)
    if cached and (now - cached["ts"]) < 86400:  # 24h cache
        return cached["data"]
    out: dict = {"date": date_slug, "meta": {}, "body": "", "error": None}
    try:
        import base64 as _b64
        with httpx.Client(timeout=6.0) as c:
            r = c.get(
                f"https://api.github.com/repos/{_BRIEF_REPO}/contents/{_BRIEF_PATH}/{date_slug}.md",
                headers=_gh_headers(),
            )
            r.raise_for_status()
            data = r.json()
            content_b64 = (data.get("content") or "").replace("\n", "")
            md = _b64.b64decode(content_b64).decode("utf-8", errors="replace")
            meta, body = _parse_brief_frontmatter(md)
            out["meta"] = meta
            out["body_md"] = body
            out["body_html"] = _render_brief_md(body)
            out["html_url"] = data.get("html_url", "")
    except Exception as exc:
        out["error"] = str(exc)[:200]
    _BRIEF_BODY_CACHE[date_slug] = {"ts": now, "data": out}
    return out


_BRIEF_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_BRIEF_STATS_TTL = 3600.0  # 1h — aggregate iteration over every brief is expensive


def _brief_aggregate_stats() -> dict:
    """Aggregate metrics across every daily brief: total, streak, domain breakdown,
    avg confidence. Hot path is cached for 1h since the underlying briefs change at most once/day."""
    import datetime as _dt
    now = time.time()
    cached = _BRIEF_STATS_CACHE.get("data")
    if cached and (now - _BRIEF_STATS_CACHE["ts"]) < _BRIEF_STATS_TTL:
        return cached

    out = {
        "total": 0,
        "streak_days": 0,
        "latest_date": "",
        "days_since_latest": None,
        "domains": [],          # [(domain, count), ...] sorted desc, top 8
        "avg_confidence": None,
        "recent_titles": [],    # [{date, title, domain, confidence}, ...] last 5
        "configured": False,
        "error": None,
    }
    listing = _brief_list()
    if listing.get("error"):
        out["error"] = listing["error"]
        _BRIEF_STATS_CACHE["data"] = out
        _BRIEF_STATS_CACHE["ts"] = now
        return out
    briefs = listing.get("briefs", [])
    if not briefs:
        _BRIEF_STATS_CACHE["data"] = out
        _BRIEF_STATS_CACHE["ts"] = now
        return out

    out["configured"] = True
    out["total"] = len(briefs)
    out["latest_date"] = briefs[0]["date"]
    try:
        latest = _dt.datetime.strptime(briefs[0]["date"], "%Y-%m-%d").date()
        today = _dt.datetime.utcnow().date()
        out["days_since_latest"] = (today - latest).days
    except Exception:
        pass

    # Streak: walk back from latest, count consecutive days
    try:
        dates = set()
        for b in briefs:
            try:
                dates.add(_dt.datetime.strptime(b["date"], "%Y-%m-%d").date())
            except Exception:
                continue
        if dates:
            streak = 0
            cur = max(dates)
            while cur in dates:
                streak += 1
                cur -= _dt.timedelta(days=1)
            out["streak_days"] = streak
    except Exception:
        pass

    # Per-brief frontmatter. _brief_body() has a 24h per-date cache, but on a
    # genuine cold start we fetch every brief from GitHub serially → ~15s for
    # ~30 briefs. Parallelize the fetches via a thread pool; each call is
    # IO-bound (GitHub round-trip), so threads release the GIL during requests.
    from concurrent.futures import ThreadPoolExecutor
    domain_counts: dict[str, int] = {}
    conf_values: list[int] = []
    dates = [b["date"] for b in briefs]
    bodies_by_date: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        for date, body in zip(dates, pool.map(_brief_body, dates)):
            bodies_by_date[date] = body
    for i, b in enumerate(briefs):
        body = bodies_by_date.get(b["date"]) or {}
        meta = body.get("meta", {}) or {}
        d = (meta.get("domain") or "").strip()
        if d:
            domain_counts[d] = domain_counts.get(d, 0) + 1
        c = meta.get("confidence")
        if isinstance(c, int):
            conf_values.append(c)
        if i < 5:
            out["recent_titles"].append({
                "date": b["date"],
                "title": meta.get("title") or "(untitled)",
                "domain": d,
                "confidence": c if isinstance(c, int) else None,
            })

    out["domains"] = sorted(domain_counts.items(), key=lambda kv: -kv[1])[:8]
    if conf_values:
        out["avg_confidence"] = round(sum(conf_values) / len(conf_values), 1)

    _BRIEF_STATS_CACHE["data"] = out
    _BRIEF_STATS_CACHE["ts"] = now
    return out


@app.get("/api/brief-stats", response_class=HTMLResponse)
def api_brief_stats(request: Request):
    """HTMX target for the daily-brief console panel. Briefs update at most
    once per day, so the browser caches the response for 10 minutes."""
    resp = templates.TemplateResponse(
        "_daily_brief_console.html",
        {"request": request, "brief_stats": _brief_aggregate_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


def _brief_archive_with_metas() -> list[dict]:
    """For every brief, return {date, title, domain, confidence, tags} using
    per-brief 24h cache. After the first hit this is essentially free."""
    listing = _brief_list()
    out = []
    for b in listing.get("briefs", []):
        body = _brief_body(b["date"])
        meta = body.get("meta", {}) or {}
        out.append({
            "date": b["date"],
            "title": meta.get("title") or "(untitled)",
            "domain": (meta.get("domain") or "").strip(),
            "confidence": meta.get("confidence") if isinstance(meta.get("confidence"), int) else None,
            "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
            "html_url": body.get("html_url", ""),
        })
    return out


def _heatmap_grid(date_set: set, days: int = 90) -> list[list[dict]]:
    """Build a GitHub-style heatmap grid for the last N days. Returns rows
    of weeks (each row = 7 days, ordered Sun..Sat). Each cell:
    {date: 'YYYY-MM-DD', present: bool, label: 'Mon May 17'}.
    Missing cells (before earliest day shown) are returned with present=None.
    """
    import datetime as _dt
    today = _dt.datetime.utcnow().date()
    start = today - _dt.timedelta(days=days - 1)
    # Snap start to its previous Sunday for clean column alignment
    start = start - _dt.timedelta(days=(start.weekday() + 1) % 7)
    weeks: list[list[dict]] = []
    cur = start
    while cur <= today:
        week = []
        for _ in range(7):
            if cur > today:
                week.append({"date": "", "present": None, "label": ""})
            else:
                ds = cur.strftime("%Y-%m-%d")
                week.append({
                    "date": ds,
                    "present": ds in date_set,
                    "label": cur.strftime("%a %b %d"),
                    "is_today": cur == today,
                })
            cur += _dt.timedelta(days=1)
        weeks.append(week)
    return weeks


@app.get("/agent/daily_ai_brief", response_class=HTMLResponse)
def page_daily_brief(request: Request, date: str = ""):
    state = _agent_state("daily_ai_brief")
    listing = _brief_list()
    archive = _brief_archive_with_metas()
    # Default-select the latest brief if no date specified
    selected_date = date or (listing.get("briefs", [{}])[0].get("date", "") if listing.get("briefs") else "")
    body = _brief_body(selected_date) if selected_date else None
    # Build heatmap grid (90 days)
    date_set = {b["date"] for b in listing.get("briefs", [])}
    heatmap_weeks = _heatmap_grid(date_set, days=90)
    return _render(
        request,
        "_daily_brief.html",
        active_view="agents",
        active_agent="daily_ai_brief",
        state=state,
        listing=listing,
        archive=archive,
        selected=body,
        selected_date=selected_date,
        heatmap_weeks=heatmap_weeks,
        brief_stats=_brief_aggregate_stats(),
    )


@app.get("/api/daily-brief/{date}", response_class=HTMLResponse)
def api_brief_body(request: Request, date: str):
    body = _brief_body(date)
    return templates.TemplateResponse(
        "_daily_brief_body.html",
        {"request": request, "selected": body, "selected_date": date},
    )


# ---------- Thought Mechanic (second cloud routine → github agent) ----------

_TM_PATH = "thought-mechanic"
_TM_LIST_CACHE: dict = {"ts": 0.0, "data": None}
_TM_LIST_TTL = 300.0
_TM_BODY_CACHE: dict[str, dict] = {}
_TM_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_TM_STATS_TTL = 3600.0


def _tm_list() -> dict:
    """List daily reel briefs from thought-mechanic/ on GitHub. Excludes the static
    PLAYBOOK.md (treated separately). 5-min cache."""
    now = time.time()
    cached = _TM_LIST_CACHE.get("data")
    if cached and (now - _TM_LIST_CACHE["ts"]) < _TM_LIST_TTL:
        return cached
    out: dict = {"briefs": [], "has_playbook": False, "error": None}
    try:
        with httpx.Client(timeout=6.0) as c:
            r = c.get(
                f"https://api.github.com/repos/{_BRIEF_REPO}/contents/{_TM_PATH}",
                headers=_gh_headers(),
            )
            r.raise_for_status()
            items = r.json()
            for it in items:
                if it.get("type") != "file" or not it.get("name", "").endswith(".md"):
                    continue
                name = it["name"]
                if name == "PLAYBOOK.md":
                    out["has_playbook"] = True
                    continue
                stem = name.rsplit(".", 1)[0]
                # Accept either YYYY-MM-DD.md (legacy) or tm-YYYY-MM-DD.md (current as of 2026-05-17).
                import re as _re
                m = _re.fullmatch(r"(?:tm-)?(\d{4}-\d{2}-\d{2})", stem)
                if not m:
                    continue
                date_slug = m.group(1)
                out["briefs"].append({
                    "date": date_slug,
                    "name": name,
                    "filename_stem": stem,
                    "size": it.get("size", 0),
                    "html_url": it.get("html_url", ""),
                })
            out["briefs"].sort(key=lambda b: b["date"], reverse=True)
    except Exception as exc:
        out["error"] = str(exc)[:200]
    _TM_LIST_CACHE["data"] = out
    _TM_LIST_CACHE["ts"] = now
    return out


def _tm_body(date_slug: str) -> dict:
    """Fetch + parse one thought-mechanic daily brief. 24h per-entry cache.
    Filename pattern changed 2026-05-17 from YYYY-MM-DD.md to tm-YYYY-MM-DD.md.
    We try the new pattern first, then fall back to the legacy one."""
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_slug):
        return {"error": "Invalid date slug"}
    now = time.time()
    cached = _TM_BODY_CACHE.get(date_slug)
    if cached and (now - cached["ts"]) < 86400:
        return cached["data"]
    out: dict = {"date": date_slug, "meta": {}, "body_md": "", "body_html": "", "error": None}
    try:
        import base64 as _b64
        with httpx.Client(timeout=6.0) as c:
            # Try current (tm- prefix) first, fall back to legacy (no prefix).
            r = c.get(
                f"https://api.github.com/repos/{_BRIEF_REPO}/contents/{_TM_PATH}/tm-{date_slug}.md",
                headers=_gh_headers(),
            )
            if r.status_code == 404:
                r = c.get(
                    f"https://api.github.com/repos/{_BRIEF_REPO}/contents/{_TM_PATH}/{date_slug}.md",
                    headers=_gh_headers(),
                )
            r.raise_for_status()
            data = r.json()
            content_b64 = (data.get("content") or "").replace("\n", "")
            md = _b64.b64decode(content_b64).decode("utf-8", errors="replace")
            meta, body = _parse_brief_frontmatter(md)
            # thought-mechanic stores virality_score as "92/100" — extract the number
            vs = meta.get("virality_score")
            if isinstance(vs, str) and "/" in vs:
                try:
                    meta["virality_score"] = int(vs.split("/", 1)[0].strip())
                except Exception:
                    pass
            elif isinstance(vs, str):
                try:
                    meta["virality_score"] = int(vs.strip())
                except Exception:
                    pass
            out["meta"] = meta
            out["body_md"] = body
            out["body_html"] = _render_brief_md(body)
            out["html_url"] = data.get("html_url", "")
    except Exception as exc:
        out["error"] = str(exc)[:200]
    _TM_BODY_CACHE[date_slug] = {"ts": now, "data": out}
    return out


def _tm_playbook() -> dict:
    """Fetch PLAYBOOK.md once (24h cache) — static brand-voice + content-pillar doc."""
    cache_key = "__playbook__"
    now = time.time()
    cached = _TM_BODY_CACHE.get(cache_key)
    if cached and (now - cached["ts"]) < 86400:
        return cached["data"]
    out: dict = {"body_md": "", "body_html": "", "error": None, "html_url": ""}
    try:
        import base64 as _b64
        with httpx.Client(timeout=6.0) as c:
            r = c.get(
                f"https://api.github.com/repos/{_BRIEF_REPO}/contents/{_TM_PATH}/PLAYBOOK.md",
                headers=_gh_headers(),
            )
            r.raise_for_status()
            data = r.json()
            content_b64 = (data.get("content") or "").replace("\n", "")
            md = _b64.b64decode(content_b64).decode("utf-8", errors="replace")
            out["body_md"] = md
            out["body_html"] = _render_brief_md(md)
            out["html_url"] = data.get("html_url", "")
    except Exception as exc:
        out["error"] = str(exc)[:200]
    _TM_BODY_CACHE[cache_key] = {"ts": now, "data": out}
    return out


def _tm_archive_with_metas() -> list[dict]:
    listing = _tm_list()
    out = []
    for b in listing.get("briefs", []):
        body = _tm_body(b["date"])
        meta = body.get("meta", {}) or {}
        out.append({
            "date": b["date"],
            "title": meta.get("title") or "(untitled)",
            "handle": meta.get("handle") or "",
            "pillar": meta.get("content_pillar") or "",
            "domain": meta.get("domain") or "",
            "virality": meta.get("virality_score") if isinstance(meta.get("virality_score"), int) else None,
            "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
            "format": meta.get("format") or "",
            "html_url": body.get("html_url", ""),
        })
    return out


def _tm_aggregate_stats() -> dict:
    import datetime as _dt
    now = time.time()
    cached = _TM_STATS_CACHE.get("data")
    if cached and (now - _TM_STATS_CACHE["ts"]) < _TM_STATS_TTL:
        return cached

    out = {
        "total": 0, "streak_days": 0, "latest_date": "",
        "days_since_latest": None, "avg_virality": None,
        "pillars": [], "recent_titles": [], "has_playbook": False, "error": None,
    }
    listing = _tm_list()
    if listing.get("error"):
        out["error"] = listing["error"]
        _TM_STATS_CACHE["data"] = out
        _TM_STATS_CACHE["ts"] = now
        return out
    out["has_playbook"] = listing.get("has_playbook", False)
    briefs = listing.get("briefs", [])
    if not briefs:
        _TM_STATS_CACHE["data"] = out
        _TM_STATS_CACHE["ts"] = now
        return out

    out["total"] = len(briefs)
    out["latest_date"] = briefs[0]["date"]
    try:
        latest = _dt.datetime.strptime(briefs[0]["date"], "%Y-%m-%d").date()
        today = _dt.datetime.utcnow().date()
        out["days_since_latest"] = (today - latest).days
        dates = set()
        for b in briefs:
            try:
                dates.add(_dt.datetime.strptime(b["date"], "%Y-%m-%d").date())
            except Exception:
                continue
        if dates:
            streak = 0
            cur = max(dates)
            while cur in dates:
                streak += 1
                cur -= _dt.timedelta(days=1)
            out["streak_days"] = streak
    except Exception:
        pass

    # Parallel per-brief body fetch (same pattern as _brief_aggregate_stats).
    from concurrent.futures import ThreadPoolExecutor
    pillar_counts: dict[str, int] = {}
    virs: list[int] = []
    dates = [b["date"] for b in briefs]
    bodies_by_date: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        for date, body in zip(dates, pool.map(_tm_body, dates)):
            bodies_by_date[date] = body
    for i, b in enumerate(briefs):
        body = bodies_by_date.get(b["date"]) or {}
        meta = body.get("meta", {}) or {}
        p = (meta.get("content_pillar") or "").strip()
        if p:
            # Pillar values may have "/" e.g., "Cognitive Awareness / System-Critique" — split & count each side
            for piece in [x.strip() for x in p.split("/") if x.strip()]:
                pillar_counts[piece] = pillar_counts.get(piece, 0) + 1
        v = meta.get("virality_score")
        if isinstance(v, int):
            virs.append(v)
        if i < 5:
            out["recent_titles"].append({
                "date": b["date"],
                "title": meta.get("title") or "(untitled)",
                "pillar": p,
                "virality": v if isinstance(v, int) else None,
            })

    out["pillars"] = sorted(pillar_counts.items(), key=lambda kv: -kv[1])[:8]
    if virs:
        out["avg_virality"] = round(sum(virs) / len(virs), 1)

    _TM_STATS_CACHE["data"] = out
    _TM_STATS_CACHE["ts"] = now
    return out


@app.get("/agent/thought_mechanic", response_class=HTMLResponse)
def page_thought_mechanic(request: Request, date: str = ""):
    state = _agent_state("thought_mechanic")
    listing = _tm_list()
    archive = _tm_archive_with_metas()
    selected_date = date or (listing.get("briefs", [{}])[0].get("date", "") if listing.get("briefs") else "")
    body = _tm_body(selected_date) if selected_date else None
    date_set = {b["date"] for b in listing.get("briefs", [])}
    heatmap_weeks = _heatmap_grid(date_set, days=90)
    return _render(
        request,
        "_thought_mechanic.html",
        active_view="agents",
        active_agent="thought_mechanic",
        state=state,
        listing=listing,
        archive=archive,
        selected=body,
        selected_date=selected_date,
        heatmap_weeks=heatmap_weeks,
        tm_stats=_tm_aggregate_stats(),
        playbook=_tm_playbook(),
    )


@app.get("/api/tm-stats", response_class=HTMLResponse)
def api_tm_stats(request: Request):
    """HTMX target for the Thought Mechanic console panel. Briefs update at
    most once per day, so the browser caches the response for 10 minutes."""
    resp = templates.TemplateResponse(
        "_thought_mechanic_console.html",
        {"request": request, "tm_stats": _tm_aggregate_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


# ---------- Labs results emailer (live view, sourced from Resend) ----------

_LABS_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_LABS_STATS_TTL = 60.0  # 1 min — Resend has the data; we just surface it. No persistence.

_MAPC_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_MAPC_STATS_TTL = 60.0

_GOLD_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_GOLD_STATS_TTL = 60.0


def _labs_emailer_stats() -> dict:
    """Pull recent labs result-email sends from Resend, filter to the labs
    verified subdomain. Cached for 60s to avoid hammering Resend on every
    page load. Returns: domain_verified, today_count, week_count, last_send,
    last 10 sends + per-test breakdown. Zero local storage — Resend is the
    source of truth."""
    import datetime as _dt
    now = time.time()
    if _LABS_STATS_CACHE["data"] and (now - _LABS_STATS_CACHE["ts"]) < _LABS_STATS_TTL:
        return _LABS_STATS_CACHE["data"]

    out: dict = {
        "configured": False,
        "domain_verified": False,
        "domain_name": "labs.manikumarjami.com",
        "today_count": 0,
        "week_count": 0,
        "total_visible": 0,
        "last_send_ts": None,
        "recent": [],         # last 10 sends, each: {ts, recipient_masked, test_label, status, id}
        "by_test": {},        # test_label -> count over the visible window
        "by_status": {},      # delivery status -> count
        "error": None,
    }

    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        out["error"] = "RESEND_API_KEY not set in .env"
        _LABS_STATS_CACHE["data"] = out
        _LABS_STATS_CACHE["ts"] = now
        return out

    out["configured"] = True
    HDR = {"Authorization": f"Bearer {key}"}

    try:
        with httpx.Client(timeout=15.0) as c:
            # Domain verification status (cheap, one call)
            try:
                rd = c.get("https://api.resend.com/domains", headers=HDR)
                if rd.status_code == 200:
                    for d in rd.json().get("data", []):
                        if d.get("name") == out["domain_name"]:
                            out["domain_verified"] = d.get("status") == "verified"
                            break
            except Exception:
                pass

            # Recent 100 labs emails — sufficient for trend view
            r = c.get("https://api.resend.com/emails?limit=100", headers=HDR)
            if r.status_code != 200:
                out["error"] = f"Resend list /emails returned {r.status_code}"
                _LABS_STATS_CACHE["data"] = out
                _LABS_STATS_CACHE["ts"] = now
                return out

            emails = r.json().get("data", []) or []
            _MAPC_SUBJECTS = ("mapc exam prep", "exam prep ·", "study guides are ready",
                               "material you requested")
            labs_emails = [
                e for e in emails
                if out["domain_name"] in (e.get("from") or "").lower()
                and not any(kw in (e.get("subject") or "").lower() for kw in _MAPC_SUBJECTS)
            ]
            out["total_visible"] = len(labs_emails)

            today_utc = _dt.datetime.utcnow().date()
            week_ago = today_utc - _dt.timedelta(days=6)  # last 7 days inclusive
            for e in labs_emails:
                ts_str = (e.get("created_at") or "").strip()
                dt = None
                try:
                    # Resend timestamp is "YYYY-MM-DD HH:MM:SS.ffffff+00" — normalize
                    ts_clean = ts_str.replace("Z", "+00:00").split(".")[0]
                    dt = _dt.datetime.fromisoformat(ts_clean.replace(" ", "T"))
                except Exception:
                    pass
                if dt:
                    dt_date = dt.date()
                    if dt_date == today_utc:
                        out["today_count"] += 1
                    if dt_date >= week_ago:
                        out["week_count"] += 1
                status = (e.get("last_event") or "?").strip()
                out["by_status"][status] = out["by_status"].get(status, 0) + 1

            _IST_DELTA = _dt.timedelta(hours=5, minutes=30)

            def _lab_ts_ist(ts_raw: str) -> str:
                try:
                    ts_clean = (ts_raw or "")[:19].replace(" ", "T")
                    dt_utc = _dt.datetime.fromisoformat(ts_clean)
                    return (dt_utc + _IST_DELTA).strftime("%-d %b %Y, %I:%M %p IST")
                except Exception:
                    return (ts_raw or "")[:19]

            for e in labs_emails[:50]:
                to_list = e.get("to") or []
                to = (to_list[0] if to_list else "").strip()
                subject = (e.get("subject") or "").strip()
                test_label = ""
                if subject.startswith("Your ") and " results" in subject:
                    test_label = subject[len("Your "):subject.rfind(" results")].strip()
                if test_label:
                    out["by_test"][test_label] = out["by_test"].get(test_label, 0) + 1
                out["recent"].append({
                    "ts": _lab_ts_ist(e.get("created_at") or ""),
                    "email": to,
                    "subject": subject[:80],
                    "test_label": test_label or "—",
                    "status": e.get("last_event") or "?",
                    "id": e.get("id") or "",
                })

            if labs_emails:
                out["last_send_ts"] = (labs_emails[0].get("created_at") or "")[:19]
    except Exception as exc:
        out["error"] = str(exc)[:200]

    _LABS_STATS_CACHE["data"] = out
    _LABS_STATS_CACHE["ts"] = now
    return out


def _mapc_delivery_stats() -> dict:
    """Query Resend for MAPC guide delivery emails. Cached 60s. No local storage."""
    import datetime as _dt
    now = time.time()
    if _MAPC_STATS_CACHE["data"] and (now - _MAPC_STATS_CACHE["ts"]) < _MAPC_STATS_TTL:
        return _MAPC_STATS_CACHE["data"]

    out: dict = {
        "configured": False,
        "today_count": 0,
        "week_count": 0,
        "total_visible": 0,
        "last_send_ts": None,
        "recent": [],
        "by_status": {},
        "error": None,
    }

    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        out["error"] = "RESEND_API_KEY not set"
        _MAPC_STATS_CACHE.update({"data": out, "ts": now})
        return out

    out["configured"] = True
    HDR = {"Authorization": f"Bearer {key}"}

    _TEST_EMAILS = {"manikumarjami1@gmail.com", "manikumarjami007@gmail.com",
                    "manikumarjami@gmail.com", "iamemkayjami@gmail.com",
                    "ratelimit_test_qa@example.com"}
    _IST = _dt.timedelta(hours=5, minutes=30)

    def _to_ist(ts_raw: str) -> str:
        try:
            dt_utc = _dt.datetime.fromisoformat(ts_raw[:19].replace(" ", "T"))
            return (dt_utc + _IST).strftime("%-d %b %Y, %I:%M %p IST")
        except Exception:
            return ts_raw

    try:
        # ── Read from mapc_all_sent.json (Gist) — single fast call, no pagination ──
        github_token = os.getenv("GITHUB_TOKEN", "")
        gist_id = os.getenv("MAPC_QUEUE_GIST", "")
        all_sent: list = []

        if github_token and gist_id:
            with httpx.Client(timeout=10.0) as c:
                gr = c.get(f"https://api.github.com/gists/{gist_id}",
                           headers={"Authorization": f"token {github_token}",
                                    "Accept": "application/vnd.github.v3+json"})
                if gr.status_code == 200:
                    raw = gr.json().get("files", {}).get("mapc_all_sent.json", {}).get("content", "[]")
                    all_sent = json.loads(raw)

        # Filter test emails
        all_sent = [s for s in all_sent if s.get("email","").lower() not in _TEST_EMAILS]

        # Deduplicate by email (keep first = most recent since we unshift)
        seen: dict = {}
        for s in all_sent:
            em = s.get("email", "").strip().lower()
            if em and em not in seen:
                seen[em] = s

        # Count today / week
        today_utc = _dt.datetime.utcnow().date()
        week_ago = today_utc - _dt.timedelta(days=6)
        for s in seen.values():
            ts_str = s.get("sent_at", "")
            try:
                dt = _dt.datetime.fromisoformat(ts_str[:19].replace(" ", "T"))
                dt_date = dt.date()
                if dt_date == today_utc:
                    out["today_count"] += 1
                if dt_date >= week_ago:
                    out["week_count"] += 1
            except Exception:
                pass
            status = s.get("status", "delivered")
            out["by_status"][status] = out["by_status"].get(status, 0) + 1

        out["total_visible"] = len(seen)

        # Course label from specialisation field
        SPEC_LABELS = {
            "counselling": "Counselling", "clinical": "Clinical",
            "io": "I&O Psychology", "yr1": "1st Year",
        }

        # Recent list — newest first (already unshifted in Gist)
        for em, s in list(seen.items())[:100]:
            out["recent"].append({
                "ts":     _to_ist(s.get("sent_at", "")),
                "email":  em,
                "status": s.get("status", "delivered"),
                "course": SPEC_LABELS.get(s.get("specialisation", ""), s.get("specLabel", "MAPC")),
                "source": "MAPC Page",
                "via":    s.get("via", "Resend").capitalize(),
                "id":     "",
            })

        if seen:
            first = next(iter(seen.values()))
            out["last_send_ts"] = _to_ist(first.get("sent_at", ""))

    except Exception as exc:
        out["error"] = str(exc)[:200]

    _MAPC_STATS_CACHE.update({"data": out, "ts": now})
    return out


@app.get("/api/mapc-stats", response_class=HTMLResponse)
def api_mapc_stats(request: Request):
    resp = templates.TemplateResponse(
        "_mapc_console.html",
        {"request": request, "mapc": _mapc_delivery_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.get("/agent/mapc_delivery", response_class=HTMLResponse)
def page_mapc_delivery(request: Request):
    return _render(
        request,
        "_mapc_console.html",
        active_view="agent",
        active_agent="mapc_delivery",
        mapc=_mapc_delivery_stats(),
    )


def _gold_rates_stats() -> dict:
    """Read the latest gold/silver rates snapshot (public GOLD_RATES_GIST),
    Gold & Silver Alerts subscriber count (Brevo), and recent >5% alert
    sends (gold_sent.json in the private MAPC_QUEUE_GIST). Cached 60s."""
    now = time.time()
    if _GOLD_STATS_CACHE["data"] and (now - _GOLD_STATS_CACHE["ts"]) < _GOLD_STATS_TTL:
        return _GOLD_STATS_CACHE["data"]

    out: dict = {
        "configured": False,
        "snapshot": None,
        "subscriber_count": None,
        "alert_history": [],
        "error": None,
    }

    github_token = os.getenv("GITHUB_TOKEN", "")
    gist_headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        gist_headers["Authorization"] = f"token {github_token}"

    gist_id = os.getenv("GOLD_RATES_GIST", "")
    if not gist_id:
        out["error"] = "GOLD_RATES_GIST not set"
        _GOLD_STATS_CACHE.update({"data": out, "ts": now})
        return out

    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"https://api.github.com/gists/{gist_id}", headers=gist_headers)
            if r.status_code == 200:
                raw = r.json().get("files", {}).get("gold_rates_latest.json", {}).get("content", "")
                if raw:
                    out["snapshot"] = json.loads(raw)
                    out["configured"] = True
            else:
                out["error"] = f"Gist fetch failed: {r.status_code}"
    except Exception as exc:
        out["error"] = str(exc)[:200]

    # Subscriber count — Gold & Silver Alerts Brevo list
    brevo_key = os.getenv("BREVO_API_KEY", "")
    list_id = os.getenv("BREVO_GOLD_LIST_ID", "")
    if brevo_key and list_id:
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(
                    f"https://api.brevo.com/v3/contacts/lists/{list_id}",
                    headers={"api-key": brevo_key},
                )
                if r.status_code == 200:
                    out["subscriber_count"] = r.json().get("totalSubscribers")
        except Exception:
            pass

    # Recent alert sends — gold_sent.json in the private queue Gist
    queue_gist = os.getenv("MAPC_QUEUE_GIST", "")
    if github_token and queue_gist:
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(f"https://api.github.com/gists/{queue_gist}", headers=gist_headers)
                if r.status_code == 200:
                    raw = r.json().get("files", {}).get("gold_sent.json", {}).get("content", "[]")
                    out["alert_history"] = json.loads(raw)[:10]
        except Exception:
            pass

    _GOLD_STATS_CACHE.update({"data": out, "ts": now})
    return out


@app.get("/api/gold-rates-stats", response_class=HTMLResponse)
def api_gold_rates_stats(request: Request):
    resp = templates.TemplateResponse(
        "_gold_rates_console.html",
        {"request": request, "gold": _gold_rates_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.get("/agent/gold_rates", response_class=HTMLResponse)
def page_gold_rates(request: Request):
    return _render(
        request,
        "_gold_rates_console.html",
        active_view="agent",
        active_agent="gold_rates",
        gold=_gold_rates_stats(),
    )


@app.get("/api/labs-stats", response_class=HTMLResponse)
def api_labs_stats(request: Request):
    """HTMX target for the labs_results_emailer overview console. Resend's
    list-emails API is the live source — we don't store anything locally."""
    resp = templates.TemplateResponse(
        "_labs_console.html",
        {"request": request, "labs": _labs_emailer_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.get("/agent/labs_results_emailer", response_class=HTMLResponse)
def page_labs_results_emailer(request: Request):
    return _render(
        request,
        "_labs_console.html",
        active_view="agent",
        active_agent="labs_results_emailer",
        labs=_labs_emailer_stats(),
    )


@app.get("/activity", response_class=HTMLResponse)
def page_activity(request: Request):
    feed = _ig_activity_feed(limit=80)
    return _render(
        request,
        "_activity.html",
        active_view="activity",
        feed=feed,
    )


@app.get("/artifacts", response_class=HTMLResponse)
def page_artifacts(request: Request):
    return _render(
        request,
        "_artifacts.html",
        active_view="artifacts",
    )


@app.get("/agent/{name}", response_class=HTMLResponse)
def page_agent_detail(request: Request, name: str):
    try:
        get_entry(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}")
    state = _agent_state(name)
    return _render(
        request,
        "_agent_detail.html",
        active_view="agent",
        active_agent=name,
        state=state,
    )


@app.get("/templates", response_class=HTMLResponse)
def page_templates(request: Request):
    all_templates = status_bus.list_templates()
    counts = status_bus.template_counts()
    # Group by (category, niche, kind) for display
    from collections import OrderedDict
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for t in all_templates:
        key = (t["category"], t["niche"], t["kind"])
        groups.setdefault(key, []).append(t)
    return _render(
        request,
        "_templates.html",
        active_view="templates",
        groups=groups,
        counts=counts,
    )


def _republish_template_set(category: str, niche: str, kind: str) -> tuple[bool, str]:
    """Recompile the active set for this (category, niche, kind) and push to Langfuse.

    The Langfuse prompt is a JSON array of all active template bodies. n8n
    fetches by name and picks one at random per incoming comment.
    """
    bodies = status_bus.active_template_bodies(category, niche, kind)
    name = f"auto_send.{category}.{niche}.{kind}"
    payload = json.dumps(bodies, ensure_ascii=False)
    ok, msg = lf.push_prompt(
        name=name,
        prompt=payload,
        labels=["production"],
        config={
            "category": category,
            "niche": niche,
            "kind": kind,
            "active_count": len(bodies),
            "schema": "json-array-of-template-bodies",
        },
    )
    return ok, msg


@app.post("/api/templates/add", response_class=HTMLResponse)
def api_template_add(
    background_tasks: BackgroundTasks,
    category: str = Form(...),
    niche: str = Form(...),
    kind: str = Form(...),
    body: str = Form(...),
    activate_immediately: bool = Form(False),
):
    """Manual template entry — paste your own copy, skip the generator agent."""
    if not body.strip():
        raise HTTPException(status_code=400, detail="Template body is required.")
    if kind not in ("public_reply", "dm"):
        raise HTTPException(status_code=400, detail="kind must be 'public_reply' or 'dm'.")
    if niche not in NICHES:
        raise HTTPException(status_code=400, detail=f"Unknown niche: {niche}")

    new_id = status_bus.add_template(
        category=category,
        niche=niche,
        kind=kind,
        body=body.strip(),
        voice_note="manually entered",
        status="active" if activate_immediately else "draft",
    )

    if activate_immediately:
        def _publish():
            ok, msg = _republish_template_set(category, niche, kind)
            status_bus.log_event(
                "template_generator",
                f"Manual template #{new_id} added + activated → Langfuse {msg}",
                level="success" if ok else "error",
            )
            lf.flush()
        background_tasks.add_task(_publish)

    return HTMLResponse(
        f'<span class="text-saffron-400 text-xs">'
        f'Added #{new_id}{" + published to Langfuse" if activate_immediately else " as draft"}'
        f'</span>'
    )


@app.post("/api/templates/{template_id}/activate", response_class=HTMLResponse)
def api_template_activate(template_id: int, background_tasks: BackgroundTasks):
    t = status_bus.get_template(template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    status_bus.update_template_status(template_id, "active")

    def _publish():
        ok, msg = _republish_template_set(t["category"], t["niche"], t["kind"])
        status_bus.log_event(
            "template_generator",
            f"Template #{template_id} activated → Langfuse {msg}",
            level="success" if ok else "error",
        )
        lf.flush()

    background_tasks.add_task(_publish)
    return HTMLResponse('<span class="text-saffron-400 text-xs">Activated · publishing</span>')


@app.post("/api/templates/{template_id}/deactivate", response_class=HTMLResponse)
def api_template_deactivate(template_id: int, background_tasks: BackgroundTasks):
    t = status_bus.get_template(template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    status_bus.update_template_status(template_id, "inactive")

    def _publish():
        ok, msg = _republish_template_set(t["category"], t["niche"], t["kind"])
        status_bus.log_event(
            "template_generator",
            f"Template #{template_id} deactivated → Langfuse {msg}",
            level="success" if ok else "error",
        )
        lf.flush()

    background_tasks.add_task(_publish)
    return HTMLResponse('<span class="text-zinc-500 text-xs">Deactivated · republishing</span>')


@app.post("/api/templates/{template_id}/delete", response_class=HTMLResponse)
def api_template_delete(template_id: int, background_tasks: BackgroundTasks):
    t = status_bus.get_template(template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    was_active = t["status"] == "active"
    status_bus.delete_template(template_id)

    if was_active:
        def _publish():
            ok, msg = _republish_template_set(t["category"], t["niche"], t["kind"])
            status_bus.log_event(
                "template_generator",
                f"Template #{template_id} deleted (was active) → Langfuse {msg}",
                level="success" if ok else "error",
            )
            lf.flush()
        background_tasks.add_task(_publish)

    return HTMLResponse('<span class="text-zinc-500 text-xs">Deleted</span>')


@app.get("/queue", response_class=HTMLResponse)
def page_queue(request: Request):
    pending = status_bus.list_drafts(status="pending", limit=200)
    counts = status_bus.draft_counts_by_status()
    return _render(
        request,
        "_queue.html",
        active_view="queue",
        drafts=pending,
        counts=counts,
    )


@app.get("/integrations", response_class=HTMLResponse)
def page_integrations(request: Request):
    return _render(
        request,
        "_integrations.html",
        active_view="integrations",
        states=integration_states(),
    )


@app.get("/prompts", response_class=HTMLResponse)
def page_prompts(request: Request):
    rows = []
    for p in pr.REGISTRY:
        try:
            _, src = pr.current(p)
        except Exception:
            src = "error"
        rows.append({"def": p, "source": src})
    return _render(
        request,
        "_prompts.html",
        active_view="prompts",
        rows=rows,
    )


@app.get("/prompts/{name:path}", response_class=HTMLResponse)
def page_prompt_detail(request: Request, name: str):
    try:
        p = pr.by_name(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {name}")
    text, src = pr.current(p)
    return _render(
        request,
        "_prompt_detail.html",
        active_view="prompts",
        p=p, text=text, source=src, length=len(text),
    )


@app.post("/api/prompts/{name:path}/push", response_class=HTMLResponse)
def api_push_prompt(name: str):
    try:
        p = pr.by_name(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {name}")
    ok, msg = pr.push(p)
    lf.flush()
    color = "text-emerald-400" if ok else "text-rose-400"
    icon = "✓" if ok else "✗"
    return HTMLResponse(f'<span class="{color}">{icon} {msg}</span>')


@app.post("/api/prompts/push-all", response_class=HTMLResponse)
def api_push_all_prompts():
    results = []
    for p in pr.REGISTRY:
        ok, msg = pr.push(p)
        results.append((ok, p.name, msg))
    lf.flush()
    ok_count = sum(1 for ok, _, _ in results if ok)
    fail_count = len(results) - ok_count
    color = "text-emerald-400" if fail_count == 0 else "text-amber-400"
    return HTMLResponse(
        f'<span class="{color}">pushed {ok_count}/{len(results)} prompts to Langfuse'
        + (f" · {fail_count} failed" if fail_count else "")
        + "</span>"
    )


# ---------- PARTIAL ROUTES ----------


@app.get("/api/sidebar", response_class=HTMLResponse)
def api_sidebar(request: Request):
    # active_agent comes from current URL on the client; we send it via the
    # template variable, but for HTMX polls we let the client decide via the
    # `.active` class on the link element. For first paint, we read the
    # Referer to highlight.
    resp = templates.TemplateResponse(
        "_sidebar_agents.html",
        {"request": request, "groups": _grouped_states(), "active_agent": ""},
    )
    # Sidebar agent list rarely changes; cache for 1 minute so HTMX `load`
    # triggers across page navigations return from browser cache.
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.get("/api/agent/{name}/pipeline", response_class=HTMLResponse)
def api_pipeline(request: Request, name: str):
    try:
        get_entry(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}")
    return templates.TemplateResponse(
        "_workflow_pipeline.html",
        {"request": request, "state": _agent_state(name)},
    )


@app.get("/api/agent/{name}/metrics", response_class=HTMLResponse)
def api_agent_metrics(request: Request, name: str):
    try:
        get_entry(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}")
    metrics = agent_metrics.metrics_for(name)
    return templates.TemplateResponse(
        "_agent_metrics.html",
        {"request": request, "metrics": metrics},
    )


@app.get("/api/agent/{name}/runs", response_class=HTMLResponse)
def api_agent_runs(request: Request, name: str):
    try:
        get_entry(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}")
    runs = status_bus.recent_runs(agent_name=name, limit=15)
    return templates.TemplateResponse(
        "_run_history.html",
        {"request": request, "runs": runs},
    )


@app.get("/api/agent/{name}/artifacts", response_class=HTMLResponse)
def api_agent_artifacts(request: Request, name: str):
    artifacts = [
        a for a in status_bus.recent_artifacts(limit=200) if a["agent_name"] == name
    ][:20]
    return templates.TemplateResponse(
        "_artifacts_list.html",
        {"request": request, "artifacts": artifacts},
    )


@app.get("/api/artifacts/partial", response_class=HTMLResponse)
def api_artifacts(request: Request, limit: int = 30):
    return templates.TemplateResponse(
        "_artifacts_list.html",
        {"request": request, "artifacts": status_bus.recent_artifacts(limit=limit)},
    )


@app.get("/api/events/stream")
async def api_events_stream(request: Request):
    async def gen() -> AsyncIterator[dict]:
        seed = status_bus.recent_events(limit=1)
        last_id = seed[0]["id"] if seed else 0
        while True:
            if await request.is_disconnected():
                break
            new_events = await asyncio.to_thread(
                status_bus.wait_for_new_events, last_id, 15.0
            )
            for ev in new_events:
                last_id = max(last_id, ev["id"])
                yield {
                    "event": "agent-event",
                    "data": json.dumps(
                        {
                            "agent": ev["agent_name"],
                            "level": ev["level"],
                            "message": ev["message"],
                            "ts": ev["ts"],
                        }
                    ),
                }

    return EventSourceResponse(gen())


@app.get("/api/artifacts/{artifact_id}/view", response_class=PlainTextResponse)
def api_view_artifact(artifact_id: int):
    artifacts = status_bus.recent_artifacts(limit=500)
    match = next((a for a in artifacts if a["id"] == artifact_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = Path(match["path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="Artifact file is gone")
    return PlainTextResponse(path.read_text(), media_type="text/markdown")


@app.get("/api/healthz")
def api_healthz():
    return JSONResponse({"ok": True, "langfuse": lf.get_status()})


# ---------- DRAFTS QUEUE (approve / reject / send via n8n) ----------


@app.get("/api/queue/partial", response_class=HTMLResponse)
def api_queue_partial(request: Request):
    pending = status_bus.list_drafts(status="pending", limit=200)
    return templates.TemplateResponse(
        "_queue_list.html",
        {"request": request, "drafts": pending},
    )


def _post_to_n8n_send_webhook(draft: dict) -> tuple[bool, str]:
    """Forward an approved draft to n8n for sending via Meta Graph API.

    n8n exposes a webhook (configured per-flow); we POST the draft payload
    signed with HMAC so n8n can verify it came from Indra.
    """
    url = os.getenv("N8N_SEND_WEBHOOK_URL", "").strip()
    if not url:
        return False, "N8N_SEND_WEBHOOK_URL not configured"
    payload = {
        "draft_id": draft["id"],
        "agent_name": draft["agent_name"],
        "kind": draft["kind"],                  # public_reply | dm | email
        "target_handle": draft["target_handle"],
        "body": draft["body"],
        "category": draft["category"],
        "source_text": draft["source_text"],
        "meta": json.loads(draft.get("meta_json") or "{}"),
    }
    body = json.dumps(payload).encode()
    signature, ts, nonce = _sign_payload(body)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Indra-Signature": signature,
                    "X-Indra-Timestamp": ts,
                    "X-Indra-Nonce": nonce,
                },
            )
        if 200 <= resp.status_code < 300:
            return True, f"n8n accepted (HTTP {resp.status_code})"
        return False, f"n8n returned HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"n8n call failed: {exc}"


@app.post("/api/drafts/{draft_id}/approve", response_class=HTMLResponse)
def api_approve_draft(draft_id: int, background_tasks: BackgroundTasks):
    draft = status_bus.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Draft is already {draft['status']}")

    # Mark as in-flight; the actual send is async.
    status_bus.update_draft_status(draft_id, "approved")

    def _send():
        ok, msg = _post_to_n8n_send_webhook(draft)
        if ok:
            status_bus.log_event(draft["agent_name"], f"Draft #{draft_id} sent to n8n for delivery", level="success")
            # Final 'sent' status set by n8n's send-confirmed webhook callback
        else:
            status_bus.update_draft_status(draft_id, "failed", error=msg)
            status_bus.log_event(draft["agent_name"], f"Draft #{draft_id} send failed: {msg}", level="error")

    background_tasks.add_task(_send)
    return HTMLResponse(
        f'<span class="text-saffron-400 text-xs">Approved · sent to n8n</span>'
    )


@app.post("/api/drafts/{draft_id}/reject", response_class=HTMLResponse)
def api_reject_draft(draft_id: int):
    draft = status_bus.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Draft is already {draft['status']}")
    status_bus.update_draft_status(draft_id, "rejected")
    return HTMLResponse(f'<span class="text-zinc-500 text-xs">Rejected</span>')


# ---------- INBOUND WEBHOOKS FROM N8N ----------


@app.post("/api/webhooks/n8n/new-comment")
async def webhook_new_comment(request: Request, background_tasks: BackgroundTasks):
    """n8n forwards a new IG comment here. We kick off comment_dm_responder.

    Expected JSON payload:
      {
        "post_context": "...",
        "dm_payload": "...",
        "comments": "from: @handle\\n<text>\\n---\\nfrom: @other\\n<text>"
      }
    """
    body = await request.body()
    _verify_hmac(request, body)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    post_context = payload.get("post_context", "")
    dm_payload = payload.get("dm_payload", "")
    comments = payload.get("comments", "")
    if not comments:
        raise HTTPException(status_code=400, detail="Missing comments")

    background_tasks.add_task(
        _run_in_background(
            CommentDMResponderAgent,
            task=f"n8n: comment batch ({post_context[:40]})",
            post_context=post_context,
            dm_payload=dm_payload,
            comments=comments,
        )
    )
    return JSONResponse({"accepted": True})


@app.post("/api/webhooks/n8n/send-confirmed")
async def webhook_send_confirmed(request: Request):
    """n8n calls this after successfully sending a public reply / DM via Meta.

    Expected JSON payload:
      {
        "draft_id": 42,
        "status": "sent" | "failed",
        "external_id": "ig_message_id_or_comment_id",
        "error": "optional error message"
      }
    """
    body = await request.body()
    _verify_hmac(request, body)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    draft_id = payload.get("draft_id")
    status = payload.get("status", "sent")
    if not draft_id:
        raise HTTPException(status_code=400, detail="Missing draft_id")

    status_bus.update_draft_status(
        int(draft_id),
        status=status,
        external_id=payload.get("external_id"),
        error=payload.get("error"),
    )
    status_bus.log_event(
        "comment_dm_responder",
        f"n8n confirmed draft #{draft_id} {status}",
        level="success" if status == "sent" else "warning",
    )
    return JSONResponse({"acknowledged": True})


# ---------- RUN ROUTES ----------


def _run_in_background(agent_class, **kwargs):
    def _go():
        try:
            agent_class().run(**kwargs)
        except Exception:
            pass
        finally:
            lf.flush()
    return _go


@app.post("/api/run/comment_dm_responder", response_class=HTMLResponse)
async def run_comment_dm_responder(
    background_tasks: BackgroundTasks,
    post_context: str = Form(""),
    dm_payload: str = Form(""),
    comments: str = Form(""),
):
    if not comments.strip():
        raise HTTPException(status_code=400, detail="Paste at least one comment (--- separated for multiple).")
    background_tasks.add_task(
        _run_in_background(
            CommentDMResponderAgent,
            task=f"Comment→Reply+DM batch ({(post_context or 'unspecified')[:40]})",
            post_context=post_context,
            dm_payload=dm_payload,
            comments=comments,
        )
    )
    return HTMLResponse('<div class="text-saffron-400 text-sm">Queued — watch the pipeline above.</div>')


# ---------- job_hunter dashboard surface ----------


def _latest_job_hunter_run() -> dict | None:
    """Return the most recent job_hunter artifact with parsed meta, or None."""
    for a in status_bus.recent_artifacts(limit=30):
        if a.get("agent_name") == "job_hunter":
            try:
                meta = json.loads(a.get("meta_json") or "{}")
            except json.JSONDecodeError:
                meta = {}
            return {**a, "meta": meta}
    return None


_ATS_KW_RE = re.compile(
    r"\*\*Top ATS keywords[^*]*\*\*\s*\n((?:\s*-\s+.+\n?)+)",
    re.IGNORECASE,
)


def _extract_ats_keywords(jd_md: str) -> list[str]:
    """Pull the bullet list under 'Top ATS keywords' out of jd.md."""
    if not jd_md:
        return []
    m = _ATS_KW_RE.search(jd_md)
    if not m:
        return []
    out: list[str] = []
    for line in m.group(1).splitlines():
        s = line.strip().lstrip("-").strip()
        if s:
            out.append(s.lstrip("`").rstrip("`"))
    return out[:12]


_LINK_RE = re.compile(r"\*\*Link:\*\*\s*(\S+)", re.IGNORECASE)


def _extract_job_url(jd_md: str) -> str:
    m = _LINK_RE.search(jd_md or "")
    return m.group(1).strip().strip("`<>") if m else ""


def _read_job_files(slug: str) -> dict:
    """Read per-job files for one slug from the CV-builder project."""
    if not slug or "/" in slug or ".." in slug:
        return {"slug": "", "_error": "invalid slug"}
    base = JOB_HUNTER_CV_DIR / "jobs" / slug
    if not base.exists():
        return {"slug": slug, "_error": f"folder not found: {base}"}

    def _read(name: str) -> str:
        p = base / name
        return p.read_text() if p.exists() else ""

    jd_md = _read("jd.md")
    cv_md = _read("tailored-cv.md")
    # Accept either new (cold-note.md) or legacy (referral-message.md)
    cold_md = _read("cold-note.md") or _read("referral-message.md")
    return {
        "slug": slug,
        "job_url": _extract_job_url(jd_md),
        "ats_keywords": _extract_ats_keywords(jd_md),
        "tailored_cv_md": cv_md,
        "tailored_cv_html": _render_brief_md(cv_md) if cv_md else "",
        "cold_note_md": cold_md,
        "cold_note_html": _render_brief_md(cold_md) if cold_md else "",
        "jd_md": jd_md,
        "jd_html": _render_brief_md(jd_md) if jd_md else "",
        "fit_report_md": _read("fit-report.md"),
        "fit_report_html": _render_brief_md(_read("fit-report.md")) if (base / "fit-report.md").exists() else "",
        "has_data_json": (base / "data.json").exists(),
        "folder_path": str(base),
    }


@app.get("/api/agent/job_hunter/latest", response_class=HTMLResponse)
def api_job_hunter_latest(request: Request, slug: str = ""):
    """HTML partial: latest run header + ranking table + selected-job detail."""
    run = _latest_job_hunter_run()
    if not run:
        return HTMLResponse(
            '<div class="card p-5 text-sm text-zinc-500">'
            'No runs yet. Use the form above to invoke Kaustubha, or wait for the '
            'next scheduled fire.'
            '</div>'
        )
    top_jobs = run["meta"].get("top_jobs", []) or []
    selected = slug or (top_jobs[0]["slug"] if top_jobs else "")
    selected_files = _read_job_files(selected) if selected else {}
    return templates.TemplateResponse(
        "_job_hunter_latest.html",
        {
            "request": request,
            "run": run,
            "top_jobs": top_jobs,
            "selected_files": selected_files,
            "selected_slug": selected,
        },
    )


@app.post("/api/run/job_hunter", response_class=HTMLResponse)
async def run_job_hunter(
    background_tasks: BackgroundTasks,
    target_role: str = Form(""),
    location: str = Form(""),
    keywords: str = Form(""),
    region: str = Form("india"),
    scrape_limit: int = Form(15),
    tailor_limit: int = Form(5),
):
    if not target_role.strip():
        raise HTTPException(status_code=400, detail="target_role is required (e.g. 'Staff PM').")
    if region not in ("india", "europe"):
        raise HTTPException(status_code=400, detail="region must be 'india' or 'europe'.")
    if not os.getenv("APIFY_TOKEN", "").strip():
        return HTMLResponse(
            '<div class="text-rose-400 text-sm">APIFY_TOKEN not set in .env. '
            'Add it (free tier at apify.com) before running this agent.</div>',
            status_code=200,
        )
    background_tasks.add_task(
        _run_in_background(
            JobHunterAgent,
            task=f"job_hunter · {target_role[:40]} · {(location or 'any')[:20]}",
            target_role=target_role.strip(),
            location=location.strip(),
            keywords=keywords.strip(),
            region=region,
            scrape_limit=scrape_limit,
            tailor_limit=tailor_limit,
        )
    )
    return HTMLResponse('<div class="text-saffron-400 text-sm">Queued — Apify scrape can take 30-90s; watch the pipeline above.</div>')


# ---------- Startup lookup (funded India startups × senior PM roles) ----------


def _latest_startup_lookup_run() -> dict | None:
    """Most recent startup_lookup artifact + its markdown body."""
    arts = [a for a in status_bus.recent_artifacts(limit=200)
            if a.get("kind") == "startup_lookup_run"]
    if not arts:
        return None
    art = arts[0]
    body = ""
    try:
        p = Path(art["path"])
        if p.exists():
            body = p.read_text()
    except Exception:
        pass
    try:
        meta = json.loads(art.get("meta_json") or "{}")
    except json.JSONDecodeError:
        meta = {}
    return {"artifact": art, "meta": meta, "body": body}


@app.get("/api/agent/startup_lookup/latest", response_class=HTMLResponse)
def api_startup_lookup_latest(request: Request):
    """HTML partial: latest startup_lookup run — summary + rendered markdown."""
    run = _latest_startup_lookup_run()
    if not run:
        return HTMLResponse(
            '<div class="card p-5 text-sm text-zinc-500">'
            'No runs yet. Use the form above to scan, or run '
            '<span class="font-mono text-saffron-300">python -m agents.startup_lookup</span>.'
            '</div>'
        )
    return templates.TemplateResponse(
        "_startup_lookup_latest.html",
        {"request": request, "run": run, "meta": run["meta"]},
    )


@app.post("/api/run/startup_lookup", response_class=HTMLResponse)
async def run_startup_lookup(
    background_tasks: BackgroundTasks,
    window_days: int = Form(14),
    stages: list[str] = Form(default=["pre-seed", "seed", "series_a", "series_b", "series_c"]),
    company_cap: int = Form(10),
    jobs_count: int = Form(40),
    location: str = Form("India"),
):
    if not os.getenv("APIFY_TOKEN", "").strip():
        return HTMLResponse(
            '<div class="text-rose-400 text-sm">APIFY_TOKEN not set in .env. '
            'Add it (free tier at apify.com) before running this agent.</div>',
            status_code=200,
        )
    window_days = max(1, min(int(window_days), 90))
    company_cap = max(1, min(int(company_cap), 25))
    jobs_count = max(5, min(int(jobs_count), 100))
    valid = [s for s in stages if s in ("pre-seed", "seed", "series_a", "series_b", "series_c")]
    if not valid:
        valid = ["pre-seed", "seed", "series_a", "series_b", "series_c"]
    background_tasks.add_task(
        _run_in_background(
            StartupLookupAgent,
            task=f"startup_lookup · {window_days}d · {location[:20]}",
            window_days=window_days,
            stages=tuple(valid),
            company_cap=company_cap,
            jobs_count=jobs_count,
            location=location.strip() or "India",
        )
    )
    return HTMLResponse('<div class="text-saffron-400 text-sm">Queued — RSS + one Apify search can take 30-90s; watch the pipeline above, then the latest panel refreshes.</div>')


# ---------- Competitor watcher (static Instagram creator benchmark) ----------

_IG_SCRAPER_DIR = BASE_DIR.parent / "outputs" / "instagram_scraper"


@app.get("/competitor-dashboard")
def page_competitor_dashboard():
    """Serve the standalone creator-benchmark dashboard HTML in a new tab."""
    f = _IG_SCRAPER_DIR / "creator_dashboard.html"
    if not f.exists():
        return HTMLResponse(
            "<p style='font-family:sans-serif;padding:2rem'>creator_dashboard.html not found "
            f"under {_IG_SCRAPER_DIR}.</p>",
            status_code=404,
        )
    return FileResponse(str(f), media_type="text/html")


@app.get("/api/agent/competitor_watcher/latest", response_class=HTMLResponse)
def api_competitor_watcher_latest(request: Request):
    """Partial: link to the visual dashboard + inline comparison + persona file list."""
    comparison = ""
    cmp_path = _IG_SCRAPER_DIR / "creator_comparison.md"
    try:
        if cmp_path.exists():
            comparison = cmp_path.read_text()
    except Exception:
        pass
    personas = []
    pdir = _IG_SCRAPER_DIR / "personas"
    if pdir.exists():
        personas = sorted(p.name for p in pdir.glob("*.md"))
    return templates.TemplateResponse(
        "_competitor_watcher.html",
        {
            "request": request,
            "comparison": comparison,
            "personas": personas,
            "dir": str(_IG_SCRAPER_DIR),
            "has_dashboard": (_IG_SCRAPER_DIR / "creator_dashboard.html").exists(),
        },
    )


# ---------- Email signups (double-opt-in) ----------


def _doi_email_html(confirm_url: str, email: str) -> str:
    return f"""\
<!doctype html>
<html><body style="font-family: -apple-system, system-ui, sans-serif; max-width: 540px; margin: 24px auto; color: #222; line-height: 1.55;">
  <h2 style="margin-bottom: 8px;">Confirm your email</h2>
  <p>Hi — you (or someone using <b>{email}</b>) asked to subscribe.</p>
  <p>Click the button below to confirm. If this wasn't you, ignore this email and nothing happens.</p>
  <p style="margin: 28px 0;">
    <a href="{confirm_url}"
       style="background:#d4a017;color:#0a0e1f;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;">
      Confirm subscription
    </a>
  </p>
  <p style="font-size:12px;color:#666;">Or paste this link into your browser:<br>
    <span style="font-family:monospace;">{confirm_url}</span>
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="font-size:11px;color:#999;">You're receiving this because someone entered this address on a sign-up form. This is a one-time confirmation; we won't email again unless you confirm.</p>
</body></html>
"""


def _doi_email_text(confirm_url: str, email: str) -> str:
    return (
        f"Hi — you (or someone using {email}) asked to subscribe.\n\n"
        f"Confirm by clicking this link:\n{confirm_url}\n\n"
        "If this wasn't you, ignore this email.\n"
    )


@app.get("/signup-test", response_class=HTMLResponse)
def page_signup_test(request: Request):
    """Tiny standalone form for testing the signup flow end-to-end."""
    return _render(request, "_signup_test.html", active_view="")


@app.post("/api/email/signup")
def api_email_signup(request: Request, email: str = Form(...), source: str = Form("")):
    """Accept a signup. Creates pending row, fires DOI email, returns JSON.
    Idempotent on email — re-submitting a pending email re-sends; already-confirmed
    just returns OK without spamming."""
    email_clean = (email or "").strip().lower()
    if not email_signups.is_valid_email(email_clean):
        return JSONResponse({"ok": False, "error": "Invalid email address"}, status_code=400)

    try:
        token, is_new, status = email_signups.create_or_get_pending(
            email_clean, source=(source or "").strip()[:80]
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if status == "confirmed":
        return JSONResponse({
            "ok": True,
            "status": "already_confirmed",
            "message": "This email is already confirmed — thanks!",
        })

    # Idempotency: only send the DOI email on the FIRST POST for this email.
    # Re-submitting (double-clicks, page reloads, abuse) returns "already sent"
    # without spamming the recipient. To wipe state and re-test, delete the row
    # from the email_signups table or use a new address.
    if not is_new and status == "pending":
        return JSONResponse({
            "ok": True,
            "status": "already_pending",
            "message": "We've already sent a confirmation to this address. Check your inbox or spam folder.",
        })

    # is_new=True, OR status=='failed_send' (retry path). Send the DOI email.
    base = os.getenv("DASHBOARD_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    confirm_url = f"{base}/confirm/{token}"

    ok, err = resend_client.send(
        to=email_clean,
        subject="Confirm your email",
        html=_doi_email_html(confirm_url, email_clean),
        text=_doi_email_text(confirm_url, email_clean),
    )
    if not ok:
        email_signups.mark_send_failed(email_clean, err or "send returned False")
        # If we're stuck in sandbox and recipient isn't the account owner, Resend
        # returns 403 with a clear message — surface that to the form UI.
        return JSONResponse(
            {"ok": False, "error": err or "Send failed", "status": "failed_send"},
            status_code=502,
        )

    return JSONResponse({
        "ok": True,
        "status": "pending",
        "message": "Check your inbox to confirm.",
    })


@app.get("/confirm/{token}", response_class=HTMLResponse)
def page_confirm(request: Request, token: str):
    row = email_signups.confirm(token)
    return _render(
        request,
        "_signup_confirmed.html",
        active_view="",
        row=row,
    )


_OWNER_EMAILS = {
    "manikumarjami@gmail.com", "manikumarjami1@gmail.com",
    "manikumarjami007@gmail.com", "iamemkayjami@gmail.com",
    "ratelimit_test_qa@example.com",
}

def _to_ist(ts_raw: str) -> str:
    """Convert UTC timestamp string to IST (UTC+5:30) formatted string."""
    import datetime as _dt
    try:
        ts = ts_raw[:19].replace(" ", "T")
        utc = _dt.datetime.fromisoformat(ts)
        ist = utc + _dt.timedelta(hours=5, minutes=30)
        return ist.strftime("%-d %b %Y, %-I:%M %p IST")
    except Exception:
        return ts_raw[:16] if ts_raw else "—"


def _unified_subscribers() -> list[dict]:
    """Merge Labs (SQLite) + MAPC (Resend) into one sorted subscriber list.
    Deduplicates by email — MAPC entry wins if same email exists in both.
    Filters owner/test emails. All timestamps shown in IST."""
    import datetime as _dt

    # ── Labs subscribers (SQLite) ────────────────────────────────────────
    labs_rows = email_signups.list_recent(limit=200)
    by_email: dict[str, dict] = {}
    for r in labs_rows:
        em = (r.get("email") or "").strip().lower()
        if not em or em in _OWNER_EMAILS:
            continue
        src = (r.get("source") or "labs").strip()
        channel = "Instagram" if "instagram" in src.lower() or "ig" in src.lower() else "Labs"
        by_email[em] = {
            "email": em,
            "status": r.get("status") or "?",
            "source": src,
            "channel": channel,
            "created_at": _to_ist(r.get("created_at") or ""),
            "confirmed_at": _to_ist(r.get("confirmed_at") or ""),
            "_sort_ts": (r.get("created_at") or ""),
        }

    # ── MAPC subscribers (Resend) ────────────────────────────────────────
    resend_key = os.getenv("RESEND_API_KEY", "").strip()
    if resend_key:
        try:
            with httpx.Client(timeout=6.0) as c:
                r = c.get("https://api.resend.com/emails?limit=100",
                           headers={"Authorization": f"Bearer {resend_key}"})
                if r.status_code == 200:
                    emails_raw = r.json().get("data", []) or []
                    mapc_emails = [
                        e for e in emails_raw
                        if any(kw in (e.get("subject") or "").lower()
                               for kw in ["exam prep", "material you requested",
                                          "study guide", "mapc"])
                    ]
                    seen: set = set()
                    for e in mapc_emails:
                        em = (e.get("to") or [""])[0].strip().lower()
                        if not em or em in _OWNER_EMAILS or em in seen:
                            continue
                        seen.add(em)
                        subj = (e.get("subject") or "").lower()
                        if "clinical" in subj:       spec = "Clinical"
                        elif "industrial" in subj:   spec = "I&O Psychology"
                        elif "1st year" in subj:     spec = "1st Year"
                        else:                        spec = "Counselling"
                        ts_raw = (e.get("created_at") or "")[:19]
                        by_email[em] = {
                            "email": em,
                            "status": e.get("last_event") or "?",
                            "source": f"MAPC · {spec}",
                            "channel": "MAPC",
                            "created_at": _to_ist(ts_raw),
                            "confirmed_at": _to_ist(ts_raw),
                            "_sort_ts": ts_raw,
                        }
        except Exception:
            pass

    # Sort newest first by raw UTC timestamp
    return sorted(by_email.values(), key=lambda r: r.get("_sort_ts") or "", reverse=True)


def _unified_counts(rows: list[dict]) -> dict:
    from collections import Counter
    channels = Counter(r["channel"] for r in rows)
    statuses = Counter(r["status"] for r in rows)
    return {
        "total": len(rows),
        "confirmed": statuses.get("confirmed", 0) + statuses.get("delivered", 0) + statuses.get("sent", 0),
        "pending": statuses.get("pending", 0),
        "failed_send": statuses.get("failed_send", 0) + statuses.get("bounced", 0),
        "by_channel": dict(channels),
    }


@app.get("/signups", response_class=HTMLResponse)
def page_signups(request: Request):
    rows = _unified_subscribers()
    return _render(
        request,
        "_signups_admin.html",
        active_view="signups",
        signups=rows,
        counts=_unified_counts(rows),
    )


# ---------- Labs assessment results emailer ----------


_TEST_LABELS = {
    "big-five":         "Big Five personality",
    "attachment-style": "Attachment Style (ECR-S)",
    "career-interest":  "RIASEC career interest",
    "cognitive":        "Cognitive Snapshot",
    "student-stress":   "Student Stress Map (PSS-10)",
    "wellbeing":        "Wellbeing screener (PHQ-9 + GAD-7)",
}


@app.post("/api/labs/results-email")
async def api_labs_results_email(request: Request):
    """Receive a labs assessment completion, send the visitor a results email,
    and (optionally) subscribe them to the newsletter.

    Body (JSON): {
      email:              required, the visitor's email
      test_id:            required, one of KNOWN_TESTS (big-five, ..., wellbeing)
      run_id:             required, a client-generated unique ID for this run (idempotency key)
      name:               optional, used to personalize the greeting
      summary_text:       required, human-readable scores summary built client-side
      results_url:        required, the labs URL to view the full report (with the ?r= fragment)
      newsletter_opt_in:  optional bool, default false. If true, also subscribes to the newsletter.
    }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    test_id = (body.get("test_id") or "").strip()
    run_id = (body.get("run_id") or "").strip()
    name = (body.get("name") or "").strip() or None
    summary_text = (body.get("summary_text") or "").strip()
    results_url = (body.get("results_url") or "").strip()
    newsletter_opt_in = bool(body.get("newsletter_opt_in", False))

    if not email_signups.is_valid_email(email):
        return JSONResponse({"ok": False, "error": "Invalid email address"}, status_code=400)
    if test_id not in assessment_emails.KNOWN_TESTS:
        return JSONResponse(
            {"ok": False, "error": f"Unknown test_id. Allowed: {sorted(assessment_emails.KNOWN_TESTS)}"},
            status_code=400,
        )
    if not run_id or len(run_id) > 120:
        return JSONResponse({"ok": False, "error": "Missing or oversized run_id"}, status_code=400)
    if not summary_text:
        return JSONResponse({"ok": False, "error": "Missing summary_text"}, status_code=400)
    if not results_url or not results_url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "Missing or invalid results_url"}, status_code=400)

    # Idempotency: if we already have a sent row for (email, test, run), short-circuit.
    # We still return an unlock_token so the labs UI reveals the deep report on
    # re-submission (e.g. browser back-button-then-submit-again).
    existing = assessment_emails.find_existing(email, test_id, run_id)
    if existing and existing["status"] == "sent":
        return JSONResponse({
            "ok": True,
            "status": "already_sent",
            "message": "Results were already emailed for this assessment run.",
            "unlock_token": _make_unlock_token(email, test_id, run_id),
        })

    # Handle the newsletter side BEFORE sending, so the unsubscribe link is valid
    # in the email itself when newsletter_opt_in is true.
    unsubscribe_url = ""
    if newsletter_opt_in:
        try:
            token = email_signups.confirm_via_labs(email, source=f"labs:{test_id}")
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        base = os.getenv("DASHBOARD_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
        unsubscribe_url = f"{base}/unsubscribe/{token}"

    # Record pending row (or reuse the existing pending/failed one) before the send.
    if existing:
        row_id = existing["id"]
    else:
        row_id = assessment_emails.record_pending(
            email=email, test_id=test_id, run_id=run_id, name=name,
            results_url=results_url, summary_text=summary_text,
            newsletter_opt=newsletter_opt_in,
        )

    greeting = f"Hey {name}," if name else "Hey,"
    test_label = _TEST_LABELS.get(test_id, test_id)
    is_wellbeing = test_id in assessment_emails.WELLBEING_TESTS
    subject = f"Your {test_label} results"

    html = templates.get_template("emails/results.html").render(
        test_id=test_id,
        test_label=test_label,
        greeting=greeting,
        summary_text=summary_text,
        results_url=results_url,
        newsletter_opt_in=newsletter_opt_in,
        unsubscribe_url=unsubscribe_url,
        wellbeing_disclaimer=is_wellbeing,
    )
    text_fallback = (
        f"{greeting}\n\nYour {test_label} results are ready. View the full report:\n"
        f"{results_url}\n\nSnapshot: {summary_text}\n"
    )

    ok, err = resend_client.send(
        to=email, subject=subject, html=html, text=text_fallback,
    )
    if not ok:
        assessment_emails.mark_failed(row_id, err or "unknown send error")
        return JSONResponse(
            {"ok": False, "error": err or "Send failed", "status": "failed"},
            status_code=502,
        )
    assessment_emails.mark_sent(row_id)
    return JSONResponse({
        "ok": True,
        "status": "sent",
        "message": "Results email sent.",
        "newsletter_subscribed": newsletter_opt_in,
        "unlock_token": _make_unlock_token(email, test_id, run_id),
    })


def _make_unlock_token(email: str, test_id: str, run_id: str) -> str:
    """Opaque marker the labs UI stores in localStorage to unlock the deep
    report. Not signed — labs treats presence + length as the gate, not the
    value (matches the existing unlock.js contract). HMAC over the triple so
    the token is stable for a given submission (idempotent on retry)."""
    import hashlib, hmac, base64
    secret = os.getenv("ASSESS_SECRET", "indra-labs-local-dev-secret").encode()
    msg = f"{email}|{test_id}|{run_id}".encode()
    digest = hmac.new(secret, msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def page_unsubscribe(request: Request, token: str):
    row = email_signups.unsubscribe_by_token(token)
    return _render(request, "_unsubscribed.html", active_view="", row=row)


@app.post("/api/mapc-retry")
def api_mapc_retry(request: Request):
    """Process the MAPC pending email queue.
    Called manually or by a scheduled task at 00:05 UTC when Resend quota resets.
    Reads mapc_pending.json from GitHub, sends queued emails, clears processed entries."""
    from agents.mapc_delivery.retry import process_queue
    try:
        result = process_queue(dry_run=False)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=500)


@app.get("/api/mapc-queue-status")
def api_mapc_queue_status(request: Request):
    """Show how many emails are currently queued for retry."""
    from agents.mapc_delivery.retry import _fetch_pending
    try:
        pending, _ = _fetch_pending()
        return JSONResponse({
            "queued": len(pending),
            "emails": [{"email": e["email"], "spec": e.get("specialisation"), "queued_at": e.get("queued_at")} for e in pending]
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200], "queued": 0}, status_code=500)


def run() -> None:
    import uvicorn

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8765"))
    uvicorn.run("dashboard.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
