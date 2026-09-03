"""World-signal ingestion: turn platform facts into durable society events.

Two sources in v1, both cheap and idempotent, run by the worker before each
dispatch cycle:

* ``ingest_task_outcomes`` — TaskSessions that ended FAILED/TIMEOUT through
  the REST/WS/timeout paths (not through a society intent) become
  ``task.failed`` / ``task.timeout`` events, one per task (deduped by
  idempotency key AND by subject so a FAIL_TASK executed by the runtime,
  which already emitted the event, is not duplicated). The task's
  ``trace_id`` becomes the correlation id so the story stays joined to the
  original escrow trace.
* ``emit_heartbeat`` — at most one ``society.heartbeat`` per interval
  (default hourly; ``0`` disables) so the Governor can reprioritise goals
  without any agent polling.

No wallet, task or agent row is modified here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import exists, or_
from sqlalchemy.orm import Session

from ..models import SocietyEvent, TaskSession, TaskStatus
from .config import SocietySettings
from .events import EventType, emit_event, utcnow

logger = logging.getLogger(__name__)

_OUTCOME_EVENT = {TaskStatus.FAILED.value: EventType.TASK_FAILED, TaskStatus.TIMEOUT.value: EventType.TASK_TIMEOUT}


def ingest_task_outcomes(db: Session, *, lookback_seconds: int = 3600, limit: int = 50, now: Optional[datetime] = None) -> int:
    """Emit one event per recently failed/timed-out task. Commits. Returns new events."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=lookback_seconds)
    already = exists().where(
        SocietyEvent.subject_type == "task",
        SocietyEvent.subject_id == TaskSession.id,
        SocietyEvent.event_type.in_([EventType.TASK_FAILED, EventType.TASK_TIMEOUT]),
    )
    tasks = (
        db.query(TaskSession)
        .filter(
            TaskSession.status.in_([TaskStatus.FAILED, TaskStatus.TIMEOUT]),
            ~already,
            or_(TaskSession.completed_at >= cutoff, TaskSession.refund_at >= cutoff),
        )
        .order_by(TaskSession.completed_at.desc().nullslast())
        .limit(limit)
        .all()
    )
    created = 0
    for t in tasks:
        status = t.status.value if hasattr(t.status, "value") else str(t.status)
        emit_event(
            db,
            event_type=_OUTCOME_EVENT[status],
            payload={
                "task_id": str(t.id),
                "capability": t.capability,
                "error": (t.error_message or "")[:500],
                "escrow_amount": t.escrow_amount,
                "caller_agent_id": str(t.caller_agent_id) if t.caller_agent_id else None,
                "callee_agent_id": str(t.callee_agent_id) if t.callee_agent_id else None,
                "source": "task_outcome_ingest",
            },
            actor_type="system",
            subject_type="task",
            subject_id=t.id,
            correlation_id=t.trace_id,
            trace_id=t.trace_id,
            idempotency_key=f"task-outcome:{t.id}:{status}",
        )
        created += 1
    if created:
        db.commit()
        logger.info("society world: ingested %d task outcome event(s)", created)
    return created


def emit_heartbeat(db: Session, settings: SocietySettings, *, now: Optional[datetime] = None) -> bool:
    """Emit ``society.heartbeat`` once per interval. Commits. Returns True when a new one was created."""
    interval = int(settings.heartbeat_interval_seconds)
    if interval <= 0:
        return False
    now = now or utcnow()
    bucket = int(now.timestamp()) // interval
    key = f"heartbeat:{bucket}"
    if db.query(SocietyEvent.id).filter(SocietyEvent.idempotency_key == key).first() is not None:
        return False
    emit_event(
        db,
        event_type=EventType.SOCIETY_HEARTBEAT,
        payload={"bucket": bucket, "interval_seconds": interval, "at": now.isoformat()},
        actor_type="system",
        idempotency_key=key,
    )
    db.commit()
    return True
