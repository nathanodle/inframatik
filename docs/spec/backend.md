# inframatik Backend Architecture

Status: **Complete**

---

## Project Structure

```
inframatik/
├── main.py                 # FastAPI app, lifespan, auth middleware, REST endpoints
├── auth.py                 # Password hashing, session management, CF JWT validation
├── node_config.py          # node.json read/write, config cache, token generation
├── services.py             # Service registry, systemd unit management, port allocation
├── system.py               # System metrics (CPU, memory, disk, network, GPU, temps)
├── cluster_routes.py       # Auth routes, setup, enrollment, proxy, deploy, install script
├── cf_routes.py            # Cloudflare API routes (tunnels, DNS, Access, setup wizard)
├── mcp_routes.py           # MCP JSON-RPC endpoint (deploy, restart, stop, logs, status)
├── nodes.py                # In-memory node registry, heartbeat, stale checker, resolve
├── proxy.py                # Forward requests to remote workers or handle locally
├── tunnel.py               # CF API client (tunnel CRUD, ingress, DNS, Access apps)
├── updater.py              # Version info, tar.gz packaging, apply updates, push to workers
├── inframatik-cli.py       # CLI tool (init, MCP info), stdlib only, no pip deps
├── install.sh              # curl|bash installer with enrollment and CF support
├── requirements.txt        # 7 dependencies: fastapi, uvicorn, psutil, httpx, bcrypt, PyJWT, cryptography
├── static/
│   ├── index.html          # Single-page dashboard (525 lines)
│   ├── app.js              # All frontend logic (1563 lines)
│   └── style.css           # Dark theme, responsive (1118 lines)
├── tests/
│   └── test_config_edit.py # 16 tests for CLI config file editing
└── docs/
    └── spec/               # Specification documents
```

**Total backend Python**: ~3,745 lines across 13 files.
**Total frontend**: ~3,206 lines across 3 files.
**Grand total**: ~6,951 lines of source code.

---

## Authentication Strategy

The system supports four authentication paths, all checked by a single middleware function. There is no decorator-based auth -- every request passes through the same middleware in `main.py`.

### Auth Path 1: Password + Session Token

Used by the browser dashboard.

1. User submits password to `POST /api/auth/login`
2. Server verifies against bcrypt hash stored in `node.json` (`admin_password_hash`)
3. Server creates a session token (`secrets.token_hex(32)`) with configurable TTL (default 24h)
4. Token stored in in-memory `_sessions` dict in `auth.py`
5. Browser stores token in `sessionStorage`, sends as `Authorization: Bearer <token>`
6. Middleware calls `validate_session(token)` which checks expiry

Session limits:
- Maximum 100 concurrent sessions (`MAX_SESSIONS = 100`)
- When exceeded, oldest session is evicted
- Expired sessions cleaned up on each new session creation
- Sessions are lost on process restart (in-memory only)

### Auth Path 2: Cloudflare Access JWT

Used when the dashboard is behind CF Access. Transparent to the user.

1. CF Access injects `Cf-Access-Jwt-Assertion` header on every request
2. Middleware extracts the JWT and calls `validate_cf_access()`
3. Public keys fetched from `https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/certs`
4. Keys cached for 1 hour (`CF_KEYS_TTL = 3600`), stale cache used on fetch failure
5. JWT validated with RS256 against the configured audience (`cf_access_aud` in node.json)
6. If valid, request is authenticated with no session needed

### Auth Path 3: API Keys (X-Api-Key)

Used for worker-to-master communication.

1. Worker sends `X-Api-Key` header with every request to master
2. Middleware compares against the node's own `api_key` in `node_config`
3. Used for: registration, heartbeat, receiving updates, receiving tunnel tokens
4. Each worker has a unique API key generated at enrollment time (`sdk_` + 32 hex bytes)

### Auth Path 4: Service Tokens (Bearer svc_...)

Used by AI agents and the MCP server.

1. Token created via `POST /api/config/service-tokens` (requires admin session)
2. Token format: `svc_` + 32 hex bytes
3. Stored in `node.json` under `service_tokens` map: `{token: {service, created_at}}`
4. Sent as `Authorization: Bearer svc_...`
5. Middleware detects `svc_` prefix, looks up scope, sets `request.state.service_scope`
6. Scoped to a single service name -- can only access that service's endpoints

---

## Auth Middleware Behavior

The middleware in `main.py` classifies every request into one of three categories before checking credentials.

### PUBLIC_PATHS

These paths require no authentication at all:

