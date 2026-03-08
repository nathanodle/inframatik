"""Tests for main auth middleware and service-token capability routing."""

import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.responses import JSONResponse

import auth
import main


class _DummyURL:
    def __init__(self, path: str):
        self.path = path


class _DummyRequest:
    def __init__(self, path: str, method: str = "GET"):
        self.url = _DummyURL(path)
        self.method = method
        self.state = types.SimpleNamespace(service_scope=None, service_capability=None)


def _response_json(resp):
    return json.loads(resp.body.decode("utf-8"))


def _run(coro):
    return asyncio.run(coro)


def test_service_token_required_capability_mapping():
    assert main._service_token_required_capability("/mcp", "POST") == "read"
    assert main._service_token_required_capability("/api/ports/next", "GET") == "deploy"
    assert main._service_token_required_capability("/api/services", "GET") == "read"
    assert main._service_token_required_capability("/api/services", "POST") == "deploy"
    assert main._service_token_required_capability("/api/services/a/logs", "GET") == "read"
    assert main._service_token_required_capability("/api/services/a/start", "POST") == "operate"
    assert main._service_token_required_capability("/api/services/a/stop", "POST") == "operate"
    assert main._service_token_required_capability("/api/services/a/restart", "POST") == "operate"
    assert main._service_token_required_capability("/api/services/a", "DELETE") == "deploy"
    assert main._service_token_required_capability("/api/system", "GET") is None
    assert main._service_token_required_capability("/api/services/a", "PATCH") is None


def test_auth_middleware_bypasses_public_paths_without_check_auth():
    original_check_auth = auth.check_auth
    calls = []

    async def fake_check_auth(_request):
        calls.append("check")
        return False

    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/")

        async def call_next(_req):
            return JSONResponse({"ok": True})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth

    assert response.status_code == 200
    assert _response_json(response) == {"ok": True}
    assert calls == []


def test_auth_middleware_bypasses_static_paths():
    original_check_auth = auth.check_auth
    called = []

    async def fake_check_auth(_request):
        called.append(True)
        return False

    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/static/app.js")

        async def call_next(_req):
            return JSONResponse({"ok": "static"})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth

    assert response.status_code == 200
    assert _response_json(response) == {"ok": "static"}
    assert called == []


def test_auth_middleware_bypasses_self_auth_paths():
    original_check_auth = auth.check_auth
    called = []

    async def fake_check_auth(_request):
        called.append(True)
        return False

    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/api/nodes/register", method="POST")

        async def call_next(_req):
            return JSONResponse({"ok": "self"})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth

    assert response.status_code == 200
    assert _response_json(response) == {"ok": "self"}
    assert called == []


def test_auth_middleware_returns_401_when_auth_fails():
    original_check_auth = auth.check_auth

    async def fake_check_auth(_request):
        return False

    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/api/system")

        async def call_next(_req):
            return JSONResponse({"ok": True})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth

    assert response.status_code == 401
    assert _response_json(response)["detail"] == "Authentication required"


def test_auth_middleware_allows_authenticated_non_scoped_request():
    original_check_auth = auth.check_auth

    async def fake_check_auth(_request):
        return True

    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/api/system")

        async def call_next(_req):
            return JSONResponse({"ok": "authed"})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth

    assert response.status_code == 200
    assert _response_json(response) == {"ok": "authed"}


def test_auth_middleware_denies_scoped_token_on_unallowed_endpoint():
    original_check_auth = auth.check_auth

    async def fake_check_auth(request):
        request.state.service_scope = "svc-a"
        request.state.service_capability = "deploy"
        return True

    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/api/system")

        async def call_next(_req):
            return JSONResponse({"ok": True})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth

    assert response.status_code == 403
    assert "cannot access this endpoint" in _response_json(response)["detail"]


def test_auth_middleware_denies_scoped_token_when_capability_insufficient():
    original_check_auth = auth.check_auth
    original_allows = main.service_token_capability_allows

    async def fake_check_auth(request):
        request.state.service_scope = "svc-a"
        request.state.service_capability = "read"
        return True

    main.service_token_capability_allows = lambda current, required: False
    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/api/services/svc-a/restart", method="POST")

        async def call_next(_req):
            return JSONResponse({"ok": True})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth
        main.service_token_capability_allows = original_allows

    assert response.status_code == 403
    assert "cannot perform this action" in _response_json(response)["detail"]


def test_auth_middleware_denies_scoped_token_service_path_mismatch():
    original_check_auth = auth.check_auth
    original_allows = main.service_token_capability_allows

    async def fake_check_auth(request):
        request.state.service_scope = "svc-a"
        request.state.service_capability = "deploy"
        return True

    main.service_token_capability_allows = lambda current, required: True
    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/api/services/svc-b", method="DELETE")

        async def call_next(_req):
            return JSONResponse({"ok": True})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth
        main.service_token_capability_allows = original_allows

    assert response.status_code == 403
    assert "Token is scoped to service 'svc-a'" == _response_json(response)["detail"]


def test_auth_middleware_allows_scoped_token_when_path_and_capability_match():
    original_check_auth = auth.check_auth
    original_allows = main.service_token_capability_allows

    async def fake_check_auth(request):
        request.state.service_scope = "svc-a"
        request.state.service_capability = "operate"
        return True

    main.service_token_capability_allows = lambda current, required: True
    auth.check_auth = fake_check_auth
    try:
        request = _DummyRequest("/api/services/svc-a/restart", method="POST")

        async def call_next(_req):
            return JSONResponse({"ok": "allowed"})

        response = _run(main.auth_middleware(request, call_next))
    finally:
        auth.check_auth = original_check_auth
        main.service_token_capability_allows = original_allows

    assert response.status_code == 200
    assert _response_json(response) == {"ok": "allowed"}


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
    print("Running main auth middleware tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
