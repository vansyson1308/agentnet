from fastapi import APIRouter

from .agents import router as agents_router
from .auth import router as auth_router
from .graph import router as graph_router
from .offers import router as offers_router
from .tasks import router as tasks_router
from .websocket import router as websocket_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(agents_router, prefix="/agents", tags=["agents"])
router.include_router(tasks_router, prefix="/tasks", tags=["tasks"])
router.include_router(offers_router, prefix="/offers", tags=["offers"])
router.include_router(graph_router, prefix="/graph", tags=["social-graph"])
router.include_router(websocket_router, prefix="/ws", tags=["websocket"])
