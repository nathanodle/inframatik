# Authentication Specification

**Status:** Complete

## Overview

inframatik supports four authentication methods that coexist within a single middleware pipeline. Browser users authenticate with a password and receive a session token. Cloudflare Access JWTs bypass password login entirely for deployments behind CF Zero Trust. API keys secure worker-to-master communication. Scoped service tokens give AI agents and CI/CD pipelines restricted access to a single service's endpoints, including the MCP server.

---

## Related Specs

| Spec | Relationship |
|------|-------------|
| [Stack](stack.md) | bcrypt, PyJWT, in-memory sessions, no CORS |
| [Backend](backend.md) | Middleware architecture, FastAPI request lifecycle |
| [Clustering](clustering.md) | API keys for worker registration and heartbeats |
| [Cloudflare Integration](cloudflare.md) | CF Access apps, team domain, audience tag |
| [AI Agent Integration](ai-agents.md) | Service tokens for MCP and REST access |

---

## Requirements

1. **Password login** -- Browser users set a password on first visit and log in to receive a session token.
2. **CF Access JWT** -- Requests arriving through Cloudflare Access with a valid JWT bypass password auth entirely.
3. **API keys** -- Workers authenticate to the master using a shared key for registration, heartbeat, updates, and CF token push.
4. **Service tokens** -- AI agents and CI/CD pipelines use scoped tokens restricted to a single service name and a limited set of endpoints.
5. **Middleware pipeline** -- Every non-public request passes through a single middleware that tries all four auth methods in order.
6. **No CORS** -- Dashboard and API share the same origin. Cross-origin access is not configured (secure default).

---

## Auth Method 1: Password Login

### Set Password

First-time setup. Only succeeds if no password has been set yet.

- **Endpoint:** `POST /api/auth/set-password`
- **Body:** `{"password": "<string>"}`
- **Validation:** Minimum 8 characters
- **Storage:** bcrypt hash stored in `node.json` as `admin_password_hash`
- **Response:** `{"status": "password_set"}`
- **Error:** 400 if password already set or under 8 characters

### Login

- **Endpoint:** `POST /api/auth/login`
- **Body:** `{"password": "<string>"}`
- **Verification:** `bcrypt.checkpw()` -- constant-time comparison prevents timing attacks
- **On success:** Creates a session token via `create_session(duration_hours)`
- **Duration:** Configurable via `session_duration_hours` in node.json (default: 24 hours)
- **Response:** `{"token": "<hex>", "expires_at": <unix_timestamp>}`
- **Error:** 401 if password is invalid

### Session Storage

Sessions are stored in an in-memory dictionary (`_sessions`):

```python
_sessions: dict[str, dict] = {
    "abc123...": {"expires_at": 1709942400},
}
```

- **Token format:** `secrets.token_hex(32)` -- 64 hex characters
- **Maximum sessions:** 100 (`MAX_SESSIONS`)
- **Eviction:** When the cap is reached, the session with the earliest `expires_at` is evicted
- **Cleanup:** Expired sessions are pruned on every `create_session()` call
- **Validation:** `validate_session()` checks existence and expiry; deletes expired sessions on access
- **Persistence:** None. Sessions are lost on process restart. This is acceptable for the single-process, small-user-count deployment model.

### Logout

- **Endpoint:** `POST /api/auth/logout`
- **Auth:** Bearer token in Authorization header
- **Action:** Removes the session from `_sessions`
- **Response:** `{"status": "logged_out"}`

### Status Check

- **Endpoint:** `GET /api/auth/status`
- **Auth:** Public (no auth required)
- **Response:** `{"has_password": <bool>}`
- **Purpose:** Frontend uses this to decide whether to show the set-password form or the login form

---

## Auth Method 2: CF Access JWT

Automatic authentication for deployments behind Cloudflare Zero Trust. The browser never sees this flow -- Cloudflare injects the JWT header after the user passes the Access policy.

### Flow

1. User visits the dashboard through a Cloudflare Access-protected hostname
2. CF Access validates the user against its configured identity providers
3. CF injects the `Cf-Access-Jwt-Assertion` header into the proxied request
4. inframatik middleware extracts the header and validates the JWT

### Validation

