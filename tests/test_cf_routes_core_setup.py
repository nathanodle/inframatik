"""Tests for core Cloudflare routes and setup wizard flows."""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException

import cf_routes
import proxy
import tunnel


class _Patch:
    def __init__(self, patches):
        self._patches = patches
        self._originals = []

    def __enter__(self):
        for obj, attr, value in self._patches:
            self._originals.append((obj, attr, getattr(obj, attr)))
            setattr(obj, attr, value)
        return self

    def __exit__(self, exc_type, exc, tb):
        for obj, attr, old in reversed(self._originals):
            setattr(obj, attr, old)
        return False


class _Resp:
    def __init__(self, payload=None, status_code=200, text="ok"):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


def _async_client_factory(*, on_get=None, on_post=None):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            if on_get is None:
                raise AssertionError("Unexpected GET")
            return on_get(url, headers=headers, params=params)

        async def post(self, url, headers=None, json=None):
            if on_post is None:
                raise AssertionError("Unexpected POST")
            return on_post(url, headers=headers, json=json)

    return _Client


def _run(coro):
    return asyncio.run(coro)


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        _run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def _cf_ok():
    return {"token": "tok", "account_id": "acct", "zone_id": "zone"}


def test_require_cf_config_rejects_missing():
    with _Patch([(cf_routes, "_load_cf_config", lambda: None)]):
        exc = _assert_raises(HTTPException, cf_routes._require_cf_config)
    assert exc.status_code == 400


def test_normalize_access_policy_member_accepts_literal_subdomain():
    kind, value, rule = tunnel._normalize_access_policy_member("team.example.com")
    assert kind == "email_domain"
    assert value == "team.example.com"
    assert rule == {"email_domain": {"domain": "team.example.com"}}


def test_normalize_access_policy_member_rejects_wildcard_domain():
    exc = _assert_raises(ValueError, tunnel._normalize_access_policy_member, "*.example.com")
    assert "literal email domain" in str(exc)


def test_api_list_tunnels_success():
    async def fake_list():
        return [{"id": "t1"}]

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "list_tunnels", fake_list),
        ]
    ):
        result = _run(cf_routes.api_list_tunnels())
    assert result == [{"id": "t1"}]


def test_api_list_tunnels_maps_value_error():
    async def fake_list():
        raise ValueError("upstream failed")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "list_tunnels", fake_list),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_list_tunnels)
    assert exc.status_code == 502


def test_api_list_zones_success():
    async def fake_list():
        return [{"id": "z1", "name": "example.com"}]

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "list_available_zones", fake_list),
        ]
    ):
        result = _run(cf_routes.api_list_zones())
    assert result == {"zones": [{"id": "z1", "name": "example.com"}]}


def test_api_list_zones_maps_value_error():
    async def fake_list():
        raise ValueError("zones failed")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "list_available_zones", fake_list),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_list_zones)
    assert exc.status_code == 502


def test_api_create_tunnel_success_calls_init():
    seen = {}

    async def fake_create(name):
        seen["name"] = name
        return {"id": "tid-1", "name": name}

    async def fake_init(tid):
        seen["tid"] = tid

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "create_tunnel", fake_create),
            (cf_routes, "init_tunnel_config", fake_init),
        ]
    ):
        result = _run(cf_routes.api_create_tunnel(cf_routes.CreateTunnelBody(name="worker-1")))

    assert result["id"] == "tid-1"
    assert seen == {"name": "worker-1", "tid": "tid-1"}


def test_api_create_tunnel_maps_value_error():
    async def fake_create(_name):
        raise ValueError("bad name")

    async def fake_init(_tid):
        raise AssertionError("should not be called")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "create_tunnel", fake_create),
            (cf_routes, "init_tunnel_config", fake_init),
        ]
    ):
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.api_create_tunnel,
            cf_routes.CreateTunnelBody(name="x"),
        )
    assert exc.status_code == 400


