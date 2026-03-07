import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
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
from node_config import get_node_config
from cluster_routes import cluster_router
from cf_routes import cf_router
from nodes import stale_checker_loop, heartbeat_sender_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_node_config()
    tasks = []
    if config:
        if config.get("role") == "master":
            tasks.append(asyncio.create_task(stale_checker_loop()))
        elif config.get("role") == "worker":
            tasks.append(asyncio.create_task(heartbeat_sender_loop()))
    yield
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="inframatik", lifespan=lifespan)
app.include_router(cluster_router)
app.include_router(cf_router)

STATIC_DIR = Path(__file__).parent / "static"

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
}


# Paths that service tokens are allowed to access
_SERVICE_TOKEN_PATHS = {"/api/services", "/api/ports/next"}
_SERVICE_TOKEN_PREFIXES = ("/api/services/",)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if path in _SELF_AUTH_PATHS:
        return await call_next(request)

    # Require auth for everything else
    request.state.service_scope = None
    from auth import check_auth
    if not await check_auth(request):
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    # If using a scoped service token, enforce path restrictions
    scope = getattr(request.state, "service_scope", None)
    if scope:
        allowed = path in _SERVICE_TOKEN_PATHS or any(path.startswith(p) for p in _SERVICE_TOKEN_PREFIXES)
        if not allowed:
            return JSONResponse(status_code=403, content={"detail": "Service token cannot access this endpoint"})
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
    lan: bool = False


# --- Dashboard ---

@app.get("/")
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


# --- System ---

@app.get("/api/system")
async def api_system():
    return get_system_metrics()


# --- Tunnel ---

@app.get("/api/tunnel")
async def api_tunnel():
    status = await get_tunnel_status()
    status["routes"] = await get_tunnel_routes()
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
