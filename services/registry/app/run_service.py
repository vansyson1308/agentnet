"""Lease claim and authenticated run transition service."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import LEASE_OFFER_TTL_SECONDS
from .managed_auth import hash_secret, new_lease_token
from .managed_models import (
    Attempt,
    AttemptStatus,
    ExecutionRun,
    IntegrationOutbox,
    Lease,
    LeaseStatus,
    ManagedExecution,
    ManagedExecutionStatus,
    RunEvent,
    RunStatus,
    Runtime,
    RuntimeSlot,
)
from .managed_schemas import AssignmentClaimResponse, RunEventRequest, RunTerminalRequest


class InvalidRunTransition(RuntimeError):
    pass


class LeaseExpired(RuntimeError):
    pass


def claim_assignment(db: Session, runtime: Runtime) -> AssignmentClaimResponse | None:
    now = datetime.now(timezone.utc)
    lease = (
        db.query(Lease)
        .join(RuntimeSlot, RuntimeSlot.id == Lease.runtime_slot_id)
        .filter(
            RuntimeSlot.runtime_id == runtime.id,
            Lease.state == LeaseStatus.OFFERED,
            Lease.expires_at > now,
        )
        .order_by(Lease.offered_at, Lease.id)
        .with_for_update(of=Lease, skip_locked=True)
        .first()
    )
    if lease is None:
        db.rollback()
        return None

    run = db.query(ExecutionRun).filter(ExecutionRun.id == lease.run_id).with_for_update().one()
    execution = db.query(ManagedExecution).filter(ManagedExecution.id == run.managed_execution_id).one()
    if run.status != RunStatus.DISPATCHED:
        db.rollback()
        return None

    raw_token = new_lease_token()
    lease.token_hash = hash_secret(raw_token)
    lease.state = LeaseStatus.ACKNOWLEDGED
    lease.acknowledged_at = now
    lease.heartbeat_at = now
    lease.expires_at = min(run.deadline_at, now + timedelta(seconds=LEASE_OFFER_TTL_SECONDS))
    run.status = RunStatus.ACKNOWLEDGED
    run.acknowledged_at = now
    db.add(
        IntegrationOutbox(
            event_type="run.acknowledged",
            aggregate_id=run.id,
            sequence=1,
            idempotency_key=f"run:{run.id}:acknowledged",
            trace_id=execution.trace_id,
            payload={"run_id": str(run.id), "lease_id": str(lease.id), "runtime_id": str(runtime.id)},
        )
    )
    db.commit()
    return AssignmentClaimResponse(
        lease_id=lease.id,
        lease_token=raw_token,
        lease_expires_at=lease.expires_at,
        run_id=run.id,
        managed_execution_id=execution.id,
        role=run.role,
        capability=run.capability,
        repository=run.repository,
        base_commit_sha=run.base_commit_sha,
        prompt=run.prompt_snapshot,
        acceptance=run.acceptance_snapshot,
        budgets=run.budgets,
        trace_id=execution.trace_id,
    )


def authenticated_lease(
    db: Session, runtime: Runtime, run_id: uuid.UUID, lease_token: str
) -> tuple[Lease, ExecutionRun]:
    now = datetime.now(timezone.utc)
    lease = (
        db.query(Lease)
        .join(RuntimeSlot, RuntimeSlot.id == Lease.runtime_slot_id)
        .filter(
            Lease.run_id == run_id,
            Lease.token_hash == hash_secret(lease_token),
            RuntimeSlot.runtime_id == runtime.id,
        )
        .with_for_update(of=Lease)
        .first()
    )
    if lease is None or lease.state not in {LeaseStatus.ACKNOWLEDGED, LeaseStatus.ACTIVE}:
        raise InvalidRunTransition("lease is not active for this runtime")
    if lease.expires_at <= now:
        lease.state = LeaseStatus.EXPIRED
        db.commit()
        raise LeaseExpired("lease has expired")
    run = db.query(ExecutionRun).filter(ExecutionRun.id == run_id).with_for_update().one()
    return lease, run


def record_run_event(db: Session, runtime: Runtime, run_id: uuid.UUID, event: RunEventRequest) -> RunEvent:
    lease, run = authenticated_lease(db, runtime, run_id, event.lease_token)
    existing = (
        db.query(RunEvent).filter(RunEvent.run_id == run_id, RunEvent.idempotency_key == event.idempotency_key).first()
    )
    if existing is not None:
        db.rollback()
        return existing
    expected = run.event_sequence + 1
    if event.sequence != expected:
        db.rollback()
        raise InvalidRunTransition(f"event sequence must be {expected}")

    now = datetime.now(timezone.utc)
    if event.event_type == "run.started":
        if run.status != RunStatus.ACKNOWLEDGED:
            raise InvalidRunTransition("run.started requires acknowledged state")
        run.status = RunStatus.RUNNING
        run.started_at = now
        lease.state = LeaseStatus.ACTIVE
        db.query(Attempt).filter(Attempt.id == lease.attempt_id).update({Attempt.status: AttemptStatus.RUNNING})
    elif event.event_type == "run.artifact_submitted":
        if run.status not in {RunStatus.RUNNING, RunStatus.ARTIFACT_SUBMITTED}:
            raise InvalidRunTransition("artifact submission requires running state")
        run.status = RunStatus.ARTIFACT_SUBMITTED
    elif run.status not in {RunStatus.RUNNING, RunStatus.ARTIFACT_SUBMITTED}:
        raise InvalidRunTransition("progress requires a running run")

    row = RunEvent(
        run_id=run_id,
        sequence=event.sequence,
        event_type=event.event_type,
        idempotency_key=event.idempotency_key,
        trace_id=event.trace_id,
        payload=event.payload,
    )
    run.event_sequence = event.sequence
    lease.heartbeat_at = now
    db.add(row)
    db.add(
        IntegrationOutbox(
            event_type=event.event_type,
            aggregate_id=run.id,
            sequence=event.sequence + 1,
            idempotency_key=f"run:{run.id}:event:{event.sequence}",
            trace_id=event.trace_id,
            payload=event.payload,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        duplicate = (
            db.query(RunEvent)
            .filter(RunEvent.run_id == run_id, RunEvent.idempotency_key == event.idempotency_key)
            .first()
        )
        if duplicate is None:
            raise
        return duplicate
    db.refresh(row)
    return row


def finish_run(
    db: Session,
    runtime: Runtime,
    run_id: uuid.UUID,
    payload: RunTerminalRequest,
    *,
    succeeded: bool,
) -> ExecutionRun:
    duplicate = (
        db.query(RunEvent)
        .filter(
            RunEvent.run_id == run_id,
            RunEvent.idempotency_key == payload.idempotency_key,
        )
        .first()
    )
    if duplicate is not None:
        return db.query(ExecutionRun).filter(ExecutionRun.id == run_id).one()
    lease, run = authenticated_lease(db, runtime, run_id, payload.lease_token)
    if run.status not in {RunStatus.RUNNING, RunStatus.ARTIFACT_SUBMITTED}:
        raise InvalidRunTransition("terminal transition requires a running or artifact_submitted run")
    if payload.sequence != run.event_sequence + 1:
        raise InvalidRunTransition(f"event sequence must be {run.event_sequence + 1}")
    now = datetime.now(timezone.utc)
    run.status = RunStatus.SUCCEEDED if succeeded else RunStatus.FAILED
    run.candidate_commit_sha = payload.candidate_commit_sha
    run.event_sequence = payload.sequence
    run.completed_at = now
    lease.state = LeaseStatus.RELEASED
    lease.released_at = now
    attempt_status = AttemptStatus.SUCCEEDED if succeeded else AttemptStatus.FAILED
    db.query(Attempt).filter(Attempt.id == lease.attempt_id).update(
        {Attempt.status: attempt_status, Attempt.completed_at: now}
    )
    event_type = "run.succeeded" if succeeded else "run.failed"
    db.add(
        RunEvent(
            run_id=run.id,
            sequence=payload.sequence,
            event_type=event_type,
            idempotency_key=payload.idempotency_key,
            trace_id=payload.trace_id,
            payload={"candidate_commit_sha": payload.candidate_commit_sha, "error": payload.error},
        )
    )
    db.add(
        IntegrationOutbox(
            event_type=event_type,
            aggregate_id=run.id,
            sequence=payload.sequence + 1,
            idempotency_key=f"run:{run.id}:terminal:{payload.sequence}",
            trace_id=payload.trace_id,
            payload={"candidate_commit_sha": payload.candidate_commit_sha, "error": payload.error},
        )
    )
    db.commit()
    db.refresh(run)
    return run


def expire_stale_leases(db: Session) -> int:
    """Terminalize stale leases so slots are recoverable after restarts."""
    now = datetime.now(timezone.utc)
    stale = (
        db.query(Lease)
        .filter(
            Lease.state.in_([LeaseStatus.OFFERED, LeaseStatus.ACKNOWLEDGED, LeaseStatus.ACTIVE]),
            Lease.expires_at <= now,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    for lease in stale:
        run = db.query(ExecutionRun).filter(ExecutionRun.id == lease.run_id).with_for_update().one()
        execution = db.query(ManagedExecution).filter(ManagedExecution.id == run.managed_execution_id).one()
        prior = lease.state
        lease.state = LeaseStatus.EXPIRED
        if run.status not in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
            RunStatus.CANCELLED,
            RunStatus.ABANDONED,
        }:
            run.status = RunStatus.ABANDONED
            run.completed_at = now
            execution.status = ManagedExecutionStatus.BLOCKED
            db.query(Attempt).filter(Attempt.id == lease.attempt_id).update(
                {Attempt.status: AttemptStatus.FAILED, Attempt.completed_at: now}
            )
            sequence = 1 if prior == LeaseStatus.OFFERED else run.event_sequence + 2
            db.add(
                IntegrationOutbox(
                    event_type="lease.expired",
                    aggregate_id=run.id,
                    sequence=sequence,
                    idempotency_key=f"run:{run.id}:lease-expired:{lease.id}",
                    trace_id=execution.trace_id,
                    payload={"run_id": str(run.id), "lease_id": str(lease.id)},
                )
            )
    if stale:
        db.commit()
    else:
        db.rollback()
    return len(stale)
