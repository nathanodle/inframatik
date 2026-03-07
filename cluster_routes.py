import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from node_config import (
    get_node_config,
    init_as_standalone,
    init_as_master,
    init_as_worker,
    reset_config,
    add_worker,
    remove_worker,
    set_tunnel_id,
    set_dashboard_hostname,
    generate_api_key,
    generate_node_id,
    create_service_token,
    revoke_service_token,
    create_enrollment_token,
    consume_enrollment_token,
    delete_enrollment_token,
)
from nodes import register_node, heartbeat_node, get_all_nodes, unregister_node
from proxy import proxy_to_node
from updater import get_version, build_package, apply_package, restart_service, push_update_to_worker

cluster_router = APIRouter()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class LoginBody(BaseModel):
    password: str


class SetPasswordBody(BaseModel):
    password: str


@cluster_router.get("/api/auth/status")
async def auth_status():
    from node_config import has_admin_password
    return {"has_password": has_admin_password()}


@cluster_router.post("/api/auth/login")
async def auth_login(body: LoginBody):
    from node_config import verify_admin_password
    from auth import create_session
    if not verify_admin_password(body.password):
        raise HTTPException(401, "Invalid password")
    config = get_node_config()
    duration = config.get("session_duration_hours", 24) if config else 24
    token, expires_at = create_session(duration)
    return {"token": token, "expires_at": expires_at}


@cluster_router.post("/api/auth/logout")
async def auth_logout(request: Request):
    from auth import invalidate_session
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        invalidate_session(auth_header[7:])
    return {"status": "logged_out"}


@cluster_router.post("/api/auth/set-password")
async def auth_set_password(body: SetPasswordBody):
    from node_config import has_admin_password, set_admin_password
    if has_admin_password():
        raise HTTPException(400, "Password already set. Use settings to change it.")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    set_admin_password(body.password)
    return {"status": "password_set"}


# ---------------------------------------------------------------------------
# Node identity (all nodes)
# ---------------------------------------------------------------------------

@cluster_router.get("/api/node/info")
async def node_info():
    config = get_node_config()
    if not config:
        return {"role": "unconfigured", "node_name": None, "node_id": None}
    return {
        "role": config["role"],
        "node_name": config["node_name"],
        "node_id": config["node_id"],
    }


@cluster_router.get("/api/node/health")
async def node_health():
    from system import get_system_metrics
    metrics = get_system_metrics()
    return {"status": "ok", "uptime_seconds": metrics["uptime_seconds"]}


# ---------------------------------------------------------------------------
# Setup / config (used by the Settings UI)
# ---------------------------------------------------------------------------

class InitBody(BaseModel):
    name: str


@cluster_router.post("/api/config/init-standalone")
async def config_init_standalone(body: InitBody):
    config = get_node_config()
    if config is not None:
        raise HTTPException(400, "Node already configured. Reset first.")
    new_config = init_as_standalone(body.name)
    return {
        "node_id": new_config["node_id"],
        "node_name": new_config["node_name"],
        "role": "standalone",
    }


@cluster_router.post("/api/config/init-master")
async def config_init_master(body: InitBody):
    config = get_node_config()
    if config is not None and config.get("role") not in ("standalone", None):
        raise HTTPException(400, "Node already configured. Reset first.")
    new_config = init_as_master(body.name)
    return {
        "node_id": new_config["node_id"],
        "node_name": new_config["node_name"],
        "role": "master",
    }


class InitWorkerBody(BaseModel):
    name: str
    master_url: str
    api_key: Optional[str] = None


@cluster_router.post("/api/config/init-worker")
async def config_init_worker(body: InitWorkerBody):
    config = get_node_config()
    if config is not None and config.get("role") not in ("standalone", None):
        raise HTTPException(400, "Node already configured. Reset first.")
    new_config = init_as_worker(body.name, body.master_url, api_key=body.api_key)
    return {
        "node_id": new_config["node_id"],
        "node_name": new_config["node_name"],
        "role": "worker",
        "api_key": new_config["api_key"],
        "master_url": new_config["master_url"],
    }


@cluster_router.post("/api/config/reset")
async def config_reset(request: Request):
    config = get_node_config()
    if config and config.get("api_key"):
        api_key = request.headers.get("X-Api-Key")
        if not api_key or api_key != config["api_key"]:
            raise HTTPException(401, "API key required")
    reset_config()
    return {"status": "reset", "role": "unconfigured"}