```python
_PUBLIC_PATHS = {
    "/", "/api/node/info", "/api/node/health", "/api/node/version",
    "/api/auth/login", "/api/auth/set-password", "/api/auth/status",
    "/api/install.sh", "/api/install/package",
}
```

Static files (`/static/*`) are also public.

Rationale: The dashboard HTML/JS/CSS must load before login. Node info and health are needed for monitoring and worker enrollment. Install endpoints serve the curl|bash installer. Auth status tells the frontend whether to show the password-set or login form.

### SELF_AUTH_PATHS

These paths have their own authentication logic inside the route handler:

```python
_SELF_AUTH_PATHS = {
    "/api/nodes/register", "/api/nodes/heartbeat", "/api/node/update",
    "/api/cf/token", "/api/config/reset", "/api/nodes/enroll",
    "/api/nodes/tunnel",
}
```

These routes check `X-Api-Key` or enrollment tokens themselves because they use different credential types than the standard Bearer token flow. The middleware skips them entirely.

### Service Token Scoping

When a service token passes auth, the middleware enforces path restrictions:

```python
_SERVICE_TOKEN_PATHS = {"/api/services", "/api/ports/next", "/mcp"}
_SERVICE_TOKEN_PREFIXES = ("/api/services/",)
```

A service token can only access:
- `GET /api/services` -- filtered to show only its scoped service
- `POST /api/services` -- body must match scoped service name
- `/api/services/{name}/*` -- name must match scope
- `GET /api/ports/next` -- needed to determine available port
- `POST /mcp` -- MCP endpoint, further scoped by tool handlers

Any other path returns 403. Path service name is extracted and compared against the token's scope.

### Middleware Flow

```
Request arrives
  → Is path in PUBLIC_PATHS or /static/*? → Pass through
  → Is path in SELF_AUTH_PATHS? → Pass through (route handles its own auth)
  → Set request.state.service_scope = None
  → Call check_auth(request):
      → Try X-Api-Key match → return True
      → Try CF JWT validation → return True
      → Try Bearer token:
          → Session token? → return True
          → svc_ prefix? → look up scope, set request.state.service_scope → return True
      → return False → 401 response
  → If service_scope is set:
      → Check path against _SERVICE_TOKEN_PATHS / _SERVICE_TOKEN_PREFIXES
      → Check path service name matches scope
      → Reject with 403 if mismatched
  → Pass to route handler
```

---

## Configuration Model

All configuration lives in `~/.config/inframatik/node.json`. There is no database. The file is read once into `_config_cache` (a module-level dict in `node_config.py`) and updated atomically via `save_node_config()` which writes the file and updates the cache in a single call.

### node.json Schema by Role

#### Standalone Node

Created by first-run setup or `POST /api/config/init-standalone`.

```json
{
  "node_id": "a1b2c3d4",
  "node_name": "my-server",
  "role": "standalone",
  "admin_password_hash": "$2b$12$...",
  "tunnel_id": "uuid-or-null",
  "dashboard_hostname": "dash.example.com",
  "cf_token": "...",
  "cf_account_id": "...",
  "cf_zone_id": "...",
  "cf_default_policy_id": "...",
  "cf_team_domain": "myteam",
  "cf_access_aud": "...",
  "service_tokens": {
    "svc_abc123...": {"service": "myapp", "created_at": 1709856000}
  }
}
```

#### Master Node

Created by `POST /api/config/init-master`. Adds worker tracking and enrollment tokens.

```json
{
  "node_id": "a1b2c3d4",
  "node_name": "master-server",
  "role": "master",
  "api_key": "sdk_...",
  "admin_password_hash": "$2b$12$...",
  "tunnel_id": "uuid-or-null",
  "dashboard_hostname": "dash.example.com",
  "workers": {
    "e5f6g7h8": {
      "name": "gpu-worker",
      "address": "http://192.168.1.50:9000",
      "api_key": "sdk_...",
      "tunnel_id": "uuid-or-null"
    }
  },
  "enrollment_tokens": {
    "enroll-abc123...": {"created_at": 1709856000}
  },
  "service_tokens": { "..." : "..." },
  "cf_token": "...",
  "cf_account_id": "...",
  "cf_zone_id": "...",
  "cf_default_policy_id": "...",
  "cf_team_domain": "...",
  "cf_access_aud": "..."
}
```

#### Worker Node

Created by `POST /api/config/init-worker` or via enrollment.

