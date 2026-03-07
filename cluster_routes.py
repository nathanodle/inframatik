import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from node_config import (
    get_node_config,
    init_as_standalone,
    init_as_master,
    init_as_worker,
    reset_config,
    add_worker,
    remove_worker,
    set_tunnel_id,
    set_dashboard_hostname,
    generate_api_key,
    generate_node_id,
    create_service_token,
    revoke_service_token,
    create_enrollment_token,
    consume_enrollment_token,
    delete_enrollment_token,
)
from nodes import register_node, heartbeat_node, get_all_nodes, unregister_node
from proxy import proxy_to_node
from updater import get_version, build_package, apply_package, restart_service, push_update_to_worker

cluster_router = APIRouter()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class LoginBody(BaseModel):
    password: str


class SetPasswordBody(BaseModel):
    password: str


@cluster_router.get("/api/auth/status")
async def auth_status():
    from node_config import has_admin_password
    return {"has_password": has_admin_password()}


@cluster_router.post("/api/auth/login")
async def auth_login(body: LoginBody):
    from node_config import verify_admin_password
    from auth import create_session
    if not verify_admin_password(body.password):
        raise HTTPException(401, "Invalid password")
    config = get_node_config()
    duration = config.get("session_duration_hours", 24) if config else 24
    token, expires_at = create_session(duration)
    return {"token": token, "expires_at": expires_at}


@cluster_router.post("/api/auth/logout")
async def auth_logout(request: Request):
    from auth import invalidate_session
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        invalidate_session(auth_header[7:])
    return {"status": "logged_out"}


@cluster_router.post("/api/auth/set-password")
async def auth_set_password(body: SetPasswordBody):
    from node_config import has_admin_password, set_admin_password
    if has_admin_password():
        raise HTTPException(400, "Password already set. Use settings to change it.")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    set_admin_password(body.password)
    return {"status": "password_set"}


# ---------------------------------------------------------------------------
# Node identity (all nodes)
# ---------------------------------------------------------------------------

@cluster_router.get("/api/node/info")
async def node_info():
    config = get_node_config()
    if not config:
        return {"role": "unconfigured", "node_name": None, "node_id": None}
    return {
        "role": config["role"],
        "node_name": config["node_name"],
        "node_id": config["node_id"],
    }


@cluster_router.get("/api/node/health")
async def node_health():
    from system import get_system_metrics
    metrics = get_system_metrics()
    return {"status": "ok", "uptime_seconds": metrics["uptime_seconds"]}


# ---------------------------------------------------------------------------
# Setup / config (used by the Settings UI)
# ---------------------------------------------------------------------------

class InitBody(BaseModel):
    name: str


@cluster_router.post("/api/config/init-standalone")
async def config_init_standalone(body: InitBody):
    config = get_node_config()
    if config is not None:
        raise HTTPException(400, "Node already configured. Reset first.")
    new_config = init_as_standalone(body.name)
    return {
        "node_id": new_config["node_id"],
        "node_name": new_config["node_name"],
        "role": "standalone",
    }


@cluster_router.post("/api/config/init-master")
async def config_init_master(body: InitBody):
    config = get_node_config()
    if config is not None and config.get("role") not in ("standalone", None):
        raise HTTPException(400, "Node already configured. Reset first.")
    new_config = init_as_master(body.name)
    return {
        "node_id": new_config["node_id"],
        "node_name": new_config["node_name"],
        "role": "master",
    }


class InitWorkerBody(BaseModel):
    name: str
    master_url: str
    api_key: Optional[str] = None


@cluster_router.post("/api/config/init-worker")
async def config_init_worker(body: InitWorkerBody):
    config = get_node_config()
    if config is not None and config.get("role") not in ("standalone", None):
        raise HTTPException(400, "Node already configured. Reset first.")
    new_config = init_as_worker(body.name, body.master_url, api_key=body.api_key)
    return {
        "node_id": new_config["node_id"],
        "node_name": new_config["node_name"],
        "role": "worker",
        "api_key": new_config["api_key"],
        "master_url": new_config["master_url"],
    }


