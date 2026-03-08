"""Tests for cloudflared service control routes in cf_routes."""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException

import cf_routes


class _DummyRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        asyncio.run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def test_require_internal_api_key_rejects_missing():
    original_get_cfg = cf_routes.get_node_config
    cf_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        exc = _assert_raises(
            HTTPException,
            cf_routes._require_internal_api_key,
            _DummyRequest(headers={}),
        )
    finally:
        cf_routes.get_node_config = original_get_cfg
    assert exc.status_code == 401


def test_require_internal_api_key_rejects_mismatch():
    original_get_cfg = cf_routes.get_node_config
    cf_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        exc = _assert_raises(
            HTTPException,
            cf_routes._require_internal_api_key,
            _DummyRequest(headers={"X-Api-Key": "wrong"}),
        )
    finally:
        cf_routes.get_node_config = original_get_cfg
    assert exc.status_code == 401


def test_require_internal_api_key_allows_match():
    original_get_cfg = cf_routes.get_node_config
    cf_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        cf_routes._require_internal_api_key(_DummyRequest(headers={"X-Api-Key": "k1"}))
    finally:
        cf_routes.get_node_config = original_get_cfg


def test_api_cf_service_logs_success():
    original_logs = cf_routes.get_cloudflared_user_service_logs

    async def fake_logs(lines=80):
        return f"log-lines-{lines}"

    cf_routes.get_cloudflared_user_service_logs = fake_logs
    try:
        result = asyncio.run(cf_routes.api_cf_service_logs(lines=33))
    finally:
        cf_routes.get_cloudflared_user_service_logs = original_logs

    assert result == {"lines": 33, "logs": "log-lines-33"}


def test_api_cf_service_logs_maps_value_error_to_400():
    original_logs = cf_routes.get_cloudflared_user_service_logs

    async def fake_logs(lines=80):
        raise ValueError("bad lines")

    cf_routes.get_cloudflared_user_service_logs = fake_logs
    try:
        exc = _assert_raises_async(HTTPException, cf_routes.api_cf_service_logs, 0)
    finally:
        cf_routes.get_cloudflared_user_service_logs = original_logs

    assert exc.status_code == 400
    assert "bad lines" in str(exc.detail)


def test_api_cf_service_logs_maps_runtime_error_to_500():
    original_logs = cf_routes.get_cloudflared_user_service_logs

    async def fake_logs(lines=80):
        raise RuntimeError("journal unavailable")

    cf_routes.get_cloudflared_user_service_logs = fake_logs
    try:
        exc = _assert_raises_async(HTTPException, cf_routes.api_cf_service_logs, 80)
    finally:
        cf_routes.get_cloudflared_user_service_logs = original_logs

    assert exc.status_code == 500
    assert "journal unavailable" in str(exc.detail)


def test_api_cf_service_restart_maps_runtime_error():
    original_restart = cf_routes.restart_cloudflared_user_service

    async def fake_restart():
        raise RuntimeError("restart failed")

    cf_routes.restart_cloudflared_user_service = fake_restart
    try:
        exc = _assert_raises_async(HTTPException, cf_routes.api_cf_service_restart)
    finally:
        cf_routes.restart_cloudflared_user_service = original_restart

    assert exc.status_code == 500
    assert "restart failed" in str(exc.detail)


def test_api_cf_service_update_success():
    original_update = cf_routes.update_cloudflared_user_binary

    async def fake_update(version=None):
        return {"version_after": version or "default"}

    cf_routes.update_cloudflared_user_binary = fake_update
    try:
        body = cf_routes.UpdateCloudflaredBody(version="2025.2.1")
        result = asyncio.run(cf_routes.api_cf_service_update(body))
    finally:
        cf_routes.update_cloudflared_user_binary = original_update

    assert result["status"] == "updated"
    assert result["cloudflared"]["version_after"] == "2025.2.1"


def test_api_cf_service_update_maps_value_error():
    original_update = cf_routes.update_cloudflared_user_binary

    async def fake_update(version=None):
        raise ValueError("invalid version")

    cf_routes.update_cloudflared_user_binary = fake_update
    try:
        exc = _assert_raises_async(HTTPException, cf_routes.api_cf_service_update, cf_routes.UpdateCloudflaredBody(version="x"))
    finally:
        cf_routes.update_cloudflared_user_binary = original_update

    assert exc.status_code == 400
    assert "invalid version" in str(exc.detail)


def test_api_cf_service_update_maps_runtime_error():
    original_update = cf_routes.update_cloudflared_user_binary

    async def fake_update(version=None):
        raise RuntimeError("download failed")

    cf_routes.update_cloudflared_user_binary = fake_update
    try:
        exc = _assert_raises_async(HTTPException, cf_routes.api_cf_service_update, cf_routes.UpdateCloudflaredBody(version="x"))
    finally:
        cf_routes.update_cloudflared_user_binary = original_update

    assert exc.status_code == 500
    assert "download failed" in str(exc.detail)


