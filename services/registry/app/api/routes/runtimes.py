from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...database import get_db
from ...managed_auth import get_current_runtime, hash_secret, new_runtime_token, require_runtime_registrar
from ...managed_models import Runtime, RuntimeHeartbeat, RuntimeSlot, RuntimeStatus
from ...managed_schemas import (
    AssignmentClaimResponse,
    RuntimeHeartbeatRequest,
    RuntimeRegister,
    RuntimeRegisterResponse,
)
from ...run_service import claim_assignment

router = APIRouter()


@router.post("/register", response_model=RuntimeRegisterResponse, status_code=status.HTTP_201_CREATED)
def register_runtime(
    payload: RuntimeRegister,
    db: Session = Depends(get_db),
    _: None = Depends(require_runtime_registrar),
):
    if db.query(Runtime).filter(Runtime.registration_key == payload.registration_key).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="registration_key already exists; rotate credentials through an operator workflow",
        )
    raw_token = new_runtime_token()
    runtime = Runtime(
        id=uuid.uuid4(),
        registration_key=payload.registration_key,
        name=payload.name,
        agent_id=payload.agent_id,
        role=payload.role,
        adapter=payload.adapter,
        capabilities=payload.capabilities,
        repository_scopes=payload.repository_scopes,
        permissions=payload.permissions,
        model=payload.model,
        provider=payload.provider,
        capacity=payload.capacity,
        token_hash=hash_secret(raw_token),
        status=RuntimeStatus.ONLINE,
        last_heartbeat_at=datetime.now(timezone.utc),
        extra_data=payload.extra_data,
    )
    db.add(runtime)
    for number in range(1, payload.capacity + 1):
        db.add(RuntimeSlot(id=uuid.uuid4(), runtime_id=runtime.id, slot_number=number, enabled=True))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="runtime registration conflicts") from exc
    return RuntimeRegisterResponse(
        id=runtime.id,
        token=raw_token,
        status=runtime.status,
        capacity=runtime.capacity,
        created=True,
    )


@router.post("/{runtime_id}/heartbeat")
def runtime_heartbeat(
    runtime_id: uuid.UUID,
    payload: RuntimeHeartbeatRequest,
    db: Session = Depends(get_db),
    runtime: Runtime = Depends(get_current_runtime),
):
    if runtime.id != runtime_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="runtime identity mismatch")
    existing = (
        db.query(RuntimeHeartbeat)
        .filter(RuntimeHeartbeat.runtime_id == runtime.id, RuntimeHeartbeat.sequence == payload.sequence)
        .first()
    )
    if existing is not None:
        return {"accepted": True, "duplicate": True, "recorded_at": existing.recorded_at}
    now = datetime.now(timezone.utc)
    runtime.last_heartbeat_at = now
    runtime.status = RuntimeStatus.ONLINE
    heartbeat = RuntimeHeartbeat(
        id=uuid.uuid4(),
        runtime_id=runtime.id,
        sequence=payload.sequence,
        run_id=payload.run_id,
        lease_id=payload.lease_id,
        resources=payload.resources,
    )
    db.add(heartbeat)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"accepted": True, "duplicate": True}
    return {"accepted": True, "duplicate": False, "recorded_at": now}


@router.post("/{runtime_id}/assignments/claim", response_model=AssignmentClaimResponse | None)
async def claim_runtime_assignment(
    runtime_id: uuid.UUID,
    response: Response,
    wait_seconds: int = Query(default=0, ge=0, le=30),
    db: Session = Depends(get_db),
    runtime: Runtime = Depends(get_current_runtime),
):
    if runtime.id != runtime_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="runtime identity mismatch")
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        assignment = claim_assignment(db, runtime)
        if assignment is not None:
            return assignment
        if asyncio.get_running_loop().time() >= deadline:
            response.status_code = status.HTTP_204_NO_CONTENT
            return None
        await asyncio.sleep(min(0.5, max(0, deadline - asyncio.get_running_loop().time())))
