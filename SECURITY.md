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
| **HTTP Basic Auth** on administrative routes | Middleware in `dashboard/app.py` fails closed if `INDRA_DASHBOARD_USER` / `INDRA_DASHBOARD_PASSWORD` are unset | Returns 401 for bad/missing credentials and 503 when server auth is unconfigured |
| **HMAC-SHA256** on all n8n webhooks | `_verify_hmac()` / `_sign_payload()` in `dashboard/app.py` | Timing-safe compare via `hmac.compare_digest` |
| **Replay protection** on webhooks | Timestamp (±5 min window) + one-time-use nonce | In-memory nonce store with auto-prune |
| **Request size cap** | 256 KB hard limit | Middleware rejects with 413 |
| **Host/path validation** | Uses the raw ASGI path and an exact host allowlist instead of reconstructed `request.url` | Mitigates known Starlette Host/path confusion advisories |
| **Rate limiting** | 12 agent runs and 5 public email requests per trusted client IP per 60s window | Proxy headers are accepted only from `INDRA_TRUSTED_PROXIES` |
| **No-index headers** | `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet` on every response + `robots.txt: Disallow: /` | Middleware |
| **Defense headers** | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` denying camera/mic/geolocation | Middleware |
| **XSS prevention** | Jinja2 auto-escape plus Bleach allowlist sanitization for Markdown rendered with `|safe` | Templates + `_render_brief_md()` |
| **SQL injection** | All queries use parameterized SQLite statements | `core/status_bus.py` |
| **Filesystem permissions** | `chmod 600` on `data/status.db`, `chmod 700` on `data/` directory | `_restrict_perms()` in `status_bus.py` |
| **`.env` file** | `chmod 600`, listed in `.gitignore` | Manual |

### What's enforced by configuration (you must do this)

| Action | Required | Why |
|---|---|---|
| Set `INDRA_DASHBOARD_USER` and a ≥16-character `INDRA_DASHBOARD_PASSWORD` in `.env` | YES | Missing/weak credentials make administrative routes fail closed with 503 |
| Set `INDRA_N8N_SHARED_SECRET` to ≥32 random bytes, identical in n8n Cloud env vars | YES | Without it, webhooks fail-close with 500 |
| Set `DASHBOARD_PUBLIC_URL` to the canonical HTTPS origin | For email flows | Prevents Host-header injection into confirmation/unsubscribe links |
| Set `ASSESS_SECRET` to ≥32 random characters | For labs email flow | No insecure development fallback is used |
| Set `INDRA_TRUSTED_PROXIES` to exact proxy IPs/CIDRs | Behind a proxy | Prevents spoofed forwarding headers from bypassing per-IP limits |
| Set `INDRA_ALLOWED_HOSTS` to any additional exact hostnames | When needed | Rejects poisoned or unexpected Host headers |
| Enable **Cloudflare Access** on top of the tunnel for human-side traffic | STRONGLY RECOMMENDED | Defense in depth; Cloudflare blocks at the edge before Indra sees the request |
| Use **Cloudflare Service Auth tokens** for n8n's webhook traffic if Access is enabled | If Access enabled | n8n's machine traffic bypasses human OAuth |
| Use a **named tunnel** (not quick tunnel) for anything beyond development | For production | Quick tunnel URLs change every restart — fine for dev, breaks Meta webhook subscriptions |
| Rotate any secret pasted in chat | See below | Conversation logs persist; chat ≠ secure channel |

## Secret rotation

If a credential is ever pasted into chat, logs, an issue, or a commit, revoke it
through the provider and replace the local `.env` value. Do not publish even a
partial credential identifier in this repository.

## Secret hygiene — things to NEVER do

- ❌ Never paste API keys, tokens, passwords into chat — they're logged forever
- ❌ Never commit `.env` to git (it's in `.gitignore`; verify after every change)
- ❌ Never log secret values via `self.log()` — they end up in the activity feed and Langfuse traces
- ❌ Never hardcode tokens in Claude prompts — Langfuse stores prompts in clear and the prompt UI shows them to anyone with Langfuse access
- ❌ Never hardcode tokens in workflow/function nodes; use the provider's credential store

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

## Dependency advisory mitigations

Python 3.11 or newer is required so patched dependency releases can be installed.
`bleach`, `python-multipart`, and `python-dotenv` are pinned to patched versions.

FastAPI 0.128.8 still requires Starlette `<1.0`, while several 2026 Starlette
advisories designate fixes only in Starlette 1.x. Until FastAPI supports that
series, this application mitigates the affected behavior as follows:

- `PYSEC-2026-161` and `PYSEC-2026-248`: raw ASGI path validation plus exact Host validation; security decisions never use `request.url.path`.
- `PYSEC-2026-249`: a body-size cap is enforced for chunked and fixed-length requests, URL-encoded fields are capped, and public multipart requests are rejected.
- `PYSEC-2026-2280`: the application uses FastAPI function routes with explicit HTTP methods, not unconstrained `HTTPEndpoint` subclasses.
- `PYSEC-2026-2281`: the affected Windows UNC-path behavior is outside the supported POSIX deployment; the static directory is fixed by the application.

Remove these temporary mitigations only after upgrading to a FastAPI release
compatible with Starlette 1.3.1 or newer.

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
