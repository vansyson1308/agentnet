from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...database import get_db
from ...managed_auth import require_managed_service
from ...managed_execution_service import IdempotencyMismatch, create_managed_shadow
from ...managed_models import ExecutionRun, Lease, LeaseStatus, ManagedExecution, ManagedExecutionStatus, RunStatus
from ...managed_schemas import ManagedExecutionCreate, ManagedExecutionResponse
from ...runtime_allocator import NoRuntimeAvailable

router = APIRouter()


@router.post("", response_model=ManagedExecutionResponse, status_code=status.HTTP_201_CREATED)
def create_execution(
    payload: ManagedExecutionCreate,
    db: Session = Depends(get_db),
    _: None = Depends(require_managed_service),
):
    try:
        response = create_managed_shadow(db, payload)
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except NoRuntimeAvailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if response.idempotent_replay:
        # FastAPI cannot vary the declared status code without a Response
        # dependency; keeping 201 is safe because the body explicitly marks
        # replay and the effect is exactly once.
        return response
    return response


@router.get("/{execution_id}")
def get_execution(
    execution_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: None = Depends(require_managed_service),
):
    execution = db.query(ManagedExecution).filter(ManagedExecution.id == execution_id).first()
    if execution is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="managed execution not found")
    run = db.query(ExecutionRun).filter(ExecutionRun.id == execution.initial_run_id).one()
    lease = db.query(Lease).filter(Lease.run_id == run.id).one()
    return {
        "id": execution.id,
        "task_session_id": execution.task_session_id,
        "status": execution.status,
        "execution_mode": execution.execution_mode,
        "work_item_id": execution.work_item_id,
        "work_item_revision": execution.work_item_revision,
        "run": {"id": run.id, "status": run.status, "runtime_id": run.runtime_id},
        "lease": {"id": lease.id, "state": lease.state, "expires_at": lease.expires_at},
    }


@router.post("/{execution_id}/cancel")
def cancel_execution(
    execution_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: None = Depends(require_managed_service),
):
    execution = db.query(ManagedExecution).filter(ManagedExecution.id == execution_id).with_for_update().first()
    if execution is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="managed execution not found")
    if execution.status in {ManagedExecutionStatus.CANCELLED, ManagedExecutionStatus.FAILED}:
        return {"id": execution.id, "status": execution.status}
    run = db.query(ExecutionRun).filter(ExecutionRun.id == execution.initial_run_id).with_for_update().one()
    if run.status == RunStatus.SUCCEEDED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="completed run cannot be cancelled")
    now = datetime.now(timezone.utc)
    execution.status = ManagedExecutionStatus.CANCELLED
    run.status = RunStatus.CANCELLED
    run.completed_at = now
    db.query(Lease).filter(
        Lease.run_id == run.id,
        Lease.state.in_([LeaseStatus.OFFERED, LeaseStatus.ACKNOWLEDGED, LeaseStatus.ACTIVE]),
    ).update({Lease.state: LeaseStatus.CANCELLED, Lease.released_at: now}, synchronize_session=False)
    db.commit()
    return {"id": execution.id, "status": execution.status}
