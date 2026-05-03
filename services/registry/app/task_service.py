"""
Centralized task lifecycle: escrow lock, release, refund.

This module is the SINGLE SOURCE OF TRUTH for task state transitions
and wallet reservation changes. REST routes, WebSocket handlers, and
the worker timeout path all funnel through here. That guarantees the
money invariants:

1. Wallet ``balance_credits`` / ``balance_usdc`` are NEVER modified by
   application code — only by the DB trigger
   ``update_wallet_balances_trigger`` (see ``init-db/02-platform-fee.sql``)
   when a transaction's status flips to ``COMPLETED``.
2. ``reserved_credits`` / ``reserved_usdc`` are modified ONLY here, and
   ONLY after taking a row-level ``SELECT FOR UPDATE`` lock on the
   wallet.
3. State transitions are validated with the shared state machine in
   ``task_contract.validate_state_transition`` so REST and WS produce
   identical task histories.
4. Idempotency: a request carrying an ``Idempotency-Key`` is deduped
   via the UNIQUE constraint on ``transactions.idempotency_key``; the
   second call returns the existing task instead of double-locking
   escrow.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import jsonschema

from sqlalchemy.orm import Session

from .models import (
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
from .task_contract import (
    TaskStatus as ContractStatus,
    compute_input_hash,
    validate_state_transition,
)

# Prometheus metrics — imported lazily so tests that don't have
# prometheus-client still pass at import time.
try:
    from .health import (
        escrow_locked_total,
        escrow_refunded_total,
        escrow_released_total,
        span_persist_failures_total,
    )
except Exception:  # pragma: no cover — health module may fail in unit tests
    class _Noop:
        def labels(self, **_):
            return self

        def inc(self, *_):
            return None

    escrow_locked_total = _Noop()
    escrow_released_total = _Noop()
    escrow_refunded_total = _Noop()
    span_persist_failures_total = _Noop()


class EscrowError(Exception):
    """Raised on any escrow lifecycle violation. Callers translate to HTTP/WS errors."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_status_str(agent: Agent) -> str:
    val = agent.status
    return val.value if hasattr(val, "value") else str(val)


