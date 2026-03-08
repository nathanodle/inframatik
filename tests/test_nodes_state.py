"""Tests for node registry state transitions and heartbeat behavior."""

import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import nodes


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
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


def _client_factory(*, on_get=None, on_post=None):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            if on_get is None:
                raise AssertionError("Unexpected GET")
            return on_get(url, **kwargs)

        async def post(self, url, **kwargs):
            if on_post is None:
                raise AssertionError("Unexpected POST")
            return on_post(url, **kwargs)

    return _Client


def _run(coro):
    return asyncio.run(coro)


def _reset_state():
    nodes._nodes.clear()
    nodes._id_map.clear()
    nodes._health_cache.clear()


def _with_reset(fn):
    def wrapper():
        _reset_state()
        try:
            fn()
        finally:
            _reset_state()
    wrapper.__name__ = fn.__name__
    return wrapper


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


@_with_reset
def test_register_node_rejects_unknown_api_key():
    with _Patch([(nodes, "get_worker_by_api_key", lambda _k: None)]):
        assert not nodes.register_node("n1", "worker", "http://10.0.0.5:9000", "bad")


@_with_reset
def test_register_node_rejects_bad_reported_address():
    with _Patch(
        [
            (nodes, "get_worker_by_api_key", lambda _k: ("w1", {"address": "http://10.0.0.5:9000"})),
            (nodes, "normalize_worker_address", lambda _a: (_ for _ in ()).throw(ValueError("bad"))),
        ]
    ):
        assert not nodes.register_node("n1", "worker", "bad", "k")


@_with_reset
def test_register_node_rejects_invalid_expected_address():
    with _Patch(
        [
            (nodes, "get_worker_by_api_key", lambda _k: ("w1", {"address": "http://10.0.0.5:9000"})),
            (nodes, "normalize_worker_address", lambda a: a),
            (nodes, "assert_worker_address_allowed", lambda _a: (_ for _ in ()).throw(ValueError("deny"))),
        ]
    ):
        assert not nodes.register_node("n1", "worker", "http://10.0.0.5:9000", "k")


@_with_reset
def test_register_node_rejects_address_mismatch():
    with _Patch(
        [
            (nodes, "get_worker_by_api_key", lambda _k: ("w1", {"address": "http://10.0.0.5:9000"})),
            (nodes, "normalize_worker_address", lambda _a: "http://10.0.0.8:9000"),
            (nodes, "assert_worker_address_allowed", lambda _a: "http://10.0.0.5:9000"),
        ]
    ):
        assert not nodes.register_node("n1", "worker", "http://10.0.0.8:9000", "k")


@_with_reset
def test_register_node_success_sets_maps():
    with _Patch(
        [
            (nodes, "get_worker_by_api_key", lambda _k: ("w1", {"address": "http://10.0.0.5:9000"})),
            (nodes, "normalize_worker_address", lambda _a: "http://10.0.0.5:9000"),
            (nodes, "assert_worker_address_allowed", lambda _a: "http://10.0.0.5:9000"),
        ]
    ):
        assert nodes.register_node("real-node-id", "worker-1", "http://10.0.0.5:9000", "k")

    assert "real-node-id" in nodes._nodes
    assert nodes._id_map["w1"] == "real-node-id"
    assert nodes._id_map["real-node-id"] == "real-node-id"
    assert nodes._nodes["real-node-id"]["config_node_id"] == "w1"


@_with_reset
def test_unregister_node_removes_registered_entry():
    nodes._nodes["real"] = {"node_name": "w", "address": "http://10.0.0.5:9000", "status": "online", "last_seen": 1, "registered_at": 1, "config_node_id": "w1"}
    nodes._id_map["w1"] = "real"
    nodes._id_map["real"] = "real"

    nodes.unregister_node("w1")

    assert "real" not in nodes._nodes
    assert "w1" not in nodes._id_map


@_with_reset
def test_heartbeat_node_updates_last_seen_and_status():
    nodes._nodes["n1"] = {"status": "offline", "last_seen": 10}
    nodes._id_map["cfg-1"] = "n1"

    before = nodes._nodes["n1"]["last_seen"]
    assert nodes.heartbeat_node("cfg-1")
    after = nodes._nodes["n1"]["last_seen"]

    assert after >= before
    assert nodes._nodes["n1"]["status"] == "online"