1. **Header:** `Cf-Access-Jwt-Assertion`
2. **Config required:** `cf_team_domain` and `cf_access_aud` must be set in node.json
3. **Public key fetch:** `GET https://{cf_team_domain}.cloudflareaccess.com/cdn-cgi/access/certs`
4. **Key cache:** In-memory, refreshed every 1 hour (`CF_KEYS_TTL = 3600`). On fetch failure, stale cache is returned.
5. **Signature:** RS256 algorithm via PyJWT
6. **Audience:** Validated against `cf_access_aud` from config
7. **Key rotation:** Tries all keys from the `public_certs` (or `keys`) array until one succeeds

### Properties

- Bypasses password login entirely -- no session token created
- Requires Cloudflare to be configured (cf_team_domain + cf_access_aud in node.json)
- If CF is not configured or the JWT is invalid, falls through to the next auth method
- No user identity is extracted -- the JWT is validated for authenticity only

---

## Auth Method 3: API Keys

Machine-to-machine authentication between workers and the master node.

### Format

- **Prefix:** `sdk_`
- **Body:** 32 random hex bytes (64 hex characters)
- **Full format:** `sdk_` + 64 hex chars (68 characters total)
- **Generation:** `secrets.token_hex(32)`
- **Header:** `X-Api-Key`

### Validation

The `X-Api-Key` header value is compared against:

1. The node's own `api_key` in node.json (for the master receiving worker requests)
2. Each worker's `api_key` in the master's `workers` config dict (for identifying which worker is calling)

### Usage

| Operation | Caller | Endpoint |
|-----------|--------|----------|
| Registration | Worker | `POST /api/nodes/register` |
| Heartbeat | Worker | `POST /api/nodes/heartbeat` |
| Code update | Master pushing to worker | `POST /api/node/update` |
| CF token push | Master pushing to worker | `POST /api/cf/token` |
| Config reset | Any node with api_key | `POST /api/config/reset` |

### Self-Auth Paths

API key validation for registration, heartbeat, update, CF token push, reset, and enrollment is handled by the route handlers themselves (not the middleware). These paths are listed in `_SELF_AUTH_PATHS` and the middleware passes them through without checking.

---

## Auth Method 4: Service Tokens

Scoped tokens for AI agents, CI/CD pipelines, and any programmatic integration that should only manage a single service.

### Format

- **Prefix:** `svc_`
- **Body:** 32 random hex bytes (64 hex characters)
- **Full format:** `svc_` + 64 hex chars (68 characters total)
- **Generation:** `secrets.token_hex(32)`
- **Header:** `Authorization: Bearer svc_...`

### Scoping

Each service token is bound to exactly one service name. The scope is enforced at two levels:

1. **Path restriction:** Service tokens can only access paths in `_SERVICE_TOKEN_PATHS` and paths starting with prefixes in `_SERVICE_TOKEN_PREFIXES`
2. **Service name enforcement:** For paths under `/api/services/{name}/...`, the `{name}` segment must match the token's scope

### Allowed Paths

```python
_SERVICE_TOKEN_PATHS = {"/api/services", "/api/ports/next", "/mcp"}
_SERVICE_TOKEN_PREFIXES = ("/api/services/",)
```

This allows service tokens to: list services (filtered to own scope), register a service, manage a specific service (start/stop/restart/logs/delete), get the next available port, and call the MCP endpoint.

### Storage

Service tokens are stored in node.json under the `service_tokens` dict:

```json
{
  "service_tokens": {
    "svc_abc123...": {
      "service": "my-app",
      "created_at": 1709856000
    }
  }
}
```

### Management Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `POST /api/config/service-tokens` | POST | Create a new service token. Body: `{"service": "<name>"}`. Returns `{"token": "svc_...", "service": "<name>"}`. Token is shown once. |
| `DELETE /api/config/service-tokens/{token}` | DELETE | Revoke a service token immediately. |

### Service List Filtering

When a service token calls `GET /api/services`, the response is filtered to only include services matching the token's scope:

```python
if scope:
    services = [s for s in services if s.get("name") == scope]
```

---

## Middleware Flow

The auth middleware (`auth_middleware` in `main.py`) runs on every HTTP request:

