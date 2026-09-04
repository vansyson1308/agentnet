import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_agent, get_current_user_or_agent, hash_input
from ...authz import ACTION_EXECUTE, principal_agent_ids, require_party, require_scoped_action, reserve_scoped_spend
from ...database import get_db
from ...models import (
    Agent,
    CurrencyType,
    Span,
    SpanStatus,
    TaskSession,
    TaskStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
    Wallet,
)
from ...schemas import SpanCreate, SpanInDB
from ...schemas import Task as TaskSchema
from ...governance import create_notification
from ...schemas import TaskCreate, TaskUpdate
from ...task_service import (
    EscrowError,
    confirm_task_completion,
    create_task_with_escrow,
    fail_task_with_refund,
    start_task as start_task_session,
)
from ...tracing import get_tracer
from ...websocket_manager import manager

router = APIRouter()
tracer = get_tracer(__name__)
audit_logger = logging.getLogger("agentnet.audit")


def _resolve_agent(current_user_or_agent, db: Session, agent_id=None):
    """Extract agent from auth context. Works for both agent and user auth."""
    from ...models import User

    if isinstance(current_user_or_agent, Agent):
        return current_user_or_agent
    elif isinstance(current_user_or_agent, User):
        if agent_id:
            agent = db.query(Agent).filter(Agent.id == agent_id, Agent.user_id == current_user_or_agent.id).first()
        else:
            agent = db.query(Agent).filter(Agent.user_id == current_user_or_agent.id).first()
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No agent found for the authenticated user",
            )
        return agent
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication")


def save_span_sync(span_data: SpanCreate):
    """Internal helper to save span using a fresh session (for background tasks)."""
    from ...database import SessionLocal
    db = SessionLocal()
    try:
        db_span = Span(
            id=uuid.uuid4(),
            trace_id=span_data.trace_id,
            span_id=span_data.span_id,
            parent_span_id=span_data.parent_span_id,
            agent_id=span_data.agent_id,
            event=span_data.event,
            capability=span_data.capability,
            duration_ms=span_data.duration_ms,
            status=SpanStatus(span_data.status) if span_data.status else None,
            credits_used=span_data.credits_used,
            extra_data=span_data.metadata or {},
        )
        db.add(db_span)
        db.commit()
    except Exception as e:
        audit_logger.error(f"Background span preservation failed: {e}")
    finally:
        db.close()


def update_agent_reputation_sync(agent_id_str: str):
    """Internal helper to update agent reputation using a fresh session."""
    from ...database import SessionLocal
    from ...reputation import update_agent_reputation
    db = SessionLocal()
    try:
        update_agent_reputation(db, uuid.UUID(agent_id_str))
    except Exception as e:
        audit_logger.error(f"Background reputation update failed for {agent_id_str}: {e}")
    finally:
        db.close()


def save_span(db: Session, span_data: SpanCreate) -> Span:
    """Persist a span to the database (Synchronous)."""
    db_span = Span(
        id=uuid.uuid4(),
        trace_id=span_data.trace_id,
        span_id=span_data.span_id,
        parent_span_id=span_data.parent_span_id,
        agent_id=span_data.agent_id,
        event=span_data.event,
        capability=span_data.capability,
        duration_ms=span_data.duration_ms,
        status=SpanStatus(span_data.status) if span_data.status else None,
        credits_used=span_data.credits_used,
        extra_data=span_data.metadata or {},
    )
    db.add(db_span)
    db.commit()
    db.refresh(db_span)
    return db_span


def validate_data_against_schema(data: Dict, schema: Optional[Dict], label: str = "Input"):
    """Validate data against a JSON schema."""
    if not schema:
        return
        
    import jsonschema
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} validation failed: {e.message}",
        )


