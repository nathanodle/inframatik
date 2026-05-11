"""Tests for direct main endpoint function behaviors."""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException

import main


class _DummyRequest:
    def __init__(self, scope=None):
        self.state = types.SimpleNamespace(service_scope=scope)


def _run(coro):
    return asyncio.run(coro)


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        _run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def test_api_tunnel_includes_routes_when_available():
    original_status = main.get_tunnel_status
    original_routes = main.get_tunnel_routes

    async def fake_status():
        return {"connected": True}

    async def fake_routes():
        return [{"hostname": "app.example.com"}]

    main.get_tunnel_status = fake_status
    main.get_tunnel_routes = fake_routes
    try:
        result = _run(main.api_tunnel())
    finally:
        main.get_tunnel_status = original_status
        main.get_tunnel_routes = original_routes

    assert result["connected"] is True
    assert result["routes"] == [{"hostname": "app.example.com"}]


def test_api_tunnel_handles_route_fetch_errors():
    original_status = main.get_tunnel_status
    original_routes = main.get_tunnel_routes

    async def fake_status():
        return {"connected": False}

    async def fake_routes():
        raise ValueError("route parse failed")

    main.get_tunnel_status = fake_status
    main.get_tunnel_routes = fake_routes
    try:
        result = _run(main.api_tunnel())
    finally:
        main.get_tunnel_status = original_status
        main.get_tunnel_routes = original_routes

    assert result["connected"] is False
    assert result["routes"] == []
    assert "route parse failed" in result["routes_error"]


def test_api_next_port_returns_503_when_unavailable():
    original_next = main.next_available_port
    main.next_available_port = lambda: None
    try:
        exc = _assert_raises_async(HTTPException, main.api_next_port)
    finally:
        main.next_available_port = original_next
    assert exc.status_code == 503


def test_api_next_port_success():
    original_next = main.next_available_port
    main.next_available_port = lambda: 8111
    try:
        result = _run(main.api_next_port())
    finally:
        main.next_available_port = original_next
    assert result == {"port": 8111}


def test_api_list_services_filters_by_scope():
    original_list = main.list_services

    async def fake_list():
        return [{"name": "svc-a"}, {"name": "svc-b"}]

    main.list_services = fake_list
    try:
        result_all = _run(main.api_list_services(_DummyRequest(scope=None)))
        result_scoped = _run(main.api_list_services(_DummyRequest(scope="svc-b")))
    finally:
        main.list_services = original_list

    assert len(result_all) == 2
    assert result_scoped == [{"name": "svc-b"}]


def test_api_register_service_rejects_scope_mismatch():
    body = main.ServiceCreate(name="svc-a", command="python app.py", working_dir="/tmp/app")
    exc = _assert_raises_async(HTTPException, main.api_register_service, body, _DummyRequest(scope="svc-b"))
    assert exc.status_code == 403


def test_api_register_service_maps_value_error():
    original_register = main.register_service

    async def fake_register(**kwargs):
        raise ValueError("bad command")

    main.register_service = fake_register
    try:
        body = main.ServiceCreate(name="svc-a", command="python app.py", working_dir="/tmp/app")
        exc = _assert_raises_async(HTTPException, main.api_register_service, body, _DummyRequest(scope=None))
    finally:
        main.register_service = original_register

    assert exc.status_code == 400
    assert "bad command" in str(exc.detail)


def test_api_register_service_success():
    original_register = main.register_service
    seen = {}

    async def fake_register(**kwargs):
        seen.update(kwargs)
        return {"name": kwargs["name"], "status": "inactive"}

    main.register_service = fake_register
    try:
        body = main.ServiceCreate(
            name="svc-a",
            command="python app.py",
            working_dir="/tmp/app",
            hostname="app.example.com",
            access_policy_id="pol-1",
        )
        result = _run(main.api_register_service(body, _DummyRequest(scope=None)))
    finally:
        main.register_service = original_register

    assert result == {"name": "svc-a", "status": "inactive"}
    assert seen["hostname"] == "app.example.com"
    assert seen["access_policy_id"] == "pol-1"


def test_api_deregister_service_maps_not_found():
    original_deregister = main.deregister_service

    async def fake_deregister(name):
        raise ValueError("missing")

    main.deregister_service = fake_deregister
    try:
        exc = _assert_raises_async(HTTPException, main.api_deregister_service, "svc-a")
    finally:
        main.deregister_service = original_deregister

    assert exc.status_code == 404


def test_api_start_stop_restart_error_mapping():
    original_start = main.start_service
    original_stop = main.stop_service
    original_restart = main.restart_service

    async def fake_start(name):
        raise ValueError("missing")

    async def fake_stop(name):
        raise RuntimeError("systemctl failed")

    async def fake_restart(name):
        return "active"

    main.start_service = fake_start
    main.stop_service = fake_stop
    main.restart_service = fake_restart
    try:
        exc_start = _assert_raises_async(HTTPException, main.api_start_service, "svc-a")
        exc_stop = _assert_raises_async(HTTPException, main.api_stop_service, "svc-a")
        ok_restart = _run(main.api_restart_service("svc-a"))
    finally:
        main.start_service = original_start
        main.stop_service = original_stop
        main.restart_service = original_restart

    assert exc_start.status_code == 404
    assert exc_stop.status_code == 500
    assert ok_restart == {"name": "svc-a", "status": "active"}


def test_api_service_logs_maps_not_found():
    original_logs = main.get_service_logs

    async def fake_logs(name, lines=100):
        raise ValueError("not found")

    main.get_service_logs = fake_logs
    try:
        exc = _assert_raises_async(HTTPException, main.api_service_logs, "svc-a", 20)
    finally:
        main.get_service_logs = original_logs

    assert exc.status_code == 404


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
    print("Running main endpoint behavior tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
