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
    Agent,
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

    # --- Proactive self-review of agent metrics ---
    try:
        review_created = run_self_review(db)
        if review_created:
            logger.info("self_review: created %d proposals", review_created)
    except Exception:  # noqa: BLE001
        logger.exception("self_review: error in self-review pass")

    return created


_LLM_SELF_REVIEW = None  # lazy-imported on first call


def _get_llm_reviewer():
    """Lazy import of DeepSeek client to avoid startup cost."""
    global _LLM_SELF_REVIEW
    if _LLM_SELF_REVIEW is None:
        import importlib
        _LLM_SELF_REVIEW = importlib.import_module("app.llm_client")
    return _LLM_SELF_REVIEW


def run_self_review(db: Session) -> int:
    """Proactively review agent metrics and create improvement proposals.

    Scans agents whose performance metrics suggest improvement opportunity
    and (no proposal already exists with source=self_reflection for them).
    Avoids agents that have never completed a task.
    """
    now = datetime.now(timezone.utc)
    cutoff = _lookback_window(now)

    has_review = exists().where(
        and_(
            ImprovementProposal.proposed_by_agent_id == Agent.id,
            ImprovementProposal.source == ProposalSource.SELF_REFLECTION.value,
            ImprovementProposal.created_at >= cutoff,
        )
    )

    candidates = (
        db.query(Agent)
        .filter(
            and_(
                Agent.total_tasks_completed > 0,  # has history
                ~has_review,
                or_(
                    Agent.success_rate < 0.8,
                    Agent.avg_response_time_ms > 5000,
                    Agent.verify_score < 50,
                ),
            )
        )
        .all()
    )

    if not candidates:
        return 0

    created = 0
    for agent in candidates:
        try:
            logger.info(
                "self_review: reviewing agent %s (rate=%.2f, rt=%dms, verify=%d, tier=%s)",
                agent.name, agent.success_rate, agent.avg_response_time_ms,
                agent.verify_score, agent.reputation_tier,
            )

            # Build lightweight improvement text without LLM cost per review
            title = f"Improve: {agent.name}"
            problems = []
            changes = []
            if agent.success_rate < 0.8:
                problems.append(f"Success rate is {agent.success_rate:.0%} (target: ≥80%)")
                changes.append("Investigate failure patterns and address top error causes")
            if agent.avg_response_time_ms > 5000:
                problems.append(f"Average response time is {agent.avg_response_time_ms}ms (target: ≤5000ms)")
                changes.append("Optimize execution path or increase timeout window")
            if agent.verify_score < 50:
                problems.append(f"Verification score is {agent.verify_score} (target: ≥50)")
                changes.append("Complete missing verification steps")

            proposal = ImprovementProposal(
                id=uuid.uuid4(),
                proposed_by_agent_id=agent.id,
                proposed_by_user_id=None,
                source=ProposalSource.SELF_REFLECTION,
                title=title,
                problem="; ".join(problems),
                root_cause="Proactive self-review identified performance degradation.",
                proposed_change="; ".join(changes),
                expected_benefit=f"Improves {agent.name}'s reliability and reputation tier.",
                risk="Low — informational proposal for review.",
                target_scope=ProposalScope.AGENT,
                importance=60 if agent.success_rate < 0.5 else 50,
                status=ProposalStatus.PROPOSED,
            )
            db.add(proposal)
            db.flush()
            created += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("self_review: failed for agent %s; continuing", agent.id)
            continue

    if created:
        try:
            db.commit()
            logger.info("self_review: created %d proposals", created)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("self_review: commit failed; rolled back batch")
            return 0
    return created


# Path of the human-readable backlog the reflection loop appends to. Relative to the
# worker process CWD by default (the local compose stack mounts the repository copy);
# absent file => the loop simply skips backlog writes.
BACKLOG_PATH = os.getenv("AGENT_BACKLOG_PATH", "AGENT_BACKLOG.md")


def _next_backlog_id() -> str:
    """Read the current backlog and return the next AB-NNN identifier."""
    try:
        import re
        with open(BACKLOG_PATH, encoding="utf-8") as f:
            content = f.read()
        ids = re.findall(r"AB-(\d+)", content)
        if not ids:
            return "AB-001"
        max_num = max(int(n) for n in ids)
        return f"AB-{max_num + 1:03d}"
    except FileNotFoundError:
        return "AB-001"


def proposal_to_backlog_entry(proposal: ImprovementProposal) -> str:
    """Format an ImprovementProposal as a YAML backlog item."""
    ab_id = _next_backlog_id()
    # Map importance to priority
    if proposal.importance >= 80:
        priority = "high"
    elif proposal.importance >= 50:
        priority = "medium"
    else:
        priority = "low"

    description_lines = [
        f"**Source:** {proposal.source.value}",
        f"**Problem:** {proposal.problem or 'N/A'}",
        f"**Root cause:** {proposal.root_cause or 'N/A'}",
        f"**Proposed change:** {proposal.proposed_change or 'N/A'}",
        f"**Expected benefit:** {proposal.expected_benefit or 'N/A'}",
        f"**Risk:** {proposal.risk or 'N/A'}",
        f"**Agent:** {proposal.proposed_by_agent_id}",
    ]
    if proposal.source_task_id:
        description_lines.append(f"**Source task:** {proposal.source_task_id}")

    # Build contextual hints from the task's callee agent
    ab_id_internal = ab_id  # avoid redefinition
    return f"""  - id: {ab_id}
    title: "{proposal.title}"
    priority: {priority}
    description: |>
      {"  ".join(description_lines)}
    acceptance:
      - test is true  # auto-generated — QA will refine
    status: open
"""


def convert_proposals_to_backlog(db: Session) -> int:
    """Read PROPOSED proposals and write them to AGENT_BACKLOG.md.

    Skips proposals that were already converted (status != PROPOSED) and
    proposals whose title already appears in the backlog (dedup).
    Returns the number of proposals converted.
    """
    import re

    proposals = (
        db.query(ImprovementProposal)
        .filter(ImprovementProposal.status == ProposalStatus.PROPOSED)
        .order_by(ImprovementProposal.importance.desc(), ImprovementProposal.created_at.asc())
        .limit(3)  # max 3 per tick to avoid flood
        .all()
    )
    if not proposals:
        return 0

    # Read current backlog
    try:
        with open(BACKLOG_PATH, encoding="utf-8") as f:
            backlog_content = f.read()
    except FileNotFoundError:
        backlog_content = ""

    converted = 0
    for proposal in proposals:
        # Dedup: check if title already exists in backlog
        title_match = re.search(rf'title:\s*"{re.escape(proposal.title)}"', backlog_content)
        if title_match:
            logger.info("proposal->backlog: skip '%s' (already in backlog)", proposal.title)
            # Mark as CONVERTED even if already there — avoid reprocessing
            proposal.status = ProposalStatus.CONVERTED_TO_TASK
            continue

        entry = proposal_to_backlog_entry(proposal)
        # Append to backlog
        separator = "\n" if backlog_content.endswith("\n") else "\n\n"
        with open(BACKLOG_PATH, "a", encoding="utf-8") as f:
            f.write(separator + entry)

        # Mark proposal as converted
        proposal.status = ProposalStatus.CONVERTED_TO_TASK
        logger.info("proposal->backlog: appended %s ('%s')", _next_backlog_id(), proposal.title)
        converted += 1

    if converted:
        db.commit()
        logger.info("proposal->backlog: converted %d proposals to backlog items", converted)

    return converted
