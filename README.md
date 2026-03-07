# inframatik

A lightweight system dashboard and service manager for Linux machines. Monitor CPU, memory, disk, network, GPUs, and temperatures. Register and manage services with automatic port assignment. Optionally cluster multiple nodes with a master/worker architecture and Cloudflare tunnel integration.

## Quick Start

**Prerequisites:** Python 3.10+, Linux with systemd

```bash
git clone https://github.com/nathanodle/inframatik.git
cd inframatik
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn main:app --host 0.0.0.0 --port 9000
```

Open `http://localhost:9000`. On first visit you'll be prompted to set up the node as **Standalone**, **Master**, or **Worker**.

## Features

- **System monitoring** — CPU (per-core), memory, disk, network interfaces, load average, temperatures, NVIDIA/AMD GPUs, top processes
- **Service management** — register apps, auto-assign ports (8000–8999), start/stop/restart, view logs, systemd user units
- **Multi-node clustering** — master/worker architecture with sidebar, proxy, heartbeats, and deploy-to-workers
- **Cloudflare integration** (optional) — tunnel management, DNS records, Access applications, per-node tunnels
- **Remote installer** — `curl | bash` script served by master for bootstrapping new nodes

## Architecture

```
FastAPI backend (Python)  ←→  Vanilla JS frontend
         │
         ├── ~/.config/inframatik/node.json      (node identity & role)
         ├── ~/.config/inframatik/services.json   (service registry)
         ├── ~/.config/inframatik/ports.env       (auto-generated port env vars)
         └── ~/.config/inframatik/cf.env          (Cloudflare credentials, optional)
```

Services run as **systemd user units** under `~/.config/systemd/user/`. They survive logout and start on boot (via `loginctl enable-linger`).

Ports are auto-assigned from the 8000–8999 range. Each service gets `$PORT` and `$HOST` environment variables set in its unit file.

## Node Roles

### Standalone

Single machine dashboard. Monitors system metrics and manages local services. No clustering.

### Master

Central node that also monitors workers. The master:
- Shows all nodes in a sidebar
- Proxies API calls to workers
- Deploys code updates to workers
- Manages Cloudflare tunnels for all nodes (if configured)

### Worker

Connects to a master via heartbeats. The worker:
- Sends registration + periodic heartbeats to the master
- Exposes the same local API for the master to proxy through
- Can receive code updates and CF tunnel tokens from the master

## Multi-Node Setup

### Option A: Install script (recommended)

1. On the master, go to Settings → Workers → **Generate Enrollment Token**
2. On the new machine, run the installer with the token:

```bash
curl -fsSL http://MASTER_IP:9000/api/install.sh | bash -s -- --enroll TOKEN

# With a custom name
curl -fsSL http://MASTER_IP:9000/api/install.sh | bash -s -- --enroll TOKEN --name gpu-server

# With Cloudflare support (non-interactive)
INSTALL_CF=1 curl -fsSL http://MASTER_IP:9000/api/install.sh | bash -s -- --enroll TOKEN
```

The worker enrolls automatically — no manual key copy-paste needed. Enrollment tokens are single-use.

### Option B: Manual

1. Clone and install on the worker machine (same as Quick Start)
2. On the master, generate an enrollment token in Settings → Workers
3. On the worker, choose **Worker** in the setup prompt and enter the master's address + enrollment token

## Cloudflare Integration (Optional)

Cloudflare integration enables public access to services via tunnels, automatic DNS records, and Zero Trust Access applications.

### Setup

