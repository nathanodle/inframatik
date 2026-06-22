"""MCP streamable HTTP server — exposes service and inference management.

Implements the MCP JSON-RPC protocol directly over HTTP. No external MCP SDK needed.
Auth uses scoped service tokens (svc_...) and scoped MCP tokens (mcp_...)
validated by the main auth middleware.
"""

import json
import logging
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, Response

from node_config import service_token_capability_allows
from nodes import get_all_nodes
from proxy import proxy_to_node
from services import (
    list_services,
    register_service,
    start_service,
    stop_service,
    restart_service,
    get_service_logs,
)

mcp_router = APIRouter()
logger = logging.getLogger("inframatik.mcp")

PROTOCOL_VERSION = "2025-03-26"

SERVER_INFO = {
    "name": "inframatik",
    "version": "1.0.0",
}

TOOLS = [
    {
        "name": "register",
        "description": (
            "Register the service and start it. If already registered, just starts it. "
            ".inframatik is a secret config file, not a runtime data directory; do not store "
            "SQLite databases, uploads, caches, or other app data under .inframatik."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The start command (e.g. 'uvicorn main:app --host 127.0.0.1'). "
                        "If setting data-directory env vars, use the app's documented data "
                        "directory or platform default, never .inframatik or .inframatik/."
                    ),
                },
                "working_dir": {
                    "type": "string",
                    "description": "Absolute path to the working directory, usually the app or repo root.",
                },
                "hostname": {
                    "type": "string",
                    "description": "Optional CF hostname for public access (e.g. 'myapp.example.com')",
                },
                "access_policy_id": {
                    "type": "string",
                    "description": "Optional reusable Cloudflare Access policy ID to attach when hostname is set",
                },
                "lan": {
                    "type": "boolean",
                    "description": "If true, bind service host to 0.0.0.0 instead of 127.0.0.1",
                    "default": False,
                },
            },
            "required": ["command", "working_dir"],
        },
    },
    {
        "name": "restart",
        "description": "Restart the service.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop",
        "description": "Stop the service.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "logs",
        "description": "Get recent service logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of log lines to return (default 100)",
                    "default": 100,
                },
            },
        },
    },
    {
        "name": "status",
        "description": "Get the service status and details.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

INFERENCE_TOOLS = [
    {
        "name": "validate_inference_profile",
        "description": "Dry-run validate and plan an inference profile draft without side effects.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile": {"type": "object"}}},
    },
    {
        "name": "render_inference_command",
        "description": "Render redacted command and systemd preview for an inference profile draft.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile": {"type": "object"}}},
    },
    {
        "name": "estimate_inference_fit",
        "description": "Best-effort GPU memory summary plus planner output for a profile draft.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile": {"type": "object"}}},
    },
    {
        "name": "render_inference_client_bundle",
        "description": "Render curl, Python OpenAI SDK, and LiteLLM examples for a saved profile.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "bundle": {"type": "object"}}, "required": ["profile_id"]},
    },
    {
        "name": "get_inference_operation",
        "description": "Read one inference operation status/result.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "operation_id": {"type": "string"}}, "required": ["operation_id"]},
    },
    {
        "name": "create_inference_profile",
        "description": "Create a stopped node-local inference profile.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile": {"type": "object"}}, "required": ["profile"]},
    },
    {
        "name": "update_inference_profile",
        "description": "Patch an existing inference profile.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "profile": {"type": "object"}}, "required": ["profile_id", "profile"]},
    },
    {
        "name": "delete_inference_profile",
        "description": "Delete a stopped inference profile and clean up owned metadata.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "force": {"type": "boolean"}}, "required": ["profile_id"]},
    },
    {
        "name": "save_inference_client_bundle",
        "description": "Save named client bundle metadata without raw secrets.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "bundle": {"type": "object"}}, "required": ["profile_id", "bundle"]},
    },
    {
        "name": "delete_inference_client_bundle",
        "description": "Delete saved client bundle metadata.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "bundle_id": {"type": "string"}}, "required": ["profile_id", "bundle_id"]},
    },
    {
        "name": "create_inference_launcher",
        "description": "Create a node-local engine launcher.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "launcher": {"type": "object"}}, "required": ["launcher"]},
    },
    {
        "name": "update_inference_launcher",
        "description": "Update a node-local engine launcher.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "launcher_id": {"type": "string"}, "launcher": {"type": "object"}}, "required": ["launcher_id", "launcher"]},
    },
    {
        "name": "delete_inference_launcher",
        "description": "Delete launcher metadata when reference checks pass.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "launcher_id": {"type": "string"}, "force_stopped_references": {"type": "boolean"}}, "required": ["launcher_id"]},
    },
    {
        "name": "rotate_inference_api_key",
        "description": "Generate or rotate a profile engine API key. Raw key is returned once.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "render_bundle": {"type": "boolean"}}, "required": ["profile_id"]},
    },
    {
        "name": "disable_inference_api_key",
        "description": "Disable a profile engine API key.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}}, "required": ["profile_id"]},
    },
    {
        "name": "generate_cloudflare_service_token",
        "description": "Generate and attach a Cloudflare Access service token. Raw secret is returned once.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "name": {"type": "string"}, "render_bundle": {"type": "boolean"}}, "required": ["profile_id"]},
    },
    {
        "name": "rotate_cloudflare_service_token",
        "description": "Rotate an attached Cloudflare Access service token. Raw secret is returned once.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "token_id": {"type": "string"}, "render_bundle": {"type": "boolean"}}, "required": ["profile_id", "token_id"]},
    },
    {
        "name": "retire_cloudflare_service_token",
        "description": "Retire a Cloudflare Access service token from a profile policy.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}, "token_id": {"type": "string"}, "delete_if_owned": {"type": "boolean"}}, "required": ["profile_id", "token_id"]},
    },
    {
        "name": "start_inference_profile",
        "description": "Start all resolved instances for a profile and return the operation record.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}}, "required": ["profile_id"]},
    },
    {
        "name": "stop_inference_profile",
        "description": "Stop all resolved instances for a profile and return the operation record.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}}, "required": ["profile_id"]},
    },
    {
        "name": "restart_inference_profile",
        "description": "Restart all resolved instances for a profile and return the operation record.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "profile_id": {"type": "string"}}, "required": ["profile_id"]},
    },
    {
        "name": "resolve_model_source",
        "description": "Inspect a local/HF/direct model source using the same resolver as REST.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "source": {"type": "object"}}, "required": ["source"]},
    },
    {
        "name": "download_model",
        "description": "Start a model download or local import job. Never creates or starts profiles.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "source": {"type": "object"}, "path": {"type": "string"}, "artifact_id": {"type": "string"}, "display_name": {"type": "string"}, "snapshot": {"type": "string"}, "metadata": {"type": "object"}}, "required": ["artifact_id"]},
    },
    {
        "name": "verify_model",
        "description": "Verify a local model artifact or snapshot.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "artifact_id": {"type": "string"}, "snapshot": {"type": "string"}}, "required": ["artifact_id"]},
    },
    {
        "name": "delete_model",
        "description": "Delete a model artifact or snapshot when reference checks pass.",
        "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}, "artifact_id": {"type": "string"}, "snapshot": {"type": "string"}, "force_stopped_references": {"type": "boolean"}}, "required": ["artifact_id"]},
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _jsonrpc_result(req_id, result):
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _jsonrpc_error(req_id, code, message):
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


def _jsonrpc_notification_response():
    return Response(status_code=202)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _tool_register(service_name: str, args: dict) -> dict:
    command = args.get("command", "")
    working_dir = args.get("working_dir", ".")
    hostname = args.get("hostname")
    access_policy_id = args.get("access_policy_id")
    lan = args.get("lan", False)
    if not isinstance(lan, bool):
        raise ValueError("lan must be a boolean")
    if access_policy_id is not None and not isinstance(access_policy_id, str):
        raise ValueError("access_policy_id must be a string")

    services = await list_services()
    existing = next((s for s in services if s.get("name") == service_name), None)
    if existing:
        # Already registered — just start if not running
        if existing.get("status") != "active":
            status = await start_service(service_name)
            return {"text": f"Service '{service_name}' started (was already registered). Status: {status}"}
        return {"text": f"Service '{service_name}' is already running."}

    svc = await register_service(
        name=service_name,
        command=command,
        working_dir=working_dir,
        hostname=hostname,
        access_policy_id=access_policy_id,
        lan=lan,
    )
    status = await start_service(service_name)
    port = svc.get("port", "?")
    result = f"Service '{service_name}' registered on port {port} and started. Status: {status}"
    if hostname:
        result += f"\nPublic URL: https://{hostname}"
    return {"text": result}


async def _tool_restart(service_name: str, args: dict) -> dict:
    status = await restart_service(service_name)
    return {"text": f"Service '{service_name}' restarted. Status: {status}"}


async def _tool_stop(service_name: str, args: dict) -> dict:
    status = await stop_service(service_name)
    return {"text": f"Service '{service_name}' stopped. Status: {status}"}


async def _tool_logs(service_name: str, args: dict) -> dict:
    lines = args.get("lines", 100)
    logs = await get_service_logs(service_name, lines=lines)
    return {"text": logs}


async def _tool_status(service_name: str, args: dict) -> dict:
    services = await list_services()
    svc = next((s for s in services if s.get("name") == service_name), None)
    if not svc:
        return {"text": f"Service '{service_name}' is not registered."}
    return {
        "text": (
            f"Service: {svc['name']}\n"
            f"Status: {svc.get('status', 'unknown')}\n"
            f"Port: {svc.get('port', '?')}\n"
            f"Command: {svc.get('command', '?')}\n"
            f"Working Dir: {svc.get('working_dir', '?')}"
        )
    }


_TOOL_HANDLERS = {
    "register": _tool_register,
    "restart": _tool_restart,
    "stop": _tool_stop,
    "logs": _tool_logs,
    "status": _tool_status,
}

_TOOL_REQUIRED_CAPABILITY = {
    "register": "deploy",
    "restart": "operate",
    "stop": "operate",
    "logs": "read",
    "status": "read",
}

_INFERENCE_TOOL_REQUIRED_SCOPE = {
    "validate_inference_profile": "mcp:inference:render",
    "render_inference_command": "mcp:inference:render",
    "estimate_inference_fit": "mcp:inference:render",
    "render_inference_client_bundle": "mcp:inference:render",
    "get_inference_operation": "mcp:read",
    "create_inference_profile": "mcp:inference:write",
    "update_inference_profile": "mcp:inference:write",
    "delete_inference_profile": "mcp:inference:write",
    "save_inference_client_bundle": "mcp:inference:write",
    "delete_inference_client_bundle": "mcp:inference:write",
    "create_inference_launcher": "mcp:inference:write",
    "update_inference_launcher": "mcp:inference:write",
    "delete_inference_launcher": "mcp:inference:write",
    "rotate_inference_api_key": "mcp:inference:write",
    "disable_inference_api_key": "mcp:inference:write",
    "generate_cloudflare_service_token": "mcp:inference:write",
    "rotate_cloudflare_service_token": "mcp:inference:write",
    "retire_cloudflare_service_token": "mcp:inference:write",
    "start_inference_profile": "mcp:inference:lifecycle",
    "stop_inference_profile": "mcp:inference:lifecycle",
    "restart_inference_profile": "mcp:inference:lifecycle",
    "resolve_model_source": "mcp:model:read",
    "download_model": "mcp:model:download",
    "verify_model": "mcp:model:write",
    "delete_model": "mcp:model:write",
}

_MCP_SCOPE_IMPLICATIONS = {
    "mcp:logs": {"mcp:read"},
    "mcp:inference:render": {"mcp:read"},
    "mcp:inference:write": {"mcp:read", "mcp:inference:render"},
    "mcp:inference:lifecycle": {"mcp:read"},
    "mcp:model:read": {"mcp:read"},
    "mcp:model:download": {"mcp:read", "mcp:model:read"},
    "mcp:model:write": {"mcp:read", "mcp:model:read"},
}


def _mcp_auth_from_request(request: Request) -> dict:
    return {
        "scopes": list(getattr(request.state, "mcp_scopes", None) or []),
        "node_ids": list(getattr(request.state, "mcp_node_ids", None) or []),
        "profile_ids": list(getattr(request.state, "mcp_profile_ids", None) or []),
        "token_id": getattr(request.state, "mcp_token_id", None),
    }


def _mcp_scope_allows(scopes: list[str], required: str) -> bool:
    if required in scopes:
        return True
    for scope in scopes:
        if required in _MCP_SCOPE_IMPLICATIONS.get(scope, set()):
            return True
    return False


def _require_mcp_scope(auth: dict, required: str):
    if not _mcp_scope_allows(auth.get("scopes") or [], required):
        raise PermissionError(f"MCP scope '{required}' required")


def _profile_arg(args: dict) -> str:
    profile_id = str(args.get("profile_id") or "").strip()
    if not profile_id:
        raise ValueError("profile_id is required")
    return profile_id


def _check_profile_scope(auth: dict, profile_id: str):
    allowed = auth.get("profile_ids") or []
    if allowed and profile_id not in allowed:
        raise PermissionError(f"MCP token is not allowed to access profile '{profile_id}'")


def _target_node_id(auth: dict, args: dict) -> str:
    node_id = str(args.get("node_id") or "").strip()
    allowed = auth.get("node_ids") or []
    if not node_id and len(allowed) == 1:
        node_id = allowed[0]
    if not node_id:
        from node_config import get_node_config

        config = get_node_config() or {}
        node_id = config.get("node_id") or "local"
    if allowed and node_id not in allowed:
        raise PermissionError(f"MCP token is not allowed to access node '{node_id}'")
    return node_id


async def _node_call(auth: dict, args: dict, method: str, path: str, body: dict = None) -> dict:
    node_id = _target_node_id(auth, args)
    return await proxy_to_node(node_id, method, path, body)


def _json_text(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _resource_result(uri: str, data: dict):
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": _json_text(data),
            }
        ]
    }


