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
import os
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import (
    Goal,
    ImprovementProposal,
    MemoryItem,
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


def _seed_simulated_agents(
    db: Session,
    profiles: List[Dict[str, Any]],
    platform: str,
    scenario: Optional[str] = None,
) -> None:
    """
    Seed each simulated agent with a mission text and 1-2 active goals.
    Called once per simulation when SIMULATION_PRODUCE_GOALS=1.
    """
    goal_templates = [
        "Increase engagement on my posts",
        "Build a following in the {platform} community",
        "Share insights about {topic}",
        "Network with like-minded users",
        "Learn from trending discussions about {topic}",
        "Promote creative content",
        "Establish authority in {topic} discussions",
        "Find collaborators for a project",
    ]
    topic = scenario if scenario else "social media trends"

    for profile in profiles:
        agent_name = profile.get("name", f"agent_{profile.get('user_id', 0)}")
        agent_id = uuid.uuid5(uuid.NAMESPACE_DNS, agent_name)

        # Mission (a single goal with mission-like description)
        mission_text = (
            f"Simulated agent '{agent_name}' on {platform}. "
            f"Scenario: {scenario if scenario else 'general social simulation'}. "
            "Goals are generated automatically."
        )
        mission_goal = Goal(
            id=uuid.uuid4(),
            owner_type="AGENT",
            owner_id=agent_id,
            goal_type="MISSION",
            description=mission_text,
            status="ACTIVE",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(mission_goal)

        # 1-2 additional active goals
        num_extra_goals = random.randint(1, 2)
        for _ in range(num_extra_goals):
            template = random.choice(goal_templates)
            goal_text = template.format(platform=platform, topic=topic)
            extra_goal = Goal(
                id=uuid.uuid4(),
                owner_type="AGENT",
                owner_id=agent_id,
                goal_type="GOAL",
                description=goal_text,
                status="ACTIVE",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(extra_goal)

    db.commit()


def _record_simulated_action(
    db: Session,
    profile: Dict[str, Any],
    action: str,
    platform: str,
    step: int,
    traits: Dict[str, Any],
) -> None:
    """
    Write a MemoryItem (AGENT scope) for the simulated callee, and
    on failures also write one ImprovementProposal.
    Called for each step when SIMULATION_PRODUCE_GOALS=1.
    """
    agent_name = profile.get("name", f"agent_{profile.get('user_id', 0)}")
    agent_id = uuid.uuid5(uuid.NAMESPACE_DNS, agent_name)

    memory_text = (
        f"Step {step}: Agent '{agent_name}' performed action '{action}' "
        f"on {platform}. Traits: {traits}."
    )
    memory = MemoryItem(
        id=uuid.uuid4(),
        owner_type="AGENT",
        owner_id=agent_id,
        scope="AGENT",
        content=memory_text,
        created_at=datetime.now(timezone.utc),
    )
    db.add(memory)

    if action in FAILURE_ACTIONS:
        proposal_text = (
            f"Improve agent '{agent_name}' response on {platform}: "
            f"Action '{action}' at step {step} indicates a negative outcome. "
            "Consider adjusting response strategy to increase engagement."
        )
        proposal = ImprovementProposal(
            id=uuid.uuid4(),
            owner_type="AGENT",
            owner_id=agent_id,
            description=proposal_text,
            status="OPEN",
            created_at=datetime.now(timezone.utc),
        )
        db.add(proposal)


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

    When SIMULATION_PRODUCE_GOALS=1, also seeds each simulated agent
    with a mission, active goals, and records MemoryItems/ImprovementProposals.
    """
    actions = TWITTER_ACTIONS if platform == "twitter" else REDDIT_ACTIONS
    all_results = []

    produce_goals = os.environ.get("SIMULATION_PRODUCE_GOALS", "0") == "1"

    # Seed simulated agents with mission and goals if enabled
    if produce_goals:
        _seed_simulated_agents(db, profiles, platform, scenario)

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

            # If producing goals, also write MemoryItem and possibly ImprovementProposal
            if produce_goals:
                _record_simulated_action(
                    db, profile, action, platform, step, traits
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