def test_api_get_tunnel_token_success():
    async def fake_get_token(tid):
        return f"token-for-{tid}"

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "get_tunnel_token", fake_get_token),
        ]
    ):
        result = _run(cf_routes.api_get_tunnel_token("tid-9"))

    assert result == {"tunnel_id": "tid-9", "token": "token-for-tid-9"}


def test_api_get_tunnel_token_maps_value_error():
    async def fake_get_token(_tid):
        raise ValueError("missing tunnel")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "get_tunnel_token", fake_get_token),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_get_tunnel_token, "bad")
    assert exc.status_code == 400


def test_api_list_routes_maps_value_error():
    async def fake_routes(tunnel_id=None):
        raise ValueError(f"bad {tunnel_id}")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "get_tunnel_routes", fake_routes),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_list_routes, "tid-1")
    assert exc.status_code == 502


def test_api_add_route_success():
    seen = {}

    async def fake_add(hostname, service, tunnel_id=None):
        seen["args"] = (hostname, service, tunnel_id)

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "add_tunnel_route", fake_add),
        ]
    ):
        body = cf_routes.AddRouteBody(hostname="app.example.com", service="http://127.0.0.1:8000", tunnel_id="t2")
        result = _run(cf_routes.api_add_route(body))

    assert result == {"status": "added", "hostname": "app.example.com"}
    assert seen["args"] == ("app.example.com", "http://127.0.0.1:8000", "t2")


def test_api_remove_route_success():
    seen = {}

    async def fake_remove(hostname, tunnel_id=None):
        seen["args"] = (hostname, tunnel_id)

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "remove_tunnel_route", fake_remove),
        ]
    ):
        result = _run(cf_routes.api_remove_route("app.example.com", tunnel_id="t2"))

    assert result == {"status": "removed", "hostname": "app.example.com"}
    assert seen["args"] == ("app.example.com", "t2")


def test_api_list_dns_maps_value_error():
    async def fake_list():
        raise ValueError("dns fail")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "list_dns_records", fake_list),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_list_dns)
    assert exc.status_code == 502


def test_api_create_dns_success():
    async def fake_create(hostname, tunnel_id=None):
        assert hostname == "a.example.com"
        assert tunnel_id == "t9"
        return "dns-id-1"

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "create_dns_record", fake_create),
        ]
    ):
        body = cf_routes.CreateDnsBody(hostname="a.example.com", tunnel_id="t9")
        result = _run(cf_routes.api_create_dns(body))
    assert result == {"status": "created", "id": "dns-id-1"}


def test_api_create_dns_maps_value_error():
    async def fake_create(_hostname, tunnel_id=None):
        raise ValueError(f"bad dns {tunnel_id}")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "create_dns_record", fake_create),
        ]
    ):
        body = cf_routes.CreateDnsBody(hostname="a.example.com", tunnel_id="bad")
        exc = _assert_raises_async(HTTPException, cf_routes.api_create_dns, body)
    assert exc.status_code == 400


def test_api_delete_dns_not_found_maps_404():
    async def fake_delete(_hostname):
        return False

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "delete_dns_record", fake_delete),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_delete_dns, "nope.example.com")
    assert exc.status_code == 404


def test_api_delete_dns_success():
    async def fake_delete(_hostname):
        return True

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "delete_dns_record", fake_delete),
        ]
    ):
        result = _run(cf_routes.api_delete_dns("ok.example.com"))
    assert result == {"status": "deleted", "hostname": "ok.example.com"}


def test_api_list_access_apps_maps_value_error():
    async def fake_apps():
        raise ValueError("apps fail")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "list_access_apps", fake_apps),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_list_access_apps)
    assert exc.status_code == 502