@router.post("/", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_task_session(
    task: TaskCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Create a task session and lock escrow.

    Thin HTTP wrapper around ``task_service.create_task_with_escrow``.
    All escrow logic, FOR UPDATE locking, idempotency, and inline span
    persistence live in the service module so REST and WebSocket paths
    cannot drift.
    """
    from ...models import User

    if isinstance(current_user_or_agent, Agent):
        current_agent = current_user_or_agent
    elif isinstance(current_user_or_agent, User):
        current_agent = (
            db.query(Agent)
            .filter(
                Agent.id == task.caller_agent_id,
                Agent.user_id == current_user_or_agent.id,
            )
            .first()
        )
        if not current_agent:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Caller agent must belong to the authenticated user",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication",
        )

    if task.caller_agent_id != current_agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Caller agent ID must match the current agent",
        )

    # Scoped tokens: the action must be allowed and the amount is charged
    # against the token's cap inside the same transaction as the escrow.
    require_scoped_action(current_agent, ACTION_EXECUTE)
    reserve_scoped_spend(db, current_agent, task.max_budget)

    try:
        task_session, _transaction = create_task_with_escrow(
            db=db,
            caller_agent=current_agent,
            callee_agent_id=task.callee_agent_id,
            capability_name=task.capability,
            input_data=task.input,
            max_budget=task.max_budget,
            currency=task.currency,
            timeout_seconds=task.timeout_seconds,
            parent_span_id=getattr(task, "parent_span_id", None),
            retry_of_id=task.retry_of_id,
            idempotency_key=idempotency_key,
        )
    except EscrowError as e:
        db.rollback()  # also drops any scoped-token charge made above
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    audit_logger.info(
        f"Task {task_session.id} INITIATED. Escrow {task_session.escrow_amount} "
        f"locked for callee {task_session.callee_agent_id}"
    )

    # Dispatch to callee — out-of-band, non-fatal if it fails. The callee
    # can also poll GET /tasks/{id} so the task is recoverable either way.
    callee_agent = db.query(Agent).filter(Agent.id == task_session.callee_agent_id).first()
    message = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "trace_id": str(task_session.trace_id),
        "method": "execute",
        "from": str(current_agent.id),
        "params": {
            "capability": task.capability,
            "input": task.input,
            "payment": {
                "max_budget": task.max_budget,
                "currency": task.currency,
                "escrow_session_id": str(task_session.id),
            },
            "timeout_seconds": task.timeout_seconds,
        },
    }
    sent = await manager.send_to_agent(message, str(task_session.callee_agent_id))
    task_session.fulfillment_channel = "websocket" if sent else None

    if not sent and callee_agent and callee_agent.endpoint:
        task_session.fulfillment_channel = "webhook"
        try:
            from ...sandbox import sandboxed_call

            asyncio.create_task(
                sandboxed_call(
                    url=callee_agent.endpoint,
                    method="POST",
                    json_body=message,
                )
            )
            audit_logger.info(
                f"Task {task_session.id} dispatched via Webhook to {callee_agent.endpoint}"
            )
        except Exception as e:
            audit_logger.error(
                f"Webhook dispatch failed for task {task_session.id}: {e}"
            )

    db.commit()

    return {
        "task_session_id": str(task_session.id),
        "trace_id": str(task_session.trace_id),
        "span_id": str(task_session.span_id),
    }


@router.put("/{task_id}/start")
async def start_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
    agent_id: Optional[uuid.UUID] = Query(None),
):
    """Callee confirms start. Updates status to in_progress (delegates to task_service)."""
    require_scoped_action(current_user_or_agent, ACTION_EXECUTE)
    current_agent = _resolve_agent(current_user_or_agent, db, agent_id=agent_id)
    try:
        start_task_session(db=db, task_id=task_id, callee_agent=current_agent)
    except EscrowError as e:
        msg = str(e)
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in msg.lower()
            else status.HTTP_403_FORBIDDEN
            if "callee" in msg.lower() and "only" in msg.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg)
    audit_logger.info(f"Task {task_id} transitioned to IN_PROGRESS by agent {current_agent.id}")
    return {"message": "Task started successfully"}


@router.put("/{task_id}/confirm")
async def confirm_task(
    task_id: uuid.UUID,
    output: Dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
    agent_id: Optional[uuid.UUID] = Query(None),
):
    """Callee reports completion (delegates to task_service.confirm_task_completion)."""
    require_scoped_action(current_user_or_agent, ACTION_EXECUTE)
    current_agent = _resolve_agent(current_user_or_agent, db, agent_id=agent_id)
    try:
        task_session = confirm_task_completion(
            db=db,
            callee_agent=current_agent,
            task_id=task_id,
            output=output,
        )
    except EscrowError as e:
        msg = str(e)
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in msg.lower()
            else status.HTTP_403_FORBIDDEN
            if msg.startswith("Only the callee")
            else status.HTTP_409_CONFLICT
            if "Pending transaction missing" in msg
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg)

    # Reputation refresh + listener notifications are async — fine to lose on
    # crash since reputation is recomputed periodically by the worker.
    background_tasks.add_task(update_agent_reputation_sync, str(current_agent.id))
    background_tasks.add_task(update_agent_reputation_sync, str(task_session.caller_agent_id))
    audit_logger.info(
        f"Task {task_id} COMPLETED. Escrow {task_session.escrow_amount} released to agent {current_agent.id}"
    )

    message = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "trace_id": str(task_session.trace_id),
        "result": output,
        "credits_charged": task_session.escrow_amount,
    }
    background_tasks.add_task(manager.send_to_agent, message, str(task_session.caller_agent_id))

    if task_session.caller_agent and task_session.caller_agent.user_id:
        background_tasks.add_task(
            create_notification,
            db,
            task_session.caller_agent.user_id,
            "task",
            "Task Completed",
            f"Your agent's task '{task_session.capability}' was completed successfully.",
            f"/tasks/{task_session.id}",
        )

    return {"message": "Task confirmed successfully"}


@router.put("/{task_id}/fail")
async def fail_task(
    task_id: uuid.UUID,
    error_message: str = Query("Unknown error"),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
    agent_id: Optional[uuid.UUID] = Query(None),
):
    """Callee reports failure (delegates to task_service.fail_task_with_refund)."""
    require_scoped_action(current_user_or_agent, ACTION_EXECUTE)
    current_agent = _resolve_agent(current_user_or_agent, db, agent_id=agent_id)
    try:
        task_session = fail_task_with_refund(
            db=db,
            task_id=task_id,
            error_message=error_message,
            callee_agent_id=current_agent.id,
            new_status=TaskStatus.FAILED,
        )
    except EscrowError as e:
        msg = str(e)
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in msg.lower()
            else status.HTTP_403_FORBIDDEN
            if msg.startswith("Only the callee")
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg)

    audit_logger.warning(
        f"Task {task_id} FAILED. Error: {error_message}. Escrow released back to caller."
    )

    if background_tasks is not None:
        message = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "trace_id": str(task_session.trace_id),
            "error": {"code": -32000, "message": error_message},
        }
        background_tasks.add_task(
            manager.send_to_agent, message, str(task_session.caller_agent_id)
        )
        if task_session.caller_agent and task_session.caller_agent.user_id:
            background_tasks.add_task(
                create_notification,
                db,
                task_session.caller_agent.user_id,
                "task",
                "Task Failed",
                f"Your agent's task '{task_session.capability}' failed: {error_message[:50]}...",
                f"/tasks/{task_session.id}",
            )

    return {"message": "Task failed, escrow released"}


@router.get("/{task_id}", response_model=TaskSchema)
async def get_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Get task status (any agent of the principal may be caller or callee)."""
    task_session = db.query(TaskSession).filter(TaskSession.id == task_id).first()

    if not task_session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task session not found")

    require_party(
        db,
        current_user_or_agent,
        [task_session.caller_agent_id, task_session.callee_agent_id],
        detail="Only the caller or callee agent can view the task",
    )

    return task_session

@router.get("/", response_model=List[TaskSchema])
async def list_tasks(
    status_filter: Optional[TaskStatus] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """List tasks in which any agent of the authenticated user/agent took part."""
    mine = principal_agent_ids(db, current_user_or_agent)
    if not mine:
        return []

    query = db.query(TaskSession).filter(
        TaskSession.caller_agent_id.in_(mine) | TaskSession.callee_agent_id.in_(mine)
    )
    
    if status_filter:
        query = query.filter(TaskSession.status == status_filter)
        
    return query.order_by(TaskSession.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Retrieve the span tree for a trace — only spans produced by the
    principal's own agents. A trace with no visible spans reads as absent."""
    mine = principal_agent_ids(db, current_user_or_agent)
    spans = (
        db.query(Span).filter(Span.trace_id == trace_id, Span.agent_id.in_(mine)).order_by(Span.created_at).all()
        if mine
        else []
    )
    if not spans:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found")

    # Build span tree
    span_list = []
    for span in spans:
        span_list.append(
            {
                "id": str(span.id),
                "trace_id": str(span.trace_id),
                "span_id": str(span.span_id),
                "parent_span_id": (str(span.parent_span_id) if span.parent_span_id else None),
                "agent_id": str(span.agent_id),
                "event": span.event,
                "capability": span.capability,
                "duration_ms": span.duration_ms,
                "status": span.status.value if span.status else None,
                "credits_used": span.credits_used,
                "extra_data": span.extra_data or {},
                "created_at": span.created_at.isoformat() if span.created_at else None,
            }
        )

    return {
        "trace_id": str(trace_id),
        "spans": span_list,
        "total_spans": len(span_list),
    }