# ---------------------------------------------------------------------------
# MCP method handlers
# ---------------------------------------------------------------------------

def _resource_uri(uri: str, name: str):
    return {"uri": uri, "name": name, "mimeType": "application/json"}


async def _list_mcp_resources(auth: dict) -> dict:
    _require_mcp_scope(auth, "mcp:read")
    resources = [_resource_uri("inframatik://nodes", "Nodes")]
    allowed_nodes = auth.get("node_ids") or []
    node_ids = allowed_nodes
    if not node_ids:
        try:
            node_ids = [node["node_id"] for node in await get_all_nodes()]
        except Exception:
            from node_config import get_node_config

            config = get_node_config() or {}
            node_ids = [config.get("node_id") or "local"]
    for node_id in node_ids:
        resources.extend([
            _resource_uri(f"inframatik://node/{node_id}/hardware", f"{node_id} hardware"),
            _resource_uri(f"inframatik://node/{node_id}/system/metrics", f"{node_id} system metrics"),
            _resource_uri(f"inframatik://node/{node_id}/models", f"{node_id} models"),
            _resource_uri(f"inframatik://node/{node_id}/inference/launchers", f"{node_id} inference launchers"),
            _resource_uri(f"inframatik://node/{node_id}/inference/profiles", f"{node_id} inference profiles"),
            _resource_uri(f"inframatik://node/{node_id}/inference/operations", f"{node_id} inference operations"),
        ])
        try:
            profiles = await proxy_to_node(node_id, "GET", "/api/inference/profiles")
            for profile in profiles.get("profiles", []):
                profile_id = profile.get("id")
                if not profile_id:
                    continue
                if auth.get("profile_ids") and profile_id not in auth["profile_ids"]:
                    continue
                resources.extend([
                    _resource_uri(
                        f"inframatik://node/{node_id}/inference/profile/{profile_id}",
                        f"{node_id} profile {profile_id}",
                    ),
                    _resource_uri(
                        f"inframatik://node/{node_id}/inference/profile/{profile_id}/client-bundles",
                        f"{node_id} profile {profile_id} client bundles",
                    ),
                    _resource_uri(
                        f"inframatik://node/{node_id}/inference/logs/{profile_id}",
                        f"{node_id} profile {profile_id} logs",
                    ),
                ])
        except Exception:
            continue
    return {"resources": resources}


