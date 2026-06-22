"""HTTP integration tests for main app middleware + routing behavior."""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth
import cf_routes
import cluster_routes
import main


class _Patch:
    def __init__(self, patches):
        self._patches = patches
        self._originals = []

    def __enter__(self):
        for obj, attr, value in self._patches:
            self._originals.append((obj, attr, getattr(obj, attr)))
            setattr(obj, attr, value)
        return self

    def __exit__(self, exc_type, exc, tb):
        for obj, attr, old in reversed(self._originals):
            setattr(obj, attr, old)
        return False


def _request(method, path, *, json_body=None, headers=None, patches=()):
    async def _run():
        base = [(main, "get_node_config", lambda: None)]
        with _Patch(base + list(patches)):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, json=json_body, headers=headers)

    return asyncio.run(_run())


async def _auth_false(_request):
    return False


async def _auth_true(_request):
    return True


def _scoped_auth(scope: str, capability: str):
    async def _inner(request):
        request.state.service_scope = scope
        request.state.service_capability = capability
        return True

    return _inner


def test_public_node_info_bypasses_auth_check():
    called = []

    async def check_auth_spy(_request):
        called.append(True)
        return False

    resp = _request(
        "GET",
        "/api/node/info",
        patches=(
            (auth, "check_auth", check_auth_spy),
            (cluster_routes, "get_node_config", lambda: None),
        ),
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["role"] == "unconfigured"
    assert payload["node_name"] is None
    assert payload["node_id"] is None
    assert "machine_hostname" in payload
    assert called == []


def test_asset_version_uses_deploy_metadata_for_cache_bust():
    original_get_version = main.get_version
    try:
        main.get_version = lambda: {"commit": "abc1234", "deployed_at": 111}
        first = main._get_asset_version()
        main.get_version = lambda: {"commit": "abc1234", "deployed_at": 222}
        second = main._get_asset_version()
    finally:
        main.get_version = original_get_version

    assert first != second


def test_protected_system_requires_auth():
    resp = _request("GET", "/api/system", patches=((auth, "check_auth", _auth_false),))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Authentication required"


def test_protected_system_with_auth_succeeds():
    resp = _request(
        "GET",
        "/api/system",
        patches=(
            (auth, "check_auth", _auth_true),
            (main, "get_system_metrics", lambda: {"uptime_seconds": 7, "host": {"name": "n"}}),
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["uptime_seconds"] == 7


def test_service_scope_filters_services_list():
    async def fake_list_services():
        return [{"name": "svc-a"}, {"name": "svc-b"}]

    resp = _request(
        "GET",
        "/api/services",
        patches=(
            (auth, "check_auth", _scoped_auth("svc-a", "read")),
            (main, "list_services", fake_list_services),
        ),
    )

    assert resp.status_code == 200
    assert resp.json() == [{"name": "svc-a"}]


def test_service_token_read_cannot_restart_service():
    resp = _request(
        "POST",
        "/api/services/svc-a/restart",
        patches=((auth, "check_auth", _scoped_auth("svc-a", "read")),),
    )
    assert resp.status_code == 403
    assert "cannot perform this action" in resp.json()["detail"]


def test_service_scope_mismatch_blocks_logs():
    resp = _request(
        "GET",
        "/api/services/svc-b/logs",
        patches=((auth, "check_auth", _scoped_auth("svc-a", "read")),),
    )
    assert resp.status_code == 403
    assert "Token is scoped to service 'svc-a'" == resp.json()["detail"]


def test_scoped_service_token_blocked_on_unmapped_endpoint():
    resp = _request(
        "GET",
        "/api/system",
        patches=((auth, "check_auth", _scoped_auth("svc-a", "deploy")),),
    )
    assert resp.status_code == 403
    assert "cannot access this endpoint" in resp.json()["detail"]


def test_service_token_read_cannot_get_next_port():
    resp = _request(
        "GET",
        "/api/ports/next",
        patches=((auth, "check_auth", _scoped_auth("svc-a", "read")),),
    )
    assert resp.status_code == 403


def test_service_token_deploy_can_get_next_port():
    resp = _request(
        "GET",
        "/api/ports/next",
        patches=(
            (auth, "check_auth", _scoped_auth("svc-a", "deploy")),
            (main, "next_available_port", lambda: 8123),
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {"port": 8123}


def test_self_auth_internal_cf_path_bypasses_check_auth():
    called = []

    async def check_auth_spy(_request):
        called.append(True)
        return False

    async def fake_cf_status():
        return {"active_state": "active"}

    resp = _request(
        "GET",
        "/api/internal/cf/service/status",
        headers={"X-Api-Key": "k1"},
        patches=(
            (auth, "check_auth", check_auth_spy),
            (cf_routes, "get_node_config", lambda: {"api_key": "k1"}),
            (cf_routes, "get_cloudflared_user_service_status", fake_cf_status),
        ),
    )

    assert resp.status_code == 200
    assert resp.json()["active_state"] == "active"
    assert called == []


def test_self_auth_internal_cf_path_requires_x_api_key():
    resp = _request(
        "GET",
        "/api/internal/cf/service/status",
        patches=(
            (auth, "check_auth", _auth_true),
            (cf_routes, "get_node_config", lambda: {"api_key": "k1"}),
        ),
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "API key required"


def test_worker_event_path_bypasses_session_and_validates_worker_key():
    auth_calls = []
    broadcasts = []

    async def check_auth_spy(_request):
        auth_calls.append(True)
        return False

    def fake_worker_by_key(api_key):
        return ("cfg-worker", {"name": "worker"}) if api_key == "worker-key" else None

    def fake_broadcast(config_node_id, real_node_id, event):
        broadcasts.append((config_node_id, real_node_id, event))

    resp = _request(
        "POST",
        "/api/nodes/events",
        json_body={
            "node_id": "real-worker",
            "event": {
                "type": "inference_operation",
                "operation": {"id": "op-1", "profile_id": "qwen"},
            },
        },
        headers={"X-Api-Key": "worker-key"},
        patches=(
            (auth, "check_auth", check_auth_spy),
            (cluster_routes, "get_worker_by_api_key", fake_worker_by_key),
            (cluster_routes, "validate_heartbeat_key", lambda node_id, key: node_id == "real-worker" and key == "worker-key"),
            (cluster_routes, "heartbeat_node", lambda node_id: node_id == "real-worker"),
            (cluster_routes, "_broadcast_worker_event", fake_broadcast),
        ),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert auth_calls == []
    assert broadcasts == [(
        "cfg-worker",
        "real-worker",
        {"type": "inference_operation", "operation": {"id": "op-1", "profile_id": "qwen"}},
    )]


def test_mcp_requires_service_token_even_when_auth_true():
    resp = _request(
        "POST",
        "/mcp",
        json_body={"id": 1, "method": "initialize"},
        patches=((auth, "check_auth", _auth_true),),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Service or MCP token required for MCP endpoint"


def test_mcp_initialize_with_scoped_token():
    resp = _request(
        "POST",
        "/mcp",
        json_body={"id": 1, "method": "initialize"},
        patches=((auth, "check_auth", _scoped_auth("svc-a", "read")),),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 1
    assert payload["result"]["protocolVersion"] == "2025-03-26"


def test_mcp_tools_list_limited_for_read_capability():
    resp = _request(
        "POST",
        "/mcp",
        json_body={"id": 2, "method": "tools/list"},
        patches=((auth, "check_auth", _scoped_auth("svc-a", "read")),),
    )
    assert resp.status_code == 200
    tools = {t["name"] for t in resp.json()["result"]["tools"]}
    assert tools == {"logs", "status"}


def test_mcp_tools_list_includes_register_for_deploy_capability():
    resp = _request(
        "POST",
        "/mcp",
        json_body={"id": 21, "method": "tools/list"},
        patches=((auth, "check_auth", _scoped_auth("svc-a", "deploy")),),
    )
    assert resp.status_code == 200
    tools = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "register" in tools
    assert "deploy" not in tools


def test_mcp_tools_call_invalid_params_shape():
    resp = _request(
        "POST",
        "/mcp",
        json_body={"id": 3, "method": "tools/call", "params": "bad"},
        patches=((auth, "check_auth", _scoped_auth("svc-a", "deploy")),),
    )
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32602


def test_services_create_rejects_scope_mismatch():
    resp = _request(
        "POST",
        "/api/services",
        json_body={"name": "svc-b", "command": "python app.py", "working_dir": "/tmp"},
        patches=((auth, "check_auth", _scoped_auth("svc-a", "deploy")),),
    )
    assert resp.status_code == 403
    assert "Token is scoped to service 'svc-a'" == resp.json()["detail"]


def test_services_create_allows_matching_scope():
    async def fake_register_service(
        name,
        command,
        working_dir,
        hostname=None,
        access_policy_id=None,
        lan=False,
    ):
        return {"name": name, "status": "inactive"}

    resp = _request(
        "POST",
        "/api/services",
        json_body={"name": "svc-a", "command": "python app.py", "working_dir": "/tmp"},
        patches=(
            (auth, "check_auth", _scoped_auth("svc-a", "deploy")),
            (main, "register_service", fake_register_service),
        ),
    )
    assert resp.status_code == 201
    assert resp.json() == {"name": "svc-a", "status": "inactive"}


def test_services_delete_rejects_scope_mismatch():
    resp = _request(
        "DELETE",
        "/api/services/svc-b",
        patches=((auth, "check_auth", _scoped_auth("svc-a", "deploy")),),
    )
    assert resp.status_code == 403
    assert "Token is scoped to service 'svc-a'" == resp.json()["detail"]


def test_services_delete_allows_matching_scope():
    async def fake_deregister_service(name):
        return {"port": 8001}

    resp = _request(
        "DELETE",
        "/api/services/svc-a",
        patches=(
            (auth, "check_auth", _scoped_auth("svc-a", "deploy")),
            (main, "deregister_service", fake_deregister_service),
        ),
    )

    assert resp.status_code == 200
    assert resp.json() == {"deleted": "svc-a", "port": 8001}


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
    print("Running app HTTP integration tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
