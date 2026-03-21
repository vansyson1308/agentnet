import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_agent, get_current_user_or_agent, hash_input
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
from ...schemas import TaskCreate, TaskUpdate
from ...tracing import get_tracer
from ...websocket_manager import manager

router = APIRouter()
tracer = get_tracer(__name__)


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


def save_span(db: Session, span_data: SpanCreate) -> Span:
    """Persist a span to the database."""
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


def validate_input_against_schema(input_data: Dict, capability: Dict):
    """Validate input data against capability's input_schema using jsonschema."""
    import jsonschema

    input_schema = capability.get("input_schema")
    if input_schema:
        try:
            jsonschema.validate(instance=input_data, schema=input_schema)
        except jsonschema.ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Input validation failed: {e.message}",
            )


@router.post("/", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_task_session(
    task: TaskCreate,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Create a task session and lock escrow."""
    # Get the caller agent - can be authenticated as agent or as user who owns the agent
    from ...models import User

    if isinstance(current_user_or_agent, Agent):
        current_agent = current_user_or_agent
    elif isinstance(current_user_or_agent, User):
        # User must own the caller agent
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication")

    # Verify that the caller agent is the current agent
    if task.caller_agent_id != current_agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Caller agent ID must match the current agent",
        )

    # Check if the callee agent exists
    callee_agent = db.query(Agent).filter(Agent.id == task.callee_agent_id).first()
    if not callee_agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Callee agent not found")

    # Check if the callee agent is active
    if callee_agent.status not in ("active", "unverified"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Callee agent is {callee_agent.status}, not active",
        )

    # Check if the callee agent has the requested capability
    capability = None
    for cap in callee_agent.capabilities:
        if cap["name"] == task.capability:
            capability = cap
            break

    if not capability:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Callee agent does not have capability {task.capability}",
        )

    # Validate input against capability schema
    validate_input_against_schema(task.input, capability)

    # Get the price for the capability
    price = capability.get("price", 0)

    # Check if the max budget is sufficient
    if task.max_budget < price:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient budget: requested {price}, provided {task.max_budget}",
        )

    # Get the caller's wallet
    caller_wallet = db.query(Wallet).filter(Wallet.owner_type == "agent", Wallet.owner_id == current_agent.id).first()

    if not caller_wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Caller wallet not found")

    # Check if the caller has sufficient balance
    if task.currency == "credits":
        available = caller_wallet.balance_credits - caller_wallet.reserved_credits
        if available < price:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient available credits: required {price}, available {available}",
            )

        # Check if the transaction would exceed the spending cap
        if caller_wallet.daily_spent + price > caller_wallet.spending_cap:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Transaction would exceed daily spending cap of {caller_wallet.spending_cap}",
            )

        # Reserve the credits
        caller_wallet.reserved_credits += price
    else:
        available = float(caller_wallet.balance_usdc) - float(caller_wallet.reserved_usdc)
        if available < price:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient available USDC: required {price}, available {available}",
            )

        # Reserve the USDC
        caller_wallet.reserved_usdc += price

    db.commit()

    # Hash the input for audit
    input_hash = hash_input(task.input)

    # Create a task session
    trace_id = uuid.uuid4()
    span_id = uuid.uuid4()

    # Set timeout
    timeout_at = datetime.utcnow() + timedelta(seconds=task.timeout_seconds)

    task_session = TaskSession(
        id=uuid.uuid4(),
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=task.parent_span_id if hasattr(task, "parent_span_id") else None,
        caller_agent_id=current_agent.id,
        callee_agent_id=callee_agent.id,
        capability=task.capability,
        input_hash=input_hash,
        escrow_amount=price,
        currency=CurrencyType[task.currency.upper()],
        status=TaskStatus.INITIATED,
        timeout_at=timeout_at,
    )

    db.add(task_session)
    db.commit()
    db.refresh(task_session)

    # Create a transaction record
    transaction = Transaction(
        id=uuid.uuid4(),
        from_wallet=caller_wallet.id,
        to_wallet=None,  # Will be set when the task is completed
        amount=price,
        currency=CurrencyType[task.currency.upper()],
        status=TransactionStatus.PENDING,
        type=TransactionType.PAYMENT,
        task_session_id=task_session.id,
        extra_data={"input_hash": input_hash},
    )

    db.add(transaction)
    db.commit()

    # Save span for task creation
    save_span(
        db,
        SpanCreate(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=(task.parent_span_id if hasattr(task, "parent_span_id") else None),
            agent_id=current_agent.id,
            event="task_created",
            capability=task.capability,
            status="success",
        ),
    )

    # Send a message to the callee agent via WebSocket
    message = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "trace_id": str(trace_id),
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

    await manager.send_to_agent(message, str(callee_agent.id))

    return {
        "task_session_id": str(task_session.id),
        "trace_id": str(trace_id),
        "span_id": str(span_id),
    }


@router.put("/{task_id}/start")
async def start_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Callee confirms start. Updates status to in_progress."""
    current_agent = _resolve_agent(current_user_or_agent, db)
    task_session = db.query(TaskSession).filter(TaskSession.id == task_id).first()

    if not task_session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task session not found")

    if task_session.callee_agent_id != current_agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the callee agent can start the task",
        )

    if task_session.status != TaskStatus.INITIATED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task is in {task_session.status} status, cannot start",
        )

    # Check if task has already timed out
    if datetime.utcnow() > task_session.timeout_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Task has already timed out")

    task_session.status = TaskStatus.IN_PROGRESS
    db.commit()

    # Save span
    save_span(
        db,
        SpanCreate(
            trace_id=task_session.trace_id,
            span_id=uuid.uuid4(),
            parent_span_id=task_session.span_id,
            agent_id=current_agent.id,
            event="task_started",
            capability=task_session.capability,
            status="success",
        ),
    )

    return {"message": "Task started successfully"}


