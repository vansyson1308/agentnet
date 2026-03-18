"""
Agent Social Graph API — view agent connections and recommendations.

Tracks agent-to-agent interaction patterns from task_sessions,
offers, and referrals. Uses PostgreSQL adjacency table.

Invariant: Social graph is read-only derived data.
Does NOT affect wallet balances or escrow state.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from ...database import get_db
from ...models import Agent, AgentInteraction, InteractionType

router = APIRouter()


@router.get("/{agent_id}/connections")
async def get_agent_connections(
    agent_id: uuid.UUID,
    interaction_type: Optional[str] = Query(None, description="Filter by interaction type"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Get all connections for an agent (who it has interacted with).

    Returns agents ranked by interaction strength (total count).
    """
    # Verify agent exists
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Query interactions (both directions)
    query = db.query(AgentInteraction).filter(
        or_(
            AgentInteraction.from_agent_id == agent_id,
            AgentInteraction.to_agent_id == agent_id,
        )
    )

    if interaction_type:
        try:
            itype = InteractionType(interaction_type)
            query = query.filter(AgentInteraction.interaction_type == itype)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid interaction type: {interaction_type}",
            )

    interactions = query.order_by(desc(AgentInteraction.count)).limit(limit).all()

    # Build connections list
    connections = []
    for interaction in interactions:
        # Determine the "other" agent
        other_id = interaction.to_agent_id if interaction.from_agent_id == agent_id else interaction.from_agent_id
        other_agent = db.query(Agent).filter(Agent.id == other_id).first()

        connections.append(
            {
                "agent_id": str(other_id),
                "agent_name": other_agent.name if other_agent else "unknown",
                "interaction_type": (
                    interaction.interaction_type.value
                    if hasattr(interaction.interaction_type, "value")
                    else str(interaction.interaction_type)
                ),
                "count": interaction.count,
                "total_volume": interaction.total_volume,
                "last_interaction": (
                    interaction.last_interaction_at.isoformat() if interaction.last_interaction_at else None
                ),
                "direction": "outgoing" if interaction.from_agent_id == agent_id else "incoming",
            }
        )

    return {
        "agent_id": str(agent_id),
        "agent_name": agent.name,
        "total_connections": len(connections),
        "connections": connections,
    }


@router.get("/{agent_id}/recommendations")
async def get_agent_recommendations(
    agent_id: uuid.UUID,
    capability: Optional[str] = Query(None, description="Filter by capability"),
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """
    Get agent recommendations based on social graph + reputation.

    Combines:
    1. Agents that this agent's connections have worked with successfully
    2. Reputation scoring (tier, success_rate)
    3. Capability matching (if specified)

    This is the "social recommendation" engine — agents that your
    trusted partners endorse through their interaction history.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Step 1: Find this agent's trusted connections (completed tasks)
    trusted_ids = (
        db.query(AgentInteraction.to_agent_id)
        .filter(
            AgentInteraction.from_agent_id == agent_id,
            AgentInteraction.interaction_type == InteractionType.TASK_COMPLETED,
            AgentInteraction.count >= 2,  # At least 2 successful interactions
        )
        .all()
    )
    trusted_agent_ids = {t[0] for t in trusted_ids}

    if not trusted_agent_ids:
        # No social data — fall back to pure reputation ranking
        query = db.query(Agent).filter(
            Agent.id != agent_id,
            Agent.status == "active",
        )
        if capability:
            query = query.filter(Agent.capabilities.contains([{"name": capability}]))

        agents = query.order_by(desc(Agent.success_rate)).limit(limit).all()

        return {
            "agent_id": str(agent_id),
            "source": "reputation_only",
            "recommendations": [
                {
                    "agent_id": str(a.id),
                    "name": a.name,
                    "reputation_tier": a.reputation_tier,
                    "success_rate": a.success_rate,
                    "reason": "Top-rated agent by reputation",
                }
                for a in agents
            ],
        }

    # Step 2: Find agents that trusted connections have worked with
    recommended_ids = (
        db.query(
            AgentInteraction.to_agent_id,
            func.sum(AgentInteraction.count).label("endorsement_strength"),
        )
        .filter(
            AgentInteraction.from_agent_id.in_(trusted_agent_ids),
            AgentInteraction.interaction_type == InteractionType.TASK_COMPLETED,
            AgentInteraction.to_agent_id != agent_id,
            AgentInteraction.to_agent_id.notin_(trusted_agent_ids),  # Exclude already known
        )
        .group_by(AgentInteraction.to_agent_id)
        .order_by(desc("endorsement_strength"))
        .limit(limit)
        .all()
    )

    recommendations = []
    for rec_id, strength in recommended_ids:
        rec_agent = db.query(Agent).filter(Agent.id == rec_id).first()
        if not rec_agent:
            continue

        # Check capability match if specified
        if capability:
            has_cap = any(c.get("name") == capability for c in (rec_agent.capabilities or []))
            if not has_cap:
                continue

        recommendations.append(
            {
                "agent_id": str(rec_id),
                "name": rec_agent.name,
                "reputation_tier": rec_agent.reputation_tier,
                "success_rate": rec_agent.success_rate,
                "endorsement_strength": int(strength),
                "reason": f"Endorsed by {int(strength)} of your trusted connections",
            }
        )

    return {
        "agent_id": str(agent_id),
        "source": "social_graph",
        "trusted_connections": len(trusted_agent_ids),
        "recommendations": recommendations,
    }
