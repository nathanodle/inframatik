"""Tests for inframatik CLI config file editing functions."""

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
        '[mcp_servers.inframatik]\nurl = "http://old:9000/mcp"\n'
    )
    assert cli.edit_codex_toml(ENDPOINT, TOKEN)
    content = Path(".codex/config.toml").read_text()
    assert "old:9000" not in content
    assert ENDPOINT in content


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
