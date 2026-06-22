import base64
import logging
import re
import secrets
from typing import Optional

import httpx

METRICS_URL = "http://127.0.0.1:20241/metrics"
logger = logging.getLogger("inframatik.tunnel")
_ACCESS_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ACCESS_EMAIL_DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


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


def _normalize_cf_team_domain(team_domain: str) -> str:
    team_domain = (team_domain or "").strip().lower()
    if team_domain.startswith("https://"):
        team_domain = team_domain[len("https://"):]
    elif team_domain.startswith("http://"):
        team_domain = team_domain[len("http://"):]
    team_domain = team_domain.split("/", 1)[0]
    if team_domain.endswith(".cloudflareaccess.com"):
        team_domain = team_domain[: -len(".cloudflareaccess.com")]
    return team_domain


async def discover_access_team_domain(token: str, account_id: str) -> str:
    """Discover the Cloudflare Access team domain from account metadata."""
    token = (token or "").strip()
    account_id = (account_id or "").strip()
    if not token or not account_id:
        raise ValueError("Cloudflare token/account_id required to discover Access organization")

    urls = [
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/access/organizations",
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/access/organization",
    ]
    last_errors = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for url in urls:
                try:
                    resp = await client.get(url, headers=_cf_headers(token))
                    data = resp.json()
                except (httpx.HTTPError, ValueError, TypeError) as e:
                    last_errors.append(str(e))
                    continue
                if not data.get("success"):
                    last_errors.append(str(data.get("errors")))
                    continue

                result = data.get("result")
                orgs = []
                if isinstance(result, dict):
                    orgs = [result]
                elif isinstance(result, list):
                    orgs = result

                for org in orgs:
                    auth_domain = org.get("auth_domain", "")
                    team_domain = _normalize_cf_team_domain(auth_domain)
                    if team_domain:
                        return team_domain
                    domain_candidate = org.get("domain", "")
                    team_domain = _normalize_cf_team_domain(domain_candidate)
                    if team_domain:
                        return team_domain
    except httpx.HTTPError as e:
        raise ValueError(f"Failed to query Cloudflare Access organizations: {e}")

    error_detail = "; ".join(last_errors) if last_errors else "no organization data returned"
    raise ValueError(
        "Unable to discover Cloudflare Access team domain. "
        "Ensure token has 'Access: Organizations, Identity Providers, and Groups: Read'. "
        f"Detail: {error_detail}"
    )


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


def _shape_access_app(app: dict) -> dict:
    return {
        "id": app["id"],
        "name": app.get("name", ""),
        "domain": app.get("domain", ""),
        "type": app.get("type", ""),
        "session_duration": app.get("session_duration", ""),
        "app_launcher_visible": app.get("app_launcher_visible", True),
        "policies": app.get("policies", []),
    }


def _extract_access_policy_member(rule: dict) -> Optional[dict]:
    if not isinstance(rule, dict):
        return None

    email = rule.get("email")
    if isinstance(email, dict):
        value = str(email.get("email", "")).strip().lower()
        if value:
            return {"kind": "email", "value": value}

    email_domain = rule.get("email_domain")
    if isinstance(email_domain, dict):
        value = str(email_domain.get("domain", "")).strip().lower()
        if value:
            return {"kind": "email_domain", "value": value}

    return None


def _extract_access_policy_members(include: list) -> list[dict]:
    members = []
    for rule in include:
        member = _extract_access_policy_member(rule)
        if member:
            members.append(member)
    return members


def _shape_access_policy(policy: dict) -> dict:
    include = policy.get("include", [])
    if not isinstance(include, list):
        include = []
    return {
        "id": policy["id"],
        "name": policy.get("name", ""),
        "decision": policy.get("decision", ""),
        "include": include,
        "exclude": policy.get("exclude", []),
        "require": policy.get("require", []),
        "approval_groups": policy.get("approval_groups", []),
        "approval_required": policy.get("approval_required"),
        "purpose_justification_prompt": policy.get("purpose_justification_prompt"),
        "purpose_justification_required": policy.get("purpose_justification_required"),
        "session_duration": policy.get("session_duration"),
        "connection_rules": policy.get("connection_rules"),
        "members": _extract_access_policy_members(include),
    }


def _shape_access_service_token(token: dict) -> dict:
    return {
        "id": token.get("id"),
        "name": token.get("name", ""),
        "client_id": token.get("client_id", ""),
        "duration": token.get("duration"),
        "created_at": token.get("created_at"),
        "updated_at": token.get("updated_at"),
        "expires_at": token.get("expires_at"),
    }


def _normalize_access_policy_member(value: str) -> tuple[str, str, dict]:
    raw = (value or "").strip().lower()
    if not raw:
        raise ValueError("Policy member is required")

    if "@" in raw:
        if not _ACCESS_EMAIL_RE.fullmatch(raw):
            raise ValueError("Policy member must be a valid email or literal email domain")
        return "email", raw, {"email": {"email": raw}}

    domain = raw[1:] if raw.startswith("@") else raw
    if not _ACCESS_EMAIL_DOMAIN_RE.fullmatch(domain):
        raise ValueError("Policy member must be a valid email or literal email domain")
    return "email_domain", domain, {"email_domain": {"domain": domain}}


