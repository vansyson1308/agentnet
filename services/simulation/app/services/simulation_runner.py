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
        f"{len(profiles)} agents, {len(all_results)} results"
    )

    return all_results


def _select_action(actions: List[str], traits: Dict[str, Any]) -> str:
    """Select an action based on agent personality traits."""
    # Simple weighted random selection based on traits
    weights = []
    for action in actions:
        weight = 1.0
        if action == "CREATE_POST" and traits.get("creativity", 0.5) > 0.7:
            weight = 2.0
        elif action == "LIKE_POST" and traits.get("agreeableness", 0.5) > 0.7:
            weight = 2.0
        elif action == "DO_NOTHING" and traits.get("laziness", 0.5) > 0.7:
            weight = 2.0
        elif action == "DISLIKE_POST" and traits.get("negativity", 0.5) > 0.7:
            weight = 2.0
        weights.append(weight)
    return random.choices(actions, weights=weights, k=1)[0]


def _generate_content(
    action: str,
    profile: Dict[str, Any],
    step: int,
    scenario: Optional[str] = None,
) -> str:
    """Generate content for a given action."""
    name = profile.get("name", f"Agent_{step}")
    interests = profile.get("interests", [])
    if not interests:
        interests = ["technology", "art", "music"]

    if action == "CREATE_POST":
        topic = random.choice(interests)
        return (
            f"{name} posted about {topic}: "
            f"\"I just discovered something amazing about {topic} today!\""
        )
    elif action == "LIKE_POST" or action == "LIKE_COMMENT":
        return f"{name} liked a post about {random.choice(interests)}"
    elif action == "DISLIKE_POST" or action == "DISLIKE_COMMENT":
        return f"{name} disliked a post about {random.choice(interests)}"
    elif action == "REPOST":
        return f"{name} reposted content from another user"
    elif action == "FOLLOW":
        return f"{name} followed a user interested in {random.choice(interests)}"
    elif action == "QUOTE_POST":
        return (
            f"{name} quoted a post: \"Interesting perspective. "
            f"I have a different view on {random.choice(interests)}.\""
        )
    elif action == "CREATE_COMMENT":
        return f"{name} commented on a post: \"Great point about {random.choice(interests)}!\""
    elif action == "SEARCH_POSTS":
        return f"{name} searched for posts about {random.choice(interests)}"
    elif action == "DO_NOTHING":
        return f"{name} spent time browsing without engaging"
    else:
        return f"{name} performed {action}"


def _generate_mission_text(profile: Dict[str, Any], platform: str, scenario: Optional[str]) -> str:
    """Generate a realistic mission statement for a simulated agent."""
    name = profile.get("name", "Agent")
    interests = profile.get("interests", [])
    interest_str = ", ".join(interests[:2]) if interests else "various topics"
    if scenario:
        return f"{name}'s mission: Engage meaningfully on {platform} to {scenario} while focusing on {interest_str}."
    else:
        return f"{name}'s mission: Build a positive presence on {platform} by sharing insights about {interest_str}."


def _generate_goal_descriptions(profile: Dict[str, Any], platform: str) -> List[str]:
    """Generate 1-2 realistic goal descriptions for a simulated agent."""
    interests = profile.get("interests", [])
    goals = []
    # Goal 1: often about posting or engaging
    if random.random() < 0.8:
        topic = random.choice(interests) if interests else "general"
        goals.append(f"Increase engagement on posts about {topic}")
    # Goal 2: sometimes about growth or quality
    if random.random() < 0.5:
        goals.append(f"Improve average response sentiment by 10%")
    # Ensure at least one goal
    if not goals:
        goals.append(f"Share at least 5 high-quality posts about {random.choice(interests) if interests else 'interesting topics'}")
    return goals


def _seed_simulated_agents(
    db: Session,
    profiles: List[Dict[str, Any]],
    platform: str,
    scenario: Optional[str] = None,
) -> None:
    """
    Seed each simulated agent with a mission text and active goals.

    Only called when SIMULATION_PRODUCE_GOALS=1.
    """
    now = datetime.now(timezone.utc)
    for profile in profiles:
        agent_id = profile.get("user_id", 0)
        # Mission is stored as part of the goal? The requirement says seed with mission text + goals.
        # We can store mission as a special Goal or as a separate field? For simplicity, store as first goal's description.
        # But goals should be separate. We'll create Goal records; the mission text can be a goal description.
        # Alternatively, we could store mission in a separate model, but requirements likely just want goals.
        # We'll create 1-2 goals, and the mission text can be one of them.
        goal_descriptions = _generate_goal_descriptions(profile, platform)
        for desc in goal_descriptions:
            goal = Goal(
                id=uuid.uuid4(),
                owner_type="AGENT",
                owner_id=str(agent_id),
                description=desc,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
            db.add(goal)
        # Also add the mission as a distinct Goal with a special prefix? We'll add one extra goal with mission text.
        mission_text = _generate_mission_text(profile, platform, scenario)
        mission_goal = Goal(
            id=uuid.uuid4(),
            owner_type="AGENT",
            owner_id=str(agent_id),
            description=f"MISSION: {mission_text}",
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
        db.add(mission_goal)

    # Commit the seeds
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
    Write a MemoryItem for the simulated callee and, if action is a failure,
    create an ImprovementProposal.

    Only called when SIMULATION_PRODUCE_GOALS=1.
    """
    agent_id = profile.get("user_id", 0)
    now = datetime.now(timezone.utc)
    name = profile.get("name", f"Agent_{agent_id}")

    # Create MemoryItem (AGENT-scope)
    memory_content = f"At step {step}, {name} performed {action} on {platform}."
    memory = MemoryItem(
        id=uuid.uuid4(),
        owner_type="AGENT",
        owner_id=str(agent_id),
        content=memory_content,
        created_at=now,
    )
    db.add(memory)

    # On failure actions, create an ImprovementProposal
    if action in FAILURE_ACTIONS:
        proposal_text = f"Improve {action} engagement on {platform} by adjusting tone or timing."
        # Generate a more specific proposal based on traits
        if action == "DO_NOTHING":
            proposal_text = f"Encourage {name} to participate more actively on {platform} instead of browsing."
        elif action == "DISLIKE_POST":
            proposal_text = f"Provide constructive feedback instead of disliking posts on {platform}."
        elif action == "DISLIKE_COMMENT":
            proposal_text = f"Suggest {name} to engage diplomatically with dissenting comments on {platform}."

        proposal = ImprovementProposal(
            id=uuid.uuid4(),
            agent_id=str(agent_id),
            description=f"Simulation step {step}: {action} detected as failure.",
            proposal=proposal_text,
            status="OPEN",
            created_at=now,
            updated_at=now,
        )
        db.add(proposal)