def test_api_internal_cf_service_update_requires_key():
    original_get_cfg = cf_routes.get_node_config
    cf_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.api_internal_cf_service_update,
            _DummyRequest(headers={}),
            cf_routes.UpdateCloudflaredBody(version="2025.2.1"),
        )
    finally:
        cf_routes.get_node_config = original_get_cfg
    assert exc.status_code == 401


def test_api_internal_cf_service_update_delegates_when_key_valid():
    original_get_cfg = cf_routes.get_node_config
    original_update = cf_routes.update_cloudflared_user_binary
    cf_routes.get_node_config = lambda: {"api_key": "k1"}

    async def fake_update(version=None):
        return {"version_after": version or "default"}

    cf_routes.update_cloudflared_user_binary = fake_update
    try:
        result = asyncio.run(
            cf_routes.api_internal_cf_service_update(
                _DummyRequest(headers={"X-Api-Key": "k1"}),
                cf_routes.UpdateCloudflaredBody(version="2025.2.1"),
            )
        )
    finally:
        cf_routes.get_node_config = original_get_cfg
        cf_routes.update_cloudflared_user_binary = original_update

    assert result["status"] == "updated"
    assert result["cloudflared"]["version_after"] == "2025.2.1"


def test_api_receive_tunnel_token_requires_api_key():
    original_get_cfg = cf_routes.get_node_config
    cf_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.api_receive_tunnel_token,
            cf_routes.ReceiveTunnelTokenBody(tunnel_id="tid", token="tok"),
            _DummyRequest(headers={}),
        )
    finally:
        cf_routes.get_node_config = original_get_cfg
    assert exc.status_code == 401


def test_api_receive_tunnel_token_success_sets_tunnel_and_starts_service():
    original_get_cfg = cf_routes.get_node_config
    original_set_tid = cf_routes.set_tunnel_id
    original_setup = cf_routes.setup_cloudflared_user_service
    seen = {}
    cf_routes.get_node_config = lambda: {"api_key": "k1"}
    cf_routes.set_tunnel_id = lambda tunnel_id: seen.setdefault("tunnel_id", tunnel_id)

    async def fake_setup(token):
        seen.setdefault("token", token)

    cf_routes.setup_cloudflared_user_service = fake_setup
    try:
        result = asyncio.run(
            cf_routes.api_receive_tunnel_token(
                cf_routes.ReceiveTunnelTokenBody(tunnel_id="tid-1", token="tok-1"),
                _DummyRequest(headers={"X-Api-Key": "k1"}),
            )
        )
    finally:
        cf_routes.get_node_config = original_get_cfg
        cf_routes.set_tunnel_id = original_set_tid
        cf_routes.setup_cloudflared_user_service = original_setup

    assert result == {"status": "token_received", "tunnel_id": "tid-1"}
    assert seen == {"tunnel_id": "tid-1", "token": "tok-1"}


def test_api_receive_tunnel_token_maps_setup_errors():
    original_get_cfg = cf_routes.get_node_config
    original_setup = cf_routes.setup_cloudflared_user_service
    original_set_tid = cf_routes.set_tunnel_id
    cf_routes.get_node_config = lambda: {"api_key": "k1"}
    cf_routes.set_tunnel_id = lambda tunnel_id: None

    async def fake_setup_value(token):
        raise ValueError("bad token")

    cf_routes.setup_cloudflared_user_service = fake_setup_value
    try:
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.api_receive_tunnel_token,
            cf_routes.ReceiveTunnelTokenBody(tunnel_id="tid", token="tok"),
            _DummyRequest(headers={"X-Api-Key": "k1"}),
        )
    finally:
        cf_routes.get_node_config = original_get_cfg
        cf_routes.setup_cloudflared_user_service = original_setup
        cf_routes.set_tunnel_id = original_set_tid

    assert exc.status_code == 400
    assert "bad token" in str(exc.detail)

    cf_routes.get_node_config = lambda: {"api_key": "k1"}
    cf_routes.set_tunnel_id = lambda tunnel_id: None

    async def fake_setup_runtime(token):
        raise RuntimeError("systemd failed")

    cf_routes.setup_cloudflared_user_service = fake_setup_runtime
    try:
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.api_receive_tunnel_token,
            cf_routes.ReceiveTunnelTokenBody(tunnel_id="tid", token="tok"),
            _DummyRequest(headers={"X-Api-Key": "k1"}),
        )
    finally:
        cf_routes.get_node_config = original_get_cfg
        cf_routes.setup_cloudflared_user_service = original_setup
        cf_routes.set_tunnel_id = original_set_tid

    assert exc.status_code == 500
    assert "systemd failed" in str(exc.detail)


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
    print("Running cf route service control tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