def _parse_resource_uri(uri: str) -> tuple[str, list[str], dict]:
    parsed = urlparse(uri)
    if parsed.scheme != "inframatik":
        raise ValueError("Unsupported resource scheme")
    query = parse_qs(parsed.query, keep_blank_values=False)
    if parsed.netloc == "nodes":
        return "nodes", [], query
    if parsed.netloc != "node":
        raise ValueError("Unsupported resource authority")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts:
        raise ValueError("Node resource missing node ID")
    return "node", parts, query


async def _read_mcp_resource(auth: dict, uri: str) -> dict:
    authority, parts, query = _parse_resource_uri(uri)
    if authority == "nodes":
        _require_mcp_scope(auth, "mcp:read")
        return _resource_result(uri, {"nodes": await get_all_nodes()})
    node_id = parts[0]
    _target_node_id(auth, {"node_id": node_id})
    tail = parts[1:]
    if tail == ["hardware"] or tail == ["system", "metrics"]:
        _require_mcp_scope(auth, "mcp:read")
        return _resource_result(uri, await proxy_to_node(node_id, "GET", "/api/system"))
    if tail == ["models"]:
        _require_mcp_scope(auth, "mcp:model:read")
        return _resource_result(uri, await proxy_to_node(node_id, "GET", "/api/models"))
    if tail == ["inference", "launchers"]:
        _require_mcp_scope(auth, "mcp:read")
        return _resource_result(uri, await proxy_to_node(node_id, "GET", "/api/inference/launchers"))
    if tail == ["inference", "profiles"]:
        _require_mcp_scope(auth, "mcp:read")
        return _resource_result(uri, await proxy_to_node(node_id, "GET", "/api/inference/profiles"))
    if tail == ["inference", "operations"]:
        _require_mcp_scope(auth, "mcp:read")
        return _resource_result(uri, await proxy_to_node(node_id, "GET", "/api/inference/operations"))
    if len(tail) >= 3 and tail[0] == "inference" and tail[1] == "profile":
        profile_id = tail[2]
        _check_profile_scope(auth, profile_id)
        if len(tail) == 3:
            _require_mcp_scope(auth, "mcp:read")
            profile = await proxy_to_node(node_id, "GET", f"/api/inference/profiles/{profile_id}")
            rendered = await proxy_to_node(node_id, "POST", f"/api/inference/profiles/{profile_id}/render")
            health = await proxy_to_node(node_id, "GET", f"/api/inference/profiles/{profile_id}/health")
            return _resource_result(uri, {"profile": profile, "render": rendered, "health": health})
        if tail[3] == "client-bundles":
            _require_mcp_scope(auth, "mcp:read")
            return _resource_result(uri, await proxy_to_node(node_id, "GET", f"/api/inference/profiles/{profile_id}/client-bundles"))
    if len(tail) >= 3 and tail[:2] == ["inference", "logs"]:
        _require_mcp_scope(auth, "mcp:logs")
        profile_id = tail[2]
        _check_profile_scope(auth, profile_id)
        lines = 100
        if query.get("lines"):
            try:
                lines = min(500, max(1, int(query["lines"][-1])))
            except ValueError:
                lines = 100
        return _resource_result(uri, await proxy_to_node(node_id, "GET", f"/api/inference/profiles/{profile_id}/logs?lines={lines}"))
    raise ValueError(f"Unknown resource URI: {uri}")


