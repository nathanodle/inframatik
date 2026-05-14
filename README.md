# inframatik

A system dashboard and service manager for Linux machines. Monitor CPU, memory, disk, network, GPUs, and temperatures. Register and manage services with automatic port assignment. Cluster multiple nodes with master/worker architecture. Cloudflare tunnel integration for public access with Zero Trust. Built-in MCP server for AI agent deployments.

## Quick Start

**Prerequisites:** Python 3.10+, Linux with systemd

```bash
git clone https://github.com/nathanodle/inframatik.git
cd inframatik
./setup
```

The setup script will:
1. Prompt you for an admin password
2. Install Python dependencies
3. Create and start a systemd user service
4. Install the `inframatik` CLI

Then open the URL it prints, log in, and choose a role: **Standalone**, **Master**, or **Worker**.

## Features

- **System monitoring** — CPU (per-core), memory, disk, network interfaces, load average, temperatures, NVIDIA/AMD GPUs, top processes
- **Service management** — register apps, auto-assign ports (8000–8999), start/stop/restart, view logs, systemd user units
- **Multi-node clustering** — master/worker with enrollment tokens, sidebar, proxy, heartbeats, deploy-to-workers
- **Cloudflare integration** — guided setup wizard, tunnel management, DNS records, Access applications, per-node tunnels, dashboard Zero Trust protection
- **Authentication** — password login, CF Access JWT bypass, scoped service tokens, API keys for worker communication
- **AI agent support** — built-in MCP server, scoped service tokens, CLI tool for repo setup, Claude Code + Codex integration

## Architecture

```
FastAPI backend (Python)  ←→  Vanilla JS frontend
         │
         ├── ~/.config/inframatik/node.json      (node identity, role, credentials, CF config)
         ├── ~/.config/inframatik/services.json   (service registry)
         └── ~/.config/inframatik/ports.env       (auto-generated port env vars)
```

- Services run as **systemd user units** under `~/.config/systemd/user/`
- Ports are auto-assigned from 8000–8999 with `$PORT` and `$HOST` env vars
- Services survive logout and start on boot (via `loginctl enable-linger`)
- All credentials (CF tokens, API keys, password hash) stored in `node.json`

## Node Roles

| Role | Description |
|------|-------------|
| **Standalone** | Single machine. System monitoring + service management. No clustering. |
| **Master** | Central node. Monitors workers via sidebar, proxies API calls, deploys updates, manages CF tunnels. |
| **Worker** | Connects to a master via heartbeats. Receives updates and, when the master has Cloudflare configured, creates its own local CF tunnel during enrollment. |

## Multi-Node Setup

### Option A: Install script (recommended)

1. On the master, go to Settings → Workers → **Generate Enrollment Token**
2. On the new machine:

```bash
curl -fsSL http://MASTER_IP:9000/api/install.sh | bash -s -- --enroll TOKEN

# With a custom name
curl -fsSL http://MASTER_IP:9000/api/install.sh | bash -s -- --enroll TOKEN --name gpu-server

# With Cloudflare support (non-interactive)
INSTALL_CF=1 curl -fsSL http://MASTER_IP:9000/api/install.sh | bash -s -- --enroll TOKEN
```

The installer prompts for an admin password, sets up a venv, creates a systemd service, installs the `inframatik` CLI, and enrolls with the master automatically. Enrollment tokens are single-use.

### Option B: Manual

1. Clone and install on the worker machine (same as Quick Start)
2. On the master, generate an enrollment token in Settings → Workers
3. On the worker, choose **Worker** in the setup prompt and enter the master's address + enrollment token

## Cloudflare Integration (Optional)

Guided setup — no manual config files needed.

### Setup

