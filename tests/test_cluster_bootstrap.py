"""Tests for bootstrap config handling in cluster routes."""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import node_config
import tunnel
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

    async def post(self, url, json=None, headers=None):
        try:
            return self.on_post(url, json=json, headers=headers)
        except TypeError:
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
    original_progress = cluster_routes._send_worker_enroll_progress

    async def fake_progress(step, message, done=False, error=False):
        seen.setdefault("progress", []).append((step, done, error))

    cluster_routes.httpx.AsyncClient = lambda *a, **kw: _AsyncClient(on_post, *a, **kw)
    cluster_routes._worker_address_for_master = lambda master_url, request: "http://10.0.0.5:9000"
    cluster_routes._send_worker_enroll_progress = fake_progress
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
        cluster_routes._send_worker_enroll_progress = original_progress

    cfg = node_config.get_node_config()
    assert seen["url"] == "http://192.168.166.186:9000/api/nodes/enroll"
    assert seen["json"] == {
        "token": "enroll-token",
        "node_name": "worker-a",
        "address": "http://10.0.0.5:9000",
    }
    assert result["role"] == "worker"
    assert result["master_url"] == "http://192.168.166.186:9000"
    assert result["address"] == "http://10.0.0.5:9000"
    assert cfg["role"] == "worker"
    assert cfg["api_key"] == "worker-key"
    assert cfg["update_public_key"] == "pub"
    assert "cf_tunnel" not in result
    assert [entry[0] for entry in seen["progress"]] == [
        "contacting_master",
        "saving_worker_config",
        "skipping_cloudflare",
        "complete",
    ]


@_run_with_temp_config
def test_enroll_worker_copies_cf_config_and_sets_up_local_tunnel():
    if cluster_routes is None:
        return
    seen = {}

    def on_post(url, json=None, headers=None):
        if url.endswith("/api/nodes/enroll"):
            return _Resp(
                200,
                {
                    "api_key": "worker-key",
                    "signing_public_key": "pub",
                    "cf_config": {
                        "token": "cf-token",
                        "account_id": "acct-1",
                        "zone_id": "zone-1",
                        "default_policy_id": "pol-1",
                        "team_domain": "team-a",
                        "access_issuer": "https://team-a.cloudflareaccess.com",
                    },
                },
            )
        if url.endswith("/api/nodes/tunnel"):
            seen["report"] = {"headers": headers, "json": json}
            return _Resp(200, {"status": "updated"})
        raise AssertionError(f"Unexpected POST {url}")

    async def fake_create_tunnel(name):
        seen["tunnel_name"] = name
        return {"id": "tid-1", "name": name}

    async def fake_init_tunnel(tunnel_id):
        seen["init_tunnel"] = tunnel_id
        return True

    async def fake_get_token(tunnel_id):
        seen["token_tunnel"] = tunnel_id
        return "connector-token"

    async def fake_setup_cloudflared(token):
        seen["cloudflared_token"] = token

    async def fake_progress(step, message, done=False, error=False):
        seen.setdefault("progress", []).append((step, message, done, error))

    originals = (
        cluster_routes.httpx.AsyncClient,
        cluster_routes._worker_address_for_master,
        cluster_routes.setup_cloudflared_user_service,
        cluster_routes._send_worker_enroll_progress,
        tunnel.create_tunnel,
        tunnel.init_tunnel_config,
        tunnel.get_tunnel_token,
    )
    cluster_routes.httpx.AsyncClient = lambda *a, **kw: _AsyncClient(on_post, *a, **kw)
    cluster_routes._worker_address_for_master = lambda master_url, request: "http://10.0.0.5:9000"
    cluster_routes.setup_cloudflared_user_service = fake_setup_cloudflared
    cluster_routes._send_worker_enroll_progress = fake_progress
    tunnel.create_tunnel = fake_create_tunnel
    tunnel.init_tunnel_config = fake_init_tunnel
    tunnel.get_tunnel_token = fake_get_token
    try:
        result = asyncio.run(
            cluster_routes.config_enroll_worker(
                cluster_routes.EnrollWorkerBody(
                    name="worker-a",
                    master_url="http://192.168.166.186:9000",
                    token="enroll-token",
                ),
                _DummyRequest(),
            )
        )
    finally:
        (
            cluster_routes.httpx.AsyncClient,
            cluster_routes._worker_address_for_master,
            cluster_routes.setup_cloudflared_user_service,
            cluster_routes._send_worker_enroll_progress,
            tunnel.create_tunnel,
            tunnel.init_tunnel_config,
            tunnel.get_tunnel_token,
        ) = originals

    cfg = node_config.get_node_config()
    assert result["cf_tunnel"]["tunnel_id"] == "tid-1"
    assert cfg["cf_token"] == "cf-token"
    assert cfg["cf_account_id"] == "acct-1"
    assert cfg["cf_zone_id"] == "zone-1"
    assert cfg["cf_default_policy_id"] == "pol-1"
    assert cfg["cf_team_domain"] == "team-a"
    assert cfg["cf_access_issuer"] == "https://team-a.cloudflareaccess.com"
    assert cfg["tunnel_id"] == "tid-1"
    assert seen["tunnel_name"] == "worker-a"
    assert seen["init_tunnel"] == "tid-1"
    assert seen["token_tunnel"] == "tid-1"
    assert seen["cloudflared_token"] == "connector-token"
    assert seen["report"]["headers"]["X-Api-Key"] == "worker-key"
    assert seen["report"]["json"] == {"tunnel_id": "tid-1"}
    assert [entry[0] for entry in seen["progress"]] == [
        "contacting_master",
        "saving_worker_config",
        "saving_cloudflare_config",
        "creating_tunnel",
        "initializing_tunnel",
        "getting_token",
        "installing_cloudflared",
        "cloudflared_ready",
        "reporting_master",
        "complete",
    ]
    assert seen["progress"][-1] == ("complete", "Worker registration complete", True, False)


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