def test_api_create_access_app_success():
    async def fake_create(name, hostname, policy_id):
        assert name == "app"
        assert hostname == "app.example.com"
        assert policy_id == "pol-1"
        return "app-1"

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "create_access_app", fake_create),
        ]
    ):
        body = cf_routes.CreateAccessAppBody(name="app", hostname="app.example.com", policy_id="pol-1")
        result = _run(cf_routes.api_create_access_app(body))
    assert result == {"id": "app-1", "status": "created"}


def test_api_update_access_app_policy_success():
    async def fake_update(app_id, policy_id):
        assert app_id == "app-1"
        assert policy_id == "pol-2"
        return {"id": "app-1", "policies": [{"id": "pol-2"}]}

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "update_access_app_policy", fake_update),
        ]
    ):
        body = cf_routes.UpdateAccessAppPolicyBody(policy_id="pol-2")
        result = _run(cf_routes.api_update_access_app_policy("app-1", body))
    assert result == {"status": "updated", "app": {"id": "app-1", "policies": [{"id": "pol-2"}]}}


def test_api_update_access_app_policy_maps_value_error():
    async def fake_update(_app_id, _policy_id):
        raise ValueError("bad app")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "update_access_app_policy", fake_update),
        ]
    ):
        body = cf_routes.UpdateAccessAppPolicyBody(policy_id="pol-2")
        exc = _assert_raises_async(HTTPException, cf_routes.api_update_access_app_policy, "app-1", body)
    assert exc.status_code == 400


def test_api_delete_access_app_not_found_maps_404():
    async def fake_delete(_hostname):
        return False

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "delete_access_app", fake_delete),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_delete_access_app, "missing.example.com")
    assert exc.status_code == 404


def test_api_list_policies_maps_value_error():
    async def fake_policies():
        raise ValueError("policy fail")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "list_access_policies", fake_policies),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_list_policies)
    assert exc.status_code == 502


def test_api_create_access_policy_success():
    async def fake_create(name, value):
        assert name == "Allow contractors"
        assert value == "contractor.example.com"
        return {"id": "pol-22", "name": "Allow contractors", "members": [{"kind": "email_domain", "value": "contractor.example.com"}]}

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "create_access_policy", fake_create),
        ]
    ):
        body = cf_routes.CreateAccessPolicyBody(name="Allow contractors", value="contractor.example.com")
        result = _run(cf_routes.api_create_access_policy(body))
    assert result == {
        "status": "created",
        "policy": {
            "id": "pol-22",
            "name": "Allow contractors",
            "members": [{"kind": "email_domain", "value": "contractor.example.com"}],
        },
    }


def test_api_create_access_policy_maps_value_error():
    async def fake_create(_name, _value):
        raise ValueError("bad policy")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "create_access_policy", fake_create),
        ]
    ):
        body = cf_routes.CreateAccessPolicyBody(name="Allow contractors", value="contractor.example.com")
        exc = _assert_raises_async(HTTPException, cf_routes.api_create_access_policy, body)
    assert exc.status_code == 400


def test_api_delete_access_policy_success():
    async def fake_delete(policy_id):
        assert policy_id == "pol-22"
        return True

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "delete_access_policy", fake_delete),
        ]
    ):
        result = _run(cf_routes.api_delete_access_policy("pol-22"))
    assert result == {"status": "deleted", "policy_id": "pol-22"}


def test_api_delete_access_policy_not_found_maps_404():
    async def fake_delete(_policy_id):
        return False

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "delete_access_policy", fake_delete),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_delete_access_policy, "pol-22")
    assert exc.status_code == 404


def test_api_add_access_policy_member_success():
    async def fake_add(policy_id, value):
        assert policy_id == "pol-1"
        assert value == "alice@example.com"
        return {"id": "pol-1", "members": [{"kind": "email", "value": "alice@example.com"}]}

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "add_access_policy_member", fake_add),
        ]
    ):
        body = cf_routes.AccessPolicyMemberBody(value="alice@example.com")
        result = _run(cf_routes.api_add_access_policy_member("pol-1", body))
    assert result == {
        "status": "updated",
        "policy": {"id": "pol-1", "members": [{"kind": "email", "value": "alice@example.com"}]},
    }


