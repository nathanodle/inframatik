"""Tests for MCP protocol and endpoint handling."""

import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException

import mcp_routes


def _response_json(resp):
    return json.loads(resp.body.decode("utf-8"))


class _DummyRequest:
    def __init__(self, body=None, body_exc=None, scope=None, capability="deploy"):
        self._body = body
        self._body_exc = body_exc
        self.state = types.SimpleNamespace(
            service_scope=scope,
            service_capability=capability,
        )

    async def json(self):
        if self._body_exc is not None:
            raise self._body_exc
        return self._body


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        asyncio.run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def test_protocol_initialize_returns_server_info():
    resp = mcp_routes._handle_mcp_protocol_method(1, "initialize", "deploy")
    payload = _response_json(resp)
    assert payload["id"] == 1
    assert payload["result"]["protocolVersion"] == mcp_routes.PROTOCOL_VERSION
    assert payload["result"]["serverInfo"]["name"] == "inframatik"


def test_protocol_notifications_initialized_returns_ok():
    resp = mcp_routes._handle_mcp_protocol_method(2, "notifications/initialized", "deploy")
    assert resp.status_code == 202
    assert resp.body == b""


def test_protocol_tools_list_filters_by_capability_read():
    resp = mcp_routes._handle_mcp_protocol_method(3, "tools/list", "read")
    payload = _response_json(resp)
    tool_names = {t["name"] for t in payload["result"]["tools"]}
    assert "logs" in tool_names
    assert "status" in tool_names
    assert "register" not in tool_names
    assert "deploy" not in tool_names
    assert "restart" not in tool_names
    assert "stop" not in tool_names


def test_protocol_tools_list_filters_by_capability_operate():
    resp = mcp_routes._handle_mcp_protocol_method(4, "tools/list", "operate")
    payload = _response_json(resp)
    tool_names = {t["name"] for t in payload["result"]["tools"]}
    assert "logs" in tool_names
    assert "status" in tool_names
    assert "restart" in tool_names
    assert "stop" in tool_names
    assert "register" not in tool_names
    assert "deploy" not in tool_names


def test_protocol_tools_list_includes_register_for_deploy_capability():
    resp = mcp_routes._handle_mcp_protocol_method(5, "tools/list", "deploy")
    payload = _response_json(resp)
    tool_names = {t["name"] for t in payload["result"]["tools"]}
    assert "register" in tool_names
    assert "deploy" not in tool_names
    assert "restart" in tool_names
    assert "stop" in tool_names
    assert "logs" in tool_names
    assert "status" in tool_names


def test_protocol_unknown_method_returns_none():
    assert mcp_routes._handle_mcp_protocol_method(6, "unknown/method", "deploy") is None


def test_tool_call_rejects_non_dict_params():
    resp = asyncio.run(mcp_routes._handle_mcp_tool_call(1, "svc-a", "deploy", []))
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32602


def test_tool_call_rejects_non_dict_arguments():
    params = {"name": "status", "arguments": "not-an-object"}
    resp = asyncio.run(mcp_routes._handle_mcp_tool_call(2, "svc-a", "read", params))
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32602


def test_tool_call_unknown_tool():
    resp = asyncio.run(
        mcp_routes._handle_mcp_tool_call(
            3,
            "svc-a",
            "deploy",
            {"name": "not_a_tool", "arguments": {}},
        )
    )
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32602


def test_tool_call_capability_denied():
    resp = asyncio.run(
        mcp_routes._handle_mcp_tool_call(
            4,
            "svc-a",
            "read",
            {"name": "register", "arguments": {"command": "echo hi", "working_dir": "/tmp"}},
        )
    )
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32603


def test_tool_call_register_success():
    original_list = mcp_routes.list_services
    original_register = mcp_routes.register_service
    original_start = mcp_routes.start_service

    async def fake_list_services():
        return []

    async def fake_register_service(
        name,
        command,
        working_dir,
        hostname=None,
        access_policy_id=None,
        lan=False,
    ):
        assert name == "svc-a"
        assert command == "python app.py"
        assert working_dir == "/tmp"
        assert hostname == "app.example.com"
        assert access_policy_id == "pol-1"
        assert lan is True
        return {"name": name, "port": 8123, "status": "inactive"}

    async def fake_start_service(name):
        assert name == "svc-a"
        return "active"

    mcp_routes.list_services = fake_list_services
    mcp_routes.register_service = fake_register_service
    mcp_routes.start_service = fake_start_service
    try:
        resp = asyncio.run(
            mcp_routes._handle_mcp_tool_call(
                5,
                "svc-a",
                "deploy",
                {
                    "name": "register",
                    "arguments": {
                        "command": "python app.py",
                        "working_dir": "/tmp",
                        "hostname": "app.example.com",
                        "access_policy_id": "pol-1",
                        "lan": True,
                    },
                },
            )
        )
    finally:
        mcp_routes.list_services = original_list
        mcp_routes.register_service = original_register
        mcp_routes.start_service = original_start

    payload = _response_json(resp)
    assert payload["result"]["content"][0]["type"] == "text"
    text = payload["result"]["content"][0]["text"]
    assert "registered on port 8123" in text
    assert "started. Status: active" in text
    assert "https://app.example.com" in text


