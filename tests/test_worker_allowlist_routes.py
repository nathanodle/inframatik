"""Tests for worker target allowlist route behavior."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException

import cluster_routes
import node_config


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        asyncio.run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


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
def test_allowlist_get_requires_master():
    node_config.init_as_standalone("solo")
    exc = _assert_raises_async(HTTPException, cluster_routes.config_get_worker_target_allowlist)
    assert exc.status_code == 403


@_run_with_temp_config
def test_allowlist_put_requires_master():
    node_config.init_as_standalone("solo")
    body = cluster_routes.WorkerTargetAllowlistBody(entries=["10.1.0.0/16"])
    exc = _assert_raises_async(HTTPException, cluster_routes.config_set_worker_target_allowlist, body)
    assert exc.status_code == 403


@_run_with_temp_config
def test_allowlist_delete_requires_master():
    node_config.init_as_standalone("solo")
    exc = _assert_raises_async(HTTPException, cluster_routes.config_clear_worker_target_allowlist)
    assert exc.status_code == 403


@_run_with_temp_config
@_with_allowlist_required(False)
def test_allowlist_put_and_get_roundtrip():
    node_config.init_as_master("master")
    body = cluster_routes.WorkerTargetAllowlistBody(
        entries=["10.2.0.0/16", "*.workers.local", "10.2.0.0/16"]
    )
    updated = asyncio.run(cluster_routes.config_set_worker_target_allowlist(body))
    assert updated["entries"] == ["10.2.0.0/16", "*.workers.local"]
    assert updated["required"] is False

    fetched = asyncio.run(cluster_routes.config_get_worker_target_allowlist())
    assert fetched["entries"] == ["10.2.0.0/16", "*.workers.local"]
    assert fetched["required"] is False


@_run_with_temp_config
@_with_allowlist_required(True)
def test_allowlist_get_reports_required_flag_from_env():
    node_config.init_as_master("master")
    fetched = asyncio.run(cluster_routes.config_get_worker_target_allowlist())
    assert fetched["entries"] == []
    assert fetched["required"] is True


@_run_with_temp_config
def test_allowlist_put_rejects_invalid_entries():
    node_config.init_as_master("master")
    body = cluster_routes.WorkerTargetAllowlistBody(entries=["http://bad-entry"])
    exc = _assert_raises_async(HTTPException, cluster_routes.config_set_worker_target_allowlist, body)
    assert exc.status_code == 400
    assert "Invalid" in str(exc.detail)


@_run_with_temp_config
def test_allowlist_get_rejects_invalid_stored_shape():
    node_config.init_as_master("master")
    cfg = node_config.get_node_config()
    cfg["worker_target_allowlist"] = "not-a-list"
    node_config.save_node_config(cfg)

    exc = _assert_raises_async(HTTPException, cluster_routes.config_get_worker_target_allowlist)
    assert exc.status_code == 400
    assert "must be a list" in str(exc.detail)


@_run_with_temp_config
def test_allowlist_delete_clears_entries():
    node_config.init_as_master("master")
    asyncio.run(
        cluster_routes.config_set_worker_target_allowlist(
            cluster_routes.WorkerTargetAllowlistBody(entries=["worker-a.internal"])
        )
    )
    cleared = asyncio.run(cluster_routes.config_clear_worker_target_allowlist())
    assert cleared["entries"] == []
    cfg = node_config.get_node_config()
    assert cfg.get("worker_target_allowlist") == []


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
    print("Running worker allowlist route tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
