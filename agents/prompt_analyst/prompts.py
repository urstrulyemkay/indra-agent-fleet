"""Prompts for the Prompt Analyst agent."""

from __future__ import annotations


CATEGORY_DESCRIPTIONS = {
    "vague": "Short or context-free instructions (e.g. 'fix it', 'run this', 'check')",
    "feature_request": "New capability asks ('I need X', 'add Y', 'build Z')",
    "bug_report": "Something is broken or not working as expected",
    "question": "Exploratory / explanatory ('what is', 'why', 'how does', 'explain')",
    "correction": "Redirect or undo ('no not that', 'wrong', 'revert', 'stop doing X')",
    "context_dump": "Large paste or data dump with no clear ask (>400 chars, no action verb)",
    "approval": "One-word or short confirms ('yes', 'ok', 'good', 'proceed', 'looks good')",
    "scope_creep": "Mid-task expansion ('also', 'and one more thing', 'while you're at it')",
}


def build_system_prompt() -> str:
    return """You are a prompt-engineering coach analysing how a developer uses Claude Code across multiple projects. Your job is to be specific, honest, and actionable — not flattering.

You will receive a structured JSON summary of the user's last 4 weeks of messages: category breakdowns by week, length distributions, most-repeated phrases, and cross-project patterns.

Produce a single markdown report with these sections:

## Summary
2–3 sentence executive summary of the biggest patterns — what the user does well and the single biggest friction point.

## Weekly Metrics
A brief narrative for each week (Week 1 = oldest, Week 4 = most recent) — what changed, what regressed.

## Category Breakdown
One paragraph per category that showed up in >5% of messages. Be specific: quote short real examples where provided. Explain *why* that pattern costs tokens or slows down execution.

## Efficiency Diagnosis
Pick the top 3 inefficiencies (ranked by impact). For each:
- Pattern name
- What it costs (token waste, rework loops, context dilution)
- Concrete fix with an example before/after rewrite

## Prompt Practices to Keep
2–3 things the user already does well (honest — only if they actually appear in the data).

## Week-by-Week Metric Table
| Week | Total | Vague | Feature | Bug | Question | Correction | Approval | Scope | Dump |
|------|-------|-------|---------|-----|----------|------------|----------|-------|------|
(fill in counts)

## Action Items
Numbered list of the top 5 changes to make — specific, ordered by impact."""


def build_user_message(analysis_json: str, sample_messages: str) -> str:
    return f"""Here is the structured 4-week prompt analysis for this user across all Claude Code projects:

```json
{analysis_json}
```

Sample messages per category (real quotes, truncated):
{sample_messages}

Write the full coaching report now. Be specific and direct — the user asked for honest analysis, not encouragement."""
