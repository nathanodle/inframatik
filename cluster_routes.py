import asyncio
import ipaddress
import logging
import os
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
    create_service_token,
    rotate_service_token,
    revoke_service_token,
    revoke_service_token_by_id,
    service_token_id,
    normalize_service_token_capability,
    create_enrollment_token,
    consume_enrollment_token,
    delete_enrollment_token,
    cleanup_expired_tokens,
    assert_worker_address_allowed,
    get_worker_target_allowlist,
    set_worker_target_allowlist,
    is_worker_allowlist_required,
)
from nodes import (
    register_node,
    heartbeat_node,
    validate_heartbeat_key,
    get_all_nodes,
    unregister_node,
)
from proxy import proxy_to_node
from updater import (
    get_version,
    build_package,
    apply_package,
    restart_service,
    push_update_to_worker,
    sign_package,
    verify_package_signature,
    get_signing_public_key_b64,
    get_signing_public_key_pem,
)
from cloudflared import setup_cloudflared_user_service

cluster_router = APIRouter()
logger = logging.getLogger("inframatik.cluster")
_NODE_ROLES = {"standalone", "master", "worker"}


def _configured_role(config: Optional[dict]) -> Optional[str]:
    if not config:
        return None
    role = config.get("role")
    if role in _NODE_ROLES:
        return role
    return None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class LoginBody(BaseModel):
    password: str


class SetPasswordBody(BaseModel):
    password: str


def _is_loopback_addr(addr: str) -> bool:
    if not addr:
        return False
    if addr == "localhost":
        return True
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _is_local_bootstrap_request(request: Request) -> bool:
    if os.getenv("INFRAMATIK_ALLOW_REMOTE_BOOTSTRAP", "").lower() in ("1", "true", "yes"):
        return True

    client_host = request.client.host if request.client else ""
    if not _is_loopback_addr(client_host):
        return False

    # If behind a local reverse proxy, ensure request wasn't forwarded from remote.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first_hop = xff.split(",", 1)[0].strip()
        if first_hop and not _is_loopback_addr(first_hop):
            return False

    return True


def _request_client_id(request: Request) -> str:
    """Best-effort client identifier for login throttling."""
    client_host = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for", "")
    if xff and _is_loopback_addr(client_host):
        first_hop = xff.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    return client_host or "unknown"


@cluster_router.get("/api/auth/status")
async def auth_status():
    from node_config import has_admin_password
    return {"has_password": has_admin_password()}


@cluster_router.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request):
    from node_config import verify_admin_password
    from auth import (
        create_session,
        login_is_allowed,
        record_failed_login,
        record_successful_login,
        SESSION_COOKIE_NAME,
        SESSION_COOKIE_SECURE,
    )

    client_id = _request_client_id(request)
    allowed, retry_after = login_is_allowed(client_id)
    if not allowed:
        raise HTTPException(429, f"Too many login attempts. Retry in {retry_after}s")

    if not verify_admin_password(body.password):
        retry_after = record_failed_login(client_id)
        if retry_after > 0:
            raise HTTPException(429, f"Too many failed attempts. Retry in {retry_after}s")
        raise HTTPException(401, "Invalid password")

    record_successful_login(client_id)
    config = get_node_config()
    duration = config.get("session_duration_hours", 24) if config else 24
    token, expires_at = create_session(duration)
    response = JSONResponse({"token": token, "expires_at": expires_at})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=duration * 3600,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@cluster_router.post("/api/auth/logout")
async def auth_logout(request: Request):
    from auth import invalidate_session, SESSION_COOKIE_NAME, SESSION_COOKIE_SECURE
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        invalidate_session(auth_header[7:])
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if cookie_token:
        invalidate_session(cookie_token)

    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
    )
    return response


