"""MCP streamable HTTP server — exposes service management as MCP tools.

Implements the MCP JSON-RPC protocol directly over HTTP. No external MCP SDK needed.
Auth uses scoped service tokens (svc_...) validated by the main auth middleware.
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from services import (
    list_services,
    register_service,
    start_service,
    stop_service,
    restart_service,
    get_service_logs,
    get_service_status,
)

mcp_router = APIRouter()

PROTOCOL_VERSION = "2025-03-26"

SERVER_INFO = {
    "name": "inframatik",
    "version": "1.0.0",
}

TOOLS = [
    {
        "name": "deploy",
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


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _tool_deploy(service_name: str, args: dict) -> dict:
    command = args.get("command", "")
    working_dir = args.get("working_dir", ".")
    hostname = args.get("hostname")

    # Check if already registered
    services = await list_services()
    existing = next((s for s in services if s.get("name") == service_name), None)

    if existing:
        # Already registered — just start if not running
        if existing.get("status") != "active":
            status = await start_service(service_name)
            return {"text": f"Service '{service_name}' started (was already registered). Status: {status}"}
        return {"text": f"Service '{service_name}' is already running."}

    # Register new service
    svc = await register_service(
        name=service_name,
        command=command,
        working_dir=working_dir,
        hostname=hostname,
    )

    # Start it
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
    "deploy": _tool_deploy,
    "restart": _tool_restart,
    "stop": _tool_stop,
    "logs": _tool_logs,
    "status": _tool_status,
}


# ---------------------------------------------------------------------------
# MCP endpoint
# ---------------------------------------------------------------------------

@mcp_router.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP streamable HTTP endpoint. Requires a scoped service token."""
    scope = getattr(request.state, "service_scope", None)
    if not scope:
        raise HTTPException(403, "Service token required for MCP endpoint")

    try:
        body = await request.json()
    except Exception:
        return _jsonrpc_error(None, -32700, "Parse error")

    req_id = body.get("id")
    method = body.get("method")

    if not method:
        return _jsonrpc_error(req_id, -32600, "Invalid request: missing method")

    # MCP protocol methods
    if method == "initialize":
        return _jsonrpc_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })

    if method == "notifications/initialized":
        # Client acknowledgment — no response needed but return OK
        return _jsonrpc_result(req_id, {})

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})

        handler = _TOOL_HANDLERS.get(tool_name)
        if not handler:
            return _jsonrpc_error(req_id, -32602, f"Unknown tool: {tool_name}")

        try:
            result = await handler(scope, tool_args)
            return _jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": result.get("text", "")}],
            })
        except ValueError as e:
            return _jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            })
        except RuntimeError as e:
            return _jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            })

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")