```json
{
  "node_id": "i9j0k1l2",
  "node_name": "gpu-worker",
  "role": "worker",
  "api_key": "sdk_...",
  "master_url": "http://192.168.1.10:9000",
  "admin_password_hash": "$2b$12$...",
  "tunnel_id": "uuid-or-null",
  "service_tokens": { "..." : "..." }
}
```

### Other Config Files

| File | Location | Purpose |
|------|----------|---------|
| `services.json` | `~/.config/inframatik/services.json` | Service registry: name -> {port, command, working_dir, hostname, lan} |
| `ports.env` | `~/.config/inframatik/ports.env` | Auto-generated shell-sourceable port assignments (`export INFRA_MYAPP_PORT=8000`) |

### Token Formats

| Token Type | Format | Entropy | Example |
|------------|--------|---------|---------|
| Session | hex | 256 bits (32 bytes) | `a1b2c3d4...` (64 hex chars) |
| API Key | `sdk_` + hex | 256 bits | `sdk_a1b2c3d4...` |
| Service Token | `svc_` + hex | 256 bits | `svc_a1b2c3d4...` |
| Enrollment Token | `enroll-` + hex | 128 bits (16 bytes) | `enroll-a1b2c3d4...` |
| Node ID | hex | 32 bits (4 bytes) | `a1b2c3d4` |

---

## API Conventions

### Response Format

All API endpoints return JSON. There is no envelope wrapper -- successful responses return the data directly.

```
GET  /api/system       → { "cpu": {...}, "memory": {...}, ... }
GET  /api/services     → [ { "name": "myapp", "port": 8000, ... }, ... ]
POST /api/services     → { "name": "myapp", "port": 8000, "status": "inactive", ... }
POST /api/auth/login   → { "token": "abc...", "expires_at": 1709942400 }
```

### Error Handling

Errors use FastAPI's `HTTPException` which produces:

```json
{ "detail": "Human-readable error message" }
```

Error sources map to specific exception types:

| Python Exception | HTTP Status | Meaning |
|-----------------|-------------|---------|
| `ValueError` | 400 or 404 | Invalid input or resource not found |
| `RuntimeError` | 500 or 502 | Operation failed or remote node error |
| `HTTPException` | As specified | Explicit error from route handler |
| Auth failure | 401 | Missing or invalid credentials |
| Scope mismatch | 403 | Service token accessing wrong resource |

### Status Codes

| Code | Usage |
|------|-------|
| 200 | Default for GET and most POST responses |
| 201 | `POST /api/services` (service registration) |
| 400 | Bad input, missing fields, invalid state |
| 401 | Authentication failed |
| 403 | Insufficient permissions (scope violation, wrong role) |
| 404 | Resource not found |
| 413 | Update package too large (50MB limit) |
| 500 | Internal server error (systemd command failed, etc.) |
| 502 | Remote node unreachable or returned error (proxy) |
| 503 | No ports available |

### MCP Responses

The `/mcp` endpoint uses JSON-RPC 2.0 instead of standard REST:

```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "..."}]}}
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Unknown tool: foo"}}
```

Tool errors return a result (not a JSON-RPC error) with `isError: true`, following MCP convention.

---

## Shared Services

### node_config Caching

`node_config.py` implements a simple read-through cache:

```python
_config_cache: Optional[dict] = None

def get_node_config() -> Optional[dict]:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not CONFIG_FILE.exists():
        return None
    _config_cache = json.loads(CONFIG_FILE.read_text())
    return _config_cache

def save_node_config(config: dict):
    global _config_cache
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    _config_cache = config
```

This is safe because FastAPI runs in a single asyncio event loop (single-threaded). All reads hit the cache after the first load. All writes update both the file and cache atomically within the same synchronous call. `invalidate_cache()` is available for the reset flow.

### _load_cf_config

`tunnel.py` provides `_load_cf_config()` which extracts Cloudflare credentials from the node config into a standardized dict:

```python
def _load_cf_config() -> dict | None:
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
```

Returns `None` if CF is not configured. Used by `cf_routes.py`, `tunnel.py`, and `services.py` (for auto-creating tunnel routes on service registration).

### proxy_to_node

`proxy.py` provides `proxy_to_node(node_id, method, path, body)` which routes requests either locally or to a remote worker:

1. Calls `resolve_node(node_id)` from `nodes.py`
2. If result is `None` (self-node), calls `_handle_local()` which dispatches to the appropriate Python function directly
3. If result contains `{address, api_key}`, makes an HTTP request to the remote worker with `X-Api-Key` auth
4. Remote errors (connect, timeout) raise `RuntimeError` which routes convert to 502
5. Used by all proxy endpoints in `cluster_routes.py`: system, services, tunnel, logs