@cluster_router.post("/api/auth/set-password")
async def auth_set_password(body: SetPasswordBody, request: Request):
    from node_config import has_admin_password, set_admin_password
    if has_admin_password():
        raise HTTPException(400, "Password already set. Use settings to change it.")
    if not _is_local_bootstrap_request(request):
        raise HTTPException(403, "First password setup must be done from localhost")
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
    role = _configured_role(config)
    if not role:
        return {"role": "unconfigured", "node_name": None, "node_id": None}
    return {
        "role": role,
        "node_name": config.get("node_name"),
        "node_id": config.get("node_id"),
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
    if _configured_role(config) is not None:
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
    role = _configured_role(config)
    if role not in ("standalone", None):
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
    update_public_key: Optional[str] = None


@cluster_router.post("/api/config/init-worker")
async def config_init_worker(body: InitWorkerBody):
    config = get_node_config()
    role = _configured_role(config)
    if role not in ("standalone", None):
        raise HTTPException(400, "Node already configured. Reset first.")
    new_config = init_as_worker(
        body.name,
        body.master_url,
        api_key=body.api_key,
        update_public_key=body.update_public_key,
    )
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

    from auth import clear_all_sessions
    reset_config()
    clear_all_sessions()
    return {"status": "reset", "role": "unconfigured"}


@cluster_router.get("/api/config")
async def config_get():
    cleanup_expired_tokens()
    config = get_node_config()
    role = _configured_role(config)
    if not role:
        return {"role": "unconfigured"}
    result = {
        "role": role,
        "node_id": config.get("node_id"),
        "node_name": config.get("node_name"),
    }
    result["tunnel_id"] = config.get("tunnel_id")
    result["dashboard_hostname"] = config.get("dashboard_hostname")
    result["cf_configured"] = bool(config.get("cf_token") and config.get("cf_account_id"))
    if role == "worker":
        result["api_key"] = config.get("api_key")
        result["master_url"] = config.get("master_url")
    elif role == "master":
        result["api_key"] = config.get("api_key")
        # Strip api_keys from worker entries (only expose name/address/tunnel_id)
        workers = {}
        for wid, w in config.get("workers", {}).items():
            workers[wid] = {k: v for k, v in w.items() if k != "api_key"}
        result["workers"] = workers
        result["worker_target_allowlist"] = get_worker_target_allowlist(config=config)
        result["worker_target_allowlist_required"] = is_worker_allowlist_required()
        result["enrollment_tokens"] = [
            {
                "token": token,
                "created_at": meta.get("created_at"),
                "expires_at": meta.get("expires_at"),
            }
            for token, meta in config.get("enrollment_tokens", {}).items()
        ]
    # Include service tokens summary (no token values)
    svc_tokens = config.get("service_tokens", {})
    result["service_tokens"] = [
        {
            "token_id": service_token_id(token),
            "service": v["service"],
            "capability": v.get("capability", "deploy"),
            "created_at": v.get("created_at"),
            "expires_at": v.get("expires_at"),
        }
        for token, v in svc_tokens.items()
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
            await setup_cloudflared_user_service(token)

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
    except RuntimeError as e:
        raise HTTPException(500, str(e))


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

    warnings: list[str] = []
    try:
        await remove_tunnel_route(hostname)
    except Exception as e:
        warnings.append(f"Failed to remove tunnel route: {e}")
        logger.warning("Dashboard access cleanup failed (route %s): %s", hostname, e)
    try:
        await delete_dns_record(hostname)
    except Exception as e:
        warnings.append(f"Failed to delete DNS record: {e}")
        logger.warning("Dashboard access cleanup failed (dns %s): %s", hostname, e)
    try:
        await delete_access_app(hostname)
    except Exception as e:
        warnings.append(f"Failed to delete Access app: {e}")
        logger.warning("Dashboard access cleanup failed (access %s): %s", hostname, e)

    set_dashboard_hostname(None)
    response = {"status": "disabled"}
    if warnings:
        response["warnings"] = warnings
    return response


# --- Worker management (master only) ---

class AddWorkerBody(BaseModel):
    name: str
    address: str
    api_key: str


class WorkerTargetAllowlistBody(BaseModel):
    entries: list[str]


@cluster_router.get("/api/config/worker-target-allowlist")
async def config_get_worker_target_allowlist():
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(400, "Only a master node can manage worker target allowlist")
    try:
        entries = get_worker_target_allowlist(config=config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "entries": entries,
        "required": is_worker_allowlist_required(),
    }


@cluster_router.put("/api/config/worker-target-allowlist")
async def config_set_worker_target_allowlist(body: WorkerTargetAllowlistBody):
    try:
        entries = set_worker_target_allowlist(body.entries)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "entries": entries,
        "required": is_worker_allowlist_required(),
    }


@cluster_router.delete("/api/config/worker-target-allowlist")
async def config_clear_worker_target_allowlist():
    try:
        entries = set_worker_target_allowlist([])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "entries": entries,
        "required": is_worker_allowlist_required(),
    }


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
    config = get_node_config() or {}
    meta = config.get("enrollment_tokens", {}).get(token, {})
    return {
        "token": token,
        "created_at": meta.get("created_at"),
        "expires_at": meta.get("expires_at"),
    }


@cluster_router.delete("/api/config/enrollment-tokens/{token}")
async def config_delete_enrollment_token(token: str):
    delete_enrollment_token(token)
    return {"status": "deleted"}


# --- Service tokens ---

class ServiceTokenBody(BaseModel):
    service: str
    capability: Optional[str] = None


@cluster_router.post("/api/config/service-tokens")
async def config_create_service_token(body: ServiceTokenBody):
    try:
        token = create_service_token(body.service, capability=body.capability)
    except ValueError as e:
        raise HTTPException(400, str(e))
    config = get_node_config() or {}
    meta = config.get("service_tokens", {}).get(token, {})
    capability = meta.get("capability")
    try:
        capability = normalize_service_token_capability(capability)
    except ValueError:
        capability = "deploy"
    return {
        "token": token,
        "token_id": service_token_id(token),
        "service": body.service,
        "capability": capability,
        "created_at": meta.get("created_at"),
        "expires_at": meta.get("expires_at"),
    }


