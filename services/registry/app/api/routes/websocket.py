import json
import logging
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...auth import get_current_user_or_agent, verify_token
from ...database import get_db
from ...websocket_manager import manager

# Import event bus for subscribing to task state changes
# This assumes an event_bus module with an async subscribe interface
from ...features.event_bus import event_bus

router = APIRouter()
logger = logging.getLogger(__name__)

# Dashboard public feed connections
dashboard_feeds: set[WebSocket] = set()

# Task timeline connections (authenticated)
task_timeline_feeds: set[WebSocket] = set()


@router.websocket("/feed")
async def dashboard_feed(websocket: WebSocket):
    """Public WebSocket endpoint for dashboard live feed.

    Streams broadcast events (task_updates, agent_status, transactions, milestones)
    to all connected dashboard clients. No authentication required.
    """
    await websocket.accept()
    dashboard_feeds.add(websocket)
    try:
        while True:
            # Keep alive — client sends ping, we pong
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "heartbeat", "timestamp": datetime.utcnow().isoformat()})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Dashboard feed error: {e}")
    finally:
        dashboard_feeds.discard(websocket)


@router.websocket("/agent/{agent_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    agent_id: str,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    """WebSocket endpoint for agent communication."""
    # Verify the token
    try:
        token_data = verify_token(token, db=db)
        if token_data.agent_id is None or str(token_data.agent_id) != agent_id:
            await websocket.close(code=1008, reason="Invalid authentication")
            return
    except Exception as e:
        logger.error(f"WebSocket authentication error: {e}")
        await websocket.close(code=1008, reason="Authentication error")
        return

    # Connect the agent
    connection_id = await manager.connect(websocket, token, db)

    if not connection_id:
        return

    try:
        # Handle incoming messages
        while True:
            # Receive message from WebSocket
            data = await websocket.receive_text()

            try:
                # Parse the message
                message = json.loads(data)

                # Handle the message
                response = await manager.handle_message(message, agent_id, db)

                # Send response back to the sender
                if response:
                    await websocket.send_json(response)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON from agent {agent_id}: {data}")
                await websocket.send_json(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "Invalid JSON"},
                    }
                )
            except Exception as e:
                logger.error(f"Error handling message from agent {agent_id}: {e}")
                await websocket.send_json(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32603,
                            "message": f"Internal error: {str(e)}",
                        },
                    }
                )
    except WebSocketDisconnect:
        # Disconnect the agent
        manager.disconnect(connection_id, db)
    except Exception as e:
        logger.error(f"WebSocket error for agent {agent_id}: {e}")
        manager.disconnect(connection_id, db)


@router.websocket("/ws/tasks/timeline")
async def task_timeline_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
):
    """WebSocket endpoint for live task execution timeline.

    Pushes task state change events to authenticated dashboard clients.
    Events include: task_id, agent_name, escrow_amount, current_state,
    previous_state, duration, timestamp.
    """
    # Authenticate via token
    try:
        verify_token(token)  # raises exception on invalid token
    except Exception as e:
        logger.error(f"Timeline WebSocket authentication error: {e}")
        await websocket.close(code=1008, reason="Authentication error")
        return

    await websocket.accept()
    task_timeline_feeds.add(websocket)
    logger.info("Task timeline WebSocket connected")

    queue: asyncio.Queue = asyncio.Queue()

    async def on_task_state_change(event: dict):
        await queue.put(event)

    unsub = await event_bus.subscribe("task_state_changes", on_task_state_change)

    try:
        while True:
            try:
                # Wait for next event from the event bus with a timeout
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                # Send the event to the client
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # No event received within timeout, send heartbeat to keep connection alive
                try:
                    await websocket.send_json({"type": "heartbeat", "timestamp": datetime.utcnow().isoformat()})
                except Exception:
                    break
    except WebSocketDisconnect:
        logger.info("Task timeline WebSocket disconnected")
    except Exception as e:
        logger.error(f"Task timeline WebSocket error: {e}")
    finally:
        unsub()
        task_timeline_feeds.discard(websocket)