@router.put("/{task_id}/confirm")
async def confirm_task(
    task_id: uuid.UUID,
    output: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Callee reports completion. Releases escrow via DB triggers."""
    current_agent = _resolve_agent(current_user_or_agent, db)
    task_session = db.query(TaskSession).filter(TaskSession.id == task_id).first()

    if not task_session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task session not found")

    if task_session.callee_agent_id != current_agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the callee agent can confirm the task",
        )

    if task_session.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task is in {task_session.status} status, cannot confirm",
        )

    # Get the pending transaction
    transaction = (
        db.query(Transaction)
        .filter(
            Transaction.task_session_id == task_session.id,
            Transaction.status == TransactionStatus.PENDING,
        )
        .first()
    )

    if not transaction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending transaction not found",
        )

    # Get callee's wallet for the transaction destination
    callee_wallet = db.query(Wallet).filter(Wallet.owner_type == "agent", Wallet.owner_id == current_agent.id).first()

    if not callee_wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Callee wallet not found")

    # Release reserved funds from caller (this is escrow release, not balance transfer)
    caller_wallet = (
        db.query(Wallet)
        .filter(
            Wallet.owner_type == "agent",
            Wallet.owner_id == task_session.caller_agent_id,
        )
        .first()
    )

    if caller_wallet:
        if task_session.currency == CurrencyType.CREDITS:
            caller_wallet.reserved_credits -= task_session.escrow_amount
        else:
            caller_wallet.reserved_usdc -= task_session.escrow_amount

    # Update the task status
    task_session.status = TaskStatus.COMPLETED
    task_session.completed_at = datetime.utcnow()
    task_session.output = output

    # Update the transaction — DB trigger update_wallet_balances_trigger
    # will handle the actual balance transfer (from caller → callee)
    transaction.to_wallet = callee_wallet.id
    transaction.status = TransactionStatus.COMPLETED
    transaction.completed_at = datetime.utcnow()

    db.commit()

    # Calculate duration for span
    duration_ms = int((datetime.utcnow() - task_session.created_at).total_seconds() * 1000)

    # Save span
    save_span(
        db,
        SpanCreate(
            trace_id=task_session.trace_id,
            span_id=uuid.uuid4(),
            parent_span_id=task_session.span_id,
            agent_id=current_agent.id,
            event="task_completed",
            capability=task_session.capability,
            duration_ms=duration_ms,
            status="success",
            credits_used=task_session.escrow_amount,
        ),
    )

    # Send result to caller via WebSocket
    message = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "trace_id": str(task_session.trace_id),
        "result": output,
        "credits_charged": task_session.escrow_amount,
    }

    await manager.send_to_agent(message, str(task_session.caller_agent_id))

    return {"message": "Task confirmed successfully"}


@router.put("/{task_id}/fail")
async def fail_task(
    task_id: uuid.UUID,
    error_message: str,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Callee reports failure. Triggers refund via DB triggers."""
    current_agent = _resolve_agent(current_user_or_agent, db)
    task_session = db.query(TaskSession).filter(TaskSession.id == task_id).first()

    if not task_session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task session not found")

    if task_session.callee_agent_id != current_agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the callee agent can fail the task",
        )

    if task_session.status not in [TaskStatus.INITIATED, TaskStatus.IN_PROGRESS]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task is in {task_session.status} status, cannot fail",
        )

    # Get the pending transaction
    transaction = (
        db.query(Transaction)
        .filter(
            Transaction.task_session_id == task_session.id,
            Transaction.status == TransactionStatus.PENDING,
        )
        .first()
    )

    if not transaction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending transaction not found",
        )

    # Release reserved funds from caller wallet
    caller_wallet = (
        db.query(Wallet)
        .filter(
            Wallet.owner_type == "agent",
            Wallet.owner_id == task_session.caller_agent_id,
        )
        .first()
    )

    if caller_wallet:
        if task_session.currency == CurrencyType.CREDITS:
            caller_wallet.reserved_credits -= task_session.escrow_amount
        else:
            caller_wallet.reserved_usdc -= task_session.escrow_amount

    # Update the task status
    task_session.status = TaskStatus.FAILED
    task_session.error_message = error_message
    task_session.completed_at = datetime.utcnow()

    # Cancel the transaction (DB trigger won't fire for cancelled status,
    # only for completed, so no double-update issue)
    transaction.status = TransactionStatus.CANCELLED
    transaction.completed_at = datetime.utcnow()

    db.commit()

    # Calculate duration for span
    duration_ms = int((datetime.utcnow() - task_session.created_at).total_seconds() * 1000)

    # Save span
    save_span(
        db,
        SpanCreate(
            trace_id=task_session.trace_id,
            span_id=uuid.uuid4(),
            parent_span_id=task_session.span_id,
            agent_id=current_agent.id,
            event="task_failed",
            capability=task_session.capability,
            duration_ms=duration_ms,
            status="failed",
            metadata={"error_message": error_message},
        ),
    )

    # Send error to caller via WebSocket
    message = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "trace_id": str(task_session.trace_id),
        "error": {"code": -32000, "message": error_message},
    }

    await manager.send_to_agent(message, str(task_session.caller_agent_id))

    return {"message": "Task failed, escrow released"}


@router.get("/{task_id}", response_model=TaskSchema)
async def get_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Get task status."""
    current_agent = _resolve_agent(current_user_or_agent, db)
    task_session = db.query(TaskSession).filter(TaskSession.id == task_id).first()

    if not task_session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task session not found")

    if task_session.caller_agent_id != current_agent.id and task_session.callee_agent_id != current_agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the caller or callee agent can view the task",
        )

    return task_session


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Retrieve full span tree for a trace."""
    spans = db.query(Span).filter(Span.trace_id == trace_id).order_by(Span.created_at).all()

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