@cluster_router.get("/api/config")
async def config_get():
    config = get_node_config()
    if not config:
        return {"role": "unconfigured"}
    result = {
        "role": config["role"],
        "node_id": config["node_id"],
        "node_name": config["node_name"],
    }
    result["tunnel_id"] = config.get("tunnel_id")
    result["dashboard_hostname"] = config.get("dashboard_hostname")
    result["cf_configured"] = bool(config.get("cf_token") and config.get("cf_account_id"))
    if config["role"] == "worker":
        result["api_key"] = config["api_key"]
        result["master_url"] = config["master_url"]
    elif config["role"] == "master":
        result["api_key"] = config["api_key"]
        # Strip api_keys from worker entries (only expose name/address/tunnel_id)
        workers = {}
        for wid, w in config.get("workers", {}).items():
            workers[wid] = {k: v for k, v in w.items() if k != "api_key"}
        result["workers"] = workers
        result["enrollment_tokens"] = list(config.get("enrollment_tokens", {}).keys())
    # Include service tokens summary (no token values)
    svc_tokens = config.get("service_tokens", {})
    result["service_tokens"] = [
        {"service": v["service"], "created_at": v.get("created_at")}
        for v in svc_tokens.values()
    ]
    return result


# --- Dashboard CF Access ---

class DashboardAccessBody(BaseModel):
    hostname: str


