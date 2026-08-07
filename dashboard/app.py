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
import csv
import html
import hashlib
import hmac
import ipaddress
import io
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

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
from agents.prompt_analyst.agent import PromptAnalystAgent
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

# CORS — allow your site origin (set SITE_ORIGIN in .env) plus localhost for dev.
# The labs site POSTs to /api/labs/results-email from the browser, so it needs an
# Access-Control-Allow-Origin header. We allow specific origins, NOT "*", because
# /api endpoints touch user-owned state (signups, sends).
_site_origin = os.getenv("SITE_ORIGIN", "").strip().rstrip("/")
_LABS_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    *([_site_origin, f"https://www.{_site_origin.removeprefix('https://')}"] if _site_origin else []),
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
    "prompt_analyst": PromptAnalystAgent,
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
PUBLIC_EMAIL_RATE_LIMIT = 5                # outbound-email attempts per IP/minute
_rate_buckets: dict[str, list[float]] = defaultdict(list)

# Fully public paths. Keep this list deliberately small: everything else fails
# closed when dashboard credentials are missing.
_PUBLIC_PATHS = {
    "/robots.txt",
    "/api/healthz",
    "/api/email/signup",
    "/api/labs/results-email",
}
_PUBLIC_PREFIXES = (
    "/static/",
    "/confirm/",
    "/unsubscribe/",
)
_WEBHOOK_PREFIX = "/api/webhooks/"  # authenticated separately with HMAC
_PUBLIC_EMAIL_PATHS = {"/api/email/signup", "/api/labs/results-email"}


def _dashboard_creds() -> tuple[str, str] | None:
    user = os.getenv("INDRA_DASHBOARD_USER", "").strip()
    pwd = os.getenv("INDRA_DASHBOARD_PASSWORD", "").strip()
    if not user or len(pwd) < 16:
        return None
    return user, pwd


def _client_ip(request: Request) -> str:
    """Return a rate-limit identity without blindly trusting proxy headers."""
    peer = request.client.host if request.client else "unknown"
    trusted = os.getenv("INDRA_TRUSTED_PROXIES", "").strip()
    if not trusted or peer == "unknown":
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer)
        networks = [
            ipaddress.ip_network(item.strip(), strict=False)
            for item in trusted.split(",") if item.strip()
        ]
    except ValueError:
        return peer
    if not any(peer_ip in network for network in networks):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(forwarded)) if forwarded else peer
    except ValueError:
        return peer


def _rate_limited(bucket_name: str, client_ip: str, maximum: int) -> bool:
    now = time.time()
    bucket = _rate_buckets[f"{bucket_name}:{client_ip}"]
    cutoff = now - RATE_LIMIT_WINDOW
    bucket[:] = [timestamp for timestamp in bucket if timestamp > cutoff]
    if len(bucket) >= maximum:
        return True
    bucket.append(now)
    return False


