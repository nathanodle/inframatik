"""Tests for updater signing, packaging, and worker push behavior."""

import asyncio
import io
import json
import os
import sys
import tarfile
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import updater


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        asyncio.run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def _run_with_temp_signing_paths(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original = (
                updater.SIGNING_DIR,
                updater.SIGNING_PRIVATE_KEY,
                updater.SIGNING_PUBLIC_KEY,
            )
            updater.SIGNING_DIR = tmp / "signing"
            updater.SIGNING_PRIVATE_KEY = updater.SIGNING_DIR / "update_signing_key.pem"
            updater.SIGNING_PUBLIC_KEY = updater.SIGNING_DIR / "update_signing_key.pub.pem"
            try:
                fn(tmp)
            finally:
                updater.SIGNING_DIR = original[0]
                updater.SIGNING_PRIVATE_KEY = original[1]
                updater.SIGNING_PUBLIC_KEY = original[2]
    wrapper.__name__ = fn.__name__
    return wrapper


def _run_with_temp_app_dir(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir:
            original_app_dir = updater.APP_DIR
            updater.APP_DIR = Path(tmpdir)
            try:
                fn(Path(tmpdir))
            finally:
                updater.APP_DIR = original_app_dir
    wrapper.__name__ = fn.__name__
    return wrapper


@_run_with_temp_signing_paths
def test_ensure_signing_keypair_creates_key_files(_tmp: Path):
    priv_pem, pub_pem = updater.ensure_signing_keypair()
    assert b"PRIVATE KEY" in priv_pem
    assert b"PUBLIC KEY" in pub_pem
    assert updater.SIGNING_PRIVATE_KEY.exists()
    assert updater.SIGNING_PUBLIC_KEY.exists()
    assert (updater.SIGNING_PRIVATE_KEY.stat().st_mode & 0o777) == 0o600
    assert (updater.SIGNING_PUBLIC_KEY.stat().st_mode & 0o777) == 0o644


@_run_with_temp_signing_paths
def test_ensure_signing_keypair_reuses_existing_files(_tmp: Path):
    first = updater.ensure_signing_keypair()
    second = updater.ensure_signing_keypair()
    assert first[0] == second[0]
    assert first[1] == second[1]


@_run_with_temp_signing_paths
def test_public_key_b64_roundtrip_matches_pem(_tmp: Path):
    pem = updater.get_signing_public_key_pem().encode()
    b64 = updater.get_signing_public_key_b64()
    import base64
    assert base64.b64decode(b64) == pem


@_run_with_temp_signing_paths
def test_sign_and_verify_package_success(_tmp: Path):
    data = b"package-bytes"
    sig = updater.sign_package(data)
    pub = updater.get_signing_public_key_pem()
    assert updater.verify_package_signature(data, sig["signature_b64"], pub)
    assert len(sig["key_id"]) == 16
    assert isinstance(sig["signed_at"], int)


@_run_with_temp_signing_paths
def test_verify_package_signature_rejects_wrong_data(_tmp: Path):
    data = b"package-a"
    sig = updater.sign_package(data)
    pub = updater.get_signing_public_key_pem()
    assert not updater.verify_package_signature(b"package-b", sig["signature_b64"], pub)


@_run_with_temp_signing_paths
def test_verify_package_signature_rejects_bad_inputs(_tmp: Path):
    assert not updater.verify_package_signature(b"data", "not-base64", "not-a-key")
    assert not updater.verify_package_signature(b"data", "Zm9v", "bad-pem")


@_run_with_temp_app_dir
def test_apply_package_rejects_unsafe_tar_paths(tmp_app: Path):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        content = b"escape"
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    payload.seek(0)
    exc = _assert_raises(ValueError, updater.apply_package, payload.read())
    assert "Unsafe path" in str(exc)
    assert not (tmp_app.parent / "escape.txt").exists()


@_run_with_temp_app_dir
def test_apply_package_extracts_safe_paths(tmp_app: Path):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="module.py")
        content = b"print('ok')\n"
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    payload.seek(0)
    updater.apply_package(payload.read())
    assert (tmp_app / "module.py").read_text() == "print('ok')\n"


@_run_with_temp_app_dir
def test_build_package_includes_expected_and_excludes_sensitive(tmp_app: Path):
    (tmp_app / "main.py").write_text("print('main')\n")
    (tmp_app / "notes.txt").write_text("ignore me\n")
    (tmp_app / "install.sh").write_text("#!/bin/sh\n")
    (tmp_app / "uninstall").write_text("#!/bin/sh\n")
    (tmp_app / "requirements.txt").write_text("fastapi\n")
    (tmp_app / "requirements.lock").write_text("fastapi==x\n")
    (tmp_app / "static").mkdir()
    (tmp_app / "static" / "app.js").write_text("console.log('x')\n")
    (tmp_app / "tests").mkdir()
    (tmp_app / "tests" / "should_not_ship.py").write_text("x=1\n")
    (tmp_app / "docs").mkdir()
    (tmp_app / "docs" / "guide.md").write_text("# guide\n")
    (tmp_app / ".git").mkdir()
    (tmp_app / ".git" / "config").write_text("x\n")
    (tmp_app / "venv").mkdir()
    (tmp_app / "venv" / "bin.py").write_text("x\n")

    pkg = updater.build_package()
    names = []
    version_metadata = None
    with tarfile.open(fileobj=io.BytesIO(pkg), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
        version_file = tar.extractfile(updater.DEPLOY_VERSION_FILENAME)
        assert version_file is not None
        version_metadata = json.loads(version_file.read().decode())

    assert "main.py" in names
    assert "install.sh" in names
    assert "uninstall" in names
    assert "requirements.txt" in names
    assert "requirements.lock" in names
    assert "static/app.js" in names
    assert updater.DEPLOY_VERSION_FILENAME in names
    assert version_metadata["source"] == "deploy-package"
    assert isinstance(version_metadata["deployed_at"], int)
    assert "notes.txt" not in names
    assert "tests/should_not_ship.py" not in names
    assert "docs/guide.md" not in names
    assert ".git/config" not in names
    assert "venv/bin.py" not in names


@_run_with_temp_app_dir
def test_get_version_prefers_deploy_metadata(tmp_app: Path):
    payload = {
        "source": "deploy-package",
        "commit": "abc1234",
        "branch": "main",
        "dirty": True,
        "deployed_at": 123456789,
    }
    (tmp_app / updater.DEPLOY_VERSION_FILENAME).write_text(json.dumps(payload))

    result = updater.get_version()

    assert result == {
        "commit": "abc1234",
        "branch": "main",
        "dirty": True,
        "summary": "abc1234 (modified, deployed)",
        "deployed": True,
        "deployed_at": 123456789,
    }


def test_push_update_to_worker_rejects_invalid_address():
    original_assert = updater.assert_worker_address_allowed
    updater.assert_worker_address_allowed = lambda _addr: (_ for _ in ()).throw(ValueError("blocked"))
    try:
        result = asyncio.run(updater.push_update_to_worker("http://bad:9000", "k", b"pkg"))
    finally:
        updater.assert_worker_address_allowed = original_assert
    assert result["status"] == "error"
    assert "Invalid worker address" in result["detail"]


def _run_push_with_httpx_stub(response_or_exc, status_code=200, json_value=None, json_exc=None):
    original_assert = updater.assert_worker_address_allowed
    original_httpx = updater.httpx
    captured = {}

    class _Resp:
        def __init__(self, code, value, exc):
            self.status_code = code
            self._value = value
            self._exc = exc

        def json(self):
            if self._exc is not None:
                raise self._exc
            return self._value

    class _Client:
        def __init__(self, timeout=30):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, content=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["content"] = content
            if response_or_exc == "raise":
                raise RuntimeError("network boom")
            return _Resp(status_code, json_value, json_exc)

    updater.assert_worker_address_allowed = lambda addr: "http://worker:9000"
    updater.httpx = types.SimpleNamespace(HTTPError=RuntimeError, AsyncClient=_Client)
    try:
        result = asyncio.run(
            updater.push_update_to_worker(
                "ignored",
                "api-key",
                b"pkg-bytes",
                signature_b64="sig-b64",
                key_id="kid-1",
            )
        )
    finally:
        updater.assert_worker_address_allowed = original_assert
        updater.httpx = original_httpx
    return result, captured


def test_push_update_to_worker_handles_http_error():
    result, _ = _run_push_with_httpx_stub("raise")
    assert result["status"] == "error"
    assert "network boom" in result["detail"]


def test_push_update_to_worker_handles_non_json_response():
    result, _ = _run_push_with_httpx_stub(
        response_or_exc="resp",
        status_code=200,
        json_exc=ValueError("bad json"),
    )
    assert result["status"] == "error"
    assert "non-JSON" in result["detail"]


def test_push_update_to_worker_handles_non_dict_json_response():
    result, _ = _run_push_with_httpx_stub(
        response_or_exc="resp",
        status_code=200,
        json_value=["not", "dict"],
    )
    assert result["status"] == "error"
    assert "unexpected response type" in result["detail"]


def test_push_update_to_worker_handles_error_status_detail_string():
    result, _ = _run_push_with_httpx_stub(
        response_or_exc="resp",
        status_code=401,
        json_value={"detail": "bad signature"},
    )
    assert result == {"status": "error", "detail": "bad signature"}


def test_push_update_to_worker_handles_error_status_detail_non_string():
    result, _ = _run_push_with_httpx_stub(
        response_or_exc="resp",
        status_code=500,
        json_value={"detail": {"nested": "value"}},
    )
    assert result["status"] == "error"
    assert "nested" in result["detail"]


def test_push_update_to_worker_success_and_headers():
    result, captured = _run_push_with_httpx_stub(
        response_or_exc="resp",
        status_code=200,
        json_value={"status": "updated"},
    )
    assert result == {"status": "updated"}
    assert captured["url"] == "http://worker:9000/api/node/update"
    assert captured["headers"]["X-Api-Key"] == "api-key"
    assert captured["headers"]["X-Inframatik-Package-Signature"] == "sig-b64"
    assert captured["headers"]["X-Inframatik-Package-Key-Id"] == "kid-1"
    assert captured["content"] == b"pkg-bytes"


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
    print("Running updater security tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
