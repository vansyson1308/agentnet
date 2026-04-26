"""Reflection loop — turns failed/timeout/refunded tasks into structured
:class:`ImprovementProposal` rows the lab can review.

The worker calls :func:`run_reflection_loop` from its main loop on the
``REFLECTION_LOOP_INTERVAL_SEC`` cadence (default 300s = 5min). Idempotent
by design: a proposal is only generated when the task has none yet
(``source_task_id`` not present in ``improvement_proposals``).

Scope is intentionally tight to keep the lab clean on the first deploy:
only tasks whose ``completed_at`` (or ``refund_at``/``created_at`` if
``completed_at`` is null) lies within the lookback window are considered.
The window defaults to 24h.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from .models import (
    ImprovementProposal,
    ProposalScope,
    ProposalSource,
    ProposalStatus,
    TaskSession,
    TaskStatus,
)

logger = logging.getLogger(__name__)

# Terminal statuses we reflect on. ``COMPLETED`` is intentionally OMITTED:
# successful tasks don't generate proposals (no improvement signal).
_REFLECTABLE_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.FAILED,
    TaskStatus.TIMEOUT,
    TaskStatus.REFUNDED,
)


def _source_for(task: TaskSession) -> ProposalSource:
    """Classify the proposal source from the terminal task status.

    Maps onto the source enum defined alongside the registry side:
    failed -> task_failure, timeout|refunded -> runtime_error.
    """
    status_value = task.status.value if hasattr(task.status, "value") else str(task.status)
    if status_value == TaskStatus.FAILED.value:
        return ProposalSource.TASK_FAILURE
    return ProposalSource.RUNTIME_ERROR


def _compose_problem(task: TaskSession) -> str:
    if task.error_message:
        return task.error_message
    status_value = task.status.value if hasattr(task.status, "value") else str(task.status)
    if status_value == TaskStatus.TIMEOUT.value:
        return f"Task timed out after the configured deadline (capability={task.capability})."
    if status_value == TaskStatus.REFUNDED.value:
        return f"Task ended with an escrow refund (capability={task.capability})."
    return f"Task '{task.capability}' did not complete successfully."


def _compose_root_cause(task: TaskSession) -> str:
    parts: list[str] = []
    if task.error_message:
        parts.append(f"reported error: {task.error_message}")
    status_value = task.status.value if hasattr(task.status, "value") else str(task.status)
    parts.append(f"terminal status: {status_value}")
    if task.timeout_at and task.created_at:
        parts.append(
            "configured timeout window: "
            + str((task.timeout_at - task.created_at).total_seconds())
            + "s"
        )
    return "Root cause not yet identified — needs investigation. Signals: " + "; ".join(parts)


def _compose_change(task: TaskSession) -> str:
    status_value = task.status.value if hasattr(task.status, "value") else str(task.status)
    if status_value == TaskStatus.TIMEOUT.value:
        return (
            f"Re-attempt '{task.capability}' with a longer timeout, or break the "
            f"work into smaller sub-tasks. Verify the callee agent's average "
            f"response time before re-issuing."
        )
    if status_value == TaskStatus.REFUNDED.value:
        return (
            f"Investigate why the escrow had to be refunded for '{task.capability}'. "
            f"Confirm the callee agent's wallet + capability are healthy before "
            f"re-issuing."
        )
    return (
        f"Re-attempt '{task.capability}' after addressing the failure surfaced "
        f"by the callee. Capture lessons in agent-scope memory before re-issuing."
    )


def _compose_benefit(task: TaskSession) -> str:
    return (
        f"Higher reliability for '{task.capability}' across the society; "
        f"reduces escrow churn and protects the callee's reputation tier."
    )


def _compose_importance(task: TaskSession) -> int:
    """Simple heuristic: bigger escrow loss = higher importance.

    50 baseline; +20 if escrow > 100 credits; +10 more if > 500.
    Capped at 100.
    """
    base = 50
    if (task.escrow_amount or 0) > 100:
        base += 20
    if (task.escrow_amount or 0) > 500:
        base += 10
    return min(100, base)


def reflect_on_task(task: TaskSession) -> dict:
    """Pure-function preview: build a proposal payload from a task.

    Mirrors the registry's ``reflection.py`` so a manual ``POST /reflect``
    on the registry side and the worker's auto-loop produce the same
    shape. The returned dict can be passed directly to
    ``ImprovementProposal(**dict)`` after attribution fields are added.
    """
    return {
        "source": _source_for(task),
        "title": f"Improve: {task.capability}",
        "problem": _compose_problem(task),
        "root_cause": _compose_root_cause(task),
        "proposed_change": _compose_change(task),
        "expected_benefit": _compose_benefit(task),
        "risk": "Low — proposal is informational pre-implementation; needs review.",
        "target_scope": ProposalScope.AGENT,
        "importance": _compose_importance(task),
    }


def _lookback_window(now: datetime) -> datetime:
    hours = int(os.getenv("REFLECTION_LOOP_LOOKBACK_HOURS", "24"))
    return now - timedelta(hours=max(1, hours))


def _terminal_time(task: TaskSession, fallback: datetime) -> datetime:
    """Pick the most-meaningful timestamp for the lookback comparison."""
    return task.completed_at or task.refund_at or task.created_at or fallback


def run_reflection_loop(db: Session) -> int:
    """Generate proposals for fresh failed/timeout/refunded tasks.

    Returns the number of proposals created in this pass. Safe to call
    repeatedly: an idempotency check (no proposal already exists with
    ``source_task_id == task.id``) prevents duplicates.

    Designed to be invoked from the worker main loop. The DB session
    is supplied by the caller so the worker can reuse its existing
    session-per-iteration pattern.
    """
    now = datetime.now(timezone.utc)
    cutoff = _lookback_window(now)

    status_values = [s.value for s in _REFLECTABLE_STATUSES]

    # Subquery: tasks that ALREADY have a proposal sourced from them.
    has_proposal = exists().where(
        ImprovementProposal.source_task_id == TaskSession.id
    )

    # NOTE: SQLAlchemy server-side timestamp comparison. We compare against
    # ``completed_at`` first; failed tasks generally have it set, but
    # timeouts may only have ``refund_at``. Use OR over the candidate
    # timestamps and let Postgres pick whichever exists.
    candidates = (
        db.query(TaskSession)
        .filter(
            and_(
                TaskSession.status.in_(status_values),
                ~has_proposal,
                or_(
                    TaskSession.completed_at >= cutoff,
                    TaskSession.refund_at >= cutoff,
                    TaskSession.created_at >= cutoff,
                ),
            )
        )
        .order_by(TaskSession.created_at.desc())
        .limit(int(os.getenv("REFLECTION_LOOP_BATCH_LIMIT", "50")))
        .all()
    )

    if not candidates:
        return 0

    created = 0
    for task in candidates:
        try:
            payload = reflect_on_task(task)
            # Attribute to the callee (it failed); fall back to caller.
            attributed_agent_id: Optional[uuid.UUID] = task.callee_agent_id or task.caller_agent_id
            proposal = ImprovementProposal(
                id=uuid.uuid4(),
                proposed_by_agent_id=attributed_agent_id,
                proposed_by_user_id=None,  # auto-generated, no user actor
                source=payload["source"],
                title=payload["title"],
                problem=payload["problem"],
                root_cause=payload["root_cause"],
                proposed_change=payload["proposed_change"],
                expected_benefit=payload["expected_benefit"],
                risk=payload["risk"],
                target_scope=payload["target_scope"],
                importance=payload["importance"],
                source_task_id=task.id,
                status=ProposalStatus.PROPOSED,
            )
            db.add(proposal)
            db.flush()  # assign id; surface FK errors before commit
            created += 1
        except Exception:  # noqa: BLE001
            # One bad task shouldn't poison the whole batch.
            db.rollback()
            logger.exception(
                "reflection_loop: failed to create proposal for task %s; continuing",
                getattr(task, "id", "<unknown>"),
            )
            continue

    if created:
        try:
            db.commit()
            logger.info("reflection_loop: created %d proposals", created)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("reflection_loop: commit failed; rolled back batch")
            return 0
    return created
