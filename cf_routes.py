import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

import httpx

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


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------

@cf_router.get("/api/cf/tunnels")
async def api_list_tunnels():
    _require_cf_config()
    return await list_tunnels()


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
    return await get_tunnel_routes(tunnel_id=tunnel_id)


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
    return await list_dns_records()


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
    return await list_access_apps()


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
    return await list_access_policies()


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

    proc = await asyncio.create_subprocess_exec(
        "sudo", "/usr/local/bin/infra-cf-setup", body.token,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        raise HTTPException(500, "Failed to setup cloudflared")

    return {"status": "token_received", "tunnel_id": body.tunnel_id}


# ---------------------------------------------------------------------------
# CF Setup Wizard (guided configuration flow)
# ---------------------------------------------------------------------------

class ValidateTokenBody(BaseModel):
    token: str


@cf_router.post("/api/cf/setup/validate-token")
async def cf_setup_validate_token(body: ValidateTokenBody):
    """Validate a CF API token and return accessible accounts."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.cloudflare.com/client/v4/accounts",
                headers=_cf_headers(body.token),
                params={"per_page": 50},
            )
            data = resp.json()
    except Exception:
        raise HTTPException(400, "Failed to connect to Cloudflare API")
    if not data.get("success"):
        raise HTTPException(401, "Invalid API token")
    accounts = [{"id": a["id"], "name": a.get("name", "")} for a in data.get("result", [])]
    if not accounts:
        raise HTTPException(400, "No accounts accessible with this token")
    return {"accounts": accounts}


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
    except Exception:
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
    except Exception:
        raise HTTPException(400, "Failed to fetch policies")
    if not data.get("success"):
        return {"policies": []}
    policies = [
        {"id": p["id"], "name": p.get("name", ""), "decision": p.get("decision", "")}
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
    except Exception:
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
