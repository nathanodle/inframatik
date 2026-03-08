"""WebSocket endpoint for real-time progress and events.

Provides authenticated WebSocket connections for streaming progress updates
during long-running operations (CF setup, cloudflared install, etc.).
Designed for future expansion to replace polling for metrics and status.
"""

import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from auth import validate_session

logger = logging.getLogger("inframatik.ws")

ws_router = APIRouter()

# Active authenticated connections
_connections: set[WebSocket] = set()


async def broadcast(message: dict):
    """Send a message to all connected WebSocket clients."""
    if not _connections:
        return
    dead = set()
    for ws in _connections:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    _connections -= dead


async def send_progress(task: str, step: str, message: str, done: bool = False, error: bool = False):
    """Send a progress update to all connected clients.

    Args:
        task: Identifier for the operation (e.g. "dashboard-access")
        step: Current step name (e.g. "downloading_cloudflared")
        message: Human-readable status message
        done: True if this is the final message for the task
        error: True if the task failed
    """
    await broadcast({
        "type": "progress",
        "task": task,
        "step": step,
        "message": message,
        "done": done,
        "error": error,
    })


@ws_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Authenticated WebSocket endpoint.

    Auth via session cookie (auto-sent by browser) or ?token= query param.
    """
    # Authenticate before accepting
    session_cookie = websocket.cookies.get("inframatik_session")
    query_token = websocket.query_params.get("token")
    token = session_cookie or query_token

    if not token or not validate_session(token):
        await websocket.close(code=4001, reason="Authentication required")
        return

    await websocket.accept()
    _connections.add(websocket)
    logger.debug("WebSocket connected (%d total)", len(_connections))

    try:
        while True:
            # Keep connection alive, handle any client messages
            data = await websocket.receive_text()
            # Future: handle subscribe/unsubscribe messages
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _connections.discard(websocket)
        logger.debug("WebSocket disconnected (%d remaining)", len(_connections))