def test_api_add_access_policy_member_maps_value_error():
    async def fake_add(_policy_id, _value):
        raise ValueError("bad member")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "add_access_policy_member", fake_add),
        ]
    ):
        body = cf_routes.AccessPolicyMemberBody(value="alice@example.com")
        exc = _assert_raises_async(HTTPException, cf_routes.api_add_access_policy_member, "pol-1", body)
    assert exc.status_code == 400


def test_api_remove_access_policy_member_success():
    async def fake_remove(policy_id, value):
        assert policy_id == "pol-1"
        assert value == "contractor.example.com"
        return {"id": "pol-1", "members": []}

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "remove_access_policy_member", fake_remove),
        ]
    ):
        body = cf_routes.AccessPolicyMemberBody(value="contractor.example.com")
        result = _run(cf_routes.api_remove_access_policy_member("pol-1", body))
    assert result == {"status": "updated", "policy": {"id": "pol-1", "members": []}}


def test_api_remove_access_policy_member_maps_value_error():
    async def fake_remove(_policy_id, _value):
        raise ValueError("missing member")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "remove_access_policy_member", fake_remove),
        ]
    ):
        body = cf_routes.AccessPolicyMemberBody(value="contractor.example.com")
        exc = _assert_raises_async(HTTPException, cf_routes.api_remove_access_policy_member, "pol-1", body)
    assert exc.status_code == 400


def test_api_setup_worker_tunnel_rejects_unknown_worker():
    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "get_worker_by_node_id", lambda _node_id: None),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_setup_worker_tunnel, "w1", None)
    assert exc.status_code == 404


def test_api_setup_worker_tunnel_success_defaults_name_from_worker():
    seen = {}

    async def fake_create(name):
        seen["name"] = name
        return {"id": "tid-7"}

    async def fake_get_token(tid):
        seen["token_tid"] = tid
        return "tok-7"

    async def fake_init(tid):
        seen["init_tid"] = tid

    async def fake_proxy(node_id, method, path, body):
        seen["proxy"] = (node_id, method, path, body)
        return {"status": "ok"}

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "_current_cf_config_payload", lambda: {"token": "tok", "account_id": "acct"}),
            (cf_routes, "get_worker_by_node_id", lambda _nid: {"name": "worker-a"}),
            (cf_routes, "create_tunnel", fake_create),
            (cf_routes, "get_tunnel_token", fake_get_token),
            (cf_routes, "init_tunnel_config", fake_init),
            (cf_routes, "set_worker_tunnel_id", lambda node_id, tid: seen.setdefault("set", (node_id, tid))),
            (proxy, "proxy_to_node", fake_proxy),
        ]
    ):
        result = _run(cf_routes.api_setup_worker_tunnel("worker-1", None))

    assert result["status"] == "setup_complete"
    assert result["tunnel_id"] == "tid-7"
    assert seen["name"] == "worker-a"
    assert seen["set"] == ("worker-1", "tid-7")
    assert seen["proxy"] == (
        "worker-1",
        "POST",
        "/api/cf/token",
        {
            "tunnel_id": "tid-7",
            "token": "tok-7",
            "cf_config": {"token": "tok", "account_id": "acct"},
        },
    )


