import base64
import hashlib
import io
import logging
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from node_config import assert_worker_address_allowed

logger = logging.getLogger("inframatik.updater")

APP_DIR = Path(__file__).parent
SERVICE_NAME = "inframatik"
SIGNING_DIR = Path(
    os.getenv(
        "INFRAMATIK_SIGNING_DIR",
        str(Path.home() / ".config" / "inframatik" / "signing"),
    )
)
SIGNING_PRIVATE_KEY = SIGNING_DIR / "update_signing_key.pem"
SIGNING_PUBLIC_KEY = SIGNING_DIR / "update_signing_key.pub.pem"

# Never include these
EXCLUDE = {"venv", "__pycache__", ".git", ".env", "node.json", "tests", "docs"}


def _secure_write_bytes(path: Path, payload: bytes, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def ensure_signing_keypair() -> tuple[bytes, bytes]:
    """Ensure an Ed25519 keypair exists and return (private_pem, public_pem)."""
    if SIGNING_PRIVATE_KEY.exists() and SIGNING_PUBLIC_KEY.exists():
        return SIGNING_PRIVATE_KEY.read_bytes(), SIGNING_PUBLIC_KEY.read_bytes()

    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    _secure_write_bytes(SIGNING_PRIVATE_KEY, private_pem, 0o600)
    _secure_write_bytes(SIGNING_PUBLIC_KEY, public_pem, 0o644)
    return private_pem, public_pem


def get_signing_public_key_pem() -> str:
    _, public_pem = ensure_signing_keypair()
    return public_pem.decode()


def get_signing_public_key_b64() -> str:
    _, public_pem = ensure_signing_keypair()
    return base64.b64encode(public_pem).decode()


def sign_package(data: bytes) -> dict:
    private_pem, public_pem = ensure_signing_keypair()
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise ValueError("Update signing key must be Ed25519")
    signature = private_key.sign(data)
    key_id = hashlib.sha256(public_pem).hexdigest()[:16]
    return {
        "signature_b64": base64.b64encode(signature).decode(),
        "key_id": key_id,
        "signed_at": int(time.time()),
    }


def verify_package_signature(data: bytes, signature_b64: str, public_key_pem: str) -> bool:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        if not isinstance(public_key, ed25519.Ed25519PublicKey):
            return False
        public_key.verify(signature, data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


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
    except Exception as e:
        logger.debug("Failed to read git version metadata: %s", e)
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
            # Only include .py files, static/*, dependency files, and install.sh
            if (rel.suffix == ".py" or rel.parts[0] == "static"
                    or rel.name in ("requirements.txt", "requirements.lock", "install.sh")):
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


async def push_update_to_worker(
    address: str,
    api_key: str,
    package: bytes,
    signature_b64: str = "",
    key_id: str = "",
) -> dict:
    """Send update package to a worker node. Returns response dict."""
    try:
        address = assert_worker_address_allowed(address)
    except ValueError as e:
        return {"status": "error", "detail": f"Invalid worker address: {e}"}

    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/octet-stream",
    }
    if signature_b64:
        headers["X-Inframatik-Package-Signature"] = signature_b64
    if key_id:
        headers["X-Inframatik-Package-Key-Id"] = key_id

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{address}/api/node/update",
                headers=headers,
                content=package,
            )
    except httpx.HTTPError as e:
        return {"status": "error", "detail": str(e)}

    try:
        data = resp.json()
    except ValueError:
        return {
            "status": "error",
            "detail": f"Worker returned non-JSON response (HTTP {resp.status_code})",
        }

    if not isinstance(data, dict):
        return {
            "status": "error",
            "detail": f"Worker returned unexpected response type (HTTP {resp.status_code})",
        }
    if resp.status_code >= 400:
        detail = data.get("detail") if isinstance(data.get("detail"), str) else str(data)
        return {"status": "error", "detail": detail}
    return data
