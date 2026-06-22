"""Tests for proxy local routing helpers and dispatch logic."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cloudflared
import inference_connect
import inference_launchers
import inference_planner
import inference_profiles
import inference_operations
import inference_routes
import proxy
import services
import system
import tunnel


def _run(coro):
    return asyncio.run(coro)


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        _run(coro_fn(*args, **kwargs))
    except exc_type:
        return
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def test_split_route_parses_path_and_query():
    path, query = proxy._split_route("/api/services/a/logs?lines=50&a=1&a=2")
    assert path == "/api/services/a/logs"
    assert query["lines"] == ["50"]
    assert query["a"] == ["1", "2"]


def test_query_int_uses_default_when_missing_or_invalid():
    assert proxy._query_int({}, "lines", 100) == 100
    assert proxy._query_int({"lines": ["abc"]}, "lines", 100) == 100
    assert proxy._query_int({"lines": []}, "lines", 100) == 100
    assert proxy._query_int({"lines": ["33"]}, "lines", 100) == 33


def test_service_name_from_route_validates_input():
    assert proxy._service_name_from_route("/api/services/my-svc/start") == "my-svc"
    _assert_raises(ValueError, proxy._service_name_from_route, "/api/system")
    _assert_raises(ValueError, proxy._service_name_from_route, "/api/services/")


def test_service_action_suffix_parsing():
    assert proxy._service_action_suffix("/api/system") is None
    assert proxy._service_action_suffix("/api/services/my-svc") == ""
    assert proxy._service_action_suffix("/api/services/my-svc/start") == "/start"
    assert proxy._service_action_suffix("/api/services/my-svc/logs") == "/logs"


def test_handle_local_services_non_service_path_returns_no_match():
    result = _run(proxy._handle_local_services("GET", "/api/system", {}))
    assert result is proxy._NO_MATCH


def test_handle_local_services_get_list():
    original_list = services.list_services

    async def fake_list_services():
        return [{"name": "svc-a"}]

    services.list_services = fake_list_services
    try:
        result = _run(proxy._handle_local_services("GET", "/api/services", {}))
    finally:
        services.list_services = original_list

    assert result == [{"name": "svc-a"}]


def test_handle_local_services_post_requires_fields():
    _assert_raises_async(
        ValueError,
        proxy._handle_local_services,
        "POST",
        "/api/services",
        {},
        {"name": "svc-a", "command": "python app.py"},
    )


def test_handle_local_services_post_registers_service():
    original_register = services.register_service
    calls = []

    async def fake_register_service(
        name,
        command,
        working_dir,
        hostname=None,
        access_policy_id=None,
        lan=False,
    ):
        calls.append(
            {
                "name": name,
                "command": command,
                "working_dir": working_dir,
                "hostname": hostname,
                "access_policy_id": access_policy_id,
                "lan": lan,
            }
        )
        return {"name": name, "status": "inactive"}

    services.register_service = fake_register_service
    try:
        payload = {
            "name": "svc-a",
            "command": "python app.py",
            "working_dir": "/tmp/app",
            "hostname": "app.example.com",
            "access_policy_id": "pol-1",
            "lan": True,
        }
        result = _run(proxy._handle_local_services("POST", "/api/services", {}, payload))
    finally:
        services.register_service = original_register

    assert result["name"] == "svc-a"
    assert calls[0]["hostname"] == "app.example.com"
    assert calls[0]["access_policy_id"] == "pol-1"
    assert calls[0]["lan"] is True


def test_handle_local_services_delete_service():
    original_deregister = services.deregister_service

    async def fake_deregister(name):
        return {"port": 8111, "name": name}

    services.deregister_service = fake_deregister
    try:
        result = _run(proxy._handle_local_services("DELETE", "/api/services/svc-a", {}))
    finally:
        services.deregister_service = original_deregister

    assert result["deleted"] == "svc-a"
    assert result["port"] == 8111


def test_handle_local_services_post_actions():
    original_start = services.start_service
    original_stop = services.stop_service
    original_restart = services.restart_service
    calls = []

    async def fake_start(name):
        calls.append(("start", name))
        return "active"

    async def fake_stop(name):
        calls.append(("stop", name))
        return "inactive"

    async def fake_restart(name):
        calls.append(("restart", name))
        return "active"

    services.start_service = fake_start
    services.stop_service = fake_stop
    services.restart_service = fake_restart
    try:
        start = _run(proxy._handle_local_services("POST", "/api/services/svc-a/start", {}))
        stop = _run(proxy._handle_local_services("POST", "/api/services/svc-a/stop", {}))
        restart = _run(proxy._handle_local_services("POST", "/api/services/svc-a/restart", {}))
    finally:
        services.start_service = original_start
        services.stop_service = original_stop
        services.restart_service = original_restart

    assert start == {"name": "svc-a", "status": "active"}
    assert stop == {"name": "svc-a", "status": "inactive"}
    assert restart == {"name": "svc-a", "status": "active"}
    assert calls == [("start", "svc-a"), ("stop", "svc-a"), ("restart", "svc-a")]


def test_handle_local_services_logs_defaults_and_parsing():
    original_logs = services.get_service_logs
    seen = []

    async def fake_logs(name, lines=100):
        seen.append((name, lines))
        return f"logs-{lines}"

    services.get_service_logs = fake_logs
    try:
        result_default = _run(proxy._handle_local_services("GET", "/api/services/svc-a/logs", {}))
        result_valid = _run(
            proxy._handle_local_services(
                "GET",
                "/api/services/svc-a/logs",
                {"lines": ["42"]},
            )
        )
        result_invalid = _run(
            proxy._handle_local_services(
                "GET",
                "/api/services/svc-a/logs",
                {"lines": ["bad"]},
            )
        )
    finally:
        services.get_service_logs = original_logs

    assert result_default == {"name": "svc-a", "logs": "logs-100"}
    assert result_valid == {"name": "svc-a", "logs": "logs-42"}
    assert result_invalid == {"name": "svc-a", "logs": "logs-100"}
    assert seen == [("svc-a", 100), ("svc-a", 42), ("svc-a", 100)]


def test_handle_local_cf_service_status_logs_restart_update():
    original_status = cloudflared.get_cloudflared_user_service_status
    original_logs = cloudflared.get_cloudflared_user_service_logs
    original_restart = cloudflared.restart_cloudflared_user_service
    original_update = cloudflared.update_cloudflared_user_binary
    seen = []

    async def fake_status():
        seen.append(("status",))
        return {"active_state": "active"}

    async def fake_logs(lines=80):
        seen.append(("logs", lines))
        return f"cf-logs-{lines}"

    async def fake_restart():
        seen.append(("restart",))
        return {"active_state": "active"}

    async def fake_update(version=None):
        seen.append(("update", version))
        return {"version_after": version or "default"}

    cloudflared.get_cloudflared_user_service_status = fake_status
    cloudflared.get_cloudflared_user_service_logs = fake_logs
    cloudflared.restart_cloudflared_user_service = fake_restart
    cloudflared.update_cloudflared_user_binary = fake_update
    try:
        status = _run(proxy._handle_local_cf_service("GET", "/api/internal/cf/service/status", {}))
        logs = _run(proxy._handle_local_cf_service("GET", "/api/internal/cf/service/logs", {"lines": ["77"]}))
        restart = _run(proxy._handle_local_cf_service("POST", "/api/internal/cf/service/restart", {}))
        update_default = _run(proxy._handle_local_cf_service("POST", "/api/internal/cf/service/update", {}, None))
        update_versioned = _run(
            proxy._handle_local_cf_service(
                "POST",
                "/api/internal/cf/service/update",
                {},
                {"version": "2025.2.1"},
            )
        )
    finally:
        cloudflared.get_cloudflared_user_service_status = original_status
        cloudflared.get_cloudflared_user_service_logs = original_logs
        cloudflared.restart_cloudflared_user_service = original_restart
        cloudflared.update_cloudflared_user_binary = original_update

    assert status["active_state"] == "active"
    assert logs == {"lines": 77, "logs": "cf-logs-77"}
    assert restart["status"] == "restarted"
    assert update_default["status"] == "updated"
    assert update_default["cloudflared"]["version_after"] == "default"
    assert update_versioned["cloudflared"]["version_after"] == "2025.2.1"
    assert ("update", None) in seen
    assert ("update", "2025.2.1") in seen


def test_handle_local_cf_service_nonmatch():
    result = _run(proxy._handle_local_cf_service("GET", "/api/system", {}))
    assert result is proxy._NO_MATCH


def test_handle_local_inference_launchers_dispatch():
    original_list = inference_launchers.list_launchers
    original_create = inference_launchers.create_launcher
    original_update = inference_launchers.update_launcher
    original_validate = inference_launchers.validate_launcher_path
    original_delete = inference_launchers.delete_launcher
    original_preview = inference_planner.preview_profile
    original_overview = inference_routes.api_inference_overview
    calls = []

    async def fake_overview(include_system=True):
        calls.append(("overview", include_system))
        return {"profiles": {"profiles": []}, "system": include_system}

    inference_launchers.list_launchers = lambda include_validation=False: {
        "launchers": [],
        "include_validation": include_validation,
    }
    inference_launchers.create_launcher = lambda **kwargs: calls.append(("create", kwargs)) or {"id": kwargs["launcher_id"]}
    inference_launchers.update_launcher = lambda launcher_id, body: calls.append(("update", launcher_id, body)) or {"id": launcher_id}
    inference_launchers.validate_launcher_path = lambda launcher_id: calls.append(("validate", launcher_id)) or {"valid": True}
    inference_launchers.delete_launcher = lambda launcher_id, force_stopped_references=False: calls.append(("delete", launcher_id, force_stopped_references)) or {"deleted": launcher_id}
    inference_planner.preview_profile = lambda body: calls.append(("preview", body)) or {"valid_for_save": True}
    inference_routes.api_inference_overview = fake_overview
    try:
        previewed = _run(proxy._handle_local_inference("POST", "/api/inference/profiles/preview", {}, {"id": "draft"}))
        overview = _run(proxy._handle_local_inference("GET", "/api/inference/overview", {"include_system": ["false"]}))
        listed = _run(proxy._handle_local_inference("GET", "/api/inference/launchers", {"include_validation": ["true"]}))
        created = _run(proxy._handle_local_inference("POST", "/api/inference/launchers", {}, {"id": "vllm-main", "engine": "vllm", "executable": "/x"}))
        updated = _run(proxy._handle_local_inference("PUT", "/api/inference/launchers/vllm-main", {}, {"base_args": ["serve"]}))
        validated = _run(proxy._handle_local_inference("POST", "/api/inference/launchers/vllm-main/validate", {}))
        deleted = _run(proxy._handle_local_inference("DELETE", "/api/inference/launchers/vllm-main", {"force_stopped_references": ["true"]}))
    finally:
        inference_launchers.list_launchers = original_list
        inference_launchers.create_launcher = original_create
        inference_launchers.update_launcher = original_update
        inference_launchers.validate_launcher_path = original_validate
        inference_launchers.delete_launcher = original_delete
        inference_planner.preview_profile = original_preview
        inference_routes.api_inference_overview = original_overview

    assert previewed == {"valid_for_save": True}
    assert overview == {"profiles": {"profiles": []}, "system": False}
    assert listed["include_validation"] is True
    assert created == {"id": "vllm-main"}
    assert updated == {"id": "vllm-main"}
    assert validated == {"valid": True}
    assert deleted == {"deleted": "vllm-main"}
    assert calls[0] == ("preview", {"id": "draft"})
    assert calls[1] == ("overview", False)
    assert calls[-1] == ("delete", "vllm-main", True)


def test_handle_local_inference_profiles_dispatch():
    original_list = inference_profiles.list_profiles
    original_create = inference_profiles.create_profile
    original_get = inference_profiles.get_profile
    original_update = inference_profiles.update_profile
    original_delete = inference_connect.delete_profile_with_cleanup
    original_render = inference_profiles.render_profile
    original_detail = inference_routes.api_inference_profile_detail
    calls = []

    async def fake_detail(profile_id):
        calls.append(("detail", profile_id))
        return {"profile": {"id": profile_id}, "instances": {"instances": []}, "plan": {}}

    inference_profiles.list_profiles = lambda: calls.append(("list",)) or {"profiles": []}
    inference_profiles.create_profile = lambda body: calls.append(("create", body)) or {"profile": {"id": body["id"]}}
    inference_profiles.get_profile = lambda profile_id: calls.append(("get", profile_id)) or {"id": profile_id}
    inference_profiles.update_profile = lambda profile_id, body: calls.append(("update", profile_id, body)) or {"id": profile_id}
    async def fake_delete(profile_id, force=False, delete_owned_tokens=False):
        calls.append(("delete", profile_id, force, delete_owned_tokens))
        return {"deleted": profile_id}

    inference_connect.delete_profile_with_cleanup = fake_delete
    inference_profiles.render_profile = lambda profile_id: calls.append(("render", profile_id)) or {"valid_for_save": True}
    inference_routes.api_inference_profile_detail = fake_detail
    try:
        listed = _run(proxy._handle_local_inference("GET", "/api/inference/profiles", {}))
        created = _run(proxy._handle_local_inference("POST", "/api/inference/profiles", {}, {"id": "qwen"}))
        detail = _run(proxy._handle_local_inference("GET", "/api/inference/profiles/qwen", {}))
        detail_bundle = _run(proxy._handle_local_inference("GET", "/api/inference/profiles/qwen/detail", {}))
        updated = _run(proxy._handle_local_inference("PUT", "/api/inference/profiles/qwen", {}, {"display_name": "Qwen"}))
        rendered = _run(proxy._handle_local_inference("POST", "/api/inference/profiles/qwen/render", {}))
        deleted = _run(proxy._handle_local_inference("DELETE", "/api/inference/profiles/qwen", {"force": ["true"]}))
    finally:
        inference_profiles.list_profiles = original_list
        inference_profiles.create_profile = original_create
        inference_profiles.get_profile = original_get
        inference_profiles.update_profile = original_update
        inference_connect.delete_profile_with_cleanup = original_delete
        inference_profiles.render_profile = original_render
        inference_routes.api_inference_profile_detail = original_detail

    assert listed == {"profiles": []}
    assert created == {"profile": {"id": "qwen"}}
    assert detail == {"id": "qwen"}
    assert detail_bundle["profile"]["id"] == "qwen"
    assert updated == {"id": "qwen"}
    assert rendered == {"valid_for_save": True}
    assert deleted == {"deleted": "qwen"}
    assert calls == [
        ("list",),
        ("create", {"id": "qwen"}),
        ("get", "qwen"),
        ("detail", "qwen"),
        ("update", "qwen", {"display_name": "Qwen"}),
        ("render", "qwen"),
        ("delete", "qwen", True, False),
    ]


def test_handle_local_inference_operations_dispatch():
    original_start = inference_operations.start_profile
    original_stop = inference_operations.stop_profile
    original_restart = inference_operations.restart_profile
    original_instances = inference_operations.get_profile_instances
    original_logs = inference_operations.get_profile_logs
    original_health = inference_operations.get_profile_health
    original_list_ops = inference_operations.list_operations
    original_get_op = inference_operations.get_operation
    original_cancel = inference_operations.cancel_operation
    calls = []

    async def fake_start(profile_id):
        calls.append(("start", profile_id))
        return {"id": "op_start"}

    async def fake_stop(profile_id):
        calls.append(("stop", profile_id))
        return {"id": "op_stop"}

    async def fake_restart(profile_id):
        calls.append(("restart", profile_id))
        return {"id": "op_restart"}

    async def fake_instances(profile_id):
        calls.append(("instances", profile_id))
        return {"instances": []}

    async def fake_logs(profile_id, lines=150, instance_index=None):
        calls.append(("logs", profile_id, lines, instance_index))
        return {"logs": ""}

    async def fake_health(profile_id):
        calls.append(("health", profile_id))
        return {"health": "healthy"}

    inference_operations.start_profile = fake_start
    inference_operations.stop_profile = fake_stop
    inference_operations.restart_profile = fake_restart
    inference_operations.get_profile_instances = fake_instances
    inference_operations.get_profile_logs = fake_logs
    inference_operations.get_profile_health = fake_health
    inference_operations.list_operations = lambda profile_id=None, state=None: calls.append(("list_ops", profile_id, state)) or {"operations": []}
    inference_operations.get_operation = lambda op_id: calls.append(("get_op", op_id)) or {"id": op_id}
    inference_operations.cancel_operation = lambda op_id: calls.append(("cancel", op_id)) or {"id": op_id, "state": "canceled"}
    try:
        started = _run(proxy._handle_local_inference("POST", "/api/inference/profiles/qwen/start", {}))
        stopped = _run(proxy._handle_local_inference("POST", "/api/inference/profiles/qwen/stop", {}))
        restarted = _run(proxy._handle_local_inference("POST", "/api/inference/profiles/qwen/restart", {}))
        instances = _run(proxy._handle_local_inference("GET", "/api/inference/profiles/qwen/instances", {}))
        logs = _run(proxy._handle_local_inference("GET", "/api/inference/profiles/qwen/logs", {"lines": ["22"], "instance": ["1"]}))
        health = _run(proxy._handle_local_inference("GET", "/api/inference/profiles/qwen/health", {}))
        ops = _run(proxy._handle_local_inference("GET", "/api/inference/operations", {"profile_id": ["qwen"], "state": ["running"]}))
        op = _run(proxy._handle_local_inference("GET", "/api/inference/operations/op_1", {}))
        canceled = _run(proxy._handle_local_inference("POST", "/api/inference/operations/op_1/cancel", {}))
    finally:
        inference_operations.start_profile = original_start
        inference_operations.stop_profile = original_stop
        inference_operations.restart_profile = original_restart
        inference_operations.get_profile_instances = original_instances
        inference_operations.get_profile_logs = original_logs
        inference_operations.get_profile_health = original_health
        inference_operations.list_operations = original_list_ops
        inference_operations.get_operation = original_get_op
        inference_operations.cancel_operation = original_cancel

    assert started == {"id": "op_start"}
    assert stopped == {"id": "op_stop"}
    assert restarted == {"id": "op_restart"}
    assert instances == {"instances": []}
    assert logs == {"logs": ""}
    assert health == {"health": "healthy"}
    assert ops == {"operations": []}
    assert op == {"id": "op_1"}
    assert canceled == {"id": "op_1", "state": "canceled"}
    assert ("logs", "qwen", 22, 1) in calls
    assert ("list_ops", "qwen", "running") in calls


def test_handle_local_system_dispatch():
    original_metrics = system.get_system_metrics
    system.get_system_metrics = lambda: {"cpu": {"percent": 10}}
    try:
        result = _run(proxy._handle_local("GET", "/api/system"))
    finally:
        system.get_system_metrics = original_metrics
    assert result["cpu"]["percent"] == 10


def test_handle_local_tunnel_dispatch_skips_routes_by_default():
    original_status = tunnel.get_tunnel_status
    original_routes = tunnel.get_tunnel_routes
    calls = {"routes": 0}

    async def fake_status():
        return {"connected": True}

    async def fake_routes():
        calls["routes"] += 1
        raise ValueError("bad route parse")

    tunnel.get_tunnel_status = fake_status
    tunnel.get_tunnel_routes = fake_routes
    try:
        result = _run(proxy._handle_local("GET", "/api/tunnel"))
    finally:
        tunnel.get_tunnel_status = original_status
        tunnel.get_tunnel_routes = original_routes

    assert result["connected"] is True
    assert "routes" not in result
    assert calls["routes"] == 0


def test_handle_local_tunnel_dispatch_with_routes_error_when_requested():
    original_status = tunnel.get_tunnel_status
    original_routes = tunnel.get_tunnel_routes

    async def fake_status():
        return {"connected": True}

    async def fake_routes():
        raise ValueError("bad route parse")

    tunnel.get_tunnel_status = fake_status
    tunnel.get_tunnel_routes = fake_routes
    try:
        result = _run(proxy._handle_local("GET", "/api/tunnel?include_routes=true"))
    finally:
        tunnel.get_tunnel_status = original_status
        tunnel.get_tunnel_routes = original_routes

    assert result["connected"] is True
    assert result["routes"] == []
    assert "bad route parse" in result["routes_error"]


def test_handle_local_unknown_route_raises():
    _assert_raises_async(ValueError, proxy._handle_local, "GET", "/api/does-not-exist")


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
    print("Running proxy routing tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
