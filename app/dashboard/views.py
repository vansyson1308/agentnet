"""
Dashboard views including the leaderboard page.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from httpx import AsyncClient

from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    """
    Render the public leaderboard page.
    Fetches data from the registry service's /api/v1/leaderboard endpoint.
    """
    registry_url = settings.registry_service_url.rstrip("/") + "/api/v1/leaderboard"
    async with AsyncClient() as client:
        response = await client.get(registry_url, params={"limit": 50, "offset": 0})
        data = response.json()

    return templates.TemplateResponse(
        "leaderboard.html",
        {"request": request, "agents": data["data"], "limit": data["limit"], "offset": data["offset"]},
    )