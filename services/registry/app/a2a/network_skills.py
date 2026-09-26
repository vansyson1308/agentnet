"""Free, immediate skills of the network tenant (the registry itself).

Answered with a ``Message`` (no task, no escrow). Read-only, bounded,
parameterized queries over public marketplace fields only.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import InvalidParamsError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import Agent
from . import cards, config, mapping

MAX_RESULTS = 20
MAX_QUERY = 200


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def choose_skill(message: pb.Message, metadata: Dict[str, Any]) -> str:
    requested = metadata.get("skillId")
    if requested:
        if requested not in (config.SKILL_MARKETPLACE_SEARCH, config.SKILL_AGENT_CARD):
            raise InvalidParamsError(message="unknown skillId for the AgentNet network")
        return requested
    data = mapping.extract_input(message)
    return config.SKILL_AGENT_CARD if "agentId" in data else config.SKILL_MARKETPLACE_SEARCH


def _search(db: Session, data: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    query = str(data.get("query") or data.get("text") or "").strip()[:MAX_QUERY]
    capability = str(data.get("capability") or "").strip()[:128]
    try:
        limit = max(1, min(MAX_RESULTS, int(data.get("limit") or 10)))
        max_price = data.get("maxPrice")
        max_price = None if max_price is None else int(max_price)
    except (TypeError, ValueError):
        raise InvalidParamsError(message="limit and maxPrice must be integers")

    q = db.query(Agent).filter(Agent.status.in_(cards.AGENT_CARD_STATUSES))
    if query:
        like = f"%{_escape_like(query)}%"
        q = q.filter(or_(Agent.name.ilike(like, escape="\\"), Agent.description.ilike(like, escape="\\")))
    if capability:
        q = q.filter(Agent.capabilities.contains([{"name": capability}]))
    rows = q.order_by(Agent.success_rate.desc().nullslast(), Agent.created_at.desc()).limit(200).all()

    results: List[Dict[str, Any]] = []
    for agent in rows:
        skills = [
            {"id": str(c.get("name"))[:128], "price": cards.skill_price(c)}
            for c in (agent.capabilities or [])
            if c.get("name")
        ]
        if max_price is not None and not any(s["price"] <= max_price for s in skills):
            continue
        results.append(
            {
                "agentId": str(agent.id),
                "name": (agent.name or "")[: cards.MAX_NAME],
                "description": (agent.description or "")[:300],
                "skills": skills[:16],
                "tenant": str(agent.id),
                "cardUrl": f"{base_url}/v1/agents/{agent.id}/a2a-card",
            }
        )
        if len(results) >= limit:
            break
    return {"query": query, "count": len(results), "agents": results}


def _card(db: Session, data: Dict[str, Any], base_url: str) -> Optional[Dict[str, Any]]:
    try:
        agent_id = uuid.UUID(str(data.get("agentId")))
    except (TypeError, ValueError):
        raise InvalidParamsError(message="agentId must be a UUID")
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not cards.card_available(agent):
        return None
    return cards.card_json(cards.agent_card(agent, base_url))


def answer(db: Session, message: pb.Message, metadata: Dict[str, Any], base_url: str) -> pb.Message:
    skill = choose_skill(message, metadata)
    data = mapping.extract_input(message)
    context_id = message.context_id or str(uuid.uuid4())
    key = f"{skill}:{message.message_id}"
    if skill == config.SKILL_AGENT_CARD:
        card = _card(db, data, base_url)
        if card is None:
            return mapping.agent_message("", context_id, key, "No available marketplace agent has that id.", {"found": False})
        return mapping.agent_message("", context_id, key, f"Agent card for {card.get('name', '')}.", {"found": True, "card": card})
    result = _search(db, data, base_url)
    text = f"{result['count']} marketplace agent(s) found." if result["count"] else "No marketplace agent matched."
    return mapping.agent_message("", context_id, key, text, result)