1. Go to Settings → Cloudflare
2. Create a [Cloudflare API token](https://dash.cloudflare.com/profile/api-tokens) with permissions:
   - Account → Cloudflare Tunnel → Edit
   - Account → Access: Apps and Policies → Edit
   - Zone → DNS → Edit
3. Paste the token → the wizard auto-discovers your account, domains, and existing Access policies
4. Select your account, domain, and optionally an Access policy (or create one)

### What it enables

- **Service hostnames** — register a service with a hostname (e.g. `myapp.example.com`) and inframatik creates the tunnel route, DNS record, and Access app automatically
- **Dashboard access** — put the dashboard itself behind CF Access (Settings → Dashboard Access)
- **Worker tunnels** — enrolled workers copy the master's saved CF config, create their own tunnel, and run cloudflared locally

## Authentication

| Method | Used by | How it works |
|--------|---------|-------------|
| **Password login** | Browser users | Set during install or first visit. Sessions default to 24h. |
| **CF Access JWT** | Users through CF | JWT validated cryptographically against CF public keys. No login needed. |
| **API keys** | Worker ↔ master | Exchanged during enrollment. Used for heartbeats, registration, updates. |
| **Service tokens** | AI agents, CI/CD | Scoped to one service. Can only manage that service. |

All mutating API endpoints require authentication. Read-only system metrics also require a valid session.

## AI Agent Integration

AI coding agents can deploy and manage services using scoped service tokens.

### Quick setup

```bash
inframatik init
```

This walks you through:
1. Authenticating with the inframatik server
2. Creating a scoped service token for your app
3. Writing a `.inframatik` config file with API instructions
4. Optionally registering the MCP server with Claude Code or Codex
5. Optionally appending deployment instructions to CLAUDE.md / AGENTS.md

### How it works

The `.inframatik` file contains the API endpoint, a scoped service token, and inline instructions any model can read:

```json
{
  "endpoint": "http://localhost:9000",
  "token": "svc_...",
  "service": "my-app",
  "instructions": "Register: POST /api/services ...\nStart: POST /api/services/my-app/start\n..."
}
```

A model reads this file and knows how to deploy via `curl` — no MCP required.

### MCP server

inframatik includes a built-in MCP server at `/mcp` (streamable HTTP transport). Five tools:

| Tool | Description |
|------|-------------|
| `register` | Register and start the service |
| `restart` | Restart the service |
| `stop` | Stop the service |
| `logs` | Get recent service logs |
| `status` | Check service status |

No extra deps or processes — the MCP server runs inside the same FastAPI app. `inframatik init` auto-configures it for Claude Code (`.mcp.json`) and Codex (`.codex/config.toml`).

### Supported agent harnesses

- **Claude Code** — detects `~/.claude`, registers MCP via `claude mcp add` or `.mcp.json`
- **Codex CLI** — detects `~/.codex`, registers MCP via `codex mcp add` or `.codex/config.toml`
- **Any agent** — reads `.inframatik` directly for REST API instructions

## Service Management

See [USAGE.md](USAGE.md) for detailed documentation on registering, managing, and configuring services.

## API Reference

### Authentication
- `GET /api/auth/status` — check if password is set
- `POST /api/auth/login` — login with password, returns session token
- `POST /api/auth/logout` — invalidate session
- `POST /api/auth/set-password` — set admin password (first run only)

### System
- `GET /api/system` — system metrics

### Services
- `GET /api/services` — list services
- `POST /api/services` — register a service
- `DELETE /api/services/{name}` — remove a service
- `POST /api/services/{name}/start|stop|restart` — control a service
- `GET /api/services/{name}/logs` — view logs
- `GET /api/ports/next` — next available port

### Cluster
- `GET /api/node/info` — node role and identity
- `GET /api/config` — node configuration
- `POST /api/config/init-standalone|init-master|init-worker` — configure node role
- `POST /api/config/reset` — reset to unconfigured
- `GET /api/nodes` — list all nodes (master)
- `POST /api/config/workers` — add a worker (master)
- `DELETE /api/config/workers/{id}` — remove a worker (master)
- `POST /api/config/enrollment-tokens` — generate enrollment token (master)
- `POST /api/nodes/enroll` — enroll a worker with token
- `POST /api/config/service-tokens` — generate scoped service token
- `DELETE /api/config/service-tokens/{token}` — revoke service token
- `POST /api/config/dashboard-access` — enable CF Access on dashboard
- `DELETE /api/config/dashboard-access` — disable CF Access on dashboard

### Deploy
- `GET /api/node/version` — version info
- `POST /api/update/deploy` — push code to workers (master)
- `POST /api/update/deploy-self` — restart this node

### MCP
- `POST /mcp` — MCP streamable HTTP endpoint (service token required)

## Migration from sysdashboard

See [TEARDOWN.md](TEARDOWN.md) for instructions on removing old sysdashboard installations.

## License

[MIT](LICENSE)
