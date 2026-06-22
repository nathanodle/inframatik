"""WebSocket endpoint for real-time progress and events.

Provides authenticated WebSocket connections for streaming progress updates
during long-running operations (CF setup, cloudflared install, etc.).
Designed for future expansion to replace polling for metrics and status.
"""

import logging
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from auth import validate_session

logger = logging.getLogger("inframatik.ws")

ws_router = APIRouter()

# Active authenticated connections
_connections: set[WebSocket] = set()
_loop: asyncio.AbstractEventLoop | None = None


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
    for ws in dead:
        _connections.discard(ws)


def publish(message: dict):
    """Best-effort broadcast from async code or worker threads."""
    if not _connections:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = _loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(broadcast(message), loop)
        return
    loop.create_task(broadcast(message))


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

    Auth via session cookie (auto-sent by browser).
    """
    # Authenticate before accepting
    token = websocket.cookies.get("inframatik_session")

    if not token or not validate_session(token):
        await websocket.close(code=4001, reason="Authentication required")
        return

    global _loop
    _loop = asyncio.get_running_loop()
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
