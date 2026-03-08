from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

import httpx
from cloudflared import (
    setup_cloudflared_user_service,
    get_cloudflared_user_service_status,
    get_cloudflared_user_service_logs,
    restart_cloudflared_user_service,
    update_cloudflared_user_binary,
)

from node_config import (
    get_node_config,
    get_worker_by_node_id,
    set_tunnel_id,
    set_worker_tunnel_id,
    save_cf_config,
    clear_cf_config,
)
from tunnel import (
    _load_cf_config,
    _cf_headers,
    list_available_zones,
    list_tunnels,
    create_tunnel,
    get_tunnel_token,
    init_tunnel_config,
    get_tunnel_routes,
    add_tunnel_route,
    remove_tunnel_route,
    create_dns_record,
    delete_dns_record,
    list_dns_records,
    list_access_apps,
    list_access_policies,
    create_access_app,
    delete_access_app,
)

cf_router = APIRouter()


def _require_cf_config():
    """Raise 400 if Cloudflare is not configured."""
    cfg = _load_cf_config()
    if not cfg:
        raise HTTPException(400, "Cloudflare not configured. Set up in Settings → Cloudflare.")


def _require_internal_api_key(request: Request):
    """Require worker API key for internal master→worker proxy calls."""
    api_key = request.headers.get("X-Api-Key")
    config = get_node_config()
    if not api_key or not config or api_key != config.get("api_key"):
        raise HTTPException(401, "API key required")


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------

@cf_router.get("/api/cf/tunnels")
async def api_list_tunnels():
    _require_cf_config()
    try:
        return await list_tunnels()
    except ValueError as e:
        raise HTTPException(502, str(e))


@cf_router.get("/api/cf/zones")
async def api_list_zones():
    _require_cf_config()
    try:
        zones = await list_available_zones()
    except ValueError as e:
        raise HTTPException(502, str(e))
    return {"zones": zones}


class CreateTunnelBody(BaseModel):
    name: str


@cf_router.post("/api/cf/tunnels")
async def api_create_tunnel(body: CreateTunnelBody):
    _require_cf_config()
    try:
        result = await create_tunnel(body.name)
        await init_tunnel_config(result["id"])
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@cf_router.get("/api/cf/tunnels/{tunnel_id}/token")
async def api_get_tunnel_token(tunnel_id: str):
    _require_cf_config()
    try:
        token = await get_tunnel_token(tunnel_id)
        return {"tunnel_id": tunnel_id, "token": token}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Ingress routes
# ---------------------------------------------------------------------------

@cf_router.get("/api/cf/routes")
async def api_list_routes(tunnel_id: Optional[str] = None):
    _require_cf_config()
    try:
        return await get_tunnel_routes(tunnel_id=tunnel_id)
    except ValueError as e:
        raise HTTPException(502, str(e))


class AddRouteBody(BaseModel):
    hostname: str
    service: str
    tunnel_id: Optional[str] = None


@cf_router.post("/api/cf/routes")
async def api_add_route(body: AddRouteBody):
    _require_cf_config()
    try:
        await add_tunnel_route(body.hostname, body.service, tunnel_id=body.tunnel_id)
        return {"status": "added", "hostname": body.hostname}
    except ValueError as e:
        raise HTTPException(400, str(e))


@cf_router.delete("/api/cf/routes/{hostname}")
async def api_remove_route(hostname: str, tunnel_id: Optional[str] = None):
    _require_cf_config()
    try:
        await remove_tunnel_route(hostname, tunnel_id=tunnel_id)
        return {"status": "removed", "hostname": hostname}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

@cf_router.get("/api/cf/dns")
async def api_list_dns():
    _require_cf_config()
    try:
        return await list_dns_records()
    except ValueError as e:
        raise HTTPException(502, str(e))


class CreateDnsBody(BaseModel):
    hostname: str
    tunnel_id: Optional[str] = None


