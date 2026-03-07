# AI Agent Integration Specification

**Status:** Complete

## Overview

inframatik provides a complete integration path for AI coding agents (Claude Code, Codex) and programmatic clients to deploy and manage services. The system consists of a CLI tool (`inframatik init`) that authenticates, creates a scoped service token, writes a `.inframatik` config file with inline API docs, detects installed agent harnesses, and registers the built-in MCP server. The MCP server implements JSON-RPC 2.0 over HTTP directly in FastAPI with no external SDK dependency, exposing five service management tools scoped to a single service name.

---

## Related Specs

| Spec | Relationship |
|------|-------------|
| [Stack](stack.md) | Stdlib-only CLI, JSON-RPC over HTTP, no external MCP deps |
| [Authentication](authentication.md) | Service tokens: creation, scoping, middleware enforcement |
| [Service Management](service-management.md) | deploy, restart, stop, logs, status -- same functions as REST API |
| [Backend](backend.md) | FastAPI router, middleware pipeline, request lifecycle |
| [Clustering](clustering.md) | Install script includes CLI symlink setup |

---

## Requirements

1. **CLI tool** -- `inframatik init` authenticates, creates a service token, writes config, detects harnesses, and registers MCP.
2. **.inframatik config** -- JSON file with endpoint, token, service name, and inline API usage instructions for any model.
3. **MCP server** -- Built-in HTTP endpoint implementing JSON-RPC 2.0 with 5 tools, authenticated via scoped service tokens.
4. **Harness detection** -- Automatically detects Claude Code and Codex, configures their MCP settings, and updates instruction files.
5. **Service token dashboard** -- Settings UI for listing, generating, and revoking service tokens.
6. **No external dependencies** -- CLI uses Python stdlib only; MCP server uses no external MCP SDK.

---

## CLI Tool (inframatik-cli.py)

### Installation

- Source file: `inframatik-cli.py` in the project root
- Installed by the installer script as a symlink: `~/.local/bin/inframatik` -> `~/inframatik/inframatik-cli.py`
- Requires only Python 3.10+ stdlib (no pip install needed)

### Dependencies (stdlib only)

| Module | Purpose |
|--------|---------|
| `urllib.request` | HTTP requests to the inframatik API |
| `json` | JSON parsing and serialization |
| `pathlib` | File path operations |
| `subprocess` | Running `claude mcp add` / `codex mcp add` CLI commands |
| `getpass` | Secure password input (no echo) |
| `shutil` | `which()` for CLI detection |
| `os`, `sys` | Environment, exit codes |

### Commands

#### `inframatik init`

Interactive setup flow that connects a project directory to an inframatik server:

```
1. Prompt for server endpoint (default: http://localhost:9000)
2. Prompt for admin password (getpass, no echo)
3. POST /api/auth/login -> session token
4. Prompt for service name (required)
5. Prompt for hostname (optional, for CF public access)
6. POST /api/config/service-tokens {service} -> svc_... token
7. Write .inframatik file with endpoint, token, service, hostname, instructions
8. Add .inframatik to .gitignore
9. Detect agent harnesses (Claude Code, Codex)
10. For each detected harness:
    a. Prompt to register MCP server
    b. Prompt to update instruction file (CLAUDE.md / AGENTS.md)
11. Print summary
```

#### `inframatik mcp`

Informational stub. Prints usage info pointing to the built-in HTTP endpoint:

```
The MCP server is built into inframatik -- no separate process needed.

Endpoint: http://<server>:9000/mcp
Transport: streamable HTTP
Auth: service token (from .inframatik)

If you ran 'inframatik init', your agent harness is already configured.
The MCP server exposes: deploy, restart, stop, logs, status
```

### HTTP Helper

`api_request(endpoint, method, path, body, token)` -- stdlib-only HTTP client:

- Uses `urllib.request.Request` with JSON content type
- Sends `Authorization: Bearer <token>` when token is provided
- Returns parsed JSON on success
- Prints error detail to stderr on `HTTPError` or `URLError`
- Returns `None` on failure (caller checks and exits)

---

## .inframatik Config File

### Format

JSON file written to the project root by `inframatik init`:

```json
{
  "endpoint": "http://localhost:9000",
  "token": "svc_abc123...",
  "service": "my-app",
  "hostname": "myapp.example.com",
  "instructions": "This app is managed by inframatik. Use the API at the endpoint below with the token as Bearer auth.\n\nRegister: POST /api/services {\"name\": \"my-app\", \"command\": \"<start command>\", \"working_dir\": \"<path>\"}\nStart: POST /api/services/my-app/start\nStop: POST /api/services/my-app/stop\nRestart: POST /api/services/my-app/restart\nLogs: GET /api/services/my-app/logs\nStatus: GET /api/services\n\nAll requests need header: Authorization: Bearer <token from this file>"
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `endpoint` | string | Yes | inframatik server URL (e.g., `http://localhost:9000`) |
| `token` | string | Yes | Scoped service token (`svc_...`) |
| `service` | string | Yes | Service name this token is scoped to |
| `hostname` | string | No | CF hostname for public access |
| `instructions` | string | Yes | Inline API usage documentation for any model |