### In-Memory Node Registry

`nodes.py` maintains `_nodes: dict[str, dict]` tracking registered workers on the master:

- `register_node()` validates the worker's API key against master config, stores connection info
- `heartbeat_node()` updates `last_seen` timestamp
- `_id_map` maps both real node IDs and config-key node IDs to registry entries
- `check_stale_nodes()` marks nodes as offline after 45 seconds without heartbeat
- `_health_cache` provides 15-second TTL active health checks for workers that haven't registered yet

### Background Tasks

Managed via FastAPI lifespan context manager in `main.py`:

- **Master**: `stale_checker_loop()` runs every 10 seconds, marks nodes offline after 45s silence
- **Worker**: `heartbeat_sender_loop()` registers with master (retry with exponential backoff up to 30s), then heartbeats every 15 seconds. Re-registers on 404 response.

---

## Routers

The application uses three FastAPI `APIRouter` instances plus routes defined directly on the `app`:

| Router | Prefix | File | Purpose |
|--------|--------|------|---------|
| `app` (direct) | `/api/system`, `/api/services`, `/api/tunnel`, `/api/ports` | `main.py` | Core system and service endpoints |
| `cluster_router` | `/api/auth`, `/api/node`, `/api/config`, `/api/nodes`, `/api/update`, `/api/install` | `cluster_routes.py` | Auth, config, clustering, proxy, deploy |
| `cf_router` | `/api/cf` | `cf_routes.py` | Cloudflare tunnel, DNS, Access, setup wizard |
| `mcp_router` | `/mcp` | `mcp_routes.py` | MCP JSON-RPC endpoint |

---

## Key Decisions

| Decision | Alternatives Considered | Why This Choice |
|----------|------------------------|-----------------|
| **No database** | SQLite, PostgreSQL | Config is small (<10KB), rarely written, always read whole. No relational queries. JSON files mean zero setup and easy manual inspection. |
| **In-memory sessions** | Redis, JWT (stateless), SQLite | Single process rarely has >10 concurrent sessions. Session loss on restart is acceptable -- users just log in again. Avoids external dependency. |
| **Middleware not decorators** | Per-route `Depends()`, decorator pattern | Single enforcement point for all routes. No risk of forgetting a decorator. Path classification (public/self-auth/scoped) is explicit and auditable in one place. |
| **Single config file (node.json)** | Separate files per concern (cf.env, auth.json) | One file to back up, one file to understand. Atomic updates via read-modify-write. CF credentials, workers, tokens all in one place. |
| **Module-level config cache** | Request-scoped cache, global singleton | Safe in single-threaded asyncio. No request overhead. Cache invalidated explicitly only on reset. Simpler than dependency injection. |
| **Direct systemd calls** | Docker API, supervisor, pm2 | systemd is on every Linux machine. User units need no root. Boot persistence via linger. No containers to manage. |
| **stdlib-only CLI** | Click, typer, argparse | CLI is installed on managed machines which may not have pip packages. Using only `urllib.request`, `json`, `pathlib` means zero deps beyond Python 3.10+. |
| **Inline MCP server** | External MCP SDK, separate process | Zero additional deps. JSON-RPC is simple to implement in ~230 lines. Same auth middleware, same service functions. No process management. |
| **Proxy via HTTP** | Direct function calls to worker, gRPC | Workers are separate machines with their own FastAPI instances. HTTP reuses existing auth (X-Api-Key) and error handling. Simple and debuggable. |
| **Scoped service tokens** | Unscoped tokens, OAuth, JWT claims | Tokens are stored in project repos (.inframatik). Scoping to one service prevents a leaked token from affecting other services. Simple lookup, no crypto needed. |

---

## Related Specs

| Spec | Relevance |
|------|-----------|
| [Stack](stack.md) | Technology choices, dependency list, data flow diagrams |
| [System Monitoring](system-monitoring.md) | `system.py` implementation details |
| [Service Management](service-management.md) | `services.py`, systemd units, port allocation |
| [Clustering](clustering.md) | `nodes.py`, `proxy.py`, enrollment, heartbeat protocol |
| [Cloudflare Integration](cloudflare.md) | `tunnel.py`, `cf_routes.py`, setup wizard flow |
| [Authentication](authentication.md) | `auth.py`, all four auth paths in detail |
| [AI Agent Integration](ai-agents.md) | `mcp_routes.py`, `inframatik-cli.py`, .inframatik format |