@cf_router.post("/api/cf/dns")
async def api_create_dns(body: CreateDnsBody):
    _require_cf_config()
    try:
        record_id = await create_dns_record(body.hostname, tunnel_id=body.tunnel_id)
        return {"status": "created", "id": record_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@cf_router.delete("/api/cf/dns/{hostname}")
async def api_delete_dns(hostname: str):
    _require_cf_config()
    try:
        result = await delete_dns_record(hostname)
        if not result:
            raise HTTPException(404, f"No DNS record found for {hostname}")
        return {"status": "deleted", "hostname": hostname}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Access apps
# ---------------------------------------------------------------------------

@cf_router.get("/api/cf/access/apps")
async def api_list_access_apps():
    _require_cf_config()
    try:
        return await list_access_apps()
    except ValueError as e:
        raise HTTPException(502, str(e))


class CreateAccessAppBody(BaseModel):
    name: str
    hostname: str
    policy_id: str


@cf_router.post("/api/cf/access/apps")
async def api_create_access_app(body: CreateAccessAppBody):
    _require_cf_config()
    try:
        app_id = await create_access_app(body.name, body.hostname, body.policy_id)
        return {"id": app_id, "status": "created"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@cf_router.delete("/api/cf/access/apps/{hostname}")
async def api_delete_access_app(hostname: str):
    _require_cf_config()
    try:
        result = await delete_access_app(hostname)
        if not result:
            raise HTTPException(404, f"No Access app found for {hostname}")
        return {"status": "deleted", "hostname": hostname}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Access policies (read-only discovery)
# ---------------------------------------------------------------------------

@cf_router.get("/api/cf/access/policies")
async def api_list_policies():
    _require_cf_config()
    try:
        return await list_access_policies()
    except ValueError as e:
        raise HTTPException(502, str(e))


# ---------------------------------------------------------------------------
# Worker tunnel setup (master-initiated)
# ---------------------------------------------------------------------------

class SetupWorkerTunnelBody(BaseModel):
    tunnel_name: Optional[str] = None


@cf_router.post("/api/nodes/{node_id}/cf/setup")
async def api_setup_worker_tunnel(node_id: str, body: SetupWorkerTunnelBody = None):
    """Create a CF tunnel for a worker and push the token to it."""
    _require_cf_config()
    from proxy import proxy_to_node

    worker = get_worker_by_node_id(node_id)
    if not worker:
        raise HTTPException(404, f"Worker {node_id} not found")

    tunnel_name = (body.tunnel_name if body and body.tunnel_name
                   else worker["name"])

    try:
        # 1. Create tunnel
        tunnel_result = await create_tunnel(tunnel_name)
        tid = tunnel_result["id"]

        # 2. Get connector token
        token = await get_tunnel_token(tid)

        # 3. Init ingress config
        await init_tunnel_config(tid)

        # 4. Store tunnel_id in master's worker config
        set_worker_tunnel_id(node_id, tid)

        # 5. Push token to worker
        push_result = await proxy_to_node(
            node_id, "POST", "/api/cf/token",
            {"tunnel_id": tid, "token": token}
        )

        return {
            "status": "setup_complete",
            "tunnel_id": tid,
            "tunnel_name": tunnel_name,
            "worker_result": push_result,
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


# ---------------------------------------------------------------------------
# Worker-side: receive tunnel token
# ---------------------------------------------------------------------------

class ReceiveTunnelTokenBody(BaseModel):
    tunnel_id: str
    token: str


@cf_router.post("/api/cf/token")
async def api_receive_tunnel_token(body: ReceiveTunnelTokenBody, request: Request):
    """Worker receives tunnel token from master, writes it, starts cloudflared."""
    api_key = request.headers.get("X-Api-Key")
    config = get_node_config()
    if not api_key or not config or api_key != config.get("api_key"):
        raise HTTPException(401, "API key required")
    set_tunnel_id(body.tunnel_id)
    try:
        await setup_cloudflared_user_service(body.token)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    return {"status": "token_received", "tunnel_id": body.tunnel_id}


# ---------------------------------------------------------------------------
# Local cloudflared user service controls
# ---------------------------------------------------------------------------

@cf_router.get("/api/cf/service/status")
async def api_cf_service_status():
    return await get_cloudflared_user_service_status()


@cf_router.get("/api/cf/service/logs")
async def api_cf_service_logs(lines: int = 80):
    try:
        logs = await get_cloudflared_user_service_logs(lines=lines)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"lines": lines, "logs": logs}


@cf_router.post("/api/cf/service/restart")
async def api_cf_service_restart():
    try:
        service = await restart_cloudflared_user_service()
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"status": "restarted", "service": service}


class UpdateCloudflaredBody(BaseModel):
    version: Optional[str] = None


@cf_router.post("/api/cf/service/update")
async def api_cf_service_update(body: UpdateCloudflaredBody = None):
    version = body.version if body else None
    try:
        result = await update_cloudflared_user_binary(version=version)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"status": "updated", "cloudflared": result}


@cf_router.get("/api/internal/cf/service/status")
async def api_internal_cf_service_status(request: Request):
    _require_internal_api_key(request)
    return await api_cf_service_status()


@cf_router.get("/api/internal/cf/service/logs")
async def api_internal_cf_service_logs(request: Request, lines: int = 80):
    _require_internal_api_key(request)
    return await api_cf_service_logs(lines=lines)


@cf_router.post("/api/internal/cf/service/restart")
async def api_internal_cf_service_restart(request: Request):
    _require_internal_api_key(request)
    return await api_cf_service_restart()


@cf_router.post("/api/internal/cf/service/update")
async def api_internal_cf_service_update(request: Request, body: UpdateCloudflaredBody = None):
    _require_internal_api_key(request)
    return await api_cf_service_update(body=body)


# ---------------------------------------------------------------------------
# CF Setup Wizard (guided configuration flow)
# ---------------------------------------------------------------------------

class ValidateTokenBody(BaseModel):
    token: str


