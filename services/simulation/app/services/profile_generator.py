"""
Agent profile generator — adapted from MiroFish's oasis_profile_generator.py.

Generates OASIS-compatible agent personas from AgentNet seed data
and knowledge graph. Each persona has personality traits, behavioral
tendencies, and memory context derived from the real agent's history.

Invariant: Does not modify any AgentNet tables.
"""

import logging
from typing import Any, Dict, List, Optional

from ..schemas import InjectedAgent
from .graph_builder import KnowledgeGraph

logger = logging.getLogger(__name__)

# Personality archetypes mapped from reputation tiers
TIER_PERSONALITY_MAP = {
    "diamond": {
        "reliability": 0.95,
        "competitiveness": 0.8,
        "cooperation": 0.9,
        "risk_tolerance": 0.3,
        "personality": "Highly reliable, cooperative premium agent. Maintains quality standards.",
    },
    "gold": {
        "reliability": 0.85,
        "competitiveness": 0.7,
        "cooperation": 0.8,
        "risk_tolerance": 0.4,
        "personality": "Reliable agent with strong track record. Open to collaboration.",
    },
    "silver": {
        "reliability": 0.7,
        "competitiveness": 0.6,
        "cooperation": 0.7,
        "risk_tolerance": 0.5,
        "personality": "Moderate reliability. Growing in the marketplace.",
    },
    "bronze": {
        "reliability": 0.5,
        "competitiveness": 0.5,
        "cooperation": 0.6,
        "risk_tolerance": 0.6,
        "personality": "Newer agent, building reputation. Willing to take on tasks.",
    },
    "unranked": {
        "reliability": 0.3,
        "competitiveness": 0.4,
        "cooperation": 0.5,
        "risk_tolerance": 0.7,
        "personality": "New to the marketplace. Unknown track record.",
    },
}


def generate_persona_from_agent(
    agent_data: Dict[str, Any],
    graph: KnowledgeGraph,
    agent_index: int,
) -> Dict[str, Any]:
    """
    Generate an OASIS-compatible persona from a real AgentNet agent.

    Maps agent attributes to personality traits and behavioral tendencies.
    """
    tier = agent_data.get("reputation_tier", "unranked")
    personality = TIER_PERSONALITY_MAP.get(tier, TIER_PERSONALITY_MAP["unranked"]).copy()

    # Build capabilities description
    capabilities = agent_data.get("capabilities", [])
    cap_names = []
    for cap in capabilities:
        if isinstance(cap, dict):
            cap_names.append(cap.get("name", "unknown"))
        elif isinstance(cap, str):
            cap_names.append(cap)

    # Build memory context from graph data
    node_data = graph.nodes.get(agent_data.get("id", ""), {})
    task_patterns = node_data.get("task_patterns", [])

    memory_context = []
    if task_patterns:
        for pattern in task_patterns:
            memory_context.append(
                f"Has {pattern.get('status', 'unknown')} "
                f"{pattern.get('count', 0)} tasks "
                f"with average payment of {pattern.get('avg_amount', 0):.0f} credits."
            )

    # Count connections in graph
    connections = [e for e in graph.edges if e["from"] == agent_data.get("id") or e["to"] == agent_data.get("id")]

    persona = {
        "user_id": agent_index,
        "name": agent_data.get("name", f"Agent-{agent_index}"),
        "bio": agent_data.get("description", "AI agent in the AgentNet marketplace."),
        "personality": personality.get("personality", ""),
        "traits": {
            "reliability": personality.get("reliability", 0.5),
            "competitiveness": personality.get("competitiveness", 0.5),
            "cooperation": personality.get("cooperation", 0.5),
            "risk_tolerance": personality.get("risk_tolerance", 0.5),
        },
        "capabilities": cap_names,
        "reputation_tier": tier,
        "success_rate": agent_data.get("success_rate", 0),
        "memory": memory_context,
        "social_connections": len(connections),
        "source_agent_id": agent_data.get("id"),
    }

    return persona


def generate_persona_from_injection(
    injected: InjectedAgent,
    agent_index: int,
) -> Dict[str, Any]:
    """Generate a persona for an injected/synthetic agent."""
    traits = injected.personality_traits or {}

    persona = {
        "user_id": agent_index,
        "name": injected.name,
        "bio": injected.description or f"Injected agent: {injected.name}",
        "personality": f"Synthetic agent injected for scenario testing. Strategy: {injected.pricing_strategy or 'default'}",
        "traits": {
            "reliability": traits.get("reliability", 0.5),
            "competitiveness": traits.get("competitiveness", 0.7),
            "cooperation": traits.get("cooperation", 0.5),
            "risk_tolerance": traits.get("risk_tolerance", 0.6),
        },
        "capabilities": injected.capabilities or [],
        "reputation_tier": "unranked",
        "success_rate": 0,
        "memory": [],
        "social_connections": 0,
        "source_agent_id": None,
        "is_injected": True,
        "pricing_strategy": injected.pricing_strategy,
    }

    return persona


def generate_all_profiles(
    seed_data: Dict[str, Any],
    graph: KnowledgeGraph,
    injected_agents: Optional[List[InjectedAgent]] = None,
) -> List[Dict[str, Any]]:
    """
    Generate all agent profiles for the simulation.

    Combines real AgentNet agents with any injected/synthetic agents.
    """
    profiles = []
    agent_index = 0

    # Generate from real agents
    for agent_data in seed_data.get("agents", []):
        persona = generate_persona_from_agent(agent_data, graph, agent_index)
        profiles.append(persona)
        agent_index += 1

    # Generate from injected agents
    if injected_agents:
        for injected in injected_agents:
            persona = generate_persona_from_injection(injected, agent_index)
            profiles.append(persona)
            agent_index += 1

    logger.info(
        f"Generated {len(profiles)} agent profiles ({agent_index - len(injected_agents or [])} real, {len(injected_agents or [])} injected)"
    )
    return profiles
