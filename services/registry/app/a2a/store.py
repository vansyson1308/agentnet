"""Durable A2A task store over PostgreSQL (ADR-0009 D5).

The event log (``a2a_task_events``) is the source of truth for streams: every
state change appends a row with the next per-task ``seq`` while the task row
is locked, so a stream that replays "events after seq N" can never miss or
reorder one, across replicas and restarts. Nothing here touches money.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from a2a.types import a2a_pb2 as pb
from sqlalchemy.orm import Session

from . import mapping, metrics
from .config import MAX_HISTORY
from .orm import A2AArtifact, A2AAuditLog, A2AMessage, A2ATask, A2ATaskEvent


def now() -> datetime:
    return datetime.now(timezone.utc)


def load_task(db: Session, task_id: uuid.UUID) -> Optional[A2ATask]:
    return db.query(A2ATask).filter(A2ATask.id == task_id).first()


def lock_task(db: Session, task_id: uuid.UUID, *, skip_locked: bool = False) -> Optional[A2ATask]:
    q = db.query(A2ATask).filter(A2ATask.id == task_id)
    q = q.with_for_update(skip_locked=True) if skip_locked else q.with_for_update()
    return q.first()


def append_event(db: Session, task: A2ATask, kind: str, payload: Dict[str, Any]) -> int:
    """Append an event; ``task`` MUST be row-locked by the caller."""
    task.last_event_seq = int(task.last_event_seq or 0) + 1
    db.add(A2ATaskEvent(task_id=task.id, seq=task.last_event_seq, kind=kind, payload=payload))
    return task.last_event_seq


def add_message(db: Session, task: A2ATask, message: pb.Message) -> None:
    db.add(
        A2AMessage(
            task_id=task.id,
            context_id=task.context_id,
            message_id=message.message_id,
            role=pb.Role.Name(message.role),
            message=mapping.to_json(message),
        )
    )


def set_state(db: Session, task: A2ATask, state: str, message: Optional[pb.Message] = None) -> int:
    """Move a locked task to ``state`` and log the status event. Terminal
    states are final: a second terminal transition is refused, never merged."""
    if mapping.is_terminal(task.state):
        raise ValueError(f"A2A task {task.id} is already terminal ({task.state})")
    at = now()
    task.state = state
    task.status_message = mapping.to_json(message) if message is not None else None
    task.status_timestamp = at
    task.updated_at = at
    if message is not None and mapping.is_terminal(state):
        add_message(db, task, message)
    st = mapping.status(state, message, at)
    seq = append_event(db, task, "status", mapping.to_json(mapping.status_event(str(task.id), str(task.context_id), st)))
    metrics.record_task_state(state)
    return seq


def add_artifact(db: Session, task: A2ATask, artifact: pb.Artifact) -> int:
    db.add(A2AArtifact(task_id=task.id, artifact_id=artifact.artifact_id, artifact=mapping.to_json(artifact)))
    event = mapping.artifact_event(str(task.id), str(task.context_id), artifact)
    return append_event(db, task, "artifact", mapping.to_json(event))


def history(db: Session, task_id: uuid.UUID) -> List[Dict[str, Any]]:
    rows = (
        db.query(A2AMessage.message)
        .filter(A2AMessage.task_id == task_id)
        .order_by(A2AMessage.id.asc())
        .limit(MAX_HISTORY)
        .all()
    )
    return [r[0] for r in rows]


def artifacts(db: Session, task_id: uuid.UUID) -> List[Dict[str, Any]]:
    rows = db.query(A2AArtifact.artifact).filter(A2AArtifact.task_id == task_id).order_by(A2AArtifact.id.asc()).all()
    return [r[0] for r in rows]


def first_user_message(db: Session, task_id: uuid.UUID) -> Optional[pb.Message]:
    row = (
        db.query(A2AMessage.message)
        .filter(A2AMessage.task_id == task_id, A2AMessage.role == "ROLE_USER")
        .order_by(A2AMessage.id.asc())
        .first()
    )
    return mapping.parse_message(row[0]) if row else None


def snapshot(db: Session, task: A2ATask, *, history_length: Optional[int], include_artifacts: bool = True) -> pb.Task:
    return mapping.build_task(
        task_id=str(task.id),
        context_id=str(task.context_id),
        state=task.state,
        status_message=task.status_message,
        status_at=task.status_timestamp,
        history=history(db, task.id),
        artifacts=artifacts(db, task.id) if include_artifacts else [],
        history_length=history_length,
        include_artifacts=include_artifacts,
    )


def events_after(db: Session, task_id: uuid.UUID, seq: int, limit: int = 100) -> List[Tuple[int, str, Dict[str, Any]]]:
    rows = (
        db.query(A2ATaskEvent.seq, A2ATaskEvent.kind, A2ATaskEvent.payload)
        .filter(A2ATaskEvent.task_id == task_id, A2ATaskEvent.seq > seq)
        .order_by(A2ATaskEvent.seq.asc())
        .limit(limit)
        .all()
    )
    return [(int(r[0]), r[1], r[2]) for r in rows]


def audit(
    db: Session,
    *,
    principal_class: str,
    principal_id: Optional[uuid.UUID],
    operation: str,
    result: str,
    target_agent_id: Optional[uuid.UUID] = None,
    a2a_task_id: Optional[uuid.UUID] = None,
    task_session_id: Optional[uuid.UUID] = None,
    economics_action: Optional[str] = None,
    request_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Append-only audit row (the table refuses UPDATE/DELETE by trigger).
    Never holds credentials or message bodies: ``detail`` carries codes."""
    db.add(
        A2AAuditLog(
            principal_class=principal_class[:16],
            principal_id=principal_id,
            target_agent_id=target_agent_id,
            operation=operation[:48],
            a2a_task_id=a2a_task_id,
            task_session_id=task_session_id,
            result=result[:48],
            economics_action=(economics_action or None) and economics_action[:32],
            request_id=(request_id or None) and str(request_id)[:64],
            detail=detail or {},
        )
    )
