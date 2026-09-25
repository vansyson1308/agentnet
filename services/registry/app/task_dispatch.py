"""Deliver a freshly created task to its callee (shared by REST and A2A).

Extracted unchanged from ``api/routes/tasks.py`` so the REST route and the
A2A gateway notify callees identically: the live WebSocket first, then the
agent's registered webhook through the sandbox. Delivery is out-of-band and
non-fatal; the callee can always poll ``GET /v1/tasks/{id}``. Nothing here
touches escrow.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

audit_logger = logging.getLogger("agentnet.audit")


def build_execute_message(
    *,
    task_session_id: uuid.UUID,
    trace_id: uuid.UUID,
    caller_agent_id: uuid.UUID,
    capability: str,
    input_data: Dict[str, Any],
    max_budget: int,
    currency: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "trace_id": str(trace_id),
        "method": "execute",
        "from": str(caller_agent_id),
        "params": {
            "capability": capability,
            "input": input_data,
            "payment": {
                "max_budget": max_budget,
                "currency": currency,
                "escrow_session_id": str(task_session_id),
            },
            "timeout_seconds": timeout_seconds,
        },
    }


async def dispatch_execute(
    *,
    message: Dict[str, Any],
    task_session_id: uuid.UUID,
    callee_agent_id: uuid.UUID,
    callee_endpoint: Optional[str],
) -> Optional[str]:
    """Send ``message`` to the callee. Returns the fulfillment channel
    (``"websocket"``, ``"webhook"``) or ``None`` when neither was available."""
    from .websocket_manager import manager

    if await manager.send_to_agent(message, str(callee_agent_id)):
        return "websocket"
    if not callee_endpoint:
        return None
    try:
        from .sandbox import sandboxed_call

        asyncio.create_task(sandboxed_call(url=callee_endpoint, method="POST", json_body=message))
        audit_logger.info(f"Task {task_session_id} dispatched via Webhook to {callee_endpoint}")
    except Exception as e:  # noqa: BLE001 - dispatch is best-effort by contract
        audit_logger.error(f"Webhook dispatch failed for task {task_session_id}: {e}")
    return "webhook"