@_with_reset
def test_heartbeat_node_unknown_returns_false():
    assert not nodes.heartbeat_node("missing")


@_with_reset
def test_validate_heartbeat_key_rejects_unknown_api_key():
    with _Patch([(nodes, "get_worker_by_api_key", lambda _k: None)]):
        assert not nodes.validate_heartbeat_key("n1", "bad")


@_with_reset
def test_validate_heartbeat_key_registered_enforces_binding():
    nodes._nodes["real"] = {"config_node_id": "w1"}
    nodes._id_map["node-a"] = "real"

    with _Patch([(nodes, "get_worker_by_api_key", lambda _k: ("w2", {}))]):
        assert not nodes.validate_heartbeat_key("node-a", "k")

    with _Patch([(nodes, "get_worker_by_api_key", lambda _k: ("w1", {}))]):
        assert nodes.validate_heartbeat_key("node-a", "k")


@_with_reset
def test_validate_heartbeat_key_unregistered_allows_known_key():
    with _Patch([(nodes, "get_worker_by_api_key", lambda _k: ("w1", {}))]):
        assert nodes.validate_heartbeat_key("unregistered", "k")


@_with_reset
def test_check_health_invalid_address_returns_offline():
    with _Patch(
        [
            (nodes, "assert_worker_address_allowed", lambda _a: (_ for _ in ()).throw(ValueError("deny"))),
        ]
    ):
        status = _run(nodes._check_health("http://10.0.0.5:9000"))
    assert status == "offline"


