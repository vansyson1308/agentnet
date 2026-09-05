from fastapi import APIRouter, Query, Request
from fastapi.templating import Jinja2Templates
from httpx import AsyncClient
from app.config import settings

router = APIRouter(tags=["leaderboard"])
templates = Jinja2Templates(directory="templates")

@router.get("/leaderboard")
async def show_leaderboard(
    request: Request,
    limit: int = Query(10, ge=1, le=100)
):
    async with AsyncClient(base_url=settings.REGISTRY_BASE_URL) as client:
        try:
            resp = await client.get("/api/v1/leaderboard", params={"limit": limit})
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            data = {"leaderboard": []}

    return templates.TemplateResponse(
        "leaderboard.html",
        {"request": request, "leaderboard": data.get("leaderboard", [])}
    )