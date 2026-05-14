"""Tests for bootstrap config handling in cluster routes."""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import node_config
try:
    import cluster_routes
except ModuleNotFoundError:
    cluster_routes = None


def _run_with_temp_config(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir:
            original_file = node_config.CONFIG_FILE
            node_config.CONFIG_FILE = Path(tmpdir) / "node.json"
            node_config.invalidate_cache()
            try:
                fn()
            finally:
                node_config.CONFIG_FILE = original_file
                node_config.invalidate_cache()
    wrapper.__name__ = fn.__name__
    return wrapper


class _DummyRequest:
    def __init__(self, url=None):
        self.url = url or types.SimpleNamespace(hostname="worker.local", port=9000)


class _Resp:
    def __init__(self, status_code=200, payload=None, json_exc=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._json_exc = json_exc

    def json(self):
        if self._json_exc:
            raise self._json_exc
        return self._payload


class _AsyncClient:
    def __init__(self, on_post, *args, **kwargs):
        self.on_post = on_post
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def post(self, url, json=None):
        return self.on_post(url, json=json)


@_run_with_temp_config
def test_node_info_handles_password_only_bootstrap_config():
    if cluster_routes is None:
        return
    node_config.save_node_config({"admin_password_hash": "hash"})
    info = asyncio.run(cluster_routes.node_info())
    assert info["role"] == "unconfigured"
    assert info["node_name"] is None
    assert info["node_id"] is None
    assert "machine_hostname" in info


@_run_with_temp_config
def test_config_get_handles_password_only_bootstrap_config():
    if cluster_routes is None:
        return
    node_config.save_node_config({"admin_password_hash": "hash"})
    cfg = asyncio.run(cluster_routes.config_get())
    assert cfg == {"role": "unconfigured"}


@_run_with_temp_config
def test_init_standalone_from_bootstrap_preserves_password_hash():
    if cluster_routes is None:
        return
    node_config.save_node_config({"admin_password_hash": "hash"})
    result = asyncio.run(cluster_routes.config_init_standalone(cluster_routes.InitBody(name="node-a")))
    assert result["role"] == "standalone"
    cfg = node_config.get_node_config()
    assert cfg is not None
    assert cfg["role"] == "standalone"
    assert cfg.get("admin_password_hash") == "hash"


@_run_with_temp_config
def test_enroll_worker_calls_master_server_side_and_saves_config():
    if cluster_routes is None:
        return
    seen = {}

    def on_post(url, json=None):
        seen["url"] = url
        seen["json"] = json
        return _Resp(200, {"api_key": "worker-key", "signing_public_key": "pub"})

    original_client = cluster_routes.httpx.AsyncClient
    original_address = cluster_routes._worker_address_for_master
    cluster_routes.httpx.AsyncClient = lambda *a, **kw: _AsyncClient(on_post, *a, **kw)
    cluster_routes._worker_address_for_master = lambda master_url, request: "http://10.0.0.5:9000"
    try:
        result = asyncio.run(
            cluster_routes.config_enroll_worker(
                cluster_routes.EnrollWorkerBody(
                    name="worker-a",
                    master_url="http://192.168.166.186:9000/",
                    token="enroll-token",
                ),
                _DummyRequest(),
            )
        )
    finally:
        cluster_routes.httpx.AsyncClient = original_client
        cluster_routes._worker_address_for_master = original_address

    cfg = node_config.get_node_config()
    assert seen == {
        "url": "http://192.168.166.186:9000/api/nodes/enroll",
        "json": {
            "token": "enroll-token",
            "node_name": "worker-a",
            "address": "http://10.0.0.5:9000",
        },
    }
    assert result["role"] == "worker"
    assert result["master_url"] == "http://192.168.166.186:9000"
    assert result["address"] == "http://10.0.0.5:9000"
    assert cfg["role"] == "worker"
    assert cfg["api_key"] == "worker-key"
    assert cfg["update_public_key"] == "pub"


@_run_with_temp_config
def test_enroll_worker_propagates_master_enrollment_error():
    if cluster_routes is None:
        return

    def on_post(_url, json=None):
        return _Resp(401, {"detail": "Invalid or expired enrollment token"})

    original_client = cluster_routes.httpx.AsyncClient
    original_address = cluster_routes._worker_address_for_master
    cluster_routes.httpx.AsyncClient = lambda *a, **kw: _AsyncClient(on_post, *a, **kw)
    cluster_routes._worker_address_for_master = lambda master_url, request: "http://10.0.0.5:9000"
    try:
        try:
            asyncio.run(
                cluster_routes.config_enroll_worker(
                    cluster_routes.EnrollWorkerBody(
                        name="worker-a",
                        master_url="http://192.168.166.186:9000",
                        token="bad-token",
                    ),
                    _DummyRequest(),
                )
            )
        except cluster_routes.HTTPException as exc:
            assert exc.status_code == 401
            assert exc.detail == "Invalid or expired enrollment token"
        else:
            raise AssertionError("Expected HTTPException")
    finally:
        cluster_routes.httpx.AsyncClient = original_client
        cluster_routes._worker_address_for_master = original_address

    assert node_config.get_node_config() is None


@_run_with_temp_config
def test_enroll_worker_rejects_master_url_with_path():
    if cluster_routes is None:
        return
    try:
        asyncio.run(
            cluster_routes.config_enroll_worker(
                cluster_routes.EnrollWorkerBody(
                    name="worker-a",
                    master_url="http://192.168.166.186:9000/path",
                    token="enroll-token",
                ),
                _DummyRequest(),
            )
        )
    except cluster_routes.HTTPException as exc:
        assert exc.status_code == 400
        assert "base URL" in str(exc.detail)
    else:
        raise AssertionError("Expected HTTPException")


def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    skipped = 0
    for test in tests:
        try:
            if cluster_routes is None:
                print(f"  - {test.__name__}: skipped (fastapi not installed)")
                skipped += 1
                continue
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return failed == 0


if __name__ == "__main__":
    print("Running cluster bootstrap tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
