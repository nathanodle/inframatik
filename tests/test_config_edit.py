"""Tests for inframatik CLI config file editing functions."""

import builtins
import json
import os
import sys
import tempfile
from pathlib import Path

# Add parent dir to path so we can import the CLI
sys.path.insert(0, str(Path(__file__).parent.parent))
from importlib.machinery import SourceFileLoader
cli = SourceFileLoader("cli", str(Path(__file__).parent.parent / "inframatik-cli.py")).load_module()

ENDPOINT = "http://localhost:9000"
TOKEN = "svc_test1234"


def _run_in_tmpdir(func):
    """Decorator to run test in a temporary directory."""
    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                func()
            finally:
                os.chdir(old_cwd)
    wrapper.__name__ = func.__name__
    return wrapper


# ---------------------------------------------------------------------------
# .mcp.json tests
# ---------------------------------------------------------------------------

@_run_in_tmpdir
def test_mcp_json_create():
    """Missing file → create with inframatik only."""
    assert cli.edit_mcp_json(ENDPOINT, TOKEN)
    data = json.loads(Path(".mcp.json").read_text())
    assert "inframatik" in data["mcpServers"]
    assert data["mcpServers"]["inframatik"]["type"] == "http"
    assert TOKEN in data["mcpServers"]["inframatik"]["headers"]["Authorization"]


@_run_in_tmpdir
def test_mcp_json_merge():
    """Existing file with other servers → add alongside."""
    existing = {"mcpServers": {"github": {"type": "http", "url": "https://github.com/mcp"}}}
    Path(".mcp.json").write_text(json.dumps(existing))
    assert cli.edit_mcp_json(ENDPOINT, TOKEN)
    data = json.loads(Path(".mcp.json").read_text())
    assert "github" in data["mcpServers"]
    assert "inframatik" in data["mcpServers"]


@_run_in_tmpdir
def test_mcp_json_update():
    """Existing file with inframatik → update in place."""
    existing = {"mcpServers": {"inframatik": {"type": "http", "url": "http://old:9000/mcp"}}}
    Path(".mcp.json").write_text(json.dumps(existing))
    assert cli.edit_mcp_json(ENDPOINT, TOKEN)
    data = json.loads(Path(".mcp.json").read_text())
    assert ENDPOINT in data["mcpServers"]["inframatik"]["url"]


@_run_in_tmpdir
def test_mcp_json_malformed():
    """Malformed file → refuse, don't corrupt."""
    Path(".mcp.json").write_text("not json {{{")
    assert not cli.edit_mcp_json(ENDPOINT, TOKEN)
    assert Path(".mcp.json").read_text() == "not json {{{"


@_run_in_tmpdir
def test_mcp_json_no_backup():
    """Existing file should not create plaintext backup files."""
    existing = {"mcpServers": {}}
    Path(".mcp.json").write_text(json.dumps(existing))
    cli.edit_mcp_json(ENDPOINT, TOKEN)
    assert not Path(".mcp.json.bak").exists()


