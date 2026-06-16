"""Per-agent metric calculators.

Each agent surfaces a DIFFERENT set of stats on its detail page. The metrics
are derived from the runs + artifacts tables in status_bus plus the meta_json
each agent records on its artifacts.

If an agent records richer meta (counts, categories, etc.), those metrics
will populate automatically. If meta is sparse, only the universal metrics
(run counts, durations) are shown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from core import status_bus


@dataclass(frozen=True)
class Metric:
    label: str          # short label, ALL CAPS in UI
    value: str          # the big number / text
    hint: str = ""      # subtle subtitle below the value
    emphasis: str = ""  # "live" (saffron) | "warn" (sindoor) | "" (default)


# ---------- helpers ----------


def _meta(artifact: dict) -> dict:
    try:
        return json.loads(artifact.get("meta_json") or "{}")
    except json.JSONDecodeError:
        return {}


def _runs(agent: str) -> list[dict]:
    return status_bus.recent_runs(agent_name=agent, limit=500)


def _artifacts(agent: str) -> list[dict]:
    return [a for a in status_bus.recent_artifacts(limit=1000) if a["agent_name"] == agent]


def _completed(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r["status"] == "completed"]


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def _avg_duration(runs: list[dict]) -> float:
    durations = [r["duration_s"] for r in runs if r.get("duration_s")]
    return (sum(durations) / len(durations)) if durations else 0.0


def _success_rate(runs: list[dict]) -> str:
    if not runs:
        return "—"
    finished = [r for r in runs if r["status"] in ("completed", "failed")]
    if not finished:
        return "—"
    ok = sum(1 for r in finished if r["status"] == "completed")
    return f"{round(100 * ok / len(finished))}%"


# ---------- per-agent metric calculators ----------


def _script_writer() -> list[Metric]:
    runs = _runs("script_writer")
    arts = _artifacts("script_writer")
    by_niche: dict[str, int] = {}
    total_out_tokens = 0
    for a in arts:
        m = _meta(a)
        n = m.get("niche", "?")
        by_niche[n] = by_niche.get(n, 0) + 1
        total_out_tokens += m.get("output_tokens", 0)
    top_niche = max(by_niche, key=by_niche.get) if by_niche else "—"
    avg_words = (total_out_tokens // len(arts) * 3 // 4) if arts else 0  # tokens→words rough

    return [
        Metric("Scripts written", str(len(arts)), hint="ready-to-shoot reels"),
        Metric("Pillars covered", str(len(by_niche)), hint=f"top: {top_niche.replace('_', ' ')}"),
        Metric("Avg script length", f"~{avg_words} words" if avg_words else "—", hint="spoken estimate"),
        Metric("Success rate", _success_rate(runs)),
    ]


def _trend_miner() -> list[Metric]:
    runs = _runs("trend_miner")
    arts = _artifacts("trend_miner")
    total_input = 0
    total_top = 0
    niches: set[str] = set()
    for a in arts:
        m = _meta(a)
        total_input += m.get("input_count", 0)
        total_top += m.get("top_n", 0)
        if m.get("niche"):
            niches.add(m["niche"])

    return [
        Metric("Mining runs", str(len(arts)), hint=f"{len(niches)} niche{'s' if len(niches) != 1 else ''} covered"),
        Metric("Candidates scanned", str(total_input), hint="across all sources"),
        Metric("Top picks surfaced", str(total_top), hint="ranked by virality"),
        Metric("Avg run time", _fmt_duration(_avg_duration(runs))),
    ]


def _cold_email_drafter() -> list[Metric]:
    runs = _runs("cold_email_drafter")
    arts = _artifacts("cold_email_drafter")
    unique_targets: set[str] = set()
    fetch_ok = 0
    for a in arts:
        m = _meta(a)
        t = m.get("target_url") or a["title"]
        if t:
            unique_targets.add(t)
        note = (m.get("fetch_note") or "").lower()
        if "fetched" in note:
            fetch_ok += 1

    return [
        Metric("Emails drafted", str(len(arts)), hint=f"3 messages each (initial + 2 follow-ups)"),
        Metric("Unique targets", str(len(unique_targets))),
        Metric("Total messages", str(len(arts) * 3), hint="across full sequences"),
        Metric("Profile fetches", f"{fetch_ok}/{len(arts)}" if arts else "—", hint="public-URL hits"),
    ]


def _citation_check() -> list[Metric]:
    runs = _runs("citation_check")
    arts = _artifacts("citation_check")
    total_claims = sum(_meta(a).get("claim_count", 0) for a in arts)
    high_risk = sum(1 for a in arts if (_meta(a).get("risk_level") or "").lower() == "high")

    return [
        Metric("Scripts checked", str(len(arts)), hint="cheap insurance gate"),
        Metric("Claims flagged", str(total_claims) if total_claims else "—", hint="VERIFY / LAWYER REVIEW"),
        Metric("High-risk reports", str(high_risk), hint="needed lawyer review", emphasis="warn" if high_risk else ""),
        Metric("Avg run time", _fmt_duration(_avg_duration(runs))),
    ]


def _carousel_generator() -> list[Metric]:
    runs = _runs("carousel_generator")
    arts = _artifacts("carousel_generator")
    total_slides = sum(_meta(a).get("slide_count", 0) for a in arts)
    pngs_rendered = sum(1 for a in arts if _meta(a).get("renderer") == "playwright")

    return [
        Metric("Carousels created", str(len(arts))),
        Metric("Total slides", str(total_slides) if total_slides else "—"),
        Metric("PNG-rendered", f"{pngs_rendered}/{len(arts)}" if arts else "—", hint="Playwright vs JSON-only"),
        Metric("Avg run time", _fmt_duration(_avg_duration(runs))),
    ]


def _competitor_watcher() -> list[Metric]:
    runs = _runs("competitor_watcher")
    arts = _artifacts("competitor_watcher")
    accounts: set[str] = set()
    total_posts = 0
    for a in arts:
        m = _meta(a)
        for h in (m.get("accounts") or []):
            accounts.add(str(h).strip("@").lower())
        total_posts += m.get("post_count", 0)

    return [
        Metric("Reports generated", str(len(arts))),
        Metric("Accounts watched", str(len(accounts)) if accounts else "—", hint="unique handles"),
        Metric("Posts analyzed", str(total_posts) if total_posts else "—"),
        Metric("Avg run time", _fmt_duration(_avg_duration(runs))),
    ]


def _comment_dm_responder() -> list[Metric]:
    arts = _artifacts("comment_dm_responder")
    total_comments = 0
    total_public_replies = 0
    total_dms = 0
    cat_counts: dict[str, int] = {}
    for a in arts:
        m = _meta(a)
        total_comments += m.get("comment_count", 0)
        total_public_replies += m.get("public_replies", 0)
        total_dms += m.get("dms_drafted", 0)
        for k, v in (m.get("categories") or {}).items():
            cat_counts[k] = cat_counts.get(k, 0) + v
    qualified = cat_counts.get("qualified_request", 0)
    conversion = (
        f"{round(100 * total_dms / total_comments)}% conv"
        if total_comments else "—"
    )
    return [
        Metric("Comments processed", str(total_comments) if total_comments else "—", hint="across all batches"),
        Metric("Public replies", str(total_public_replies) if total_public_replies else "—", hint="varied phrasing"),
        Metric("DMs drafted", str(total_dms) if total_dms else "—",
               hint=conversion, emphasis="live" if total_dms else ""),
        Metric("Qualified requests", str(qualified), hint="people who asked for the offer",
               emphasis="live" if qualified else ""),
    ]


def _dm_triage() -> list[Metric]:
    runs = _runs("dm_triage")
    arts = _artifacts("dm_triage")
    total_msgs = 0
    total_drafts = 0
    cat_counts: dict[str, int] = {}
    for a in arts:
        m = _meta(a)
        total_msgs += m.get("message_count", 0)
        total_drafts += m.get("draft_count", 0)
        for k, v in (m.get("categories") or {}).items():
            cat_counts[k] = cat_counts.get(k, 0) + v
    leads = cat_counts.get("lead", 0)
    spam = cat_counts.get("spam", 0)
    top_cat = max(cat_counts, key=cat_counts.get) if cat_counts else "—"

    return [
        Metric("Messages classified", str(total_msgs) if total_msgs else "—", hint="across all batches"),
        Metric("Replies drafted", str(total_drafts) if total_drafts else "—", hint="for human approval"),
        Metric("Leads spotted", str(leads), hint="non-spam, non-fan", emphasis="live" if leads else ""),
        Metric("Spam filtered", str(spam), hint=f"top category: {top_cat}"),
    ]


def _topic_curator() -> list[Metric]:
    arts = _artifacts("topic_curator")
    runs = _runs("topic_curator")
    days_since_last = "—"
    if arts:
        last_iso = arts[0]["created_at"]
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(last_iso)
            now = datetime.now(last.tzinfo or timezone.utc)
            delta_h = (now - last).total_seconds() / 3600
            days_since_last = f"{delta_h:.1f}h ago" if delta_h < 48 else f"{int(delta_h/24)}d ago"
        except Exception:
            pass
    completed = [r for r in runs if r["status"] == "completed"]
    fresh = bool(arts) and days_since_last not in ("—",) and "h ago" in days_since_last
    return [
        Metric("Daily pipelines", str(len(arts)) if arts else "—", hint="ranked topic lists produced"),
        Metric("Last run", days_since_last, hint="auto-runs at 07:00",
               emphasis="live" if fresh else ""),
        Metric("Total runs", str(len(runs))),
        Metric("Success rate", f"{round(100 * len(completed) / max(len(runs), 1))}%" if runs else "—"),
    ]


def _template_generator() -> list[Metric]:
    arts = _artifacts("template_generator")
    drafts = status_bus.list_templates(status="draft")
    active = status_bus.list_templates(status="active")
    inactive = status_bus.list_templates(status="inactive")
    return [
        Metric("Batches generated", str(len(arts)) if arts else "—", hint="authoring runs"),
        Metric("Drafts awaiting review", str(len(drafts)),
               hint="review at /templates", emphasis="live" if drafts else ""),
        Metric("Active in Langfuse", str(len(active)),
               hint="rotating in n8n", emphasis="live" if active else ""),
        Metric("Deactivated", str(len(inactive)), hint="kept as history"),
    ]


_CALCULATORS: dict[str, Callable[[], list[Metric]]] = {
    "script_writer": _script_writer,
    "trend_miner": _trend_miner,
    "cold_email_drafter": _cold_email_drafter,
    "citation_check": _citation_check,
    "carousel_generator": _carousel_generator,
    "competitor_watcher": _competitor_watcher,
    "dm_triage": _dm_triage,
    "comment_dm_responder": _comment_dm_responder,
    "template_generator": _template_generator,
    "topic_curator": _topic_curator,
}


def metrics_for(agent_name: str) -> list[Metric]:
    calc = _CALCULATORS.get(agent_name)
    if calc is None:
        return []
    return calc()
