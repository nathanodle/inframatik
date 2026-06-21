#!/usr/bin/env python3
"""Rich-powered post-extraction installer steps for inframatik."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table


console = Console()


class InstallerError(RuntimeError):
    pass


def env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y"}


def require_command(name: str):
    if shutil.which(name) is None:
        raise InstallerError(f"{name} is required but not installed.")


def run_command(cmd: list[str], label: str, *, cwd: Path | None = None, capture: bool = True) -> str:
    kwargs = {
        "cwd": str(cwd) if cwd else None,
        "text": True,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    result = subprocess.run(cmd, **kwargs)
    output = (result.stdout or "").strip() if capture else ""
    if result.returncode != 0:
        detail = f"\n{output}" if output else ""
        raise InstallerError(f"{label} failed.{detail}")
    return output


def run_status(message: str, cmd: list[str], *, cwd: Path | None = None, capture: bool = True) -> str:
    with console.status(message):
        return run_command(cmd, message, cwd=cwd, capture=capture)


def request_json(
    url: str,
    body: dict | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    data = None
    method = "GET"
    req_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        method = "POST"
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
            detail = payload.get("detail") or payload.get("error") or raw
        except Exception:
            detail = raw or str(e)
        raise InstallerError(f"{url} returned HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise InstallerError(f"Failed to reach {url}: {e}") from e
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise InstallerError(f"{url} returned invalid JSON") from e
    if not isinstance(payload, dict):
        raise InstallerError(f"{url} returned unexpected JSON")
    return payload


def wait_for_local_service():
    deadline = time.time() + 20
    last_error = None
    while time.time() < deadline:
        try:
            request_json("http://127.0.0.1:9000/api/node/info", timeout=2)
            return
        except InstallerError as e:
            last_error = e
            time.sleep(0.5)
    raise InstallerError(f"inframatik did not start on port 9000: {last_error}")


def prompt_admin_password(initial: str) -> str:
    if initial:
        if len(initial) < 8:
            raise InstallerError("INFRAMATIK_ADMIN_PASSWORD must be at least 8 characters.")
        return initial
    if not Path("/dev/tty").exists():
        console.print(
            "[yellow]No interactive terminal found for password setup. "
            "Set INFRAMATIK_ADMIN_PASSWORD to configure it during install.[/]"
        )
        return ""
    while True:
        password = getpass.getpass("Set admin password (min 8 chars): ")
        if len(password) < 8:
            console.print("[yellow]Password too short. Minimum 8 characters.[/]")
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            console.print("[yellow]Passwords do not match.[/]")
            continue
        return password


def prompt_from_tty(prompt: str) -> str | None:
    fd = None
    try:
        fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
        os.write(fd, prompt.encode("utf-8", errors="replace"))
        chunks = []
        while True:
            chunk = os.read(fd, 1)
            if not chunk:
                return None
            if chunk in (b"\n", b"\r"):
                break
            chunks.append(chunk)
    except OSError:
        if sys.stdin.isatty():
            try:
                return input(prompt)
            except EOFError:
                return None
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    return b"".join(chunks).decode("utf-8", errors="replace")


def prompt_yes_no_from_tty(prompt: str, default: bool = False) -> bool | None:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = prompt_from_tty(prompt + suffix)
        if answer is None:
            return None
        answer = answer.strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        console.print("[yellow]Please answer yes or no.[/]")


def default_worker_name() -> str:
    return socket.gethostname().split(".", 1)[0] or "worker"


def maybe_prompt_worker_enrollment(
    master_url: str,
    enroll_token: str,
    node_name: str,
    skip_cf: bool,
    *,
    prompt_text=prompt_from_tty,
    prompt_bool=prompt_yes_no_from_tty,
) -> tuple[str, str, bool]:
    if enroll_token:
        return enroll_token, node_name, skip_cf
    if not normalize_install_source_master_url(master_url):
        return enroll_token, node_name, skip_cf

    console.print()
    console.print("[bold]Worker enrollment[/]")
    console.print("Paste an enrollment token to register this machine now, or press Enter to skip.")
    token = prompt_text("Enrollment token: ")
    if token is None:
        console.print("[yellow]No interactive terminal found for enrollment. Skipping worker enrollment.[/]")
        return enroll_token, node_name, skip_cf

    token = token.strip()
    if not token or token.lower() == "skip":
        return "", node_name, skip_cf

    default_name = node_name or default_worker_name()
    entered_name = prompt_text(f"Worker name [{default_name}]: ")
    if entered_name is None:
        entered_name = ""
    node_name = entered_name.strip() or default_name

    local_only = prompt_bool("Local-only worker mode (skip Cloudflare setup if available)?", default=skip_cf)
    if local_only is not None:
        skip_cf = local_only
    return token, node_name, skip_cf


def setup_systemd_service(install_dir: Path, service_name: str):
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / f"{service_name}.service"
    unit_path.write_text(
        f"""[Unit]
