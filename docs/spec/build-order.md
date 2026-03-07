# inframatik Build Order (Retrospective)

Status: **Complete**

This document records the actual implementation order of inframatik, reconstructed from the git history. All steps are complete.

---

## Step 1: Core System Monitoring + Service Management

**Status:** Complete
**Commit:** `2dfcaea` -- Initial commit: inframatik system dashboard and service manager
**Depends on:** Nothing (foundation)

The initial commit shipped a fully functional system with monitoring, service management, multi-node clustering, Cloudflare integration, and authentication. However, the features were built incrementally during development and can be decomposed into logical layers. This step covers the core that everything else builds on.

### What was built

- **System metrics** (`system.py`, 265 lines): CPU (per-core, frequency, model), memory, swap, disk (filters snap/tmpfs), network interfaces, temperatures (AMD k10temp, Intel coretemp, NVMe), GPU monitoring (NVIDIA via `nvidia-smi`, AMD via `rocm-smi`), top processes, load averages, uptime. CPU model cached since it never changes.

- **Service management** (`services.py`, 247 lines in initial commit): Register/deregister services with auto port allocation from range 8000-8999. Generate systemd user units with `infra-` prefix. Start/stop/restart/logs via `systemctl --user`. Service registry stored in `~/.config/inframatik/services.json`. Auto-generated `ports.env` file for shell-sourceable port assignments. Service name validation (lowercase alphanumeric, hyphens, underscores, max 48 chars).

- **FastAPI application** (`main.py`): REST endpoints for system metrics, services CRUD, tunnel status, port allocation. Static file serving for the dashboard. Lifespan context manager for background tasks.

- **Dashboard** (`static/index.html`, `static/app.js`, `static/style.css`): Single-page app with vanilla JS. Dark theme with CSS custom properties. System monitoring cards, service management panel, real-time polling every 5 seconds. Responsive layout with breakpoints at 768px and 480px.

### Relevant specs

- [System Monitoring](system-monitoring.md) -- metrics collection, GPU detection
- [Service Management](service-management.md) -- port allocation, systemd units, registry format
- [Stack](stack.md) -- technology choices (FastAPI, vanilla JS, systemd, JSON files)
- [Backend](backend.md) -- project structure, API conventions

---

## Step 2: Multi-Node Clustering

**Status:** Complete
**Commit:** `2dfcaea` (part of initial commit)
**Depends on:** Step 1 (core system + services)

### What was built

- **Node configuration** (`node_config.py`): Three node roles (standalone, master, worker). Config stored in `~/.config/inframatik/node.json` with in-memory cache. Functions for initializing each role, adding/removing workers, managing enrollment tokens.

- **Node registry** (`nodes.py`): In-memory `_nodes` dict on master tracking registered workers. `_id_map` for dual-key lookup (real node ID and config-key node ID). Worker registration validates API key against master's worker list. Heartbeat updates `last_seen`. Stale checker marks nodes offline after 45 seconds. Active health check fallback for workers that haven't registered yet (15-second TTL cache).

- **Proxy layer** (`proxy.py`): `proxy_to_node()` resolves node ID to connection info, forwards HTTP requests to remote workers with `X-Api-Key` auth. Self-node requests handled locally via `_handle_local()` which calls Python functions directly instead of making HTTP requests.

- **Cluster routes** (`cluster_routes.py`): Setup endpoints (init-standalone, init-master, init-worker). Worker management (add/remove). Enrollment flow (create token on master, worker presents token to enroll, master generates API key). Proxy endpoints for system metrics, services, tunnel status, and service actions on remote workers.

- **Background tasks**: Master runs `stale_checker_loop()` every 10 seconds. Worker runs `heartbeat_sender_loop()` -- registers with exponential backoff (5s to 30s), then heartbeats every 15 seconds. Re-registers on 404 (master forgot the worker).