def test_api_setup_missing_worker_tunnels_sets_up_only_missing_workers():
    calls = []

    async def fake_setup(node_id, body=None, send_progress=False):
        calls.append((node_id, body, send_progress))
        return {"status": "setup_complete", "tunnel_id": f"tid-{node_id}", "name": node_id}

    async def fake_progress(step, message, done=False, error=False):
        calls.append(("progress", step, done, error))

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (
                cf_routes,
                "get_node_config",
                lambda: {
                    "role": "master",
                    "workers": {
                        "w1": {"name": "worker-1"},
                        "w2": {"name": "worker-2", "tunnel_id": "existing"},
                        "w3": {"name": "worker-3", "cf_opt_out": True},
                    },
                },
            ),
            (cf_routes, "_setup_worker_tunnel", fake_setup),
            (cf_routes, "_send_worker_cf_setup_progress", fake_progress),
        ]
    ):
        result = _run(cf_routes.api_setup_missing_worker_tunnels())

    assert result["status"] == "setup_complete"
    assert sorted(result["workers"].keys()) == ["w1"]
    assert calls[0] == ("progress", "starting", False, False)
    assert calls[1] == ("w1", None, True)


def test_api_setup_worker_tunnel_uses_body_tunnel_name():
    seen = {}

    async def fake_create(name):
        seen["name"] = name
        return {"id": "tid-8"}

    async def fake_get_token(_tid):
        return "tok"

    async def fake_init(_tid):
        return None

    async def fake_proxy(_node_id, _method, _path, _body):
        return {"status": "ok"}

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "get_worker_by_node_id", lambda _nid: {"name": "worker-a"}),
            (cf_routes, "create_tunnel", fake_create),
            (cf_routes, "get_tunnel_token", fake_get_token),
            (cf_routes, "init_tunnel_config", fake_init),
            (cf_routes, "set_worker_tunnel_id", lambda _node_id, _tid: None),
            (proxy, "proxy_to_node", fake_proxy),
        ]
    ):
        body = cf_routes.SetupWorkerTunnelBody(tunnel_name="custom-name")
        _run(cf_routes.api_setup_worker_tunnel("worker-1", body))

    assert seen["name"] == "custom-name"


def test_api_setup_worker_tunnel_maps_value_error():
    async def fake_create(_name):
        raise ValueError("bad request")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "get_worker_by_node_id", lambda _nid: {"name": "worker-a"}),
            (cf_routes, "create_tunnel", fake_create),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_setup_worker_tunnel, "worker-1", None)
    assert exc.status_code == 400


def test_api_setup_worker_tunnel_maps_runtime_error():
    async def fake_create(_name):
        raise RuntimeError("upstream down")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "get_worker_by_node_id", lambda _nid: {"name": "worker-a"}),
            (cf_routes, "create_tunnel", fake_create),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_setup_worker_tunnel, "worker-1", None)
    assert exc.status_code == 502


def test_api_setup_worker_tunnel_does_not_mark_worker_when_token_push_fails():
    calls = []

    async def fake_create(_name):
        calls.append("create")
        return {"id": "tid-7"}

    async def fake_get_token(_tid):
        calls.append("token")
        return "tok-7"

    async def fake_init(_tid):
        calls.append("init")

    async def fake_proxy(_node_id, _method, _path, _body):
        calls.append("proxy")
        raise RuntimeError("worker unreachable")

    def fake_set(_node_id, _tid):
        calls.append("set")

    with _Patch(
        [
            (cf_routes, "_load_cf_config", _cf_ok),
            (cf_routes, "get_worker_by_node_id", lambda _nid: {"name": "worker-a"}),
            (cf_routes, "create_tunnel", fake_create),
            (cf_routes, "get_tunnel_token", fake_get_token),
            (cf_routes, "init_tunnel_config", fake_init),
            (cf_routes, "set_worker_tunnel_id", fake_set),
            (proxy, "proxy_to_node", fake_proxy),
        ]
    ):
        exc = _assert_raises_async(HTTPException, cf_routes.api_setup_worker_tunnel, "worker-1", None)

    assert exc.status_code == 502
    assert calls == ["create", "token", "init", "proxy"]


