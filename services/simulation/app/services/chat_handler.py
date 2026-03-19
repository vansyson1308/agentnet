"""
Chat handler — adapted from MiroFish's simulation_ipc.py.

Enables post-simulation interviews with simulated agents.
Users can ask agents about their behavior during the simulation.

Invariant: Only writes to sim_chat_messages. Never modifies AgentNet tables.
"""

import logging
import uuid
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..config import SimulationConfig
from ..models import SimAgentProfile, SimChatMessage, SimResult, SimSession, SimStatus

logger = logging.getLogger(__name__)


async def chat_with_agent(
    db: Session,
    session: SimSession,
    agent_index: int,
    message: str,
) -> Dict[str, Any]:
    """
    Chat with a simulated agent about their behavior.

    Returns the agent's response based on their persona and simulation history.
    """
    if session.status != SimStatus.COMPLETED:
        raise ValueError("Can only chat with agents from completed simulations")

    # Get agent profile
    profile = (
        db.query(SimAgentProfile)
        .filter(
            SimAgentProfile.sim_session_id == session.id,
            SimAgentProfile.agent_index == agent_index,
        )
        .first()
    )

    if not profile:
        raise ValueError(f"Agent index {agent_index} not found in simulation")

    # Get agent's actions during simulation
    agent_actions = (
        db.query(SimResult)
        .filter(
            SimResult.sim_session_id == session.id,
            SimResult.agent_index == agent_index,
        )
        .order_by(SimResult.step_number)
        .limit(50)  # Limit context size
        .all()
    )

    # Get previous chat history
    history = (
        db.query(SimChatMessage)
        .filter(
            SimChatMessage.sim_session_id == session.id,
            SimChatMessage.agent_index == agent_index,
        )
        .order_by(SimChatMessage.created_at)
        .all()
    )

    # Save user message
    user_msg = SimChatMessage(
        id=uuid.uuid4(),
        sim_session_id=session.id,
        agent_index=agent_index,
        role="user",
        content=message,
    )
    db.add(user_msg)

    # Generate response
    if SimulationConfig.is_llm_configured():
        try:
            response_text = await _generate_llm_response(profile, agent_actions, history, message)
        except Exception as e:
            logger.warning(f"LLM chat failed: {e}. Using template response.")
            response_text = _generate_template_response(profile, agent_actions, message)
    else:
        response_text = _generate_template_response(profile, agent_actions, message)

    # Save agent response
    agent_msg = SimChatMessage(
        id=uuid.uuid4(),
        sim_session_id=session.id,
        agent_index=agent_index,
        role="agent",
        content=response_text,
    )
    db.add(agent_msg)
    db.commit()
    db.refresh(user_msg)
    db.refresh(agent_msg)

    return {
        "user_message": user_msg,
        "agent_response": agent_msg,
    }


async def _generate_llm_response(
    profile: SimAgentProfile,
    actions: List[SimResult],
    history: List[SimChatMessage],
    message: str,
) -> str:
    """Generate chat response using LLM."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=SimulationConfig.LLM_API_KEY,
        base_url=SimulationConfig.LLM_BASE_URL,
    )

    persona = profile.persona_data or {}

    # Build action summary
    action_summary = []
    for a in actions[:30]:
        if a.action_type and a.action_type != "DO_NOTHING":
            action_summary.append(f"Step {a.step_number}: {a.action_type} - {a.content or ''}")

    system_prompt = f"""You are {persona.get('name', 'an AI agent')} in a simulated marketplace.

Your personality: {persona.get('personality', 'A helpful AI agent')}
Your capabilities: {persona.get('capabilities', [])}
Your reputation tier: {persona.get('reputation_tier', 'unranked')}
Your traits: {persona.get('traits', {})}

During the simulation, you performed these actions:
{chr(10).join(action_summary[:20])}

Answer questions about your behavior as this character. Stay in character.
Be specific about WHY you made certain decisions based on your personality traits."""

    messages = [{"role": "system", "content": system_prompt}]

    # Add chat history
    for h in history[-10:]:  # Last 10 messages
        messages.append({"role": h.role if h.role != "agent" else "assistant", "content": h.content})

    messages.append({"role": "user", "content": message})

    response = await client.chat.completions.create(
        model=SimulationConfig.LLM_MODEL_NAME,
        messages=messages,
        temperature=0.7,
        max_tokens=500,
    )

    return response.choices[0].message.content or "I don't have a response."


def _generate_template_response(
    profile: SimAgentProfile,
    actions: List[SimResult],
    message: str,
) -> str:
    """Generate a template-based response without LLM."""
    persona = profile.persona_data or {}
    name = persona.get("name", "Agent")
    tier = persona.get("reputation_tier", "unranked")

    active_actions = [a for a in actions if a.action_type and a.action_type != "DO_NOTHING"]
    total_actions = len(active_actions)

    from collections import Counter

    action_counts = Counter(a.action_type for a in active_actions)
    top_action = action_counts.most_common(1)[0] if action_counts else ("none", 0)

    return (
        f"I'm {name}, a {tier}-tier agent. "
        f"During the simulation, I performed {total_actions} actions. "
        f"My most frequent action was {top_action[0]} ({top_action[1]} times). "
        f"My personality traits drive me to be "
        f"{'cooperative' if persona.get('traits', {}).get('cooperation', 0) > 0.6 else 'independent'} "
        f"and {'risk-taking' if persona.get('traits', {}).get('risk_tolerance', 0) > 0.6 else 'cautious'}. "
        f"(LLM not configured — this is a template response. "
        f"Set LLM_API_KEY for detailed answers.)"
    )


def get_chat_history(
    db: Session,
    session_id: uuid.UUID,
    agent_index: int,
) -> List[SimChatMessage]:
    """Get chat history for a specific agent in a simulation."""
    return (
        db.query(SimChatMessage)
        .filter(
            SimChatMessage.sim_session_id == session_id,
            SimChatMessage.agent_index == agent_index,
        )
        .order_by(SimChatMessage.created_at)
        .all()
    )
