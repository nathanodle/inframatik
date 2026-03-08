"""Tests for auth.check_auth request path handling."""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth
import node_config


class _DummyRequest:
    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.state = types.SimpleNamespace(service_scope=None, service_capability=None)


def _run(coro):
    return asyncio.run(coro)


def test_check_auth_accepts_valid_x_api_key():
    original_get_node_config = auth.get_node_config
    auth.get_node_config = lambda: {"api_key": "api-123"}
    try:
        req = _DummyRequest(headers={"X-Api-Key": "api-123"})
        assert _run(auth.check_auth(req))
    finally:
        auth.get_node_config = original_get_node_config


def test_check_auth_accepts_valid_cf_jwt():
    original_get_node_config = auth.get_node_config
    original_validate_cf_access_claims = auth.validate_cf_access_claims
    auth.get_node_config = lambda: {"cf_team_domain": "team", "cf_access_aud": "aud"}

    async def fake_validate_cf_access_claims(token: str, config: dict):
        if token == "jwt-ok" and config.get("cf_access_aud") == "aud":
            return {"email": "ops@example.com"}
        return None

    auth.validate_cf_access_claims = fake_validate_cf_access_claims
    try:
        req = _DummyRequest(headers={"Cf-Access-Jwt-Assertion": "jwt-ok"})
        assert _run(auth.check_auth(req))
        assert req.state.user_email == "ops@example.com"
    finally:
        auth.get_node_config = original_get_node_config
        auth.validate_cf_access_claims = original_validate_cf_access_claims


def test_check_auth_accepts_valid_session_cookie():
    original_validate_session = auth.validate_session
    original_get_node_config = auth.get_node_config
    auth.get_node_config = lambda: {}
    auth.validate_session = lambda token: token == "sess-cookie"
    try:
        req = _DummyRequest(cookies={auth.SESSION_COOKIE_NAME: "sess-cookie"})
        assert _run(auth.check_auth(req))
    finally:
        auth.validate_session = original_validate_session
        auth.get_node_config = original_get_node_config


def test_check_auth_accepts_valid_bearer_session():
    original_validate_session = auth.validate_session
    original_get_node_config = auth.get_node_config
    auth.get_node_config = lambda: {}
    auth.validate_session = lambda token: token == "sess-bearer"
    try:
        req = _DummyRequest(headers={"Authorization": "Bearer sess-bearer"})
        assert _run(auth.check_auth(req))
    finally:
        auth.validate_session = original_validate_session
        auth.get_node_config = original_get_node_config


def test_check_auth_accepts_service_token_and_sets_scope():
    original_validate_session = auth.validate_session
    original_get_node_config = auth.get_node_config
    original_get_service_token_auth = node_config.get_service_token_auth
    auth.get_node_config = lambda: {}
    auth.validate_session = lambda _token: False
    node_config.get_service_token_auth = lambda token: (
        {"service": "svc-a", "capability": "operate"}
        if token == "svc_token"
        else None
    )
    try:
        req = _DummyRequest(headers={"Authorization": "Bearer svc_token"})
        assert _run(auth.check_auth(req))
        assert req.state.service_scope == "svc-a"
        assert req.state.service_capability == "operate"
    finally:
        auth.validate_session = original_validate_session
        auth.get_node_config = original_get_node_config
        node_config.get_service_token_auth = original_get_service_token_auth


def test_check_auth_rejects_service_token_without_service_scope():
    original_validate_session = auth.validate_session
    original_get_node_config = auth.get_node_config
    original_get_service_token_auth = node_config.get_service_token_auth
    auth.get_node_config = lambda: {}
    auth.validate_session = lambda _token: False
    node_config.get_service_token_auth = lambda _token: {"capability": "read"}
    try:
        req = _DummyRequest(headers={"Authorization": "Bearer svc_no_scope"})
        assert not _run(auth.check_auth(req))
        assert req.state.service_scope is None
    finally:
        auth.validate_session = original_validate_session
        auth.get_node_config = original_get_node_config
        node_config.get_service_token_auth = original_get_service_token_auth


def test_check_auth_session_bearer_takes_precedence_over_service_scope():
    original_validate_session = auth.validate_session
    original_get_node_config = auth.get_node_config
    original_get_service_token_auth = node_config.get_service_token_auth
    auth.get_node_config = lambda: {}
    auth.validate_session = lambda token: token == "svc_real_session"

    def fake_get_service_token_auth(_token: str):
        raise AssertionError("Service token lookup should not run when session token already valid")

    node_config.get_service_token_auth = fake_get_service_token_auth
    try:
        req = _DummyRequest(headers={"Authorization": "Bearer svc_real_session"})
        assert _run(auth.check_auth(req))
        assert req.state.service_scope is None
    finally:
        auth.validate_session = original_validate_session
        auth.get_node_config = original_get_node_config
        node_config.get_service_token_auth = original_get_service_token_auth


def test_check_auth_rejects_invalid_bearer_token():
    original_validate_session = auth.validate_session
    original_get_node_config = auth.get_node_config
    auth.get_node_config = lambda: {}
    auth.validate_session = lambda _token: False
    try:
        req = _DummyRequest(headers={"Authorization": "Bearer not_service_prefix"})
        assert not _run(auth.check_auth(req))
    finally:
        auth.validate_session = original_validate_session
        auth.get_node_config = original_get_node_config


def test_check_auth_rejects_when_no_auth_paths_match():
    original_get_node_config = auth.get_node_config
    original_validate_session = auth.validate_session
    original_validate_cf_access_claims = auth.validate_cf_access_claims
    auth.get_node_config = lambda: {"api_key": "api-123", "cf_team_domain": "team", "cf_access_aud": "aud"}
    auth.validate_session = lambda _token: False

    async def fake_validate_cf_access_claims(_token: str, _config: dict):
        return None

    auth.validate_cf_access_claims = fake_validate_cf_access_claims
    try:
        req = _DummyRequest(headers={"X-Api-Key": "wrong"})
        assert not _run(auth.check_auth(req))
    finally:
        auth.get_node_config = original_get_node_config
        auth.validate_session = original_validate_session
        auth.validate_cf_access_claims = original_validate_cf_access_claims


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
    print("Running auth check path tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
