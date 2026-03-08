import base64
import logging
import secrets
from typing import Optional

import httpx

METRICS_URL = "http://127.0.0.1:20241/metrics"
logger = logging.getLogger("inframatik.tunnel")


# ---------------------------------------------------------------------------
# CF config loading (reads from node.json, configured via settings wizard)
# ---------------------------------------------------------------------------

def _load_cf_config() -> dict | None:
    from node_config import get_node_config
    config = get_node_config()
    if not config:
        return None
    token = config.get("cf_token")
    account_id = config.get("cf_account_id")
    if not token or not account_id:
        return None
    return {
        "token": token,
        "account_id": account_id,
        "zone_id": config.get("cf_zone_id"),
        "default_policy_id": config.get("cf_default_policy_id"),
    }


def _cf_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _get_tunnel_id(tunnel_id: Optional[str] = None) -> str:
    """Resolve tunnel_id from argument or node config. Raises ValueError if not found."""
    if tunnel_id:
        return tunnel_id
    from node_config import get_tunnel_id
    tid = get_tunnel_id()
    if not tid:
        raise ValueError("No tunnel_id configured for this node")
    return tid


def _require_cf_config():
    cfg = _load_cf_config()
    if not cfg:
        raise ValueError("Cloudflare not configured. Set up in Settings → Cloudflare.")
    return cfg


def _dns_record_name_for_hostname(hostname: str, zone_name: Optional[str]) -> str:
    """Return Cloudflare DNS record name for a hostname relative to a zone."""
    hostname = (hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ValueError("Hostname is required")
    if not zone_name:
        # Backward-compatible fallback when zone name is unknown.
        return hostname.split(".")[0]
    zone = zone_name.strip().lower().rstrip(".")
    if not zone:
        return hostname.split(".")[0]
    if hostname == zone:
        return "@"
    suffix = f".{zone}"
    if not hostname.endswith(suffix):
        raise ValueError(f"Hostname '{hostname}' is not under zone '{zone}'")
    rel = hostname[: -len(suffix)]
    if not rel:
        return "@"
    return rel


# ---------------------------------------------------------------------------
# Tunnel status (local metrics)
# ---------------------------------------------------------------------------

async def get_tunnel_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(METRICS_URL)
            text = resp.text
    except (httpx.ConnectError, httpx.TimeoutException):
        return {"connected": False, "connections": 0, "detail": "cloudflared unreachable"}

    connections = 0
    locations = []
    for line in text.splitlines():
        if line.startswith("cloudflared_tunnel_ha_connections "):
            try:
                connections = int(float(line.split()[-1]))
            except (ValueError, IndexError):
                continue
        if line.startswith("cloudflared_tunnel_server_locations{") and line.endswith(" 1"):
            try:
                loc = line.split('edge_location="')[1].split('"')[0]
                locations.append(loc)
            except IndexError:
                continue

    return {
        "connected": connections > 0,
        "connections": connections,
        "locations": locations,
        "detail": f"{connections} connection(s) — {', '.join(locations)}" if locations else f"{connections} connection(s)",
    }


# ---------------------------------------------------------------------------
# Tunnel ingress routes
# ---------------------------------------------------------------------------

async def get_tunnel_routes(tunnel_id: Optional[str] = None) -> list[dict]:
    cfg = _load_cf_config()
    if not cfg:
        return []
    try:
        tid = _get_tunnel_id(tunnel_id)
    except ValueError:
        return []
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/cfd_tunnel/{tid}/configurations"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch tunnel routes for %s: %s", tid, e)
        raise ValueError("Failed to fetch tunnel routes from Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected tunnel route list for %s: %s", tid, data.get("errors"))
        raise ValueError(f"Failed to read tunnel config: {data.get('errors')}")
    ingress = data.get("result", {}).get("config", {}).get("ingress", [])
    return [r for r in ingress if r.get("hostname")]


async def list_available_zones() -> list[dict]:
    """List active zones in the configured Cloudflare account."""
    cfg = _require_cf_config()
    url = "https://api.cloudflare.com/client/v4/zones"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers=_cf_headers(cfg["token"]),
                params={"account.id": cfg["account_id"], "per_page": 100, "status": "active"},
            )
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch zones from Cloudflare: %s", e)
        raise ValueError("Failed to list zones from Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected zones list request: %s", data.get("errors"))
        raise ValueError(f"Failed to list zones: {data.get('errors')}")
    return [{"id": z["id"], "name": z["name"]} for z in data.get("result", [])]