def _policy_update_payload(policy: dict, include: list[dict]) -> dict:
    payload = {
        "name": policy.get("name", ""),
        "decision": policy.get("decision", "allow"),
        "include": include,
    }
    for key in (
        "exclude",
        "require",
        "approval_groups",
        "approval_required",
        "purpose_justification_prompt",
        "purpose_justification_required",
        "session_duration",
        "connection_rules",
    ):
        if key in policy and policy.get(key) is not None:
            payload[key] = policy.get(key)
    return payload

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


async def get_access_app(app_id: str) -> dict:
    """Fetch a single CF Access application."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/apps/{app_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch Access app %s from Cloudflare: %s", app_id, e)
        raise ValueError("Failed to fetch Access app from Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access app read for %s: %s", app_id, data.get("errors"))
        raise ValueError(f"Failed to read Access app: {data.get('errors')}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Cloudflare returned an invalid Access app payload")
    return result


async def update_access_app_policy(app_id: str, policy_id: str) -> dict:
    """Replace the reusable policy attached to an Access application."""
    cfg = _require_cf_config()
    app = await get_access_app(app_id)
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/apps/{app_id}"
    payload = {
        "name": app.get("name", ""),
        "domain": app.get("domain", ""),
        "type": app.get("type", "self_hosted"),
        "session_duration": app.get("session_duration", "24h"),
        "app_launcher_visible": app.get("app_launcher_visible", True),
        "policies": [{"id": policy_id, "precedence": 1}],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to update Access app %s in Cloudflare: %s", app_id, e)
        raise ValueError("Failed to update Access app policy in Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access app update for %s: %s", app_id, data.get("errors"))
        raise ValueError(f"Failed to update Access app: {data.get('errors')}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Cloudflare returned an invalid Access app update payload")
    return _shape_access_app(result)


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
    return [_shape_access_app(a) for a in data.get("result", [])]


async def list_access_policies() -> list[dict]:
    """List reusable CF Access policies."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/policies"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch Access policies from Cloudflare: %s", e)
        raise ValueError("Failed to fetch Access policies from Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access policy list request: %s", data.get("errors"))
        raise ValueError(f"Failed to list Access policies: {data.get('errors')}")
    return [_shape_access_policy(p) for p in data.get("result", [])]


async def create_access_policy(name: str, value: str) -> dict:
    """Create a reusable CF Access policy with an initial email/email-domain rule."""
    cfg = _require_cf_config()
    _kind, _canonical_value, rule = _normalize_access_policy_member(value)
    policy_name = (name or "").strip()
    if not policy_name:
        raise ValueError("Policy name is required")
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/policies"
    payload = {
        "name": policy_name,
        "decision": "allow",
        "include": [rule],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to create Access policy in Cloudflare: %s", e)
        raise ValueError("Failed to create Access policy in Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access policy create request: %s", data.get("errors"))
        raise ValueError(f"Failed to create Access policy: {data.get('errors')}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Cloudflare returned an invalid Access policy create payload")
    return _shape_access_policy(result)


async def delete_access_policy(policy_id: str) -> bool:
    """Delete a reusable CF Access policy by ID."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/policies/{policy_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to delete Access policy %s in Cloudflare: %s", policy_id, e)
        raise ValueError("Failed to delete Access policy in Cloudflare")
    return bool(data.get("success", False))


async def get_access_policy(policy_id: str) -> dict:
    """Fetch a single reusable CF Access policy."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/policies/{policy_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch Access policy %s from Cloudflare: %s", policy_id, e)
        raise ValueError("Failed to fetch Access policy from Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access policy read for %s: %s", policy_id, data.get("errors"))
        raise ValueError(f"Failed to read Access policy: {data.get('errors')}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Cloudflare returned an invalid Access policy payload")
    return _shape_access_policy(result)