1. Go to Settings → Cloudflare
2. Create a [Cloudflare API token](https://dash.cloudflare.com/profile/api-tokens) with these permissions:
   - Account → Cloudflare Tunnel → Edit
   - Account → Access: Apps and Policies → Edit
   - Zone → DNS → Edit
3. Paste the token and click Validate
4. Select your account and domain
5. Optionally select or create an Access policy (for Zero Trust protection)

The wizard auto-discovers your account, domains, and existing policies. All credentials are stored in `node.json` — no manual config files needed.

### What it does

When you register a service with a **hostname** (e.g. `myapp.example.com`):
1. Adds an ingress route to the Cloudflare tunnel
2. Creates a CNAME DNS record pointing to the tunnel
3. Creates a CF Access application with the default policy (if configured)

Deleting the service cleans up all three.

## Configuration Reference

| File | Purpose | Auto-created |
|------|---------|-------------|
| `node.json` | Node identity, role, API keys, worker list, CF credentials | Yes (on setup) |
| `services.json` | Registered services with ports and commands | Yes (first service) |
| `ports.env` | Shell-sourceable port env vars (`$INFRA_<NAME>_PORT`) | Yes (auto-generated) |

All files live in `~/.config/inframatik/`.

### Port environment variables

After registering a service, its port is available as an environment variable:

```bash
# Source in your .bashrc:
source ~/.config/inframatik/ports.env

echo $INFRA_MY_APP_PORT     # 8001
curl localhost:$INFRA_MY_APP_PORT/api/health
```

## API Endpoints

### System
- `GET /api/system` — system metrics (CPU, memory, disk, network, GPUs, processes, temps)

### Services
- `GET /api/services` — list all services with status
- `POST /api/services` — register a new service (`{name, command, working_dir, hostname?, lan?}`)
- `DELETE /api/services/{name}` — remove a service
- `POST /api/services/{name}/start|stop|restart` — control a service
- `GET /api/services/{name}/logs` — view service logs
- `GET /api/ports/next` — next available port

### Tunnel
- `GET /api/tunnel` — tunnel connection status

### Cluster
- `GET /api/node/info` — node role and identity
- `GET /api/config` — full node configuration
- `POST /api/config/init-standalone` — configure as standalone
- `POST /api/config/init-master` — configure as master
- `POST /api/config/init-worker` — configure as worker
- `POST /api/config/reset` — reset to unconfigured
- `GET /api/nodes` — list all nodes (master only)
- `POST /api/config/workers` — add a worker (master only)
- `DELETE /api/config/workers/{id}` — remove a worker (master only)
- `POST /api/config/dashboard-access` — put dashboard behind CF Access (`{hostname}`)
- `DELETE /api/config/dashboard-access` — remove dashboard from CF Access

### Deploy
- `GET /api/node/version` — current version info
- `POST /api/update/deploy` — push code to all workers (master only)
- `POST /api/update/deploy-self` — restart this node

## Security

All dashboard and API access requires authentication:

- **Password login** — set during installation or first visit. Sessions default to 24 hours (configurable).
- **CF Access bypass** — when accessed through Cloudflare Access, the CF JWT is validated cryptographically. No additional login needed.
- **API keys** — worker-to-master communication uses API keys exchanged during enrollment.

The install script prompts for an admin password. All mutating API endpoints require a valid session token, CF JWT, or API key.

- **Service tokens** — scoped to a single service (`svc_...`). Can only manage that one service. Ideal for AI agents and CI/CD.

## Agent / AI Integration

AI coding agents can deploy and manage services using scoped service tokens.

### Quick setup

```bash
inframatik init
```

This walks you through: authenticating, creating a service token, writing a `.inframatik` config file, and optionally configuring Claude Code or Codex MCP servers.

### How it works

1. `inframatik init` creates a `.inframatik` file in your repo with:
   - API endpoint and scoped service token
   - Inline instructions the model can read to know how to deploy
2. The model reads `.inframatik` and uses `curl` or the REST API to register, start, stop, and manage the service
3. The service token is scoped — it can only manage the one service it was created for

### Supported agent harnesses

- **Claude Code** — detects `~/.claude`, registers MCP server in `.mcp.json`, appends to `CLAUDE.md`
- **Codex CLI** — detects `~/.codex`, registers MCP server in `.codex/config.toml`, appends to `AGENTS.md`
- **Any agent** — reads `.inframatik` directly for API instructions (no MCP required)

## Service Management

See [USAGE.md](USAGE.md) for detailed documentation on registering, managing, and configuring services.

## License

[MIT](LICENSE)
