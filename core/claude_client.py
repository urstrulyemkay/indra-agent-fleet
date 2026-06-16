"""Thin wrapper around the Anthropic SDK with prompt caching baked in.

Using the official `anthropic` SDK rather than `claude-agent-sdk`:
- No Claude Code subprocess dependency (cleaner for VPS deployment)
- Native prompt caching control
- Lighter footprint
- We layer agent-like abstractions ourselves where needed

If you later want full agent-loop semantics (tool use, sub-agents, hooks),
swap this file for a claude-agent-sdk-based client without touching agents.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from . import langfuse_integration as lf


DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-7")
DEFAULT_MAX_TOKENS = 8000


@dataclass
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    model: str
    stop_reason: str


class ClaudeClient:
    def __init__(self, model: str = DEFAULT_MODEL):
        self._client = Anthropic()
        self.model = model

    def complete(
        self,
        system: str | list[dict[str, Any]],
        user_message: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 1.0,
        cache_system: bool = True,
        trace_name: str = "claude.complete",
        agent_name: str = "fleet",
        trace_metadata: dict | None = None,
        trace_tags: list[str] | None = None,
        trace_prompt: Any = None,
    ) -> CompletionResult:
        if isinstance(system, str):
            system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        else:
            system_blocks = list(system)

        if cache_system and system_blocks:
            system_blocks[-1] = {
                **system_blocks[-1],
                "cache_control": {"type": "ephemeral"},
            }

        # Langfuse trace (no-op if not configured)
        trace_ctx = lf.start_trace(
            name=trace_name,
            agent_name=agent_name,
            model=self.model,
            system=system_blocks[-1].get("text", "") if system_blocks else "",
            user_message=user_message,
            metadata=trace_metadata,
            tags=trace_tags,
            prompt=trace_prompt,
        )

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_blocks,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            lf.finish_trace(
                trace_ctx, output="", input_tokens=0, output_tokens=0, error=str(exc)
            )
            raise

        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        usage = response.usage
        result = CompletionResult(
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            model=response.model,
            stop_reason=response.stop_reason or "",
        )

        lf.finish_trace(
            trace_ctx,
            output=text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            stop_reason=result.stop_reason,
        )
        return result
