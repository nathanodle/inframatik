#!/usr/bin/env bash
set -euo pipefail

MASTER_URL="__MASTER_URL__"
PACKAGE_PUBLIC_KEY_B64="__PACKAGE_PUBLIC_KEY_B64__"
INSTALL_DIR="$HOME/inframatik"
CONFIG_DIR="$HOME/.config/inframatik"
SERVICE_NAME="inframatik"
ENROLL_TOKEN=""
NODE_NAME=""
SKIP_CF="${INFRAMATIK_SKIP_CF:-}"
INSTALL_SOURCE_MASTER_URL=""

case "$MASTER_URL" in
    http://*|https://*) INSTALL_SOURCE_MASTER_URL="$MASTER_URL" ;;
esac

usage() {
    echo "Usage: curl ... | bash -s -- [--enroll <token>] [--name <node-name>] [--local-only]"
    echo "       curl ... | bash        # prompts for enrollment token, or Enter to skip"
    echo ""
    echo "Options:"
    echo "  --enroll <token>   Enroll this machine as a worker"
    echo "  --name <name>      Worker node name when enrolling (defaults to system hostname)"
    echo "  --local-only       Skip worker Cloudflare setup even if master has Cloudflare"
    echo "  --skip-cf          Alias for --local-only"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --enroll)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --enroll requires a token"
                exit 1
            fi
            ENROLL_TOKEN="$2"
            shift 2
            ;;
        --name)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --name requires a value"
                exit 1
            fi
            NODE_NAME="$2"
            shift 2
            ;;
        --local-only|--skip-cf)
            SKIP_CF=1
            shift
            ;;
        --password)
            echo "ERROR: --password is no longer supported for security reasons."
            echo "Use interactive prompt input or INFRAMATIK_ADMIN_PASSWORD environment variable."
            exit 1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

for cmd in python3 sudo openssl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: $cmd is required but not installed."
        exit 1
    fi
done

if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: Do not run this script as root."
    echo "Run as a regular user with sudo access instead."
    exit 1
fi

echo "==> Preparing Python environment..."
mkdir -p "$INSTALL_DIR"
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/python" -m pip install -q --disable-pip-version-check "rich==13.9.4"

export INFRAMATIK_MASTER_URL="$MASTER_URL"
export INFRAMATIK_PACKAGE_PUBLIC_KEY_B64="$PACKAGE_PUBLIC_KEY_B64"
export INFRAMATIK_INSTALL_DIR="$INSTALL_DIR"
export INFRAMATIK_CONFIG_DIR="$CONFIG_DIR"
export INFRAMATIK_SERVICE_NAME="$SERVICE_NAME"
export INFRAMATIK_ENROLL_TOKEN="$ENROLL_TOKEN"
export INFRAMATIK_NODE_NAME="$NODE_NAME"
export INFRAMATIK_SKIP_CF="$SKIP_CF"
export INFRAMATIK_INSTALL_SOURCE_MASTER_URL="$INSTALL_SOURCE_MASTER_URL"

"$INSTALL_DIR/venv/bin/python" - <<'PY'
import base64
import os
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn

console = Console()
master_url = os.environ["INFRAMATIK_MASTER_URL"].rstrip("/")
install_dir = Path(os.environ["INFRAMATIK_INSTALL_DIR"]).expanduser()
package_public_key_b64 = os.environ["INFRAMATIK_PACKAGE_PUBLIC_KEY_B64"]


class InstallError(RuntimeError):
    pass


def download_package(url: str, destination: Path) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "inframatik-installer"})
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.URLError as e:
        raise InstallError(f"Failed to download install package: {e}") from e

    with response:
        signature = response.headers.get("X-Inframatik-Package-Signature")
        if not signature:
            raise InstallError("Missing package signature header from master.")

        total_raw = response.headers.get("Content-Length")
        total = int(total_raw) if total_raw and total_raw.isdigit() else None
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Downloading signed code package...", total=total)
            with destination.open("wb") as f:
                while True:
                    chunk = response.read(1024 * 128)
                    if not chunk:
                        break
                    f.write(chunk)
                    progress.update(task, advance=len(chunk))
        return signature


def verify_package(package_path: Path, signature_b64: str, tmpdir: Path):
    pub_path = tmpdir / "package.pub"
    sig_path = tmpdir / "package.sig"
    try:
        pub_path.write_bytes(base64.b64decode(package_public_key_b64, validate=True))
        sig_path.write_bytes(base64.b64decode(signature_b64, validate=True))
    except Exception as e:
        raise InstallError("Invalid package signature encoding.") from e

    result = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-verify",
            "-rawin",
            "-pubin",
            "-inkey",
            str(pub_path),
            "-sigfile",
            str(sig_path),
            "-in",
            str(package_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError("Package signature verification failed.")


def extract_package(package_path: Path):
    install_dir.mkdir(parents=True, exist_ok=True)
    root = install_dir.resolve()
    with tarfile.open(package_path, "r:gz") as tar:
        for member in tar.getmembers():
            target = (install_dir / member.name).resolve()
            if not target.is_relative_to(root):
                raise InstallError(f"Unsafe path in install package: {member.name}")
        tar.extractall(install_dir)


try:
    console.rule("[bold]inframatik code install[/]")
    console.print(f"Master: [bold]{master_url}[/]")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        package_path = tmpdir / "inframatik-package.tgz"
        signature = download_package(f"{master_url}/api/install/package", package_path)
        with console.status("Verifying package signature..."):
            verify_package(package_path, signature, tmpdir)
        with console.status(f"Extracting package to {install_dir}..."):
            extract_package(package_path)
    console.print("[green]Code package installed.[/]")
except InstallError as e:
    console.print(f"[red]ERROR:[/] {e}")
    raise SystemExit(1)
PY

"$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/installer_rich.py"
