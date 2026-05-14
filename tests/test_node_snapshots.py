"""Tests for master-side dashboard node snapshot caching."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import node_snapshots


def _run(coro):
    return asyncio.run(coro)


def test_collect_node_snapshot_fetches_dashboard_payloads_and_caches():
    original_proxy = node_snapshots.proxy_to_node
    calls = []

    async def fake_proxy(node_id, method, path, body=None):
        calls.append((node_id, method, path, body))
        if path == "/api/system":
            return {"cpu": {"percent": 10}}
        if path == "/api/tunnel":
            return {"connected": True}
        if path == "/api/services":
            return [{"name": "svc-a"}]
        raise AssertionError(f"unexpected path: {path}")

    node_snapshots._reset_snapshots_for_tests()
    node_snapshots.proxy_to_node = fake_proxy
    try:
        snapshot = _run(node_snapshots.collect_node_snapshot("node-1"))
        cached = _run(node_snapshots.get_node_snapshot("node-1"))
    finally:
        node_snapshots.proxy_to_node = original_proxy
        node_snapshots._reset_snapshots_for_tests()

    assert snapshot["node_id"] == "node-1"
    assert snapshot["system"]["cpu"]["percent"] == 10
    assert snapshot["tunnel"]["connected"] is True
    assert snapshot["services"] == [{"name": "svc-a"}]
    assert snapshot["errors"] == {}
    assert cached["updated_at"] == snapshot["updated_at"]
    assert sorted(path for _node, _method, path, _body in calls) == [
        "/api/services",
        "/api/system",
        "/api/tunnel",
    ]


def test_collect_node_snapshot_records_component_errors():
    original_proxy = node_snapshots.proxy_to_node

    async def fake_proxy(_node_id, _method, path, body=None):
        if path == "/api/tunnel":
            raise RuntimeError("offline")
        if path == "/api/services":
            return []
        return {"ok": True}

    node_snapshots._reset_snapshots_for_tests()
    node_snapshots.proxy_to_node = fake_proxy
    try:
        snapshot = _run(node_snapshots.collect_node_snapshot("node-1"))
    finally:
        node_snapshots.proxy_to_node = original_proxy
        node_snapshots._reset_snapshots_for_tests()

    assert snapshot["system"] == {"ok": True}
    assert snapshot["services"] == []
    assert snapshot["tunnel"] is None
    assert "offline" in snapshot["errors"]["tunnel"]


if __name__ == "__main__":
    print("Running node snapshot tests...\n")
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {test.__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
