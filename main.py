import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from system import get_system_metrics
from services import (
    list_services,
    register_service,
    deregister_service,
    start_service,
    stop_service,
    restart_service,
    get_service_logs,
    next_available_port,
)
from tunnel import get_tunnel_status, get_tunnel_routes
from node_config import get_node_config, service_token_capability_allows
from updater import get_version
from cluster_routes import cluster_router
from cf_routes import cf_router
from mcp_routes import mcp_router
from ws_routes import ws_router
from nodes import stale_checker_loop, heartbeat_sender_loop
from node_snapshots import snapshot_collector_loop

logger = logging.getLogger("inframatik.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_node_config()
    tasks = []
    if config:
        if config.get("role") == "master":
            tasks.append(asyncio.create_task(stale_checker_loop()))
            tasks.append(asyncio.create_task(snapshot_collector_loop()))
        elif config.get("role") == "worker":
            tasks.append(asyncio.create_task(heartbeat_sender_loop()))
    yield
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.debug("Background task cancelled during shutdown: %s", task.get_name())
            continue


app = FastAPI(title="inframatik", lifespan=lifespan)
app.include_router(cluster_router)
app.include_router(cf_router)
app.include_router(mcp_router)
app.include_router(ws_router)

STATIC_DIR = Path(__file__).parent / "static"

# Cache buster: combine git commit hash with static asset mtimes so local edits
# invalidate browser caches even before they are committed.
def _get_asset_version() -> str:
    latest_mtime = "0"
    try:
        asset_paths = [
            STATIC_DIR / "app.js",
            STATIC_DIR / "style.css",
            STATIC_DIR / "index.html",
        ]
        latest_mtime = str(int(max(path.stat().st_mtime for path in asset_paths if path.exists())))
    except Exception:
        pass
    try:
        version = get_version()
        parts = [
            str(version.get("commit") or "unknown"),
            str(version.get("deployed_at") or ""),
            latest_mtime,
        ]
        return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]
    except Exception:
        pass
    return latest_mtime

_ASSET_VERSION = _get_asset_version()

# Paths that don't require session auth
_PUBLIC_PATHS = {
    "/", "/api/node/info", "/api/node/health", "/api/node/version",
    "/api/auth/login", "/api/auth/set-password", "/api/auth/status",
    "/api/install.sh", "/api/install/package",
}
# Paths with their own auth checks (X-Api-Key, enrollment tokens)
_SELF_AUTH_PATHS = {
    "/api/nodes/register", "/api/nodes/heartbeat", "/api/node/update",
    "/api/cf/token", "/api/config/reset", "/api/nodes/enroll",
    "/api/nodes/tunnel",
    "/api/internal/cf/service/status",
    "/api/internal/cf/service/logs",
    "/api/internal/cf/service/restart",
    "/api/internal/cf/service/update",
}


def _service_token_required_capability(path: str, method: str) -> Optional[str]:
    if path == "/mcp" and method == "POST":
        return "read"
    if path == "/api/ports/next" and method == "GET":
        return "deploy"
    if path == "/api/services":
        if method == "GET":
            return "read"
        if method == "POST":
            return "deploy"
        return None
    if path.startswith("/api/services/"):
        if method == "GET" and path.endswith("/logs"):
            return "read"
        if method == "POST" and (
            path.endswith("/start")
            or path.endswith("/stop")
            or path.endswith("/restart")
        ):
            return "operate"
        if method == "DELETE":
            return "deploy"
        return None
    return None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if path in _SELF_AUTH_PATHS:
        return await call_next(request)

    # Require auth for everything else
    request.state.service_scope = None
    request.state.service_capability = None
    from auth import check_auth
    if not await check_auth(request):
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    # If using a scoped service token, enforce path restrictions
    scope = getattr(request.state, "service_scope", None)
    if scope:
        required_capability = _service_token_required_capability(path, request.method)
        if not required_capability:
            return JSONResponse(status_code=403, content={"detail": "Service token cannot access this endpoint"})
        token_capability = getattr(request.state, "service_capability", "deploy")
        if not service_token_capability_allows(token_capability, required_capability):
            return JSONResponse(
                status_code=403,
                content={"detail": f"Service token capability '{token_capability}' cannot perform this action"},
            )
        # Verify the service name in the path matches the scope
        if path.startswith("/api/services/"):
            path_service = path.split("/api/services/")[1].split("/")[0]
            if path_service != scope:
                return JSONResponse(status_code=403, content={"detail": f"Token is scoped to service '{scope}'"})

    return await call_next(request)


class ServiceCreate(BaseModel):
    name: str
    command: str
    working_dir: str
    hostname: Optional[str] = None
    access_policy_id: Optional[str] = None
    lan: bool = False


# --- Dashboard ---

@app.get("/")
async def dashboard():
    html = (STATIC_DIR / "index.html").read_text()
    html = html.replace("__ASSET_VERSION__", _ASSET_VERSION)
    return HTMLResponse(html)


# --- System ---

@app.get("/api/system")
async def api_system():
    return get_system_metrics()


# --- Tunnel ---

@app.get("/api/tunnel")
async def api_tunnel(include_routes: bool = False):
    status = await get_tunnel_status()
    if include_routes:
        try:
            status["routes"] = await get_tunnel_routes()
        except ValueError as e:
            status["routes"] = []
            status["routes_error"] = str(e)
    return status


# --- Ports ---

@app.get("/api/ports/next")
async def api_next_port():
    port = next_available_port()
    if port is None:
        raise HTTPException(status_code=503, detail="No ports available")
    return {"port": port}


# --- Services ---

@app.get("/api/services")
async def api_list_services(request: Request):
    services = await list_services()
    scope = getattr(request.state, "service_scope", None)
    if scope:
        services = [s for s in services if s.get("name") == scope]
    return services


@app.post("/api/services", status_code=201)
async def api_register_service(body: ServiceCreate, request: Request):
    scope = getattr(request.state, "service_scope", None)
    if scope and body.name != scope:
        raise HTTPException(status_code=403, detail=f"Token is scoped to service '{scope}'")
    try:
        svc = await register_service(
            name=body.name,
            command=body.command,
            working_dir=body.working_dir,
            hostname=body.hostname,
            access_policy_id=body.access_policy_id,
            lan=body.lan,
        )
        return svc
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/services/{name}")
async def api_deregister_service(name: str):
    try:
        svc = await deregister_service(name)
        return {"deleted": name, **svc}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/services/{name}/start")
async def api_start_service(name: str):
    try:
        status = await start_service(name)
        return {"name": name, "status": status}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{name}/stop")
async def api_stop_service(name: str):
    try:
        status = await stop_service(name)
        return {"name": name, "status": status}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{name}/restart")
async def api_restart_service(name: str):
    try:
        status = await restart_service(name)
        return {"name": name, "status": status}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/services/{name}/logs")
async def api_service_logs(name: str, lines: int = 100):
    try:
        logs = await get_service_logs(name, lines=lines)
        return {"name": name, "logs": logs}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# Mount static files last so API routes take priority
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
