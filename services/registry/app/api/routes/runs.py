from __future__ import annotations

import base64
import binascii
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...artifact_store import ArtifactIntegrityError, store_bytes
from ...database import get_db
from ...managed_auth import get_current_runtime
from ...managed_models import Artifact, Runtime
from ...managed_schemas import ArtifactCreate, RunEventRequest, RunHeartbeatRequest, RunTerminalRequest
from ...run_service import (
    InvalidRunTransition,
    LeaseExpired,
    authenticated_lease,
    finish_run,
    record_run_event,
)

router = APIRouter()


def _transition_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LeaseExpired):
        return HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{run_id}/heartbeat")
def run_heartbeat(
    run_id: uuid.UUID,
    payload: RunHeartbeatRequest,
    db: Session = Depends(get_db),
    runtime: Runtime = Depends(get_current_runtime),
):
    try:
        lease, run = authenticated_lease(db, runtime, run_id, payload.lease_token)
        from datetime import datetime, timedelta, timezone

        from ...config import LEASE_OFFER_TTL_SECONDS

        now = datetime.now(timezone.utc)
        lease.heartbeat_at = now
        lease.expires_at = min(run.deadline_at, now + timedelta(seconds=LEASE_OFFER_TTL_SECONDS))
        db.commit()
        return {"accepted": True, "run_status": run.status, "lease_state": lease.state}
    except (InvalidRunTransition, LeaseExpired) as exc:
        raise _transition_error(exc) from exc


@router.post("/{run_id}/events")
def run_event(
    run_id: uuid.UUID,
    payload: RunEventRequest,
    db: Session = Depends(get_db),
    runtime: Runtime = Depends(get_current_runtime),
):
    try:
        event = record_run_event(db, runtime, run_id, payload)
        return {"id": event.id, "sequence": event.sequence, "event_type": event.event_type}
    except (InvalidRunTransition, LeaseExpired) as exc:
        raise _transition_error(exc) from exc


@router.post("/{run_id}/artifacts", status_code=status.HTTP_201_CREATED)
def create_artifact(
    run_id: uuid.UUID,
    payload: ArtifactCreate,
    db: Session = Depends(get_db),
    runtime: Runtime = Depends(get_current_runtime),
):
    try:
        _, run = authenticated_lease(db, runtime, run_id, payload.lease_token)
    except (InvalidRunTransition, LeaseExpired) as exc:
        raise _transition_error(exc) from exc

    limits = {"test_result": 256 * 1024, "patch": 1024 * 1024, "manifest": 1024 * 1024}
    max_size = limits.get(payload.artifact_type, 10 * 1024 * 1024)
    if payload.size_bytes > max_size:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="artifact exceeds type limit")

    uri = payload.uri
    if payload.content_base64 is not None:
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
            uri = store_bytes(content, payload.sha256, payload.size_bytes)
        except (binascii.Error, ArtifactIntegrityError) as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    elif not uri or not (uri.startswith("s3://") or uri.startswith("sha256/")):
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported artifact URI")

    existing = (
        db.query(Artifact)
        .filter(
            Artifact.run_id == run.id,
            Artifact.artifact_type == payload.artifact_type,
            Artifact.sha256 == payload.sha256,
        )
        .first()
    )
    if existing is not None:
        db.rollback()
        return {"id": existing.id, "uri": existing.uri, "duplicate": True}
    artifact = Artifact(
        id=uuid.uuid4(),
        run_id=run.id,
        artifact_type=payload.artifact_type,
        uri=uri,
        sha256=payload.sha256,
        size_bytes=payload.size_bytes,
        mime_type=payload.mime_type,
        base_commit_sha=payload.base_commit_sha or run.base_commit_sha,
        candidate_commit_sha=payload.candidate_commit_sha,
        manifest=payload.manifest,
        changed_files=payload.changed_files,
        provenance=payload.provenance,
        usage=payload.usage,
    )
    db.add(artifact)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(Artifact)
            .filter(
                Artifact.run_id == run.id,
                Artifact.artifact_type == payload.artifact_type,
                Artifact.sha256 == payload.sha256,
            )
            .one()
        )
        return {"id": existing.id, "uri": existing.uri, "duplicate": True}
    return {"id": artifact.id, "uri": artifact.uri, "duplicate": False}


@router.post("/{run_id}/complete")
def complete_run(
    run_id: uuid.UUID,
    payload: RunTerminalRequest,
    db: Session = Depends(get_db),
    runtime: Runtime = Depends(get_current_runtime),
):
    if not payload.candidate_commit_sha:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="candidate_commit_sha is required")
    try:
        run = finish_run(db, runtime, run_id, payload, succeeded=True)
        return {"id": run.id, "status": run.status, "candidate_commit_sha": run.candidate_commit_sha}
    except (InvalidRunTransition, LeaseExpired) as exc:
        raise _transition_error(exc) from exc


@router.post("/{run_id}/fail")
def fail_run(
    run_id: uuid.UUID,
    payload: RunTerminalRequest,
    db: Session = Depends(get_db),
    runtime: Runtime = Depends(get_current_runtime),
):
    try:
        run = finish_run(db, runtime, run_id, payload, succeeded=False)
        return {"id": run.id, "status": run.status}
    except (InvalidRunTransition, LeaseExpired) as exc:
        raise _transition_error(exc) from exc