def _configured_public_base() -> str | None:
    value = os.getenv("DASHBOARD_PUBLIC_URL", "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None
    return value


def _allowed_results_url(value: str) -> bool:
    """Prevent the public email endpoint being used to send phishing links."""
    try:
        candidate = urlsplit(value)
    except ValueError:
        return False
    if candidate.scheme not in {"http", "https"} or not candidate.netloc:
        return False
    if candidate.username or candidate.password:
        return False
    if candidate.scheme == "http" and candidate.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    allowed_origins = {
        origin
        for raw in (os.getenv("SITE_ORIGIN", ""), os.getenv("SITE_BASE_URL", ""))
        if (origin := raw.strip().rstrip("/"))
    }
    for origin in allowed_origins:
        try:
            expected = urlsplit(origin)
        except ValueError:
            continue
        if (candidate.scheme, candidate.netloc) == (expected.scheme, expected.netloc):
            return True
    return False


def _assess_secret() -> bytes | None:
    secret = os.getenv("ASSESS_SECRET", "").strip()
    return secret.encode() if len(secret) >= 32 else None


def _allowed_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
    hosts.update(
        item.strip().lower()
        for item in os.getenv("INDRA_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    )
    for raw in (
        os.getenv("DASHBOARD_PUBLIC_URL", ""),
        os.getenv("SITE_ORIGIN", ""),
        os.getenv("SITE_BASE_URL", ""),
    ):
        try:
            if hostname := urlsplit(raw.strip()).hostname:
                hosts.add(hostname.lower())
        except ValueError:
            continue
    return hosts


def _valid_host_header(raw_host: str) -> bool:
    """Validate Host without using Starlette's reconstructed request URL."""
    if not raw_host or any(char in raw_host for char in "/\\\r\n\t"):
        return False
    try:
        parsed = urlsplit(f"//{raw_host}")
        hostname = parsed.hostname
    except ValueError:
        return False
    if not hostname or parsed.username or parsed.password:
        return False
    return hostname.lower() in _allowed_hosts()


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Combined security layer: Basic Auth + size limit + rate limit + noindex."""
    # Use the ASGI path, never request.url.path: vulnerable Starlette releases
    # can reconstruct request.url from a malicious Host/path combination.
    path = request.scope.get("path", "")
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        return JSONResponse({"detail": "Invalid request path"}, status_code=400)
    if not _valid_host_header(request.headers.get("host", "")):
        return JSONResponse({"detail": "Invalid Host header"}, status_code=400)

    # 1) Request size limit (defense against payload abuse)
    content_length = request.headers.get("content-length")
    try:
        oversized = bool(content_length) and int(content_length) > REQUEST_MAX_BYTES
    except ValueError:
        return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
    if oversized:
        return JSONResponse(
            {"detail": "Request body too large"},
            status_code=413,
            headers={"X-Robots-Tag": "noindex, nofollow"},
        )

    public_request = path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES)
    webhook_request = path.startswith(_WEBHOOK_PREFIX)

    # 2) HTTP Basic Auth — fail closed for every administrative surface.
    # Public browser flows and HMAC-authenticated webhooks are explicit exceptions.
    creds = _dashboard_creds()
    if not public_request and not webhook_request:
        if creds is None:
            return JSONResponse(
                {"detail": "Dashboard authentication is not configured"},
                status_code=503,
            )
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

    # Reject browser cross-site state changes on authenticated admin routes.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not public_request and not webhook_request:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
        origin = request.headers.get("origin", "").strip().rstrip("/")
        if origin:
            allowed_admin_origins = {str(request.base_url).rstrip("/")}
            if configured_base := _configured_public_base():
                allowed_admin_origins.add(configured_base)
            if origin not in allowed_admin_origins:
                return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)

    # 3) Rate limit expensive agent runs and public outbound-email routes.
    client_ip = _client_ip(request)
    if path.startswith("/api/run/"):
        if _rate_limited("agent-run", client_ip, RATE_LIMIT_MAX_RUNS):
            return JSONResponse(
                {"detail": f"Rate limit: max {RATE_LIMIT_MAX_RUNS} runs per {RATE_LIMIT_WINDOW}s"},
                status_code=429,
                headers={"Retry-After": str(RATE_LIMIT_WINDOW), "X-Robots-Tag": "noindex, nofollow"},
            )
    if request.method == "POST" and path in _PUBLIC_EMAIL_PATHS:
        if _rate_limited("public-email", client_ip, PUBLIC_EMAIL_RATE_LIMIT):
            return JSONResponse(
                {"detail": f"Rate limit: max {PUBLIC_EMAIL_RATE_LIMIT} email requests per {RATE_LIMIT_WINDOW}s"},
                status_code=429,
                headers={"Retry-After": str(RATE_LIMIT_WINDOW)},
            )

    # Enforce the body cap even for chunked requests, for which Content-Length
    # is absent. Public endpoints never accept multipart file uploads.
    if request.method in {"POST", "PUT", "PATCH"}:
        body = await request.body()
        if len(body) > REQUEST_MAX_BYTES:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if public_request and media_type == "multipart/form-data":
            return JSONResponse({"detail": "Multipart requests are not accepted"}, status_code=415)
        if media_type == "application/x-www-form-urlencoded" and body.count(b"&") >= 100:
            return JSONResponse({"detail": "Too many form fields"}, status_code=413)

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
    return secret if len(secret) >= 32 else ""


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
            _digital_delivery_stats()
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
        "integration_states": integration_states(),
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
                            "link": f"{os.getenv('N8N_API_BASE_URL','').replace('/api/v1','')}/workflow/{wf_id}/executions/{e['id']}",
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
    """Render Markdown and remove raw/scriptable HTML before marking it safe."""
    try:
        import bleach
        import markdown as _md
        rendered = _md.markdown(md, extensions=["fenced_code", "tables", "sane_lists", "nl2br"])
        return bleach.clean(
            rendered,
            tags={
                "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3",
                "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "strong",
                "table", "tbody", "td", "th", "thead", "tr", "ul",
            },
            attributes={"a": ["href", "title"]},
            protocols={"http", "https", "mailto"},
            strip=True,
            strip_comments=True,
        )
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

_DELIVERY_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_MAPC_STATS_CACHE = _DELIVERY_STATS_CACHE   # backward compat alias
_MAPC_STATS_TTL = 120.0  # 2 min — paginating Resend is ~3-5s

_GOLD_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_GOLD_STATS_TTL = 60.0

_PROMPT_ANALYST_CACHE: dict = {"ts": 0.0, "data": None}
_PROMPT_ANALYST_TTL = 300.0  # 5 min — JSONL scan takes ~1-2s on cold start

_MBT_STATS_CACHE: dict = {"ts": 0.0, "data": None}
_MBT_STATS_TTL = 300.0  # 5 min — the n8n discovery workflow only writes once a day at 9am


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
        "domain_name": os.getenv("LABS_DOMAIN", ""),
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


def _digital_delivery_stats() -> dict:
    """Query Resend for MAPC guide delivery emails. Cached 60s. No local storage."""
    import datetime as _dt
    now = time.time()
    if _MAPC_STATS_CACHE["data"] and (now - _MAPC_STATS_CACHE["ts"]) < _MAPC_STATS_TTL:
        return _MAPC_STATS_CACHE["data"]

    out: dict = {
        "configured": False,
        "today_count": 0,
        "week_count": 0,
        "month_count": 0,
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

    _raw_test = os.getenv("DELIVERY_TEST_EMAILS", "ratelimit_test_qa@example.com")
    _TEST_EMAILS = {e.strip().lower() for e in _raw_test.split(",") if e.strip()}
    _IST = _dt.timedelta(hours=5, minutes=30)

    def _to_ist(ts_raw: str) -> str:
        try:
            dt_utc = _dt.datetime.fromisoformat(ts_raw[:19].replace(" ", "T"))
            return (dt_utc + _IST).strftime("%-d %b %Y, %I:%M %p IST")
        except Exception:
            return ts_raw

    _MAPC_SUBJECTS = ("mapc exam prep", "exam prep ·", "study guides are ready",
                       "material you requested")

    SPEC_LABELS = {
        "counselling": "Counselling", "clinical": "Clinical",
        "io": "I&O Psychology", "yr1": "1st Year",
    }

    def _mask_email(em: str) -> str:
        local, _, domain = em.partition("@")
        if len(local) <= 2:
            masked = local[0] + "***"
        else:
            masked = local[:2] + "***" + local[-1:]
        return f"{masked}@{domain}"

    try:
        seen: dict = {}  # email -> record

        # ── Primary: Gist (mapc_all_sent.json — synced full history) ──
        github_token = os.getenv("GITHUB_TOKEN", "")
        gist_id = os.getenv("MAPC_QUEUE_GIST", "")
        if github_token and gist_id:
            with httpx.Client(timeout=10.0) as c:
                gr = c.get(f"https://api.github.com/gists/{gist_id}",
                           headers={"Authorization": f"token {github_token}",
                                    "Accept": "application/vnd.github.v3+json"})
                if gr.status_code == 200:
                    raw = gr.json().get("files", {}).get("mapc_all_sent.json", {}).get("content", "[]")
                    for s in json.loads(raw):
                        em = (s.get("email") or "").strip().lower()
                        if em and em not in _TEST_EMAILS:
                            seen[em] = {
                                "email": em,
                                "sent_at": s.get("sent_at", ""),
                                "status": s.get("status", "delivered"),
                                "course": SPEC_LABELS.get(s.get("specialisation", ""), s.get("specLabel", "MAPC")),
                                "via": (s.get("via") or "Resend").capitalize(),
                            }

        # ── Supplement: first Resend page picks up brand-new sends ──
        with httpx.Client(timeout=10.0) as c:
            r = c.get("https://api.resend.com/emails?limit=100", headers=HDR)
            if r.status_code == 200:
                for e in (r.json().get("data") or []):
                    subj = (e.get("subject") or "").lower()
                    if not any(kw in subj for kw in _MAPC_SUBJECTS):
                        continue
                    for t in (e.get("to") or []):
                        em = t.strip().lower()
                        if em and em not in _TEST_EMAILS and em not in seen:
                            sl = (e.get("subject") or "").lower()
                            if "clinical" in sl: course = "Clinical"
                            elif "counselling" in sl: course = "Counselling"
                            elif "i&o" in sl: course = "I&O Psychology"
                            elif "1st year" in sl: course = "1st Year"
                            else: course = "MAPC"
                            seen[em] = {
                                "email": em,
                                "sent_at": (e.get("created_at") or "")[:19].replace(" ", "T"),
                                "status": e.get("last_event") or "delivered",
                                "course": course,
                                "via": "Resend",
                            }

        # ── Sort by sent_at descending ──
        sorted_subs = sorted(seen.values(), key=lambda s: s.get("sent_at") or "", reverse=True)

        today_utc = _dt.datetime.utcnow().date()
        week_ago = today_utc - _dt.timedelta(days=6)
        month_ago = today_utc - _dt.timedelta(days=29)
        for s in sorted_subs:
            ts_str = s.get("sent_at", "")
            try:
                dt = _dt.datetime.fromisoformat(ts_str[:19].replace(" ", "T"))
                dt_date = dt.date()
                if dt_date == today_utc:
                    out["today_count"] += 1
                if dt_date >= week_ago:
                    out["week_count"] += 1
                if dt_date >= month_ago:
                    out["month_count"] += 1
            except Exception:
                pass
            status = s.get("status", "delivered")
            out["by_status"][status] = out["by_status"].get(status, 0) + 1

        out["total_visible"] = len(sorted_subs)

        for s in sorted_subs[:150]:
            out["recent"].append({
                "ts":     _to_ist(s.get("sent_at", "")),
                "email":  _mask_email(s["email"]),
                "status": s.get("status", "delivered"),
                "course": s.get("course", "MAPC"),
                "source": "MAPC Page",
                "via":    s.get("via", "Resend"),
            })

        if sorted_subs:
            out["last_send_ts"] = _to_ist(sorted_subs[0].get("sent_at", ""))

    except Exception as exc:
        out["error"] = str(exc)[:200]

    _MAPC_STATS_CACHE.update({"data": out, "ts": now})
    return out


@app.get("/api/delivery-stats", response_class=HTMLResponse)
def api_delivery_stats(request: Request):
    resp = templates.TemplateResponse(
        "_mapc_console.html",
        {"request": request, "mapc": _digital_delivery_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.get("/api/mapc-stats", response_class=HTMLResponse)
def api_mapc_stats_redirect(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/api/delivery-stats", status_code=301)


@app.get("/agent/digital_delivery", response_class=HTMLResponse)
def page_digital_delivery(request: Request):
    return _render(
        request,
        "_mapc_console.html",
        active_view="agent",
        active_agent="digital_delivery",
        mapc=_digital_delivery_stats(),
    )


@app.get("/agent/mapc_delivery", response_class=HTMLResponse)
def page_mapc_delivery_redirect(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/agent/digital_delivery", status_code=301)


def _scrape_goodreturns_gold() -> dict | None:
    """Scrape live gold + silver rates from goodreturns.in. Returns a snapshot
    dict matching the Gist schema, or None on failure."""
    import datetime as _dt
    from html import unescape as _unescape
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as c:
            gold_r = c.get("https://www.goodreturns.in/gold-rates/",
                           headers={"User-Agent": "Mozilla/5.0"})
            silver_r = c.get("https://www.goodreturns.in/silver-rates/",
                             headers={"User-Agent": "Mozilla/5.0"})
        if gold_r.status_code != 200 or silver_r.status_code != 200:
            return None

        # unescape so &#x20b9; and &#8377; become ₹
        gold_html = _unescape(gold_r.text)
        silver_html = _unescape(silver_r.text)

        inr_pat = re.compile(r"₹\s?([\d,]+)")
        chg_pat = re.compile(r"\(([+-]?[\d,]+)\)")

        # --- Gold national 10g rates ---
        # The HTML has a table row starting with ">10</td>" followed by 24K, 22K, 18K
        gold_10g: dict = {"24k_per_10g": None, "22k_per_10g": None, "18k_per_10g": None}
        gold_change_abs = 0.0
        row_idx = gold_html.find(">10</td>")
        if row_idx > 0:
            row_chunk = gold_html[row_idx:row_idx + 800]
            row_amounts = inr_pat.findall(row_chunk)
            row_changes = chg_pat.findall(row_chunk)
            if len(row_amounts) >= 3:
                gold_10g["24k_per_10g"] = float(row_amounts[0].replace(",", ""))
                gold_10g["22k_per_10g"] = float(row_amounts[1].replace(",", ""))
                gold_10g["18k_per_10g"] = float(row_amounts[2].replace(",", ""))
            if row_changes:
                gold_change_abs = float(row_changes[0].replace(",", ""))

        gold_change_pct = 0.0
        if gold_10g["24k_per_10g"] and gold_change_abs:
            prev = gold_10g["24k_per_10g"] - gold_change_abs
            if prev:
                gold_change_pct = round((gold_change_abs / prev) * 100, 2)

        # --- Silver national rate ---
        silver_per_kg = None
        silver_change_abs = 0.0
        silver_amounts = inr_pat.findall(silver_html)
        for a in silver_amounts:
            val = float(a.replace(",", ""))
            if 50000 < val < 500000:
                silver_per_kg = val
                break
        silver_changes = chg_pat.findall(silver_html)
        if silver_changes:
            silver_change_abs = float(silver_changes[0].replace(",", ""))

        silver_change_pct = 0.0
        if silver_per_kg and silver_change_abs:
            prev = silver_per_kg - silver_change_abs
            if prev:
                silver_change_pct = round((silver_change_abs / prev) * 100, 2)

        # --- City rates (per gram from the city table) ---
        # The city table starts after "Major Cities" heading — search from there
        # to avoid matching nav links earlier in the page.
        city_section_start = gold_html.find("Major Cities")
        if city_section_start == -1:
            city_section_start = 0
        gold_city_html = gold_html[city_section_start:]

        silver_city_start = silver_html.find("Major Cities")
        if silver_city_start == -1:
            silver_city_start = 0
        silver_city_html = silver_html[silver_city_start:]

        city_names = [
            "Chennai", "Mumbai", "Delhi", "Kolkata", "Bangalore",
            "Hyderabad", "Kerala", "Pune", "Vadodara", "Ahmedabad",
            "Jaipur", "Lucknow", "Coimbatore", "Madurai", "Visakhapatnam",
        ]
        cities: dict = {}
        for city in city_names:
            idx = gold_city_html.find(city)
            if idx == -1:
                continue
            chunk = gold_city_html[idx:idx + 400]
            city_amounts = inr_pat.findall(chunk)
            if len(city_amounts) >= 3:
                g24 = float(city_amounts[0].replace(",", ""))
                g22 = float(city_amounts[1].replace(",", ""))
                g18 = float(city_amounts[2].replace(",", ""))
                slug = city.lower().replace(" ", "_")
                cities[slug] = {
                    "name": city,
                    "gold": {
                        "24k_per_10g": g24 * 10 if g24 < 20000 else g24,
                        "22k_per_10g": g22 * 10 if g22 < 20000 else g22,
                        "18k_per_10g": g18 * 10 if g18 < 20000 else g18,
                    },
                    "silver": {"per_kg": None},
                }
                # Silver city rate
                s_idx = silver_city_html.find(city)
                if s_idx != -1:
                    s_chunk = silver_city_html[s_idx:s_idx + 400]
                    s_amounts = inr_pat.findall(s_chunk)
                    for sa in s_amounts:
                        sv = float(sa.replace(",", ""))
                        if sv > 50000:
                            cities[slug]["silver"]["per_kg"] = sv
                            break

        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        snapshot = {
            "date": today,
            "updated_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "goodreturns.in",
            "national": {
                "gold": gold_10g,
                "silver": {"per_kg": silver_per_kg},
            },
            "international": {
                "xau_usd_per_oz": None,
                "xag_usd_per_oz": None,
                "usd_inr": None,
            },
            "cities": cities,
            "change_pct": {
                "gold": gold_change_pct,
                "silver": silver_change_pct,
            },
        }
        return snapshot
    except Exception:
        return None


def _prompt_analyst_stats() -> dict:
    """Scan JSONL sessions, categorise messages, return analysis + insights + rendered report.
    No Claude call — pure data. Cached 5 min."""
    from datetime import date as _date, timedelta as _td
    now = time.time()
    if _PROMPT_ANALYST_CACHE["data"] and (now - _PROMPT_ANALYST_CACHE["ts"]) < _PROMPT_ANALYST_TTL:
        return _PROMPT_ANALYST_CACHE["data"]

    out: dict = {
        "configured": True,
        "analysis": None,
        "insights": [],
        "action_items": [],
        "report_html": None,
        "last_run_date": None,
        "next_run_date": None,
        "error": None,
    }

    try:
        from agents.prompt_analyst.agent import _load_messages, _analyse
        msgs = _load_messages(weeks=4)
        out["analysis"] = _analyse(msgs, weeks=4)
    except Exception as exc:
        out["error"] = str(exc)
        out["configured"] = False

    # ── Derive insight cards from live analysis data ──────────────────────────
    A = out["analysis"]
    if A:
        total = A["total_messages"]
        cats = A["overall_by_category"]
        correction_n = cats.get("correction", 0)
        vague_n = cats.get("vague", 0)
        feature_n = cats.get("feature_request", 0)
        correction_pct = round(correction_n / total * 100, 1) if total else 0
        vague_pct = round(vague_n / total * 100, 1) if total else 0

        # Project correction rates (only projects with >20 messages)
        proj_rates = {
            p: round(d["by_category"].get("correction", 0) / d["total"] * 100, 1)
            for p, d in A["by_project"].items() if d["total"] > 20
        }
        worst_proj = max(proj_rates, key=proj_rates.get) if proj_rates else None
        best_proj = min(proj_rates, key=proj_rates.get) if proj_rates else None

        # Week 3 spike detection
        week3 = next((w for w in A["weekly"] if w["week_label"] == "Week 3"), None)
        w3_correction_pct = round(week3["by_category"].get("correction", 0) / week3["total"] * 100, 1) if week3 and week3["total"] else 0

        out["insights"] = [
            {
                "impact": "high",
                "impact_label": "HIGH COST",
                "impact_color": "#e74c3c",
                "metric": f"{correction_pct}% of messages",
                "title": "Correction loop is your #1 token drain",
                "finding": (
                    f"{correction_n} out of {total} messages are redirects or undos — that's "
                    f"~25–40 wasted turns per week. When corrections chain, one wrong assumption "
                    f"costs 3–5 turns instead of 1."
                ),
                "before": "gold rates are not coming up and not in sync with goodreturns values",
                "after": "Gold rates panel shows ₹93,000/10g but GoodReturns shows ₹95,200. Scraper hits goodreturns.in — city table mismatch. Last good scrape 3h ago.",
                "rule": "Name the symptom + expected value + data source. One line eliminates the investigation loop.",
            },
            {
                "impact": "medium",
                "impact_label": "MEDIUM COST",
                "impact_color": "#f4c542",
                "metric": f"{vague_pct}% of messages",
                "title": "Vague messages force me to guess",
                "finding": (
                    f"{vague_n} messages carry no context anchor. You describe what you see "
                    f"('cant see 150 subscribers') but not where the data should come from, "
                    f"what you last changed, or what the expected output is. I pick an "
                    f"interpretation, it's wrong, correction follows."
                ),
                "before": "digital product, i cant see last 150 subscribers",
                "after": "MAPC console shows 1 subscriber. Source is mapc_all_sent.json Gist (primary) + Resend first page (supplement). Expected: ~130 deduplicated, emails masked.",
                "rule": "Every stat/count question needs: what you see + what you expect + which source is authoritative.",
            },
            {
                "impact": "medium" if worst_proj and proj_rates.get(worst_proj, 0) < 35 else "high",
                "impact_label": "PROJECT RISK",
                "impact_color": "#fb923c",
                "metric": f"{proj_rates.get(worst_proj, 0)}% corrections in {worst_proj}" if worst_proj else "Late constraints",
                "title": f"{'cv builder' if worst_proj and 'cv' in worst_proj.lower() else (worst_proj or 'Long sessions')} needs an opening brief",
                "finding": (
                    f"{'cv builder' if worst_proj and 'cv' in worst_proj.lower() else (worst_proj or 'Multi-thread sessions')} "
                    f"has the worst correction rate "
                    f"({proj_rates.get(worst_proj, 0)}% vs {proj_rates.get(best_proj, 0)}% in {best_proj or 'other projects'}). "
                    f"The driver: style rules and scope constraints arrive after significant work is done — "
                    f"'position as GPM not AVP' after 100 lines, 'no em-dashes' after a full draft."
                ),
                "before": "ok build the CV  [after 100 lines]  actually, position me as GPM not AVP",
                "after": "Build CV for [job]. Constraints: title = Group PM, no em-dashes, 105–125 char bullets, prioritise marketplace metrics, include GitHub link.",
                "rule": "3-line constraint brief at session start. Role · style rules · deal-breakers. Keep a snippet to paste.",
            },
        ]

        # Action items derived from analysis
        out["action_items"] = [
            {
                "n": 1,
                "impact": "high",
                "text": "Add [symptom] + [expected] + [source] to every bug report and stat question.",
                "why": f"Targets the {correction_n} correction messages — estimated 30–40% reduction.",
            },
            {
                "n": 2,
                "impact": "high",
                "text": "Start every cv-builder session with a 3-line brief: role title · style rules · deal-breakers.",
                "why": f"cv builder has a {proj_rates.get('cv builder', proj_rates.get(worst_proj, 0))}% correction rate — highest of all projects.",
            },
            {
                "n": 3,
                "impact": "medium",
                "text": 'End feature requests with "Done when [observable outcome]."',
                "why": f"You write strong feature briefs ({feature_n} this month) but rarely define the exit condition.",
            },
            {
                "n": 4,
                "impact": "medium",
                "text": "One debugging thread per session. Two issues = two sessions.",
                "why": f"Week 3 mixed 3 threads and hit {w3_correction_pct}% corrections — double your baseline.",
            },
            {
                "n": 5,
                "impact": "low",
                "text": "Move scope questions before the build, not after.",
                "why": f"{cats.get('question', 0)} questions this month — several came after work that should have been scoped first.",
            },
        ]

    # ── Load and render latest coaching report ────────────────────────────────
    report_dir = BASE_DIR.parent / "outputs" / "prompt_analyst"
    last_run_date = None
    if report_dir.exists():
        reports = sorted(report_dir.glob("*.md"), reverse=True)
        if reports:
            stem = reports[0].stem
            try:
                last_run_date = _date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))
                out["last_run_date"] = str(last_run_date)
            except Exception:
                pass
            try:
                raw = reports[0].read_text(encoding="utf-8")
                # Skip the title + generated header, render body as HTML
                body_start = raw.find("\n## ")
                body = raw[body_start:].strip() if body_start != -1 else raw.strip()
                out["report_html"] = _render_brief_md(body)
            except Exception:
                pass

    base = last_run_date or _date.today()
    out["next_run_date"] = str(base + _td(days=14))

    _PROMPT_ANALYST_CACHE.update({"data": out, "ts": now})
    return out


def _gold_rates_stats() -> dict:
    """Live gold/silver rates from GoodReturns, subscriber count from Brevo,
    alert history from Gist. Cached 60s."""
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

    # Primary source: scrape GoodReturns live
    snapshot = _scrape_goodreturns_gold()
    if snapshot:
        out["snapshot"] = snapshot
        out["configured"] = True
    else:
        # Fallback: read from Gist if configured
        gist_id = os.getenv("GOLD_RATES_GIST", "")
        if gist_id:
            try:
                with httpx.Client(timeout=10.0) as c:
                    r = c.get(f"https://api.github.com/gists/{gist_id}", headers=gist_headers)
                    if r.status_code == 200:
                        raw = r.json().get("files", {}).get("gold_rates_latest.json", {}).get("content", "")
                        if raw:
                            out["snapshot"] = json.loads(raw)
                            out["configured"] = True
                    else:
                        out["error"] = f"Gist fallback failed: {r.status_code}"
            except Exception as exc:
                out["error"] = str(exc)[:200]
        if not out["configured"]:
            out["error"] = "GoodReturns scrape failed and no GOLD_RATES_GIST fallback"

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


def _mbt_stats() -> dict:
    """MBT (Meet by Travel) creator outreach console. Reads the daily shortlist
    + drafted DM/email from the read-only 'MBT Sheet Reader' n8n webhook, then
    overlays local contacted/skipped status (tracked only in Indra — never
    written back to the sheet, never auto-sent)."""
    now = time.time()
    if _MBT_STATS_CACHE["data"] and (now - _MBT_STATS_CACHE["ts"]) < _MBT_STATS_TTL:
        return _MBT_STATS_CACHE["data"]

    out: dict = {
        "configured": False,
        "error": None,
        "rows": [],
        "total": 0,
        "pending_count": 0,
        "contacted_count": 0,
        "skipped_count": 0,
        "latest_week": None,
    }

    webhook_url = os.getenv("MBT_SHEET_WEBHOOK_URL", "").strip()
    if not webhook_url:
        out["error"] = "MBT_SHEET_WEBHOOK_URL not configured"
        _MBT_STATS_CACHE.update({"data": out, "ts": now})
        return out

    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(webhook_url)
            if r.status_code != 200:
                out["error"] = f"Sheet reader webhook returned HTTP {r.status_code}"
                _MBT_STATS_CACHE.update({"data": out, "ts": now})
                return out
            csv_text = r.text
    except Exception as exc:
        out["error"] = str(exc)[:200]
        _MBT_STATS_CACHE.update({"data": out, "ts": now})
        return out

    local_status = status_bus.get_mbt_statuses()
    rows = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        username = (row.get("username") or "").strip()
        if not username or username == "__none__":
            continue
        try:
            composite_score = float(row.get("composite_score") or 0)
        except ValueError:
            composite_score = 0.0
        row["composite_score"] = composite_score
        row["local_status"] = local_status.get(username.lower(), "pending")
        rows.append(row)

    rows.sort(key=lambda r: (r.get("week") or "", r["composite_score"]), reverse=True)

    out["configured"] = True
    out["rows"] = rows
    out["total"] = len(rows)
    out["pending_count"] = sum(1 for r in rows if r["local_status"] == "pending")
    out["contacted_count"] = sum(1 for r in rows if r["local_status"] == "contacted")
    out["skipped_count"] = sum(1 for r in rows if r["local_status"] == "skipped")
    out["latest_week"] = rows[0]["week"] if rows else None

    _MBT_STATS_CACHE.update({"data": out, "ts": now})
    return out


@app.get("/api/mbt-stats", response_class=HTMLResponse)
def api_mbt_stats(request: Request):
    resp = templates.TemplateResponse(
        "_mbt_console.html",
        {"request": request, "mbt": _mbt_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.get("/agent/mbt_creator_outreach", response_class=HTMLResponse)
def page_mbt_creator_outreach(request: Request):
    return _render(
        request,
        "_mbt_console.html",
        active_view="agent",
        active_agent="mbt_creator_outreach",
        mbt=_mbt_stats(),
    )


@app.post("/api/mbt/mark", response_class=HTMLResponse)
def api_mbt_mark(username: str = Form(...), status: str = Form(...)):
    if status not in ("pending", "contacted", "skipped"):
        raise HTTPException(status_code=400, detail="status must be pending, contacted, or skipped")
    status_bus.set_mbt_status(username, status)
    _MBT_STATS_CACHE["data"] = None  # force refresh on next load
    badge = {
        "contacted": '<span class="text-[10px] text-emerald-400 font-mono uppercase tracking-wider">✓ contacted</span>',
        "skipped": '<span class="text-[10px] text-zinc-600 font-mono uppercase tracking-wider">skipped</span>',
        "pending": '<span class="text-[10px] text-saffron-400 font-mono uppercase tracking-wider">pending</span>',
    }[status]
    return HTMLResponse(badge)


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


@app.get("/agent/prompt_analyst", response_class=HTMLResponse)
def page_prompt_analyst(request: Request):
    return _render(
        request,
        "_prompt_analyst_console.html",
        active_view="agent",
        active_agent="prompt_analyst",
        pa=_prompt_analyst_stats(),
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
    # Public liveness probe: deliberately excludes integration/configuration state.
    return JSONResponse({"ok": True})


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


# ---------- Prompt Analyst ----------

@app.post("/api/run/prompt_analyst", response_class=HTMLResponse)
async def run_prompt_analyst(
    background_tasks: BackgroundTasks,
    weeks: int = Form(4),
):
    weeks = max(1, min(int(weeks), 12))
    background_tasks.add_task(
        _run_in_background(
            PromptAnalystAgent,
            task=f"Prompt efficiency analysis — last {weeks} weeks",
            weeks=weeks,
        )
    )
    return HTMLResponse(
        f'<div class="text-saffron-400 text-sm">Queued — scanning {weeks} weeks of session JSONL files, then one Claude call for the coaching report. Watch the pipeline above; the report will appear in artifacts when done.</div>'
    )


@app.get("/api/prompt-analyst-stats", response_class=HTMLResponse)
def api_prompt_analyst_stats(request: Request):
    resp = templates.TemplateResponse(
        "_prompt_analyst_console.html",
        {"request": request, "pa": _prompt_analyst_stats()},
    )
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


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
    safe_email = html.escape(email, quote=True)
    safe_url = html.escape(confirm_url, quote=True)
    return f"""\
<!doctype html>
<html><body style="font-family: -apple-system, system-ui, sans-serif; max-width: 540px; margin: 24px auto; color: #222; line-height: 1.55;">
  <h2 style="margin-bottom: 8px;">Confirm your email</h2>
  <p>Hi — you (or someone using <b>{safe_email}</b>) asked to subscribe.</p>
  <p>Click the button below to confirm. If this wasn't you, ignore this email and nothing happens.</p>
  <p style="margin: 28px 0;">
    <a href="{safe_url}"
       style="background:#d4a017;color:#0a0e1f;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;">
      Confirm subscription
    </a>
  </p>
  <p style="font-size:12px;color:#666;">Or paste this link into your browser:<br>
    <span style="font-family:monospace;">{safe_url}</span>
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
    public_base = _configured_public_base()
    if not public_base:
        return JSONResponse(
            {"ok": False, "error": "DASHBOARD_PUBLIC_URL is not configured"},
            status_code=503,
        )

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
    confirm_url = f"{public_base}/confirm/{token}"

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


_raw_owner = os.getenv("OWNER_EMAILS", "ratelimit_test_qa@example.com")
_OWNER_EMAILS = {e.strip().lower() for e in _raw_owner.split(",") if e.strip()}

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

    public_base = _configured_public_base()
    if not public_base:
        return JSONResponse(
            {"ok": False, "error": "DASHBOARD_PUBLIC_URL is not configured"},
            status_code=503,
        )
    if _assess_secret() is None:
        return JSONResponse(
            {"ok": False, "error": "ASSESS_SECRET must contain at least 32 characters"},
            status_code=503,
        )

    if not email_signups.is_valid_email(email):
        return JSONResponse({"ok": False, "error": "Invalid email address"}, status_code=400)
    if test_id not in assessment_emails.KNOWN_TESTS:
        return JSONResponse(
            {"ok": False, "error": f"Unknown test_id. Allowed: {sorted(assessment_emails.KNOWN_TESTS)}"},
            status_code=400,
        )
    if not run_id or len(run_id) > 120:
        return JSONResponse({"ok": False, "error": "Missing or oversized run_id"}, status_code=400)
    if name and len(name) > 120:
        return JSONResponse({"ok": False, "error": "Oversized name"}, status_code=400)
    if not summary_text or len(summary_text) > 10_000:
        return JSONResponse({"ok": False, "error": "Missing summary_text"}, status_code=400)
    if len(results_url) > 2_048 or not _allowed_results_url(results_url):
        return JSONResponse({"ok": False, "error": "results_url origin is not allowed"}, status_code=400)

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
        unsubscribe_url = f"{public_base}/unsubscribe/{token}"

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
    secret = _assess_secret()
    if secret is None:
        raise RuntimeError("ASSESS_SECRET must contain at least 32 characters")
    msg = f"{email}|{test_id}|{run_id}".encode()
    digest = hmac.new(secret, msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def page_unsubscribe(request: Request, token: str):
    row = email_signups.get_by_token(token)
    return _render(request, "_unsubscribed.html", active_view="", row=row, completed=False)


@app.post("/unsubscribe/{token}", response_class=HTMLResponse)
def post_unsubscribe(request: Request, token: str):
    row = email_signups.unsubscribe_by_token(token)
    return _render(request, "_unsubscribed.html", active_view="", row=row, completed=True)


@app.post("/api/delivery-retry")
def api_delivery_retry(request: Request):
    """Process the pending email delivery queue.
    Called manually or by a scheduled task when Resend quota resets.
    Reads the queue from GitHub Gist, retries sends, clears processed entries."""
    from agents.digital_delivery.retry import process_queue
    try:
        result = process_queue(dry_run=False)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=500)


@app.get("/api/delivery-queue-status")
def api_delivery_queue_status(request: Request):
    """Show how many emails are currently queued for retry."""
    from agents.digital_delivery.retry import _fetch_pending
    try:
        pending, _ = _fetch_pending()
        return JSONResponse({
            "queued": len(pending),
            "emails": [{"email": e.get("email"), "queued_at": e.get("queued_at")} for e in pending],
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200], "queued": 0}, status_code=500)


@app.post("/api/mapc-retry")
def api_mapc_retry_redirect(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/api/delivery-retry", status_code=307)


@app.get("/api/mapc-queue-status")
def api_mapc_queue_redirect(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/api/delivery-queue-status", status_code=301)


def run() -> None:
    import uvicorn

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8765"))
    uvicorn.run("dashboard.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
