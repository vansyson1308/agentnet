"""Atomic creation of shadow Task/Execution/Run/Attempt/Lease aggregates."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import LEASE_OFFER_TTL_SECONDS
from .managed_models import (
    Attempt,
    AttemptKind,
    AttemptStatus,
    ExecutionMode,
    ExecutionRun,
    IntegrationOutbox,
    Lease,
    LeaseStatus,
    ManagedExecution,
    ManagedExecutionStatus,
    RunStatus,
)
from .managed_schemas import ManagedExecutionCreate, ManagedExecutionResponse
from .models import CurrencyType, TaskSession, TaskStatus
from .runtime_allocator import NoRuntimeAvailable, allocate_runtime_slot


class IdempotencyMismatch(RuntimeError):
    pass


def canonical_request_hash(payload: ManagedExecutionCreate) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _response(db: Session, execution: ManagedExecution, replay: bool) -> ManagedExecutionResponse:
    run = db.query(ExecutionRun).filter(ExecutionRun.id == execution.initial_run_id).one()
    lease = db.query(Lease).filter(Lease.run_id == run.id).one()
    return ManagedExecutionResponse(
        id=execution.id,
        task_session_id=execution.task_session_id,
        initial_run_id=run.id,
        lease_id=lease.id,
        runtime_id=run.runtime_id,
        status=execution.status,
        run_status=run.status,
        created_at=execution.created_at,
        idempotent_replay=replay,
    )


def create_managed_shadow(db: Session, payload: ManagedExecutionCreate) -> ManagedExecutionResponse:
    """Create the complete initial aggregate and commit exactly once.

    No wallet/transaction row is touched. On allocation failure every added
    object is rolled back, so callers never observe a managed Task without a
    Run and Lease.
    """

    request_hash = canonical_request_hash(payload)
    existing = db.query(ManagedExecution).filter(ManagedExecution.idempotency_key == payload.idempotency_key).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyMismatch("idempotency key was already used with a different payload")
        return _response(db, existing, True)

    now = datetime.now(timezone.utc)
    timeout_seconds = int(payload.budgets.get("time_seconds", 3600))
    timeout_seconds = min(max(timeout_seconds, 30), 86400)

    try:
        runtime, slot = allocate_runtime_slot(
            db,
            role=payload.role,
            capability=payload.capability,
            repository=payload.repository,
            requirements=payload.requirements,
            required_runtime_id=payload.required_runtime_id,
        )

        task = TaskSession(
            id=uuid.uuid4(),
            trace_id=payload.trace_id,
            span_id=uuid.uuid4(),
            capability=payload.capability,
            input={
                "control_plane": payload.control_plane,
                "goal_id": payload.goal_id,
                "work_item_id": payload.work_item_id,
                "revision": payload.work_item_revision,
            },
            input_hash=request_hash,
            escrow_amount=0,
            currency=CurrencyType.CREDITS,
            status=TaskStatus.INITIATED,
            timeout_at=now + timedelta(seconds=timeout_seconds),
            fulfillment_channel="managed_lease",
            execution_mode=ExecutionMode.MANAGED_SHADOW.value,
        )
        db.add(task)
        db.flush()

        execution = ManagedExecution(
            id=uuid.uuid4(),
            control_plane=payload.control_plane,
            goal_id=payload.goal_id,
            work_item_id=payload.work_item_id,
            work_item_revision=payload.work_item_revision,
            external_attempt_no=payload.attempt_no,
            task_session_id=task.id,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            execution_mode=ExecutionMode.MANAGED_SHADOW,
            status=ManagedExecutionStatus.ACTIVE,
            role=payload.role,
            capability=payload.capability,
            priority=payload.priority,
            repository=payload.repository,
            base_commit_sha=payload.base_commit_sha.lower(),
            repository_scope=payload.repository_scope,
            prompt=payload.prompt,
            acceptance_snapshot=payload.acceptance.model_dump(mode="json"),
            requirements=payload.requirements,
            budgets=payload.budgets,
            approval_policy_version=payload.approval_policy_version,
            required_runtime_id=payload.required_runtime_id,
            trace_id=payload.trace_id,
        )
        db.add(execution)
        # Break the intentional aggregate-root/initial-run FK cycle without
        # leaving the transaction: persist Task + root with initial_run_id
        # NULL, then create the Run and bind it before the single commit.
        db.flush()

        run = ExecutionRun(
            id=uuid.uuid4(),
            managed_execution_id=execution.id,
            task_session_id=task.id,
            runtime_id=runtime.id,
            run_number=1,
            role=payload.role,
            capability=payload.capability,
            repository=payload.repository,
            base_commit_sha=payload.base_commit_sha.lower(),
            prompt_snapshot=payload.prompt,
            acceptance_snapshot=payload.acceptance.model_dump(mode="json"),
            budgets=payload.budgets,
            status=RunStatus.DISPATCHED,
            deadline_at=now + timedelta(seconds=timeout_seconds),
        )
        db.add(run)
        db.flush()

        attempt = Attempt(
            id=uuid.uuid4(),
            run_id=run.id,
            attempt_number=1,
            kind=AttemptKind.INITIAL,
            status=AttemptStatus.PENDING,
        )
        db.add(attempt)
        db.flush()

        lease = Lease(
            id=uuid.uuid4(),
            run_id=run.id,
            attempt_id=attempt.id,
            runtime_slot_id=slot.id,
            state=LeaseStatus.OFFERED,
            expires_at=now + timedelta(seconds=LEASE_OFFER_TTL_SECONDS),
        )
        db.add(lease)
        db.flush()
        execution.initial_run_id = run.id

        db.add(
            IntegrationOutbox(
                event_type="managed_execution.created",
                aggregate_id=execution.id,
                sequence=1,
                idempotency_key=f"managed-execution:{execution.id}:1",
                trace_id=payload.trace_id,
                payload={
                    "managed_execution_id": str(execution.id),
                    "task_session_id": str(task.id),
                    "run_id": str(run.id),
                    "lease_id": str(lease.id),
                    "runtime_id": str(runtime.id),
                    "work_item_id": payload.work_item_id,
                    "work_item_revision": payload.work_item_revision,
                    "delivery_semantics": "at-least-once",
                },
            )
        )
        db.commit()
        db.refresh(execution)
        return _response(db, execution, False)
    except (NoRuntimeAvailable, IdempotencyMismatch):
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        winner = db.query(ManagedExecution).filter(ManagedExecution.idempotency_key == payload.idempotency_key).first()
        if winner is None:
            winner = (
                db.query(ManagedExecution)
                .filter(
                    ManagedExecution.control_plane == payload.control_plane,
                    ManagedExecution.work_item_id == payload.work_item_id,
                    ManagedExecution.work_item_revision == payload.work_item_revision,
                    ManagedExecution.external_attempt_no == payload.attempt_no,
                    ManagedExecution.role == payload.role,
                )
                .first()
            )
        if winner is None:
            raise
        if winner.request_hash != request_hash:
            raise IdempotencyMismatch("idempotency key was already used with a different payload")
        return _response(db, winner, True)
    except Exception:
        db.rollback()
        raise
