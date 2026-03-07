import httpx

from nodes import resolve_node

PROXY_TIMEOUT = 10


async def proxy_to_node(node_id: str, method: str, path: str, body: dict = None) -> dict:
    """Forward an API request to a specific node.

    If node_id is the local node, calls local functions directly.
    Otherwise, proxies via HTTP to the remote node.
    """
    target = resolve_node(node_id)

    # Self-node: call local functions directly
    if target is None:
        return await _handle_local(method, path, body)

    address = target["address"]
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
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Node returned {resp.status_code}: {detail}")

    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"Node returned non-JSON response")


def _service_name_from_path(path: str) -> str:
    return path.split("/api/services/")[1].split("/")[0]


async def _handle_local(method: str, path: str, body: dict = None):
    """Handle a proxied request locally by calling the appropriate Python functions."""
    from system import get_system_metrics
    from services import (
        list_services,
        register_service,
        deregister_service,
        start_service,
        stop_service,
        restart_service,
        get_service_logs,
    )
    from tunnel import get_tunnel_status, get_tunnel_routes

    if path == "/api/system" and method == "GET":
        return get_system_metrics()

    if path == "/api/services" and method == "GET":
        return await list_services()

    if path == "/api/services" and method == "POST":
        for field in ("name", "command", "working_dir"):
            if field not in body:
                raise ValueError(f"Missing required field: {field}")
        svc = await register_service(
            name=body["name"],
            command=body["command"],
            working_dir=body["working_dir"],
            hostname=body.get("hostname"),
            lan=body.get("lan", False),
        )
        return svc

    if path.startswith("/api/services/") and method == "DELETE":
        name = _service_name_from_path(path)
        svc = await deregister_service(name)
        return {"deleted": name, **svc}

    if path.startswith("/api/services/") and path.endswith("/start") and method == "POST":
        name = _service_name_from_path(path)
        status = await start_service(name)
        return {"name": name, "status": status}

    if path.startswith("/api/services/") and path.endswith("/stop") and method == "POST":
        name = _service_name_from_path(path)
        status = await stop_service(name)
        return {"name": name, "status": status}

    if path.startswith("/api/services/") and path.endswith("/restart") and method == "POST":
        name = _service_name_from_path(path)
        status = await restart_service(name)
        return {"name": name, "status": status}

    if path.startswith("/api/services/") and path.endswith("/logs") and method == "GET":
        name = _service_name_from_path(path)
        logs = await get_service_logs(name)
        return {"name": name, "logs": logs}

    if path == "/api/tunnel" and method == "GET":
        status = await get_tunnel_status()
        status["routes"] = await get_tunnel_routes()
        return status

    raise ValueError(f"Unknown local route: {method} {path}")
