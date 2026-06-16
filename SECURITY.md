# Indra · Security Policy & Audit

**Threat model**: Single-operator content + outbound system. Threats we defend against:
- Random scraping / public discovery
- Unauthenticated access to dashboard or APIs
- Webhook spoofing (someone pretending to be n8n)
- Webhook replay (capturing one webhook and replaying it later)
- Self-XSS via user input flowing into logs
- Secret leakage through the conversation log
- Cost abuse (someone running Claude calls on our dime)

**Out of scope** (would require a different architecture): nation-state attackers, supply-chain attacks on Python deps, side-channels.

## Current security posture

### What's enforced in code

| Layer | Implementation | Where |
|---|---|---|
| **HTTP Basic Auth** on all dashboard + API routes | Middleware in `dashboard/app.py` — fail-closed if `INDRA_DASHBOARD_USER` / `INDRA_DASHBOARD_PASSWORD` unset | Returns 401 on missing creds, 500 if server unconfigured |
| **HMAC-SHA256** on all n8n webhooks | `_verify_hmac()` / `_sign_payload()` in `dashboard/app.py` | Timing-safe compare via `hmac.compare_digest` |
| **Replay protection** on webhooks | Timestamp (±5 min window) + one-time-use nonce | In-memory nonce store with auto-prune |
| **Request size cap** | 256 KB hard limit | Middleware rejects with 413 |
| **Rate limiting** on agent runs | 12 runs per IP per 60s window | Middleware rejects with 429 |
| **No-index headers** | `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet` on every response + `robots.txt: Disallow: /` | Middleware |
| **Defense headers** | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` denying camera/mic/geolocation | Middleware |
| **XSS prevention** | Jinja2 auto-escape in templates; explicit HTML escape + `[a-z0-9_-]` slugging in SSE feed JavaScript | Templates + `shell.html` |
| **SQL injection** | All queries use parameterized SQLite statements | `core/status_bus.py` |
| **Filesystem permissions** | `chmod 600` on `data/status.db`, `chmod 700` on `data/` directory | `_restrict_perms()` in `status_bus.py` |
| **`.env` file** | `chmod 600`, listed in `.gitignore` | Manual |

### What's enforced by configuration (you must do this)

| Action | Required | Why |
|---|---|---|
| Set `INDRA_DASHBOARD_PASSWORD` to a strong random value in `.env` | YES | Without it, dashboard fail-closes with 500 |
| Set `INDRA_N8N_SHARED_SECRET` to ≥32 random bytes, identical in n8n Cloud env vars | YES | Without it, webhooks fail-close with 500 |
| Enable **Cloudflare Access** on top of the tunnel for human-side traffic | STRONGLY RECOMMENDED | Defense in depth; Cloudflare blocks at the edge before Indra sees the request |
| Use **Cloudflare Service Auth tokens** for n8n's webhook traffic if Access is enabled | If Access enabled | n8n's machine traffic bypasses human OAuth |
| Use a **named tunnel** (not quick tunnel) for anything beyond development | For production | Quick tunnel URLs change every restart — fine for dev, breaks Meta webhook subscriptions |
| Rotate any secret pasted in chat | See below | Conversation logs persist; chat ≠ secure channel |

## URGENT: Rotate these secrets now

You pasted these in chat during this conversation. They are exposed in the conversation log permanently. Treat as compromised and rotate:

1. **`LANGFUSE_SECRET_KEY`** — `sk-lf-a15167d3-...`
   - Go to https://cloud.langfuse.com → Settings → API Keys → revoke the exposed key → create new
   - Update your `.env` (`LANGFUSE_SECRET_KEY`)
   - The `LANGFUSE_PUBLIC_KEY` is fine to leave (it's the public half)

You can do this in 2 minutes. Don't skip it.

## Secret hygiene — things to NEVER do

- ❌ Never paste API keys, tokens, passwords into chat — they're logged forever
- ❌ Never commit `.env` to git (it's in `.gitignore`; verify after every change)
- ❌ Never log secret values via `self.log()` — they end up in the activity feed and Langfuse traces
- ❌ Never hardcode tokens in Claude prompts — Langfuse stores prompts in clear and the prompt UI shows them to anyone with Langfuse access
- ❌ Never hardcode tokens in n8n function nodes — your existing workflows (LinkedIn Content Engine, Drivex, AI Twitter) have **`sk-ant-...` keys hardcoded in node code**. This is a pre-existing issue inherited from before Indra; move them to n8n's credential store at your convenience

## Webhook security details

### Inbound (n8n → Indra)
- Both endpoints (`/api/webhooks/n8n/new-comment`, `/api/webhooks/n8n/send-confirmed`) require:
  - `X-Indra-Signature: <HMAC-SHA256(secret, timestamp + "." + nonce + "." + body)>`
  - `X-Indra-Timestamp: <unix-epoch-seconds>`
  - `X-Indra-Nonce: <32-char random hex>`
- Indra rejects with 401 if:
  - Signature missing or doesn't match
  - Timestamp older than 5 minutes (replay protection)
  - Nonce already seen within the window (replay protection)
- Webhooks **do not** require Basic Auth (HMAC is their gate). They are explicitly bypass-listed in the security middleware

### Outbound (Indra → n8n)
- Indra calls `N8N_SEND_WEBHOOK_URL` with the same headers, signed the same way
- The provided `indra__approve_and_send_via_meta.json` workflow's first node verifies signature + timestamp + nonce identically (uses `$getWorkflowStaticData` for n8n-side replay protection)

## Dashboard authentication details

- Browser hits dashboard → 401 with `WWW-Authenticate: Basic realm="Indra"` → browser prompts for username/password
- Credentials compared with `hmac.compare_digest` (timing-safe)
- Successful auth cached by the browser for the session — every subsequent request includes the auth header automatically
- HTMX requests inherit the same auth (browser handles it)
- To log out, close the browser tab (Basic Auth has no server-side session to invalidate)

## Pre-existing risks from your other projects

These are **outside Indra's blast radius** but worth flagging since they relate to your account:

- Your existing n8n workflows (LinkedIn / Drivex / AI Twitter) hardcode `ANTHROPIC_API_KEY` in function-node code. Move them to n8n's credential store
- Your existing n8n API key (JWT) is in a reference file you keep on disk. Make sure that file is not in any synced folder (iCloud, Dropbox, etc.) or git repo
- Your `~/.n8n/database.sqlite` contains all stored credentials. The file is encrypted but the encryption key is in `~/.n8n/config`. Both files need to live on a disk you own

## What I will NOT do

Per the project's hard rules:
- I will not store any Meta Access Token in Indra. They live exclusively in n8n Cloud's credential vault
- I will not accept any secret pasted in chat. If you offer one, I'll decline and tell you to put it in `.env` directly
- I will not auto-approve drafts. The approve gate is the human check; removing it = ban risk on Instagram
- I will not wire unofficial scraping libraries (instagrapi, instabot, etc.) anywhere in the stack
- I will not bypass HMAC verification "just for testing" — if you need to test, use the real signing flow

## When to re-audit

Re-run this audit whenever:
- You expose Indra to a new network surface (new tunnel, new VPS, new domain)
- You add a new external integration (new MCP, new third-party API)
- You add a new agent that handles personally identifiable data (user DMs, email addresses, payment info)
- Meta App Review clears and real IG data starts flowing
- More than 6 months have passed without an audit
