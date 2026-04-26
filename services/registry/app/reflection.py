"""Reflection — turn a finished/failed task + last review into a structured
ImprovementProposal payload.

This is a *pure function*: input is a TaskSession (and optional review
information), output is a dict suitable for ImprovementProposal(**dict).
No DB writes happen here. Callers are:

- services/registry/app/api/routes/improvements.py
  POST /v1/improvements/reflect — the dashboard previews an
  auto-generated proposal before saving.

- services/worker/app/reflection_loop.py
  Background loop scans newly-FAILED tasks every 5 min and persists a
  proposal for each (idempotent on source_task_id).

V1 is template-based. The function signature is shaped to drop in an
LLM call in v2 without changing callers.
"""

from __future__ import annotations

from typing import Any

from .models import ProposalScope, ProposalSource, TaskSession, TaskStatus


def _classify_source(task: TaskSession) -> ProposalSource:
    """Pick the proposal source enum from the task's terminal status."""
    status = task.status if isinstance(task.status, str) else (
        task.status.value if hasattr(task.status, "value") else str(task.status)
    )
    if status == TaskStatus.FAILED.value:
        return ProposalSource.TASK_FAILURE
    if status == TaskStatus.TIMEOUT.value:
        return ProposalSource.RUNTIME_ERROR
    if status == TaskStatus.REFUNDED.value:
        return ProposalSource.RUNTIME_ERROR
    return ProposalSource.SELF_REFLECTION


def _compose_problem(task: TaskSession) -> str:
    if task.error_message:
        return task.error_message.strip()
    if task.status in (TaskStatus.TIMEOUT, "timeout"):
        return f"Task '{task.capability}' timed out before completion."
    if task.status in (TaskStatus.FAILED, "failed"):
        return f"Task '{task.capability}' failed without explicit error message."
    return f"Task '{task.capability}' did not complete successfully."


def _compose_root_cause(task: TaskSession) -> str:
    status_str = task.status if isinstance(task.status, str) else (
        task.status.value if hasattr(task.status, "value") else str(task.status)
    )
    if status_str == TaskStatus.TIMEOUT.value:
        return (
            "Callee did not respond within the timeout window. Likely causes: "
            "callee endpoint unreachable, callee crashed mid-task, or work "
            "exceeded the budgeted duration."
        )
    if task.error_message:
        return f"Reported error from callee: {task.error_message.strip()}"
    return "Root cause not yet identified — needs investigation by the assignee."


def _compose_change(task: TaskSession) -> str:
    return (
        f"Re-attempt '{task.capability}' after addressing the root cause. "
        f"Consider increasing timeout, improving callee error handling, or "
        f"selecting a different callee with higher reputation tier."
    )


def _compose_benefit(task: TaskSession) -> str:
    return (
        f"Future invocations of '{task.capability}' avoid the same failure "
        "mode; agent reputation recovers; caller wallet not blocked by retry "
        "loops."
    )


def _compose_title(task: TaskSession) -> str:
    return f"Improve handling of '{task.capability}'"


def reflect_on_task(task: TaskSession) -> dict[str, Any]:
    """Build the kwargs for ``ImprovementProposal(**reflect_on_task(t))``.

    Returns a dict with these keys (exclude ``proposed_by_*`` and
    ``source_task_id`` — caller fills those):
        title, problem, root_cause, proposed_change, expected_benefit,
        risk, source, target_scope, importance.
    """
    return {
        "title": _compose_title(task),
        "problem": _compose_problem(task),
        "root_cause": _compose_root_cause(task),
        "proposed_change": _compose_change(task),
        "expected_benefit": _compose_benefit(task),
        "risk": "Low — proposal is informational pre-implementation; review before converting.",
        "source": _classify_source(task),
        "target_scope": ProposalScope.AGENT,
        "importance": 60 if task.status == TaskStatus.FAILED else 50,
    }


__all__ = ["reflect_on_task"]
