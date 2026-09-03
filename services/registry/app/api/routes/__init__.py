from fastapi import APIRouter

from .agents import router as agents_router
from .auth import router as auth_router
from .discovery import router as discovery_router
from .graph import router as graph_router
from .offers import router as offers_router
from .tasks import router as tasks_router
from .websocket import router as websocket_router
from .health import router as health_router

from .notifications import router as notifications_router
from .stats import router as stats_router
from .stories import router as stories_router
from .chat import router as chat_router
from .goals import router as goals_router
from .improvements import router as improvements_router
from .memory import router as memory_router
from .fleet import router as fleet_router
from .catalog import router as catalog_router
from .tokens import router as tokens_router
from .projects import router as projects_router
from .orchestrator import router as orchestrator_router
from .society import router as society_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(agents_router, prefix="/agents", tags=["agents"])
router.include_router(discovery_router, prefix="/agents", tags=["agents"])
router.include_router(tasks_router, prefix="/tasks", tags=["tasks"])
router.include_router(offers_router, prefix="/offers", tags=["offers"])
router.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
router.include_router(graph_router, prefix="/graph", tags=["social-graph"])
router.include_router(websocket_router, prefix="/ws", tags=["websocket"])
router.include_router(stats_router, prefix="/stats", tags=["stats"])
router.include_router(stories_router, prefix="/stories", tags=["stories"])
router.include_router(chat_router, prefix="/chat", tags=["chat"])
router.include_router(health_router, prefix="/v1/health", tags=["health"])
# Phase: agent-goals-and-self-improvement
router.include_router(goals_router, prefix="/goals", tags=["goals"])
router.include_router(improvements_router, prefix="/improvements", tags=["improvements"])
router.include_router(memory_router, prefix="/memory", tags=["memory"])
router.include_router(fleet_router, tags=["fleet"])
# AgentNet Provisioning Protocol (APP) — AB-415 through AB-418
router.include_router(catalog_router, tags=["catalog"])
router.include_router(tokens_router, tags=["tokens"])
router.include_router(projects_router, tags=["projects"])
router.include_router(orchestrator_router, tags=["orchestrator"])
# Autonomous Society Runtime v1 — observability + JARVIS-style queries
router.include_router(society_router, tags=["society"])
