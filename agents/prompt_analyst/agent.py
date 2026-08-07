"""Prompt Analyst agent.

Reads ~/.claude/projects/*/  *.jsonl files, extracts user messages from the last
N weeks, categorises them, computes weekly metrics, and asks Claude to produce
a coaching report on prompt efficiency and better practices.

CLI:
  python -m agents.prompt_analyst --weeks 4
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.base_agent import BaseAgent, WorkflowStep
from core import langfuse_integration as lf

from .prompts import build_system_prompt, build_user_message, CATEGORY_DESCRIPTIONS


# ── Category heuristics ───────────────────────────────────────────────────────

def _categorise(text: str) -> str:
    t = text.strip().lower()
    words = t.split()
    n = len(words)

    # Approval: very short affirmatives
    if n <= 5 and re.search(r'\b(yes|ok|okay|good|great|perfect|done|proceed|sure|go ahead|looks good|nice|continue|got it|thanks|thank you|sounds good)\b', t):
        return "approval"

    # Correction: explicit redirect / undo signals
    if re.search(r'\b(no[,. ]|not that|wrong|stop doing|don\'t|revert|undo|that\'s not|that is not|i said|actually|instead)\b', t):
        return "correction"

    # Bug report
    if re.search(r'\b(not working|not showing|broken|error|fail|issue|bug|exception|crash|doesn\'t work|does not work|not loading|not fetching|wrong result|incorrect)\b', t):
        return "bug_report"

    # Question
    if re.search(r'\b(what|why|how|where|which|when|explain|tell me|what is|what are|what does|can you explain)\b', t) and '?' in text:
        return "question"

    # Scope creep: additive mid-task expansions
    if n > 3 and re.search(r'^(also|and also|one more|while you\'re|by the way|additionally|btw|oh and|add one more|and also please)', t):
        return "scope_creep"

    # Context dump: long paste with no clear action
    if len(text) > 400 and not re.search(r'\b(add|build|fix|create|update|change|make|remove|rename|show|send|run|deploy|write|generate|improve|help|check|get|fetch|push|pull)\b', t):
        return "context_dump"

    # Feature request: action verb + new thing
    if re.search(r'\b(add|build|create|implement|make|generate|write|develop|set up|wire|hook|integrate|new|need|want)\b', t):
        return "feature_request"

    # Vague: short with no clear intent
    if n <= 8:
        return "vague"

    return "feature_request"  # long explicit asks default here


# ── JSONL reader ──────────────────────────────────────────────────────────────

def _load_messages(weeks: int = 4, projects_dir: Path | None = None) -> list[dict]:
    """Return all user messages from the last `weeks` weeks across all projects."""
    if projects_dir is None:
        projects_dir = Path(os.path.expanduser("~/.claude/projects"))

    cutoff_dt = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    cutoff = cutoff_dt.isoformat()
    msgs: list[dict] = []

    if not projects_dir.exists():
        return msgs

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        project_raw = project_dir.name
        # Derive a readable project label
        project_label = project_raw.replace("-Users-EmkayJami-Desktop-claude-projects-", "").replace("-", " ").strip() or project_raw

        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") != "user":
                            continue
                        ts = obj.get("timestamp", "")
                        if not ts or ts < cutoff:
                            continue
                        content = obj.get("message", {}).get("content", "")
                        texts: list[str] = []
                        if isinstance(content, str):
                            texts = [content.strip()]
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    texts.append(c.get("text", "").strip())
                        for text in texts:
                            # Skip system injections and very short noise
                            if not text or len(text) < 8:
                                continue
                            if text.startswith("This session is being continued"):
                                continue
                            if text.startswith("<ide_") or text.startswith("<system"):
                                continue
                            msgs.append({
                                "project": project_label,
                                "ts": ts,
                                "date": ts[:10],
                                "text": text,
                                "length": len(text),
                            })
            except Exception:
                continue

    return msgs


# ── Analytics ─────────────────────────────────────────────────────────────────

def _week_index(date_str: str, now: datetime) -> int:
    """0 = current week, 1 = last week, … 3 = 4 weeks ago."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return -1
    delta = (now.date() - d).days
    return delta // 7


