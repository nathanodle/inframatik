import io
import os
import subprocess
import tarfile
from pathlib import Path

import httpx

APP_DIR = Path(__file__).parent
SERVICE_NAME = "inframatik"

# Files/dirs to include in the update package
INCLUDE_PATTERNS = [
    "*.py",
    "static/*",
    "requirements.txt",
]

# Never include these
EXCLUDE = {"venv", "__pycache__", ".git", ".env", "node.json", "tests", "docs"}


def get_version() -> dict:
    """Return version info from git (if available) or fallback."""
    info = {"commit": None, "branch": None, "dirty": False, "summary": "unknown"}
    try:
        info["commit"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=APP_DIR, timeout=5,
        ).stdout.strip() or None

        info["branch"] = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=APP_DIR, timeout=5,
        ).stdout.strip() or None

        dirty_check = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=APP_DIR, timeout=5,
        )
        info["dirty"] = bool(dirty_check.stdout.strip())

        if info["commit"]:
            info["summary"] = info["commit"]
            if info["dirty"]:
                info["summary"] += " (modified)"
    except Exception:
        pass
    return info


def build_package() -> bytes:
    """Create a tar.gz of the app source files. Returns bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(APP_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(APP_DIR)
            # Skip excluded dirs/files
            if any(part in EXCLUDE for part in rel.parts):
                continue
            # Only include .py files, static/*, requirements.txt, and install.sh
            if (rel.suffix == ".py" or rel.parts[0] == "static"
                    or rel.name in ("requirements.txt", "install.sh")):
                tar.add(path, arcname=str(rel))
    buf.seek(0)
    return buf.read()


def apply_package(data: bytes):
    """Extract a tar.gz package over the app directory."""
    buf = io.BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        # Security: reject any paths that escape the app dir
        for member in tar.getmembers():
            resolved = (APP_DIR / member.name).resolve()
            if not resolved.is_relative_to(APP_DIR.resolve()):
                raise ValueError(f"Unsafe path in package: {member.name}")
        tar.extractall(path=APP_DIR, filter="data")


def restart_service():
    """Restart the inframatik systemd user service."""
    subprocess.run(
        ["systemctl", "--user", "restart", SERVICE_NAME],
        timeout=10,
    )


async def push_update_to_worker(address: str, api_key: str, package: bytes) -> dict:
    """Send update package to a worker node. Returns response dict."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{address}/api/node/update",
                headers={"X-Api-Key": api_key, "Content-Type": "application/octet-stream"},
                content=package,
            )
            return resp.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}
