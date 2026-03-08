import asyncio
import hashlib
import os
import platform
import re
import tempfile
from pathlib import Path

CLOUDFLARED_BINARY_PATH = Path.home() / ".local" / "bin" / "cloudflared"
CLOUDFLARED_TOKEN_PATH = Path.home() / ".config" / "inframatik" / "cf-tunnel-token"
CLOUDFLARED_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "cloudflared.service"
CLOUDFLARED_UNIT_NAME = "cloudflared.service"
MAX_LOG_LINES = 500
MAX_CLOUDFLARED_DOWNLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_CLOUDFLARED_VERSION = os.getenv("INFRAMATIK_CLOUDFLARED_VERSION", "2025.2.1")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_UNIT_TEMPLATE = """\
[Unit]
Description=cloudflared tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={binary} tunnel run --token-file {token_file}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
"""


def _secure_write_text(path: Path, payload: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _secure_write_bytes(path: Path, payload: bytes, mode: int = 0o755):
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


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode().strip()


async def _run_checked(cmd: list[str], error_prefix: str):
    code, output = await _run(cmd)
    if code != 0:
        detail = output or f"exit code {code}"
        raise RuntimeError(f"{error_prefix}: {detail}")


def _cloudflared_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("armv7l", "armv6l", "arm"):
        return "arm"
    raise ValueError(f"Unsupported architecture for cloudflared update: {machine}")


def _normalize_version(version: str | None) -> str:
    candidate = (version or DEFAULT_CLOUDFLARED_VERSION).strip()
    if not candidate:
        raise ValueError("cloudflared version cannot be empty")
    if not _VERSION_RE.match(candidate):
        raise ValueError("Invalid cloudflared version format")
    return candidate


async def _download_bytes(url: str, max_bytes: int = MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
    try:
        import httpx as _httpx
    except ModuleNotFoundError:
        raise RuntimeError("cloudflared update requires the 'httpx' package")
    try:
        async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
    except _httpx.HTTPError as e:
        raise RuntimeError(f"Failed to download {url}: {e}")
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to download {url}: HTTP {resp.status_code}")
    data = resp.content
    if len(data) > max_bytes:
        raise RuntimeError(f"Downloaded file from {url} exceeds size limit")
    return data


def _extract_sha256(checksum_text: str) -> str:
    match = re.search(r"\b[a-fA-F0-9]{64}\b", checksum_text)
    if not match:
        raise RuntimeError("Could not parse SHA256 checksum")
    return match.group(0).lower()


async def _download_expected_sha(base_url: str, arch: str) -> str:
    checksum_urls = [
        f"{base_url}/cloudflared-linux-{arch}.sha256",
        f"{base_url}/cloudflared-linux-{arch}.sha256sum",
    ]
    last_error = None
    for url in checksum_urls:
        try:
            data = await _download_bytes(url, max_bytes=64 * 1024)
            return _extract_sha256(data.decode("utf-8", errors="replace"))
        except RuntimeError as e:
            last_error = e
    if last_error is not None:
        raise RuntimeError(str(last_error))
    raise RuntimeError("Failed to download cloudflared checksum")


async def get_cloudflared_binary_version() -> str:
    if not CLOUDFLARED_BINARY_PATH.exists() or not os.access(CLOUDFLARED_BINARY_PATH, os.X_OK):
        return ""
    try:
        code, output = await _run([str(CLOUDFLARED_BINARY_PATH), "--version"])
    except OSError:
        return ""
    if code != 0 or not output:
        return ""
    first_line = output.splitlines()[0].strip()
    match = re.search(r"cloudflared version\s+([^\s]+)", first_line, re.IGNORECASE)
    if match:
        return match.group(1)
    return first_line


def _write_cloudflared_unit():
    unit_content = _UNIT_TEMPLATE.format(
        binary=str(CLOUDFLARED_BINARY_PATH),
        token_file=str(CLOUDFLARED_TOKEN_PATH),
    )
    _secure_write_text(CLOUDFLARED_UNIT_PATH, unit_content, mode=0o644)


async def setup_cloudflared_user_service(token: str):
    token = (token or "").strip()
    if not token:
        raise ValueError("Tunnel token is required")

    if not CLOUDFLARED_BINARY_PATH.exists():
        raise RuntimeError(
            f"cloudflared is not installed at {CLOUDFLARED_BINARY_PATH}. "
            "Re-run installer with INSTALL_CF=1."
        )
    if not os.access(CLOUDFLARED_BINARY_PATH, os.X_OK):
        raise RuntimeError(f"cloudflared binary is not executable: {CLOUDFLARED_BINARY_PATH}")

    _secure_write_text(CLOUDFLARED_TOKEN_PATH, token, mode=0o600)
    _write_cloudflared_unit()

    await _run_checked(
        ["systemctl", "--user", "daemon-reload"],
        "Failed to reload user systemd daemon",
    )
    await _run_checked(
        ["systemctl", "--user", "enable", "--now", CLOUDFLARED_UNIT_NAME],
        "Failed to enable/start cloudflared user service",
    )


def _parse_systemctl_show(output: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


async def get_cloudflared_user_service_status() -> dict:
    binary_installed = CLOUDFLARED_BINARY_PATH.exists() and os.access(CLOUDFLARED_BINARY_PATH, os.X_OK)
    binary_version = await get_cloudflared_binary_version()
    token_present = CLOUDFLARED_TOKEN_PATH.exists()
    unit_present = CLOUDFLARED_UNIT_PATH.exists()

    show_cmd = [
        "systemctl",
        "--user",
        "show",
        CLOUDFLARED_UNIT_NAME,
        "--property=ActiveState",
        "--property=SubState",
        "--property=UnitFileState",
        "--property=Result",
        "--property=MainPID",
    ]
    code, output = await _run(show_cmd)
    show_props = _parse_systemctl_show(output) if code == 0 else {}

    active_state = show_props.get("ActiveState", "not-found" if not unit_present else "unknown")
    sub_state = show_props.get("SubState", "not-found" if not unit_present else "unknown")
    unit_file_state = show_props.get("UnitFileState", "not-found" if not unit_present else "unknown")
    result = show_props.get("Result", "")
    main_pid_raw = show_props.get("MainPID", "0")
    try:
        main_pid = int(main_pid_raw)
    except ValueError:
        main_pid = 0

    return {
        "binary_installed": binary_installed,
        "binary_version": binary_version,
        "token_present": token_present,
        "unit_present": unit_present,
        "binary_path": str(CLOUDFLARED_BINARY_PATH),
        "token_path": str(CLOUDFLARED_TOKEN_PATH),
        "unit_path": str(CLOUDFLARED_UNIT_PATH),
        "active_state": active_state,
        "sub_state": sub_state,
        "unit_file_state": unit_file_state,
        "result": result,
        "main_pid": main_pid,
    }


async def get_cloudflared_user_service_logs(lines: int = 80) -> str:
    if lines < 1 or lines > MAX_LOG_LINES:
        raise ValueError(f"lines must be between 1 and {MAX_LOG_LINES}")

    code, output = await _run(
        [
            "journalctl",
            "--user",
            "-u",
            CLOUDFLARED_UNIT_NAME,
            "-n",
            str(lines),
            "--no-pager",
            "--output=short-iso",
        ]
    )
    if code != 0:
        if "No journal files were found" in output or "No entries" in output:
            return ""
        detail = output or f"exit code {code}"
        raise RuntimeError(f"Failed to read cloudflared logs: {detail}")
    return output.strip()


async def restart_cloudflared_user_service() -> dict:
    if not CLOUDFLARED_UNIT_PATH.exists():
        raise RuntimeError(
            f"cloudflared user service is not configured at {CLOUDFLARED_UNIT_PATH}"
        )
    await _run_checked(
        ["systemctl", "--user", "restart", CLOUDFLARED_UNIT_NAME],
        "Failed to restart cloudflared user service",
    )
    return await get_cloudflared_user_service_status()


async def update_cloudflared_user_binary(version: str | None = None) -> dict:
    target_version = _normalize_version(version)
    arch = _cloudflared_arch()
    base_url = f"https://github.com/cloudflare/cloudflared/releases/download/{target_version}"
    binary_url = f"{base_url}/cloudflared-linux-{arch}"

    expected_sha = await _download_expected_sha(base_url, arch)
    binary_data = await _download_bytes(binary_url)
    actual_sha = hashlib.sha256(binary_data).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError("cloudflared checksum verification failed")

    previous_version = await get_cloudflared_binary_version()
    _secure_write_bytes(CLOUDFLARED_BINARY_PATH, binary_data, mode=0o755)

    restarted = False
    if CLOUDFLARED_UNIT_PATH.exists():
        await _run_checked(
            ["systemctl", "--user", "restart", CLOUDFLARED_UNIT_NAME],
            "Failed to restart cloudflared user service after update",
        )
        restarted = True

    current_version = await get_cloudflared_binary_version()
    return {
        "version_requested": target_version,
        "version_before": previous_version or None,
        "version_after": current_version or None,
        "binary_path": str(CLOUDFLARED_BINARY_PATH),
        "sha256": actual_sha,
        "restarted": restarted,
    }
