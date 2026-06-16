"""Base class every agent in the fleet inherits from.

Handles status reporting, event logging, artifact recording, and the
workflow-step concept so the dashboard always knows what's happening —
without each agent reimplementing it.

Subclasses declare `description` and `workflow_steps` so the dashboard
can render a live pipeline view per agent.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any

from . import status_bus
from .claude_client import ClaudeClient


@dataclass(frozen=True)
class WorkflowStep:
    key: str
    label: str
    description: str


class BaseAgent:
    name: str = "unnamed_agent"
    description: str = ""
    workflow_steps: list[WorkflowStep] = []

    def __init__(self, model: str | None = None):
        self.claude = ClaudeClient(model=model) if model else ClaudeClient()
        self._current_task: str | None = None

    def log(self, message: str, level: str = "info", data: dict[str, Any] | None = None) -> None:
        status_bus.log_event(self.name, message, level=level, data=data)

    def set_status(
        self,
        status: str,
        current_task: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        status_bus.set_agent_status(self.name, status, current_task=current_task, meta=meta)

    def record_artifact(
        self, kind: str, title: str, path: str, meta: dict[str, Any] | None = None
    ) -> int:
        return status_bus.record_artifact(self.name, kind, title, path, meta=meta)

    def step(self, key: str, detail: str = "") -> None:
        """Mark the agent as actively working on a named workflow step.

        Updates `agents.meta_json.step` (read by dashboard pipeline view) and
        logs an arrow-prefixed event for the activity feed.
        """
        label = self._step_label(key)
        meta = {"step": key, "step_label": label}
        self.set_status("running", current_task=self._current_task, meta=meta)
        message = f"→ {label}" if not detail else f"→ {label} — {detail}"
        self.log(message, data={"step": key})

    def _step_label(self, key: str) -> str:
        for s in self.workflow_steps:
            if s.key == key:
                return s.label
        return key

    def run(self, task: str, **kwargs: Any) -> Any:
        """Subclasses implement `_run`; this wrapper handles status + run-tracking."""
        self._current_task = task
        run_id = status_bus.begin_run(self.name, task)
        self.set_status("running", current_task=task, meta={"step": None, "run_id": run_id})
        self.log(f"Started: {task}")
        try:
            result = self._run(task, **kwargs)
            artifact_id = result.get("artifact_id") if isinstance(result, dict) else None
            status_bus.finish_run(run_id, "completed", artifact_id=artifact_id)
            self.set_status("idle", current_task=None, meta={"step": None})
            self.log(f"Completed: {task}", level="success")
            return result
        except Exception as exc:
            status_bus.finish_run(run_id, "failed", error=str(exc))
            self.set_status("error", current_task=task, meta={"error": str(exc)})
            self.log(
                f"Failed: {task} — {exc}",
                level="error",
                data={"traceback": traceback.format_exc()},
            )
            raise
        finally:
            self._current_task = None

    def _run(self, task: str, **kwargs: Any) -> Any:
        raise NotImplementedError
