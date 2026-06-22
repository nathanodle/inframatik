"""WebSocket endpoint for real-time progress and events.

Provides authenticated WebSocket connections for streaming progress updates
during long-running operations (CF setup, cloudflared install, etc.).
Designed for future expansion to replace polling for metrics and status.
"""

import logging
import asyncio
import threading
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import httpx
from auth import validate_session
from node_config import get_node_config

logger = logging.getLogger("inframatik.ws")

ws_router = APIRouter()

# Active authenticated connections
_connections: set[WebSocket] = set()
_loop: asyncio.AbstractEventLoop | None = None
_FORWARDED_EVENT_TYPES = {"inference_operation", "model_job"}


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
    _forward_worker_event(message)
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


def _worker_event_payload(message: dict, config: dict | None = None) -> dict | None:
    event = dict(message or {})
    if event.get("type") not in _FORWARDED_EVENT_TYPES:
        return None
    cfg = config if config is not None else (get_node_config() or {})
    if cfg.get("role") != "worker":
        return None
    if not cfg.get("master_url") or not cfg.get("api_key") or not cfg.get("node_id"):
        return None
    return {
        "master_url": str(cfg["master_url"]).rstrip("/"),
        "api_key": cfg["api_key"],
        "payload": {
            "node_id": cfg["node_id"],
            "event": event,
        },
    }


async def _post_worker_event_async(target: dict):
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.post(
                f"{target['master_url']}/api/nodes/events",
                headers={"X-Api-Key": target["api_key"]},
                json=target["payload"],
            )
    except Exception as e:
        logger.debug("Failed to forward worker event to master: %s", e)


def _post_worker_event_sync(target: dict):
    try:
        with httpx.Client(timeout=3) as client:
            client.post(
                f"{target['master_url']}/api/nodes/events",
                headers={"X-Api-Key": target["api_key"]},
                json=target["payload"],
            )
    except Exception as e:
        logger.debug("Failed to forward worker event to master: %s", e)


def _forward_worker_event(message: dict):
    target = _worker_event_payload(message)
    if not target:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=_post_worker_event_sync, args=(target,), daemon=True).start()
        return
    loop.create_task(_post_worker_event_async(target))


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