@_run_with_temp_config
def test_master_enrollment_response_includes_cf_config_when_configured():
    if cluster_routes is None:
        return
    node_config.init_as_master("master-a")
    node_config.save_cf_config(
        "cf-token",
        "acct-1",
        "zone-1",
        "pol-1",
        team_domain="team-a",
        access_issuer="https://team-a.cloudflareaccess.com",
    )
    token = node_config.create_enrollment_token()

    result = asyncio.run(
        cluster_routes.enroll_worker(
            cluster_routes.EnrollBody(
                token=token,
                node_name="worker-a",
                address="http://10.0.0.5:9000",
            )
        )
    )

    assert result["status"] == "enrolled"
    assert result["cf_config"] == {
        "token": "cf-token",
        "account_id": "acct-1",
        "zone_id": "zone-1",
        "default_policy_id": "pol-1",
        "team_domain": "team-a",
        "access_issuer": "https://team-a.cloudflareaccess.com",
    }


@_run_with_temp_config
def test_master_enrollment_response_omits_cf_config_when_local_only():
    if cluster_routes is None:
        return
    node_config.init_as_master("master-a")
    token = node_config.create_enrollment_token()

    result = asyncio.run(
        cluster_routes.enroll_worker(
            cluster_routes.EnrollBody(
                token=token,
                node_name="worker-a",
                address="http://10.0.0.5:9000",
            )
        )
    )

    assert result["status"] == "enrolled"
    assert "cf_config" not in result


@_run_with_temp_config
def test_worker_tunnel_report_updates_master_worker_record():
    if cluster_routes is None:
        return
    node_config.init_as_master("master-a")
    worker_id = node_config.add_worker("worker-a", "http://10.0.0.5:9000", "worker-key")

    result = asyncio.run(
        cluster_routes.report_worker_tunnel(
            cluster_routes.WorkerTunnelBody(tunnel_id="tid-1"),
            types.SimpleNamespace(headers={"X-Api-Key": "worker-key"}),
        )
    )

    cfg = node_config.get_node_config()
    assert result == {"status": "updated", "tunnel_id": "tid-1"}
    assert cfg["workers"][worker_id]["tunnel_id"] == "tid-1"


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
