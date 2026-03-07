import json
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session

from ...auth import verify_token
from ...database import get_db
from ...websocket_manager import manager

router = APIRouter()
logger = logging.getLogger(__name__)


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
        token_data = verify_token(token)

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
        manager.disconnect(connection_id)
    except Exception as e:
        logger.error(f"WebSocket error for agent {agent_id}: {e}")
        manager.disconnect(connection_id)