@_with_reset
def test_check_health_uses_cache_without_http_call():
    address = "http://10.0.0.5:9000"
    nodes._health_cache[address] = ("online", 100.0)

    def fake_time():
        return 105.0

    with _Patch(
        [
            (nodes, "assert_worker_address_allowed", lambda a: a),
            (nodes.time, "time", fake_time),
            (nodes.httpx, "AsyncClient", _client_factory(on_get=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no http")))),
        ]
    ):
        status = _run(nodes._check_health(address))
    assert status == "online"


@_with_reset
def test_check_health_marks_online_when_node_health_200():
    calls = []

    def on_get(url, **kwargs):
        calls.append(url)
        return _Resp(status_code=200)

    with _Patch(
        [
            (nodes, "assert_worker_address_allowed", lambda a: a),
            (nodes.httpx, "AsyncClient", _client_factory(on_get=on_get)),
        ]
    ):
        status = _run(nodes._check_health("http://10.0.0.5:9000"))
    assert status == "online"
    assert calls == ["http://10.0.0.5:9000/api/node/health"]


@_with_reset
def test_check_health_falls_back_to_system_endpoint():
    calls = []

    def on_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/node/health"):
            return _Resp(status_code=404)
        return _Resp(status_code=200)

    with _Patch(
        [
            (nodes, "assert_worker_address_allowed", lambda a: a),
            (nodes.httpx, "AsyncClient", _client_factory(on_get=on_get)),
        ]
    ):
        status = _run(nodes._check_health("http://10.0.0.5:9000"))
    assert status == "online"
    assert calls == [
        "http://10.0.0.5:9000/api/node/health",
        "http://10.0.0.5:9000/api/system",
    ]


@_with_reset
def test_check_health_http_error_sets_offline():
    def on_get(url, **kwargs):
        raise httpx.HTTPError("timeout")

    with _Patch(
        [
            (nodes, "assert_worker_address_allowed", lambda a: a),
            (nodes.httpx, "AsyncClient", _client_factory(on_get=on_get)),
        ]
    ):
        status = _run(nodes._check_health("http://10.0.0.5:9000"))
    assert status == "offline"


@_with_reset
def test_get_all_nodes_includes_self_registered_and_unregistered_workers():
    config = {
        "node_id": "master-1",
        "node_name": "master",
        "tunnel_id": "tm",
        "workers": {
            "w-reg": {"name": "worker-reg", "address": "http://10.0.0.2:9000", "tunnel_id": "tw1"},
            "w-cfg": {"name": "worker-cfg", "address": "http://10.0.0.3:9000", "tunnel_id": "tw2"},
        },
    }
    nodes._nodes["real-1"] = {
        "node_name": "worker-reg",
        "address": "http://10.0.0.2:9000",
        "status": "online",
        "last_seen": 123.0,
        "registered_at": 120.0,
        "config_node_id": "w-reg",
    }

    async def fake_health(address):
        assert address == "http://10.0.0.3:9000"
        return "offline"

    with _Patch(
        [
            (nodes, "get_node_config", lambda: config),
            (nodes, "_check_health", fake_health),
        ]
    ):
        result = _run(nodes.get_all_nodes())

    assert result[0]["is_self"] is True
    assert result[0]["node_id"] == "master-1"
    by_id = {entry["node_id"]: entry for entry in result}
    assert by_id["real-1"]["tunnel_id"] == "tw1"
    assert by_id["w-cfg"]["status"] == "offline"
    assert by_id["w-cfg"]["tunnel_id"] == "tw2"


@_with_reset
def test_get_all_nodes_without_config_still_returns_registered_nodes():
    nodes._nodes["real-1"] = {
        "node_name": "worker-reg",
        "address": "http://10.0.0.2:9000",
        "status": "online",
        "last_seen": 123.0,
        "registered_at": 120.0,
        "config_node_id": "w-reg",
    }

    with _Patch([(nodes, "get_node_config", lambda: None)]):
        result = _run(nodes.get_all_nodes())

    assert len(result) == 1
    assert result[0]["node_id"] == "real-1"
    assert result[0]["tunnel_id"] is None


@_with_reset
def test_resolve_node_self_returns_none():
    with _Patch([(nodes, "get_node_config", lambda: {"node_id": "master", "workers": {}})]):
        assert nodes.resolve_node("master") is None


@_with_reset
def test_resolve_node_registered_uses_worker_config_and_normalizes():
    nodes._nodes["real-1"] = {
        "config_node_id": "w1",
        "address": "http://10.0.0.2:9000",
        "status": "online",
        "last_seen": 1,
        "registered_at": 1,
        "node_name": "w1",
    }
    nodes._id_map["w1"] = "real-1"
    config = {
        "node_id": "master",
        "workers": {"w1": {"address": "HTTP://WORKER.local:9000", "api_key": "k1"}},
    }

    with _Patch(
        [
            (nodes, "get_node_config", lambda: config),
            (nodes, "assert_worker_address_allowed", lambda address, config=None: "https://worker.local:9000"),
        ]
    ):
        result = nodes.resolve_node("w1")

    assert result == {"address": "https://worker.local:9000", "api_key": "k1"}


@_with_reset
def test_resolve_node_registered_requires_configured_address():
    nodes._nodes["real-1"] = {"config_node_id": "w1", "status": "online", "last_seen": 1, "registered_at": 1, "node_name": "w1", "address": "http://x"}
    nodes._id_map["w1"] = "real-1"
    config = {"node_id": "master", "workers": {"w1": {"api_key": "k1"}}}

    with _Patch([(nodes, "get_node_config", lambda: config)]):
        exc = _assert_raises(ValueError, nodes.resolve_node, "w1")
    assert "has no configured address" in str(exc)


@_with_reset
def test_resolve_node_registered_invalid_address_is_wrapped():
    nodes._nodes["real-1"] = {"config_node_id": "w1", "status": "online", "last_seen": 1, "registered_at": 1, "node_name": "w1", "address": "http://x"}
    nodes._id_map["w1"] = "real-1"
    config = {"node_id": "master", "workers": {"w1": {"address": "http://bad", "api_key": "k1"}}}

    with _Patch(
        [
            (nodes, "get_node_config", lambda: config),
            (nodes, "assert_worker_address_allowed", lambda address, config=None: (_ for _ in ()).throw(ValueError("blocked"))),
        ]
    ):
        exc = _assert_raises(ValueError, nodes.resolve_node, "w1")
    assert "Invalid worker address for 'w1'" in str(exc)


@_with_reset
def test_resolve_node_unregistered_from_config():
    config = {
        "node_id": "master",
        "workers": {"w2": {"address": "http://10.0.0.3:9000", "api_key": "k2"}},
    }

    with _Patch(
        [
            (nodes, "get_node_config", lambda: config),
            (nodes, "assert_worker_address_allowed", lambda address, config=None: "http://10.0.0.3:9000"),
        ]
    ):
        result = nodes.resolve_node("w2")
    assert result == {"address": "http://10.0.0.3:9000", "api_key": "k2"}


@_with_reset
def test_resolve_node_unknown_raises():
    config = {"node_id": "master", "workers": {}}
    with _Patch([(nodes, "get_node_config", lambda: config)]):
        exc = _assert_raises(ValueError, nodes.resolve_node, "nope")
    assert "Unknown node: nope" in str(exc)


@_with_reset
def test_check_stale_nodes_marks_old_entries_offline():
    now = time.time()
    nodes._nodes["fresh"] = {"last_seen": now - 1, "status": "online"}
    nodes._nodes["old"] = {"last_seen": now - (nodes.STALE_THRESHOLD + 5), "status": "online"}

    nodes.check_stale_nodes()

    assert nodes._nodes["fresh"]["status"] == "online"
    assert nodes._nodes["old"]["status"] == "offline"


@_with_reset
def test_stale_checker_loop_runs_check_then_sleeps():
    seen = {"checks": 0, "sleeps": []}

    async def fake_sleep(delay):
        seen["sleeps"].append(delay)
        raise RuntimeError("stop-loop")

    def fake_check():
        seen["checks"] += 1

    with _Patch(
        [
            (nodes, "check_stale_nodes", fake_check),
            (nodes.asyncio, "sleep", fake_sleep),
        ]
    ):
        exc = _assert_raises_async(RuntimeError, nodes.stale_checker_loop)

    assert "stop-loop" in str(exc)
    assert seen["checks"] == 1
    assert seen["sleeps"] == [10]


@_with_reset
def test_heartbeat_sender_loop_returns_when_not_worker():
    with _Patch([(nodes, "get_node_config", lambda: {"role": "master"})]):
        assert _run(nodes.heartbeat_sender_loop()) is None


@_with_reset
def test_heartbeat_sender_loop_registers_and_reregisters_on_404():
    calls = []
    responses = [
        _Resp(status_code=200, text="registered"),
        _Resp(status_code=404, text="missing"),
        _Resp(status_code=200, text="registered-again"),
    ]

    def on_post(url, headers=None, json=None):
        calls.append((url, headers, json))
        return responses.pop(0)

    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        if len(sleep_calls) == 1:
            return None
        raise RuntimeError("stop-heartbeat-loop")

    config = {
        "role": "worker",
        "node_id": "worker-1",
        "node_name": "worker-1",
        "api_key": "k1",
        "master_url": "http://127.0.0.1:9000",
        "listen_port": 9000,
    }

    with _Patch(
        [
            (nodes, "get_node_config", lambda: config),
            (nodes.httpx, "AsyncClient", _client_factory(on_post=on_post)),
            (nodes.asyncio, "sleep", fake_sleep),
        ]
    ):
        exc = _assert_raises_async(RuntimeError, nodes.heartbeat_sender_loop)

    assert "stop-heartbeat-loop" in str(exc)
    assert sleep_calls == [15, 15]
    urls = [c[0] for c in calls]
    assert urls == [
        "http://127.0.0.1:9000/api/nodes/register",
        "http://127.0.0.1:9000/api/nodes/heartbeat",
        "http://127.0.0.1:9000/api/nodes/register",
    ]
    assert calls[0][2]["address"].endswith(":9000")


@_with_reset
def test_heartbeat_sender_loop_retries_registration_with_backoff():
    calls = []

    def on_post(url, headers=None, json=None):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("network down")
        return _Resp(status_code=200)

    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        if delay == 5:
            return None
        raise RuntimeError("stop-after-registration")

    config = {
        "role": "worker",
        "node_id": "worker-2",
        "node_name": "worker-2",
        "api_key": "k2",
        "master_url": "http://127.0.0.1:9000",
        "listen_port": 9100,
    }

    with _Patch(
        [
            (nodes, "get_node_config", lambda: config),
            (nodes.httpx, "AsyncClient", _client_factory(on_post=on_post)),
            (nodes.asyncio, "sleep", fake_sleep),
        ]
    ):
        exc = _assert_raises_async(RuntimeError, nodes.heartbeat_sender_loop)

    assert "stop-after-registration" in str(exc)
    assert sleep_calls[0] == 5
    assert sleep_calls[1] == 15
    assert calls[0].endswith("/api/nodes/register")
    assert calls[1].endswith("/api/nodes/register")


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
    print("Running node state tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
