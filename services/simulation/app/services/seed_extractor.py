"""
Seed data extractor — queries AgentNet's database for simulation seed data.

Extracts agents, interaction history, and task patterns from the existing
social graph to feed into the MiroFish knowledge graph builder.

Invariant: Read-only access to registry tables. Never writes to agents,
wallets, or task_sessions tables.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..schemas import AgentFilter, SeedConfig

logger = logging.getLogger(__name__)


def extract_seed_agents(db: Session, agent_filter: AgentFilter) -> List[Dict[str, Any]]:
    """
    Extract agent data from the registry as simulation seed.

    Returns a list of agent dicts with: id, name, description,
    capabilities, reputation_tier, success_rate, avg_response_time_ms.
    """
    query = text("""
        SELECT
            id, name, description, capabilities,
            reputation_tier, success_rate, avg_response_time_ms,
            total_tasks_completed, total_tasks_failed, endpoint
        FROM agents
        WHERE status IN ('active', 'unverified')
        ORDER BY success_rate DESC NULLS LAST
        LIMIT :limit
    """)

    params = {
        "limit": agent_filter.limit,
    }

    result = db.execute(query, params)
    agents = []

    for row in result.mappings():
        agent_data = dict(row)

        # Apply capability filter if specified
        if agent_filter.capabilities:
            agent_caps = agent_data.get("capabilities") or []
            cap_names = []
            for cap in agent_caps:
                if isinstance(cap, dict):
                    cap_names.append(cap.get("name", ""))
                elif isinstance(cap, str):
                    cap_names.append(cap)

            if not any(c in cap_names for c in agent_filter.capabilities):
                continue

        # Apply reputation tier filter
        if agent_filter.min_reputation_tier:
            tier_order = {"unranked": 0, "bronze": 1, "silver": 2, "gold": 3, "diamond": 4}
            agent_tier = tier_order.get(agent_data.get("reputation_tier", "unranked"), 0)
            min_tier = tier_order.get(agent_filter.min_reputation_tier, 0)
            if agent_tier < min_tier:
                continue

        # Convert UUID to string for JSON serialization
        agent_data["id"] = str(agent_data["id"])
        agents.append(agent_data)

    logger.info(f"Extracted {len(agents)} seed agents")
    return agents


def extract_interactions(db: Session, agent_ids: List[str], time_range_days: int = 90) -> List[Dict[str, Any]]:
    """
    Extract interaction history between the seed agents.

    Returns a list of interaction dicts: from_agent, to_agent,
    type, count, volume, last_interaction.
    """
    if not agent_ids:
        return []

    cutoff = datetime.utcnow() - timedelta(days=time_range_days)

    # Validate UUIDs and build safe parameterized query
    import uuid as uuid_mod

    validated_ids = []
    for aid in agent_ids:
        try:
            validated_ids.append(str(uuid_mod.UUID(aid)))
        except ValueError:
            continue

    if not validated_ids:
        return []

    id_list = "{" + ",".join(validated_ids) + "}"
    query = text("""
        SELECT
            from_agent_id, to_agent_id,
            interaction_type, count, total_volume,
            last_interaction_at
        FROM agent_interactions
        WHERE from_agent_id = ANY(CAST(:id_list AS uuid[]))
          AND to_agent_id = ANY(CAST(:id_list AS uuid[]))
          AND last_interaction_at > :cutoff
        ORDER BY count DESC
    """)

    result = db.execute(
        query,
        {"id_list": id_list, "cutoff": cutoff},
    )

    interactions = []
    for row in result.mappings():
        interaction = dict(row)
        interaction["from_agent_id"] = str(interaction["from_agent_id"])
        interaction["to_agent_id"] = str(interaction["to_agent_id"])
        if interaction.get("last_interaction_at"):
            interaction["last_interaction_at"] = interaction["last_interaction_at"].isoformat()
        interactions.append(interaction)

    logger.info(f"Extracted {len(interactions)} interactions")
    return interactions


def extract_task_history(db: Session, agent_ids: List[str], time_range_days: int = 90) -> List[Dict[str, Any]]:
    """
    Extract task session history for behavioral modeling.

    Returns aggregated task patterns: capability, avg_price,
    success_rate per agent.
    """
    if not agent_ids:
        return []

    cutoff = datetime.utcnow() - timedelta(days=time_range_days)

    import uuid as uuid_mod

    validated_ids = []
    for aid in agent_ids:
        try:
            validated_ids.append(str(uuid_mod.UUID(aid)))
        except ValueError:
            continue

    if not validated_ids:
        return []

    id_list = "{" + ",".join(validated_ids) + "}"
    query = text("""
        SELECT
            callee_agent_id,
            status,
            COUNT(*) as task_count,
            AVG(escrow_amount) as avg_amount,
            AVG(EXTRACT(EPOCH FROM (completed_at - created_at))) as avg_duration_secs
        FROM task_sessions
        WHERE callee_agent_id = ANY(CAST(:id_list AS uuid[]))
          AND created_at > :cutoff
        GROUP BY callee_agent_id, status
        ORDER BY callee_agent_id, task_count DESC
    """)

    result = db.execute(
        query,
        {"id_list": id_list, "cutoff": cutoff},
    )

    tasks = []
    for row in result.mappings():
        task = dict(row)
        task["callee_agent_id"] = str(task["callee_agent_id"])
        if task.get("avg_amount"):
            task["avg_amount"] = float(task["avg_amount"])
        if task.get("avg_duration_secs"):
            task["avg_duration_secs"] = float(task["avg_duration_secs"])
        tasks.append(task)

    logger.info(f"Extracted {len(tasks)} task history records")
    return tasks


def extract_full_seed(db: Session, seed_config: SeedConfig) -> Dict[str, Any]:
    """
    Extract complete seed data for a simulation.

    Returns a dict with: agents, interactions, task_history.
    """
    agents = extract_seed_agents(db, seed_config.agent_filter)
    agent_ids = [a["id"] for a in agents]

    interactions = []
    if seed_config.include_interactions:
        interactions = extract_interactions(db, agent_ids, seed_config.time_range_days)

    task_history = []
    if seed_config.include_task_history:
        task_history = extract_task_history(db, agent_ids, seed_config.time_range_days)

    return {
        "agents": agents,
        "interactions": interactions,
        "task_history": task_history,
        "num_agents": len(agents),
        "num_interactions": len(interactions),
    }
