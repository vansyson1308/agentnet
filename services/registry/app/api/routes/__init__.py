from fastapi import APIRouter
from .agents import router as agents_router
from .tasks import router as tasks_router
from .websocket import router as websocket_router
from .auth import router as auth_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(agents_router, prefix="/agents", tags=["agents"])
router.include_router(tasks_router, prefix="/tasks", tags=["tasks"])
router.include_router(websocket_router, prefix="/ws", tags=["websocket"])