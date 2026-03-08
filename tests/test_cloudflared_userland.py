"""Tests for userland cloudflared setup helper."""

import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cloudflared


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        asyncio.run(coro_fn(*args, **kwargs))
    except exc_type:
        return
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def _run_with_temp_paths(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_home = Path(tmpdir)
            original_paths = (
                cloudflared.CLOUDFLARED_BINARY_PATH,
                cloudflared.CLOUDFLARED_TOKEN_PATH,
                cloudflared.CLOUDFLARED_UNIT_PATH,
            )
            cloudflared.CLOUDFLARED_BINARY_PATH = tmp_home / ".local" / "bin" / "cloudflared"
            cloudflared.CLOUDFLARED_TOKEN_PATH = tmp_home / ".config" / "inframatik" / "cf-tunnel-token"
            cloudflared.CLOUDFLARED_UNIT_PATH = (
                tmp_home / ".config" / "systemd" / "user" / "cloudflared.service"
            )
            try:
                fn(tmp_home)
            finally:
                cloudflared.CLOUDFLARED_BINARY_PATH = original_paths[0]
                cloudflared.CLOUDFLARED_TOKEN_PATH = original_paths[1]
                cloudflared.CLOUDFLARED_UNIT_PATH = original_paths[2]
    wrapper.__name__ = fn.__name__
    return wrapper


@_run_with_temp_paths
def test_setup_cloudflared_user_service_writes_files_and_reloads_systemd(_tmp_home: Path):
    cloudflared.CLOUDFLARED_BINARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    cloudflared.CLOUDFLARED_BINARY_PATH.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(cloudflared.CLOUDFLARED_BINARY_PATH, 0o755)

    calls = []

    async def fake_run(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        return 0, "ok"

    original_run = cloudflared._run
    cloudflared._run = fake_run
    try:
        asyncio.run(cloudflared.setup_cloudflared_user_service("token-123"))
    finally:
        cloudflared._run = original_run

    token_text = cloudflared.CLOUDFLARED_TOKEN_PATH.read_text().strip()
    assert token_text == "token-123"
    token_mode = stat.S_IMODE(cloudflared.CLOUDFLARED_TOKEN_PATH.stat().st_mode)
    assert token_mode == 0o600

    unit_text = cloudflared.CLOUDFLARED_UNIT_PATH.read_text()
    assert "ExecStart=" in unit_text
    assert str(cloudflared.CLOUDFLARED_BINARY_PATH) in unit_text
    assert str(cloudflared.CLOUDFLARED_TOKEN_PATH) in unit_text

    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "cloudflared.service"],
    ]


@_run_with_temp_paths
def test_setup_cloudflared_user_service_requires_binary(_tmp_home: Path):
    _assert_raises_async(RuntimeError, cloudflared.setup_cloudflared_user_service, "token-123")


def test_setup_cloudflared_user_service_requires_token():
    _assert_raises_async(ValueError, cloudflared.setup_cloudflared_user_service, "")


@_run_with_temp_paths
def test_cloudflared_status_reads_systemd_properties(_tmp_home: Path):
    cloudflared.CLOUDFLARED_BINARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    cloudflared.CLOUDFLARED_BINARY_PATH.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(cloudflared.CLOUDFLARED_BINARY_PATH, 0o755)
    cloudflared.CLOUDFLARED_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    cloudflared.CLOUDFLARED_TOKEN_PATH.write_text("token")
    cloudflared.CLOUDFLARED_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    cloudflared.CLOUDFLARED_UNIT_PATH.write_text("[Unit]\n")

    async def fake_run(cmd: list[str]) -> tuple[int, str]:
        if cmd[:3] == ["systemctl", "--user", "show"]:
            return 0, "\n".join(
                [
                    "ActiveState=active",
                    "SubState=running",
                    "UnitFileState=enabled",
                    "Result=success",
                    "MainPID=1234",
                ]
            )
        return 1, "unexpected command"

    original_run = cloudflared._run
    cloudflared._run = fake_run
    try:
        status = asyncio.run(cloudflared.get_cloudflared_user_service_status())
    finally:
        cloudflared._run = original_run

    assert status["binary_installed"]
    assert status["token_present"]
    assert status["unit_present"]
    assert status["active_state"] == "active"
    assert status["sub_state"] == "running"
    assert status["unit_file_state"] == "enabled"
    assert status["result"] == "success"
    assert status["main_pid"] == 1234


@_run_with_temp_paths
def test_cloudflared_restart_runs_systemctl_restart(_tmp_home: Path):
    cloudflared.CLOUDFLARED_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    cloudflared.CLOUDFLARED_UNIT_PATH.write_text("[Unit]\n")

    calls = []

    async def fake_run(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        if cmd[:4] == ["systemctl", "--user", "restart", "cloudflared.service"]:
            return 0, ""
        if cmd[:3] == ["systemctl", "--user", "show"]:
            return 0, "\n".join(
                [
                    "ActiveState=active",
                    "SubState=running",
                    "UnitFileState=enabled",
                    "Result=success",
                    "MainPID=987",
                ]
            )
        return 1, "unexpected command"

    original_run = cloudflared._run
    cloudflared._run = fake_run
    try:
        result = asyncio.run(cloudflared.restart_cloudflared_user_service())
    finally:
        cloudflared._run = original_run

    assert calls[0] == ["systemctl", "--user", "restart", "cloudflared.service"]
    assert result["active_state"] == "active"


def test_cloudflared_logs_line_bounds_enforced():
    _assert_raises_async(ValueError, cloudflared.get_cloudflared_user_service_logs, 0)
    _assert_raises_async(
        ValueError,
        cloudflared.get_cloudflared_user_service_logs,
        cloudflared.MAX_LOG_LINES + 1,
    )


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
    print("Running cloudflared userland tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
