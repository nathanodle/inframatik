import asyncio
import os
import tempfile
from pathlib import Path

CLOUDFLARED_BINARY_PATH = Path.home() / ".local" / "bin" / "cloudflared"
CLOUDFLARED_TOKEN_PATH = Path.home() / ".config" / "inframatik" / "cf-tunnel-token"
CLOUDFLARED_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "cloudflared.service"
CLOUDFLARED_UNIT_NAME = "cloudflared.service"
MAX_LOG_LINES = 500

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
