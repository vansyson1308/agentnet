"""
Escrow client — HTTP interface to registry/payment for escrow operations.

Uses existing task_session mechanism with capability 'swarm_simulation'.

Invariant: This client NEVER modifies wallet tables directly.
All escrow operations go through the registry's task session API.
"""

import logging
import os
import uuid
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8001")
TIMEOUT = 30.0


async def lock_escrow(
    user_token: str,
    caller_agent_id: uuid.UUID,
    callee_agent_id: uuid.UUID,
    amount: float,
    currency: str = "credits",
    capability: str = "swarm_simulation",
) -> Optional[dict]:
    """
    Lock escrow via registry task session creation.

    Returns the created task_session dict on success, None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{REGISTRY_URL}/api/v1/tasks/",
                headers={"Authorization": f"Bearer {user_token}"},
                json={
                    "caller_agent_id": str(caller_agent_id),
                    "callee_agent_id": str(callee_agent_id),
                    "capability": capability,
                    "input_data": {"type": "simulation"},
                    "escrow_amount": amount,
                    "currency": currency,
                    "timeout_seconds": int(os.getenv("SIMULATION_TIMEOUT_SECONDS", "3600")),
                },
            )

        if resp.status_code in (200, 201, 202):
            data = resp.json()
            logger.info(f"Escrow locked: task_session={data.get('id')}, amount={amount}")
            return data
        else:
            logger.error(f"Failed to lock escrow: {resp.status_code} — {resp.text}")
            return None

    except Exception as e:
        logger.error(f"Escrow lock request failed: {e}")
        return None


async def release_escrow(
    user_token: str,
    task_session_id: uuid.UUID,
) -> bool:
    """
    Release escrow (complete the task session) — pays the callee.

    Returns True on success.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{REGISTRY_URL}/api/v1/tasks/{task_session_id}/complete",
                headers={"Authorization": f"Bearer {user_token}"},
                json={"output_data": {"type": "simulation_complete"}},
            )

        if resp.status_code in (200, 201):
            logger.info(f"Escrow released: task_session={task_session_id}")
            return True
        else:
            logger.error(f"Failed to release escrow: {resp.status_code} — {resp.text}")
            return False

    except Exception as e:
        logger.error(f"Escrow release request failed: {e}")
        return False


async def refund_escrow(
    user_token: str,
    task_session_id: uuid.UUID,
    reason: str = "simulation_failed",
) -> bool:
    """
    Refund escrow (cancel the task session) — returns funds to caller.

    Returns True on success.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{REGISTRY_URL}/api/v1/tasks/{task_session_id}/cancel",
                headers={"Authorization": f"Bearer {user_token}"},
                json={"reason": reason},
            )

        if resp.status_code in (200, 201):
            logger.info(f"Escrow refunded: task_session={task_session_id}, reason={reason}")
            return True
        else:
            logger.error(f"Failed to refund escrow: {resp.status_code} — {resp.text}")
            return False

    except Exception as e:
        logger.error(f"Escrow refund request failed: {e}")
        return False
