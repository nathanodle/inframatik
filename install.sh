#!/usr/bin/env bash
set -euo pipefail

MASTER_URL="__MASTER_URL__"
PACKAGE_PUBLIC_KEY_B64="__PACKAGE_PUBLIC_KEY_B64__"
INSTALL_DIR="$HOME/inframatik"
CONFIG_DIR="$HOME/.config/inframatik"
SERVICE_NAME="inframatik"
ENROLL_TOKEN=""
NODE_NAME=""
ADMIN_PW="${INFRAMATIK_ADMIN_PASSWORD:-}"

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
            echo "ERROR: --password is no longer supported for security reasons."
            echo "Use interactive prompt input or INFRAMATIK_ADMIN_PASSWORD environment variable."
            exit 1
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: curl ... | bash -s -- [--enroll <token>] [--name <node-name>]"
            exit 1
            ;;
    esac
done

echo "==> inframatik installer"
echo "    Master: $MASTER_URL"
echo ""

# Validate and acknowledge env-provided admin password when used.
if [ -n "$ADMIN_PW" ]; then
    if [ ${#ADMIN_PW} -lt 8 ]; then
        echo "ERROR: INFRAMATIK_ADMIN_PASSWORD must be at least 8 characters."
        exit 1
    fi
    echo "==> Using admin password from INFRAMATIK_ADMIN_PASSWORD"
fi

# Prompt for admin password if not provided via env and terminal is interactive.
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
for cmd in python3 curl sudo sha256sum openssl base64; do
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
TMP_PKG="$(mktemp)"
TMP_HEADERS="$(mktemp)"
TMP_PUB="$(mktemp)"
TMP_SIG="$(mktemp)"

if ! curl -fsSL -D "$TMP_HEADERS" -o "$TMP_PKG" "$MASTER_URL/api/install/package"; then
    echo "ERROR: Failed to download install package."
    rm -f "$TMP_PKG" "$TMP_HEADERS" "$TMP_PUB" "$TMP_SIG"
    exit 1
fi

PKG_SIG="$(awk -F': ' 'tolower($1) == "x-inframatik-package-signature" {print $2}' "$TMP_HEADERS" | tr -d '\r' | tail -n 1)"
if [ -z "$PKG_SIG" ]; then
    echo "ERROR: Missing package signature header from master."
    rm -f "$TMP_PKG" "$TMP_HEADERS" "$TMP_PUB" "$TMP_SIG"
    exit 1
fi

if ! printf '%s' "$PACKAGE_PUBLIC_KEY_B64" | base64 -d > "$TMP_PUB"; then
    echo "ERROR: Invalid embedded signing public key."
    rm -f "$TMP_PKG" "$TMP_HEADERS" "$TMP_PUB" "$TMP_SIG"
    exit 1
fi
if ! printf '%s' "$PKG_SIG" | base64 -d > "$TMP_SIG"; then
    echo "ERROR: Invalid package signature encoding."
    rm -f "$TMP_PKG" "$TMP_HEADERS" "$TMP_PUB" "$TMP_SIG"
    exit 1
fi

if ! openssl pkeyutl -verify -rawin -pubin -inkey "$TMP_PUB" -sigfile "$TMP_SIG" -in "$TMP_PKG" >/dev/null 2>&1; then
    echo "ERROR: Package signature verification failed."
    rm -f "$TMP_PKG" "$TMP_HEADERS" "$TMP_PUB" "$TMP_SIG"
    exit 1
fi

tar xzf "$TMP_PKG" -C "$INSTALL_DIR"
rm -f "$TMP_PKG" "$TMP_HEADERS" "$TMP_PUB" "$TMP_SIG"
echo "    Extracted to $INSTALL_DIR"

# 2. Create Python venv and install deps
echo "==> Setting up Python environment..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
REQ_LOCK_FILE="$INSTALL_DIR/requirements.lock"
REQ_FILE="$INSTALL_DIR/requirements.txt"
ALLOW_UNHASHED_DEPS="${INFRAMATIK_ALLOW_UNHASHED_DEPS:-}"

if [ -f "$REQ_LOCK_FILE" ]; then
    if "$INSTALL_DIR/venv/bin/pip" install -q --require-hashes -r "$REQ_LOCK_FILE"; then
        echo "    Installed hash-pinned dependencies from requirements.lock"
    else
        if [ "$ALLOW_UNHASHED_DEPS" = "1" ]; then
            echo "WARNING: Hash-locked install failed; falling back to requirements.txt because INFRAMATIK_ALLOW_UNHASHED_DEPS=1."
            "$INSTALL_DIR/venv/bin/pip" install -q -r "$REQ_FILE"
        else
            echo "ERROR: Hash-locked dependency install failed."
            echo "If this host/platform cannot satisfy requirements.lock, set INFRAMATIK_ALLOW_UNHASHED_DEPS=1 to bypass (not recommended)."
            exit 1
        fi
    fi
else
    if [ "$ALLOW_UNHASHED_DEPS" = "1" ]; then
        echo "WARNING: requirements.lock missing; falling back to requirements.txt because INFRAMATIK_ALLOW_UNHASHED_DEPS=1."
        "$INSTALL_DIR/venv/bin/pip" install -q -r "$REQ_FILE"
    else
        echo "ERROR: requirements.lock is required for secure hash-pinned installs."
        echo "Set INFRAMATIK_ALLOW_UNHASHED_DEPS=1 to bypass (not recommended)."
        exit 1
    fi
fi
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
    CF_USER_BIN="$HOME/.local/bin/cloudflared"
    if [ -x "$CF_USER_BIN" ]; then
        echo "==> cloudflared already installed: $("$CF_USER_BIN" --version 2>&1 | head -1)"
    else
        echo "==> Installing cloudflared..."
        ARCH=$(uname -m)
        case "$ARCH" in
            x86_64)  CF_ARCH="amd64" ;;
            aarch64) CF_ARCH="arm64" ;;
            armv7l)  CF_ARCH="arm" ;;
            *)       echo "    Unsupported architecture: $ARCH"; exit 1 ;;
        esac
        CLOUDFLARED_VERSION="${CLOUDFLARED_VERSION:-2025.2.1}"
        CF_BASE_URL="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}"
        CF_BIN_URL="${CF_BASE_URL}/cloudflared-linux-${CF_ARCH}"
        CF_SHA_URL="${CF_BASE_URL}/cloudflared-linux-${CF_ARCH}.sha256"
        CF_SHA_URL_ALT="${CF_BASE_URL}/cloudflared-linux-${CF_ARCH}.sha256sum"
        TMP_BIN="$(mktemp)"
        TMP_SHA="$(mktemp)"

        if ! curl -fsSL -o "$TMP_BIN" "$CF_BIN_URL"; then
            echo "ERROR: Failed to download cloudflared binary from ${CF_BIN_URL}"
            rm -f "$TMP_BIN" "$TMP_SHA"
            exit 1
        fi
        if ! curl -fsSL -o "$TMP_SHA" "$CF_SHA_URL"; then
            if ! curl -fsSL -o "$TMP_SHA" "$CF_SHA_URL_ALT"; then
                echo "ERROR: Failed to download cloudflared checksum file."
                rm -f "$TMP_BIN" "$TMP_SHA"
                exit 1
            fi
        fi

        EXPECTED_SHA="$(grep -Eo '[0-9a-fA-F]{64}' "$TMP_SHA" | head -n 1 | tr 'A-F' 'a-f')"
        ACTUAL_SHA="$(sha256sum "$TMP_BIN" | awk '{print $1}')"
        if [ -z "$EXPECTED_SHA" ] || [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
            echo "ERROR: cloudflared checksum verification failed."
            rm -f "$TMP_BIN" "$TMP_SHA"
            exit 1
        fi

        mkdir -p "$HOME/.local/bin"
        chmod 0755 "$TMP_BIN"
        mv "$TMP_BIN" "$CF_USER_BIN"
        rm -f "$TMP_BIN" "$TMP_SHA"
        echo "    Installed cloudflared ${CLOUDFLARED_VERSION} to ${CF_USER_BIN}"
    fi
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
    PW_RESULT_FILE="$(mktemp)"
    if ! PW_HTTP_CODE=$(curl -sS -o "$PW_RESULT_FILE" -w "%{http_code}" -X POST "http://127.0.0.1:9000/api/auth/set-password" \
        -H "Content-Type: application/json" \
        -d "$PW_BODY"); then
        echo "ERROR: Failed to call local password setup endpoint."
        rm -f "$PW_RESULT_FILE"
        exit 1
    fi
    if [ "$PW_HTTP_CODE" != "200" ]; then
        PW_DETAIL=$(python3 - "$PW_RESULT_FILE" <<'PY'
import json
import pathlib
import sys
text = pathlib.Path(sys.argv[1]).read_text()
try:
    data = json.loads(text)
except Exception:
    print(text.strip() or "unknown error")
    raise SystemExit(0)
detail = data.get("detail") or data.get("error") or text
print(str(detail).strip() or "unknown error")
PY
)
        echo "ERROR: Failed to set admin password (HTTP $PW_HTTP_CODE): $PW_DETAIL"
        rm -f "$PW_RESULT_FILE"
        exit 1
    fi
    PW_STATUS=$(python3 - "$PW_RESULT_FILE" <<'PY'
import json
import pathlib
import sys
text = pathlib.Path(sys.argv[1]).read_text()
try:
    data = json.loads(text)
except Exception:
    print("")
    raise SystemExit(0)
print(data.get("status", ""))
PY
)
    rm -f "$PW_RESULT_FILE"
    if [ "$PW_STATUS" != "password_set" ]; then
        echo "ERROR: Password setup endpoint returned unexpected response."
        exit 1
    fi
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

    API_KEY=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null || echo "")
    SIGNING_PUBLIC_KEY=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('signing_public_key',''))" 2>/dev/null || echo "")

    if [ -n "$API_KEY" ]; then
        # Configure as worker locally with credentials from master
        CONFIG_BODY=$(python3 -c "import json,sys; print(json.dumps({'name': sys.argv[1], 'master_url': sys.argv[2], 'api_key': sys.argv[3], 'update_public_key': sys.argv[4]}))" "$NODE_NAME" "$MASTER_URL" "$API_KEY" "$SIGNING_PUBLIC_KEY")
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
