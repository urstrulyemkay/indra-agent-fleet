"""Central registry of every system prompt in the fleet.

Currently the fleet has only the Instagram auto-comment and DM responder
running on n8n with pure template rotation (no LLM in the loop). This
registry exists for future Claude-driven agents — it's empty right now.

Usage (when prompts exist):
    python -m core.prompt_registry --list
    python -m core.prompt_registry --push all
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable

from dotenv import load_dotenv

from agents.job_hunter.prompts import build_system_prompt as _job_hunter_system
from core import langfuse_integration as lf


@dataclass(frozen=True)
class PromptDef:
    name: str
    agent: str
    variant: str          # niche key or "default"
    description: str


# variant is unused for prompts that don't fan out across niches; the builder
# just ignores the value. Keep the signature so future per-niche prompts plug
# in cleanly.
_BUILDERS: dict[str, Callable[[str], str]] = {
    "job_hunter": lambda _variant: _job_hunter_system(),
}


def _generate_registry() -> list[PromptDef]:
    """Return all prompt definitions for Claude-driven agents."""
    return [
        PromptDef(
            name="job_hunter.system",
            agent="job_hunter",
            variant="default",
            description=(
                "Job hunter — ranks Apify-scraped LinkedIn jobs against the master profile "
                "and writes a 2-page tailored CV markdown per top match. Enforces the hard "
                "CV rules from cv builder/CLAUDE.md."
            ),
        ),
    ]


REGISTRY: list[PromptDef] = _generate_registry()


def by_name(name: str) -> PromptDef:
    for p in REGISTRY:
        if p.name == name:
            return p
    raise KeyError(f"Unknown prompt: {name}")


def by_agent(agent: str) -> list[PromptDef]:
    return [p for p in REGISTRY if p.agent == agent]


def build_fallback(p: PromptDef) -> str:
    """Return the current code-fallback text for this prompt."""
    builder = _BUILDERS.get(p.agent)
    if builder is None:
        raise KeyError(f"No builder registered for agent: {p.agent}")
    return builder(p.variant)


def push_to_langfuse(p: PromptDef) -> tuple[bool, str]:
    """Push a prompt's current fallback text to Langfuse as label='production'."""
    fallback = build_fallback(p)
    return lf.push_prompt(p.name, fallback, label="production")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Indra prompt registry")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="List all prompts in the registry")
    g.add_argument("--show", metavar="NAME", help="Show a prompt's fallback text")
    g.add_argument("--push", metavar="NAME", help="Push a prompt to Langfuse (use 'all' for everything)")
    args = parser.parse_args()

    if args.list:
        if not REGISTRY:
            print("(no prompts registered — fleet currently runs zero LLM-driven agents)")
            return 0
        for p in REGISTRY:
            print(f"{p.name}  ·  {p.description}")
        return 0

    if args.show:
        try:
            p = by_name(args.show)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(build_fallback(p))
        return 0

    if args.push:
        targets = REGISTRY if args.push == "all" else [by_name(args.push)]
        for p in targets:
            ok, msg = push_to_langfuse(p)
            status = "✓" if ok else "✗"
            print(f"{status} {p.name}: {msg}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
