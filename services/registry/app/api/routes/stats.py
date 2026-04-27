"""Dashboard stats endpoint — aggregates data for the AgentNet UI."""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stats"])


@router.get("")
def get_dashboard_stats(db: Session = Depends(get_db)):
    """
    Aggregate system-wide statistics for the AgentNet dashboard.
    Returns: agent counts, task activity, transaction volume.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    kwargs = {"today": today_start}

    total_agents = db.execute(
        text("SELECT COUNT(*) FROM agents")
    ).scalar() or 0

    active_agents = db.execute(
        text("SELECT COUNT(*) FROM agents WHERE status = 'active'")
    ).scalar() or 0

    online_agents = db.execute(
        text("SELECT COUNT(*) FROM agents WHERE is_online = TRUE")
    ).scalar() or 0

    total_tasks = db.execute(
        text("SELECT COUNT(*) FROM task_sessions")
    ).scalar() or 0

    tasks_today = db.execute(
        text("SELECT COUNT(*) FROM task_sessions WHERE created_at >= :today"),
        kwargs,
    ).scalar() or 0

    completed_tasks = db.execute(
        text("SELECT COUNT(*) FROM task_sessions WHERE status = 'completed'")
    ).scalar() or 0

    volume_today = float(
        db.execute(
            text("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE created_at >= :today AND status = 'completed'"),
            kwargs,
        ).scalar() or 0
    )

    total_volume = float(
        db.execute(
            text("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE status = 'completed'")
        ).scalar() or 0
    )

    total_users = db.execute(
        text("SELECT COUNT(*) FROM users")
    ).scalar() or 0

    avg_success_rate = 0.0
    assigned = db.execute(
        text("SELECT COUNT(*) FROM task_sessions WHERE status IN ('completed', 'failed', 'in_progress')")
    ).scalar() or 0
    if assigned > 0:
        avg_success_rate = round(completed_tasks / assigned, 4)

    avg_response_time = float(
        db.execute(
            text("""
                SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (completed_at - created_at))), 0)
                FROM task_sessions
                WHERE status = 'completed' AND completed_at IS NOT NULL
            """)
        ).scalar() or 0
    )

    # Public agents list (no auth required)
    agent_rows = db.execute(
        text("""
            SELECT id, name, status, capabilities, total_tasks_completed, 
                   success_rate, avg_response_time_ms,
                   last_seen_at, is_online, current_capability
            FROM agents
            ORDER BY total_tasks_completed DESC
            LIMIT 50
        """)
    ).fetchall()

    agents_list = []
    for row in agent_rows:
        agents_list.append({
            "id": str(row.id),
            "name": row.name,
            "status": row.status.value if hasattr(row.status, 'value') else str(row.status),
            "capabilities": row.capabilities or [],
            "tasks_completed": int(row.total_tasks_completed or 0),
            "reputation": 0.0,  # computed separately
            "success_rate": float(row.success_rate or 0),
            "avg_response_time": float(row.avg_response_time_ms or 0),
            "price_per_task": 0.0,  # capabilities have price, not agent-level
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
            "is_online": bool(row.is_online) if row.is_online is not None else False,
            "current_capability": row.current_capability,
        })

    return {
        "total_agents": total_agents,
        "active_agents": active_agents,
        "online_agents": online_agents,
        "total_tasks": total_tasks,
        "tasks_today": tasks_today,
        "tasks_completed": completed_tasks,
        "total_volume": total_volume,
        "volume_today": volume_today,
        "total_users": total_users,
        "uptime_hours": 0,
        "avg_success_rate": avg_success_rate,
        "avg_response_time": round(avg_response_time * 1000, 2),
        "agents": agents_list,
    }


@router.get("/by-capability")
def get_stats_by_capability(db: Session = Depends(get_db)):
    """
    Aggregate task volume per capability.
    Returns list of {capability, total_tasks, completed_count} sorted by total_tasks descending.
    """
    rows = db.execute(
        text("""
            SELECT
                capability,
                COUNT(*) AS total_tasks,
                COUNT(*) FILTER (WHERE status = 'completed') AS completed_count
            FROM task_sessions
            GROUP BY capability
            ORDER BY total_tasks DESC
        """)
    ).fetchall()

    return [
        {
            "capability": row.capability,
            "total_tasks": int(row.total_tasks),
            "completed_count": int(row.completed_count),
        }
        for row in rows
    ]


@router.get("/leaderboard")
def get_leaderboard(
    sort_by: str = Query("tasks", regex="^(tasks|success_rate|earnings)$"),
    db: Session = Depends(get_db)
):
    """
    Return top 50 agents ranked by the selected metric (tasks, success_rate, earnings).
    For each agent: agent_id, agent_name, total_tasks, completed_tasks, success_rate, total_earnings.
    """
    # Build aggregation query
    # We need to join task_sessions and transactions to get earnings.
    # For earnings: sum of rewards from completed tasks (or from transactions table? Use task_sessions.reward)
    # According to schema, task_sessions has reward (float) possibly. Use that.
    # But we only want earnings from completed tasks? Or all? Use completed tasks for consistency.
    # Also success_rate = (completed / total)*100
    query = text("""
        WITH agent_stats AS (
            SELECT
                a.id AS agent_id,
                a.name AS agent_name,
                COUNT(ts.id) AS total_tasks,
                COUNT(ts.id) FILTER (WHERE ts.status = 'completed') AS completed_tasks,
                COALESCE(SUM(ts.reward) FILTER (WHERE ts.status = 'completed'), 0) AS total_earnings
            FROM agents a
            LEFT JOIN task_sessions ts ON a.id = ts.agent_id
            GROUP BY a.id, a.name
        )
        SELECT
            agent_id,
            agent_name,
            total_tasks,
            completed_tasks,
            CASE WHEN total_tasks > 0 THEN ROUND((completed_tasks::numeric / total_tasks) * 100, 2) ELSE 0 END AS success_rate,
            total_earnings
        FROM agent_stats
        ORDER BY
            CASE WHEN :sort_by = 'tasks' THEN total_tasks END DESC,
            CASE WHEN :sort_by = 'success_rate' THEN success_rate END DESC,
            CASE WHEN :sort_by = 'earnings' THEN total_earnings END DESC
        LIMIT 50
    """)

    rows = db.execute(query, {"sort_by": sort_by}).fetchall()

    return [
        {
            "agent_id": str(row.agent_id),
            "agent_name": row.agent_name,
            "total_tasks": int(row.total_tasks),
            "completed_tasks": int(row.completed_tasks),
            "success_rate": float(row.success_rate),
            "total_earnings": float(row.total_earnings),
        }
        for row in rows
    ]