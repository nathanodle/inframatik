"""Tests for login throttling and backoff behavior."""

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import bcrypt  # noqa: F401
except Exception:
    sys.modules["bcrypt"] = types.SimpleNamespace(
        hashpw=lambda password, salt: b"",
        gensalt=lambda: b"",
        checkpw=lambda password, hashed: True,
    )

try:
    import httpx  # noqa: F401
except Exception:
    class _DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            raise RuntimeError("httpx stub")

    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=_DummyAsyncClient)

try:
    import jwt  # noqa: F401
except Exception:
    class _DummyRSA:
        @staticmethod
        def from_jwk(_):
            return ""

    class _DummyJwt(types.SimpleNamespace):
        InvalidTokenError = Exception
        algorithms = types.SimpleNamespace(RSAAlgorithm=_DummyRSA)

        @staticmethod
        def decode(*args, **kwargs):
            return {}

    sys.modules["jwt"] = _DummyJwt()

import auth


def _with_tuned_limits(fn):
    def wrapper():
        original = (
            auth.LOGIN_WINDOW_SECONDS,
            auth.LOGIN_MAX_ATTEMPTS,
            auth.LOGIN_BASE_BACKOFF_SECONDS,
            auth.LOGIN_MAX_BACKOFF_SECONDS,
        )
        auth.LOGIN_WINDOW_SECONDS = 1
        auth.LOGIN_MAX_ATTEMPTS = 2
        auth.LOGIN_BASE_BACKOFF_SECONDS = 1
        auth.LOGIN_MAX_BACKOFF_SECONDS = 2
        auth._login_failures.clear()
        try:
            fn()
        finally:
            auth.LOGIN_WINDOW_SECONDS = original[0]
            auth.LOGIN_MAX_ATTEMPTS = original[1]
            auth.LOGIN_BASE_BACKOFF_SECONDS = original[2]
            auth.LOGIN_MAX_BACKOFF_SECONDS = original[3]
            auth._login_failures.clear()
    wrapper.__name__ = fn.__name__
    return wrapper


@_with_tuned_limits
def test_login_backoff_triggers_after_failures():
    client = "192.0.2.10"
    assert auth.record_failed_login(client) == 0
    retry = auth.record_failed_login(client)
    assert retry >= 1
    allowed, retry_after = auth.login_is_allowed(client)
    assert not allowed
    assert retry_after >= 1


@_with_tuned_limits
def test_login_backoff_expires_after_window():
    client = "192.0.2.11"
    auth.record_failed_login(client)
    auth.record_failed_login(client)
    allowed, _ = auth.login_is_allowed(client)
    assert not allowed
    time.sleep(1.2)
    allowed, retry_after = auth.login_is_allowed(client)
    assert allowed
    assert retry_after == 0


@_with_tuned_limits
def test_successful_login_clears_failures():
    client = "192.0.2.12"
    auth.record_failed_login(client)
    auth.record_successful_login(client)
    allowed, retry_after = auth.login_is_allowed(client)
    assert allowed
    assert retry_after == 0


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
    print("Running auth throttle tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
