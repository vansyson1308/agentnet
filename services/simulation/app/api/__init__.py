from fastapi import APIRouter

from .routes.chat import router as chat_router
from .routes.feedback import router as feedback_router
from .routes.results import router as results_router
from .routes.simulations import router as simulations_router

router = APIRouter(prefix="/v1")

router.include_router(simulations_router, prefix="/simulations", tags=["simulations"])
router.include_router(results_router, prefix="/simulations", tags=["results"])
router.include_router(chat_router, prefix="/simulations", tags=["chat"])
router.include_router(feedback_router, prefix="/simulations", tags=["feedback"])
