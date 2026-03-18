"""
Enhanced Reputation System for AgentNet agents.

Computes rich reputation metrics from task_sessions and spans data:
- success_rate: completed / total tasks
- avg_response_time_ms: average from spans
- reliability: 1 - (timeouts / total)
- reputation_tier: unranked → bronze → silver → gold → diamond

Invariant: Reputation is read-only derived data.
It does NOT modify wallet balances or escrow state.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Agent, Span, SpanStatus, TaskSession, TaskStatus

logger = logging.getLogger(__name__)


def compute_reputation_tier(
    success_rate: float,
    total_completed: int,
    avg_response_time_ms: int,
) -> str:
    """
    Compute reputation tier based on performance metrics.

    Tiers (all require minimum task count):
      - diamond: 95%+ success, 50+ tasks, <2s avg
      - gold:    90%+ success, 25+ tasks, <5s avg
      - silver:  80%+ success, 10+ tasks
      - bronze:  60%+ success, 5+ tasks
      - unranked: everything else
    """
    if total_completed >= 50 and success_rate >= 0.95 and avg_response_time_ms < 2000:
        return "diamond"
    elif total_completed >= 25 and success_rate >= 0.90 and avg_response_time_ms < 5000:
        return "gold"
    elif total_completed >= 10 and success_rate >= 0.80:
        return "silver"
    elif total_completed >= 5 and success_rate >= 0.60:
        return "bronze"
    else:
        return "unranked"


def compute_agent_reputation(db: Session, agent_id) -> dict:
    """
    Compute reputation metrics for a single agent from task_sessions and spans.

    Returns dict with all reputation fields.
    Does NOT write to DB — caller decides whether to persist.
    """
    # Count tasks by status (as callee — reputation is about how well you serve)
    task_counts = (
        db.query(
            TaskSession.status,
            func.count(TaskSession.id).label("count"),
        )
        .filter(TaskSession.callee_agent_id == agent_id)
        .group_by(TaskSession.status)
        .all()
    )

    counts = {status.value if hasattr(status, "value") else status: count for status, count in task_counts}

    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    timeout = counts.get("timeout", 0)
    total = completed + failed + timeout

    success_rate = completed / total if total > 0 else 0.0

    # Average response time from spans
    avg_time_result = (
        db.query(func.avg(Span.duration_ms))
        .filter(
            Span.agent_id == agent_id,
            Span.status == SpanStatus.SUCCESS,
            Span.duration_ms.isnot(None),
        )
        .scalar()
    )
    avg_response_time_ms = int(avg_time_result) if avg_time_result else 0

    # Total volume (credits earned as callee)
    volume_result = (
        db.query(func.sum(TaskSession.escrow_amount))
        .filter(
            TaskSession.callee_agent_id == agent_id,
            TaskSession.status == TaskStatus.COMPLETED,
        )
        .scalar()
    )
    total_volume = int(volume_result) if volume_result else 0

    # Compute tier
    tier = compute_reputation_tier(success_rate, completed, avg_response_time_ms)

    # Reliability
    reliability = 1.0 - (timeout / total) if total > 0 else 1.0

    return {
        "total_tasks_completed": completed,
        "total_tasks_failed": failed,
        "total_tasks_timeout": timeout,
        "success_rate": round(success_rate, 4),
        "avg_response_time_ms": avg_response_time_ms,
        "total_volume_credits": total_volume,
        "reputation_tier": tier,
        "reliability": round(reliability, 4),
    }


def update_agent_reputation(db: Session, agent_id) -> Optional[dict]:
    """
    Compute and persist reputation for a single agent.

    Returns the computed metrics dict, or None if agent not found.
    Invariant: Only updates reputation fields, never wallet/escrow.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return None

    metrics = compute_agent_reputation(db, agent_id)

    agent.total_tasks_completed = metrics["total_tasks_completed"]
    agent.total_tasks_failed = metrics["total_tasks_failed"]
    agent.total_tasks_timeout = metrics["total_tasks_timeout"]
    agent.success_rate = metrics["success_rate"]
    agent.avg_response_time_ms = metrics["avg_response_time_ms"]
    agent.total_volume_credits = metrics["total_volume_credits"]
    agent.reputation_tier = metrics["reputation_tier"]
    agent.reputation_updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(agent)

    return metrics


def update_all_reputations(db: Session) -> int:
    """
    Recompute reputation for ALL agents. Called periodically by worker.

    Returns the number of agents updated.
    Invariant: Only updates reputation fields, never wallet/escrow.
    """
    agents = db.query(Agent).all()
    count = 0
    for agent in agents:
        try:
            update_agent_reputation(db, agent.id)
            count += 1
        except Exception as e:
            logger.error(f"Failed to compute reputation for agent {agent.id}: {e}")
    return count
