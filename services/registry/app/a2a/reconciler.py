"""Project economic task state into A2A task state (ADR-0009 D5).

One direction only: TaskSession -> A2A events. The reconciler reads a
TaskSession (never locks or writes it) and appends protocol events under the
A2A row lock. It is idempotent (an event is added only when the mapped state
differs), never overwrites a terminal A2A state, and is safe on several
replicas (``FOR UPDATE SKIP LOCKED`` in the background sweep).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Callable, List, Optional

from sqlalchemy.orm import Session

from ..models import TaskSession, Transaction
from . import config, mapping, store
from .orm import A2ATask

logger = logging.getLogger(__name__)

_FAILED_TEXT = "The agent reported that the task failed. Any reserved budget was released."
_TIMEOUT_TEXT = "The task timed out before the agent completed it. Any reserved budget was released."
_CANCELED_TEXT = "The task was canceled by the caller before the agent started. Any reserved budget was released."
_COMPLETED_TEXT = "The task completed."
_ORPHAN_TEXT = "The task was interrupted before any budget was reserved. Send the message again."
MAX_DETAIL_CHARS = 500


def escrow_idempotency_key(a2a_key: str) -> str:
    """The TaskSession idempotency key derived from the A2A one (ADR-0009 D11).
    ``transactions.idempotency_key`` is VARCHAR(64): 4 + 56 hex chars (224 bits)."""
    return f"a2a:{a2a_key[:56]}"


def link_session_by_key(db: Session, task: A2ATask) -> Optional[uuid.UUID]:
    """Find the TaskSession an interrupted SendMessage created (its escrow
    transaction carries the derived idempotency key) and link it."""
    if task.task_session_id is not None:
        return task.task_session_id
    row = (
        db.query(Transaction.task_session_id)
        .filter(Transaction.idempotency_key == escrow_idempotency_key(task.idempotency_key))
        .first()
    )
    if row and row[0] is not None:
        task.task_session_id = row[0]
        return row[0]
    return None


def _failure_message(task: A2ATask, session: TaskSession, state: str) -> "mapping.pb.Message":
    raw = session.status.value if hasattr(session.status, "value") else str(session.status)
    tid, cid = str(task.id), str(task.context_id)
    if state == mapping.CANCELED:
        return mapping.agent_message(tid, cid, "canceled", _CANCELED_TEXT, {"reason": "canceled"})
    if raw == "timeout":
        return mapping.agent_message(tid, cid, "timeout", _TIMEOUT_TEXT, {"reason": "timeout"})
    detail = (session.error_message or "")[:MAX_DETAIL_CHARS]
    return mapping.agent_message(tid, cid, "failed", _FAILED_TEXT, {"reason": "failed", "agentError": detail})


def apply_projection(db: Session, task: A2ATask) -> bool:
    """Bring a LOCKED A2A task up to date with its TaskSession. Returns
    whether an event was appended. Does not commit."""
    if mapping.is_terminal(task.state):
        return False
    if task.task_session_id is None:
        link_session_by_key(db, task)
    if task.task_session_id is None:
        age = store.now() - task.created_at if task.created_at else timedelta(0)
        if task.state == mapping.SUBMITTED and age > timedelta(seconds=config.ORPHAN_AFTER_SECONDS):
            msg = mapping.agent_message(str(task.id), str(task.context_id), "orphan", _ORPHAN_TEXT, {"reason": "interrupted"})
            store.set_state(db, task, mapping.REJECTED, msg)
            return True
        return False

    session = db.query(TaskSession).filter(TaskSession.id == task.task_session_id).first()
    if session is None:
        return False
    target = mapping.economic_state(session.status, session.error_message)
    if target == task.state:
        return False
    if target == mapping.WORKING:
        store.set_state(db, task, mapping.WORKING)
    elif target == mapping.COMPLETED:
        store.add_artifact(db, task, mapping.artifact_from_output(session.output))
        store.set_state(db, task, mapping.COMPLETED, mapping.agent_message(str(task.id), str(task.context_id), "completed", _COMPLETED_TEXT))
    elif target in (mapping.FAILED, mapping.CANCELED):
        store.set_state(db, task, target, _failure_message(task, session, target))
    else:  # SUBMITTED again cannot happen: TaskSession never returns to INITIATED
        return False
    return True


def reconcile_task(db: Session, task_id: uuid.UUID, *, skip_locked: bool = False) -> bool:
    """Lock, project, commit. Returns whether the task changed."""
    task = store.lock_task(db, task_id, skip_locked=skip_locked)
    if task is None:
        db.rollback()
        return False
    changed = apply_projection(db, task)
    if changed:
        db.commit()
    else:
        db.rollback()
    return changed


def sweep(session_factory: Callable[[], Session], limit: int = 200) -> List[uuid.UUID]:
    """One background pass over open A2A tasks. Returns the ids that changed
    (the caller wakes their streams)."""
    changed: List[uuid.UUID] = []
    db = session_factory()
    try:
        ids = [
            r[0]
            for r in db.query(A2ATask.id)
            .filter(A2ATask.state.in_([mapping.SUBMITTED, mapping.WORKING]))
            .order_by(A2ATask.updated_at.asc())
            .limit(limit)
            .all()
        ]
        db.rollback()
        for task_id in ids:
            try:
                if reconcile_task(db, task_id, skip_locked=True):
                    changed.append(task_id)
            except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
                db.rollback()
                logger.exception("a2a reconcile failed for one task")
    finally:
        db.close()
    return changed