@cf_router.post("/api/cf/setup/validate-token")
async def cf_setup_validate_token(body: ValidateTokenBody):
    """Validate a CF API token and return accessible accounts.

    Tries /accounts first, falls back to discovering accounts from /zones
    (works with tokens that only have zone-scoped permissions).
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Try listing accounts directly
            resp = await client.get(
                "https://api.cloudflare.com/client/v4/accounts",
                headers=_cf_headers(body.token),
                params={"per_page": 50},
            )
            data = resp.json()
            if not data.get("success"):
                raise HTTPException(401, "Invalid API token")
            accounts = [{"id": a["id"], "name": a.get("name", "")} for a in data.get("result", [])]

            # If no accounts returned, discover from zones
            if not accounts:
                zone_resp = await client.get(
                    "https://api.cloudflare.com/client/v4/zones",
                    headers=_cf_headers(body.token),
                    params={"per_page": 50, "status": "active"},
                )
                zone_data = zone_resp.json()
                if not zone_data.get("success") or not zone_data.get("result"):
                    raise HTTPException(400, "Token has no accessible accounts or zones")
                # Extract unique accounts from zones
                seen = {}
                for z in zone_data["result"]:
                    acct = z.get("account", {})
                    aid = acct.get("id")
                    if aid and aid not in seen:
                        seen[aid] = acct.get("name", aid)
                accounts = [{"id": k, "name": v} for k, v in seen.items()]

            if not accounts:
                raise HTTPException(400, "No accounts accessible with this token")

            # Probe permissions on the first account
            acct_id = accounts[0]["id"]
            missing_perms = []

            # Check Tunnel permission
            tunnel_resp = await client.get(
                f"https://api.cloudflare.com/client/v4/accounts/{acct_id}/cfd_tunnel",
                headers=_cf_headers(body.token),
                params={"per_page": 1},
            )
            if not tunnel_resp.json().get("success"):
                missing_perms.append("Cloudflare Tunnel: Edit")

            # Check DNS permission (via zones)
            zone_check = await client.get(
                "https://api.cloudflare.com/client/v4/zones",
                headers=_cf_headers(body.token),
                params={"per_page": 1, "status": "active"},
            )
            if not zone_check.json().get("success"):
                missing_perms.append("Zone DNS: Edit")

            # Check Access permission
            access_resp = await client.get(
                f"https://api.cloudflare.com/client/v4/accounts/{acct_id}/access/policies",
                headers=_cf_headers(body.token),
            )
            if not access_resp.json().get("success"):
                missing_perms.append("Access: Apps and Policies: Edit")

            if missing_perms:
                raise HTTPException(400,
                    f"Token is missing permissions: {', '.join(missing_perms)}")

            return {"accounts": accounts}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Failed to connect to Cloudflare API")


class ListZonesBody(BaseModel):
    token: str
    account_id: str


@cf_router.post("/api/cf/setup/zones")
async def cf_setup_zones(body: ListZonesBody):
    """List zones (domains) accessible for an account."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.cloudflare.com/client/v4/zones",
                headers=_cf_headers(body.token),
                params={"account.id": body.account_id, "per_page": 50, "status": "active"},
            )
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(400, "Failed to fetch zones")
    if not data.get("success"):
        raise HTTPException(400, f"Failed to list zones: {data.get('errors')}")
    zones = [{"id": z["id"], "name": z["name"]} for z in data.get("result", [])]
    return {"zones": zones}


class ListPoliciesBody(BaseModel):
    token: str
    account_id: str


@cf_router.post("/api/cf/setup/policies")
async def cf_setup_policies(body: ListPoliciesBody):
    """List existing reusable Access policies."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.cloudflare.com/client/v4/accounts/{body.account_id}/access/policies",
                headers=_cf_headers(body.token),
            )
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(400, "Failed to fetch policies")
    if not data.get("success"):
        return {"policies": []}
    policies = [
        {
            "id": p["id"],
            "name": p.get("name", ""),
            "decision": p.get("decision", ""),
            "include": p.get("include", []),
        }
        for p in data.get("result", [])
    ]
    return {"policies": policies}


class CreatePolicyBody(BaseModel):
    token: str
    account_id: str
    name: str
    email_domain: str


@cf_router.post("/api/cf/setup/create-policy")
async def cf_setup_create_policy(body: CreatePolicyBody):
    """Create a reusable Access policy allowing an email domain."""
    payload = {
        "name": body.name,
        "decision": "allow",
        "include": [{"email_domain": {"domain": body.email_domain}}],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.cloudflare.com/client/v4/accounts/{body.account_id}/access/policies",
                headers=_cf_headers(body.token),
                json=payload,
            )
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(400, "Failed to create policy")
    if not data.get("success"):
        raise HTTPException(400, f"Failed to create policy: {data.get('errors')}")
    result = data["result"]
    return {"id": result["id"], "name": result.get("name", "")}


class SaveCfConfigBody(BaseModel):
    token: str
    account_id: str
    zone_id: str
    default_policy_id: Optional[str] = None


@cf_router.post("/api/cf/setup/save")
async def cf_setup_save(body: SaveCfConfigBody):
    """Store Cloudflare credentials in node config."""
    config = get_node_config()
    if not config:
        raise HTTPException(400, "Node not configured")
    save_cf_config(body.token, body.account_id, body.zone_id, body.default_policy_id)
    return {"status": "saved"}


@cf_router.delete("/api/cf/setup")
async def cf_setup_clear():
    """Remove Cloudflare configuration."""
    clear_cf_config()
    return {"status": "cleared"}
