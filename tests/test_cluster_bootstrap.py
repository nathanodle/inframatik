"""Tests for bootstrap config handling in cluster routes."""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import node_config
try:
    import cluster_routes
except ModuleNotFoundError:
    cluster_routes = None


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


@_run_with_temp_config
def test_node_info_handles_password_only_bootstrap_config():
    if cluster_routes is None:
        return
    node_config.save_node_config({"admin_password_hash": "hash"})
    info = asyncio.run(cluster_routes.node_info())
    assert info["role"] == "unconfigured"
    assert info["node_name"] is None
    assert info["node_id"] is None
    assert "machine_hostname" in info


@_run_with_temp_config
def test_config_get_handles_password_only_bootstrap_config():
    if cluster_routes is None:
        return
    node_config.save_node_config({"admin_password_hash": "hash"})
    cfg = asyncio.run(cluster_routes.config_get())
    assert cfg == {"role": "unconfigured"}


@_run_with_temp_config
def test_init_standalone_from_bootstrap_preserves_password_hash():
    if cluster_routes is None:
        return
    node_config.save_node_config({"admin_password_hash": "hash"})
    result = asyncio.run(cluster_routes.config_init_standalone(cluster_routes.InitBody(name="node-a")))
    assert result["role"] == "standalone"
    cfg = node_config.get_node_config()
    assert cfg is not None
    assert cfg["role"] == "standalone"
    assert cfg.get("admin_password_hash") == "hash"


def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    skipped = 0
    for test in tests:
        try:
            if cluster_routes is None:
                print(f"  - {test.__name__}: skipped (fastapi not installed)")
                skipped += 1
                continue
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return failed == 0


if __name__ == "__main__":
    print("Running cluster bootstrap tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
