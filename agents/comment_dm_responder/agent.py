"""Comment-to-DM Responder agent.

For each comment on one of your IG posts, drafts a personal DM the user
sends within Instagram's 24-hour reply window. Manual-paste mode in v1;
ready to plug into IG Graph API + n8n later via a stable input shape.

No auto-send. Drafts are for human approval.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from core import langfuse_integration as lf
from core import status_bus
from core.base_agent import BaseAgent, WorkflowStep

from .prompts import build_system_prompt, build_user_message


OUTPUTS_DIR = Path(os.getenv("OUTPUTS_DIR", "./outputs")) / "comment_dms"

_CATEGORY_KEYS = (
    "qualified_request",
    "tangential_question",
    "pure_praise",
    "partnership_inquiry",
    "spam_or_skip",
    "other",
)


def _parse_drafts_from_output(text: str) -> list[dict]:
    """Extract individual (public_reply, dm) pairs from the markdown output.

    Looks for `### Comment N` blocks and extracts From / Comment / Category /
    Public reply / DM draft fields. Robust to whitespace variations; tolerant
    of skipped fields.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if re.match(r"^###\s+Comment\s+\d+", line):
            if current:
                blocks.append("\n".join(current))
                current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))

    drafts: list[dict] = []
    for block in blocks:
        if "### Comment" not in block:
            continue
        sender = ""
        comment_text = ""
        category = ""
        public_reply = ""
        dm = ""

        m = re.search(r"\*\*From:\*\*\s*([^\n]+)", block)
        if m:
            sender = m.group(1).strip().strip("`").strip()
        m = re.search(r"\*\*Comment:\*\*\s*\n?\s*>?\s*([^\n]+)", block)
        if m:
            comment_text = m.group(1).strip()
        m = re.search(r"\*\*Category:\*\*\s*`?(\w+)`?", block)
        if m:
            category = m.group(1).strip().lower()
        m = re.search(r"\*\*Public reply:\*\*\s*\n?\s*>?\s*([^\n]+)", block)
        if m:
            public_reply = m.group(1).strip()
        m = re.search(r"\*\*DM draft:\*\*\s*\n?\s*>?\s*((?:.+\n?)+?)(?=\n\s*-\s*\*\*Voice note:|$)", block)
        if m:
            dm = m.group(1).strip().rstrip("-").strip()
            # If multi-line, strip leading > markers
            dm_lines = [ln.lstrip("> ").strip() for ln in dm.splitlines() if ln.strip()]
            dm = " ".join(dm_lines).strip()
            if dm.startswith("- "):
                dm = ""

        drafts.append({
            "sender": sender,
            "source_text": comment_text,
            "category": category,
            "public_reply": public_reply,
            "dm": dm,
        })
    return drafts


def _parse_comments(blob: str) -> list[dict]:
    """Split comments on `---` separators. Each chunk may start with `from: @handle`.

    Returns: [{"sender": "@handle", "text": "..."}, ...]
    """
    if not blob or not blob.strip():
        return []
    raw_chunks: list[str] = []
    buf: list[str] = []
    for line in blob.splitlines():
        if line.strip() == "---":
            raw_chunks.append("\n".join(buf))
            buf = []
        else:
            buf.append(line)
    raw_chunks.append("\n".join(buf))

    parsed: list[dict] = []
    for chunk in raw_chunks:
        text = chunk.strip()
        if not text:
            continue
        sender = ""
        body_lines: list[str] = []
        consumed_first = False
        for line in text.splitlines():
            if not consumed_first and line.strip().lower().startswith("from:"):
                sender = line.split(":", 1)[1].strip()
                consumed_first = True
                continue
            consumed_first = True
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        parsed.append({"sender": sender, "text": body})
    return parsed


def _format_comments_for_claude(parsed: list[dict]) -> str:
    blocks: list[str] = []
    for idx, c in enumerate(parsed, start=1):
        head = f"# Comment {idx}"
        if c["sender"]:
            head += f"\nfrom: {c['sender']}"
        blocks.append(f"{head}\n{c['text']}")
    return "\n---\n".join(blocks)


def _extract_category_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"\s*[-*]\s*\*?\*?(\w+(?:_\w+)*)\*?\*?\s*:\s*(\d+)", line.strip(), re.IGNORECASE)
        if not m:
            continue
        key = m.group(1).lower()
        if key in _CATEGORY_KEYS:
            counts[key] = int(m.group(2))
    return counts


def _extract_summary_counts(text: str) -> dict[str, int]:
    """Parse the Summary block for public-replies / DMs / skipped counters."""
    out: dict[str, int] = {}
    patterns = {
        "public_replies": r"\*\*?Public replies drafted\*?\*?",
        "dms_drafted":    r"\*\*?DMs drafted\*?\*?",
        "skipped":        r"\*\*?Skipped\*?\*?",
    }
    for line in text.splitlines():
        for key, pat in patterns.items():
            m = re.search(rf"{pat}\s*:\s*(\d+)", line, re.IGNORECASE)
            if m:
                out[key] = int(m.group(1))
    return out


