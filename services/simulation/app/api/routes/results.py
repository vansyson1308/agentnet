"""
Simulation results and report endpoints.

GET /{sim_id}/results — Raw simulation results
GET /{sim_id}/report — Prediction report
GET /{sim_id}/agents — Simulated agent profiles
GET /{sim_id}/agents/{agent_idx}/states — Agent action timeline
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...auth import get_current_user_id
from ...database import get_db
from ...models import SimAgentProfile, SimReport, SimResult, SimSession, SimStatus
from ...schemas import SimAgentProfileResponse, SimReportResponse, SimResultResponse

router = APIRouter()


def _get_session_or_404(db: Session, sim_id: uuid.UUID, user_id: uuid.UUID) -> SimSession:
    session = db.query(SimSession).filter(SimSession.id == sim_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return session


@router.get("/{sim_id}/results", response_model=List[SimResultResponse])
async def get_simulation_results(
    sim_id: uuid.UUID,
    step: Optional[int] = Query(None, description="Filter by step number"),
    agent_index: Optional[int] = Query(None, description="Filter by agent"),
    skip: int = 0,
    limit: int = Query(100, le=1000),
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Get raw simulation results with optional filters."""
    _get_session_or_404(db, sim_id, user_id)

    query = db.query(SimResult).filter(SimResult.sim_session_id == sim_id)

    if step is not None:
        query = query.filter(SimResult.step_number == step)
    if agent_index is not None:
        query = query.filter(SimResult.agent_index == agent_index)

    results = query.order_by(SimResult.step_number, SimResult.agent_index).offset(skip).limit(limit).all()

    return [
        SimResultResponse(
            id=r.id,
            step_number=r.step_number,
            agent_index=r.agent_index,
            action_type=r.action_type,
            content=r.content,
            metadata=r.metadata_,
            created_at=r.created_at,
        )
        for r in results
    ]


@router.get("/{sim_id}/report", response_model=SimReportResponse)
async def get_simulation_report(
    sim_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Get the prediction report for a simulation."""
    session = _get_session_or_404(db, sim_id, user_id)

    if SimStatus(session.status) not in {SimStatus.COMPLETED, SimStatus.GENERATING_REPORT}:
        raise HTTPException(
            status_code=400,
            detail=f"Report not available — simulation status is '{session.status}'",
        )

    report = (
        db.query(SimReport).filter(SimReport.sim_session_id == sim_id).order_by(SimReport.created_at.desc()).first()
    )

    if not report:
        raise HTTPException(status_code=404, detail="Report not yet generated")

    return SimReportResponse(
        id=report.id,
        report_type=report.report_type,
        title=report.title,
        content=report.content,
        summary=report.summary,
        key_findings=report.key_findings,
        confidence_score=report.confidence_score,
        created_at=report.created_at,
    )


@router.get("/{sim_id}/agents", response_model=List[SimAgentProfileResponse])
async def get_simulated_agents(
    sim_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Get all simulated agent profiles."""
    _get_session_or_404(db, sim_id, user_id)

    profiles = (
        db.query(SimAgentProfile)
        .filter(SimAgentProfile.sim_session_id == sim_id)
        .order_by(SimAgentProfile.agent_index)
        .all()
    )
    return profiles


@router.get("/{sim_id}/agents/{agent_idx}/states", response_model=List[SimResultResponse])
async def get_agent_timeline(
    sim_id: uuid.UUID,
    agent_idx: int,
    skip: int = 0,
    limit: int = Query(100, le=1000),
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Get the action timeline for a specific simulated agent."""
    _get_session_or_404(db, sim_id, user_id)

    results = (
        db.query(SimResult)
        .filter(
            SimResult.sim_session_id == sim_id,
            SimResult.agent_index == agent_idx,
        )
        .order_by(SimResult.step_number)
        .offset(skip)
        .limit(limit)
        .all()
    )

    return [
        SimResultResponse(
            id=r.id,
            step_number=r.step_number,
            agent_index=r.agent_index,
            action_type=r.action_type,
            content=r.content,
            metadata=r.metadata_,
            created_at=r.created_at,
        )
        for r in results
    ]