### Properties

- **Gitignored:** Added to `.gitignore` automatically (contains secret token)
- **Per-project:** One `.inframatik` file per project directory
- **Self-documenting:** The `instructions` field contains complete API docs so any model can use it, even without MCP support
- **Not committed:** Token is a secret; file must never be checked into version control

---

## MCP Server (mcp_routes.py)

### Endpoint

- **URL:** `POST /mcp`
- **Protocol:** JSON-RPC 2.0 (streamable HTTP transport)
- **Auth:** Scoped service token required (validated by auth middleware, scope extracted to `request.state.service_scope`)
- **Router:** `mcp_router` included in the main FastAPI app

### Protocol Implementation

Implements the MCP protocol directly without any external MCP SDK. All JSON-RPC dispatch is in a single endpoint handler.

**Protocol version:** `2025-03-26`

**Server info:**
```json
{
  "name": "inframatik",
  "version": "1.0.0"
}
```

### Supported Methods

| Method | Description | Response |
|--------|-------------|----------|
| `initialize` | Protocol handshake | `{protocolVersion, capabilities: {tools: {}}, serverInfo}` |
| `notifications/initialized` | Client acknowledgment | `{}` (no-op) |
| `tools/list` | List available tools | `{tools: [...]}` |
| `tools/call` | Execute a tool | `{content: [{type: "text", text: "..."}]}` or `{content: [...], isError: true}` |

### JSON-RPC Helpers

```python
_jsonrpc_result(req_id, result)  # -> {"jsonrpc": "2.0", "id": req_id, "result": result}
_jsonrpc_error(req_id, code, message)  # -> {"jsonrpc": "2.0", "id": req_id, "error": {code, message}}
```

### Tools

#### 1. deploy

Register the service and start it. If already registered, just starts it.

```json
{
  "name": "deploy",
  "inputSchema": {
    "type": "object",
    "properties": {
      "command": {"type": "string", "description": "The start command (e.g. 'uvicorn main:app --host 127.0.0.1')"},
      "working_dir": {"type": "string", "description": "Absolute path to the working directory"},
      "hostname": {"type": "string", "description": "Optional CF hostname for public access (e.g. 'myapp.example.com')"}
    },
    "required": ["command", "working_dir"]
  }
}
```

**Logic:**
- Checks if service is already registered (by name from token scope)
- If registered and not running: starts it
- If registered and running: returns "already running"
- If not registered: calls `register_service()` then `start_service()`
- Includes port number and optional public URL in response text

#### 2. restart

Restart the service.

```json
{
  "name": "restart",
  "inputSchema": {"type": "object", "properties": {}}
}
```

#### 3. stop

Stop the service.

```json
{
  "name": "stop",
  "inputSchema": {"type": "object", "properties": {}}
}
```

#### 4. logs

Get recent service logs.

```json
{
  "name": "logs",
  "inputSchema": {
    "type": "object",
    "properties": {
      "lines": {"type": "integer", "description": "Number of log lines to return (default 100)", "default": 100}
    }
  }
}
```

#### 5. status

Get the service status and details. Returns name, status, port, command, and working directory.

```json
{
  "name": "status",
  "inputSchema": {"type": "object", "properties": {}}
}
```

### Service Name Resolution

All tools receive the service name from `request.state.service_scope` (set by the auth middleware from the service token). The agent never specifies a service name -- it is implicit from the token. This prevents a token scoped to `my-app` from operating on `other-app`.

### Tool Execution

Each tool handler calls the same `services.py` functions used by the REST API:

| Tool | services.py Function(s) |
|------|------------------------|
| deploy | `list_services()`, `register_service()`, `start_service()` |
| restart | `restart_service()` |
| stop | `stop_service()` |
| logs | `get_service_logs()` |
| status | `list_services()` (filtered by name) |

### Error Handling