def test_tool_call_deploy_is_unknown_after_rename():
    resp = asyncio.run(
        mcp_routes._handle_mcp_tool_call(
            6,
            "svc-a",
            "deploy",
            {"name": "deploy", "arguments": {"command": "python app.py", "working_dir": "/tmp"}},
        )
    )
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32602
    assert "Unknown tool: deploy" in payload["error"]["message"]


def test_tool_call_success_result_shape():
    original = mcp_routes._TOOL_HANDLERS["status"]

    async def fake_status(scope, args):
        assert scope == "svc-a"
        assert args == {}
        return {"text": "ok-status"}

    mcp_routes._TOOL_HANDLERS["status"] = fake_status
    try:
        resp = asyncio.run(
            mcp_routes._handle_mcp_tool_call(
                5,
                "svc-a",
                "read",
                {"name": "status", "arguments": {}},
            )
        )
    finally:
        mcp_routes._TOOL_HANDLERS["status"] = original

    payload = _response_json(resp)
    assert payload["result"]["content"][0]["type"] == "text"
    assert payload["result"]["content"][0]["text"] == "ok-status"


def test_tool_call_handles_value_error_as_is_error():
    original = mcp_routes._TOOL_HANDLERS["status"]

    async def fake_status(_scope, _args):
        raise ValueError("bad input")

    mcp_routes._TOOL_HANDLERS["status"] = fake_status
    try:
        resp = asyncio.run(
            mcp_routes._handle_mcp_tool_call(
                6,
                "svc-a",
                "read",
                {"name": "status", "arguments": {}},
            )
        )
    finally:
        mcp_routes._TOOL_HANDLERS["status"] = original

    payload = _response_json(resp)
    assert payload["result"]["isError"] is True
    assert "bad input" in payload["result"]["content"][0]["text"]


def test_tool_call_handles_os_error_as_is_error():
    original = mcp_routes._TOOL_HANDLERS["status"]

    async def fake_status(_scope, _args):
        raise OSError("io failure")

    mcp_routes._TOOL_HANDLERS["status"] = fake_status
    try:
        resp = asyncio.run(
            mcp_routes._handle_mcp_tool_call(
                7,
                "svc-a",
                "read",
                {"name": "status", "arguments": {}},
            )
        )
    finally:
        mcp_routes._TOOL_HANDLERS["status"] = original

    payload = _response_json(resp)
    assert payload["result"]["isError"] is True
    assert "io failure" in payload["result"]["content"][0]["text"]


def test_endpoint_requires_service_scope():
    req = _DummyRequest(body={"id": 1, "method": "initialize"}, scope=None)
    exc = _assert_raises_async(HTTPException, mcp_routes.mcp_endpoint, req)
    assert exc.status_code == 403


def test_endpoint_parse_error():
    req = _DummyRequest(body_exc=ValueError("bad json"), scope="svc-a")
    resp = asyncio.run(mcp_routes.mcp_endpoint(req))
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32700


def test_endpoint_body_must_be_object():
    req = _DummyRequest(body=["not-object"], scope="svc-a")
    resp = asyncio.run(mcp_routes.mcp_endpoint(req))
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32600


def test_endpoint_method_must_be_string():
    req = _DummyRequest(body={"id": 1, "method": 123}, scope="svc-a")
    resp = asyncio.run(mcp_routes.mcp_endpoint(req))
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32600


def test_endpoint_tools_call_with_invalid_params_object():
    req = _DummyRequest(
        body={"id": 11, "method": "tools/call", "params": "bad"},
        scope="svc-a",
        capability="deploy",
    )
    resp = asyncio.run(mcp_routes.mcp_endpoint(req))
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32602


def test_endpoint_tools_list_ok():
    req = _DummyRequest(
        body={"id": 12, "method": "tools/list"},
        scope="svc-a",
        capability="read",
    )
    resp = asyncio.run(mcp_routes.mcp_endpoint(req))
    payload = _response_json(resp)
    assert payload["id"] == 12
    assert "tools" in payload["result"]


def test_endpoint_initialized_notification_returns_no_body():
    req = _DummyRequest(
        body={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        scope="svc-a",
        capability="deploy",
    )
    resp = asyncio.run(mcp_routes.mcp_endpoint(req))
    assert resp.status_code == 202
    assert resp.body == b""


def test_endpoint_unknown_method_not_found():
    req = _DummyRequest(
        body={"id": 13, "method": "unknown"},
        scope="svc-a",
        capability="deploy",
    )
    resp = asyncio.run(mcp_routes.mcp_endpoint(req))
    payload = _response_json(resp)
    assert payload["error"]["code"] == -32601


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
    print("Running MCP route tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