def test_cf_setup_validate_token_success():
    def on_get(url, headers=None, params=None):
        if url.endswith("/accounts"):
            return _Resp(
                {
                    "success": True,
                    "result": [{"id": "a1", "name": "Acct One"}],
                }
            )
        if "/cfd_tunnel" in url:
            return _Resp({"success": True, "result": []})
        if url.endswith("/zones") and params and params.get("per_page") == 1:
            return _Resp({"success": True, "result": [{"id": "zone-1"}]})
        if "/dns_records" in url:
            return _Resp({"success": True, "result": []})
        if "/access/policies" in url:
            return _Resp({"success": True, "result": []})
        raise AssertionError(f"Unexpected URL: {url}")

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        result = _run(cf_routes.cf_setup_validate_token(cf_routes.ValidateTokenBody(token="tok")))
    assert result == {"accounts": [{"id": "a1", "name": "Acct One"}]}


def test_cf_setup_validate_token_invalid_token():
    def on_get(_url, headers=None, params=None):
        return _Resp({"success": False, "errors": [{"message": "bad"}]})

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.cf_setup_validate_token,
            cf_routes.ValidateTokenBody(token="tok"),
        )
    assert exc.status_code == 401


def test_cf_setup_validate_token_requires_accessible_account():
    def on_get(_url, headers=None, params=None):
        return _Resp({"success": True, "result": []})

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.cf_setup_validate_token,
            cf_routes.ValidateTokenBody(token="tok"),
        )
    assert exc.status_code == 400


def test_cf_setup_validate_token_maps_transport_error():
    def on_get(_url, headers=None, params=None):
        raise ValueError("parse fail")

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.cf_setup_validate_token,
            cf_routes.ValidateTokenBody(token="tok"),
        )
    assert exc.status_code == 400
    assert "Failed to connect to Cloudflare API" == exc.detail


def test_cf_setup_zones_success():
    def on_get(url, headers=None, params=None):
        assert "zones" in url
        assert params["account.id"] == "acc"
        return _Resp({"success": True, "result": [{"id": "z1", "name": "example.com"}]})

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        result = _run(
            cf_routes.cf_setup_zones(cf_routes.ListZonesBody(token="tok", account_id="acc"))
        )
    assert result == {"zones": [{"id": "z1", "name": "example.com"}]}


def test_cf_setup_zones_maps_failed_response():
    def on_get(_url, headers=None, params=None):
        return _Resp({"success": False, "errors": ["no access"]})

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.cf_setup_zones,
            cf_routes.ListZonesBody(token="tok", account_id="acc"),
        )
    assert exc.status_code == 400


def test_cf_setup_zones_maps_transport_error():
    def on_get(_url, headers=None, params=None):
        raise httpx.HTTPError("down")

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.cf_setup_zones,
            cf_routes.ListZonesBody(token="tok", account_id="acc"),
        )
    assert exc.status_code == 400
    assert exc.detail == "Failed to fetch zones"


def test_cf_setup_policies_returns_empty_on_unsuccessful_response():
    def on_get(_url, headers=None, params=None):
        return _Resp({"success": False, "errors": ["no access"]})

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        result = _run(
            cf_routes.cf_setup_policies(cf_routes.ListPoliciesBody(token="tok", account_id="acc"))
        )
    assert result == {"policies": []}


def test_cf_setup_policies_success():
    def on_get(_url, headers=None, params=None):
        return _Resp(
            {
                "success": True,
                "result": [{"id": "p1", "name": "Default", "decision": "allow"}],
            }
        )

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_get=on_get))]):
        result = _run(
            cf_routes.cf_setup_policies(cf_routes.ListPoliciesBody(token="tok", account_id="acc"))
        )
    assert result == {
        "policies": [{"id": "p1", "name": "Default", "decision": "allow", "include": []}]
    }