Tool errors are returned as MCP tool results with `isError: true` (not JSON-RPC errors), so agents can read the error message and recover:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{"type": "text", "text": "Error: Service 'my-app' not found"}],
    "isError": true
  }
}
```

Protocol-level errors use JSON-RPC error codes:

| Code | Meaning |
|------|---------|
| -32700 | Parse error (malformed JSON body) |
| -32600 | Invalid request (missing method) |
| -32601 | Method not found |
| -32602 | Invalid params (unknown tool name) |

---

## Agent Harness Detection and Configuration

### Detection

The CLI detects installed agent harnesses by checking for:

| Harness | Detection Method |
|---------|-----------------|
| Claude Code | `~/.claude` directory exists OR `claude` CLI in PATH (`shutil.which`) |
| Codex | `~/.codex` directory exists OR `codex` CLI in PATH (`shutil.which`) |

### Claude Code Configuration

Two registration strategies, tried in order:

**Strategy 1: CLI registration (preferred)**
```bash
claude mcp add --transport http --scope project \
  --header "Authorization: Bearer svc_..." \
  inframatik http://localhost:9000/mcp
```

If the `claude` CLI is available and the command succeeds, this registers the MCP server at project scope.

**Strategy 2: .mcp.json file (fallback)**

If `claude` CLI is unavailable or the command fails, writes/updates `.mcp.json`:

```json
{
  "mcpServers": {
    "inframatik": {
      "type": "http",
      "url": "http://localhost:9000/mcp",
      "headers": {
        "Authorization": "Bearer svc_..."
      }
    }
  }
}
```

**CLAUDE.md update:**

Optionally appends a deployment instructions section:

```markdown
## Deployment

This app is deployed via inframatik. See `.inframatik` for API endpoint, auth token, and usage instructions.
```

Skipped if the file already contains both "inframatik" and ".inframatik".

### Codex Configuration

Two registration strategies, tried in order:

**Strategy 1: CLI registration (preferred)**
```bash
codex mcp add inframatik --transport http http://localhost:9000/mcp
```

**Strategy 2: .codex/config.toml file (fallback)**

If `codex` CLI is unavailable or the command fails, writes/updates `.codex/config.toml`:

```toml
[mcp_servers.inframatik]
type = "http"
url = "http://localhost:9000/mcp"

