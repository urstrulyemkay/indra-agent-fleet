"""Langfuse integration — observability + prompt management.

Two-way safe:
- If LANGFUSE_* env vars are missing or Langfuse is unreachable, every helper
  returns sane fallbacks. The fleet keeps working with the hardcoded prompts
  and untraced Claude calls.
- If Langfuse is configured, every Claude call is traced (input/output/usage)
  and prompts can be sourced from Langfuse instead of code.

Usage in agents:

    from core.langfuse_integration import get_prompt

    system = get_prompt("script_writer.system", fallback=build_system_prompt(niche))
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("fleet.langfuse")

_lf_client: Any = None
_init_attempted: bool = False


def _client() -> Any | None:
    """Lazy singleton. Returns None if Langfuse isn't configured or import fails."""
    global _lf_client, _init_attempted
    if _init_attempted:
        return _lf_client
    _init_attempted = True

    public = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").strip()

    if not public or not secret:
        logger.info("Langfuse keys unset — running without tracing.")
        return None

    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning("langfuse not installed — pip install langfuse")
        return None

    try:
        _lf_client = Langfuse(public_key=public, secret_key=secret, host=host)
        logger.info(f"Langfuse connected → {host}")
    except Exception as exc:
        logger.warning(f"Langfuse init failed: {exc}")
        _lf_client = None

    return _lf_client


def is_enabled() -> bool:
    return _client() is not None


def get_status() -> dict:
    """Snapshot for the dashboard health card."""
    public = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").strip()
    return {
        "configured": bool(public),
        "connected": is_enabled(),
        "host": host,
    }


def get_prompt(name: str, fallback: str) -> tuple[str, str, Any]:
    """Return (prompt_text, source, prompt_obj).

    source ∈ {"langfuse", "fallback"}.
    prompt_obj is the Langfuse TextPromptClient (for linking to generations
    in traces), or None when falling back to code.
    Best-practice convention: always fetch label="production" for stability.
    """
    client = _client()
    if client is None:
        return fallback, "fallback", None
    try:
        p = client.get_prompt(name, label="production")
        text = p.prompt if hasattr(p, "prompt") else str(p)
        return text, "langfuse", p
    except Exception as exc:
        logger.info(f"Prompt '{name}' not found in Langfuse ({exc}) — using fallback.")
        return fallback, "fallback", None


@dataclass
class TraceContext:
    """Returned by `start_trace`. Pass to `finish_trace` after the LLM call."""
    trace: Any
    generation: Any
    enabled: bool


def start_trace(
    *,
    name: str,
    agent_name: str,
    model: str,
    system: str,
    user_message: str,
    metadata: dict | None = None,
    tags: list[str] | None = None,
    prompt: Any = None,
) -> TraceContext:
    """Start a Langfuse trace + generation for an LLM call.

    Pass `prompt` (a Langfuse prompt object from get_prompt) to link the
    specific prompt version to the generation. This shows up in the trace UI.
    """
    client = _client()
    if client is None:
        return TraceContext(trace=None, generation=None, enabled=False)
    try:
        trace = client.trace(
            name=name,
            metadata={"agent": agent_name, **(metadata or {})},
            tags=list({agent_name, *(tags or [])}),
        )
        gen_kwargs = dict(
            name=f"{agent_name}.completion",
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
        )
        if prompt is not None:
            gen_kwargs["prompt"] = prompt
        gen = trace.generation(**gen_kwargs)
        return TraceContext(trace=trace, generation=gen, enabled=True)
    except Exception as exc:
        logger.info(f"Langfuse start_trace failed: {exc}")
        return TraceContext(trace=None, generation=None, enabled=False)


def finish_trace(
    ctx: TraceContext,
    *,
    output: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    stop_reason: str = "",
    error: str | None = None,
) -> None:
    if not ctx.enabled or ctx.generation is None:
        return
    try:
        ctx.generation.end(
            output=output,
            usage={
                "input": input_tokens,
                "output": output_tokens,
                "input_cached": cache_read_tokens,
            },
            metadata={"stop_reason": stop_reason},
            level="ERROR" if error else "DEFAULT",
            status_message=error or None,
        )
    except Exception as exc:
        logger.info(f"Langfuse finish_trace failed: {exc}")


def flush() -> None:
    """Force-flush pending observations. Call before process exit."""
    client = _client()
    if client is not None:
        try:
            client.flush()
        except Exception:
            pass


def push_prompt(
    name: str,
    prompt: str,
    *,
    labels: list[str] | None = None,
    config: dict | None = None,
) -> tuple[bool, str]:
    """Generic prompt publisher for dynamic content (auto-send templates etc).

    Used by the template library: every time the active set of templates for a
    (category, niche, kind) changes, we re-publish the JSON array to Langfuse
    under a stable name. n8n fetches by name at send time and picks one randomly.
    """
    client = _client()
    if client is None:
        return False, "Langfuse not configured"
    try:
        client.create_prompt(
            name=name,
            prompt=prompt,
            labels=labels or ["production"],
            config=config or {},
        )
        return True, f"pushed ({len(prompt)} chars)"
    except Exception as exc:
        return False, f"push failed: {exc}"