def _handle_mcp_protocol_method(req_id, method: str, token_capability: str, mcp_auth: dict = None):
    if method == "initialize":
        capabilities = {"tools": {}}
        if mcp_auth and mcp_auth.get("scopes"):
            capabilities["resources"] = {}
        return _jsonrpc_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": capabilities,
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized":
        return _jsonrpc_notification_response()
    if method == "tools/list":
        allowed_tools = []
        if mcp_auth and mcp_auth.get("scopes"):
            for tool in INFERENCE_TOOLS:
                required = _INFERENCE_TOOL_REQUIRED_SCOPE.get(tool["name"], "mcp:read")
                if _mcp_scope_allows(mcp_auth["scopes"], required):
                    allowed_tools.append(tool)
        else:
            for tool in TOOLS:
                required = _TOOL_REQUIRED_CAPABILITY.get(tool["name"], "deploy")
                if service_token_capability_allows(token_capability, required):
                    allowed_tools.append(tool)
        return _jsonrpc_result(req_id, {"tools": allowed_tools})
    return None


async def _handle_mcp_tool_call(req_id, scope: str, token_capability: str, params: dict):
    if not isinstance(params, dict):
        return _jsonrpc_error(req_id, -32602, "Invalid params")

    tool_name = params.get("name")
    tool_args = params.get("arguments", {})
    if tool_args is None:
        tool_args = {}
    if not isinstance(tool_args, dict):
        return _jsonrpc_error(req_id, -32602, "Invalid params: arguments must be an object")

    handler = _TOOL_HANDLERS.get(tool_name)
    if not handler:
        return _jsonrpc_error(req_id, -32602, f"Unknown tool: {tool_name}")

    required = _TOOL_REQUIRED_CAPABILITY.get(tool_name, "deploy")
    if not service_token_capability_allows(token_capability, required):
        return _jsonrpc_error(
            req_id,
            -32603,
            f"Tool '{tool_name}' requires '{required}' capability",
        )

    try:
        result = await handler(scope, tool_args)
        return _jsonrpc_result(req_id, {
            "content": [{"type": "text", "text": result.get("text", "")}],
        })
    except (ValueError, RuntimeError, TypeError, OSError) as e:
        logger.debug("MCP tool call failed: tool=%s error=%s", tool_name, e)
        return _jsonrpc_result(req_id, {
            "content": [{"type": "text", "text": f"Error: {e}"}],
            "isError": True,
        })