```
Request arrives
    |
    v
Path in _PUBLIC_PATHS or /static/* ?
    |-- Yes --> pass through (no auth)
    |
    v
Path in _SELF_AUTH_PATHS ?
    |-- Yes --> pass through (route handler does its own auth)
    |
    v
Initialize request.state.service_scope = None
    |
    v
check_auth(request) -->
    |
    |-- Try 1: X-Api-Key header matches config api_key?
    |     |-- Yes --> return True (full access)
    |
    |-- Try 2: Cf-Access-Jwt-Assertion header present?
    |     |-- Yes + valid JWT --> return True (full access)
    |
    |-- Try 3: Authorization: Bearer <token>
    |     |-- Session token valid? --> return True (full access)
    |     |-- Token starts with svc_?
    |           |-- get_service_token_scope() returns scope?
    |                 |-- Yes --> set request.state.service_scope, return True
    |
    |-- All failed --> 401 "Authentication required"
    |
    v
If service_scope is set:
    |
    |-- Path in _SERVICE_TOKEN_PATHS or matches _SERVICE_TOKEN_PREFIXES?
    |     |-- No --> 403 "Service token cannot access this endpoint"
    |
    |-- Path starts with /api/services/?
    |     |-- Extract service name from path
    |     |-- Name != scope? --> 403 "Token is scoped to service '{scope}'"
    |
    v
Pass to route handler
```

### Public Paths

These paths require no authentication:

| Path | Purpose |
|------|---------|
| `/` | Dashboard HTML page |
| `/api/node/info` | Node identity (role, name, id) |
| `/api/node/health` | Health check |
| `/api/node/version` | Git version info |
| `/api/auth/login` | Login endpoint |
| `/api/auth/set-password` | First-time password setup |
| `/api/auth/status` | Check if password exists |
| `/api/install.sh` | Installer script download |
| `/api/install/package` | Code package download |
| `/static/*` | Static files (CSS, JS, fonts) |

### Self-Auth Paths

These paths bypass the middleware and implement their own authentication:

| Path | Auth Method |
|------|-------------|
| `/api/nodes/register` | X-Api-Key (validated by handler) |
| `/api/nodes/heartbeat` | X-Api-Key (validated by handler) |
| `/api/node/update` | X-Api-Key (validated by handler) |
| `/api/cf/token` | X-Api-Key (validated by handler) |
| `/api/config/reset` | X-Api-Key (validated by handler) |
| `/api/nodes/enroll` | Enrollment token (validated by handler) |

---

## API Endpoints

### Authentication

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `GET /api/auth/status` | GET | Public | Returns `{has_password: bool}` |
| `POST /api/auth/set-password` | POST | Public | Set initial password (min 8 chars) |
| `POST /api/auth/login` | POST | Public | Authenticate, returns session token |
| `POST /api/auth/logout` | POST | Session | Invalidate session token |

### Service Token Management

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `POST /api/config/service-tokens` | POST | Session/CF JWT | Create scoped service token |
| `DELETE /api/config/service-tokens/{token}` | DELETE | Session/CF JWT | Revoke service token |

---

## Data Model

### node.json -- Auth-Related Fields

```json
{
  "admin_password_hash": "$2b$12$...",
  "session_duration_hours": 24,
  "cf_team_domain": "mycompany",
  "cf_access_aud": "abc123...",
  "api_key": "sdk_...",
  "service_tokens": {
    "svc_abc123...": {
      "service": "my-app",
      "created_at": 1709856000
    }
  },
  "workers": {
    "e5f6a7b8": {
      "name": "gpu-server",
      "address": "http://192.168.1.20:9000",
      "api_key": "sdk_..."
    }
  }
}
```

### In-Memory State

| Store | Type | Contents |
|-------|------|----------|
| `_sessions` | `dict[str, dict]` | `{token: {expires_at}}` -- max 100 entries |
| `_cf_keys_cache` | `dict` | `{keys: [...], fetched_at: float}` -- refreshed every 3600s |

### Key Formats Summary

| Key Type | Prefix | Random Bytes | Total Characters | Generation |
|----------|--------|-------------|------------------|------------|
| Session token | (none) | 32 hex bytes | 64 | `secrets.token_hex(32)` |
| API key | `sdk_` | 32 hex bytes | 68 | `secrets.token_hex(32)` |
| Service token | `svc_` | 32 hex bytes | 68 | `secrets.token_hex(32)` |
| Enrollment token | `enroll-` | 16 hex bytes | 39 | `secrets.token_hex(16)` |