Description=inframatik
After=network-online.target

[Service]
Type=simple
WorkingDirectory={install_dir}
ExecStart={install_dir}/venv/bin/uvicorn main:app --host 0.0.0.0 --port 9000
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
"""
    )


def setup_shell_paths(config_dir: Path):
    config_dir.mkdir(parents=True, exist_ok=True)
    bashrc = Path.home() / ".bashrc"
    marker = "inframatik/ports.env"
    existing = bashrc.read_text() if bashrc.exists() else ""
    if marker not in existing:
        with bashrc.open("a") as f:
            f.write("\n# inframatik service ports\n")
            f.write("[ -f ~/.config/inframatik/ports.env ] && source ~/.config/inframatik/ports.env\n")

    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    if str(local_bin) not in os.getenv("PATH", ""):
        with bashrc.open("a") as f:
            f.write('export PATH="$HOME/.local/bin:$PATH"\n')


def normalize_install_source_master_url(raw: str) -> str:
    value = (raw or "").strip().rstrip("/")
    if not value or value.startswith("__"):
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value


def persist_install_source_master(config_dir: Path, source_url: str):
    source_url = normalize_install_source_master_url(source_url)
    if not source_url:
        return

    node_path = config_dir / "node.json"
    payload = {}
    if node_path.exists():
        try:
            existing = json.loads(node_path.read_text())
            if isinstance(existing, dict):
                payload = existing
        except Exception:
            console.print("[yellow]Could not read existing node config; skipping master prefill.[/]")
            return

    if payload.get("role") in {"master", "standalone", "worker"}:
        return

    payload["install_source_master_url"] = source_url
    node_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{node_path.name}.", suffix=".tmp", dir=str(node_path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, node_path)
        os.chmod(node_path, 0o600)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def install_cli(install_dir: Path):
    cli = install_dir / "inframatik-cli.py"
    cli.chmod(0o755)
    target = Path.home() / ".local" / "bin" / "inframatik"
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(cli)


def install_dependencies(install_dir: Path):
    pip = install_dir / "venv" / "bin" / "pip"
    req_lock = install_dir / "requirements.lock"
    req = install_dir / "requirements.txt"
    allow_unhashed = env_bool("INFRAMATIK_ALLOW_UNHASHED_DEPS")
    if req_lock.exists():
        try:
            run_status(
                "Installing hash-pinned Python dependencies...",
                [str(pip), "install", "-q", "--require-hashes", "-r", str(req_lock)],
            )
            return
        except InstallerError:
            if not allow_unhashed:
                raise
            console.print(
                "[yellow]Hash-locked install failed; falling back to requirements.txt because "
                "INFRAMATIK_ALLOW_UNHASHED_DEPS=1.[/]"
            )
    elif not allow_unhashed:
        raise InstallerError(
            "requirements.lock is required for secure hash-pinned installs. "
            "Set INFRAMATIK_ALLOW_UNHASHED_DEPS=1 to bypass."
        )

    run_status("Installing Python dependencies...", [str(pip), "install", "-q", "-r", str(req)])


def worker_address_for_master(master_url: str) -> str:
    parsed = urlparse(master_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InstallerError("Master URL must be an http(s) base URL.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        family = socket.AF_INET6 if ":" in parsed.hostname else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect((parsed.hostname, port))
            local_ip = sock.getsockname()[0]
    except OSError:
        local_ip = socket.gethostbyname(socket.gethostname())
    host_part = f"[{local_ip}]" if ":" in local_ip and not local_ip.startswith("[") else local_ip
    return f"http://{host_part}:9000"


def save_cf_config_payload(cf_config: dict):
    from node_config import save_cf_config

    token = (cf_config.get("token") or "").strip()
    account_id = (cf_config.get("account_id") or "").strip()
    if not token or not account_id:
        raise InstallerError("Master returned incomplete Cloudflare config.")
    save_cf_config(
        token,
        account_id,
        (cf_config.get("zone_id") or "").strip(),
        cf_config.get("default_policy_id"),
        team_domain=cf_config.get("team_domain"),
        access_issuer=cf_config.get("access_issuer"),
    )


async def setup_worker_tunnel(
    node_name: str,
    master_url: str,
    api_key: str,
    progress: Progress,
    task_id,
) -> tuple[dict | None, str | None]:
    from cloudflared import setup_cloudflared_user_service
    from node_config import set_tunnel_id
    from tunnel import create_tunnel, get_tunnel_token, init_tunnel_config

    try:
        progress.update(task_id, completed=48, description="Creating worker Cloudflare tunnel...")
        tunnel_result = await create_tunnel(node_name)
        tunnel_id = tunnel_result["id"]

        progress.update(task_id, completed=58, description="Initializing worker tunnel routing...")
        await init_tunnel_config(tunnel_id)

        progress.update(task_id, completed=66, description="Getting tunnel connector token...")
        token = await get_tunnel_token(tunnel_id)

        cloudflared_steps = {
            "cloudflared_present": 78,
            "downloading_binary": 74,
            "verifying_checksum": 80,
            "installing_binary": 84,
            "writing_config": 87,
            "reloading_systemd": 89,
            "starting_service": 92,
            "restarting_service": 92,
        }

        async def cloudflared_progress(step: str, message: str):
            progress.update(
                task_id,
                completed=cloudflared_steps.get(step, 86),
                description=message,
            )

        await setup_cloudflared_user_service(token, progress=cloudflared_progress)
        set_tunnel_id(tunnel_id)

        progress.update(task_id, completed=94, description="Reporting tunnel to master...")
        try:
            request_json(
                f"{master_url}/api/nodes/tunnel",
                {"tunnel_id": tunnel_id},
                headers={"X-Api-Key": api_key},
            )
            master_report_error = None
        except InstallerError as e:
            master_report_error = str(e)

        result = {
            "tunnel_id": tunnel_id,
            "tunnel_name": tunnel_result.get("name") or node_name,
        }
        if master_report_error:
            result["master_report_error"] = master_report_error
        return result, None
    except Exception as e:
        return None, str(e)


async def enroll_worker(install_dir: Path, master_url: str, token: str, node_name: str, skip_cf: bool):
    sys.path.insert(0, str(install_dir))
    from node_config import init_as_worker

    node_name = node_name or default_worker_name()
    worker_address = worker_address_for_master(master_url)
    cf_tunnel = None
    cf_tunnel_error = None

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    )
    with progress:
        task = progress.add_task("Contacting master...", total=100)
        enrollment = request_json(
            f"{master_url}/api/nodes/enroll",
            {
                "token": token,
                "node_name": node_name,
                "address": worker_address,
                "skip_cf": skip_cf,
            },
        )
        api_key = enrollment.get("api_key")
        if not api_key:
            raise InstallerError("Master returned an invalid enrollment response.")

        progress.update(task, completed=25, description="Saving worker configuration...")
        config = init_as_worker(
            node_name,
            master_url,
            api_key=api_key,
            update_public_key=enrollment.get("signing_public_key"),
        )

        cf_config = enrollment.get("cf_config")
        if skip_cf:
            progress.update(task, completed=100, description="Local-only worker selected; skipping Cloudflare.")
        elif isinstance(cf_config, dict):
            progress.update(task, completed=38, description="Saving Cloudflare configuration...")
            save_cf_config_payload(cf_config)
            cf_tunnel, cf_tunnel_error = await setup_worker_tunnel(
                config["node_name"],
                config["master_url"],
                config["api_key"],
                progress,
                task,
            )
            if cf_tunnel_error:
                progress.update(task, completed=100, description="Cloudflare setup needs attention.")
            else:
                progress.update(task, completed=100, description="Worker registration complete.")
        else:
            progress.update(task, completed=100, description="Master is local-only; skipping Cloudflare.")

    return {
        "node_name": config["node_name"],
        "master_url": config["master_url"],
        "worker_address": worker_address,
        "cf_tunnel": cf_tunnel,
        "cf_tunnel_error": cf_tunnel_error,
        "skip_cf": skip_cf,
    }


def setup_admin_password(admin_password: str):
    if not admin_password:
        return
    try:
        request_json("http://127.0.0.1:9000/api/auth/set-password", {"password": admin_password})
    except InstallerError as e:
        if "password already set" in str(e).lower():
            console.print("[yellow]Admin password already set; continuing.[/]")
            return
        raise


def display_summary(install_dir: Path, enrollment: dict | None):
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Install dir", str(install_dir))
    if enrollment:
        table.add_row("Worker", enrollment["node_name"])
        table.add_row("Master", enrollment["master_url"])
        table.add_row("Address", enrollment["worker_address"])
        if enrollment.get("skip_cf"):
            table.add_row("Cloudflare", "local-only opt-out")
        elif enrollment.get("cf_tunnel"):
            table.add_row("Cloudflare", f"tunnel {enrollment['cf_tunnel']['tunnel_id']}")
        else:
            table.add_row("Cloudflare", "local-only")
        if enrollment.get("cf_tunnel_error"):
            table.add_row("CF warning", enrollment["cf_tunnel_error"])
    console.print(Panel(table, title="inframatik installed", border_style="green"))


def local_url() -> str:
    try:
        output = run_command(["hostname", "-I"], "hostname lookup").split()
        ip = output[0] if output else "127.0.0.1"
    except InstallerError:
        ip = "127.0.0.1"
    return f"http://{ip}:9000"


def main() -> int:
    install_dir = Path(os.environ["INFRAMATIK_INSTALL_DIR"]).expanduser()
    config_dir = Path(os.environ["INFRAMATIK_CONFIG_DIR"]).expanduser()
    service_name = os.environ.get("INFRAMATIK_SERVICE_NAME", "inframatik")
    master_url = os.environ["INFRAMATIK_MASTER_URL"].rstrip("/")
    install_source_master_url = os.environ.get("INFRAMATIK_INSTALL_SOURCE_MASTER_URL", "")
    enroll_token = os.environ.get("INFRAMATIK_ENROLL_TOKEN", "").strip()
    node_name = os.environ.get("INFRAMATIK_NODE_NAME", "").strip()
    skip_cf = env_bool("INFRAMATIK_SKIP_CF")

    console.rule("[bold]inframatik installer[/]")
    console.print(f"Master: [bold]{master_url}[/]")

    if os.geteuid() == 0:
        raise InstallerError("Do not run this script as root. Run as a regular user with sudo access.")
    for cmd in ("sudo", "systemctl", "loginctl"):
        require_command(cmd)

    admin_password = prompt_admin_password(os.environ.get("INFRAMATIK_ADMIN_PASSWORD", ""))
    enroll_token, node_name, skip_cf = maybe_prompt_worker_enrollment(
        master_url,
        enroll_token,
        node_name,
        skip_cf,
    )

    console.print("Checking sudo access...")
    run_command(["sudo", "-v"], "sudo validation", capture=False)
    install_dependencies(install_dir)
    persist_install_source_master(config_dir, install_source_master_url)
    setup_shell_paths(config_dir)
    setup_systemd_service(install_dir, service_name)
    run_status("Enabling user service startup...", ["sudo", "loginctl", "enable-linger", getpass.getuser()], capture=False)
    install_cli(install_dir)
    run_status("Reloading systemd user units...", ["systemctl", "--user", "daemon-reload"])
    run_status("Enabling inframatik service...", ["systemctl", "--user", "enable", service_name])
    run_status("Starting inframatik service...", ["systemctl", "--user", "restart", service_name])
    with console.status("Waiting for inframatik on port 9000..."):
        wait_for_local_service()

    setup_admin_password(admin_password)
    if admin_password:
        console.print("[green]Admin password set.[/]")

    enrollment = None
    if enroll_token:
        enrollment = asyncio.run(enroll_worker(install_dir, master_url, enroll_token, node_name, skip_cf))
        run_status("Restarting service with worker configuration...", ["systemctl", "--user", "restart", service_name])

    display_summary(install_dir, enrollment)
    console.print(f"Dashboard: [bold]{local_url()}[/]")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallerError as e:
        console.print(f"[red]ERROR:[/] {e}")
        raise SystemExit(1)
