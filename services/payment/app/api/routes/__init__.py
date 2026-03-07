from fastapi import APIRouter

from .approvals import router as approvals_router
from .transactions import router as transactions_router
from .wallets import router as wallets_router

router = APIRouter()

router.include_router(wallets_router, prefix="/wallets", tags=["wallets"])
router.include_router(transactions_router, prefix="/transactions", tags=["transactions"])
router.include_router(approvals_router, prefix="/approval_requests", tags=["approvals"])
