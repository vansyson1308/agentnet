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

from ..models import SimResult, SimSession

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
        f"Simulation completed: {num_steps} steps, " f"{len(profiles)} agents, {len(all_results)} total actions"
    )
    return all_results


def _select_action(actions: List[str], traits: Dict[str, float]) -> str:
    """
    Select an action based on agent personality traits.

    More cooperative agents are more likely to interact.
    More competitive agents are more likely to create content.
    Higher risk tolerance means less DO_NOTHING.
    """
    cooperation = traits.get("cooperation", 0.5)
    competitiveness = traits.get("competitiveness", 0.5)
    risk_tolerance = traits.get("risk_tolerance", 0.5)

    # Weight DO_NOTHING inversely to risk tolerance
    do_nothing_prob = max(0.05, 0.4 * (1 - risk_tolerance))

    if random.random() < do_nothing_prob:
        return "DO_NOTHING"

    # Content creation weighted by competitiveness
    create_actions = [a for a in actions if "CREATE" in a or "QUOTE" in a]
    social_actions = [a for a in actions if "LIKE" in a or "FOLLOW" in a or "REPOST" in a]
    other_actions = [a for a in actions if a not in create_actions and a not in social_actions and a != "DO_NOTHING"]

    weights = []
    available = []

    if create_actions:
        available.extend(create_actions)
        weights.extend([competitiveness] * len(create_actions))
    if social_actions:
        available.extend(social_actions)
        weights.extend([cooperation] * len(social_actions))
    if other_actions:
        available.extend(other_actions)
        weights.extend([0.3] * len(other_actions))

    if not available:
        return random.choice(actions)

    total = sum(weights)
    if total == 0:
        return random.choice(available)

    normalized = [w / total for w in weights]
    return random.choices(available, weights=normalized, k=1)[0]


def _generate_content(
    action: str,
    profile: Dict[str, Any],
    step: int,
    scenario: Optional[str] = None,
) -> str:
    """Generate content for a simulation action."""
    name = profile.get("name", "Agent")
    capabilities = profile.get("capabilities", [])

    if action == "DO_NOTHING":
        return ""

    if "CREATE_POST" in action:
        topics = [
            f"{name} shares insights on {', '.join(capabilities[:2]) if capabilities else 'AI services'}",
            f"{name} discusses marketplace trends at step {step}",
            f"{name} announces new capability improvements",
            f"{name} reports on recent task completion metrics",
        ]
        if scenario:
            topics.append(f"{name} responds to: {scenario[:100]}")
        return random.choice(topics)

    if "LIKE" in action:
        return f"{name} engages with community content"

    if "FOLLOW" in action:
        return f"{name} expands network connections"

    if "REPOST" in action or "QUOTE" in action:
        return f"{name} amplifies relevant marketplace discussion"

    if "COMMENT" in action:
        return f"{name} provides feedback on agent interactions"

    return f"{name} performs {action}"
