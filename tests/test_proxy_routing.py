"""Tests for proxy local routing helpers and dispatch logic."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cloudflared
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

    async def fake_register_service(name, command, working_dir, hostname=None, lan=False):
        calls.append(
            {
                "name": name,
                "command": command,
                "working_dir": working_dir,
                "hostname": hostname,
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
            "lan": True,
        }
        result = _run(proxy._handle_local_services("POST", "/api/services", {}, payload))
    finally:
        services.register_service = original_register

    assert result["name"] == "svc-a"
    assert calls[0]["hostname"] == "app.example.com"
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


def test_handle_local_system_dispatch():
    original_metrics = system.get_system_metrics
    system.get_system_metrics = lambda: {"cpu": {"percent": 10}}
    try:
        result = _run(proxy._handle_local("GET", "/api/system"))
    finally:
        system.get_system_metrics = original_metrics
    assert result["cpu"]["percent"] == 10


def test_handle_local_tunnel_dispatch_with_routes_error():
    original_status = tunnel.get_tunnel_status
    original_routes = tunnel.get_tunnel_routes

    async def fake_status():
        return {"connected": True}

    async def fake_routes():
        raise ValueError("bad route parse")

    tunnel.get_tunnel_status = fake_status
    tunnel.get_tunnel_routes = fake_routes
    try:
        result = _run(proxy._handle_local("GET", "/api/tunnel"))
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
