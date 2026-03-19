"""
Simulation CRUD endpoints.

POST /   — Create and start a simulation
GET  /   — List user's simulations
GET  /{sim_id} — Get simulation status
DELETE /{sim_id} — Cancel a simulation
POST /preview — Estimate cost without starting
"""

import asyncio
import logging
import uuid
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...auth import get_current_user_id
from ...config import SimulationConfig
from ...database import get_db
from ...models import SimSession, SimStatus
from ...schemas import (
    SimulationCreate,
    SimulationPreview,
    SimulationPreviewResponse,
    SimulationResponse,
)
from ...services.cost_calculator import estimate_cost
from ...services.seed_extractor import extract_full_seed
from ...services.simulation_manager import run_simulation_pipeline

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/", response_model=SimulationResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_simulation(
    sim_create: SimulationCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """
    Create and start a new simulation.

    Extracts seed data from AgentNet's social graph, generates
    agent personas, runs OASIS simulation, and generates predictions.

    Runs asynchronously in the background.
    """
    if not SimulationConfig.is_llm_configured():
        logger.warning("LLM not configured — simulation will use template-based content")

    # Preview to get agent count and estimate cost
    seed_data = extract_full_seed(db, sim_create.seed_config)
    num_agents = seed_data.get("num_agents", 0)

    if num_agents == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No agents found matching the filter criteria. Adjust your seed configuration.",
        )

    # Calculate cost
    cost = estimate_cost(
        seed_config=sim_create.seed_config,
        simulation_config=sim_create.simulation_config,
        num_seed_agents=num_agents,
    )

    # Check budget
    if sim_create.payment and sim_create.payment.max_budget < cost:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Estimated cost ({cost} credits) exceeds budget ({sim_create.payment.max_budget} credits)",
        )

    # Create simulation session
    session = SimSession(
        id=uuid.uuid4(),
        user_id=user_id,
        name=sim_create.name,
        description=sim_create.description,
        status=SimStatus.INITIALIZING,
        seed_config=sim_create.seed_config.model_dump(),
        simulation_config=sim_create.simulation_config.model_dump(),
        platform=sim_create.simulation_config.platform,
        num_steps=sim_create.simulation_config.num_steps,
        cost_credits=cost,
        progress_pct=0,
    )

    db.add(session)
    db.commit()
    db.refresh(session)

    # Start pipeline in background
    background_tasks.add_task(
        _run_pipeline_sync,
        session_id=session.id,
    )

    logger.info(f"Simulation {session.id} created for user {user_id}")
    return session


def _run_pipeline_sync(session_id: uuid.UUID):
    """Wrapper to run async pipeline from sync BackgroundTasks."""
    from ...database import SessionLocal

    db = SessionLocal()
    try:
        session = db.query(SimSession).filter(SimSession.id == session_id).first()
        if session:
            asyncio.run(run_simulation_pipeline(db, session))
    except Exception as e:
        logger.error(f"Pipeline error for {session_id}: {e}")
    finally:
        db.close()


@router.get("/", response_model=List[SimulationResponse])
async def list_simulations(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """List the current user's simulations."""
    simulations = (
        db.query(SimSession)
        .filter(SimSession.user_id == user_id)
        .order_by(SimSession.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return simulations


@router.get("/{sim_id}", response_model=SimulationResponse)
async def get_simulation(
    sim_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Get a simulation by ID."""
    session = db.query(SimSession).filter(SimSession.id == sim_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return session


@router.delete("/{sim_id}")
async def cancel_simulation(
    sim_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Cancel a running simulation."""
    session = db.query(SimSession).filter(SimSession.id == sim_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    terminal_states = {SimStatus.COMPLETED, SimStatus.FAILED, SimStatus.CANCELLED, SimStatus.TIMEOUT}
    if SimStatus(session.status) in terminal_states:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel simulation in state '{session.status}'",
        )

    session.status = SimStatus.CANCELLED
    session.error_message = "Cancelled by user"
    db.commit()

    return {"message": "Simulation cancelled", "simulation_id": str(sim_id)}


@router.post("/preview", response_model=SimulationPreviewResponse)
async def preview_simulation(
    preview: SimulationPreview,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """
    Preview simulation cost and seed data without starting.
    Does not lock escrow.
    """
    seed_data = extract_full_seed(db, preview.seed_config)
    num_agents = seed_data.get("num_agents", 0)

    cost = estimate_cost(
        seed_config=preview.seed_config,
        simulation_config=preview.simulation_config,
        num_seed_agents=num_agents,
    )

    warnings = []
    if not SimulationConfig.is_llm_configured():
        warnings.append("LLM not configured — simulation will use template content")
    if not SimulationConfig.is_zep_configured():
        warnings.append("Zep not configured — knowledge graph will be in-memory only")
    if num_agents < 5:
        warnings.append("Few agents found — consider broadening filter criteria")
    if num_agents > 200:
        warnings.append("Large number of agents — simulation may take longer")

    return SimulationPreviewResponse(
        estimated_cost=cost,
        num_seed_agents=num_agents,
        num_interactions=seed_data.get("num_interactions", 0),
        platform=preview.simulation_config.platform,
        num_steps=preview.simulation_config.num_steps,
        warnings=warnings,
    )
