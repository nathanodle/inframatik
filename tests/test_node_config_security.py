"""Security-focused tests for node config token and address handling."""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import node_config


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def _run_with_temp_config(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir:
            original_file = node_config.CONFIG_FILE
            node_config.CONFIG_FILE = Path(tmpdir) / "node.json"
            node_config.invalidate_cache()
            try:
                fn()
            finally:
                node_config.CONFIG_FILE = original_file
                node_config.invalidate_cache()
    wrapper.__name__ = fn.__name__
    return wrapper


def _with_allowlist_required(required: bool):
    def decorator(fn):
        def wrapper():
            env_key = "INFRAMATIK_REQUIRE_WORKER_ALLOWLIST"
            old_val = os.environ.get(env_key)
            if required:
                os.environ[env_key] = "1"
            else:
                os.environ.pop(env_key, None)
            try:
                fn()
            finally:
                if old_val is None:
                    os.environ.pop(env_key, None)
                else:
                    os.environ[env_key] = old_val
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


@_run_with_temp_config
def test_enrollment_token_expiry_enforced():
    node_config.init_as_master("master")
    token = node_config.create_enrollment_token()
    cfg = node_config.get_node_config()
    cfg["enrollment_tokens"][token]["expires_at"] = int(time.time()) - 1
    node_config.save_node_config(cfg)

    assert not node_config.consume_enrollment_token(token)
    cfg = node_config.get_node_config()
    assert token not in cfg.get("enrollment_tokens", {})


@_run_with_temp_config
def test_service_token_expiry_enforced():
    node_config.init_as_master("master")
    token = node_config.create_service_token("api")
    cfg = node_config.get_node_config()
    cfg["service_tokens"][token]["expires_at"] = int(time.time()) - 1
    node_config.save_node_config(cfg)

    assert node_config.get_service_token_scope(token) is None
    cfg = node_config.get_node_config()
    assert token not in cfg.get("service_tokens", {})


@_run_with_temp_config
def test_rotate_service_token_replaces_old_token():
    node_config.init_as_master("master")
    old_token = node_config.create_service_token("api")
    new_token, service, capability = node_config.rotate_service_token(old_token)

    assert service == "api"
    assert capability == "deploy"
    assert new_token != old_token
    assert node_config.get_service_token_scope(old_token) is None
    assert node_config.get_service_token_scope(new_token) == "api"


@_run_with_temp_config
def test_service_token_capability_persisted_and_rotated():
    node_config.init_as_master("master")
    token = node_config.create_service_token("api", capability="operate")
    token_auth = node_config.get_service_token_auth(token)
    assert token_auth is not None
    assert token_auth["capability"] == "operate"

    new_token, service, capability = node_config.rotate_service_token(token)
    assert service == "api"
    assert capability == "operate"
    new_auth = node_config.get_service_token_auth(new_token)
    assert new_auth is not None
    assert new_auth["capability"] == "operate"


@_run_with_temp_config
def test_service_token_invalid_capability_rejected():
    node_config.init_as_master("master")
    _assert_raises(ValueError, node_config.create_service_token, "api", "root")


@_run_with_temp_config
def test_service_token_id_and_revoke_by_id():
    node_config.init_as_master("master")
    token = node_config.create_service_token("api")
    token_id = node_config.service_token_id(token)
    assert token_id.startswith("st_")
    assert node_config.revoke_service_token_by_id(token_id)
    assert node_config.get_service_token_scope(token) is None


@_run_with_temp_config
def test_revoke_service_token_by_id_unknown_returns_false():
    node_config.init_as_master("master")
    assert not node_config.revoke_service_token_by_id("st_doesnotexist")


def test_service_token_capability_hierarchy():
    assert node_config.service_token_capability_allows("read", "read")
    assert not node_config.service_token_capability_allows("read", "operate")
    assert node_config.service_token_capability_allows("operate", "read")
    assert node_config.service_token_capability_allows("deploy", "operate")


def test_worker_address_normalization_and_restrictions():
    assert node_config.normalize_worker_address("HTTP://Example.com:9000/") == "http://example.com:9000"
    _assert_raises(ValueError, node_config.normalize_worker_address, "http://localhost:9000")
    _assert_raises(ValueError, node_config.normalize_worker_address, "http://127.0.0.1:9000")
    _assert_raises(ValueError, node_config.normalize_worker_address, "http://10.0.0.10")
    _assert_raises(ValueError, node_config.normalize_worker_address, "http://example.com/path")
    _assert_raises(ValueError, node_config.normalize_worker_address, "ftp://example.com:21")


@_run_with_temp_config
def test_add_worker_persists_normalized_address():
    node_config.init_as_master("master")
    api_key = "sdk_test"
    node_id = node_config.add_worker("worker-a", "HTTPS://WORKER.local:9100/", api_key)
    cfg = node_config.get_node_config()
    assert cfg["workers"][node_id]["address"] == "https://worker.local:9100"


@_run_with_temp_config
def test_worker_cf_opt_out_persists_until_tunnel_is_set():
    node_config.init_as_master("master")
    node_id = node_config.add_worker(
        "worker-a",
        "http://worker.local:9000",
        "sdk_test",
        cf_opt_out=True,
    )
    cfg = node_config.get_node_config()
    assert cfg["workers"][node_id]["cf_opt_out"] is True

    node_config.set_worker_tunnel_id(node_id, "tid-1")
    cfg = node_config.get_node_config()
    assert cfg["workers"][node_id]["tunnel_id"] == "tid-1"
    assert "cf_opt_out" not in cfg["workers"][node_id]


@_run_with_temp_config
@_with_allowlist_required(False)
def test_worker_allowlist_optional_mode_allows_when_empty():
    node_config.init_as_master("master")
    assert node_config.is_worker_address_allowed("http://198.51.100.10:9000")


@_run_with_temp_config
@_with_allowlist_required(True)
def test_worker_allowlist_required_mode_blocks_when_empty():
    node_config.init_as_master("master")
    _assert_raises(
        ValueError,
        node_config.assert_worker_address_allowed,
        "http://198.51.100.10:9000",
    )


@_run_with_temp_config
@_with_allowlist_required(False)
def test_worker_allowlist_matching_modes():
    node_config.init_as_master("master")
    entries = node_config.set_worker_target_allowlist(
        ["worker-1.internal", "*.cluster.local", "10.50.0.0/16", "10.50.0.0/16"]
    )
    assert entries == ["worker-1.internal", "*.cluster.local", "10.50.0.0/16"]

    assert node_config.is_worker_address_allowed("http://worker-1.internal:9000")
    assert node_config.is_worker_address_allowed("http://app.cluster.local:9000")
    assert not node_config.is_worker_address_allowed("http://cluster.local:9000")
    assert node_config.is_worker_address_allowed("http://10.50.1.20:9000")
    assert not node_config.is_worker_address_allowed("http://10.51.1.20:9000")


@_run_with_temp_config
@_with_allowlist_required(False)
def test_add_worker_enforces_allowlist():
    node_config.init_as_master("master")
    node_config.set_worker_target_allowlist(["10.60.0.0/16"])
    node_config.add_worker("worker-a", "http://10.60.1.7:9000", "sdk_1")
    _assert_raises(
        ValueError,
        node_config.add_worker,
        "worker-b",
        "http://198.51.100.7:9000",
        "sdk_2",
    )


@_run_with_temp_config
def test_invalid_allowlist_entry_rejected():
    node_config.init_as_master("master")
    _assert_raises(ValueError, node_config.set_worker_target_allowlist, ["http://bad-entry"])


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
    print("Running node config security tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
