"""Tests for service input validation hardening."""

import json
import sys
import tempfile
from pathlib import Path

# Add parent dir to path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import services


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def _run_with_temp_registry(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            original_services_file = services.SERVICES_FILE
            original_ports_env_file = services.PORTS_ENV_FILE
            services.SERVICES_FILE = tmpdir_path / "services.json"
            services.PORTS_ENV_FILE = tmpdir_path / "ports.env"
            try:
                fn(tmpdir_path)
            finally:
                services.SERVICES_FILE = original_services_file
                services.PORTS_ENV_FILE = original_ports_env_file

    wrapper.__name__ = fn.__name__
    return wrapper


def test_command_allows_normal_input():
    services._validate_command("uvicorn main:app --host 127.0.0.1 --port 8000")


def test_command_rejects_semicolons():
    _assert_raises(ValueError, services._validate_command, "python app.py; whoami")


def test_command_rejects_control_whitespace():
    _assert_raises(ValueError, services._validate_command, "python\t;\twhoami")


def test_working_dir_rejects_control_chars():
    _assert_raises(ValueError, services._validate_working_dir, "/tmp/app\nx")


@_run_with_temp_registry
def test_load_registry_recovers_from_backup(tmpdir: Path):
    backup_data = {"svc-a": {"port": 8100}}
    services.SERVICES_FILE.write_text("{not json")
    services._registry_backup_file().write_text(json.dumps(backup_data))

    loaded = services._load_registry()
    assert loaded == backup_data
    assert json.loads(services.SERVICES_FILE.read_text()) == backup_data
    assert services.PORTS_ENV_FILE.exists()
    assert "INFRA_SVC_A_PORT=8100" in services.PORTS_ENV_FILE.read_text()


@_run_with_temp_registry
def test_load_registry_quarantines_corrupt_when_no_backup(tmpdir: Path):
    services.SERVICES_FILE.write_text("{bad json")

    loaded = services._load_registry()
    assert loaded == {}
    assert not services.SERVICES_FILE.exists()
    quarantined = list(tmpdir.glob("services.json.corrupt-*"))
    assert len(quarantined) == 1


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
    print("Running service validation tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
