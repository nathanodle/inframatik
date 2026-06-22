"""Tests for cluster route update/install/deploy behaviors."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException

import cluster_routes


class _DummyRequest:
    def __init__(self, headers=None, body=b""):
        self.headers = headers or {}
        self._body = body

    async def body(self):
        return self._body


class _BigBody:
    def __len__(self):
        return 50 * 1024 * 1024 + 1

    def __bool__(self):
        return True


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        asyncio.run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def _with_env(name: str, value: str | None):
    def decorator(fn):
        def wrapper():
            old = os.environ.get(name)
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
            try:
                fn()
            finally:
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


def test_node_update_requires_api_key_header():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        req = _DummyRequest(headers={}, body=b"pkg")
        exc = _assert_raises_async(HTTPException, cluster_routes.node_update, req)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 401


def test_node_update_requires_matching_api_key():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        req = _DummyRequest(headers={"X-Api-Key": "wrong"}, body=b"pkg")
        exc = _assert_raises_async(HTTPException, cluster_routes.node_update, req)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 401


def test_node_update_rejects_empty_body():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        req = _DummyRequest(headers={"X-Api-Key": "k1"}, body=b"")
        exc = _assert_raises_async(HTTPException, cluster_routes.node_update, req)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 400
    assert "Empty package" in str(exc.detail)


def test_node_update_rejects_oversized_body():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"api_key": "k1"}
    try:
        req = _DummyRequest(headers={"X-Api-Key": "k1"}, body=_BigBody())
        exc = _assert_raises_async(HTTPException, cluster_routes.node_update, req)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 413


@_with_env("INFRAMATIK_ALLOW_UNSIGNED_UPDATES", None)
def test_node_update_requires_signature_by_default():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"api_key": "k1", "role": "worker"}
    try:
        req = _DummyRequest(headers={"X-Api-Key": "k1"}, body=b"pkg")
        exc = _assert_raises_async(HTTPException, cluster_routes.node_update, req)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 401
    assert "Missing package signature" in str(exc.detail)


@_with_env("INFRAMATIK_ALLOW_UNSIGNED_UPDATES", "1")
def test_node_update_allows_unsigned_when_env_enabled():
    original_get_cfg = cluster_routes.get_node_config
    original_apply = cluster_routes.apply_package
    original_loop = cluster_routes.asyncio.get_event_loop
    original_restart = cluster_routes.restart_service
    calls = []

    class _Loop:
        def call_later(self, delay, fn):
            calls.append((delay, fn))

    cluster_routes.get_node_config = lambda: {"api_key": "k1", "role": "worker"}
    cluster_routes.apply_package = lambda data: calls.append(("apply", data))
    cluster_routes.restart_service = lambda: None
    cluster_routes.asyncio.get_event_loop = lambda: _Loop()
    try:
        req = _DummyRequest(headers={"X-Api-Key": "k1"}, body=b"pkg")
        result = asyncio.run(cluster_routes.node_update(req))
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.apply_package = original_apply
        cluster_routes.asyncio.get_event_loop = original_loop
        cluster_routes.restart_service = original_restart

    assert result["status"] == "updated"
    assert ("apply", b"pkg") in calls
    assert calls[-1][0] == 1


def test_node_update_requires_trusted_key_when_signature_present():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"api_key": "k1", "role": "worker"}
    try:
        req = _DummyRequest(
            headers={
                "X-Api-Key": "k1",
                "X-Inframatik-Package-Signature": "sig",
            },
            body=b"pkg",
        )
        exc = _assert_raises_async(HTTPException, cluster_routes.node_update, req)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 400
    assert "No trusted update signing key configured" in str(exc.detail)


def test_node_update_uses_master_public_key_fallback_for_verification():
    original_get_cfg = cluster_routes.get_node_config
    original_get_pub = cluster_routes.get_signing_public_key_pem
    original_verify = cluster_routes.verify_package_signature
    original_apply = cluster_routes.apply_package
    original_loop = cluster_routes.asyncio.get_event_loop
    original_restart = cluster_routes.restart_service
    seen = {}

    class _Loop:
        def call_later(self, delay, fn):
            seen["delay"] = delay
            seen["fn"] = fn

    def fake_verify(data, sig, pub):
        seen["verify"] = (data, sig, pub)
        return True

    cluster_routes.get_node_config = lambda: {"api_key": "k1", "role": "master"}
    cluster_routes.get_signing_public_key_pem = lambda: "MASTER-PUB"
    cluster_routes.verify_package_signature = fake_verify
    cluster_routes.apply_package = lambda data: seen.setdefault("applied", data)
    cluster_routes.restart_service = lambda: None
    cluster_routes.asyncio.get_event_loop = lambda: _Loop()
    try:
        req = _DummyRequest(
            headers={
                "X-Api-Key": "k1",
                "X-Inframatik-Package-Signature": "sig",
            },
            body=b"pkg",
        )
        result = asyncio.run(cluster_routes.node_update(req))
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.get_signing_public_key_pem = original_get_pub
        cluster_routes.verify_package_signature = original_verify
        cluster_routes.apply_package = original_apply
        cluster_routes.asyncio.get_event_loop = original_loop
        cluster_routes.restart_service = original_restart

    assert result["status"] == "updated"
    assert seen["verify"] == (b"pkg", "sig", "MASTER-PUB")


def test_node_update_rejects_invalid_signature():
    original_get_cfg = cluster_routes.get_node_config
    original_verify = cluster_routes.verify_package_signature
    cluster_routes.get_node_config = lambda: {
        "api_key": "k1",
        "role": "worker",
        "update_public_key": "PUB",
    }
    cluster_routes.verify_package_signature = lambda data, sig, pub: False
    try:
        req = _DummyRequest(
            headers={
                "X-Api-Key": "k1",
                "X-Inframatik-Package-Signature": "sig",
            },
            body=b"pkg",
        )
        exc = _assert_raises_async(HTTPException, cluster_routes.node_update, req)
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.verify_package_signature = original_verify
    assert exc.status_code == 401
    assert "Invalid package signature" in str(exc.detail)


def test_node_update_maps_apply_package_errors():
    original_get_cfg = cluster_routes.get_node_config
    original_verify = cluster_routes.verify_package_signature
    original_apply = cluster_routes.apply_package
    cluster_routes.get_node_config = lambda: {
        "api_key": "k1",
        "role": "worker",
        "update_public_key": "PUB",
    }
    cluster_routes.verify_package_signature = lambda data, sig, pub: True
    cluster_routes.apply_package = lambda data: (_ for _ in ()).throw(ValueError("bad package"))
    try:
        req = _DummyRequest(
            headers={
                "X-Api-Key": "k1",
                "X-Inframatik-Package-Signature": "sig",
            },
            body=b"pkg",
        )
        exc = _assert_raises_async(HTTPException, cluster_routes.node_update, req)
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.verify_package_signature = original_verify
        cluster_routes.apply_package = original_apply
    assert exc.status_code == 400
    assert "Failed to apply package" in str(exc.detail)


def test_deploy_to_workers_requires_master():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"role": "worker"}
    try:
        exc = _assert_raises_async(HTTPException, cluster_routes.deploy_to_workers)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 403


def test_deploy_to_workers_handles_empty_workers():
    original_get_cfg = cluster_routes.get_node_config
    original_build = cluster_routes.build_package
    original_sign = cluster_routes.sign_package
    cluster_routes.get_node_config = lambda: {"role": "master", "workers": {}}
    cluster_routes.build_package = lambda: b"pkg"
    cluster_routes.sign_package = lambda data: {"signature_b64": "sig", "key_id": "kid"}
    try:
        result = asyncio.run(cluster_routes.deploy_to_workers())
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.build_package = original_build
        cluster_routes.sign_package = original_sign
    assert result == {"status": "deployed", "workers": {}}


def test_deploy_to_workers_aggregates_worker_outcomes():
    original_get_cfg = cluster_routes.get_node_config
    original_build = cluster_routes.build_package
    original_sign = cluster_routes.sign_package
    original_push = cluster_routes.push_update_to_worker
    cluster_routes.get_node_config = lambda: {
        "role": "master",
        "workers": {
            "w1": {"name": "one", "address": "http://w1:9000", "api_key": "k1"},
            "w2": {"name": "two", "address": "http://w2:9000", "api_key": "k2"},
            "w3": {"name": "three", "address": "http://w3:9000", "api_key": "k3"},
        },
    }
    cluster_routes.build_package = lambda: b"pkg"
    cluster_routes.sign_package = lambda data: {"signature_b64": "sig", "key_id": "kid"}

    async def fake_push(address, api_key, package, signature_b64, key_id):
        if address.endswith("w1:9000"):
            return {"status": "updated"}
        if address.endswith("w2:9000"):
            raise RuntimeError("push failed")
        return ["unexpected"]

    cluster_routes.push_update_to_worker = fake_push
    try:
        result = asyncio.run(cluster_routes.deploy_to_workers())
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.build_package = original_build
        cluster_routes.sign_package = original_sign
        cluster_routes.push_update_to_worker = original_push

    workers = result["workers"]
    assert workers["w1"]["status"] == "updated"
    assert workers["w1"]["name"] == "one"
    assert workers["w2"]["status"] == "error"
    assert "push failed" in workers["w2"]["detail"]
    assert workers["w3"]["status"] == "error"
    assert "Invalid worker response type" in workers["w3"]["detail"]


def test_deploy_self_schedules_restart():
    original_loop = cluster_routes.asyncio.get_event_loop
    original_restart = cluster_routes.restart_service
    seen = {}

    class _Loop:
        def call_later(self, delay, fn):
            seen["delay"] = delay
            seen["fn"] = fn

    cluster_routes.restart_service = lambda: None
    cluster_routes.asyncio.get_event_loop = lambda: _Loop()
    try:
        result = asyncio.run(cluster_routes.deploy_self())
    finally:
        cluster_routes.asyncio.get_event_loop = original_loop
        cluster_routes.restart_service = original_restart

    assert result == {"status": "restarting"}
    assert seen["delay"] == 1


def test_update_master_from_git_requires_master():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"role": "worker"}
    try:
        exc = _assert_raises_async(HTTPException, cluster_routes.update_master_from_git)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 403


def test_update_master_from_git_schedules_restart():
    original_get_cfg = cluster_routes.get_node_config
    original_update = cluster_routes.update_from_git
    original_loop = cluster_routes.asyncio.get_event_loop
    original_restart = cluster_routes.restart_service
    seen = {}

    class _Loop:
        def call_later(self, delay, fn):
            seen["delay"] = delay
            seen["fn"] = fn

    cluster_routes.get_node_config = lambda: {"role": "master"}
    cluster_routes.update_from_git = lambda: {
        "status": "updated",
        "detail": "Already up to date.",
        "before": {"commit": "a"},
        "after": {"commit": "b"},
    }
    cluster_routes.restart_service = lambda: None
    cluster_routes.asyncio.get_event_loop = lambda: _Loop()
    try:
        result = asyncio.run(cluster_routes.update_master_from_git())
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.update_from_git = original_update
        cluster_routes.asyncio.get_event_loop = original_loop
        cluster_routes.restart_service = original_restart

    assert result["status"] == "updated"
    assert result["restart"] == "scheduled"
    assert seen["delay"] == 1


def test_proxy_cf_service_update_normalizes_non_dict_body():
    original_proxy = cluster_routes.proxy_to_node
    seen = {}

    async def fake_proxy(node_id, method, path, body=None):
        seen["args"] = (node_id, method, path, body)
        return {"ok": True}

    cluster_routes.proxy_to_node = fake_proxy
    try:
        result = asyncio.run(cluster_routes.proxy_cf_service_update("node-1", body=["bad"]))
    finally:
        cluster_routes.proxy_to_node = original_proxy

    assert result == {"ok": True}
    assert seen["args"] == ("node-1", "POST", "/api/internal/cf/service/update", {})


def test_proxy_cf_service_update_maps_errors():
    original_proxy = cluster_routes.proxy_to_node

    async def fake_proxy_value(*args, **kwargs):
        raise ValueError("bad payload")

    cluster_routes.proxy_to_node = fake_proxy_value
    try:
        exc = _assert_raises_async(HTTPException, cluster_routes.proxy_cf_service_update, "node-1", {})
    finally:
        cluster_routes.proxy_to_node = original_proxy
    assert exc.status_code == 400

    async def fake_proxy_runtime(*args, **kwargs):
        raise RuntimeError("unreachable")

    cluster_routes.proxy_to_node = fake_proxy_runtime
    try:
        exc = _assert_raises_async(HTTPException, cluster_routes.proxy_cf_service_update, "node-1", {})
    finally:
        cluster_routes.proxy_to_node = original_proxy
    assert exc.status_code == 502


def test_proxy_inference_overview_forwards_include_system_flag():
    original_proxy = cluster_routes.proxy_to_node
    seen = []

    async def fake_proxy(node_id, method, path, body=None):
        seen.append((node_id, method, path, body))
        return {"ok": True, "path": path}

    cluster_routes.proxy_to_node = fake_proxy
    try:
        default_result = asyncio.run(cluster_routes.proxy_inference_overview("node-1"))
        light_result = asyncio.run(cluster_routes.proxy_inference_overview("node-1", include_system=False))
    finally:
        cluster_routes.proxy_to_node = original_proxy

    assert default_result["path"] == "/api/inference/overview"
    assert light_result["path"] == "/api/inference/overview?include_system=false"
    assert seen == [
        ("node-1", "GET", "/api/inference/overview", None),
        ("node-1", "GET", "/api/inference/overview?include_system=false", None),
    ]


def test_install_script_requires_master_role():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"role": "worker"}
    try:
        exc = _assert_raises_async(
            HTTPException,
            cluster_routes.install_script,
            _DummyRequest(headers={"host": "example.com"}),
        )
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 403


def test_install_script_rejects_invalid_host_header():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"role": "master"}
    try:
        exc = _assert_raises_async(
            HTTPException,
            cluster_routes.install_script,
            _DummyRequest(headers={"host": "bad host"}),
        )
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 400


def test_install_script_embeds_master_url_and_public_key():
    original_get_cfg = cluster_routes.get_node_config
    original_pub = cluster_routes.get_signing_public_key_b64
    original_path = cluster_routes._INSTALL_SCRIPT_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = Path(tmpdir) / "install.sh.tpl"
        script_path.write_text("url=__MASTER_URL__\nkey=__PACKAGE_PUBLIC_KEY_B64__\n")
        cluster_routes._INSTALL_SCRIPT_PATH = script_path
        cluster_routes.get_node_config = lambda: {"role": "master"}
        cluster_routes.get_signing_public_key_b64 = lambda: "PUBKEYB64"
        try:
            resp = asyncio.run(
                cluster_routes.install_script(
                    _DummyRequest(headers={"host": "example.com:9000", "x-forwarded-proto": "https"})
                )
            )
        finally:
            cluster_routes._INSTALL_SCRIPT_PATH = original_path
            cluster_routes.get_node_config = original_get_cfg
            cluster_routes.get_signing_public_key_b64 = original_pub

    body = resp.body.decode()
    assert "url=https://example.com:9000" in body
    assert "key=PUBKEYB64" in body


def test_install_script_no_longer_exposes_install_cf_flag():
    original_get_cfg = cluster_routes.get_node_config
    original_pub = cluster_routes.get_signing_public_key_b64
    cluster_routes.get_node_config = lambda: {"role": "master"}
    cluster_routes.get_signing_public_key_b64 = lambda: "PUBKEYB64"
    try:
        resp = asyncio.run(
            cluster_routes.install_script(
                _DummyRequest(headers={"host": "example.com:9000"})
            )
        )
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.get_signing_public_key_b64 = original_pub

    body = resp.body.decode()
    assert "INSTALL_CF" not in body
    assert "--local-only" in body
    assert "INFRAMATIK_SKIP_CF" in body
    assert "INFRAMATIK_INSTALL_SOURCE_MASTER_URL" in body
    assert 'MASTER_URL="http://example.com:9000"' in body
    assert 'http://*|https://*' in body
    assert '!= "http://example.com:9000"' not in body
    assert "installer_rich.py" in body


def test_install_package_requires_master_role():
    original_get_cfg = cluster_routes.get_node_config
    cluster_routes.get_node_config = lambda: {"role": "worker"}
    try:
        exc = _assert_raises_async(HTTPException, cluster_routes.install_package)
    finally:
        cluster_routes.get_node_config = original_get_cfg
    assert exc.status_code == 403


def test_install_package_returns_signed_payload_headers():
    original_get_cfg = cluster_routes.get_node_config
    original_build = cluster_routes.build_package
    original_sign = cluster_routes.sign_package
    cluster_routes.get_node_config = lambda: {"role": "master"}
    cluster_routes.build_package = lambda: b"PKGDATA"
    cluster_routes.sign_package = lambda data: {
        "signature_b64": "SIG",
        "key_id": "KID",
        "signed_at": 1234,
    }
    try:
        resp = asyncio.run(cluster_routes.install_package())
    finally:
        cluster_routes.get_node_config = original_get_cfg
        cluster_routes.build_package = original_build
        cluster_routes.sign_package = original_sign

    assert resp.body == b"PKGDATA"
    assert resp.headers["X-Inframatik-Package-Signature"] == "SIG"
    assert resp.headers["X-Inframatik-Package-Key-Id"] == "KID"
    assert resp.headers["X-Inframatik-Package-Signed-At"] == "1234"


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
    print("Running cluster route update/install tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