@cluster_router.post("/api/config/reset")
async def config_reset(request: Request):
    config = get_node_config()
    if config and config.get("api_key"):
        api_key = request.headers.get("X-Api-Key")
        if not api_key or api_key != config["api_key"]:
            raise HTTPException(401, "API key required")
    reset_config()
    return {"status": "reset", "role": "unconfigured"}


@cluster_router.get("/api/config")
async def config_get():
    config = get_node_config()
    if not config:
        return {"role": "unconfigured"}
    result = {
        "role": config["role"],
        "node_id": config["node_id"],
        "node_name": config["node_name"],
    }
    result["tunnel_id"] = config.get("tunnel_id")
    result["dashboard_hostname"] = config.get("dashboard_hostname")
    result["cf_configured"] = bool(config.get("cf_token") and config.get("cf_account_id"))
    if config["role"] == "worker":
        result["api_key"] = config["api_key"]
        result["master_url"] = config["master_url"]
    elif config["role"] == "master":
        result["api_key"] = config["api_key"]
        result["workers"] = config.get("workers", {})
        result["enrollment_tokens"] = list(config.get("enrollment_tokens", {}).keys())
    # Include service tokens summary (no token values)
    svc_tokens = config.get("service_tokens", {})
    result["service_tokens"] = [
        {"service": v["service"], "created_at": v.get("created_at")}
        for v in svc_tokens.values()
    ]
    return result


# --- Dashboard CF Access ---

class DashboardAccessBody(BaseModel):
    hostname: str