@_run_in_tmpdir
def test_mcp_json_mode_600():
    """Secret-bearing MCP config should be owner-only readable."""
    cli.edit_mcp_json(ENDPOINT, TOKEN)
    assert (Path(".mcp.json").stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# .codex/config.toml tests
# ---------------------------------------------------------------------------

@_run_in_tmpdir
def test_codex_toml_create():
    """Missing file → create directory and file."""
    assert cli.edit_codex_toml(ENDPOINT, TOKEN)
    content = Path(".codex/config.toml").read_text()
    assert "[mcp_servers.inframatik]" in content
    assert ENDPOINT in content
    assert TOKEN in content
    assert 'http_headers = { Authorization = "Bearer svc_test1234" }' in content
    assert "[mcp_servers.inframatik.headers]" not in content


@_run_in_tmpdir
def test_codex_toml_merge():
    """Existing file with other servers → add alongside."""
    Path(".codex").mkdir()
    Path(".codex/config.toml").write_text('[mcp_servers.github]\ncommand = "gh"\n')
    assert cli.edit_codex_toml(ENDPOINT, TOKEN)
    content = Path(".codex/config.toml").read_text()
    assert "[mcp_servers.github]" in content
    assert "[mcp_servers.inframatik]" in content


@_run_in_tmpdir
def test_codex_toml_update():
    """Existing file with inframatik → update in place."""
    Path(".codex").mkdir()
    Path(".codex/config.toml").write_text(
        '[mcp_servers.inframatik]\nurl = "http://old:9000/mcp"\n\n'
        '[mcp_servers.inframatik.headers]\nAuthorization = "Bearer old"\n'
    )
    assert cli.edit_codex_toml(ENDPOINT, TOKEN)
    content = Path(".codex/config.toml").read_text()
    assert "old:9000" not in content
    assert ENDPOINT in content
    assert 'http_headers = { Authorization = "Bearer svc_test1234" }' in content
    assert "[mcp_servers.inframatik.headers]" not in content


@_run_in_tmpdir
def test_codex_toml_no_backup():
    """Existing file should not create plaintext backup files."""
    Path(".codex").mkdir()
    original = '[mcp_servers.other]\ncommand = "test"\n'
    Path(".codex/config.toml").write_text(original)
    cli.edit_codex_toml(ENDPOINT, TOKEN)
    assert not Path(".codex/config.toml.bak").exists()


@_run_in_tmpdir
def test_codex_toml_mode_600():
    """Secret-bearing Codex config should be owner-only readable."""
    cli.edit_codex_toml(ENDPOINT, TOKEN)
    assert (Path(".codex/config.toml").stat().st_mode & 0o777) == 0o600


@_run_in_tmpdir
def test_secure_write_text_mode_600():
    """Secure writes should enforce restrictive mode even for new files."""
    cli.secure_write_text(".inframatik", "{}\n")
    assert (Path(".inframatik").stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# .inframatik instruction generation tests
# ---------------------------------------------------------------------------

def test_build_service_registration_body_with_hostname():
    payload = cli.build_service_registration_body("uderp", "uderp.discovery-tech.com")
    assert payload["name"] == "uderp"
    assert payload["hostname"] == "uderp.discovery-tech.com"


def test_build_service_registration_body_with_access_policy():
    payload = cli.build_service_registration_body(
        "uderp",
        "uderp.discovery-tech.com",
        "pol-123",
    )
    assert payload["access_policy_id"] == "pol-123"


def test_normalize_public_hostname_accepts_fqdn():
    hostname = cli.normalize_public_hostname(" UDERP.Discovery-Tech.com. ")
    assert hostname == "uderp.discovery-tech.com"


def test_normalize_public_hostname_rejects_single_label():
    try:
        cli.normalize_public_hostname("uderp")
    except ValueError as e:
        assert "include a domain" in str(e)
        return
    raise AssertionError("Expected ValueError")


def test_normalize_public_hostname_rejects_scheme_or_port():
    try:
        cli.normalize_public_hostname("https://uderp.discovery-tech.com:8443")
    except ValueError as e:
        assert "without scheme, path, or port" in str(e)
        return
    raise AssertionError("Expected ValueError")


def test_normalize_public_hostname_rejects_wildcard():
    try:
        cli.normalize_public_hostname("*.discovery-tech.com")
    except ValueError as e:
        assert "not a wildcard" in str(e)
        return
    raise AssertionError("Expected ValueError")


def test_normalize_public_hostname_rejects_ip_address():
    try:
        cli.normalize_public_hostname("127.0.0.1")
    except ValueError as e:
        assert "not an IP address" in str(e)
        return
    raise AssertionError("Expected ValueError")


def test_build_inframatik_instructions_with_hostname():
    instructions = cli.build_inframatik_instructions("uderp", "uderp.discovery-tech.com")
    assert '"hostname": "uderp.discovery-tech.com"' in instructions
    assert "full public hostname/FQDN" in instructions
    assert "not just a subdomain label" in instructions


def test_build_inframatik_instructions_with_access_policy():
    instructions = cli.build_inframatik_instructions(
        "uderp",
        "uderp.discovery-tech.com",
        "pol-123",
    )
    assert '"access_policy_id": "pol-123"' in instructions
    assert "Access policy: use reusable Cloudflare Access policy ID pol-123" in instructions


def test_build_inframatik_instructions_without_hostname():
    instructions = cli.build_inframatik_instructions("uderp")
    assert '"hostname"' not in instructions
    assert "like app.example.com" in instructions
    assert "full public hostname/FQDN" in instructions


def test_choose_access_policy_returns_selected_existing_policy():
    original_api_request = cli.api_request
    original_input = builtins.input
    policies = [
        {"id": "pol-1", "name": "Admins", "members": [{"kind": "email", "value": "a@example.com"}]},
        {"id": "pol-2", "name": "Team", "members": [{"kind": "email_domain", "value": "example.com"}]},
    ]
    prompts = iter(["2"])

    def fake_api_request(endpoint, method, path, body=None, token=None):
        assert endpoint == ENDPOINT
        assert method == "GET"
        assert path == "/api/cf/access/policies"
        assert token == TOKEN
        return policies

    def fake_input(_prompt=""):
        return next(prompts)

    cli.api_request = fake_api_request
    builtins.input = fake_input
    try:
        selected = cli.choose_access_policy(ENDPOINT, TOKEN)
    finally:
        cli.api_request = original_api_request
        builtins.input = original_input

    assert selected["id"] == "pol-2"


def test_choose_access_policy_can_create_new_policy():
    original_api_request = cli.api_request
    original_input = builtins.input
    calls = []
    prompts = iter(["n", "Uderp Team", "team.example.com"])

    def fake_api_request(endpoint, method, path, body=None, token=None):
        calls.append((method, path, body, token))
        if method == "GET":
            return []
        if method == "POST":
            return {
                "status": "created",
                "policy": {"id": "pol-new", "name": "Uderp Team", "members": []},
            }
        raise AssertionError("Unexpected api_request call")

    def fake_input(_prompt=""):
        return next(prompts)

    cli.api_request = fake_api_request
    builtins.input = fake_input
    try:
        selected = cli.choose_access_policy(ENDPOINT, TOKEN)
    finally:
        cli.api_request = original_api_request
        builtins.input = original_input

    assert selected["id"] == "pol-new"
    assert calls[0][0:2] == ("GET", "/api/cf/access/policies")
    assert calls[1][0:3] == (
        "POST",
        "/api/cf/access/policies",
        {"name": "Uderp Team", "value": "team.example.com"},
    )


# ---------------------------------------------------------------------------
# .gitignore tests
# ---------------------------------------------------------------------------

@_run_in_tmpdir
def test_gitignore_create():
    """Missing file → create with entry."""
    assert cli.ensure_gitignore(".inframatik")
    assert ".inframatik" in Path(".gitignore").read_text()


@_run_in_tmpdir
def test_gitignore_append():
    """Existing file without entry → append."""
    Path(".gitignore").write_text("node_modules/\n")
    assert cli.ensure_gitignore(".inframatik")
    content = Path(".gitignore").read_text()
    assert "node_modules/" in content
    assert ".inframatik" in content


@_run_in_tmpdir
def test_gitignore_noop():
    """Existing file with entry → no-op."""
    Path(".gitignore").write_text(".inframatik\n")
    assert not cli.ensure_gitignore(".inframatik")


@_run_in_tmpdir
def test_gitignore_no_trailing_newline():
    """Existing file without trailing newline → adds newline before entry."""
    Path(".gitignore").write_text("*.pyc")
    cli.ensure_gitignore(".inframatik")
    content = Path(".gitignore").read_text()
    assert "*.pyc\n.inframatik\n" == content


# ---------------------------------------------------------------------------
# CLAUDE.md / AGENTS.md tests
# ---------------------------------------------------------------------------

@_run_in_tmpdir
def test_md_create():
    """Missing file → create."""
    assert cli.append_instructions("CLAUDE.md", cli.DEPLOYMENT_INSTRUCTIONS)
    assert ".inframatik" in Path("CLAUDE.md").read_text()


@_run_in_tmpdir
def test_md_append():
    """Existing file without section → append."""
    Path("CLAUDE.md").write_text("# My Project\n\nSome instructions.\n")
    assert cli.append_instructions("CLAUDE.md", cli.DEPLOYMENT_INSTRUCTIONS)
    content = Path("CLAUDE.md").read_text()
    assert "My Project" in content
    assert ".inframatik" in content


@_run_in_tmpdir
def test_md_noop():
    """Existing file with section → no-op."""
    Path("CLAUDE.md").write_text("# Deploy\n\nSee `.inframatik` for details.\n")
    assert not cli.append_instructions("CLAUDE.md", cli.DEPLOYMENT_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    print("Running config editing tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
