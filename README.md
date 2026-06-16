# Fleet — Personal Brand Content Agents

A fleet of specialized Claude agents for content creation and outbound across three pillars:
**behavioral psychology**, **technology**, and **legal tech**.

## Fleet status

| Agent | Purpose | Status |
|---|---|---|
| `script_writer` | Hook→tension→value→CTA video scripts in your psychology pipeline format. | ✅ Built |
| `trend_miner` | Per-niche topic candidates from Reddit / HN / ArXiv / legal-tech RSS, scored by Claude. | ✅ Built |
| `cold_email_drafter` | One target → research + initial email + 2 follow-ups, ready to copy into Gmail. | ✅ Built |
| `competitor_watcher` | Tracks IG account hook patterns per niche. | ⏳ Roadmap |
| `carousel_generator` | Script → 5–10 slide IG carousel PNGs (Playwright). | ⏳ Roadmap |
| `citation_check` | Verifies legal-tech and claim-heavy psych scripts; flags risky claims. | ⏳ Roadmap |
| `dm_triage` | IG Graph API → classify DMs/comments → draft replies for approval. | ⏳ Roadmap |

## Dashboard

The dashboard now has two levels:

- **`/`** — fleet overview. Every agent (built + roadmap) as a card with live status. Click any built agent to open its detail page.
- **`/agent/<name>`** — per-agent detail. Live **workflow pipeline** (current step highlighted), run form, filtered activity feed, and recent artifacts for that agent only.

The workflow view lets you see exactly what each agent does step-by-step. If you want to suggest an improvement ("step 3 should also do X"), open the detail page, point at a step, and tell the assistant.

## Quick start

```bash
cd "Agents_end to end"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY
```

### Run the dashboard

```bash
python -m dashboard.app
# open http://127.0.0.1:8765
```

### Run agents from CLI (also reflected in the dashboard)

```bash
# Script Writer
python -m agents.script_writer \
  --niche behavioral_psychology \
  --topic "Why your brain rewards anxiety more than calm" \
  --length 60

# Trend Miner — one niche
python -m agents.trend_miner --niche technology --top 10

# Trend Miner — all niches in one go
python -m agents.trend_miner --niche all --top 8

# Cold Email Drafter
python -m agents.cold_email_drafter \
  --name "Jane Doe" \
  --url "https://www.linkedin.com/in/janedoe" \
  --paste-file ./target_profile.txt \
  --offer "I'd love your read on a 60s reel I'm scripting about <topic>" \
  --context "I run a small content brand on behavioral psych + legal tech" \
  --cta "5-min DM exchange — no calendar"
```

Outputs land in `outputs/<kind>/<timestamp>-<slug>.md`.

## Architecture

```
core/
  claude_client.py     ← Anthropic SDK wrapper with prompt caching
  status_bus.py        ← SQLite-backed status + events + artifacts
  base_agent.py        ← BaseAgent with WorkflowStep + step() helper
  niche_config.py      ← Per-pillar voice profile, hooks, accuracy bar
  fleet_manifest.py    ← Every agent's metadata (built + roadmap)

agents/
  script_writer/       ← Built
  trend_miner/         ← Built (sources.py has Reddit/HN/ArXiv/RSS fetchers)
  cold_email_drafter/  ← Built

dashboard/
  app.py               ← FastAPI + HTMX + Tailwind, per-agent detail pages
  templates/
    index.html              ← fleet overview
    agent_detail.html       ← per-agent: workflow + form + feed + artifacts
    _fleet_overview.html    ← partial: agent cards
    _workflow_pipeline.html ← partial: live step-by-step view
    _artifacts_list.html    ← partial: artifacts list
```

### Why these choices (still)

- **Anthropic SDK over `claude-agent-sdk`**: lighter, no Claude Code subprocess on VPS, native prompt caching. Easy to swap if we later need agent-loop semantics.
- **SQLite for the status bus**: zero setup, adequate at light cadence. The dashboard polls + SSE — fine for fleet of 10–20 agents.
- **HTMX + Tailwind via CDN**: zero build step, the entire dashboard is 5 small files.
- **Workflow steps declared on the class**: each agent owns its own pipeline definition. The dashboard renders whatever the class says. No drift between manifest and reality.

## Cold email — important context

The `cold_email_drafter` agent is **deliberately a drafter, not a sender**:

- **Sending at scale is the one place you'll need to pay** (~$40/mo, Smartlead or Instantly). Free-tier transactional services (Resend, Brevo, Mailgun) ban cold email under their AUP and will close your account on the first complaint.
- **Below ~10 sends/day**, the free path works: this agent → copy → paste into your Gmail → manual send. Track with Streak free or Mailtrack free.
- **The agent will not auto-fetch LinkedIn behind the login wall** — LinkedIn returns thin HTML to anonymous requests. Paste the profile content into the form and you'll get drastically better drafts.
- **The agent never fabricates signals.** If inputs are too thin, the research summary will say so and the email will be honest about it.

## Adding a new agent

1. Create `agents/<name>/agent.py` subclassing `core.base_agent.BaseAgent`.
2. Declare `name`, `description`, and `workflow_steps = [WorkflowStep(key, label, description), ...]`.
3. Implement `_run(self, task, **kwargs)`. Call `self.step("key")` at each pipeline boundary so the dashboard shows progress.
4. Use `self.log(...)`, `self.record_artifact(...)` as needed.
5. Add a CLI entry in `agents/<name>/__main__.py`.
6. Register a `FleetEntry` in `core/fleet_manifest.py` with `built=True`.
7. Add the class to `AGENT_CLASSES` in `dashboard/app.py` and add a POST `/api/run/<name>` route + form block in `agent_detail.html`.

The dashboard picks up workflow_steps automatically from the class.

## Constraints

- **Free / OSS only** beyond Claude itself. No paid integrations.
- **Light cadence**: 3–5 posts/week, 1 platform (Instagram) in v1.
- **Approval gates on everything**: agents draft, you approve.
- **No Instagram auto-reply** via unofficial libraries — ban risk. Use Graph API draft-for-approval only.
- **No cold-email auto-send.** Drafter outputs markdown; you copy into Gmail manually.