---

## UI

### Login Screen

- Shown when `GET /api/auth/status` returns `{has_password: false}` or when no valid session exists
- **First visit:** Set-password form (single password field, min 8 chars)
- **Subsequent visits:** Login form (password field)
- Session token stored in `sessionStorage` (cleared on tab close)
- On 401 response from any API call, the frontend redirects to the login screen

### Settings -- Service Tokens Section

- Lists existing tokens: service name + created date (token value is NOT shown after creation)
- "Generate" button: prompts for service name, calls `POST /api/config/service-tokens`
- Token displayed once in a copy-friendly format after creation
- "Revoke" button per token: calls `DELETE /api/config/service-tokens/{token}`
- Available on standalone, master, and worker settings views

---

## Error Handling

| Scenario | Response |
|----------|----------|
| No auth on protected endpoint | 401: "Authentication required" |
| Invalid password | 401: "Invalid password" |
| Password already set | 400: "Password already set. Use settings to change it." |
| Password too short | 400: "Password must be at least 8 characters" |
| Service token on restricted path | 403: "Service token cannot access this endpoint" |
| Service token scope mismatch | 403: "Token is scoped to service '{scope}'" |
| Invalid API key on register | 401: "Unknown API key" |
| Invalid API key on reset | 401: "API key required" |
| Invalid enrollment token | 401: "Invalid or expired enrollment token" |
| CF JWT with missing config | Falls through to next auth method (no error) |
| CF JWT with invalid signature | Falls through to next auth method (no error) |
| CF public key fetch failure | Returns stale cached keys; if no cache, validation fails silently |

---

## Security Measures

| Measure | Implementation |
|---------|---------------|
| Password hashing | bcrypt with auto-generated salt (`bcrypt.gensalt()`) |
| Constant-time comparison | `bcrypt.checkpw()` prevents timing attacks |
| Session count cap | Maximum 100 sessions; oldest evicted when exceeded |
| Session cleanup | Expired sessions pruned on every `create_session()` call |
| Enrollment tokens | 128-bit entropy (16 random bytes), one-time use, consumed on success |
| Service token scoping | Path restriction + service name enforcement in middleware |
| No CORS | No `CORSMiddleware` configured; same-origin only (secure default) |
| Token entropy | All tokens use `secrets.token_hex()` (cryptographically secure) |
| CF key caching | 1-hour TTL; stale cache used on fetch failure (graceful degradation) |
| API key header | `X-Api-Key` separate from `Authorization` to avoid confusion with session/service tokens |

---

## Decisions

| Decision | Alternatives Considered | Why This Choice |
|----------|------------------------|-----------------|
| In-memory sessions | Redis, database, signed JWTs | Single process, rarely >10 concurrent users. Lost on restart is acceptable. Simplest implementation. |
| bcrypt for passwords | argon2, scrypt | Well-established, available via pip, NIST-recommended. `checkpw()` provides constant-time comparison. |
| Session token (not JWT) | Stateless JWT sessions | Sessions need revocation (logout). Server-side store allows immediate invalidation. |
| 100 session cap | Unlimited, 10, 1000 | Prevents memory exhaustion from token accumulation. 100 is far beyond expected concurrent users. |
| No CORS | Explicit CORS config | Dashboard and API on same origin. No cross-origin access needed. Adding CORS would expand attack surface. |
| `svc_` prefix for service tokens | No prefix, `agent_`, `tok_` | Prefix enables quick dispatch in `check_auth()` without database lookup for non-service tokens. |
| CF JWT as second auth check | First check, last check | After API key (fast, exact match) but before session (which requires dict lookup). CF JWT validation involves crypto so it runs only when the header is present. |
| Separate `_SELF_AUTH_PATHS` | All auth in middleware | Registration, heartbeat, and enrollment have unique auth semantics (enrollment tokens, per-route API key validation). Cleaner to let handlers manage their own auth. |
| `X-Api-Key` header for workers | Bearer token, mTLS | Distinct from `Authorization: Bearer` used by sessions and service tokens. Avoids ambiguity in the middleware dispatch. |
