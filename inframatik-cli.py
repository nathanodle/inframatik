#!/usr/bin/env python3
"""inframatik CLI — service token management and agent harness setup."""

import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def api_request(endpoint, method, path, body=None, token=None):
    url = f"{endpoint}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            detail = err.get("detail", str(e))
        except Exception:
            detail = str(e)
        print(f"  Error: {detail}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Config file editing
# ---------------------------------------------------------------------------

def ensure_gitignore(entry, path=".gitignore"):
    """Add entry to .gitignore if not already present."""
    gitignore = Path(path)
    if gitignore.exists():
        content = gitignore.read_text()
        if entry in content.splitlines():
            return False
    else:
        content = ""
    with open(gitignore, "a") as f:
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write(f"{entry}\n")
    return True


def edit_mcp_json(endpoint, token, path=".mcp.json"):
    """Add/update inframatik entry in .mcp.json (Claude Code project scope)."""
    mcp_path = Path(path)
    config = {}
    if mcp_path.exists():
        try:
            config = json.loads(mcp_path.read_text())
        except (json.JSONDecodeError, ValueError):
            print(f"  Warning: {path} is malformed, skipping")
            return False
        # Backup
        backup = Path(f"{path}.bak")
        backup.write_text(mcp_path.read_text())

    config.setdefault("mcpServers", {})
    config["mcpServers"]["inframatik"] = {
        "type": "http",
        "url": f"{endpoint}/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }
    mcp_path.write_text(json.dumps(config, indent=2) + "\n")
    return True


def edit_codex_toml(endpoint, token, path=".codex/config.toml"):
    """Add/update inframatik entry in .codex/config.toml (Codex project scope)."""
    toml_path = Path(path)
    toml_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing content
    existing_lines = []
    if toml_path.exists():
        existing_lines = toml_path.read_text().splitlines()
        # Backup
        backup = Path(f"{path}.bak")
        backup.write_text(toml_path.read_text())

    # Remove existing inframatik section if present
    filtered = []
    skip = False
    for line in existing_lines:
        if line.strip().startswith("[mcp_servers.inframatik"):
            skip = True
            continue
        if skip and line.strip().startswith("["):
            skip = False
        if not skip:
            filtered.append(line)

    # Append new inframatik section
    if filtered and filtered[-1].strip():
        filtered.append("")
    filtered.extend([
        "[mcp_servers.inframatik]",
        'type = "http"',
        f'url = "{endpoint}/mcp"',
        "",
        "[mcp_servers.inframatik.headers]",
        f'Authorization = "Bearer {token}"',
    ])

    toml_path.write_text("\n".join(filtered) + "\n")
    return True


def append_instructions(path, content):
    """Append deployment section to a markdown file if not already present."""
    md_path = Path(path)
    if md_path.exists():
        existing = md_path.read_text()
        if "inframatik" in existing.lower() and ".inframatik" in existing:
            return False
        with open(md_path, "a") as f:
            if not existing.endswith("\n"):
                f.write("\n")
            f.write(f"\n{content}\n")
    else:
        md_path.write_text(f"{content}\n")
    return True


DEPLOYMENT_INSTRUCTIONS = """## Deployment

This app is deployed via inframatik. See `.inframatik` for API endpoint, auth token, and usage instructions."""


# ---------------------------------------------------------------------------
# Agent harness detection
# ---------------------------------------------------------------------------

def detect_claude():
    return Path.home().joinpath(".claude").exists() or shutil.which("claude")


def detect_codex():
    return Path.home().joinpath(".codex").exists() or shutil.which("codex")


def prompt_yn(question, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    resp = input(f"{question} {suffix} ").strip().lower()
    if not resp:
        return default
    return resp in ("y", "yes")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init():
    print("inframatik init\n")

    # 1. Server endpoint
    endpoint = input("inframatik server [http://localhost:9000]: ").strip()
    if not endpoint:
        endpoint = "http://localhost:9000"
    endpoint = endpoint.rstrip("/")

    # 2. Authenticate
    password = getpass.getpass("Admin password: ")
    result = api_request(endpoint, "POST", "/api/auth/login", {"password": password})
    if not result:
        print("Authentication failed.")
        sys.exit(1)
    session_token = result["token"]
    print("✓ Authenticated\n")

    # 3. Service name + hostname
    service = input("Service name: ").strip()
    if not service:
        print("Service name is required.")
        sys.exit(1)
    hostname = input("Hostname (optional): ").strip() or None

    # 4. Create service token
    result = api_request(endpoint, "POST", "/api/config/service-tokens",
                         {"service": service}, token=session_token)
    if not result:
        print("Failed to create service token.")
        sys.exit(1)
    svc_token = result["token"]
    print(f"✓ Created service token for {service}")

    # 5. Write .inframatik
    instructions = (
        f"This app is managed by inframatik. "
        f"Use the API at the endpoint below with the token as Bearer auth.\n\n"
        f"Register: POST /api/services "
        f'{{"name": "{service}", "command": "<start command>", "working_dir": "<path>"}}\n'
        f"Start: POST /api/services/{service}/start\n"
        f"Stop: POST /api/services/{service}/stop\n"
        f"Restart: POST /api/services/{service}/restart\n"
        f"Logs: GET /api/services/{service}/logs\n"
        f"Status: GET /api/services\n\n"
        f"All requests need header: Authorization: Bearer <token from this file>"
    )
    inframatik_config = {
        "endpoint": endpoint,
        "token": svc_token,
        "service": service,
    }
    if hostname:
        inframatik_config["hostname"] = hostname
    inframatik_config["instructions"] = instructions

    Path(".inframatik").write_text(json.dumps(inframatik_config, indent=2) + "\n")
    print("✓ Wrote .inframatik")

    # 6. Gitignore
    if ensure_gitignore(".inframatik"):
        print("✓ Added .inframatik to .gitignore")

    print()

    # 7. Detect and configure agent harnesses
    has_claude = detect_claude()
    has_codex = detect_codex()

    if has_claude:
        print("Detected: Claude Code")
        if prompt_yn("  Register MCP server for this project?"):
            claude_cli = shutil.which("claude")
            if claude_cli:
                try:
                    subprocess.run([
                        claude_cli, "mcp", "add",
                        "--transport", "http",
                        "--scope", "project",
                        "--header", f"Authorization: Bearer {svc_token}",
                        "inframatik", f"{endpoint}/mcp",
                    ], check=True, capture_output=True)
                    print("  ✓ Registered MCP server (project scope)")
                except subprocess.CalledProcessError:
                    if edit_mcp_json(endpoint, svc_token):
                        print("  ✓ Updated .mcp.json")
            else:
                if edit_mcp_json(endpoint, svc_token):
                    print("  ✓ Updated .mcp.json")

        if prompt_yn("  Add deployment instructions to CLAUDE.md?"):
            if append_instructions("CLAUDE.md", DEPLOYMENT_INSTRUCTIONS):
                print("  ✓ Updated CLAUDE.md")
            else:
                print("  Already present in CLAUDE.md")
        print()

    if has_codex:
        print("Detected: Codex CLI")
        if prompt_yn("  Register MCP server for this project?"):
            codex_cli = shutil.which("codex")
            if codex_cli:
                try:
                    subprocess.run([
                        codex_cli, "mcp", "add",
                        "inframatik",
                        "--transport", "http",
                        f"{endpoint}/mcp",
                    ], check=True, capture_output=True)
                    print("  ✓ Registered MCP server")
                except subprocess.CalledProcessError:
                    if edit_codex_toml(endpoint, svc_token):
                        print("  ✓ Created .codex/config.toml")
            else:
                if edit_codex_toml(endpoint, svc_token):
                    print("  ✓ Created .codex/config.toml")

        if prompt_yn("  Add deployment instructions to AGENTS.md?"):
            if append_instructions("AGENTS.md", DEPLOYMENT_INSTRUCTIONS):
                print("  ✓ Updated AGENTS.md")
            else:
                print("  Already present in AGENTS.md")
        print()

    if not has_claude and not has_codex:
        print("No agent harnesses detected (Claude Code, Codex).")
        print("Models can still use the REST API — see .inframatik for details.")
        print()

    print(f"Done! Token is scoped to service '{service}'.")


def cmd_mcp():
    print("MCP server coming soon — will be an HTTP endpoint built into inframatik.")
    print("For now, models can use the REST API directly. See .inframatik for details.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: inframatik <command>")
        print()
        print("Commands:")
        print("  init    Set up a service token and configure agent harnesses")
        print("  mcp     Run as MCP server (coming soon)")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "init":
        cmd_init()
    elif cmd == "mcp":
        cmd_mcp()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
