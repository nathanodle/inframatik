# inframatik Tech Stack

## Overview

Single-machine or multi-machine deployment on Linux with systemd. No containers, no cloud orchestration. The application runs as a systemd user service and manages other user services.

---

## Stack Summary

| Layer | Technology | Notes |
|-------|------------|-------|
| **Backend** | FastAPI (Python 3.10+) | Async, single process, lifespan-managed background tasks |
| **Frontend** | Vanilla JS + HTML/CSS | No build step, no framework, served as static files |
| **Styling** | Custom CSS with variables | Dark theme, Inter + JetBrains Mono fonts |
| **Config Storage** | JSON files (`~/.config/inframatik/`) | node.json, services.json, ports.env |
| **Process Management** | systemd user units | Services survive logout, start on boot via linger |
| **Auth** | bcrypt + session tokens + CF JWT + service tokens | Four auth paths checked by middleware |
| **CF Integration** | Cloudflare API (httpx) | Tunnels, DNS, Access apps |
| **MCP** | JSON-RPC 2.0 over HTTP | Built into FastAPI, no external deps |
| **CLI** | Python stdlib only | urllib.request, json, pathlib — no pip deps |

---

## Backend Details

### FastAPI
- Async endpoints with `async/await`
- Lifespan context manager for background tasks (heartbeat, stale checker)
- Multiple routers: `cluster_router`, `cf_router`, `mcp_router`
- Auth middleware checks every non-public request
- Static file serving via `StaticFiles` mount

### Dependencies (requirements.txt)
```
fastapi
uvicorn[standard]
psutil
httpx
bcrypt
PyJWT
cryptography
```

### No Database
All state in JSON files. No ORM, no migrations. Config is a single `node.json` file read into a dict and cached in memory. Writes update both file and cache atomically (single-threaded asyncio).

---

## Frontend Details

### Vanilla JS
- No build step, no transpilation, no npm
- Single `app.js` file (~1500 lines)
- `api()` helper function handles auth headers and 401 redirects
- `innerHTML` with `esc()` function for XSS prevention
- `sessionStorage` for auth token

### CSS
- CSS custom properties (variables) for theming
- Dark theme only
- Responsive with media queries at 768px and 480px
- Inter for UI text, JetBrains Mono for code/data

---

## Infrastructure

### Development
```
git clone → python3 -m venv venv → pip install → uvicorn main:app --port 9000
```

### Production
```
User request → http://server:9000 → FastAPI (uvicorn)
                                       ├── /static/* (HTML/CSS/JS)
                                       ├── /api/* (REST endpoints)
                                       └── /mcp (MCP JSON-RPC)

Managed services → systemd user units → ports 8000-8999
                                          └── $PORT env var

Optional: Cloudflare tunnel → ingress routes → services
                           → DNS CNAME records
                           → Access applications
```

### Process Management
- inframatik itself: `systemctl --user` service (created by installer)
- Managed services: `systemctl --user` units with `infra-` prefix
- cloudflared: system-level service at `/etc/systemd/system/cloudflared.service`
- User linger enabled for boot persistence

---

## External Services

### Cloudflare API (optional)
- Base URL: `https://api.cloudflare.com/client/v4/`
- Auth: Bearer token in headers
- Used for: tunnels, ingress routes, DNS records, Access apps/policies
- Token stored in `node.json` (not a separate file)
- All operations via `httpx.AsyncClient` with 10s timeout

### cloudflared binary (optional)
- Local metrics at `http://127.0.0.1:20241/metrics` (Prometheus format)
- Connector token stored at `/etc/cloudflared/token`
- Managed by `infra-cf-setup` helper script (requires sudo)

---

## Data Flow

```
Browser → Login → Session Token
       → API calls with Bearer token
       → FastAPI middleware validates auth
       → Route handler executes
       → Returns JSON

Agent → .inframatik file → service token
     → POST /mcp with Bearer svc_...
     → MCP JSON-RPC dispatch
     → Tool calls services.py functions
     → Returns JSON-RPC result

Worker → heartbeat_sender_loop
      → POST /api/nodes/register (X-Api-Key)
      → POST /api/nodes/heartbeat every 15s
      → Master tracks in-memory _nodes dict

Master → proxy_to_node(node_id, method, path)
      → resolve_node() → {address, api_key}
      → httpx request to worker
      → Returns worker response to browser
```

---

## Decisions

| Decision | Alternatives Considered | Why This Choice |
|----------|------------------------|-----------------|
| Vanilla JS (no React/Vue) | React, Svelte, Vue | Zero build step, no npm, ~50KB total. Dashboard is read-heavy with simple forms. |
| JSON files (no database) | SQLite, PostgreSQL | Config is small, rarely written, always read whole. No query needs. Simpler ops. |
| systemd user units | Docker, supervisor, pm2 | Already on every Linux machine. No root for service management. Boot persistence built in. |
| In-memory sessions | Redis, database sessions | Single process, rarely >10 concurrent sessions. Sessions lost on restart is acceptable. |
| bcrypt for passwords | argon2, scrypt | Well-established, available via pip. NIST-recommended with proper cost factor. |
| No CORS | Explicit CORS config | Dashboard and API on same origin. No cross-origin access needed. Secure default. |
| CF credentials in node.json | Separate cf.env file, vault | Single config file simplifies management. File permissions provide access control. |

---

## Future Considerations

- **WebSocket for real-time updates** — Currently polling every 5s. WebSocket would reduce latency and bandwidth.
- **Persistent sessions** — Could move to signed JWT tokens (stateless) to survive restarts.
- **Plugin system** — Service types beyond systemd (Docker containers, screen sessions).
- **Metrics history** — Time-series storage for CPU/memory/disk trends.
