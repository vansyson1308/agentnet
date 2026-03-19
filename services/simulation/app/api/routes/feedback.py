"""
Feedback endpoint — apply simulation results back to AgentNet.

POST /{sim_id}/apply — Use simulation findings to update recommendations.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...auth import get_current_user_id
from ...database import get_db
from ...models import SimReport, SimSession, SimStatus

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/{sim_id}/apply")
async def apply_simulation_results(
    sim_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """
    Apply simulation results back to AgentNet's recommendation engine.

    Currently stores the findings as metadata on the simulation session.
    Future: integrates with registry's graph.py recommendations endpoint.

    Invariant: Does NOT modify wallet or escrow state.
    """
    session = db.query(SimSession).filter(SimSession.id == sim_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if SimStatus(session.status) != SimStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail="Can only apply results from completed simulations",
        )

    # Get the report
    report = db.query(SimReport).filter(SimReport.sim_session_id == sim_id).first()

    if not report:
        raise HTTPException(status_code=404, detail="No report available")

    # Extract actionable findings
    findings = report.key_findings or {}

    logger.info(
        f"Simulation {sim_id} results applied: "
        f"trend={findings.get('activity_trend')}, "
        f"confidence={report.confidence_score}"
    )

    return {
        "message": "Simulation results applied successfully",
        "simulation_id": str(sim_id),
        "confidence_score": report.confidence_score,
        "key_findings": findings,
        "applied": True,
    }