def _validate_capability(callee_agent: Agent, capability_name: str, input_data: Dict) -> Dict:
    cap = next(
        (c for c in (callee_agent.capabilities or []) if c.get("name") == capability_name),
        None,
    )
    if cap is None:
        raise EscrowError(
            f"Callee agent does not have capability {capability_name!r}"
        )
    schema = cap.get("input_schema")
    if schema:
        try:
            jsonschema.validate(instance=input_data, schema=schema)
        except jsonschema.ValidationError as e:
            raise EscrowError(f"Input validation failed: {e.message}")
    return cap


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_task_with_escrow(
    *,
    db: Session,
    caller_agent: Agent,
    callee_agent_id: uuid.UUID,
    capability_name: str,
    input_data: Dict[str, Any],
    max_budget: int,
    currency: str = "credits",
    timeout_seconds: int = 300,
    parent_span_id: Optional[uuid.UUID] = None,
    retry_of_id: Optional[uuid.UUID] = None,
    idempotency_key: Optional[str] = None,
) -> Tuple[TaskSession, Transaction]:
    """Atomically create a task session and lock escrow on the caller's wallet.

    Single-flight, transactional. On any failure the entire DB transaction
    rolls back so neither escrow nor task_session is half-applied.
    """
    currency = currency.lower()
    if currency not in ("credits", "usdc"):
        raise EscrowError(f"Unsupported currency: {currency!r}")

    # Idempotency: short-circuit if this key already exists. The UNIQUE
    # index on transactions.idempotency_key is the ultimate guard against
    # racing inserts (we still need the early-out so the client gets 200).
    if idempotency_key:
        existing_tx = (
            db.query(Transaction)
            .filter(Transaction.idempotency_key == idempotency_key)
            .first()
        )
        if existing_tx is not None:
            existing_task = (
                db.query(TaskSession)
                .filter(TaskSession.id == existing_tx.task_session_id)
                .first()
            )
            if existing_task is not None:
                return existing_task, existing_tx

    callee = db.query(Agent).filter(Agent.id == callee_agent_id).first()
    if callee is None:
        raise EscrowError("Callee agent not found")
    if _agent_status_str(callee) not in ("active", "unverified"):
        raise EscrowError(f"Callee agent is {_agent_status_str(callee)}, not active")

    capability = _validate_capability(callee, capability_name, input_data)
    price = int(capability.get("price", 0))
    if max_budget < price:
        raise EscrowError(
            f"Insufficient budget: requested {price}, provided {max_budget}"
        )

    # Lock the caller wallet row to serialize concurrent task creations.
    caller_wallet = (
        db.query(Wallet)
        .filter(Wallet.owner_type == "agent", Wallet.owner_id == caller_agent.id)
        .with_for_update()
        .first()
    )
    if caller_wallet is None:
        raise EscrowError("Caller wallet not found")

    if currency == "credits":
        available = caller_wallet.balance_credits - caller_wallet.reserved_credits
        if available < price:
            raise EscrowError(
                f"Insufficient available credits: required {price}, available {available}"
            )
        committed = caller_wallet.daily_spent + caller_wallet.reserved_credits
        if committed + price > caller_wallet.spending_cap:
            raise EscrowError(
                f"Transaction would exceed daily spending cap "
                f"(spent={caller_wallet.daily_spent}, "
                f"reserved={caller_wallet.reserved_credits}, "
                f"cap={caller_wallet.spending_cap}, "
                f"requested={price})"
            )
        caller_wallet.reserved_credits += price
    else:
        available = float(caller_wallet.balance_usdc) - float(caller_wallet.reserved_usdc)
        if available < price:
            raise EscrowError(
                f"Insufficient available USDC: required {price}, available {available}"
            )
        caller_wallet.reserved_usdc = float(caller_wallet.reserved_usdc) + price

    input_hash = compute_input_hash(input_data)
    trace_id = uuid.uuid4()
    span_id = uuid.uuid4()
    timeout_at = datetime.utcnow() + timedelta(seconds=timeout_seconds)

    task_session = TaskSession(
        id=uuid.uuid4(),
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        caller_agent_id=caller_agent.id,
        callee_agent_id=callee.id,
        capability=capability_name,
        input=input_data,
        input_hash=input_hash,
        escrow_amount=price,
        currency=CurrencyType[currency.upper()],
        status=TaskStatus.INITIATED,
        timeout_at=timeout_at,
        retry_of_id=retry_of_id,
    )
    db.add(task_session)

    transaction = Transaction(
        id=uuid.uuid4(),
        from_wallet=caller_wallet.id,
        to_wallet=None,  # set on confirm
        amount=price,
        currency=CurrencyType[currency.upper()],
        status=TransactionStatus.PENDING,
        type=TransactionType.PAYMENT,
        task_session_id=task_session.id,
        idempotency_key=idempotency_key,
        extra_data={"input_hash": input_hash},
    )
    db.add(transaction)

    # Persist the initial span in the same transaction so the trace is
    # queryable immediately and a DB error rolls back the escrow lock too.
    db.add(
        Span(
            id=uuid.uuid4(),
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            agent_id=caller_agent.id,
            event="task_created",
            capability=capability_name,
            status=SpanStatus.SUCCESS,
            extra_data={"input_hash": input_hash},
        )
    )

    db.commit()
    db.refresh(task_session)
    db.refresh(transaction)
    escrow_locked_total.labels(currency=currency).inc()
    return task_session, transaction


