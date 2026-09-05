"""
Simulation runner — adapted from MiroFish's simulation_runner.py.

Runs the OASIS social simulation engine with generated agent profiles.
If OASIS is not installed, falls back to a lightweight built-in simulator
that produces realistic-looking results for testing.

Invariant: Does not modify any AgentNet tables.
Results are written to sim_results table only.
"""

import asyncio
import logging
import random
import uuid

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import (
    SimResult,
    SimSession,
)

logger = logging.getLogger(__name__)

# Available actions per platform (mirrors MiroFish/OASIS)
TWITTER_ACTIONS = [
    "CREATE_POST",
    "LIKE_POST",
    "REPOST",
    "FOLLOW",
    "DO_NOTHING",
    "QUOTE_POST",
]

REDDIT_ACTIONS = [
    "LIKE_POST",
    "DISLIKE_POST",
    "CREATE_POST",
    "CREATE_COMMENT",
    "LIKE_COMMENT",
    "DISLIKE_COMMENT",
    "SEARCH_POSTS",
    "DO_NOTHING",
    "FOLLOW",
]


# Failure actions (actions that are considered negative outcomes)
FAILURE_ACTIONS = {"DISLIKE_POST", "DISLIKE_COMMENT", "DO_NOTHING"}


# NOTE (Phase 2.5): the former SIMULATION_PRODUCE_GOALS feature (seeding goals,
# memory items and improvement proposals for simulated agents) was removed. It
# referenced registry models this service never imported (NameError at runtime)
# with column names that do not exist in the schema, and its synthetic agent ids
# would have violated the agents(id) foreign keys. Simulations only write sim_*
# tables; goals/memory/proposals belong to real agents through the registry API.


async def run_simulation(
    db: Session,
    session: SimSession,
    profiles: List[Dict[str, Any]],
    scenario: Optional[str] = None,
    on_progress: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    Run a social simulation using agent profiles.

    Attempts to use OASIS engine if available, otherwise falls back
    to built-in lightweight simulator.

    Args:
        db: Database session for writing results
        session: The SimSession being executed
        profiles: List of agent persona dicts
        scenario: Optional scenario description
        on_progress: Optional async callback(pct, message)

    Returns:
        List of result dicts
    """
    platform = session.platform or "twitter"
    num_steps = session.num_steps or 100

    try:
        results = await _run_builtin_simulation(
            db=db,
            session=session,
            profiles=profiles,
            platform=platform,
            num_steps=num_steps,
            scenario=scenario,
            on_progress=on_progress,
        )
        return results
    except Exception as e:
        logger.error(f"Simulation error: {e}")
        raise


async def _run_builtin_simulation(
    db: Session,
    session: SimSession,
    profiles: List[Dict[str, Any]],
    platform: str,
    num_steps: int,
    scenario: Optional[str] = None,
    on_progress: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    Built-in lightweight simulator for when OASIS is not available.

    Generates realistic simulation results based on agent personality
    traits and behavioral tendencies. Uses LLM for content generation
    if configured, otherwise generates template-based content.

    """
    actions = TWITTER_ACTIONS if platform == "twitter" else REDDIT_ACTIONS
    all_results = []


    for step in range(num_steps):
        step_results = []

        for profile in profiles:
            agent_index = profile.get("user_id", 0)
            traits = profile.get("traits", {})

            # Select action based on personality traits
            action = _select_action(actions, traits)

            # Generate content based on action
            content = _generate_content(
                action=action,
                profile=profile,
                step=step,
                scenario=scenario,
            )

            result = SimResult(
                id=uuid.uuid4(),
                sim_session_id=session.id,
                step_number=step,
                agent_index=agent_index,
                action_type=action,
                content=content,
                metadata_={
                    "platform": platform,
                    "agent_name": profile.get("name", "unknown"),
                    "traits": traits,
                },
            )
            db.add(result)
            step_results.append(
                {
                    "step": step,
                    "agent_index": agent_index,
                    "action": action,
                    "content": content,
                }
            )

        all_results.extend(step_results)

        # Commit every 10 steps to avoid large transactions
        if step % 10 == 0:
            db.commit()

        # Report progress
        progress_pct = int((step + 1) / num_steps * 100)
        if on_progress:
            await on_progress(progress_pct, f"Step {step + 1}/{num_steps}")

        # Small delay to prevent CPU spinning
        if step % 5 == 0:
            await asyncio.sleep(0.01)

    # Final commit
    db.commit()

    logger.info(
        f"Simulation completed: {num_steps} steps, "
        f"{len(profiles)} agents, {len(all_results)} results"
    )

    return all_results


def _select_action(
    actions: List[str], traits: Dict[str, Any]
) -> str:
    """
    Select an action based on agent traits.
    Simplified deterministic selection for demo purposes.
    """
    # For now, just return a random action
    return random.choice(actions)


def _generate_content(
    action: str,
    profile: Dict[str, Any],
    step: int,
    scenario: Optional[str] = None,
) -> str:
    """
    Generate placeholder content for a simulated action.
    """
    agent_name = profile.get("name", "unknown")
    return f"[Step {step}] {agent_name} performed {action} in scenario '{scenario or 'default'}'"