def _analyse(msgs: list[dict], weeks: int = 4) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    # Attach categories and week indices
    for m in msgs:
        m["category"] = _categorise(m["text"])
        m["week_idx"] = _week_index(m["date"], now)

    # Filter to valid weeks
    msgs = [m for m in msgs if 0 <= m["week_idx"] < weeks]

    # ── Weekly breakdown ──────────────────────────────────────────────────────
    cats = list(CATEGORY_DESCRIPTIONS.keys())
    weekly: list[dict] = []
    for w in range(weeks - 1, -1, -1):  # oldest first
        week_msgs = [m for m in msgs if m["week_idx"] == w]
        by_cat = defaultdict(int)
        for m in week_msgs:
            by_cat[m["category"]] += 1
        total_w = len(week_msgs)
        avg_len = int(sum(m["length"] for m in week_msgs) / max(total_w, 1))

        correction_pct_w = (by_cat["correction"] / total_w * 100) if total_w else 0.0
        vague_pct_w = (by_cat["vague"] / total_w * 100) if total_w else 0.0
        efficiency_w = round(100 - correction_pct_w - vague_pct_w / 2) if total_w else None

        end_date = now.date() - timedelta(days=w * 7)
        start_date = end_date - timedelta(days=6)

        weekly.append({
            "week_label": f"Week {weeks - w}",
            "week_idx": w,
            "date_range": f"{start_date.strftime('%b %-d')} – {end_date.strftime('%b %-d')}",
            "start_date": str(start_date),
            "end_date": str(end_date),
            "total": total_w,
            "avg_length": avg_len,
            "by_category": {c: by_cat[c] for c in cats},
            "correction_pct": round(correction_pct_w, 1),
            "vague_pct": round(vague_pct_w, 1),
            "efficiency_score": efficiency_w,
        })

    # ── Overall category counts ───────────────────────────────────────────────
    overall_by_cat: dict[str, int] = defaultdict(int)
    for m in msgs:
        overall_by_cat[m["category"]] += 1

    # ── Project breakdown ─────────────────────────────────────────────────────
    by_project: dict[str, dict] = defaultdict(lambda: {"total": 0, "by_category": defaultdict(int)})
    for m in msgs:
        by_project[m["project"]]["total"] += 1
        by_project[m["project"]]["by_category"][m["category"]] += 1

    # ── Samples per category (up to 4 real messages, short) ──────────────────
    samples: dict[str, list[str]] = defaultdict(list)
    for m in msgs:
        c = m["category"]
        if len(samples[c]) < 4:
            samples[c].append(m["text"][:180])

    # ── Length distribution ───────────────────────────────────────────────────
    lengths = [m["length"] for m in msgs]
    length_dist = {
        "under_20_chars": sum(1 for l in lengths if l < 20),
        "20_to_100": sum(1 for l in lengths if 20 <= l < 100),
        "100_to_300": sum(1 for l in lengths if 100 <= l < 300),
        "300_to_1000": sum(1 for l in lengths if 300 <= l < 1000),
        "over_1000": sum(1 for l in lengths if l >= 1000),
    }

    total_all = len(msgs)
    overall_correction_pct = (overall_by_cat["correction"] / total_all * 100) if total_all else 0.0
    overall_vague_pct = (overall_by_cat["vague"] / total_all * 100) if total_all else 0.0
    overall_efficiency = round(100 - overall_correction_pct - overall_vague_pct / 2) if total_all else None

    week_scores = [w["efficiency_score"] for w in weekly if w["efficiency_score"] is not None]
    trend = None
    if len(week_scores) >= 2:
        trend = week_scores[-1] - week_scores[0]  # latest week vs oldest week

    return {
        "total_messages": len(msgs),
        "weeks_analysed": weeks,
        "weekly": weekly,
        "overall_by_category": dict(overall_by_cat),
        "overall_efficiency_score": overall_efficiency,
        "efficiency_trend": trend,
        "by_project": {k: {"total": v["total"], "by_category": dict(v["by_category"])} for k, v in by_project.items()},
        "length_distribution": length_dist,
        "avg_length_chars": int(sum(lengths) / max(len(lengths), 1)),
        "samples": dict(samples),
    }


