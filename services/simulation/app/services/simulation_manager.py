"""
Simulation manager — orchestrates the full MiroFish pipeline.

Pipeline stages:
1. Extract seed data from AgentNet (seed_extractor)
2. Build knowledge graph (graph_builder)
3. Generate agent profiles (profile_generator)
4. Run simulation (simulation_runner)
5. Generate report (report_generator)

State machine transitions are enforced via validate_sim_transition().

Invariant: Escrow is locked BEFORE pipeline starts and released/refunded
via escrow_client. Simulation manager NEVER modifies wallet tables.
"""

import logging
import uuid
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from ..models import (
    SimAgentProfile,
    SimSession,
    SimStatus,
    validate_sim_transition,
)
from .graph_builder import build_knowledge_graph
from .profile_generator import generate_all_profiles
from .report_generator import generate_report
from .seed_extractor import extract_full_seed
from .simulation_runner import run_simulation

logger = logging.getLogger(__name__)


def _transition(session: SimSession, target: SimStatus, db: Session, error: str = None):
    """Safely transition simulation state."""
    if not validate_sim_transition(SimStatus(session.status), target):
        raise ValueError(f"Invalid state transition: {session.status} -> {target.value}")
    session.status = target
    if error:
        session.error_message = error
    session.updated_at = datetime.utcnow()
    db.commit()


async def run_simulation_pipeline(
    db: Session,
    session: SimSession,
    on_progress: Optional[Callable] = None,
):
    """
    Run the complete simulation pipeline asynchronously.

    This is the main entry point called by the API route after
    the simulation session is created and escrow is locked.
    """
    try:
        # Stage 1: Extract seed data
        _transition(session, SimStatus.BUILDING_GRAPH, db)
        if on_progress:
            await on_progress(5, "Extracting seed data from AgentNet...")

        seed_data = extract_full_seed(db, session.seed_config_parsed)

        if not seed_data.get("agents"):
            _transition(session, SimStatus.FAILED, db, error="No agents found matching filter criteria")
            return

        # Stage 2: Build knowledge graph
        if on_progress:
            await on_progress(15, "Building knowledge graph...")

        graph = await build_knowledge_graph(
            seed_data=seed_data,
            project_id=str(session.id),
        )

        # Stage 3: Generate agent profiles
        _transition(session, SimStatus.GENERATING_AGENTS, db)
        if on_progress:
            await on_progress(25, "Generating agent personas...")

        sim_config = session.simulation_config or {}
        injected_agents = None
        if sim_config.get("injected_agents"):
            from ..schemas import InjectedAgent

            injected_agents = [InjectedAgent(**ia) for ia in sim_config["injected_agents"]]

        profiles = generate_all_profiles(
            seed_data=seed_data,
            graph=graph,
            injected_agents=injected_agents,
        )

        # Save profiles to database
        for profile in profiles:
            db_profile = SimAgentProfile(
                id=uuid.uuid4(),
                sim_session_id=session.id,
                source_agent_id=uuid.UUID(profile["source_agent_id"]) if profile.get("source_agent_id") else None,
                persona_name=profile.get("name", f"Agent-{profile.get('user_id', 0)}"),
                persona_data=profile,
                is_injected=profile.get("is_injected", False),
                agent_index=profile.get("user_id", 0),
            )
            db.add(db_profile)

        session.num_simulated_agents = len(profiles)
        db.commit()

        # Stage 4: Run simulation
        _transition(session, SimStatus.RUNNING, db)
        session.started_at = datetime.utcnow()
        db.commit()

        if on_progress:
            await on_progress(30, "Running social simulation...")

        async def sim_progress(pct, msg):
            # Map simulation progress (0-100) to overall progress (30-80)
            overall = 30 + int(pct * 0.5)
            session.progress_pct = overall
            db.commit()
            if on_progress:
                await on_progress(overall, msg)

        scenario = sim_config.get("scenario")
        await run_simulation(
            db=db,
            session=session,
            profiles=profiles,
            scenario=scenario,
            on_progress=sim_progress,
        )

        # Stage 5: Generate report
        _transition(session, SimStatus.GENERATING_REPORT, db)
        if on_progress:
            await on_progress(85, "Generating prediction report...")

        await generate_report(
            db=db,
            session=session,
            profiles=profiles,
            scenario=scenario,
        )

        # Complete
        _transition(session, SimStatus.COMPLETED, db)
        session.completed_at = datetime.utcnow()
        session.progress_pct = 100
        db.commit()

        if on_progress:
            await on_progress(100, "Simulation completed!")

        logger.info(f"Simulation {session.id} completed successfully")

    except Exception as e:
        logger.error(f"Simulation {session.id} failed: {e}", exc_info=True)
        try:
            session.status = SimStatus.FAILED
            session.error_message = str(e)[:500]
            session.updated_at = datetime.utcnow()
            db.commit()
        except Exception:
            db.rollback()


# Helper property for SimSession to parse seed_config
@property
def _seed_config_parsed(self):
    """Parse seed_config JSONB into SeedConfig object."""
    from ..schemas import SeedConfig

    if isinstance(self.seed_config, dict):
        return SeedConfig(**self.seed_config)
    return SeedConfig()


# Monkey-patch the property onto SimSession
SimSession.seed_config_parsed = _seed_config_parsed