class CommentDMResponderAgent(BaseAgent):
    name = "comment_dm_responder"
    description = (
        "For every comment on your IG post, drafts TWO outputs — a short public "
        "reply (varied across the batch) AND a personal DM that delivers what "
        "the post promised. Native Meta pattern, no scrapers, no auto-send."
    )
    workflow_steps = [
        WorkflowStep(
            "parse_inputs",
            "Parse post + payload + comments",
            "Reads post context, the DM payload to deliver, and the comments blob.",
        ),
        WorkflowStep(
            "build_context",
            "Frame batch for Claude",
            "Aligns DM tone with post topic and confirms what's being delivered.",
        ),
        WorkflowStep(
            "claude_draft_pair",
            "Draft public reply + DM per comment",
            "Single Claude call produces both outputs for every commenter.",
        ),
        WorkflowStep(
            "save_artifact",
            "Save approval batch",
            "Writes the markdown bundle to outputs/comment_dms/.",
        ),
    ]

    def _run(
        self,
        task: str,
        post_context: str,
        dm_payload: str,
        comments: str,
    ) -> dict:
        self.step("parse_inputs")
        parsed = _parse_comments(comments)
        self.log(f"Parsed {len(parsed)} comment(s).")
        if not parsed:
            self.log("No comments parsed — output will be a near-empty stub.", level="warning")
        if not dm_payload.strip():
            self.log(
                "No DM payload provided — DMs will open conversation only without delivering an asset.",
                level="warning",
            )

        self.step("build_context", detail=f"{len(parsed)} comments")
        comments_block = _format_comments_for_claude(parsed)
        user_msg = build_user_message(post_context, dm_payload, comments_block)

        self.step("claude_draft_pair")
        system, src, prompt_obj = lf.get_prompt(
            "comment_dm_responder.system",
            fallback=build_system_prompt(),
        )
        self.log(f"System prompt source: {src}")
        result = self.claude.complete(
            system=system,
            user_message=user_msg,
            max_tokens=4500,
            temperature=0.85,
            trace_name="comment_dm_responder.run",
            agent_name=self.name,
            trace_metadata={
                "comment_count": len(parsed),
                "payload_provided": bool(dm_payload.strip()),
                "prompt_source": src,
            },
            trace_tags=["engagement"],
            trace_prompt=prompt_obj,
        )
        self.log(
            f"Drafted {len(result.text)} chars for {len(parsed)} comment(s) · "
            f"in={result.input_tokens} (cached={result.cache_read_tokens}) "
            f"out={result.output_tokens}"
        )

        self.step("save_artifact")
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = OUTPUTS_DIR / f"{timestamp}-batch.md"

        header = (
            f"# Comment → Reply + DM Batch — {timestamp}\n\n"
            f"- **Comments processed:** {len(parsed)}\n"
            f"- **Post context:** {(post_context or '(none)').strip()[:200]}\n"
            f"- **DM payload:** {(dm_payload or '(none)').strip()[:200]}\n"
            f"- **Mode:** manual-paste (v1). Send each reply + DM within IG's 24-hour reply window.\n\n"
            "> Drafts are SUGGESTIONS for approval. Review every one before sending.\n"
            "> No unofficial scrapers used. When wired via official Meta Graph API + n8n,\n"
            "> approval will trigger sends through Meta's Private Replies endpoint.\n\n"
            "---\n\n"
        )
        out_path.write_text(header + result.text)

        categories = _extract_category_counts(result.text)
        summary = _extract_summary_counts(result.text)
        spam_n = categories.get("spam_or_skip", 0)
        public_replies = summary.get("public_replies", max(0, len(parsed) - spam_n))
        dms_drafted = summary.get("dms_drafted", 0)
        skipped = summary.get("skipped", spam_n)

        artifact_id = self.record_artifact(
            kind="comment_dm_batch",
            title=f"Comment→DM — {len(parsed)} comment(s)",
            path=str(out_path),
            meta={
                "comment_count": len(parsed),
                "public_replies": public_replies,
                "dms_drafted": dms_drafted,
                "skipped": skipped,
                "categories": categories,
                "model": result.model,
                "output_tokens": result.output_tokens,
                "prompt_source": src,
            },
        )
        self.log(f"Saved → {out_path.name}", data={"artifact_id": artifact_id})

        # Persist each draft as its own approvable row in the drafts queue.
        extracted = _parse_drafts_from_output(result.text)
        draft_ids: list[int] = []
        for d in extracted:
            if d["category"] == "spam_or_skip":
                continue  # don't queue spam — Claude already skipped these
            if d["public_reply"] and d["public_reply"] != "(skip)":
                draft_ids.append(status_bus.add_draft(
                    agent_name=self.name,
                    kind="public_reply",
                    body=d["public_reply"],
                    artifact_id=artifact_id,
                    target_handle=d["sender"],
                    source_text=d["source_text"],
                    category=d["category"],
                    meta={"post_context": post_context[:200]},
                ))
            if d["dm"] and not d["dm"].lower().startswith("(no dm") and d["dm"] != "(skip)":
                draft_ids.append(status_bus.add_draft(
                    agent_name=self.name,
                    kind="dm",
                    body=d["dm"],
                    artifact_id=artifact_id,
                    target_handle=d["sender"],
                    source_text=d["source_text"],
                    category=d["category"],
                    meta={"post_context": post_context[:200]},
                ))
        if draft_ids:
            self.log(f"Queued {len(draft_ids)} draft(s) for approval at /queue")

        return {
            "path": str(out_path),
            "artifact_id": artifact_id,
            "comment_count": len(parsed),
            "public_replies": public_replies,
            "dms_drafted": dms_drafted,
            "draft_ids": draft_ids,
        }
