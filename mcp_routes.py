"""MCP streamable HTTP server — exposes service management as MCP tools.

Implements the MCP JSON-RPC protocol directly over HTTP. No external MCP SDK needed.
Auth uses scoped service tokens (svc_...) validated by the main auth middleware.
"""

import logging

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, Response

from node_config import service_token_capability_allows
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
        "description": "Register the service and start it. If already registered, just starts it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The start command (e.g. 'uvicorn main:app --host 127.0.0.1')",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Absolute path to the working directory",
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


# ---------------------------------------------------------------------------
# MCP method handlers
# ---------------------------------------------------------------------------

def _handle_mcp_protocol_method(req_id, method: str, token_capability: str):
    if method == "initialize":
        return _jsonrpc_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized":
        return _jsonrpc_notification_response()
    if method == "tools/list":
        allowed_tools = []
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


# ---------------------------------------------------------------------------
# MCP endpoint
# ---------------------------------------------------------------------------

@mcp_router.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP streamable HTTP endpoint. Requires a scoped service token."""
    scope = getattr(request.state, "service_scope", None)
    if not scope:
        raise HTTPException(403, "Service token required for MCP endpoint")
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

    protocol_response = _handle_mcp_protocol_method(req_id, method, token_capability)
    if protocol_response is not None:
        return protocol_response

    if method == "tools/call":
        params = body.get("params", {})
        return await _handle_mcp_tool_call(req_id, scope, token_capability, params)

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")