[mcp_servers.inframatik.headers]
Authorization = "Bearer svc_..."
```

**AGENTS.md update:**

Same deployment instructions section as CLAUDE.md, appended to `AGENTS.md` for Codex.

### Config File Editing

All config file editing functions follow the same pattern: read, parse, merge, backup, write.

#### `edit_mcp_json(endpoint, token, path)`

- If file missing: create with `mcpServers.inframatik` entry
- If file exists: parse JSON, merge inframatik entry into `mcpServers`, preserve other servers
- If file malformed: print warning, return False (do not corrupt)
- Backup: writes `.mcp.json.bak` before modifying existing files

#### `edit_codex_toml(endpoint, token, path)`

- If file missing: create `.codex/` directory and write new file
- If file exists: read lines, remove existing `[mcp_servers.inframatik]` section, append new section
- Section removal: scans for `[mcp_servers.inframatik` header, skips lines until next `[` header
- TOML escaping: backslash, double-quote, and newline characters escaped in values
- Backup: writes `.codex/config.toml.bak` before modifying existing files

#### `ensure_gitignore(entry, path)`

- If file missing: create with entry
- If file exists and entry present: no-op (returns False)
- If file exists without entry: append (with newline separator if file lacks trailing newline)

#### `append_instructions(path, content)`

- If file missing: create with content
- If file exists and already has inframatik section: no-op (returns False)
- If file exists without section: append with newline separator
- Detection: checks for both "inframatik" (case-insensitive) and ".inframatik" in existing content

### Test Coverage

The config editing functions are covered by 16 tests in `tests/test_config_edit.py`:

| Category | Tests | Coverage |
|----------|-------|---------|
| .mcp.json | 5 | create, merge, update, malformed (refuse), backup |
| .codex/config.toml | 4 | create, merge, update, backup |
| .gitignore | 4 | create, append, no-op (already present), no trailing newline |
| CLAUDE.md / AGENTS.md | 3 | create, append, no-op (already present) |

All tests run in temporary directories using `tempfile.TemporaryDirectory` and restore `cwd` after completion.

---

## Service Token Management Dashboard

### Settings UI

The service tokens section appears in the Settings modal for standalone, master, and worker nodes.

**Token list:**
- Displays each token's scoped service name and creation date
- Token value is NOT displayed in the list (never stored in UI after creation)

**Generate new token:**
- Prompts for service name
- Calls `POST /api/config/service-tokens {"service": "<name>"}`
- Displays the token value once in a copy-friendly format
- Token cannot be retrieved again after dismissal

**Revoke token:**
- Per-token revoke button
- Calls `DELETE /api/config/service-tokens/{token}`
- Token is immediately invalidated (removed from node.json)

### Config API

`GET /api/config` includes a service token summary (without token values):

```json
{
  "service_tokens": [
    {"service": "my-app", "created_at": 1709856000},
    {"service": "worker-api", "created_at": 1709857000}
  ]
}
```

---

## API Endpoints

### MCP

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `POST /mcp` | POST | Service token | JSON-RPC 2.0 MCP endpoint |

### Service Token Management

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `POST /api/config/service-tokens` | POST | Session/CF JWT | Create scoped service token |
| `DELETE /api/config/service-tokens/{token}` | DELETE | Session/CF JWT | Revoke service token |

---

## Data Model

### .inframatik (per-project, gitignored)

```json
{
  "endpoint": "http://localhost:9000",
  "token": "svc_abc123...",
  "service": "my-app",
  "hostname": "myapp.example.com",
  "instructions": "..."
}
```

### node.json -- Service Tokens

```json
{
  "service_tokens": {
    "svc_abc123...": {
      "service": "my-app",
      "created_at": 1709856000
    },
    "svc_def456...": {
      "service": "worker-api",
      "created_at": 1709857000
    }
  }
}
```

### .mcp.json (Claude Code project scope)

```json
{
  "mcpServers": {
    "inframatik": {
      "type": "http",
      "url": "http://localhost:9000/mcp",
      "headers": {
        "Authorization": "Bearer svc_..."
      }
    }
  }
}
```

### .codex/config.toml (Codex project scope)

```toml
[mcp_servers.inframatik]
type = "http"
url = "http://localhost:9000/mcp"

[mcp_servers.inframatik.headers]
Authorization = "Bearer svc_..."
```

---

## Error Handling

| Scenario | Handling |
|----------|---------|
| Authentication failed during `inframatik init` | Print "Authentication failed.", exit 1 |
| Empty service name | Print "Service name is required.", exit 1 |
| Service token creation failure | Print "Failed to create service token.", exit 1 |
| Server unreachable | Print "Error: server unreachable ({reason})", return None |
| HTTP error from API | Parse error detail from JSON response, print to stderr, return None |
| Malformed .mcp.json | Print warning, skip file (do not corrupt), return False |
| No agent harnesses detected | Print info message pointing to .inframatik REST API docs |
| MCP request without service token | 403: "Service token required for MCP endpoint" |
| MCP malformed JSON body | JSON-RPC error -32700: "Parse error" |
| MCP missing method | JSON-RPC error -32600: "Invalid request: missing method" |
| MCP unknown method | JSON-RPC error -32601: "Method not found: {method}" |
| MCP unknown tool | JSON-RPC error -32602: "Unknown tool: {name}" |
| Tool execution error | MCP result with `isError: true` and error message in content |
| `claude mcp add` fails | Falls back to .mcp.json file editing |
| `codex mcp add` fails | Falls back to .codex/config.toml file editing |

---

## Decisions

| Decision | Alternatives Considered | Why This Choice |
|----------|------------------------|-----------------|
| Built-in HTTP MCP (not stdio) | stdio transport, separate MCP server process | HTTP transport works over the network -- agents connect to the same endpoint as the browser. No process management needed. No pipe coordination. Token auth integrates with existing middleware. Single deployment. |
| Stdlib-only CLI | Click, Typer, argparse with pip deps | CLI is installed via symlink on fresh machines before any venv exists. Zero pip dependencies means it works immediately after Python is available. No version conflicts with the main app's venv. |
| Inline instructions in .inframatik | Separate docs file, link to web docs, model-specific prompts | Any model (with or without MCP support) can read .inframatik and understand the API. Works with models that only have file access. Self-contained -- no network fetch needed for docs. |
| No external MCP SDK | `mcp` pip package, `fastmcp` | Five tools with simple schemas. JSON-RPC 2.0 dispatch is ~50 lines. No dependency to track, no version conflicts, no SDK update lag. Full control over the protocol surface. |
| Service name from token scope (not tool args) | Agent specifies service name per call | Eliminates an entire class of authorization bugs. Agent cannot accidentally (or maliciously) operate on another service. Simpler tool schemas. |
| Harness detection by directory + CLI | Only CLI, only directory, config file | Some users have the directory but not the CLI (or vice versa). Checking both maximizes detection accuracy. |
| Config file backup before edit | No backup, git-based recovery | Users may have hand-edited configs. A `.bak` file provides immediate recovery without requiring git. |
| Prompt-based harness config (not automatic) | Auto-configure all detected, skip all | Respects user preferences. Some users may want MCP but not CLAUDE.md updates. Interactive prompts give control. |
| `tools/call` errors as results (not JSON-RPC errors) | JSON-RPC error responses for tool failures | MCP spec convention. Agents can read the error text and retry or adjust. JSON-RPC errors are reserved for protocol-level failures. |
