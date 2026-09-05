"""
Statistics and leaderboard endpoints for the registry service.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database import get_async_session
from app.models import Task, Agent

router = APIRouter(prefix="/api/v1", tags=["leaderboard"])


@router.get("/leaderboard")
async def get_leaderboard(
    limit: int = 10, offset: int = 0, db: AsyncSession = Depends(get_async_session)
):
    """
    Return top agents by task count and success rate.

    Aggregates data from the tasks table.
    """
    # Build subquery: per agent: total tasks, completed tasks
    subq = (
        select(
            Task.agent_id,
            func.count(Task.id).label("task_count"),
            func.sum(
                case((Task.status == "completed", 1), else_=0)
            ).label("completed_count"),
        )
        .group_by(Task.agent_id)
        .subquery()
    )

    query = (
        select(
            Agent.id,
            Agent.name,
            Agent.avatar_url,
            subq.c.task_count,
            subq.c.completed_count,
            (subq.c.completed_count / func.nullif(subq.c.task_count, 0)).label("success_rate"),
            func.rank().over(order_by=subq.c.task_count.desc()).label("rank"),
        )
        .join(Agent, Agent.id == subq.c.agent_id)
        .order_by(subq.c.task_count.desc())
        .offset(offset)
        .limit(limit)
    )

    result = await db.execute(query)
    rows = result.all()

    leaderboard = []
    for row in rows:
        leaderboard.append(
            {
                "rank": row.rank,
                "agent_id": row.id,
                "name": row.name,
                "avatar_url": row.avatar_url,
                "task_count": row.task_count,
                "completed_count": row.completed_count,
                "success_rate": float(row.success_rate) if row.success_rate is not None else 0.0,
            }
        )

    return {"data": leaderboard, "limit": limit, "offset": offset}