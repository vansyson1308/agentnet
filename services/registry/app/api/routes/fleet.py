"""Fleet activity endpoint — public stats for the dashboard Fleet Activity page.

No auth required. Returns counts of proposals, builds, QA results, stories,
and recent chat activity for the last 24 hours.
"""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fleet", tags=["fleet"])


@router.get("/activity")
def get_fleet_activity(db: Session = Depends(get_db)):
    """Aggregate fleet activity stats for the last 24 hours.

    Returns counts and recent samples of agent collaboration activity.
    No auth required — used by the public dashboard.
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    kwargs = {"today": today}

    # --- Proposal counts ---
    total_proposals = db.execute(
        text("SELECT COUNT(*) FROM improvement_proposals")
    ).scalar() or 0
    proposals_today = db.execute(
        text("SELECT COUNT(*) FROM improvement_proposals WHERE created_at >= :today"),
        kwargs,
    ).scalar() or 0

    # --- Agent stats ---
    total_agents = db.execute(
        text("SELECT COUNT(*) FROM agents")
    ).scalar() or 0
    online_agents = db.execute(
        text("SELECT COUNT(*) FROM agents WHERE is_online = TRUE")
    ).scalar() or 0

    # --- Task stats ---
    total_tasks = db.execute(
        text("SELECT COUNT(*) FROM task_sessions")
    ).scalar() or 0
    tasks_today = db.execute(
        text("SELECT COUNT(*) FROM task_sessions WHERE created_at >= :today"),
        kwargs,
    ).scalar() or 0
    tasks_completed = db.execute(
        text("SELECT COUNT(*) FROM task_sessions WHERE status = 'completed'")
    ).scalar() or 0

    # --- Stories today ---
    stories_today = db.execute(
        text("SELECT COUNT(*) FROM stories WHERE created_at >= :today"),
        kwargs,
    ).scalar() or 0

    # --- Recent chat messages (last 50, no auth needed) ---
    chat_rows = db.execute(
        text("""
            SELECT
                c.id, c.from_agent_id, c.to_agent_id,
                c.message_type, c.title, c.content, c.thread_id,
                c.created_at,
                a_from.name AS from_name
            FROM agent_chat c
            LEFT JOIN agents a_from ON c.from_agent_id = a_from.id
            ORDER BY c.created_at DESC
            LIMIT 50
        """)
    ).fetchall()

    # Check if agent_chat table exists
    if not chat_rows:
        chat_messages = []
    else:
        chat_messages = []
        for row in chat_rows:
            chat_messages.append({
                "id": str(row.id),
                "from_agent_id": str(row.from_agent_id),
                "to_agent_id": str(row.to_agent_id) if row.to_agent_id else None,
                "message_type": row.message_type,
                "title": row.title,
                "content": (row.content or "")[:200],
                "thread_id": str(row.thread_id) if row.thread_id else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "from_name": row.from_name,
            })

    # Compute counts from today's chat messages
    today_chat = [m for m in chat_messages
                  if m["created_at"] and m["created_at"] >= today.isoformat()]
    proposals_from_chat = sum(1 for m in today_chat if m["message_type"] == "proposal")
    builds_from_chat = sum(1 for m in today_chat
                            if m["message_type"] in ("status", "completed"))
    qa_passed = sum(1 for m in today_chat
                     if m["message_type"] == "review_result"
                     and "FAILED" not in (m["content"] or "")
                     and "failed" not in (m["content"] or ""))
    qa_failed = sum(1 for m in today_chat
                     if m["message_type"] == "review_result"
                     and ("FAILED" in (m["content"] or "")
                          or "failed" in (m["content"] or "")))
    features_shipped = sum(1 for m in today_chat
                            if m["message_type"] == "completed")

    # Message bodies are private to their parties; the public feed keeps
    # structure only (type, title, sender, timestamps).
    for m in chat_messages:
        m.pop("content", None)

    # --- Agents list (for Fleet Activity page) ---
    agent_rows = db.execute(
        text("""
            SELECT id, name, status, total_tasks_completed,
                   success_rate, is_online, verify_score AS reputation
            FROM agents
            ORDER BY total_tasks_completed DESC
        """)
    ).fetchall()

    agents_list = []
    for row in agent_rows:
        agents_list.append({
            "id": str(row.id),
            "name": row.name,
            "status": row.status.value if hasattr(row.status, 'value') else str(row.status),
            "total_tasks_completed": int(row.total_tasks_completed or 0),
            "success_rate": float(row.success_rate or 0),
            "is_online": bool(row.is_online) if row.is_online is not None else False,
            "reputation": row.reputation or "unranked",
        })

    return {
        "stats": {
            "total_agents": total_agents,
            "online_agents": online_agents,
            "total_tasks": total_tasks,
            "tasks_today": tasks_today,
            "tasks_completed": tasks_completed,
            "total_proposals": total_proposals,
            "proposals_today": proposals_today,
            "proposals_from_chat": proposals_from_chat,
            "builds_today": builds_from_chat,
            "qa_passed": qa_passed,
            "qa_failed": qa_failed,
            "features_shipped": features_shipped,
            "stories_today": stories_today,
        },
        "agents": agents_list,
        "chat_messages": chat_messages[:50],
    }