def test_cf_setup_create_policy_success():
    def on_post(url, headers=None, json=None):
        assert "access/policies" in url
        assert json["decision"] == "allow"
        assert json["include"][0]["email_domain"]["domain"] == "example.com"
        return _Resp({"success": True, "result": {"id": "pol-9", "name": "Corp"}})

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_post=on_post))]):
        body = cf_routes.CreatePolicyBody(
            token="tok",
            account_id="acc",
            name="Corp",
            email_domain="example.com",
        )
        result = _run(cf_routes.cf_setup_create_policy(body))
    assert result == {"id": "pol-9", "name": "Corp"}


def test_cf_setup_create_policy_maps_failed_response():
    def on_post(_url, headers=None, json=None):
        return _Resp({"success": False, "errors": ["bad payload"]})

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_post=on_post))]):
        body = cf_routes.CreatePolicyBody(
            token="tok",
            account_id="acc",
            name="Corp",
            email_domain="example.com",
        )
        exc = _assert_raises_async(HTTPException, cf_routes.cf_setup_create_policy, body)
    assert exc.status_code == 400


def test_cf_setup_create_policy_maps_transport_error():
    def on_post(_url, headers=None, json=None):
        raise ValueError("decode error")

    with _Patch([(cf_routes.httpx, "AsyncClient", _async_client_factory(on_post=on_post))]):
        body = cf_routes.CreatePolicyBody(
            token="tok",
            account_id="acc",
            name="Corp",
            email_domain="example.com",
        )
        exc = _assert_raises_async(HTTPException, cf_routes.cf_setup_create_policy, body)
    assert exc.status_code == 400
    assert exc.detail == "Failed to create policy"


def test_cf_setup_save_requires_existing_node_config():
    with _Patch([(cf_routes, "get_node_config", lambda: None)]):
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.cf_setup_save,
            cf_routes.SaveCfConfigBody(token="tok", account_id="acc", zone_id="zone", default_policy_id=None),
        )
    assert exc.status_code == 400


def test_cf_setup_save_persists_values():
    seen = {}

    def fake_save(
        token,
        account_id,
        zone_id,
        default_policy_id,
        team_domain=None,
        access_issuer=None,
    ):
        seen["args"] = (
            token,
            account_id,
            zone_id,
            default_policy_id,
            team_domain,
            access_issuer,
        )

    async def fake_discover(token, account_id):
        assert token == "tok"
        assert account_id == "acc"
        return "team-one"

    with _Patch(
        [
            (cf_routes, "get_node_config", lambda: {"role": "master"}),
            (cf_routes, "discover_access_team_domain", fake_discover),
            (cf_routes, "save_cf_config", fake_save),
        ]
    ):
        body = cf_routes.SaveCfConfigBody(
            token="tok",
            account_id="acc",
            zone_id="zone",
            default_policy_id="p-default",
        )
        result = _run(cf_routes.cf_setup_save(body))

    assert result == {"status": "saved"}
    assert seen["args"] == (
        "tok",
        "acc",
        "zone",
        "p-default",
        "team-one",
        "https://team-one.cloudflareaccess.com",
    )


def test_cf_setup_save_maps_team_domain_discovery_error():
    async def fake_discover(_token, _account_id):
        raise ValueError("missing Access Organizations read permission")

    with _Patch(
        [
            (cf_routes, "get_node_config", lambda: {"role": "master"}),
            (cf_routes, "discover_access_team_domain", fake_discover),
        ]
    ):
        exc = _assert_raises_async(
            HTTPException,
            cf_routes.cf_setup_save,
            cf_routes.SaveCfConfigBody(token="tok", account_id="acc", zone_id="zone", default_policy_id=None),
        )
    assert exc.status_code == 400
    assert "missing Access Organizations read permission" in str(exc.detail)


def test_cf_setup_clear_calls_clear_cf_config():
    called = []

    def fake_clear():
        called.append(True)

    with _Patch([(cf_routes, "clear_cf_config", fake_clear)]):
        result = _run(cf_routes.cf_setup_clear())
    assert result == {"status": "cleared"}
    assert called == [True]


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
    print("Running cf routes core/setup tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
