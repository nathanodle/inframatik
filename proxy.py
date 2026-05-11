import httpx
from urllib.parse import parse_qs, urlsplit

from nodes import resolve_node
from node_config import assert_worker_address_allowed

PROXY_TIMEOUT = 10
_NO_MATCH = object()


async def proxy_to_node(node_id: str, method: str, path: str, body: dict = None) -> dict:
    """Forward an API request to a specific node.

    If node_id is the local node, calls local functions directly.
    Otherwise, proxies via HTTP to the remote node.
    """
    target = resolve_node(node_id)

    # Self-node: call local functions directly
    if target is None:
        return await _handle_local(method, path, body)

    try:
        address = assert_worker_address_allowed(target["address"])
    except ValueError as e:
        raise ValueError(f"Invalid worker address: {e}")
    api_key = target["api_key"]
    url = f"{address}{path}"
    headers = {"X-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "POST":
                headers["Content-Type"] = "application/json"
                resp = await client.post(url, headers=headers, json=body)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        raise RuntimeError(f"Node unreachable: {e}")

    if resp.status_code >= 400:
        try:
            data = resp.json()
            if isinstance(data, dict):
                detail = data.get("detail", resp.text)
            else:
                detail = str(data) or resp.text
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"Node returned {resp.status_code}: {detail}")

    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f"Node returned non-JSON response")


def _split_route(path: str):
    parsed = urlsplit(path)
    return parsed.path, parse_qs(parsed.query, keep_blank_values=False)


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    values = query.get(key)
    if not values:
        return default
    try:
        return int(values[-1])
    except (TypeError, ValueError):
        return default


def _service_name_from_route(route_path: str) -> str:
    if not route_path.startswith("/api/services/"):
        raise ValueError("Invalid service route")
    tail = route_path[len("/api/services/"):]
    name = tail.split("/", 1)[0]
    if not name:
        raise ValueError("Missing service name")
    return name


def _service_action_suffix(route_path: str):
    if not route_path.startswith("/api/services/"):
        return None
    tail = route_path[len("/api/services/"):]
    if "/" not in tail:
        return ""
    return "/" + tail.split("/", 1)[1]


async def _handle_local_services(method: str, route_path: str, query: dict[str, list[str]], body: dict = None):
    if route_path == "/api/services":
        from services import list_services, register_service

        if method == "GET":
            return await list_services()
        if method == "POST":
            payload = body or {}
            for field in ("name", "command", "working_dir"):
                if field not in payload:
                    raise ValueError(f"Missing required field: {field}")
            return await register_service(
                name=payload["name"],
                command=payload["command"],
                working_dir=payload["working_dir"],
                hostname=payload.get("hostname"),
                access_policy_id=payload.get("access_policy_id"),
                lan=payload.get("lan", False),
            )
        return _NO_MATCH

    if not route_path.startswith("/api/services/"):
        return _NO_MATCH

    from services import (
        deregister_service,
        start_service,
        stop_service,
        restart_service,
        get_service_logs,
    )

    name = _service_name_from_route(route_path)
    suffix = _service_action_suffix(route_path)

    if method == "DELETE" and suffix == "":
        svc = await deregister_service(name)
        return {"deleted": name, **svc}

    if method == "POST" and suffix in ("/start", "/stop", "/restart"):
        if suffix == "/start":
            status = await start_service(name)
        elif suffix == "/stop":
            status = await stop_service(name)
        else:
            status = await restart_service(name)
        return {"name": name, "status": status}

    if method == "GET" and suffix == "/logs":
        lines = _query_int(query, "lines", 100)
        logs = await get_service_logs(name, lines=lines)
        return {"name": name, "logs": logs}

    return _NO_MATCH


async def _handle_local_cf_service(
    method: str,
    route_path: str,
    query: dict[str, list[str]],
    body: dict = None,
):
    if not route_path.startswith("/api/internal/cf/service/"):
        return _NO_MATCH

    from cloudflared import (
        get_cloudflared_user_service_status,
        get_cloudflared_user_service_logs,
        restart_cloudflared_user_service,
        update_cloudflared_user_binary,
    )

    if route_path == "/api/internal/cf/service/status" and method == "GET":
        return await get_cloudflared_user_service_status()

    if route_path == "/api/internal/cf/service/logs" and method == "GET":
        lines = _query_int(query, "lines", 80)
        logs = await get_cloudflared_user_service_logs(lines=lines)
        return {"lines": lines, "logs": logs}

    if route_path == "/api/internal/cf/service/restart" and method == "POST":
        service = await restart_cloudflared_user_service()
        return {"status": "restarted", "service": service}

    if route_path == "/api/internal/cf/service/update" and method == "POST":
        payload = body if isinstance(body, dict) else {}
        version = payload.get("version")
        result = await update_cloudflared_user_binary(version=version)
        return {"status": "updated", "cloudflared": result}

    return _NO_MATCH


async def _handle_local(method: str, path: str, body: dict = None):
    """Handle a proxied request locally by calling the appropriate Python functions."""
    route_path, query = _split_route(path)

    from system import get_system_metrics
    from tunnel import get_tunnel_status, get_tunnel_routes

    if route_path == "/api/system" and method == "GET":
        return get_system_metrics()

    service_response = await _handle_local_services(method, route_path, query, body)
    if service_response is not _NO_MATCH:
        return service_response

    if route_path == "/api/tunnel" and method == "GET":
        status = await get_tunnel_status()
        try:
            status["routes"] = await get_tunnel_routes()
        except ValueError as e:
            status["routes"] = []
            status["routes_error"] = str(e)
        return status

    cf_response = await _handle_local_cf_service(method, route_path, query, body)
    if cf_response is not _NO_MATCH:
        return cf_response

    raise ValueError(f"Unknown local route: {method} {path}")