- **Deploy/update** (`updater.py`): Version info from git. `build_package()` creates tar.gz of source files (excludes venv, .git, .env, __pycache__). `apply_package()` extracts with path traversal protection. Master can push updates to all workers. Self-restart via systemd.

- **Enrollment protocol**: Master creates one-time enrollment tokens (`enroll-` + 16 hex bytes). Worker presents token to `POST /api/nodes/enroll`. Master validates and consumes token, generates API key, stores worker config. Worker receives API key and begins heartbeat loop.

### Relevant specs

- [Clustering](clustering.md) -- master/worker architecture, enrollment, heartbeat, proxy
- [Backend](backend.md) -- proxy_to_node, in-memory node registry, background tasks

---

## Step 3: Cloudflare Integration

**Status:** Complete
**Commit:** `2dfcaea` (part of initial commit)
**Depends on:** Step 2 (clustering, for worker tunnel setup)

### What was built

- **Tunnel management** (`tunnel.py`): Create/list CF tunnels via API. Get connector tokens. Initialize tunnel ingress with catch-all 404 rule. Add/remove ingress routes. Tunnel status from local cloudflared Prometheus metrics (`http://127.0.0.1:20241/metrics`) -- connection count, edge locations.

- **DNS management** (`tunnel.py`): Create/delete/list CNAME records pointing to tunnel (`{tunnel_id}.cfargotunnel.com`). Paginated listing with per_page=100.

- **Access apps** (`tunnel.py`): Create/delete/list CF Access applications. Self-hosted type with 24h session duration. Policy attachment by ID. Policy discovery by inspecting existing Access apps.

- **CF routes** (`cf_routes.py`): REST endpoints wrapping all tunnel.py functions. Worker tunnel setup flow -- master creates tunnel, gets token, pushes to worker via proxy. Worker-side token receiver starts cloudflared via `sudo /usr/local/bin/infra-cf-setup`.

- **Setup wizard** (`cf_routes.py`): Multi-step guided flow -- validate API token (fetches accounts), list zones, list/create reusable Access policies, save credentials to node.json. All wizard endpoints accept the token in the request body (credentials not yet saved during wizard).

- **Dashboard CF Access** (`cluster_routes.py`): Put the dashboard behind CF Access. Creates tunnel (if needed), adds ingress route to `localhost:9000`, creates DNS record, creates Access app. Stores `dashboard_hostname` in config.

- **Auto-CF on service registration** (`services.py`): When a service is registered with a `hostname`, automatically creates tunnel route, DNS record, and Access app (if default policy configured). Cleanup on deregister.

### Relevant specs

- [Cloudflare Integration](cloudflare.md) -- tunnel lifecycle, DNS, Access, wizard flow
- [Backend](backend.md) -- _load_cf_config shared service

---

## Step 4: First-Run Setup UX

**Status:** Complete
**Commit:** `2dfcaea` (part of initial commit)
**Depends on:** Steps 1-3 (needs all features to configure)

### What was built

- **Setup modal** (`static/app.js`): Detects unconfigured node via `GET /api/node/info` returning `role: "unconfigured"`. Shows modal with node name input and role selection (standalone/master/worker). Standalone is the default for single-machine use.

- **Password setup**: `GET /api/auth/status` returns `has_password: false` on fresh install. Dashboard shows password creation form. `POST /api/auth/set-password` accepts first password (rejects if already set, requires 8+ chars). Subsequent visits show login form.

- **CF wizard in settings**: After node setup, Settings tab offers Cloudflare configuration. Step-by-step: enter API token, select account, select zone, pick or create Access policy, save.

- **Install script** (`install.sh`): Served by master at `GET /api/install.sh` with master URL embedded. Handles: git clone, venv creation, pip install, systemd service setup, user linger, optional enrollment with master, optional cloudflared setup. `GET /api/install/package` serves tar.gz for fresh installs without git.

### Relevant specs

- [Cloudflare Integration](cloudflare.md) -- setup wizard flow
- [UI](ui.md) -- setup modal, settings panel

