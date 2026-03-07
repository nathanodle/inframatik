#!/usr/bin/env bash
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