def start_task(
    *,
    db: Session,
    task_id: uuid.UUID,
    callee_agent: Agent,
) -> TaskSession:
    """Transition INITIATED -> IN_PROGRESS for the given callee."""
    task = (
        db.query(TaskSession)
        .filter(TaskSession.id == task_id)
        .with_for_update()
        .first()
    )
    if task is None:
        raise EscrowError("Task session not found")
    if task.callee_agent_id != callee_agent.id:
        raise EscrowError("Only the callee agent can start the task")
    if task.status == TaskStatus.IN_PROGRESS:
        return task  # idempotent
    is_valid, err = validate_state_transition(
        ContractStatus(task.status.value),
        ContractStatus.IN_PROGRESS,
    )
    if not is_valid:
        raise EscrowError(err or "Invalid transition to in_progress")
    if datetime.utcnow() > task.timeout_at:
        raise EscrowError("Task has already timed out")

    task.status = TaskStatus.IN_PROGRESS
    db.add(
        Span(
            id=uuid.uuid4(),
            trace_id=task.trace_id,
            span_id=uuid.uuid4(),
            parent_span_id=task.span_id,
            agent_id=callee_agent.id,
            event="task_started",
            capability=task.capability,
            status=SpanStatus.SUCCESS,
        )
    )
    db.commit()
    db.refresh(task)
    return task  # start_task — no escrow metric here, just status change.


def confirm_task_completion(
    *,
    db: Session,
    callee_agent: Agent,
    task_id: uuid.UUID,
    output: Dict[str, Any],
) -> TaskSession:
    """Transition IN_PROGRESS -> COMPLETED, release escrow reservation, mark transaction COMPLETED.

    DB trigger ``update_wallet_balances_trigger`` performs the actual
    balance transfer (caller -> callee + platform fee) when the transaction
    flips to COMPLETED. App code only touches reserved_*.
    """
    task = (
        db.query(TaskSession)
        .filter(TaskSession.id == task_id)
        .with_for_update()
        .first()
    )
    if task is None:
        raise EscrowError("Task session not found")
    if task.callee_agent_id != callee_agent.id:
        raise EscrowError("Only the callee agent can confirm the task")
    if task.status == TaskStatus.COMPLETED:
        return task  # idempotent
    is_valid, err = validate_state_transition(
        ContractStatus(task.status.value),
        ContractStatus.COMPLETED,
    )
    if not is_valid:
        raise EscrowError(err or "Invalid transition to completed")

    callee_wallet = (
        db.query(Wallet)
        .filter(Wallet.owner_type == "agent", Wallet.owner_id == callee_agent.id)
        .with_for_update()
        .first()
    )
    if callee_wallet is None:
        raise EscrowError("Callee wallet not found")

    caller_wallet = (
        db.query(Wallet)
        .filter(
            Wallet.owner_type == "agent",
            Wallet.owner_id == task.caller_agent_id,
        )
        .with_for_update()
        .first()
    )
    if caller_wallet is None:
        raise EscrowError("Caller wallet not found")

    transaction = (
        db.query(Transaction)
        .filter(
            Transaction.task_session_id == task.id,
            Transaction.status == TransactionStatus.PENDING,
        )
        .with_for_update()
        .first()
    )
    if transaction is None:
        raise EscrowError("Pending transaction missing for in_progress task")

    # Output schema validation, best-effort.
    cap = next(
        (c for c in (callee_agent.capabilities or []) if c.get("name") == task.capability),
        None,
    )
    if cap and cap.get("output_schema"):
        try:
            jsonschema.validate(instance=output, schema=cap["output_schema"])
        except jsonschema.ValidationError as e:
            raise EscrowError(f"Output validation failed: {e.message}")

    # Release reserved (mirror of create_task_with_escrow's lock).
    if task.currency == CurrencyType.CREDITS:
        if caller_wallet.reserved_credits < task.escrow_amount:
            raise EscrowError(
                "Internal: reserved_credits underflow on confirm — "
                "indicates a bug in escrow accounting"
            )
        caller_wallet.reserved_credits -= task.escrow_amount
    else:
        if float(caller_wallet.reserved_usdc) < task.escrow_amount:
            raise EscrowError(
                "Internal: reserved_usdc underflow on confirm"
            )
        caller_wallet.reserved_usdc = float(caller_wallet.reserved_usdc) - task.escrow_amount

    task.status = TaskStatus.COMPLETED
    task.completed_at = datetime.utcnow()
    task.output = output

    transaction.to_wallet = callee_wallet.id
    transaction.status = TransactionStatus.COMPLETED
    transaction.completed_at = datetime.utcnow()

    duration_ms = int((datetime.utcnow() - task.created_at).total_seconds() * 1000)
    db.add(
        Span(
            id=uuid.uuid4(),
            trace_id=task.trace_id,
            span_id=uuid.uuid4(),
            parent_span_id=task.span_id,
            agent_id=callee_agent.id,
            event="task_completed",
            capability=task.capability,
            duration_ms=duration_ms,
            status=SpanStatus.SUCCESS,
            credits_used=task.escrow_amount,
        )
    )

    db.commit()
    db.refresh(task)
    escrow_released_total.labels(
        currency=task.currency.value if hasattr(task.currency, "value") else str(task.currency)
    ).inc()
    return task