---

## Step 5: Authentication System

**Status:** Complete
**Commit:** `2dfcaea` (part of initial commit)
**Depends on:** Step 4 (password setup is part of first-run)

### What was built

- **Password hashing** (`auth.py`): bcrypt with default cost factor (12 rounds). Hash stored in `node.json` as `admin_password_hash`.

- **Session management** (`auth.py`): In-memory `_sessions` dict. `secrets.token_hex(32)` for 256-bit entropy. Configurable session duration (default 24h). Cleanup of expired sessions on new session creation.

- **CF JWT validation** (`auth.py`): Fetch public keys from CF Access certs endpoint. 1-hour cache with stale-on-error fallback. RS256 validation with audience check. Supports both `public_certs` and `keys` response formats.

- **Auth middleware** (`main.py`): Single middleware function checking all four auth paths in order. Three path classifications: PUBLIC_PATHS (no auth), SELF_AUTH_PATHS (route handles own auth), everything else (middleware validates). Service token scoping enforced at middleware level.

- **Auth routes** (`cluster_routes.py`): Login, logout, set-password, auth-status endpoints. All in `_PUBLIC_PATHS` except logout (requires session).

### Relevant specs

- [Authentication](authentication.md) -- all four auth paths, middleware flow
- [Backend](backend.md) -- auth middleware behavior, PUBLIC_PATHS, SELF_AUTH_PATHS

---

## Step 6: AI Agent Platform

**Status:** Complete
**Commits:** `a0157e6`, `7b452af`, `7ae43e9`
**Depends on:** Step 5 (service tokens need auth system)

### What was built

**Commit `a0157e6` -- Service tokens + CLI + agent harness integration:**

- **Scoped service tokens** (`node_config.py`): `create_service_token()` generates `svc_` + 32 hex bytes, stores in node.json with service name and creation timestamp. `get_service_token_scope()` returns service name for a token, or None. `revoke_service_token()` removes from config.

- **Service token middleware** (`main.py`): Added `_SERVICE_TOKEN_PATHS` and `_SERVICE_TOKEN_PREFIXES`. Middleware detects `svc_` prefix in Bearer token, looks up scope, enforces path restrictions. `GET /api/services` filtered to scoped service only.

- **Token management routes** (`cluster_routes.py`): `POST /api/config/service-tokens` creates token. `DELETE /api/config/service-tokens/{token}` revokes. Config endpoint includes token summary (service names, creation dates, but not token values).

- **CLI tool** (`inframatik-cli.py`, 330 lines): `inframatik init` command -- authenticates with admin password, creates service token, writes `.inframatik` config file. Detects Claude Code and Codex CLI. Registers MCP server via `claude mcp add` or `.mcp.json` file. Generates `.codex/config.toml` for Codex. Appends deployment instructions to `CLAUDE.md` / `AGENTS.md`. All stdlib -- no pip dependencies.

- **.inframatik config file**: JSON with endpoint, token, service name, optional hostname, and inline API instructions. Any model that reads this file gets everything needed to deploy via REST. Gitignored by default.

- **Config file editing** with 16 tests (`tests/test_config_edit.py`): Safe merge for `.mcp.json` (preserves existing servers, backs up, refuses malformed). TOML section editing for `.codex/config.toml` (remove old section, append new). Idempotent `.gitignore` and markdown updates.

- **Dashboard UI**: Service tokens section in Settings tab -- generate tokens, view active tokens.

**Commit `7b452af` -- Built-in MCP server:**

- **MCP endpoint** (`mcp_routes.py`, 233 lines): JSON-RPC 2.0 over HTTP at `POST /mcp`. Implements `initialize`, `notifications/initialized`, `tools/list`, `tools/call`. Protocol version `2025-03-26`.