def _tool_json_result(req_id, result: dict):
    return _jsonrpc_result(req_id, {
        "content": [{"type": "text", "text": _json_text(result)}],
    })


def _tool_error_result(req_id, message: str):
    return _jsonrpc_result(req_id, {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "isError": True,
    })


async def _handle_mcp_inference_tool_call(req_id, auth: dict, params: dict):
    if not isinstance(params, dict):
        return _jsonrpc_error(req_id, -32602, "Invalid params")
    tool_name = params.get("name")
    args = params.get("arguments", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _jsonrpc_error(req_id, -32602, "Invalid params: arguments must be an object")
    required = _INFERENCE_TOOL_REQUIRED_SCOPE.get(tool_name)
    if not required:
        return _jsonrpc_error(req_id, -32602, f"Unknown tool: {tool_name}")
    try:
        _require_mcp_scope(auth, required)
        result = await _run_mcp_inference_tool(auth, tool_name, args)
        return _tool_json_result(req_id, result)
    except (PermissionError, ValueError, RuntimeError, TypeError, OSError) as e:
        logger.debug("MCP inference tool failed: tool=%s error=%s", tool_name, e)
        return _tool_error_result(req_id, str(e))


async def _run_mcp_inference_tool(auth: dict, tool_name: str, args: dict) -> dict:
    if tool_name in ("validate_inference_profile", "render_inference_command"):
        profile = args.get("profile") if isinstance(args.get("profile"), dict) else args
        return await _node_call(auth, args, "POST", "/api/inference/profiles/preview", profile)

    if tool_name == "estimate_inference_fit":
        profile = args.get("profile") if isinstance(args.get("profile"), dict) else args
        preview = await _node_call(auth, args, "POST", "/api/inference/profiles/preview", profile)
        metrics = await _node_call(auth, args, "GET", "/api/system")
        gpus = metrics.get("gpus") or []
        return {
            "preview": preview,
            "gpu_summary": [
                {
                    "index": gpu.get("index"),
                    "name": gpu.get("name"),
                    "mem_total_mb": gpu.get("mem_total_mb"),
                    "mem_used_mb": gpu.get("mem_used_mb"),
                    "mem_free_mb": (gpu.get("mem_total_mb") or 0) - (gpu.get("mem_used_mb") or 0),
                }
                for gpu in gpus
            ],
        }

    if tool_name == "render_inference_client_bundle":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        return await _node_call(
            auth,
            args,
            "POST",
            f"/api/inference/profiles/{profile_id}/client-bundles/render",
            args.get("bundle") or {},
        )

    if tool_name == "get_inference_operation":
        operation_id = str(args.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        return await _node_call(auth, args, "GET", f"/api/inference/operations/{operation_id}")

    if tool_name == "create_inference_profile":
        profile = args.get("profile") or {}
        if auth.get("profile_ids"):
            _check_profile_scope(auth, str(profile.get("id") or "").strip())
        return await _node_call(auth, args, "POST", "/api/inference/profiles", profile)

    if tool_name == "update_inference_profile":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        return await _node_call(auth, args, "PUT", f"/api/inference/profiles/{profile_id}", args.get("profile") or {})

    if tool_name == "delete_inference_profile":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        suffix = "?force=true" if args.get("force") else ""
        return await _node_call(auth, args, "DELETE", f"/api/inference/profiles/{profile_id}{suffix}")

    if tool_name == "save_inference_client_bundle":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        return await _node_call(auth, args, "POST", f"/api/inference/profiles/{profile_id}/client-bundles", args.get("bundle") or {})

    if tool_name == "delete_inference_client_bundle":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        bundle_id = str(args.get("bundle_id") or "").strip()
        if not bundle_id:
            raise ValueError("bundle_id is required")
        return await _node_call(auth, args, "DELETE", f"/api/inference/profiles/{profile_id}/client-bundles/{bundle_id}")

    if tool_name == "create_inference_launcher":
        return await _node_call(auth, args, "POST", "/api/inference/launchers", args.get("launcher") or {})

    if tool_name == "update_inference_launcher":
        launcher_id = str(args.get("launcher_id") or "").strip()
        if not launcher_id:
            raise ValueError("launcher_id is required")
        return await _node_call(auth, args, "PUT", f"/api/inference/launchers/{launcher_id}", args.get("launcher") or {})

    if tool_name == "delete_inference_launcher":
        launcher_id = str(args.get("launcher_id") or "").strip()
        if not launcher_id:
            raise ValueError("launcher_id is required")
        suffix = "?force_stopped_references=true" if args.get("force_stopped_references") else ""
        return await _node_call(auth, args, "DELETE", f"/api/inference/launchers/{launcher_id}{suffix}")

    if tool_name == "rotate_inference_api_key":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        return await _node_call(auth, args, "POST", f"/api/inference/profiles/{profile_id}/api-key", {"render_bundle": bool(args.get("render_bundle"))})

    if tool_name == "disable_inference_api_key":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        return await _node_call(auth, args, "DELETE", f"/api/inference/profiles/{profile_id}/api-key")

    if tool_name == "generate_cloudflare_service_token":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        return await _node_call(auth, args, "POST", f"/api/inference/profiles/{profile_id}/cloudflare/service-tokens", args)

    if tool_name == "rotate_cloudflare_service_token":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        token_id = str(args.get("token_id") or "").strip()
        if not token_id:
            raise ValueError("token_id is required")
        return await _node_call(auth, args, "POST", f"/api/inference/profiles/{profile_id}/cloudflare/service-tokens/{token_id}/rotate", args)

    if tool_name == "retire_cloudflare_service_token":
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        token_id = str(args.get("token_id") or "").strip()
        if not token_id:
            raise ValueError("token_id is required")
        suffix = "?delete_if_owned=true" if args.get("delete_if_owned") else ""
        return await _node_call(auth, args, "DELETE", f"/api/inference/profiles/{profile_id}/cloudflare/service-tokens/{token_id}{suffix}")

    if tool_name in ("start_inference_profile", "stop_inference_profile", "restart_inference_profile"):
        profile_id = _profile_arg(args)
        _check_profile_scope(auth, profile_id)
        action = tool_name.split("_", 1)[0]
        result = await _node_call(auth, args, "POST", f"/api/inference/profiles/{profile_id}/{action}")
        return {"operation": result, "poll": {"tool": "get_inference_operation", "operation_id": result.get("id")}}

    if tool_name == "resolve_model_source":
        return await _node_call(auth, args, "POST", "/api/models/resolve", {"source": args.get("source") or {}})

    if tool_name == "download_model":
        source = args.get("source") if isinstance(args.get("source"), dict) else {}
        body = {
            "source": source,
            "artifact_id": args.get("artifact_id"),
            "display_name": args.get("display_name"),
            "snapshot": args.get("snapshot"),
            "metadata": args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
        }
        if args.get("path") or source.get("type") == "local":
            return await _node_call(auth, args, "POST", "/api/models/import", {**body, "path": args.get("path") or source.get("path")})
        return await _node_call(auth, args, "POST", "/api/models/download", body)

    if tool_name == "verify_model":
        artifact_id = str(args.get("artifact_id") or "").strip()
        if not artifact_id:
            raise ValueError("artifact_id is required")
        suffix = f"?snapshot={args['snapshot']}" if args.get("snapshot") else ""
        return await _node_call(auth, args, "POST", f"/api/models/{artifact_id}/verify{suffix}")

    if tool_name == "delete_model":
        artifact_id = str(args.get("artifact_id") or "").strip()
        if not artifact_id:
            raise ValueError("artifact_id is required")
        params = []
        if args.get("snapshot"):
            params.append(f"snapshot={args['snapshot']}")
        if args.get("force_stopped_references"):
            params.append("force_stopped_references=true")
        suffix = "?" + "&".join(params) if params else ""
        return await _node_call(auth, args, "DELETE", f"/api/models/{artifact_id}{suffix}")

    raise ValueError(f"Unknown tool: {tool_name}")


# ---------------------------------------------------------------------------
# MCP endpoint
# ---------------------------------------------------------------------------

@mcp_router.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP streamable HTTP endpoint. Requires a scoped service or MCP token."""
    scope = getattr(request.state, "service_scope", None)
    mcp_auth = _mcp_auth_from_request(request)
    has_mcp_token = bool(mcp_auth.get("scopes"))
    if not scope and not has_mcp_token:
        raise HTTPException(403, "Service or MCP token required for MCP endpoint")
    token_capability = getattr(request.state, "service_capability", "deploy")

    try:
        body = await request.json()
    except ValueError:
        logger.debug("MCP parse error on request body")
        return _jsonrpc_error(None, -32700, "Parse error")
    if not isinstance(body, dict):
        return _jsonrpc_error(None, -32600, "Invalid request: body must be an object")

    req_id = body.get("id")
    method = body.get("method")

    if not isinstance(method, str) or not method:
        return _jsonrpc_error(req_id, -32600, "Invalid request: missing method")

    protocol_response = _handle_mcp_protocol_method(req_id, method, token_capability, mcp_auth if has_mcp_token else None)
    if protocol_response is not None:
        return protocol_response

    if has_mcp_token and method == "resources/list":
        try:
            return _jsonrpc_result(req_id, await _list_mcp_resources(mcp_auth))
        except (PermissionError, ValueError, RuntimeError, TypeError, OSError) as e:
            return _jsonrpc_error(req_id, -32603, str(e))

    if has_mcp_token and method == "resources/read":
        params = body.get("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("uri"), str):
            return _jsonrpc_error(req_id, -32602, "Invalid params: uri is required")
        try:
            return _jsonrpc_result(req_id, await _read_mcp_resource(mcp_auth, params["uri"]))
        except (PermissionError, ValueError, RuntimeError, TypeError, OSError) as e:
            return _jsonrpc_error(req_id, -32603, str(e))

    if method == "tools/call":
        params = body.get("params", {})
        if has_mcp_token:
            return await _handle_mcp_inference_tool_call(req_id, mcp_auth, params)
        if not scope:
            raise HTTPException(403, "Service token required for service MCP tools")
        return await _handle_mcp_tool_call(req_id, scope, token_capability, params)

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")