def _format_samples(samples: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for cat, msgs in samples.items():
        if not msgs:
            continue
        lines.append(f"\n**{cat}**")
        for m in msgs:
            preview = m.replace("\n", " ")[:160]
            lines.append(f'  - "{preview}"')
    return "\n".join(lines)


# ── Agent ─────────────────────────────────────────────────────────────────────

class PromptAnalystAgent(BaseAgent):
    name = "prompt_analyst"
    description = (
        "Reads your Claude Code session JSONL files, categorises every user "
        "message from the last N weeks, computes weekly efficiency metrics, "
        "and generates a coaching report with concrete recommendations."
    )
    workflow_steps = [
        WorkflowStep(
            key="load",
            label="Load sessions",
            description="Scan ~/.claude/projects for JSONL session files within the time window.",
        ),
        WorkflowStep(
            key="categorise",
            label="Categorise messages",
            description="Classify each user message: feature request, bug report, vague, correction, etc.",
        ),
        WorkflowStep(
            key="analyse",
            label="Compute weekly metrics",
            description="Aggregate counts, length distributions, and per-project breakdowns by week.",
        ),
        WorkflowStep(
            key="coach",
            label="Generate coaching report",
            description="Ask Claude to produce a detailed efficiency diagnosis and action plan.",
        ),
        WorkflowStep(
            key="save",
            label="Save report",
            description="Write the markdown report to outputs/prompt_analyst/.",
        ),
    ]

    def _run(self, task: str, **kwargs) -> str:
        weeks: int = int(kwargs.get("weeks", 4))
        projects_dir: Path | None = kwargs.get("projects_dir")

        self.set_status("running", current_task=f"Analysing {weeks} weeks of prompt history")

        # ── Step 1: Load ──────────────────────────────────────────────────────
        self.step("load")
        msgs = _load_messages(weeks=weeks, projects_dir=projects_dir)
        self.log(f"Loaded {len(msgs)} user messages across {weeks} weeks")
        if not msgs:
            raise RuntimeError("No user messages found in ~/.claude/projects. Check the path.")

        # ── Step 2: Categorise ────────────────────────────────────────────────
        self.step("categorise")
        analysis = _analyse(msgs, weeks=weeks)
        n_projects = len(analysis["by_project"])
        self.log(f"Categorised {analysis['total_messages']} messages across {n_projects} projects")

        # ── Step 3: Analyse ───────────────────────────────────────────────────
        self.step("analyse")
        analysis_json = json.dumps(analysis, indent=2, default=str)
        samples_text = _format_samples(analysis["samples"])
        self.log(
            f"Weekly totals: " + " | ".join(
                f"{w['week_label']}: {w['total']}" for w in analysis["weekly"]
            )
        )

        # ── Step 4: Coach ─────────────────────────────────────────────────────
        self.step("coach")
        system, src, prompt_obj = lf.get_prompt(
            "prompt_analyst.system",
            fallback=build_system_prompt(),
        )
        user_msg = build_user_message(analysis_json, samples_text)

        result = self.claude.complete(
            system=system,
            user_message=user_msg,
            max_tokens=4096,
            trace_name="prompt_analyst.coaching_report",
            agent_name=self.name,
            trace_tags=["prompt_analyst", "coaching"],
            trace_prompt=prompt_obj,
        )
        report = result.text if hasattr(result, "text") else str(result)
        self.log("Coaching report generated")

        # ── Step 5: Save ──────────────────────────────────────────────────────
        self.step("save")
        out_dir = Path(os.getenv("OUTPUTS_DIR", "./outputs")) / "prompt_analyst"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"{ts}-coaching-report.md"

        header = (
            f"# Prompt Efficiency Coaching Report\n"
            f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  \n"
            f"**Scope:** {weeks} weeks · {analysis['total_messages']} messages · {n_projects} projects\n\n"
        )
        out_path.write_text(header + report, encoding="utf-8")

        artifact_id = self.record_artifact(
            "coaching_report",
            f"Prompt coaching {ts}",
            str(out_path),
            meta={"weeks": weeks, "total_messages": analysis["total_messages"]},
        )
        self.set_status("idle")
        self.log(f"Report saved → {out_path}", level="success")

        return report