- **5 MCP tools**: `deploy` (register + start, or just start if exists), `restart`, `stop`, `logs` (configurable line count), `status`. Each tool operates only on the token's scoped service. Tool implementations call the same `services.py` functions as the REST API.

- **No external deps**: JSON-RPC dispatch is ~100 lines. No MCP SDK, no separate process. Works with existing `.mcp.json` / `.codex/config.toml` configs.

**Commit `7ae43e9` -- Fixes:**

- MCP: catch all exceptions in tool handlers (was only catching ValueError/RuntimeError). Service function errors now return JSON-RPC error responses instead of HTTP 500.
- CLI: catch `URLError` for server unreachable (was only catching `HTTPError`).
- CLI: escape TOML values to handle backslashes, quotes, and newlines in endpoint URLs and tokens.

### Relevant specs

- [AI Agent Integration](ai-agents.md) -- MCP server, CLI tool, .inframatik format, harness detection
- [Backend](backend.md) -- service token scoping, MCP response format

---

## Step 7: Security Hardening

**Status:** Complete
**Commits:** `0cc546b`, `ebeb84c`, `bb3e4e1`
**Depends on:** Step 6 (all features must exist to audit)

Three rounds of security hardening addressing different attack surfaces.

**Commit `0cc546b` -- Token entropy, systemd injection, key exposure:**

- Enrollment tokens: increased from 4 bytes to 16 bytes entropy (was trivially brute-forceable).
- Service commands: reject semicolons (systemd interprets `;` as command separators, allowing injection of additional ExecStart directives).
- `/api/config` response: strip worker `api_key` values (was exposing secret keys to the browser dashboard).

**Commit `ebeb84c` -- Host header, specifier escaping, session limits:**

- Install script endpoint: validate `Host` header with regex (`^[a-zA-Z0-9._-]+(:\d+)?$`) to prevent host header injection when embedding master URL.
- Enrollment response: removed `master_url` from response (worker already knows it; was unnecessary data exposure).
- Systemd specifier escaping: escape `%` as `%%` in service commands before writing unit files. Prevents systemd specifier expansion (`%H` = hostname, `%u` = user, `%n` = unit name) which could leak system info or cause unexpected behavior.
- Session limits: cap at 100 concurrent sessions, evict oldest when exceeded. Prevents memory exhaustion via repeated login requests.

**Commit `bb3e4e1` -- Race condition, upload limit:**

- Service registration race condition: save registry entry (with allocated port) BEFORE async CF operations. Previously, two concurrent `POST /api/services` requests could both call `next_available_port()` and get the same port, since the registry wasn't updated until after the `await` points for CF setup.
- Update package size limit: reject uploads over 50MB (`len(body) > 50 * 1024 * 1024`) to prevent memory exhaustion via oversized POST to `/api/node/update`.

### Security measures summary

| Attack Surface | Mitigation | Location |
|---------------|-----------|----------|
| Brute-force enrollment tokens | 128-bit entropy (`secrets.token_hex(16)`) | `node_config.py` |
| Systemd command injection (semicolons) | Reject `;` in service commands | `services.py` |
| Systemd specifier expansion | Escape `%` as `%%` | `services.py` |
| Systemd newline injection | Reject `\n`, `\r` in commands and working_dir | `services.py` |
| API key leakage via config endpoint | Strip `api_key` from worker entries in response | `cluster_routes.py` |
| Host header injection | Regex validation on Host header | `cluster_routes.py` |
| Session memory exhaustion | Cap at 100 sessions, evict oldest | `auth.py` |
| Port allocation race condition | Save registry before async operations | `services.py` |
| Update memory exhaustion | 50MB upload limit | `cluster_routes.py` |
| Path traversal in updates | Resolve and check `is_relative_to(APP_DIR)` | `updater.py` |
| Shell injection | No `shell=True` anywhere; all `subprocess_exec` | Throughout |

### Relevant specs

- [Authentication](authentication.md) -- session limits, token entropy
- [Service Management](service-management.md) -- command validation, specifier escaping
- [Backend](backend.md) -- auth middleware, host header validation