@cluster_router.post("/api/config/dashboard-access")
async def config_enable_dashboard_access(body: DashboardAccessBody):
    """Put the dashboard behind Cloudflare Access."""
    from tunnel import (
        _load_cf_config, create_tunnel, get_tunnel_token,
        init_tunnel_config, add_tunnel_route, create_dns_record,
        create_access_app,
    )
    config = get_node_config()
    if not config:
        raise HTTPException(400, "Node not configured")

    cf_cfg = _load_cf_config()
    if not cf_cfg:
        raise HTTPException(400, "Cloudflare not configured. Set up in Settings → Cloudflare.")

    try:
        # Create tunnel if this node doesn't have one yet
        tid = config.get("tunnel_id")
        if not tid:
            tunnel_result = await create_tunnel(config["node_name"])
            tid = tunnel_result["id"]
            await init_tunnel_config(tid)
            set_tunnel_id(tid)

            # Start cloudflared with the tunnel token
            token = await get_tunnel_token(tid)
            import asyncio
            proc = await asyncio.create_subprocess_exec(
                "sudo", "/usr/local/bin/infra-cf-setup", token,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                raise ValueError("Failed to start cloudflared")

        # Add ingress route for the dashboard
        await add_tunnel_route(body.hostname, "http://localhost:9000", tunnel_id=tid)

        # Create DNS record
        await create_dns_record(body.hostname, tunnel_id=tid)

        # Create Access app if default policy is configured
        policy_id = cf_cfg.get("default_policy_id")
        if policy_id:
            await create_access_app("inframatik dashboard", body.hostname, policy_id)

        set_dashboard_hostname(body.hostname)

        return {"status": "enabled", "hostname": body.hostname, "tunnel_id": tid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@cluster_router.delete("/api/config/dashboard-access")
async def config_disable_dashboard_access():
    """Remove the dashboard from Cloudflare Access."""
    from tunnel import remove_tunnel_route, delete_dns_record, delete_access_app

    config = get_node_config()
    if not config:
        raise HTTPException(400, "Node not configured")

    hostname = config.get("dashboard_hostname")
    if not hostname:
        raise HTTPException(404, "Dashboard access not configured")

    try:
        await remove_tunnel_route(hostname)
    except Exception:
        pass
    try:
        await delete_dns_record(hostname)
    except Exception:
        pass
    try:
        await delete_access_app(hostname)
    except Exception:
        pass

    set_dashboard_hostname(None)
    return {"status": "disabled"}


# --- Worker management (master only) ---

class AddWorkerBody(BaseModel):
    name: str
    address: str
    api_key: str


@cluster_router.post("/api/config/workers")
async def config_add_worker(body: AddWorkerBody):
    try:
        node_id = add_worker(body.name, body.address, body.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"node_id": node_id, "status": "added"}


@cluster_router.delete("/api/config/workers/{node_id}")
async def config_remove_worker(node_id: str):
    try:
        remove_worker(node_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    unregister_node(node_id)
    return {"status": "removed"}


# --- Enrollment tokens ---

@cluster_router.post("/api/config/enrollment-tokens")
async def config_create_enrollment_token():
    try:
        token = create_enrollment_token()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"token": token}


@cluster_router.delete("/api/config/enrollment-tokens/{token}")
async def config_delete_enrollment_token(token: str):
    delete_enrollment_token(token)
    return {"status": "deleted"}


# --- Service tokens ---

class ServiceTokenBody(BaseModel):
    service: str


@cluster_router.post("/api/config/service-tokens")
async def config_create_service_token(body: ServiceTokenBody):
    try:
        token = create_service_token(body.service)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"token": token, "service": body.service}


@cluster_router.delete("/api/config/service-tokens/{token}")
async def config_revoke_service_token(token: str):
    revoke_service_token(token)
    return {"status": "revoked"}


class EnrollBody(BaseModel):
    token: str
    node_name: str
    address: str


@cluster_router.post("/api/nodes/enroll")
async def enroll_worker(body: EnrollBody, request: Request):
    """Worker presents an enrollment token to register with the master."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(400, "Not a master node")

    if not consume_enrollment_token(body.token):
        raise HTTPException(401, "Invalid or expired enrollment token")

    # Generate credentials for the worker
    worker_api_key = generate_api_key()

    # Store worker in master config
    try:
        add_worker(body.node_name, body.address, worker_api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Build master URL from the incoming request
    host = request.headers.get("host", "localhost:9000")
    scheme = request.headers.get("x-forwarded-proto", "http")
    master_url = f"{scheme}://{host}"

    return {
        "status": "enrolled",
        "api_key": worker_api_key,
        "master_url": master_url,
    }


# ---------------------------------------------------------------------------
# Registration / heartbeat (master receives from workers)
# ---------------------------------------------------------------------------

class RegisterBody(BaseModel):
    node_id: str
    node_name: str
    address: str


@cluster_router.post("/api/nodes/register")
async def register(body: RegisterBody, request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        raise HTTPException(401, "API key required")
    ok = register_node(body.node_id, body.node_name, body.address, api_key)
    if not ok:
        raise HTTPException(401, "Unknown API key")
    return {"status": "registered"}


class HeartbeatBody(BaseModel):
    node_id: str


@cluster_router.post("/api/nodes/heartbeat")
async def heartbeat(body: HeartbeatBody, request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        raise HTTPException(401, "API key required")
    ok = heartbeat_node(body.node_id)
    if not ok:
        raise HTTPException(404, "Node not registered — re-register required")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Node list (master)
# ---------------------------------------------------------------------------

@cluster_router.get("/api/nodes")
async def list_nodes():
    return await get_all_nodes()


# ---------------------------------------------------------------------------
# Proxy endpoints (master forwards to workers)
# ---------------------------------------------------------------------------

@cluster_router.get("/api/nodes/{node_id}/system")
async def proxy_system(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/system")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/services")
async def proxy_services(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/services")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/services")
async def proxy_create_service(node_id: str, body: dict):
    try:
        return await proxy_to_node(node_id, "POST", "/api/services", body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@cluster_router.delete("/api/nodes/{node_id}/services/{name}")
async def proxy_delete_service(node_id: str, name: str):
    try:
        return await proxy_to_node(node_id, "DELETE", f"/api/services/{name}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.post("/api/nodes/{node_id}/services/{name}/{action}")
async def proxy_service_action(node_id: str, name: str, action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, f"Unknown action: {action}")
    try:
        return await proxy_to_node(node_id, "POST", f"/api/services/{name}/{action}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/services/{name}/logs")
async def proxy_service_logs(node_id: str, name: str, lines: int = 100):
    try:
        return await proxy_to_node(node_id, "GET", f"/api/services/{name}/logs?lines={lines}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@cluster_router.get("/api/nodes/{node_id}/tunnel")
async def proxy_tunnel(node_id: str):
    try:
        return await proxy_to_node(node_id, "GET", "/api/tunnel")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


# ---------------------------------------------------------------------------
# Updates / deployment
# ---------------------------------------------------------------------------

@cluster_router.get("/api/node/version")
async def node_version():
    return get_version()


@cluster_router.post("/api/node/update")
async def node_update(request: Request):
    """Receive an update package (tar.gz bytes), apply it, and restart."""
    api_key = request.headers.get("X-Api-Key")
    config = get_node_config()
    if not api_key or not config or api_key != config.get("api_key"):
        raise HTTPException(401, "API key required")
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty package")
    try:
        apply_package(body)
    except Exception as e:
        raise HTTPException(400, f"Failed to apply package: {e}")
    # Restart in background so the response gets sent first
    asyncio.get_event_loop().call_later(1, restart_service)
    return {"status": "updated", "detail": "Restarting..."}


@cluster_router.post("/api/update/deploy")
async def deploy_to_workers():
    """Master-only: package local code and push to all workers."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only master can deploy updates")

    package = build_package()
    workers = config.get("workers", {})
    results = {}

    tasks = []
    for node_id, worker in workers.items():
        tasks.append((node_id, worker["name"], push_update_to_worker(
            worker["address"], worker["api_key"], package
        )))

    for node_id, name, coro in tasks:
        result = await coro
        results[node_id] = {"name": name, **result}

    return {"status": "deployed", "workers": results}


