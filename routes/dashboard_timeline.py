from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import List
import json

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Store connected WebSocket clients
connected_clients: List[WebSocket] = []

@router.get("/dashboard/timeline", response_class=HTMLResponse)
async def get_timeline_partial(request: Request):
    """Return the timeline HTML partial to be included in the dashboard."""
    return templates.TemplateResponse("dashboard_timeline.html", {"request": request})

@router.websocket("/ws/timeline")
async def timeline_websocket(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        while True:
            # Keep connection alive – we push updates from external calls
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.remove(websocket)

async def broadcast_task_state(task_id: str, state: str):
    """Broadcast a task state change to all connected timeline clients."""
    message = json.dumps({
        "task_id": task_id,
        "state": state,
        "timestamp": datetime.utcnow().isoformat()
    })
    for client in connected_clients[:]:
        try:
            await client.send_text(message)
        except Exception:
            connected_clients.remove(client)

# Import datetime and ensure it is available
from datetime import datetime