---

## Step 8: Code Cleanup and Documentation

**Status:** Complete
**Commits:** `5b44061`, `0181e32`, `29fa279`, `5a03408`
**Depends on:** Steps 1-7 (polish after all features and hardening)

**Commit `5b44061` -- Code cleanup:**

- Extracted 280-line install script from inline string literal in `cluster_routes.py` to standalone `install.sh` file. Enables proper syntax highlighting, linting, and independent testing.
- Fixed `__import__("time")` hack in `node_config.py` with proper `import time` statement.
- Removed redundant inline `import asyncio` in `cluster_routes.py`.
- Added `logging` to silent exception handlers in `services.py`. CF setup/cleanup failures now logged at debug level instead of silently swallowed.

**Commit `0181e32` -- Packaging and proxy fixes:**

- `updater.py`: include `install.sh` in `build_package()`. Was missing from deploy packages, breaking install script updates when pushing to workers.
- `proxy.py`: parse `?lines=N` query parameter when proxying service logs locally. Was always defaulting to 100 lines regardless of the requested count.

**Commit `29fa279` -- Migration docs:**

- `TEARDOWN.md`: Step-by-step instructions for migrating from the predecessor project (sysdashboard). Designed to be handed to another Claude instance for execution on remote servers.

**Commit `5a03408` -- Documentation rewrite:**

- `README.md`: Updated architecture diagram (removed stale cf.env references). Added authentication section with table of all auth methods. Complete API reference including auth, enrollment, service tokens, MCP. Node roles as scannable table.
- `USAGE.md`: Added auth headers to all API examples. Added "From an AI Agent" section. Removed stale references to manual CF configuration.

### Relevant specs

- [Backend](backend.md) -- project structure
- [Stack](stack.md) -- infrastructure, process management

---

## Dependency Graph

```
Step 1: Core System + Services
  │
  ├── Step 2: Multi-Node Clustering
  │     │
  │     └── Step 3: Cloudflare Integration
  │           │
  │           └── Step 4: First-Run Setup UX
  │                 │
  │                 └── Step 5: Authentication System
  │                       │
  │                       └── Step 6: AI Agent Platform
  │                             │
  │                             └── Step 7: Security Hardening
  │                                   │
  │                                   └── Step 8: Code Cleanup + Docs
```

Each step required the previous steps to exist. Clustering needs services and system metrics to proxy. CF integration needs clustering for worker tunnel setup. Setup UX needs all features to configure them. Auth needs setup to create passwords. Agent platform needs auth for service tokens. Security hardening needs all features to audit. Cleanup and docs come last.

---

## Commit History (Complete)

| # | Hash | Description | Lines Changed |
|---|------|-------------|---------------|
| 1 | `2dfcaea` | Initial commit: system dashboard and service manager | +6,778 |
| 2 | `a0157e6` | Scoped service tokens, CLI tool, agent harness integration | +764, -8 |
| 3 | `7b452af` | Built-in MCP server over streamable HTTP | +261, -3 |
| 4 | `7ae43e9` | Fix MCP error handling, CLI network errors, TOML escaping | +9, -8 |
| 5 | `0cc546b` | Security hardening: token entropy, systemd injection, key exposure | +8, -2 |
| 6 | `5b44061` | Code cleanup: extract install script, fix imports, add logging | +294, -289 |
| 7 | `0181e32` | Fix install.sh missing from deploy package, proxy log line count | +15, -4 |
| 8 | `ebeb84c` | Security: host header validation, specifier escaping, session limits | +15, -10 |
| 9 | `bb3e4e1` | Fix service registration race condition, update size limit | +13, -10 |
| 10 | `29fa279` | Add TEARDOWN.md for migrating from sysdashboard | +74 |
| 11 | `5a03408` | Rewrite README.md and USAGE.md for current feature set | +150, -147 |
