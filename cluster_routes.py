import asyncio
import ipaddress
import logging
import os
import platform
import re
import socket
import tarfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
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
    set_worker_tunnel_id_for_api_key,
    set_dashboard_hostname,
    save_cf_config,
    generate_api_key,
    get_worker_by_api_key,
    create_service_token,
    rotate_service_token,
    revoke_service_token,
    revoke_service_token_by_id,
    service_token_id,
    normalize_service_token_capability,
    create_mcp_token,
    revoke_mcp_token_by_id,
    mcp_token_id,
    normalize_mcp_scopes,
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
from node_snapshots import get_node_snapshot
from proxy import proxy_to_node
from updater import (
    get_version,
    build_package,
    apply_package,
    restart_service,
    update_from_git,
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
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _configured_role(config: Optional[dict]) -> Optional[str]:
    if not config:
        return None
    role = config.get("role")
    if role in _NODE_ROLES:
        return role
    return None


def _normalize_dashboard_subdomain(raw: str) -> str:
    subdomain = (raw or "").strip().lower().strip(".")
    if not subdomain:
        raise HTTPException(400, "Subdomain is required")
    if "." in subdomain:
        raise HTTPException(400, "Subdomain must be a single DNS label")
    if not _DNS_LABEL_RE.fullmatch(subdomain):
        raise HTTPException(400, "Subdomain must be a valid DNS label")
    return subdomain


def _normalize_dashboard_hostname(raw: str) -> str:
    hostname = (raw or "").strip().lower().strip(".")
    if not hostname:
        raise HTTPException(400, "Hostname is required")
    if "/" in hostname or " " in hostname:
        raise HTTPException(400, "Hostname must be a valid domain name")
    if "." not in hostname:
        raise HTTPException(400, "Hostname must include a domain (e.g. dash.example.com)")
    return hostname


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


@cluster_router.get("/api/auth/me")
async def auth_me(request: Request):
    """Return the current user's identity."""
    email = getattr(request.state, "user_email", None)
    return {"email": email, "auth_method": "cf_access" if email else "session"}


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
    machine_hostname = (platform.node() or "").strip()
    if machine_hostname:
        machine_hostname = machine_hostname.split(".", 1)[0]
    config = get_node_config()
    role = _configured_role(config)
    if not role:
        result = {
            "role": "unconfigured",
            "node_name": None,
            "node_id": None,
            "machine_hostname": machine_hostname or None,
        }
        install_source = (config or {}).get("install_source_master_url") if isinstance(config, dict) else None
        if isinstance(install_source, str):
            parsed = urlparse(install_source)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                result["install_source_master_url"] = install_source.rstrip("/")
        return result
    return {
        "role": role,
        "node_name": config.get("node_name"),
        "node_id": config.get("node_id"),
        "machine_hostname": machine_hostname or None,
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
    cf_config: Optional[dict] = None
    skip_cf: bool = False


class EnrollWorkerBody(BaseModel):
    name: str
    master_url: str
    token: str
    skip_cf: bool = False


class WorkerTunnelBody(BaseModel):
    tunnel_id: str


def _normalize_master_url(master_url: str) -> str:
    master_url = (master_url or "").strip().rstrip("/")
    parsed = urlparse(master_url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Master address must use http or https")
    if not parsed.hostname:
        raise HTTPException(400, "Master address must include a host")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise HTTPException(400, "Master address must be a base URL without path/query/fragment")
    return master_url


def _worker_address_for_master(master_url: str, request: Request) -> str:
    parsed = urlparse(master_url)
    master_host = parsed.hostname
    master_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    local_ip = None

    if master_host:
        try:
            family = socket.AF_INET6 if ":" in master_host else socket.AF_INET
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((master_host, master_port))
                local_ip = sock.getsockname()[0]
        except OSError:
            local_ip = None

    if not local_ip:
        local_ip = request.url.hostname
    if not local_ip:
        raise HTTPException(400, "Unable to determine worker address for master")

    worker_port = request.url.port or 9000
    host_part = f"[{local_ip}]" if ":" in local_ip and not local_ip.startswith("[") else local_ip
    return f"http://{host_part}:{worker_port}"


def _master_cf_config_for_enrollment(config: Optional[dict]) -> Optional[dict]:
    """Return the CF config fields a worker needs to manage its own tunnel."""
    if not config:
        return None
    token = (config.get("cf_token") or "").strip()
    account_id = (config.get("cf_account_id") or "").strip()
    if not token or not account_id:
        return None
    payload = {
        "token": token,
        "account_id": account_id,
        "zone_id": config.get("cf_zone_id"),
        "default_policy_id": config.get("cf_default_policy_id"),
        "team_domain": config.get("cf_team_domain"),
        "access_issuer": config.get("cf_access_issuer"),
    }
    return {k: v for k, v in payload.items() if v}


def _save_enrolled_cf_config(cf_config: Optional[dict]) -> bool:
    if not isinstance(cf_config, dict):
        return False
    token = (cf_config.get("token") or "").strip()
    account_id = (cf_config.get("account_id") or "").strip()
    if not token or not account_id:
        return False
    save_cf_config(
        token,
        account_id,
        (cf_config.get("zone_id") or "").strip(),
        cf_config.get("default_policy_id"),
        team_domain=cf_config.get("team_domain"),
        access_issuer=cf_config.get("access_issuer"),
    )
    return True


async def _send_worker_enroll_progress(
    step: str,
    message: str,
    *,
    done: bool = False,
    error: bool = False,
):
    try:
        from ws_routes import send_progress
        await send_progress("worker-enroll", step, message, done=done, error=error)
    except Exception as e:
        logger.debug("Failed to send worker enrollment progress: %s", e)


async def _report_worker_tunnel_to_master(master_url: str, api_key: str, tunnel_id: str):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{master_url}/api/nodes/tunnel",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"tunnel_id": tunnel_id},
        )
        if resp.status_code >= 400:
            try:
                data = resp.json()
                detail = data.get("detail", resp.text) if isinstance(data, dict) else str(data)
            except ValueError:
                detail = resp.text
            raise RuntimeError(f"Master rejected worker tunnel report: {detail}")


async def _setup_enrolled_worker_tunnel(node_name: str, master_url: str, api_key: str) -> dict:
    from tunnel import create_tunnel, get_tunnel_token, init_tunnel_config

    await _send_worker_enroll_progress("creating_tunnel", "Creating worker Cloudflare tunnel...")
    tunnel_result = await create_tunnel(node_name)
    tunnel_id = tunnel_result["id"]
    await _send_worker_enroll_progress("initializing_tunnel", "Initializing worker tunnel routing...")
    await init_tunnel_config(tunnel_id)
    await _send_worker_enroll_progress("getting_token", "Getting tunnel connector token...")
    token = await get_tunnel_token(tunnel_id)
    await _send_worker_enroll_progress("installing_cloudflared", "Installing and starting cloudflared...")
    await setup_cloudflared_user_service(token)
    await _send_worker_enroll_progress("cloudflared_ready", "cloudflared is running")
    set_tunnel_id(tunnel_id)
    result = {
        "tunnel_id": tunnel_id,
        "tunnel_name": tunnel_result.get("name") or node_name,
    }
    try:
        await _send_worker_enroll_progress("reporting_master", "Reporting tunnel to master...")
        await _report_worker_tunnel_to_master(master_url, api_key, tunnel_id)
    except (RuntimeError, httpx.HTTPError, OSError) as e:
        logger.warning("Worker tunnel created but master report failed: %s", e)
        result["master_report_error"] = str(e)
    return result


async def _configure_enrolled_worker_cloudflare(
    cf_config: Optional[dict],
    node_name: str,
    master_url: str,
    api_key: str,
    skip_cf: bool = False,
) -> tuple[Optional[dict], Optional[str]]:
    if skip_cf:
        await _send_worker_enroll_progress(
            "skipping_cloudflare",
            "Local-only worker selected; skipping Cloudflare tunnel setup.",
        )
        return None, None
    if not _save_enrolled_cf_config(cf_config):
        await _send_worker_enroll_progress(
            "skipping_cloudflare",
            "Master has no Cloudflare config; skipping worker tunnel setup.",
        )
        return None, None
    try:
        await _send_worker_enroll_progress("saving_cloudflare_config", "Saving Cloudflare configuration...")
        return await _setup_enrolled_worker_tunnel(node_name, master_url, api_key), None
    except (ValueError, RuntimeError, httpx.HTTPError, OSError) as e:
        logger.warning("Worker Cloudflare tunnel setup failed during enrollment: %s", e)
        await _send_worker_enroll_progress(
            "cloudflare_error",
            f"Cloudflare tunnel setup needs attention: {e}",
            done=True,
            error=True,
        )
        return None, str(e)


@cluster_router.post("/api/config/init-worker")
async def config_init_worker(body: InitWorkerBody):
    config = get_node_config()
    role = _configured_role(config)
    if role not in ("standalone", None):
        raise HTTPException(400, "Node already configured. Reset first.")
    await _send_worker_enroll_progress("saving_worker_config", "Saving worker configuration...")
    new_config = init_as_worker(
        body.name,
        body.master_url,
        api_key=body.api_key,
        update_public_key=body.update_public_key,
    )
    cf_tunnel, cf_tunnel_error = await _configure_enrolled_worker_cloudflare(
        body.cf_config,
        new_config["node_name"],
        new_config["master_url"],
        new_config["api_key"],
        skip_cf=body.skip_cf,
    )
    result = {
        "node_id": new_config["node_id"],
        "node_name": new_config["node_name"],
        "role": "worker",
        "api_key": new_config["api_key"],
        "master_url": new_config["master_url"],
    }
    if cf_tunnel:
        result["cf_tunnel"] = cf_tunnel
    if cf_tunnel_error:
        result["cf_tunnel_error"] = cf_tunnel_error
    if not cf_tunnel_error:
        await _send_worker_enroll_progress("complete", "Worker registration complete", done=True)
    return result


@cluster_router.post("/api/config/enroll-worker")
async def config_enroll_worker(body: EnrollWorkerBody, request: Request):
    config = get_node_config()
    role = _configured_role(config)
    if role not in ("standalone", None):
        raise HTTPException(400, "Node already configured. Reset first.")

    master_url = _normalize_master_url(body.master_url)
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(400, "Enrollment token is required")

    worker_address = _worker_address_for_master(master_url, request)
    await _send_worker_enroll_progress("contacting_master", "Contacting master...")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{master_url}/api/nodes/enroll",
                json={
                    "token": token,
                    "node_name": body.name,
                    "address": worker_address,
                    "skip_cf": body.skip_cf,
                },
            )
            try:
                data = resp.json()
            except ValueError:
                data = {}
    except httpx.HTTPError as e:
        await _send_worker_enroll_progress(
            "enrollment_error",
            f"Failed to reach master: {e}",
            done=True,
            error=True,
        )
        raise HTTPException(400, f"Failed to reach master: {e}") from e

    if resp.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        await _send_worker_enroll_progress(
            "enrollment_error",
            detail or "Enrollment failed",
            done=True,
            error=True,
        )
        raise HTTPException(resp.status_code, detail or "Enrollment failed")
    if not isinstance(data, dict) or not data.get("api_key"):
        await _send_worker_enroll_progress(
            "enrollment_error",
            "Master returned an invalid enrollment response",
            done=True,
            error=True,
        )
        raise HTTPException(400, "Master returned an invalid enrollment response")

    await _send_worker_enroll_progress("saving_worker_config", "Saving worker configuration...")
    new_config = init_as_worker(
        body.name,
        master_url,
        api_key=data["api_key"],
        update_public_key=data.get("signing_public_key"),
    )
    cf_tunnel, cf_tunnel_error = await _configure_enrolled_worker_cloudflare(
        data.get("cf_config"),
        new_config["node_name"],
        new_config["master_url"],
        new_config["api_key"],
        skip_cf=body.skip_cf,
    )
    result = {
        "node_id": new_config["node_id"],
        "node_name": new_config["node_name"],
        "role": "worker",
        "master_url": new_config["master_url"],
        "address": worker_address,
    }
    if cf_tunnel:
        result["cf_tunnel"] = cf_tunnel
    if cf_tunnel_error:
        result["cf_tunnel_error"] = cf_tunnel_error
    if not cf_tunnel_error:
        await _send_worker_enroll_progress("complete", "Worker registration complete", done=True)
    return result


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
    result["dashboard_zone_id"] = config.get("dashboard_zone_id")
    result["dashboard_zone_name"] = config.get("dashboard_zone_name")
    result["cf_configured"] = bool(config.get("cf_token") and config.get("cf_account_id"))
    result["cf_zone_id"] = config.get("cf_zone_id")
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
    result["mcp_tokens"] = [
        {
            "token_id": mcp_token_id(token),
            "name": v.get("name"),
            "scopes": v.get("scopes") or [],
            "node_ids": v.get("node_ids") or [],
            "profile_ids": v.get("profile_ids") or [],
            "created_at": v.get("created_at"),
            "expires_at": v.get("expires_at"),
            "last_used_at": v.get("last_used_at"),
        }
        for token, v in config.get("mcp_tokens", {}).items()
    ]
    return result


# --- Dashboard CF Access ---

class DashboardAccessBody(BaseModel):
    hostname: Optional[str] = None
    subdomain: Optional[str] = None
    zone_id: Optional[str] = None


@cluster_router.post("/api/config/dashboard-access")
async def config_enable_dashboard_access(body: DashboardAccessBody):
    """Put the dashboard behind Cloudflare Access."""
    from tunnel import (
        _load_cf_config, create_tunnel, get_tunnel_token,
        init_tunnel_config, add_tunnel_route, create_dns_record,
        create_access_app, list_available_zones, discover_access_team_domain,
    )
    config = get_node_config()
    if not config:
        raise HTTPException(400, "Node not configured")

    cf_cfg = _load_cf_config()
    if not cf_cfg:
        raise HTTPException(400, "Cloudflare not configured. Set up in Settings → Cloudflare.")

    try:
        zones = await list_available_zones()
    except ValueError as e:
        raise HTTPException(502, str(e))
    if not zones:
        raise HTTPException(400, "No active Cloudflare domains found in this account")

    selected_zone_id = body.zone_id or cf_cfg.get("zone_id")
    if not selected_zone_id:
        raise HTTPException(400, "No Cloudflare domain selected")
    zone = next((z for z in zones if z.get("id") == selected_zone_id), None)
    if not zone:
        raise HTTPException(400, "Selected Cloudflare domain is not available")

    zone_name = zone["name"]
    if body.subdomain:
        subdomain = _normalize_dashboard_subdomain(body.subdomain)
        hostname = f"{subdomain}.{zone_name}"
    elif body.hostname:
        hostname = _normalize_dashboard_hostname(body.hostname)
    else:
        raise HTTPException(400, "Hostname or subdomain is required")

    if not (hostname == zone_name or hostname.endswith(f".{zone_name}")):
        raise HTTPException(400, f"Hostname must be under selected domain '{zone_name}'")

    from ws_routes import send_progress
    _task = "dashboard-access"

    try:
        policy_id = cf_cfg.get("default_policy_id")
        if policy_id:
            pinned_team_domain = (config.get("cf_team_domain") or "").strip().lower()
            if not pinned_team_domain:
                await send_progress(_task, "discovering_access_org", "Discovering Cloudflare Access organization...")
                team_domain = await discover_access_team_domain(cf_cfg.get("token", ""), cf_cfg.get("account_id", ""))
                cfg_mut = get_node_config()
                if cfg_mut:
                    cfg_mut["cf_team_domain"] = team_domain
                    cfg_mut.setdefault("cf_access_issuer", f"https://{team_domain}.cloudflareaccess.com")
                    from node_config import save_node_config
                    save_node_config(cfg_mut)
            elif not config.get("cf_access_issuer"):
                cfg_mut = get_node_config()
                if cfg_mut:
                    cfg_mut["cf_access_issuer"] = f"https://{pinned_team_domain}.cloudflareaccess.com"
                    from node_config import save_node_config
                    save_node_config(cfg_mut)

        tid = config.get("tunnel_id")
        if not tid:
            await send_progress(_task, "creating_tunnel", "Creating Cloudflare tunnel...")
            tunnel_result = await create_tunnel(config["node_name"])
            tid = tunnel_result["id"]
            await init_tunnel_config(tid)
            set_tunnel_id(tid)

            await send_progress(_task, "adding_route", "Adding tunnel route for dashboard...")
            await add_tunnel_route(hostname, "http://localhost:9000", tunnel_id=tid)

            await send_progress(_task, "getting_token", "Getting tunnel connector token...")
            token = await get_tunnel_token(tid)

            await send_progress(_task, "installing_cloudflared", "Installing and starting cloudflared...")
            await setup_cloudflared_user_service(token)
            await send_progress(_task, "cloudflared_ready", "cloudflared is running")
        else:
            # Tunnel exists already — just add the route
            await send_progress(_task, "adding_route", "Adding tunnel route for dashboard...")
            await add_tunnel_route(hostname, "http://localhost:9000", tunnel_id=tid)

        await send_progress(_task, "creating_dns", f"Creating DNS record for {hostname}...")
        await create_dns_record(
            hostname,
            tunnel_id=tid,
            zone_id=selected_zone_id,
            zone_name=zone_name,
        )

        if policy_id:
            await send_progress(_task, "creating_access", "Creating Cloudflare Access app...")
            app_result = await create_access_app("inframatik dashboard", hostname, policy_id)
            # Store the Access app AUD for CF JWT validation
            if app_result.get("aud"):
                config = get_node_config()
                if config:
                    config["cf_access_aud"] = app_result["aud"]
                    from node_config import save_node_config
                    save_node_config(config)

        set_dashboard_hostname(hostname, zone_id=selected_zone_id, zone_name=zone_name)

        await send_progress(_task, "complete", f"Dashboard access enabled at {hostname}", done=True)

        return {
            "status": "enabled",
            "hostname": hostname,
            "zone_id": selected_zone_id,
            "zone_name": zone_name,
            "tunnel_id": tid,
        }
    except ValueError as e:
        await send_progress(_task, "error", str(e), done=True, error=True)
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        await send_progress(_task, "error", str(e), done=True, error=True)
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
    zone_id = config.get("dashboard_zone_id")

    warnings: list[str] = []
    try:
        await remove_tunnel_route(hostname)
    except (ValueError, RuntimeError) as e:
        warnings.append(f"Failed to remove tunnel route: {e}")
        logger.warning("Dashboard access cleanup failed (route %s): %s", hostname, e)
    try:
        await delete_dns_record(hostname, zone_id=zone_id)
    except (ValueError, RuntimeError) as e:
        warnings.append(f"Failed to delete DNS record: {e}")
        logger.warning("Dashboard access cleanup failed (dns %s): %s", hostname, e)
    try:
        await delete_access_app(hostname)
    except (ValueError, RuntimeError) as e:
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


def _require_master_for_worker_allowlist(config: Optional[dict]):
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only a master node can manage worker target allowlist")


@cluster_router.get("/api/config/worker-target-allowlist")
async def config_get_worker_target_allowlist():
    config = get_node_config()
    _require_master_for_worker_allowlist(config)
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
    config = get_node_config()
    _require_master_for_worker_allowlist(config)
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
    config = get_node_config()
    _require_master_for_worker_allowlist(config)
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


class McpTokenBody(BaseModel):
    name: Optional[str] = None
    scopes: list[str]
    node_ids: list[str] = []
    profile_ids: list[str] = []


@cluster_router.post("/api/config/mcp-tokens")
async def config_create_mcp_token(body: McpTokenBody):
    try:
        token = create_mcp_token(
            name=body.name,
            scopes=body.scopes,
            node_ids=body.node_ids,
            profile_ids=body.profile_ids,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    config = get_node_config() or {}
    meta = config.get("mcp_tokens", {}).get(token, {})
    try:
        scopes = normalize_mcp_scopes(meta.get("scopes"))
    except ValueError:
        scopes = []
    return {
        "token": token,
        "token_id": mcp_token_id(token),
        "name": meta.get("name"),
        "scopes": scopes,
        "node_ids": meta.get("node_ids") or [],
        "profile_ids": meta.get("profile_ids") or [],
        "created_at": meta.get("created_at"),
        "expires_at": meta.get("expires_at"),
    }


@cluster_router.delete("/api/config/mcp-tokens/by-id/{token_id}")
async def config_revoke_mcp_token_by_id(token_id: str):
    if not revoke_mcp_token_by_id(token_id):
        raise HTTPException(404, "MCP token not found")
    return {"status": "revoked"}


class EnrollBody(BaseModel):
    token: str
    node_name: str
    address: str
    skip_cf: bool = False


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
        add_worker(body.node_name, normalized_address, worker_api_key, cf_opt_out=body.skip_cf)
    except ValueError as e:
        raise HTTPException(400, str(e))

    result = {
        "status": "enrolled",
        "api_key": worker_api_key,
        "signing_public_key": signing_public_key,
    }
    cf_config = _master_cf_config_for_enrollment(config)
    if cf_config and not body.skip_cf:
        result["cf_config"] = cf_config
    return result


# ---------------------------------------------------------------------------
# Registration / heartbeat (master receives from workers)
# ---------------------------------------------------------------------------

class RegisterBody(BaseModel):
    node_id: str
    node_name: str
    address: str
    tunnel_id: Optional[str] = None


@cluster_router.post("/api/nodes/register")
async def register(body: RegisterBody, request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        raise HTTPException(401, "API key required")
    ok = register_node(body.node_id, body.node_name, body.address, api_key)
    if not ok:
        raise HTTPException(401, "Unknown API key")
    if body.tunnel_id:
        try:
            set_worker_tunnel_id_for_api_key(api_key, body.tunnel_id)
        except ValueError as e:
            logger.warning("Failed to record worker tunnel during register: %s", e)
    return {"status": "registered"}


class HeartbeatBody(BaseModel):
    node_id: str
    tunnel_id: Optional[str] = None


class WorkerEventBody(BaseModel):
    node_id: str
    event: dict


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
    if body.tunnel_id:
        try:
            set_worker_tunnel_id_for_api_key(api_key, body.tunnel_id)
        except ValueError as e:
            logger.warning("Failed to record worker tunnel during heartbeat: %s", e)
    return {"status": "ok"}


def _broadcast_worker_event(config_node_id: str, real_node_id: str, event: dict):
    from ws_routes import publish

    publish({
        **event,
        "node_id": config_node_id,
        "real_node_id": real_node_id,
        "source": "worker",
    })


@cluster_router.post("/api/nodes/events")
async def worker_event(body: WorkerEventBody, request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        raise HTTPException(401, "API key required")
    match = get_worker_by_api_key(api_key)
    if not match:
        raise HTTPException(401, "Unknown API key")
    if not validate_heartbeat_key(body.node_id, api_key):
        raise HTTPException(401, "Invalid API key for node")
    if not heartbeat_node(body.node_id):
        raise HTTPException(404, "Node not registered — re-register required")

    event = dict(body.event or {})
    if event.get("type") not in {"inference_operation", "model_job"}:
        return {"status": "ignored"}
    config_node_id, _worker = match
    _broadcast_worker_event(config_node_id, body.node_id, event)
    return {"status": "ok"}


@cluster_router.post("/api/nodes/tunnel")
async def report_worker_tunnel(body: WorkerTunnelBody, request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        raise HTTPException(401, "API key required")
    try:
        set_worker_tunnel_id_for_api_key(api_key, body.tunnel_id)
    except ValueError as e:
        raise HTTPException(401, str(e))
    return {"status": "updated", "tunnel_id": body.tunnel_id}


# ---------------------------------------------------------------------------
# Node list (master)
# ---------------------------------------------------------------------------

@cluster_router.get("/api/nodes")
async def list_nodes():
    return await get_all_nodes()


@cluster_router.get("/api/nodes/{node_id}/snapshot")
async def node_snapshot(node_id: str):
    try:
        return await get_node_snapshot(node_id)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


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


@cluster_router.post("/api/nodes/{node_id}/cf/service/update")
async def proxy_cf_service_update(node_id: str, body: dict = None):
    payload = body if isinstance(body, dict) else {}
    try:
        return await proxy_to_node(node_id, "POST", "/api/internal/cf/service/update", payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/models")
async def proxy_models(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/models")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/models/resolve")
async def proxy_models_resolve(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", "/api/models/resolve", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/models/storage")
async def proxy_models_storage(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/models/storage")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.put("/api/nodes/{node_id}/models/storage")
async def proxy_models_update_storage(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "PUT", "/api/models/storage", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/models/import")
async def proxy_models_import(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", "/api/models/import", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/models/download")
async def proxy_models_download(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", "/api/models/download", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/models/jobs/{job_id}")
async def proxy_models_job(node_id: str, job_id: str):
    try:
        return await proxy_to_node(node_id, "GET", f"/api/models/jobs/{job_id}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/models/jobs/{job_id}/cancel")
async def proxy_models_cancel_job(node_id: str, job_id: str):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/models/jobs/{job_id}/cancel")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.delete("/api/nodes/{node_id}/models/jobs/{job_id}/staging")
async def proxy_models_clean_job_staging(node_id: str, job_id: str):
    try:
        return await proxy_to_node(node_id, "DELETE", f"/api/models/jobs/{job_id}/staging")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/models/{artifact_id}/manifest")
async def proxy_models_manifest(node_id: str, artifact_id: str, snapshot: Optional[str] = None):
    path = f"/api/models/{artifact_id}/manifest"
    if snapshot:
        path = f"{path}?{urlencode({'snapshot': snapshot})}"
    try:
        return await proxy_to_node(node_id, "GET", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/models/{artifact_id}/verify")
async def proxy_models_verify(node_id: str, artifact_id: str, snapshot: Optional[str] = None):
    path = f"/api/models/{artifact_id}/verify"
    if snapshot:
        path = f"{path}?{urlencode({'snapshot': snapshot})}"
    try:
        return await proxy_to_node(node_id, "POST", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.delete("/api/nodes/{node_id}/models/{artifact_id}")
async def proxy_models_delete(
    node_id: str,
    artifact_id: str,
    snapshot: Optional[str] = None,
    force_stopped_references: bool = False,
    new_active_snapshot: Optional[str] = None,
):
    params = {}
    if snapshot:
        params["snapshot"] = snapshot
    if force_stopped_references:
        params["force_stopped_references"] = "true"
    if new_active_snapshot:
        params["new_active_snapshot"] = new_active_snapshot
    path = f"/api/models/{artifact_id}"
    if params:
        path = f"{path}?{urlencode(params)}"
    try:
        return await proxy_to_node(node_id, "DELETE", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/overview")
async def proxy_inference_overview(node_id: str, include_system: bool = True):
    path = "/api/inference/overview"
    if not include_system:
        path = f"{path}?{urlencode({'include_system': 'false'})}"
    try:
        return await proxy_to_node(node_id, "GET", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/launchers")
async def proxy_inference_launchers(node_id: str, include_validation: bool = False):
    path = "/api/inference/launchers"
    if include_validation:
        path = f"{path}?{urlencode({'include_validation': 'true'})}"
    try:
        return await proxy_to_node(node_id, "GET", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/launchers")
async def proxy_create_inference_launcher(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", "/api/inference/launchers", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.put("/api/nodes/{node_id}/inference/launchers/{launcher_id}")
async def proxy_update_inference_launcher(node_id: str, launcher_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "PUT", f"/api/inference/launchers/{launcher_id}", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.delete("/api/nodes/{node_id}/inference/launchers/{launcher_id}")
async def proxy_delete_inference_launcher(
    node_id: str,
    launcher_id: str,
    force_stopped_references: bool = False,
):
    path = f"/api/inference/launchers/{launcher_id}"
    if force_stopped_references:
        path = f"{path}?{urlencode({'force_stopped_references': 'true'})}"
    try:
        return await proxy_to_node(node_id, "DELETE", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/preview")
async def proxy_preview_inference_profile(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", "/api/inference/profiles/preview", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/profiles")
async def proxy_inference_profiles(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/inference/profiles")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles")
async def proxy_create_inference_profile(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", "/api/inference/profiles", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/profiles/{profile_id}")
async def proxy_get_inference_profile(node_id: str, profile_id: str):
    try:
        return await proxy_to_node(node_id, "GET", f"/api/inference/profiles/{profile_id}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.put("/api/nodes/{node_id}/inference/profiles/{profile_id}")
async def proxy_update_inference_profile(node_id: str, profile_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "PUT", f"/api/inference/profiles/{profile_id}", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.delete("/api/nodes/{node_id}/inference/profiles/{profile_id}")
async def proxy_delete_inference_profile(node_id: str, profile_id: str, force: bool = False):
    path = f"/api/inference/profiles/{profile_id}"
    if force:
        path = f"{path}?{urlencode({'force': 'true'})}"
    try:
        return await proxy_to_node(node_id, "DELETE", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/render")
async def proxy_render_inference_profile(node_id: str, profile_id: str):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/render")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/start")
async def proxy_start_inference_profile(node_id: str, profile_id: str):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/start")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/stop")
async def proxy_stop_inference_profile(node_id: str, profile_id: str):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/stop")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/restart")
async def proxy_restart_inference_profile(node_id: str, profile_id: str):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/restart")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/profiles/{profile_id}/instances")
async def proxy_inference_profile_instances(node_id: str, profile_id: str):
    try:
        return await proxy_to_node(node_id, "GET", f"/api/inference/profiles/{profile_id}/instances")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/instances/{instance_index}/start")
async def proxy_start_inference_instance(node_id: str, profile_id: str, instance_index: int):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/instances/{instance_index}/start")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/instances/{instance_index}/stop")
async def proxy_stop_inference_instance(node_id: str, profile_id: str, instance_index: int):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/instances/{instance_index}/stop")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/instances/{instance_index}/restart")
async def proxy_restart_inference_instance(node_id: str, profile_id: str, instance_index: int):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/instances/{instance_index}/restart")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/profiles/{profile_id}/logs")
async def proxy_inference_profile_logs(node_id: str, profile_id: str, lines: int = 150, instance: Optional[int] = None):
    params = {"lines": str(lines)}
    if instance is not None:
        params["instance"] = str(instance)
    path = f"/api/inference/profiles/{profile_id}/logs?{urlencode(params)}"
    try:
        return await proxy_to_node(node_id, "GET", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/profiles/{profile_id}/instances/{instance_index}/logs")
async def proxy_inference_instance_logs(node_id: str, profile_id: str, instance_index: int, lines: int = 300):
    path = f"/api/inference/profiles/{profile_id}/instances/{instance_index}/logs?{urlencode({'lines': str(lines)})}"
    try:
        return await proxy_to_node(node_id, "GET", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/profiles/{profile_id}/health")
async def proxy_inference_profile_health(node_id: str, profile_id: str):
    try:
        return await proxy_to_node(node_id, "GET", f"/api/inference/profiles/{profile_id}/health")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/profiles/{profile_id}/instances/{instance_index}/health")
async def proxy_inference_instance_health(node_id: str, profile_id: str, instance_index: int):
    try:
        return await proxy_to_node(node_id, "GET", f"/api/inference/profiles/{profile_id}/instances/{instance_index}/health")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/test")
async def proxy_test_inference_profile(node_id: str, profile_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/test", body)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/profiles/{profile_id}/instances/{instance_index}/test")
async def proxy_test_inference_instance(node_id: str, profile_id: str, instance_index: int, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/instances/{instance_index}/test", body)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/operations")
async def proxy_inference_operations(node_id: str, profile_id: Optional[str] = None, state: Optional[str] = None):
    params = {}
    if profile_id:
        params["profile_id"] = profile_id
    if state:
        params["state"] = state
    path = "/api/inference/operations"
    if params:
        path = f"{path}?{urlencode(params)}"
    try:
        return await proxy_to_node(node_id, "GET", path)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/inference/operations/{operation_id}")
async def proxy_get_inference_operation(node_id: str, operation_id: str):
    try:
        return await proxy_to_node(node_id, "GET", f"/api/inference/operations/{operation_id}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/operations/{operation_id}/cancel")
async def proxy_cancel_inference_operation(node_id: str, operation_id: str):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/operations/{operation_id}/cancel")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/inference/launchers/{launcher_id}/validate")
async def proxy_validate_inference_launcher(node_id: str, launcher_id: str):
    try:
        return await proxy_to_node(node_id, "POST", f"/api/inference/launchers/{launcher_id}/validate")
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
    except (ValueError, OSError, tarfile.TarError) as e:
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


@cluster_router.post("/api/update/git")
async def update_master_from_git():
    """Master-only: update this checkout from git and restart."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only master can update from git")
    try:
        result = update_from_git()
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    asyncio.get_event_loop().call_later(1, restart_service)
    return {**result, "restart": "scheduled"}


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