@cluster_router.post("/api/config/dashboard-access")
async def config_enable_dashboard_access(body: DashboardAccessBody):
    """Put the dashboard behind Cloudflare Access."""
    from tunnel import (
        _load_cf_config, create_tunnel, get_tunnel_token,
        init_tunnel_config, add_tunnel_route, create_dns_record,
        create_access_app,
    )
    config = get_node_config()
    if not config:
        raise HTTPException(400, "Node not configured")

    cf_cfg = _load_cf_config()
    if not cf_cfg:
        raise HTTPException(400, "Cloudflare not configured. Set up in Settings → Cloudflare.")

    try:
        # Create tunnel if this node doesn't have one yet
        tid = config.get("tunnel_id")
        if not tid:
            tunnel_result = await create_tunnel(config["node_name"])
            tid = tunnel_result["id"]
            await init_tunnel_config(tid)
            set_tunnel_id(tid)

            # Start cloudflared with the tunnel token
            token = await get_tunnel_token(tid)
            proc = await asyncio.create_subprocess_exec(
                "sudo", "/usr/local/bin/infra-cf-setup", token,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                raise ValueError("Failed to start cloudflared")

        # Add ingress route for the dashboard
        await add_tunnel_route(body.hostname, "http://localhost:9000", tunnel_id=tid)

        # Create DNS record
        await create_dns_record(body.hostname, tunnel_id=tid)

        # Create Access app if default policy is configured
        policy_id = cf_cfg.get("default_policy_id")
        if policy_id:
            await create_access_app("inframatik dashboard", body.hostname, policy_id)

        set_dashboard_hostname(body.hostname)

        return {"status": "enabled", "hostname": body.hostname, "tunnel_id": tid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@cluster_router.delete("/api/config/dashboard-access")
async def config_disable_dashboard_access():
    """Remove the dashboard from Cloudflare Access."""
    from tunnel import remove_tunnel_route, delete_dns_record, delete_access_app

    config = get_node_config()
    if not config:
        raise HTTPException(400, "Node not configured")

    hostname = config.get("dashboard_hostname")
    if not hostname:
        raise HTTPException(404, "Dashboard access not configured")

    try:
        await remove_tunnel_route(hostname)
    except Exception:
        pass
    try:
        await delete_dns_record(hostname)
    except Exception:
        pass
    try:
        await delete_access_app(hostname)
    except Exception:
        pass

    set_dashboard_hostname(None)
    return {"status": "disabled"}


# --- Worker management (master only) ---

class AddWorkerBody(BaseModel):
    name: str
    address: str
    api_key: str


@cluster_router.post("/api/config/workers")
async def config_add_worker(body: AddWorkerBody):
    try:
        node_id = add_worker(body.name, body.address, body.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"node_id": node_id, "status": "added"}


@cluster_router.delete("/api/config/workers/{node_id}")
async def config_remove_worker(node_id: str):
    try:
        remove_worker(node_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    unregister_node(node_id)
    return {"status": "removed"}


# --- Enrollment tokens ---

@cluster_router.post("/api/config/enrollment-tokens")
async def config_create_enrollment_token():
    try:
        token = create_enrollment_token()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"token": token}


@cluster_router.delete("/api/config/enrollment-tokens/{token}")
async def config_delete_enrollment_token(token: str):
    delete_enrollment_token(token)
    return {"status": "deleted"}


# --- Service tokens ---

class ServiceTokenBody(BaseModel):
    service: str


@cluster_router.post("/api/config/service-tokens")
async def config_create_service_token(body: ServiceTokenBody):
    try:
        token = create_service_token(body.service)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"token": token, "service": body.service}


@cluster_router.delete("/api/config/service-tokens/{token}")
async def config_revoke_service_token(token: str):
    revoke_service_token(token)
    return {"status": "revoked"}


class EnrollBody(BaseModel):
    token: str
    node_name: str
    address: str


@cluster_router.post("/api/nodes/enroll")
async def enroll_worker(body: EnrollBody, request: Request):
    """Worker presents an enrollment token to register with the master."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(400, "Not a master node")

    if not consume_enrollment_token(body.token):
        raise HTTPException(401, "Invalid or expired enrollment token")

    # Generate credentials for the worker
    worker_api_key = generate_api_key()

    # Store worker in master config
    try:
        add_worker(body.node_name, body.address, worker_api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "status": "enrolled",
        "api_key": worker_api_key,
    }


# ---------------------------------------------------------------------------
# Registration / heartbeat (master receives from workers)
# ---------------------------------------------------------------------------

class RegisterBody(BaseModel):
    node_id: str
    node_name: str
    address: str


@cluster_router.post("/api/nodes/register")
async def register(body: RegisterBody, request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        raise HTTPException(401, "API key required")
    ok = register_node(body.node_id, body.node_name, body.address, api_key)
    if not ok:
        raise HTTPException(401, "Unknown API key")
    return {"status": "registered"}


class HeartbeatBody(BaseModel):
    node_id: str


@cluster_router.post("/api/nodes/heartbeat")
async def heartbeat(body: HeartbeatBody, request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        raise HTTPException(401, "API key required")
    ok = heartbeat_node(body.node_id)
    if not ok:
        raise HTTPException(404, "Node not registered — re-register required")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Node list (master)
# ---------------------------------------------------------------------------

@cluster_router.get("/api/nodes")
async def list_nodes():
    return await get_all_nodes()


# ---------------------------------------------------------------------------
# Proxy endpoints (master forwards to workers)
# ---------------------------------------------------------------------------

@cluster_router.get("/api/nodes/{node_id}/system")
async def proxy_system(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/system")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/services")
async def proxy_services(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/services")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/services")
async def proxy_create_service(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", "/api/services", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.delete("/api/nodes/{node_id}/services/{name}")
async def proxy_delete_service(node_id: str, name: str):
    try:
        return await proxy_to_node(node_id, "DELETE", f"/api/services/{name}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/services/{name}/{action}")
async def proxy_service_action(node_id: str, name: str, action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, f"Unknown action: {action}")
    try:
        return await proxy_to_node(node_id, "POST", f"/api/services/{name}/{action}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/services/{name}/logs")
async def proxy_service_logs(node_id: str, name: str, lines: int = 100):
    try:
        return await proxy_to_node(node_id, "GET", f"/api/services/{name}/logs?lines={lines}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/tunnel")
async def proxy_tunnel(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/tunnel")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


# ---------------------------------------------------------------------------
# Updates / deployment
# ---------------------------------------------------------------------------

@cluster_router.get("/api/node/version")
async def node_version():
    return get_version()


@cluster_router.post("/api/node/update")
async def node_update(request: Request):
    """Receive an update package (tar.gz bytes), apply it, and restart."""
    api_key = request.headers.get("X-Api-Key")
    config = get_node_config()
    if not api_key or not config or api_key != config.get("api_key"):
        raise HTTPException(401, "API key required")
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty package")
    if len(body) > 50 * 1024 * 1024:  # 50MB max
        raise HTTPException(413, "Package too large")
    try:
        apply_package(body)
    except Exception as e:
        raise HTTPException(400, f"Failed to apply package: {e}")
    # Restart in background so the response gets sent first
    asyncio.get_event_loop().call_later(1, restart_service)
    return {"status": "updated", "detail": "Restarting..."}


@cluster_router.post("/api/update/deploy")
async def deploy_to_workers():
    """Master-only: package local code and push to all workers."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only master can deploy updates")

    package = build_package()
    workers = config.get("workers", {})
    results = {}

    tasks = []
    for node_id, worker in workers.items():
        tasks.append((node_id, worker["name"], push_update_to_worker(
            worker["address"], worker["api_key"], package
        )))

    for node_id, name, coro in tasks:
        result = await coro
        results[node_id] = {"name": name, **result}

    return {"status": "deployed", "workers": results}


@cluster_router.post("/api/update/deploy-self")
async def deploy_self():
    """Restart this node (useful after local code changes on master)."""
    asyncio.get_event_loop().call_later(1, restart_service)
    return {"status": "restarting"}


# ---------------------------------------------------------------------------
# Install script (master serves to new machines)
# ---------------------------------------------------------------------------

_INSTALL_SCRIPT_PATH = Path(__file__).parent / "install.sh"




@cluster_router.get("/api/install.sh")
async def install_script(request: Request):
    """Serve the install script with the master URL embedded."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only master can serve install scripts")

    # Build master URL from the request (validate host header)
    import re as _re
    host = request.headers.get("host", "localhost:9000")
    if not _re.match(r'^[a-zA-Z0-9._-]+(:\d+)?$', host):
        raise HTTPException(400, "Invalid host header")
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else "http"
    master_url = f"{scheme}://{host}"

    script = _INSTALL_SCRIPT_PATH.read_text().replace("__MASTER_URL__", master_url)
    return PlainTextResponse(script, media_type="text/plain")


@cluster_router.get("/api/install/package")
async def install_package():
    """Serve the code package for new installs."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only master can serve install packages")

    package = build_package()
    return Response(content=package, media_type="application/gzip")