async def add_tunnel_route(hostname: str, service, tunnel_id: Optional[str] = None) -> bool:
    """Add an ingress route. `service` can be a port (int) or full URL (str)."""
    cfg = _require_cf_config()
    tid = _get_tunnel_id(tunnel_id)
    if isinstance(service, int):
        service = f"http://localhost:{service}"
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/cfd_tunnel/{tid}/configurations"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
            if not data.get("success"):
                raise ValueError(f"Failed to read tunnel config: {data.get('errors')}")

            config = data["result"]["config"]
            ingress = config.get("ingress", [])

            for rule in ingress:
                if rule.get("hostname") == hostname:
                    raise ValueError(f"Route for {hostname} already exists")

            new_rule = {"hostname": hostname, "service": service}
            if ingress and not ingress[-1].get("hostname"):
                ingress.insert(len(ingress) - 1, new_rule)
            else:
                ingress.append(new_rule)

            config["ingress"] = ingress
            put_resp = await client.put(
                url,
                headers=_cf_headers(cfg["token"]),
                json={"config": config},
            )
            put_data = put_resp.json()
            if not put_data.get("success"):
                raise ValueError(f"Failed to update tunnel config: {put_data.get('errors')}")
            return True
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")


async def remove_tunnel_route(hostname: str, tunnel_id: Optional[str] = None) -> bool:
    cfg = _require_cf_config()
    tid = _get_tunnel_id(tunnel_id)
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/cfd_tunnel/{tid}/configurations"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
            if not data.get("success"):
                raise ValueError(f"Failed to read tunnel config: {data.get('errors')}")

            config = data["result"]["config"]
            ingress = config.get("ingress", [])

            original_len = len(ingress)
            ingress = [r for r in ingress if r.get("hostname") != hostname]
            if len(ingress) == original_len:
                raise ValueError(f"No route found for {hostname}")

            config["ingress"] = ingress
            put_resp = await client.put(
                url,
                headers=_cf_headers(cfg["token"]),
                json={"config": config},
            )
            put_data = put_resp.json()
            if not put_data.get("success"):
                raise ValueError(f"Failed to update tunnel config: {put_data.get('errors')}")
            return True
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")


# ---------------------------------------------------------------------------
# DNS records
# ---------------------------------------------------------------------------

async def create_dns_record(
    hostname: str,
    tunnel_id: Optional[str] = None,
    zone_id: Optional[str] = None,
    zone_name: Optional[str] = None,
) -> str:
    """Create a proxied CNAME record pointing to the tunnel. Returns record ID."""
    cfg = _require_cf_config()
    effective_zone_id = zone_id or cfg.get("zone_id")
    if not effective_zone_id:
        raise ValueError("CF API missing zone_id")
    tid = _get_tunnel_id(tunnel_id)
    url = f"https://api.cloudflare.com/client/v4/zones/{effective_zone_id}/dns_records"
    record_name = _dns_record_name_for_hostname(hostname, zone_name)
    payload = {
        "type": "CNAME",
        "name": record_name,
        "content": f"{tid}.cfargotunnel.com",
        "proxied": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()
            if not data.get("success"):
                raise ValueError(f"Failed to create DNS record: {data.get('errors')}")
            return data["result"]["id"]
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")


async def delete_dns_record(hostname: str, zone_id: Optional[str] = None) -> bool:
    """Find and delete the DNS record matching a hostname."""
    cfg = _require_cf_config()
    effective_zone_id = zone_id or cfg.get("zone_id")
    if not effective_zone_id:
        raise ValueError("CF API missing zone_id")
    base_url = f"https://api.cloudflare.com/client/v4/zones/{effective_zone_id}/dns_records"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                base_url,
                headers=_cf_headers(cfg["token"]),
                params={"type": "CNAME", "name": hostname},
            )
            data = resp.json()
            if not data.get("success"):
                raise ValueError(f"Failed to list DNS records: {data.get('errors')}")

            records = data.get("result", [])
            if not records:
                return False

            del_resp = await client.delete(
                f"{base_url}/{records[0]['id']}",
                headers=_cf_headers(cfg["token"]),
            )
            return del_resp.json().get("success", False)
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")


async def list_dns_records() -> list[dict]:
    """List all DNS records in the zone."""
    cfg = _require_cf_config()
    if not cfg.get("zone_id"):
        return []
    url = f"https://api.cloudflare.com/client/v4/zones/{cfg['zone_id']}/dns_records"
    records = []
    page = 1
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                resp = await client.get(url, headers=_cf_headers(cfg["token"]),
                                        params={"page": page, "per_page": 100})
                data = resp.json()
                if not data.get("success"):
                    logger.warning("Cloudflare rejected DNS list request: %s", data.get("errors"))
                    raise ValueError(f"Failed to list DNS records: {data.get('errors')}")
                records.extend(data.get("result", []))
                info = data.get("result_info", {})
                if page >= info.get("total_pages", 1):
                    break
                page += 1
    except ValueError:
        raise
    except (httpx.HTTPError, TypeError) as e:
        logger.warning("Failed to fetch DNS records from Cloudflare: %s", e)
        raise ValueError("Failed to list DNS records from Cloudflare")
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "name": r["name"],
            "content": r["content"],
            "proxied": r.get("proxied", False),
        }
        for r in records
    ]