async def update_access_policy(policy_id: str, payload: dict) -> dict:
    """Update a reusable CF Access policy."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/policies/{policy_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to update Access policy %s in Cloudflare: %s", policy_id, e)
        raise ValueError("Failed to update Access policy in Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access policy update for %s: %s", policy_id, data.get("errors"))
        raise ValueError(f"Failed to update Access policy: {data.get('errors')}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Cloudflare returned an invalid Access policy update payload")
    return _shape_access_policy(result)


async def add_access_policy_member(policy_id: str, value: str) -> dict:
    """Add an email or email-domain include rule to a reusable Access policy."""
    policy = await get_access_policy(policy_id)
    kind, canonical_value, rule = _normalize_access_policy_member(value)
    include = list(policy.get("include", []))
    existing = _extract_access_policy_members(include)
    if any(m["kind"] == kind and m["value"] == canonical_value for m in existing):
        raise ValueError(f"Policy already includes {canonical_value}")
    include.append(rule)
    return await update_access_policy(policy_id, _policy_update_payload(policy, include))


async def remove_access_policy_member(policy_id: str, value: str) -> dict:
    """Remove an email or email-domain include rule from a reusable Access policy."""
    policy = await get_access_policy(policy_id)
    kind, canonical_value, _rule = _normalize_access_policy_member(value)
    include = list(policy.get("include", []))
    updated = []
    removed = False
    for entry in include:
        member = _extract_access_policy_member(entry)
        if (
            not removed
            and member
            and member["kind"] == kind
            and member["value"] == canonical_value
        ):
            removed = True
            continue
        updated.append(entry)
    if not removed:
        raise ValueError(f"Policy does not include {canonical_value}")
    return await update_access_policy(policy_id, _policy_update_payload(policy, updated))


def _service_token_include_rules(service_token_ids: list[str]) -> list[dict]:
    ids = []
    for token_id in service_token_ids or []:
        value = str(token_id or "").strip()
        if value and value not in ids:
            ids.append(value)
    if not ids:
        raise ValueError("At least one service token is required")
    return [{"service_token": {"token_id": token_id}} for token_id in ids]


async def list_access_service_tokens() -> list[dict]:
    """List Cloudflare Access service tokens for the configured account."""
    cfg = _require_cf_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/service_tokens"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch Access service tokens from Cloudflare: %s", e)
        raise ValueError("Failed to list Access service tokens from Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access service token list request: %s", data.get("errors"))
        raise ValueError(f"Failed to list Access service tokens: {data.get('errors')}")
    return [_shape_access_service_token(item) for item in data.get("result", [])]


async def create_access_service_token(name: str, duration: str = "8760h") -> dict:
    """Create a Cloudflare Access service token. The returned client_secret is one-time only."""
    cfg = _require_cf_config()
    token_name = (name or "").strip()
    if not token_name:
        raise ValueError("Service token name is required")
    payload = {"name": token_name, "duration": (duration or "8760h").strip() or "8760h"}
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/service_tokens"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to create Access service token in Cloudflare: %s", e)
        raise ValueError("Failed to create Access service token in Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access service token create request: %s", data.get("errors"))
        raise ValueError(f"Failed to create Access service token: {data.get('errors')}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Cloudflare returned an invalid Access service token payload")
    shaped = _shape_access_service_token(result)
    shaped["client_secret"] = result.get("client_secret")
    return shaped


async def rotate_access_service_token(service_token_id: str) -> dict:
    """Rotate a Cloudflare Access service token secret. The returned client_secret is one-time only."""
    cfg = _require_cf_config()
    token_id = str(service_token_id or "").strip()
    if not token_id:
        raise ValueError("Service token ID is required")
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/service_tokens/{token_id}/rotate"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to rotate Access service token %s in Cloudflare: %s", token_id, e)
        raise ValueError("Failed to rotate Access service token in Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Access service token rotate request for %s: %s", token_id, data.get("errors"))
        raise ValueError(f"Failed to rotate Access service token: {data.get('errors')}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Cloudflare returned an invalid Access service token rotation payload")
    shaped = _shape_access_service_token(result)
    shaped["client_secret"] = result.get("client_secret")
    return shaped


async def delete_access_service_token(service_token_id: str) -> bool:
    """Delete a Cloudflare Access service token by ID."""
    cfg = _require_cf_config()
    token_id = str(service_token_id or "").strip()
    if not token_id:
        raise ValueError("Service token ID is required")
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/service_tokens/{token_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(url, headers=_cf_headers(cfg["token"]))
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to delete Access service token %s in Cloudflare: %s", token_id, e)
        raise ValueError("Failed to delete Access service token in Cloudflare")
    return bool(data.get("success", False))


async def create_service_auth_policy(name: str, service_token_ids: list[str]) -> dict:
    """Create a reusable Service Auth policy for one or more Access service tokens."""
    cfg = _require_cf_config()
    policy_name = (name or "").strip()
    if not policy_name:
        raise ValueError("Policy name is required")
    payload = {
        "name": policy_name,
        "decision": "non_identity",
        "include": _service_token_include_rules(service_token_ids),
    }
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/access/policies"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=_cf_headers(cfg["token"]), json=payload)
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        logger.warning("Failed to create Service Auth policy in Cloudflare: %s", e)
        raise ValueError("Failed to create Service Auth policy in Cloudflare")
    if not data.get("success"):
        logger.warning("Cloudflare rejected Service Auth policy create request: %s", data.get("errors"))
        raise ValueError(f"Failed to create Service Auth policy: {data.get('errors')}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Cloudflare returned an invalid Service Auth policy payload")
    return _shape_access_policy(result)


async def update_service_auth_policy_tokens(policy_id: str, service_token_ids: list[str]) -> dict:
    """Replace service-token include rules on a reusable Service Auth policy."""
    policy = await get_access_policy(policy_id)
    payload = _policy_update_payload(policy, _service_token_include_rules(service_token_ids))
    payload["decision"] = "non_identity"
    return await update_access_policy(policy_id, payload)


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
