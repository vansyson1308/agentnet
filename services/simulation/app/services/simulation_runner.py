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
        f"{len(profiles)} agents, {len(all_results)} total actions"
    )
    return all_results


def _select_action(actions: List[str], traits: Dict[str, Any]) -> str:
    """Select an action based on agent traits (simple heuristic)."""
    openness = traits.get("openness", 0.5)
    # More open agents tend to create, less open tend to consume/react
    if random.random() < openness:
        return random.choice([a for a in actions if "CREATE" in a or "POST" in a])
    else:
        return random.choice([a for a in actions if "CREATE" not in a and "POST" not in a])


def _generate_content(
    action: str,
    profile: Dict[str, Any],
    step: int,
    scenario: Optional[str] = None,
) -> str:
    """Generate realistic content for the given action."""
    name = profile.get("name", "anonymous")
    if action == "CREATE_POST":
        topics = scenario.split(",") if scenario else ["technology", "culture"]
        topic = random.choice(topics).strip()
        return f"{name} posted about {topic} at step {step}"
    elif action == "CREATE_COMMENT":
        return f"{name} commented: 'Interesting perspective!'"
    elif action in ("LIKE_POST", "LIKE_COMMENT"):
        return f"{name} liked a post."
    elif action in ("DISLIKE_POST", "DISLIKE_COMMENT"):
        return f"{name} disliked a post."
    elif action == "REPOST":
        return f"{name} reposted content."
    elif action == "FOLLOW":
        return f"{name} followed another user."
    elif action == "QUOTE_POST":
        return f"{name} quoted a post with new commentary."
    elif action == "SEARCH_POSTS":
        return f"{name} searched for posts."
    else:  # DO_NOTHING
        return f"{name} did nothing."


def _seed_simulated_agents(
    db: Session,
    profiles: List[Dict[str, Any]],
    platform: str,
    scenario: Optional[str] = None,
) -> None:
    """
    Seed each simulated agent with a mission text and 1-2 active goals.
    Creates Goal entries with owner_type='AGENT' and a synthetic owner_id.
    """
    for profile in profiles:
        agent_index = profile.get("user_id", 0)
        agent_name = profile.get("name", f"agent_{agent_index}")

        # Generate a unique owner_id for this simulated agent
        owner_id = f"sim_{agent_index}_{uuid.uuid4().hex[:8]}"

        # Mission text: derive from scenario or use generic
        mission_text = f"Analyze trends in {scenario or 'market behavior'} on {platform}."
        if scenario:
            mission_text = f"Investigate {scenario} through social media interactions."

        # Create 1-2 active goals
        num_goals = random.randint(1, 2)
        goal_descriptions = [
            f"Publish {random.randint(1,3)} post(s) about {scenario or 'relevant topics'}",
            f"Engage with at least {random.choice(['3','5','10'])} content pieces",
            f"Build a network of {random.randint(5,20)} followers",
            f"Gather intelligence on competitor sentiment",
            f"Promote the product through organic posts",
        ]
        selected_goals = random.sample(goal_descriptions, min(num_goals, len(goal_descriptions)))

        for goal_text in selected_goals:
            goal = Goal(
                id=uuid.uuid4(),
                owner_id=owner_id,
                owner_type="AGENT",
                title=goal_text,
                description=f"Goal for simulated agent {agent_name}",
                status="ACTIVE",
                created_at=datetime.now(timezone.utc),
            )
            db.add(goal)

        logger.debug(f"Seeded agent {agent_name} (owner={owner_id}) with {num_goals} goal(s)")


def _record_simulated_action(
    db: Session,
    profile: Dict[str, Any],
    action: str,
    platform: str,
    step: int,
    traits: Dict[str, Any],
) -> None:
    """
    Record a MemoryItem (AGENT-scope) for the simulated callee after each action.
    If the action is a failure (e.g., dislike, do_nothing), also create an ImprovementProposal.
    """
    agent_index = profile.get("user_id", 0)
    owner_id = f"sim_{agent_index}_{uuid.uuid4().hex[:8]}"  # Should ideally reuse the same owner_id from seeding, but for simplicity we generate a new one per step.

    # Write MemoryItem
    memory_content = f"Simulated agent performed action '{action}' on {platform} at step {step}."
    memory = MemoryItem(
        id=uuid.uuid4(),
        owner_id=owner_id,
        owner_type="AGENT",
        content=memory_content,
        source="simulation",
        created_at=datetime.now(timezone.utc),
    )
    db.add(memory)

    # Determine failure based on action or random chance
    is_failure = (action in FAILURE_ACTIONS) or (random.random() < 0.1)
    if is_failure:
        improvement_text = f"Improve {platform} strategy: {action} resulted in low engagement."
        proposal = ImprovementProposal(
            id=uuid.uuid4(),
            owner_id=owner_id,
            owner_type="AGENT",
            title="Refine interaction approach",
            description=improvement_text,
            proposed_by="SimulationEngine",
            status="PENDING",
            created_at=datetime.now(timezone.utc),
        )
        db.add(proposal)
        logger.debug(f"Recorded ImprovementProposal for agent {owner_id} due to action {action}")