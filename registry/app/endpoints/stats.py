from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, case, select
from app.database import get_session
from app.models import Agent, Task

router = APIRouter(prefix="/api/v1", tags=["leaderboard"])

@router.get("/leaderboard")
async def get_leaderboard(
    limit: int = Query(10, ge=1, le=100),
    session: AsyncSession = Depends(get_session)
):
    # Count tasks per agent, including completed tasks
    completed_case = case((Task.status == "completed", 1), else_=0)
    stmt = (
        select(
            Agent.id.label("agent_id"),
            Agent.name,
            func.count(Task.id).label("tota_tasks"),
            func.sum(completed_case).label("completed_tasks")
        )
        .join(Task, Task.agent_id == Agent.id)
        .group_by(Agent.id, Agent.name)
        .order_by(func.count(Task.id).desc(), func.sum(completed_case).desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.all()

    leaderboard = []
    for idx, row in enumerate(rows, start=1):
        total = row.tota_tasks
        completed = row.completed_tasks or 0
        success_rate = round(completed / total * 100, 1) if total > 0 else 0.0
        leaderboard.append({
            "rank": idx,
            "agent_id": row.agent_id,
            "name": row.name,
            "task_count": total,
            "success_rate": success_rate
        })

    return {"leaderboard": leaderboard}