# ---------------------------------------------------------------------------
# Access applications
# ---------------------------------------------------------------------------

async def create_access_app(name: str, hostname: str, policy_id: str) -> dict:
    """Create a CF Access Application with specified policy. Returns {id, aud}."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/apps"
    payload = {
        "name": name,
        "domain": hostname,
        "type": "self_hosted",
        "session_duration": "24h",
        "app_launcher_visible": True,
        "policies": [{"id": policy_id, "precedence": 1}],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()
            if not data.get("success"):
                raise ValueError(f"Failed to create Access app: {data.get('errors')}")
            result = data["result"]
            return {"id": result["id"], "aud": result.get("aud", "")}
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")


async def delete_access_app(hostname: str) -> bool:
    """Find and delete the CF Access Application matching a hostname."""
    cfg = _require_cf_config()
    base_url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/apps"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(base_url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
            if not data.get("success"):
                raise ValueError(f"Failed to list Access apps: {data.get('errors')}")

            app_id = None
            for app in data.get("result", []):
                if app.get("domain") == hostname:
                    app_id = app["id"]
                    break
            if not app_id:
                return False

            del_resp = await client.delete(f"{base_url}/{app_id}", headers=_cf_headers(cfg["token"]))
            return del_resp.json().get("success", False)
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")


async def list_access_apps() -> list[dict]:
    """List all CF Access applications."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/apps"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch Access apps from Cloudflare: %s", e)
        raise ValueError("Failed to fetch Access apps from Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access app list request: %s", data.get("errors"))
        raise ValueError(f"Failed to list Access apps: {data.get('errors')}")
    return [
        {
            "id": a["id"],
            "name": a.get("name", ""),
            "domain": a.get("domain", ""),
            "type": a.get("type", ""),
            "session_duration": a.get("session_duration", ""),
            "policies": a.get("policies", []),
        }
        for a in data.get("result", [])
    ]


async def list_access_policies() -> list[dict]:
    """Discover reusable policies by inspecting existing Access apps."""
    apps = await list_access_apps()
    seen = {}
    for app in apps:
        for policy in app.get("policies", []):
            pid = policy.get("id")
            if pid and pid not in seen:
                seen[pid] = {
                    "id": pid,
                    "name": policy.get("name", "Unnamed"),
                    "decision": policy.get("decision", ""),
                }
    return list(seen.values())


# ---------------------------------------------------------------------------
# Tunnel management
# ---------------------------------------------------------------------------

async def list_tunnels() -> list[dict]:
    """List all tunnels in the CF account."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/cfd_tunnel"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]),
                                    params={"is_deleted": "false"})
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch tunnels from Cloudflare: %s", e)
        raise ValueError("Failed to list tunnels from Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected tunnel list request: %s", data.get("errors"))
        raise ValueError(f"Failed to list tunnels: {data.get('errors')}")
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "status": t.get("status", "unknown"),
        }
        for t in data.get("result", [])
    ]


async def create_tunnel(name: str) -> dict:
    """Create a new CF tunnel, or reuse existing one with the same name. Returns {id, name}."""
    cfg = _require_cf_config()
    base_url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/cfd_tunnel"
    tunnel_secret = base64.b64encode(secrets.token_bytes(32)).decode()
    payload = {
        "name": name,
        "tunnel_secret": tunnel_secret,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(base_url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()

            if data.get("success"):
                result = data["result"]
                return {"id": result["id"], "name": result["name"]}

            # If tunnel with same name already exists, find and reuse it
            errors = data.get("errors", [])
            is_duplicate = any("already" in str(e.get("message", "")).lower() for e in errors)
            if is_duplicate:
                logger.info("Tunnel '%s' already exists, reusing", name)
                list_resp = await client.get(
                    base_url,
                    headers=_cf_headers(cfg["token"]),
                    params={"name": name, "is_deleted": "false"},
                )
                list_data = list_resp.json()
                if list_data.get("success") and list_data.get("result"):
                    existing = list_data["result"][0]
                    return {"id": existing["id"], "name": existing["name"]}

            raise ValueError(f"Failed to create tunnel: {errors}")
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")


async def get_tunnel_token(tunnel_id: str) -> str:
    """Get the connector token for a tunnel (used by cloudflared to connect)."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/cfd_tunnel/{tunnel_id}/token"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")
    if not data.get("success"):
        raise ValueError(f"Failed to get tunnel token: {data.get('errors')}")
    return data["result"]


async def init_tunnel_config(tunnel_id: str) -> bool:
    """Initialize a tunnel's ingress config with a catch-all 404 rule."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/cfd_tunnel/{tunnel_id}/configurations"
    payload = {"config": {"ingress": [{"service": "http_status:404"}]}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()
    except httpx.HTTPError as e:
        raise ValueError(f"CF API error: {e}")
    return data.get("success", False)
