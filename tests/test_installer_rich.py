"""Tests for the Rich installer helpers."""

import sys
import types
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import rich  # noqa: F401
except ModuleNotFoundError:
    class _FakeRichObject:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def add_task(self, *args, **kwargs):
            return 1

        def update(self, *args, **kwargs):
            pass

    class _FakeConsole:
        def print(self, *args, **kwargs):
            pass

        def rule(self, *args, **kwargs):
            pass

        def status(self, *args, **kwargs):
            return _FakeRichObject()

    rich_module = types.ModuleType("rich")
    rich_console = types.ModuleType("rich.console")
    rich_panel = types.ModuleType("rich.panel")
    rich_progress = types.ModuleType("rich.progress")
    rich_table = types.ModuleType("rich.table")
    rich_console.Console = _FakeConsole
    rich_panel.Panel = _FakeRichObject
    rich_progress.Progress = _FakeRichObject
    rich_progress.BarColumn = _FakeRichObject
    rich_progress.SpinnerColumn = _FakeRichObject
    rich_progress.TextColumn = _FakeRichObject
    rich_progress.TimeElapsedColumn = _FakeRichObject
    rich_table.Table = _FakeRichObject
    sys.modules.update({
        "rich": rich_module,
        "rich.console": rich_console,
        "rich.panel": rich_panel,
        "rich.progress": rich_progress,
        "rich.table": rich_table,
    })

import installer_rich


def test_prompt_from_tty_uses_low_level_tty_io():
    writes = []
    reads = iter([b"e", b"n", b"r", b"o", b"l", b"l", b"-", b"1", b"\n"])

    with mock.patch.object(installer_rich.os, "open", return_value=7), \
            mock.patch.object(installer_rich.os, "write", side_effect=lambda _fd, data: writes.append(data) or len(data)), \
            mock.patch.object(installer_rich.os, "read", side_effect=lambda _fd, _n: next(reads)), \
            mock.patch.object(installer_rich.os, "close") as close:
        result = installer_rich.prompt_from_tty("Enrollment token: ")

    assert result == "enroll-1"
    assert writes == [b"Enrollment token: "]
    close.assert_called_once_with(7)


def test_worker_enrollment_prompt_skips_blank_token():
    prompts = iter([""])

    token, node_name, skip_cf = installer_rich.maybe_prompt_worker_enrollment(
        "http://master.local:9000",
        "",
        "",
        False,
        prompt_text=lambda _prompt: next(prompts),
        prompt_bool=lambda _prompt, default=False: (_ for _ in ()).throw(AssertionError("unexpected bool prompt")),
    )

    assert token == ""
    assert node_name == ""
    assert skip_cf is False


def test_worker_enrollment_prompt_accepts_token_name_and_local_only():
    prompts = iter(["enroll-token", "worker-a"])

    token, node_name, skip_cf = installer_rich.maybe_prompt_worker_enrollment(
        "http://master.local:9000",
        "",
        "",
        False,
        prompt_text=lambda _prompt: next(prompts),
        prompt_bool=lambda _prompt, default=False: True,
    )

    assert token == "enroll-token"
    assert node_name == "worker-a"
    assert skip_cf is True


def test_worker_enrollment_prompt_defaults_blank_name_to_hostname():
    prompts = iter(["enroll-token", ""])
    original_default = installer_rich.default_worker_name
    installer_rich.default_worker_name = lambda: "host-a"
    try:
        token, node_name, skip_cf = installer_rich.maybe_prompt_worker_enrollment(
            "http://master.local:9000",
            "",
            "",
            False,
            prompt_text=lambda _prompt: next(prompts),
            prompt_bool=lambda _prompt, default=False: False,
        )
    finally:
        installer_rich.default_worker_name = original_default

    assert token == "enroll-token"
    assert node_name == "host-a"
    assert skip_cf is False


def test_worker_enrollment_prompt_uses_existing_flags_without_prompting():
    token, node_name, skip_cf = installer_rich.maybe_prompt_worker_enrollment(
        "http://master.local:9000",
        "enroll-token",
        "worker-a",
        True,
        prompt_text=lambda _prompt: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )

    assert token == "enroll-token"
    assert node_name == "worker-a"
    assert skip_cf is True


def test_worker_enrollment_prompt_ignores_unresolved_master_template():
    token, node_name, skip_cf = installer_rich.maybe_prompt_worker_enrollment(
        "__MASTER_URL__",
        "",
        "",
        False,
        prompt_text=lambda _prompt: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )

    assert token == ""
    assert node_name == ""
    assert skip_cf is False


def test_setup_admin_password_tolerates_existing_password():
    calls = []
    original_request = installer_rich.request_json

    def fake_request(url, body=None, **_kwargs):
        calls.append((url, body))
        raise installer_rich.InstallerError("http://127.0.0.1:9000/api/auth/set-password returned HTTP 400: Password already set. Use settings to change it.")

    installer_rich.request_json = fake_request
    try:
        installer_rich.setup_admin_password("password123")
    finally:
        installer_rich.request_json = original_request

    assert calls == [("http://127.0.0.1:9000/api/auth/set-password", {"password": "password123"})]


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
    print("Running installer Rich tests...\n")
    raise SystemExit(0 if run_tests() else 1)