@cluster_router.post("/api/config/service-tokens/{token}/rotate")
async def config_rotate_service_token(token: str):
    try:
        new_token, service, capability = rotate_service_token(token)
    except ValueError as e:
        raise HTTPException(404, str(e))
    config = get_node_config() or {}
    meta = config.get("service_tokens", {}).get(new_token, {})
    return {
        "token": new_token,
        "token_id": service_token_id(new_token),
        "service": service,
        "capability": capability,
        "created_at": meta.get("created_at"),
        "expires_at": meta.get("expires_at"),
    }


@cluster_router.delete("/api/config/service-tokens/by-id/{token_id}")
async def config_revoke_service_token_by_id(token_id: str):
    if not revoke_service_token_by_id(token_id):
        raise HTTPException(404, "Service token not found")
    return {"status": "revoked"}


@cluster_router.delete("/api/config/service-tokens/{token}")
async def config_revoke_service_token(token: str):
    revoke_service_token(token)
    return {"status": "revoked"}


class EnrollBody(BaseModel):
    token: str
    node_name: str
    address: str


@cluster_router.post("/api/nodes/enroll")
async def enroll_worker(body: EnrollBody):
    """Worker presents an enrollment token to register with the master."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(400, "Not a master node")

    try:
        normalized_address = assert_worker_address_allowed(body.address, config=config)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not consume_enrollment_token(body.token):
        raise HTTPException(401, "Invalid or expired enrollment token")

    # Generate credentials for the worker
    worker_api_key = generate_api_key()
    signing_public_key = get_signing_public_key_pem()

    # Store worker in master config
    try:
        add_worker(body.node_name, normalized_address, worker_api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "status": "enrolled",
        "api_key": worker_api_key,
        "signing_public_key": signing_public_key,
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
    if not validate_heartbeat_key(body.node_id, api_key):
        raise HTTPException(401, "Invalid API key for node")
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


@cluster_router.get("/api/nodes/{node_id}/cf/service/status")
async def proxy_cf_service_status(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/internal/cf/service/status")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/cf/service/logs")
async def proxy_cf_service_logs(node_id: str, lines: int = 80):
    try:
        return await proxy_to_node(
            node_id,
            "GET",
            f"/api/internal/cf/service/logs?lines={lines}",
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/cf/service/restart")
async def proxy_cf_service_restart(node_id: str):
    try:
        return await proxy_to_node(node_id, "POST", "/api/internal/cf/service/restart")
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
    allow_unsigned = os.getenv("INFRAMATIK_ALLOW_UNSIGNED_UPDATES", "").lower() in ("1", "true", "yes")
    package_sig = request.headers.get("X-Inframatik-Package-Signature", "").strip()
    trusted_public_key = config.get("update_public_key")
    if not trusted_public_key and config.get("role") in ("master", "standalone"):
        trusted_public_key = get_signing_public_key_pem()

    if not package_sig:
        if not allow_unsigned:
            raise HTTPException(401, "Missing package signature")
    else:
        if not trusted_public_key:
            raise HTTPException(400, "No trusted update signing key configured")
        if not verify_package_signature(body, package_sig, trusted_public_key):
            raise HTTPException(401, "Invalid package signature")

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
    package_sig = sign_package(package)
    workers = config.get("workers", {})
    results = {}

    worker_targets = []
    coroutines = []
    for node_id, worker in workers.items():
        worker_targets.append((node_id, worker["name"]))
        coroutines.append(
            push_update_to_worker(
                worker["address"],
                worker["api_key"],
                package,
                package_sig["signature_b64"],
                package_sig["key_id"],
            )
        )

    if coroutines:
        responses = await asyncio.gather(*coroutines, return_exceptions=True)
        for (node_id, name), response in zip(worker_targets, responses):
            if isinstance(response, Exception):
                results[node_id] = {
                    "name": name,
                    "status": "error",
                    "detail": str(response),
                }
            elif isinstance(response, dict):
                results[node_id] = {"name": name, **response}
            else:
                results[node_id] = {
                    "name": name,
                    "status": "error",
                    "detail": "Invalid worker response type",
                }

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
    package_public_key_b64 = get_signing_public_key_b64()

    script = (
        _INSTALL_SCRIPT_PATH.read_text()
        .replace("__MASTER_URL__", master_url)
        .replace("__PACKAGE_PUBLIC_KEY_B64__", package_public_key_b64)
    )
    return PlainTextResponse(script, media_type="text/plain")


@cluster_router.get("/api/install/package")
async def install_package():
    """Serve the code package for new installs."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only master can serve install packages")

    package = build_package()
    package_sig = sign_package(package)
    return Response(
        content=package,
        media_type="application/gzip",
        headers={
            "X-Inframatik-Package-Signature": package_sig["signature_b64"],
            "X-Inframatik-Package-Key-Id": package_sig["key_id"],
            "X-Inframatik-Package-Signed-At": str(package_sig["signed_at"]),
        },
    )