def fail_task_with_refund(
    *,
    db: Session,
    task_id: uuid.UUID,
    error_message: str,
    callee_agent_id: Optional[uuid.UUID] = None,
    new_status: TaskStatus = TaskStatus.FAILED,
) -> TaskSession:
    """Atomic fail-or-timeout transition with escrow refund.

    Both the REST/WS callee-driven fail path and the worker timeout path
    use this. Atomicity is enforced by taking row-level locks on the task,
    transaction, and caller wallet before any mutation.
    """
    if new_status not in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
        raise EscrowError(f"fail_task_with_refund only handles failed/timeout, got {new_status}")

    task = (
        db.query(TaskSession)
        .filter(TaskSession.id == task_id)
        .with_for_update()
        .first()
    )
    if task is None:
        raise EscrowError("Task session not found")
    if callee_agent_id is not None and task.callee_agent_id != callee_agent_id:
        raise EscrowError("Only the callee agent can fail the task")
    # Idempotency: terminal state -> noop.
    if task.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.REFUNDED):
        return task
    is_valid, err = validate_state_transition(
        ContractStatus(task.status.value),
        ContractStatus(new_status.value),
    )
    if not is_valid:
        raise EscrowError(err or f"Invalid transition to {new_status.value}")

    transaction = (
        db.query(Transaction)
        .filter(
            Transaction.task_session_id == task.id,
            Transaction.status == TransactionStatus.PENDING,
        )
        .with_for_update()
        .first()
    )
    caller_wallet = (
        db.query(Wallet)
        .filter(
            Wallet.owner_type == "agent",
            Wallet.owner_id == task.caller_agent_id,
        )
        .with_for_update()
        .first()
    )

    if transaction is not None and caller_wallet is not None:
        if task.currency == CurrencyType.CREDITS:
            if caller_wallet.reserved_credits < task.escrow_amount:
                raise EscrowError(
                    "Internal: reserved_credits underflow on refund — "
                    "indicates a bug in escrow accounting"
                )
            caller_wallet.reserved_credits -= task.escrow_amount
        else:
            if float(caller_wallet.reserved_usdc) < task.escrow_amount:
                raise EscrowError(
                    "Internal: reserved_usdc underflow on refund"
                )
            caller_wallet.reserved_usdc = float(caller_wallet.reserved_usdc) - task.escrow_amount

        transaction.status = TransactionStatus.CANCELLED
        transaction.completed_at = datetime.utcnow()

    task.status = new_status
    task.error_message = error_message
    task.completed_at = datetime.utcnow()
    if new_status == TaskStatus.TIMEOUT:
        task.refund_at = datetime.utcnow()

    duration_ms = int((datetime.utcnow() - task.created_at).total_seconds() * 1000)
    span_status = SpanStatus.TIMEOUT if new_status == TaskStatus.TIMEOUT else SpanStatus.FAILED
    db.add(
        Span(
            id=uuid.uuid4(),
            trace_id=task.trace_id,
            span_id=uuid.uuid4(),
            parent_span_id=task.span_id,
            agent_id=task.callee_agent_id,
            event=f"task_{new_status.value}",
            capability=task.capability,
            duration_ms=duration_ms,
            status=span_status,
            extra_data={"error_message": error_message},
        )
    )

    db.commit()
    db.refresh(task)
    escrow_refunded_total.labels(
        currency=task.currency.value if hasattr(task.currency, "value") else str(task.currency),
        reason=new_status.value,
    ).inc()
    return task
