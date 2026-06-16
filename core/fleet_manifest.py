"""Single source of truth for every agent in the fleet.

Three declared agents: comment_dm_responder (n8n IG auto-responder),
daily_ai_brief (Claude Cloud Routine → GitHub, ~03:36 UTC), and thought_mechanic
(Instagram Reels brief generator — being converted from a manual slash command
to a Cloud Routine; see docs/THOUGHT-MECHANIC-ROUTINE.md). Other agents were
trashed on 2026-05-17 per operator decision. Add new agents back here when
they're built and approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Category = Literal["content", "intelligence", "outbound", "engagement", "ops"]

# Only the categories we currently use. Easy to add more later.
CATEGORIES: dict[str, dict] = {
    "engagement": {
        "label": "Engagement",
        "description": "Auto-responds to public interactions on social platforms.",
    },
    "intelligence": {
        "label": "Intelligence",
        "description": "Daily digests + signal scans curated from the AI world.",
    },
    "ops": {
        "label": "Operations",
        "description": "Site/fleet health, SEO, and guardrails. Runs in the background so the operator can stay an observer.",
    },
}


@dataclass(frozen=True)
class FleetEntry:
    name: str
    label: str
    short_description: str          # one-line for the overview grid
    description: str                # longer copy for the detail page
    built: bool
    category: Category
    workflow: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    rationale: str = ""
    production_status: str = ""     # "" (default) | "live"
    runtime: str = ""               # e.g. "n8n"
    hidden: bool = False            # hide from dashboard sidebar/overview; CLI still works


FLEET: tuple[FleetEntry, ...] = (
    FleetEntry(
        name="comment_dm_responder",
        label="Instagram auto-comment and DM",
        short_description="Auto-replies to IG comments + DMs the link with follow-gate flow",
        description=(
            "Production agent running on n8n. When someone comments on a configured "
            "Instagram post, replies publicly and DMs them the link. Non-followers "
            "get a gated DM with quick-reply buttons asking to follow first; tapping "
            "a button re-checks follow status and delivers the link. Anti-spam dedup "
            "prevents double-sends. Pure template rotation — no LLM in the loop."
        ),
        built=True,
        production_status="live",
        runtime="n8n",
        category="engagement",
        workflow=(
            "Webhook: comment or DM-button event from Meta",
            "Parse payload, filter spam + own echoes",
            "Look up post in campaign map",
            "Check follower status via Meta Graph API",
            "Send public reply (10 rotating variants)",
            "Send link DM if follower, gated DM with buttons if not",
            "Mark dedup so users aren't double-sent",
        ),
        inputs=(
            "Instagram post added to campaign map (post_id + payload URL + campaign slug)",
            "Public-reply + link-DM templates in Langfuse",
        ),
        outputs=(
            "Public comment replies + DMs sent via Meta Graph API",
            "n8n execution log + dedup state in workflow static data",
        ),
        rationale="Single most legitimate IG growth pattern under Meta's rules. Native APIs only, no scrapers.",
    ),
    FleetEntry(
        name="daily_ai_brief",
        label="Daily AI brief",
        short_description="Daily digest of one big AI-world story — pushed to GitHub at ~03:40 UTC",
        description=(
            "Production agent running on Claude Cloud Routines. Each day picks one "
            "high-signal story from the AI ecosystem, writes a long-form brief with "
            "the core insight, mental model to steal, a 90-minute experiment, and "
            "three claims you should be able to defend. Published as a markdown file "
            "to GitHub for permanent archive — dashboard surfaces the readable view "
            "so you don't have to navigate the repo."
        ),
        built=True,
        production_status="live",
        runtime="cloud routines → github",
        category="intelligence",
        workflow=(
            "Cloud routine fires daily ~03:40 UTC",
            "Claude picks a high-signal story across the AI ecosystem",
            "Writes long-form brief (title, frontmatter, structured sections)",
            "Pushes as markdown to urstrulyemkay/emkayjami/daily-brief/YYYY-MM-DD.md",
            "Dashboard reads GitHub API, renders the brief inline",
        ),
        inputs=(
            "(none — autonomous cloud routine schedule)",
        ),
        outputs=(
            "1 markdown brief per day in daily-brief/ on GitHub",
            "Surfaced in Indra dashboard at /agent/daily_ai_brief",
        ),
        rationale="One quality story per day beats a feed of low-signal news. Indra surfaces it so the operator doesn't have to crawl GitHub.",
    ),
    FleetEntry(
        name="thought_mechanic",
        label="Thought Mechanic reel planner",
        short_description="Daily Instagram Reel brief for @thoughtmechanic — viral score, content pillars, research-backed",
        description=(
            "Production agent running on Claude (Cloud Routine or /thought-mechanic slash "
            "command from Claude Code). Each day generates a ready-to-shoot Instagram Reel "
            "brief for @thoughtmechanic: viral hook concept, audience targeting, research "
            "sources (NIMHANS / ANCIPS / academic), content pillar rotation, virality score. "
            "Published as markdown to GitHub at thought-mechanic/tm-YYYY-MM-DD.md (the tm- "
            "prefix was added 2026-05-17). Dashboard surfaces the briefs + PLAYBOOK.md."
        ),
        built=True,
        production_status="live",
        runtime="cloud routines → github",
        category="intelligence",
        workflow=(
            "Cloud routine fires daily",
            "Claude picks an Indian mental-health story with viral potential",
            "Writes Reel brief: hook, body beats, CTA, research sources",
            "Pushes as markdown to thought-mechanic/YYYY-MM-DD.md",
            "Dashboard renders alongside the static PLAYBOOK.md strategy doc",
        ),
        inputs=(
            "(none — autonomous cloud routine schedule)",
            "PLAYBOOK.md as static brand-voice + content-pillar reference",
        ),
        outputs=(
            "1 Reel brief per day in thought-mechanic/ on GitHub",
            "Surfaced in Indra dashboard at /agent/thought_mechanic",
        ),
        rationale="Reel briefs that arrive ready-to-shoot remove the daily 'what should I post' decision. Strategy is hardcoded in PLAYBOOK.md; tactics are generated daily.",
    ),
    FleetEntry(
        name="labs_results_emailer",
        label="Lab assessment results",
        short_description="Emails personalized results to visitors who complete a /labs/ assessment and (optionally) subscribes them to the newsletter",
        description=(
            "Webhook endpoint on Indra that receives an assessment completion event from "
            "manikumarjami.com/labs (Big Five, attachment style, career interest, cognitive, "
            "student stress; PHQ-9 + GAD-7 are EXEMPT from gating per the lab's ethical carve-out). "
            "Renders a personalized summary email — top strengths, top growth edges, link back "
            "to the full report — and sends via Resend. Records the visitor's email in the "
            "shared email_signups table; if the visitor ticked the newsletter consent box on "
            "the form, marks the row as a confirmed newsletter subscriber. Replaces nothing on "
            "the labs side — adds an opt-in email layer to a flow that's currently URL-fragment-only."
        ),
        built=True,
        production_status="",
        runtime="Indra (FastAPI webhook + Resend) — local only, NOT YET DEPLOYED",
        category="engagement",
        workflow=(
            "Webhook: POST /api/labs/results-email from labs form on assessment completion",
            "Validate payload (test_id, email, results, optional newsletter_opt_in)",
            "Idempotency check on (email, test_id, run_id) so re-clicks don't re-send",
            "Render results email from test-specific HTML template + visitor data",
            "Send transactional email via Resend",
            "Record assessment_emails row + insert/update email_signups row (status reflects opt-in)",
        ),
        inputs=(
            "POST body: {email, name?, test_id, run_id, summary, results_url, newsletter_opt_in}",
            "Resend API key + verified sending domain (sandbox only delivers to your own email)",
            "Per-test email templates under dashboard/templates/emails/<test_id>.html",
        ),
        outputs=(
            "Transactional result email to the visitor (always)",
            "assessment_emails row in data/status.db (audit trail)",
            "email_signups row (status='pending' if no consent, 'confirmed' if newsletter opt-in)",
            "Admin view at /signups + /agent/labs_results_emailer console on Indra",
        ),
        rationale=(
            "Assessment completion is the highest-intent moment a visitor reaches. A clean, branded "
            "email-deliverable summary outperforms a URL-fragment-only results page (which they "
            "can't easily share or revisit). Also gives the operator a real audience to email — "
            "currently labs hands subscribers to Substack via iframe, with no operator-side list."
        ),
    ),
    FleetEntry(
        name="seo_audit",
        label="SEO daily audit",
        short_description="Daily SEO audit of manikumarjami.com — top 3-5 actionable suggestions pushed to GitHub at ~04:00 UTC",
        description=(
            "Claude Cloud Routine that crawls manikumarjami.com each morning, runs technical and "
            "content SEO checks (meta tags, schema, headings, internal links, sitemap freshness, "
            "broken links, page speed signals, title/description length, keyword coverage), and "
            "writes the top 3-5 actionable fixes for that day to seo-audit/YYYY-MM-DD.md on GitHub. "
            "Dashboard reads the GitHub API and surfaces today's audit at /agent/seo_audit. PostHog "
            "is wired as an optional input — when configured, the agent uses page-view counts to "
            "prioritize fixes on high-traffic URLs. No paid services beyond Claude."
        ),
        built=False,
        production_status="",
        runtime="cloud routines → github",
        category="ops",
        workflow=(
            "Cloud routine fires daily ~04:00 UTC",
            "Crawl: fetch manikumarjami.com root + /labs + /blog + /drivex (sitemap-driven)",
            "Technical checks: meta tags, OpenGraph, Twitter cards, JSON-LD schema, robots, sitemap freshness, canonical tags, broken links, image alts",
            "Content checks: title/description length, H1 presence, heading hierarchy, keyword density vs intent",
            "(Optional) PostHog query: pull last-7-day pageviews per URL → weight findings by traffic",
            "Rank top 3-5 fixes by (impact × ease)",
            "Write seo-audit/YYYY-MM-DD.md to GitHub with the prioritized list + reasoning + suggested copy/diff where applicable",
        ),
        inputs=(
            "manikumarjami.com — publicly fetched HTML + sitemap.xml + llms.txt",
            "(Optional) POSTHOG_API_KEY in routine env for pageview-weighted prioritization",
            "GitHub write access to urstrulyemkay/emkayjami for the seo-audit/ path",
        ),
        outputs=(
            "1 markdown audit per day in seo-audit/ on GitHub",
            "Dashboard view at /agent/seo_audit (latest + archive)",
        ),
        rationale=(
            "Daily small SEO wins compound. A single human SEO audit eats hours; an LLM agent can "
            "iterate fixes overnight and surface only what the operator needs to action. PostHog "
            "data turns the audit from generic best-practice into traffic-prioritized impact."
        ),
    ),
    FleetEntry(
        name="job_hunter",
        label="LinkedIn hunter + CV tailor",
        short_description="Pulls fresh LinkedIn jobs via Apify, ranks fit, drafts a tailored CV per top match into cv-builder",
        description=(
            "Local Python agent. Calls an Apify LinkedIn jobs scraper actor with the operator's "
            "target role and filters, ranks every posting against the master profile in the sibling "
            "cv-builder project, then writes a 2-page tailored CV markdown per top match directly "
            "into `cv builder/jobs/<slug>/` alongside a JD analysis (ATS keywords + fit notes + "
            "match gaps). Honors the hard CV rules locked in cv builder/CLAUDE.md (no em-dashes, "
            "no arrows, 105-125 char bullets, canonical metrics, AVP-not-VP). Drafts only — no "
            "auto-apply. The operator ports each markdown to data.json and runs templates/render.py "
            "manually to produce the PDF."
        ),
        built=True,
        production_status="",
        runtime="local (FastAPI dashboard) — calls Apify + Claude SDK directly",
        category="intelligence",
        workflow=(
            "Form input: target role + location + keywords + region (india|europe) + scrape/tailor limits",
            "Build a LinkedIn jobs search URL (keywords + location + posted-last-7d) and POST it to the Apify actor",
            "Normalize the Apify dataset fields across actor variants",
            "Load master-profile.json and templates/<region>/sample-data.json from cv builder/",
            "Single Claude call: rank every job + write four blocks per top N (CV markdown, JSON patches, fit report, referral)",
            "Per top job: write jd.md + tailored-cv.md + data.json (sample-data merged with patches) + fit-report.md + referral-message.md into cv builder/jobs/<slug>/",
            "Save the full bundle as one artifact under outputs/job_hunter/ on Indra",
        ),
        inputs=(
            "Apify token (APIFY_TOKEN) — free tier $5/month platform credit covers ~5000 jobs/month at $0.001/result",
            "Optional APIFY_LINKEDIN_ACTOR_ID (defaults to curious_coder~linkedin-jobs-scraper, pay-per-result, no monthly rental)",
            "CV builder project path set via CV_BUILDER_DIR env var (defaults to ./cv-builder)",
            "cv builder/templates/<region>/sample-data.json as the data.json base; cv builder/profile/master-profile.json as the truth source",
        ),
        outputs=(
            "Per top job: cv builder/jobs/<slug>/{jd.md, tailored-cv.md, data.json, fit-report.md, referral-message.md}",
            "data.json is render-ready: `python templates/render.py templates/<region>/cv.html.j2 jobs/<slug>/data.json jobs/<slug>/cv.pdf`",
            "Master bundle under outputs/job_hunter/<timestamp>-<query>.md (ranking table + every block per job)",
            "Artifact row in Indra surfaced via /artifacts and /agent/job_hunter",
        ),
        rationale=(
            "Closes the loop between job discovery and CV tailoring without manual copy-paste. "
            "Operator sees fresh roles + a defensible first-draft CV per role in one run, then "
            "decides which to render to PDF and pursue. No auto-apply keeps the legal/ethical "
            "guardrails intact."
        ),
    ),
    FleetEntry(
        name="competitor_watcher",
        label="Creator watcher — Instagram benchmarking",
        short_description="Cross-creator analysis of @saptarshiux, @vaibhavsisinty, @thevarunmayya — reels, hooks, what lifts engagement",
        description=(
            "Static analysis output (not a re-runnable agent yet). A one-off Instagram "
            "creator benchmark comparing @saptarshiux (Saptarshi Singha), @vaibhavsisinty "
            "(Vaibhav Sisinty), and @thevarunmayya (Varun Mayya): reel-level engagement, "
            "hook patterns, long- vs short-form lift, and per-creator playbooks for the "
            "behavioral-psych / tech / legal-tech pillars. Lives under "
            "outputs/instagram_scraper/ — the dashboard here just links to it. In the "
            "mythology mapping this is Garuda (गरुड़) under Vishnu / Intelligence."
        ),
        built=True,
        production_status="",
        runtime="static output — outputs/instagram_scraper/ (no live run)",
        category="intelligence",
        outputs=(
            "outputs/instagram_scraper/creator_dashboard.html — the visual comparison dashboard",
            "outputs/instagram_scraper/creator_comparison.md — written cross-creator analysis",
            "outputs/instagram_scraper/personas/*.md — per-creator profile + insights playbook",
        ),
        rationale=(
            "Captures who to model on Instagram and exactly which mechanics lift engagement, "
            "so the content agents (script_writer / thought_mechanic) borrow from proven "
            "patterns instead of guessing."
        ),
    ),
    FleetEntry(
        name="startup_lookup",
        label="Startup lookup — funded India startups × senior PM roles",
        short_description="Recently-funded Indian startups (pre-seed→C) that are hiring at Group PM / Product Lead / Head of Product level",
        description=(
            "Local Python agent, no Claude API call (free). Pulls India funding RSS "
            "(Entrackr + YourStory), parses company/stage/amount/date from titles, filters to "
            "the operator's stages + recency window, then runs ONE Apify LinkedIn search for "
            "senior PM-track roles in India (Group PM, Product Lead, Director/Head of Product, "
            "Principal/AVP Product). Cross-references the two lists, scores each funded-company × "
            "open-role match, and writes a markdown bundle with company details, the job link, and "
            "a heuristic fit note. Unmatched fresh raises are listed for manual review. For an "
            "LLM-quality 'why you're a fit' rewrite, run `rank startup lookup latest` in a Claude "
            "Code session — keeps Anthropic spend at zero."
        ),
        built=True,
        production_status="",
        runtime="local (FastAPI dashboard) — RSS + Apify, no LLM",
        category="intelligence",
        workflow=(
            "Form input: recency window (days) + funding stages + company cap + jobs to scan",
            "Fetch India funding RSS — Entrackr /rss + YourStory funding feed (no API key)",
            "Parse titles → company, stage, amount, date; drop VC-fund-launch noise; filter stage + window",
            "One Apify LinkedIn search: senior PM roles in India, past 30 days, date-sorted",
            "Cross-reference funded companies × open senior-PM roles; score by stage + role-level + recency",
            "Write outputs/startup_lookup/<ts>-india.md (matches + unmatched raises) and record artifact",
        ),
        inputs=(
            "Apify token (APIFY_TOKEN) — free tier $5/month; ~one LinkedIn search per run (~$0.05)",
            "Optional APIFY_LINKEDIN_ACTOR_ID (defaults to curious_coder~linkedin-jobs-scraper)",
            "India funding RSS: Entrackr https://entrackr.com/rss + YourStory funding feed (free)",
        ),
        outputs=(
            "outputs/startup_lookup/<ts>-india.md — per-match block (company, funding, job link, fit note) + unmatched raises",
            "Artifact row in Indra surfaced via /artifacts and /agent/startup_lookup",
        ),
        rationale=(
            "Targets companies with fresh capital + runway + active senior-product hiring, in one "
            "scan. RSS is real-time so the natural window is ~1-2 weeks; a single broad LinkedIn "
            "search keeps Apify spend trivial. No Claude API call keeps it free — the operator "
            "upgrades the fit rationale on demand from a Code session."
        ),
    ),
    FleetEntry(
        name="mapc_delivery",
        label="MAPC study guide delivery",
        short_description="Sends the MAPC Exam Prep PDF to subscribers via Resend — one unique download link per email, single-use + 48h expiry",
        description=(
            "Engagement agent. When a student subscribes at manikumarjami.com/mapc, "
            "the site POSTs to n8n which calls Resend directly for real-time delivery. "
            "This agent is the manual/bulk companion: send or re-send the guide to any "
            "email address, view delivery history in the Indra dashboard, and batch-send "
            "to a list file. Each recipient gets a unique HMAC-signed download link that "
            "works exactly once and expires after 48 hours — prevents link sharing. "
            "Answers guide (with Block → Unit → Section source references) will be sent "
            "as a follow-up email to the same subscriber list within 48 hours of the "
            "initial PDF delivery."
        ),
        built=True,
        production_status="live",
        runtime="Indra (local) + n8n Cloud (real-time website trigger)",
        category="engagement",
        workflow=(
            "Input: single email or path to newline-separated email list",
            "Validate inputs and confirm RESEND_API_KEY is set",
            "Generate HMAC-signed single-use download URL per recipient",
            "Send branded delivery email via Resend (HTML + plain text)",
            "Log success/failure per recipient as markdown artifact",
        ),
        inputs=(
            "email: str  — single recipient",
            "emails: list[str]  — multiple recipients",
            "--list <file>  — path to newline-separated email list (CLI)",
            "RESEND_API_KEY in .env",
            "ASSESS_SECRET in .env (for HMAC token signing)",
            "SITE_BASE_URL in .env (defaults to https://manikumarjami.com)",
        ),
        outputs=(
            "Transactional delivery email to each recipient",
            "outputs/mapc_deliveries/<timestamp>-delivery.md — per-recipient status log",
            "Artifact row in Indra surfaced at /agent/mapc_delivery",
        ),
        rationale=(
            "Students who subscribe at /mapc deserve immediate, reliable delivery. "
            "The n8n path handles real-time website-triggered sends (laptop-off safe). "
            "This Indra agent handles re-sends, bulk campaigns, and delivery auditing "
            "from the dashboard — the operator sees every send in one place."
        ),
    ),
    FleetEntry(
        name="gold_rates",
        label="Gold & silver rates",
        short_description="Daily India gold/silver rates page (15 cities) + >5% move email alerts",
        description=(
            "Engagement agent for the manikumarjami.com/gold-rates SEO page. A daily Vercel "
            "Cron (api/gold-rates-cron.js) fetches live XAU/XAG spot prices (gold-api.com) and "
            "USD-INR forex (frankfurter.dev), computes India national rates (24K/22K/18K gold "
            "+ silver, with import duty + GST) and 15 city-adjusted rates, and writes the "
            "snapshot to a public price-data Gist that the page reads directly. If gold or "
            "silver moves more than 5% day-over-day, it emails everyone on the 'Gold & "
            "Silver Alerts' Brevo list. Visitors can subscribe to that list via an on-page "
            "form (api/gold-rates-subscribe.js). This Indra agent is the dashboard companion: "
            "reads the latest snapshot, checks subscriber count, and reports freshness — the "
            "actual fetch/compute/alert pipeline runs on Vercel, independent of this fleet."
        ),
        built=True,
        production_status="live",
        runtime="Vercel Cron (daily, 06:00 IST) + Brevo",
        category="engagement",
        workflow=(
            "Read gold_rates_latest.json from the public GOLD_RATES_GIST",
            "Query the Gold & Silver Alerts Brevo list for subscriber count",
            "Write a markdown report: today's national + international rates, change %, subscriber count",
        ),
        inputs=(
            "GOLD_RATES_GIST in .env — public Gist ID with gold_rates_latest.json / gold_rates_history.json",
            "BREVO_API_KEY + BREVO_GOLD_LIST_ID in .env — for subscriber count",
            "GITHUB_TOKEN in .env — optional, raises the unauthenticated Gist read rate limit",
        ),
        outputs=(
            "outputs/gold_rates/<timestamp>-report.md — rates + subscriber snapshot",
            "Dashboard view at /agent/gold_rates (current rates, change %, subscriber count)",
        ),
        rationale=(
            "manikumarjami.com/gold-rates is a programmatic-SEO page (1 hub + 15 city pages) "
            "targeting 'gold rate today <city>' searches, doubling as a newsletter funnel "
            "('Gold & Silver Alerts'). This agent gives the operator one place to confirm the "
            "daily Vercel cron actually ran, rates are fresh, and the alert list is growing — "
            "without logging into Vercel/Brevo/GitHub separately."
        ),
    ),
    FleetEntry(
        name="fleet_health_monitor",
        label="Fleet health monitor",
        short_description="Daily 04:30 UTC sweep of every other agent — emails operator if anything goes red",
        description=(
            "Cloud Routine that audits every other agent in the fleet daily and reports state. "
            "Reads core/fleet_manifest.py to know which agents exist, then for each: checks recent "
            "execution (GitHub commits for Cloud Routines, n8n executions for n8n workflows, last "
            "result email for the labs emailer), verifies integration tokens haven't expired "
            "(Resend ping, Meta Graph /me ping, GitHub /user ping, Langfuse health, n8n workflow "
            "active flag), and checks free-tier quotas (Resend usage, Langfuse observations). "
            "Writes a health report to GitHub. If ANY check goes red — agent silent ≥24h, token "
            "rejected, quota <10%, n8n workflow inactive — sends an alert email to "
            "$OPERATOR_EMAIL via Resend. Otherwise silent."
        ),
        built=False,
        production_status="",
        runtime="cloud routines → github + resend alerts",
        category="ops",
        workflow=(
            "Cloud routine fires daily ~04:30 UTC (after seo_audit + daily_ai_brief have run)",
            "Fetch the current fleet_manifest from the operator's repo — list every agent",
            "Per agent: type-specific freshness check (Cloud Routine = GitHub commit today; n8n = recent successful execution; labs emailer = recent sent row in admin view)",
            "Ping each integration token (Resend /domains, Meta Graph /me, GitHub /user, Langfuse, n8n /workflows)",
            "Pull quota state (Resend usage if exposed, Langfuse observation count)",
            "Write health-monitor/YYYY-MM-DD.md to GitHub with per-agent status + integration status + quota table",
            "If any red flag → send alert email via Resend to $OPERATOR_EMAIL with the specific failure(s)",
        ),
        inputs=(
            "Read access to core/fleet_manifest.py (agent inventory)",
            "n8n API key + base URL (existing)",
            "Resend API key (for alerts + Resend health ping)",
            "Meta Graph token + Langfuse + GitHub tokens (already in .env, exposed to routine)",
        ),
        outputs=(
            "1 markdown health report per day in health-monitor/ on GitHub",
            "Alert email to $OPERATOR_EMAIL ONLY when something is red",
            "Dashboard view at /agent/fleet_health_monitor (latest + 7-day status grid)",
        ),
        rationale=(
            "User is an observer, not an operator (memory: user-role-observer). For the fleet "
            "to be trustable, silent failures must be detected automatically. This is the "
            "guardrail layer: every other agent gets one daily check, and the operator only "
            "gets a notification when action is required."
        ),
    ),
)


def get_entry(name: str) -> FleetEntry:
    for e in FLEET:
        if e.name == name:
            return e
    raise KeyError(f"Unknown agent: {name}")


def grouped_by_category() -> list[tuple[str, dict, list[FleetEntry]]]:
    """Return [(category_key, category_meta, [entries...]), ...] in display order."""
    out: list[tuple[str, dict, list[FleetEntry]]] = []
    for key, meta in CATEGORIES.items():
        entries = [e for e in FLEET if e.category == key and not e.hidden]
        if entries:
            out.append((key, meta, entries))
    return out