@cluster_router.post("/api/update/deploy-self")
async def deploy_self():
    """Restart this node (useful after local code changes on master)."""
    asyncio.get_event_loop().call_later(1, restart_service)
    return {"status": "restarting"}


# ---------------------------------------------------------------------------
# Install script (master serves to new machines)
# ---------------------------------------------------------------------------

INSTALL_SCRIPT = r'''#!/usr/bin/env bash
set -euo pipefail

MASTER_URL="__MASTER_URL__"
INSTALL_DIR="$HOME/inframatik"
CONFIG_DIR="$HOME/.config/inframatik"
SERVICE_NAME="inframatik"
ENROLL_TOKEN=""
NODE_NAME=""
ADMIN_PW=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --enroll)
            ENROLL_TOKEN="$2"
            shift 2
            ;;
        --name)
            NODE_NAME="$2"
            shift 2
            ;;
        --password)
            ADMIN_PW="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: curl ... | bash -s -- [--enroll <token>] [--name <node-name>] [--password <pw>]"
            exit 1
            ;;
    esac
done

echo "==> inframatik installer"
echo "    Master: $MASTER_URL"
echo ""

# Prompt for admin password if not provided and terminal is interactive
if [ -z "$ADMIN_PW" ] && [ -t 0 ]; then
    while true; do
        read -sp "Set admin password (min 8 chars): " ADMIN_PW
        echo
        if [ ${#ADMIN_PW} -lt 8 ]; then
            echo "    Password too short. Minimum 8 characters."
            ADMIN_PW=""
            continue
        fi
        read -sp "Confirm password: " ADMIN_PW_CONFIRM
        echo
        if [ "$ADMIN_PW" != "$ADMIN_PW_CONFIRM" ]; then
            echo "    Passwords do not match."
            ADMIN_PW=""
            continue
        fi
        break
    done
fi

# Check prerequisites
for cmd in python3 curl sudo; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd is required but not installed."
        exit 1
    fi
done

# Check for root — this script should run as a regular user with sudo access
if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: Do not run this script as root."
    echo "Run as a regular user with sudo access instead."
    exit 1
fi

# Validate sudo access upfront (caches credentials for subsequent calls)
if ! sudo -v 2>/dev/null; then
    echo "ERROR: sudo access is required. Please ensure your user can run sudo."
    exit 1
fi

# 1. Download and extract code
echo "==> Downloading code package..."
mkdir -p "$INSTALL_DIR"
curl -sSL "$MASTER_URL/api/install/package" | tar xz -C "$INSTALL_DIR"
echo "    Extracted to $INSTALL_DIR"

# 2. Create Python venv and install deps
echo "==> Setting up Python environment..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
echo "    Dependencies installed"

# 3. Create config directory
mkdir -p "$CONFIG_DIR"

CURRENT_USER=$(whoami)

# 4. Optional: Cloudflare tunnel support
INSTALL_CF="${INSTALL_CF:-}"
if [ -z "$INSTALL_CF" ]; then
    if [ -t 0 ]; then
        read -p "Install Cloudflare tunnel support? [y/N] " -n 1 -r
        echo
        [[ $REPLY =~ ^[Yy]$ ]] && INSTALL_CF=1
    else
        echo "==> Skipping Cloudflare setup (non-interactive)"
        echo "    To include CF support: INSTALL_CF=1 curl ... | bash"
    fi
fi

if [ -n "$INSTALL_CF" ]; then
    if command -v cloudflared &>/dev/null; then
        echo "==> cloudflared already installed: $(cloudflared --version 2>&1 | head -1)"
    else
        echo "==> Installing cloudflared..."
        ARCH=$(uname -m)
        case "$ARCH" in
            x86_64)  CF_ARCH="amd64" ;;
            aarch64) CF_ARCH="arm64" ;;
            armv7l)  CF_ARCH="arm" ;;
            *)       echo "    Unsupported architecture: $ARCH"; exit 1 ;;
        esac
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
        sudo curl -sSL -o /usr/local/bin/cloudflared "$CF_URL"
        sudo chmod +x /usr/local/bin/cloudflared
        echo "    Installed cloudflared to /usr/local/bin/cloudflared"
    fi

    echo "==> Installing CF setup helper..."
    sudo tee /usr/local/bin/infra-cf-setup > /dev/null << 'HELPER_EOF'
#!/bin/bash
# infra-cf-setup: accepts a tunnel token, writes config, starts cloudflared
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: infra-cf-setup <tunnel-token>"
    exit 1
fi

TOKEN="$1"
CF_DIR="/etc/cloudflared"

mkdir -p "$CF_DIR"
echo "$TOKEN" > "$CF_DIR/token"
chmod 600 "$CF_DIR/token"

# Create systemd service for cloudflared
cat > /etc/systemd/system/cloudflared.service << 'SVC'
[Unit]
Description=cloudflared tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared tunnel run --token-file /etc/cloudflared/token
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable cloudflared
systemctl restart cloudflared
echo "cloudflared service started"
HELPER_EOF
    sudo chmod +x /usr/local/bin/infra-cf-setup

    echo "==> Configuring sudoers for CF helper..."
    sudo tee /etc/sudoers.d/infra-cf > /dev/null << SUDOERS_EOF
${CURRENT_USER} ALL=(root) NOPASSWD: /usr/local/bin/infra-cf-setup
SUDOERS_EOF
    sudo chmod 440 /etc/sudoers.d/infra-cf
fi

# 5. Add ports.env to .bashrc
if ! grep -q 'inframatik/ports.env' "$HOME/.bashrc" 2>/dev/null; then
    echo '' >> "$HOME/.bashrc"
    echo '# inframatik service ports' >> "$HOME/.bashrc"
    echo '[ -f ~/.config/inframatik/ports.env ] && source ~/.config/inframatik/ports.env' >> "$HOME/.bashrc"
    echo "==> Added ports.env to .bashrc"
fi

# 6. Create systemd user service
echo "==> Setting up systemd service..."
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/${SERVICE_NAME}.service" << SVC_EOF
[Unit]
Description=inframatik
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn main:app --host 0.0.0.0 --port 9000
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
SVC_EOF

# 7. Enable linger so user services start at boot
echo "==> Enabling linger for $CURRENT_USER..."
sudo loginctl enable-linger "$CURRENT_USER"

# 8. Install CLI
echo "==> Installing CLI..."
chmod +x "$INSTALL_DIR/inframatik-cli.py"
mkdir -p "$HOME/.local/bin"
ln -sf "$INSTALL_DIR/inframatik-cli.py" "$HOME/.local/bin/inframatik"
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo "    Added ~/.local/bin to PATH (open a new terminal to use 'inframatik' command)"
fi

# 9. Enable and start the service

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"
echo "==> inframatik started on port 9000"

# 10. Set admin password
if [ -n "$ADMIN_PW" ]; then
    sleep 2
    PW_BODY=$(python3 -c "import json,sys; print(json.dumps({'password': sys.argv[1]}))" "$ADMIN_PW")
    PW_RESULT=$(curl -sS -X POST "http://127.0.0.1:9000/api/auth/set-password" \
        -H "Content-Type: application/json" \
        -d "$PW_BODY")
    echo "==> Admin password set"
fi

# 11. Optional: enroll as worker with master
if [ -n "$ENROLL_TOKEN" ]; then
    [ -z "$NODE_NAME" ] && NODE_NAME=$(hostname -s)
    echo "==> Enrolling with master as: $NODE_NAME"
    sleep 3  # Give the service a moment to start

    # Determine our routable IP
    MASTER_HOST=$(echo "$MASTER_URL" | sed 's|https\?://||;s|:.*||;s|/.*||')
    LOCAL_IP=$(python3 -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('$MASTER_HOST',1)); print(s.getsockname()[0]); s.close()" 2>/dev/null || hostname -I | awk '{print $1}')
    WORKER_ADDRESS="http://${LOCAL_IP}:9000"

    # Call master's enrollment endpoint
    ENROLL_BODY=$(python3 -c "import json,sys; print(json.dumps({'token': sys.argv[1], 'node_name': sys.argv[2], 'address': sys.argv[3]}))" "$ENROLL_TOKEN" "$NODE_NAME" "$WORKER_ADDRESS")
    RESULT=$(curl -sS -X POST "$MASTER_URL/api/nodes/enroll" \
        -H "Content-Type: application/json" \
        -d "$ENROLL_BODY")

    API_KEY=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])" 2>/dev/null || echo "")

    if [ -n "$API_KEY" ]; then
        # Configure as worker locally with credentials from master
        CONFIG_BODY=$(python3 -c "import json,sys; print(json.dumps({'name': sys.argv[1], 'master_url': sys.argv[2], 'api_key': sys.argv[3]}))" "$NODE_NAME" "$MASTER_URL" "$API_KEY")
        curl -sS -X POST "http://127.0.0.1:9000/api/config/init-worker" \
            -H "Content-Type: application/json" \
            -d "$CONFIG_BODY" > /dev/null

        # Restart so heartbeat loop picks up worker config
        systemctl --user restart "$SERVICE_NAME"
        echo ""
        echo "============================================"
        echo "  Enrolled: $NODE_NAME"
        echo "  Master:   $MASTER_URL"
        echo "  Address:  $WORKER_ADDRESS"
        echo "  Connected automatically — no manual setup needed."
        echo "============================================"
    else
        echo "    Enrollment failed: $RESULT"
        echo "    Check that the enrollment token is valid."
    fi
fi

echo ""
echo "==> Done! inframatik is running at http://$(hostname -I | awk '{print $1}'):9000"
'''


@cluster_router.get("/api/install.sh")
async def install_script(request: Request):
    """Serve the install script with the master URL embedded."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only master can serve install scripts")

    # Build master URL from the request
    host = request.headers.get("host", "localhost:9000")
    scheme = request.headers.get("x-forwarded-proto", "http")
    master_url = f"{scheme}://{host}"

    script = INSTALL_SCRIPT.replace("__MASTER_URL__", master_url)
    return PlainTextResponse(script, media_type="text/plain")


@cluster_router.get("/api/install/package")
async def install_package():
    """Serve the code package for new installs."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise HTTPException(403, "Only master can serve install packages")

    package = build_package()
    return Response(content=package, media_type="application/gzip")
