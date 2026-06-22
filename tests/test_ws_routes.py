"""Tests for WebSocket auth behavior."""

import asyncio
import sys
from pathlib import Path

from fastapi import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).parent.parent))

import ws_routes


class _DummyWebSocket:
    def __init__(self, cookies=None, query_params=None):
        self.cookies = cookies or {}
        self.query_params = query_params or {}
        self.accepted = False
        self.closed = None

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        raise WebSocketDisconnect(code=1000)



def _run(coro):
    return asyncio.run(coro)


def test_websocket_rejects_query_token_without_cookie():
    original_validate_session = ws_routes.validate_session
    ws_routes.validate_session = lambda token: token == "sess-ok"
    try:
        ws = _DummyWebSocket(query_params={"token": "sess-ok"})
        _run(ws_routes.websocket_endpoint(ws))
    finally:
        ws_routes.validate_session = original_validate_session
        ws_routes._connections.clear()

    assert ws.accepted is False
    assert ws.closed == (4001, "Authentication required")


def test_websocket_accepts_valid_session_cookie():
    original_validate_session = ws_routes.validate_session
    ws_routes.validate_session = lambda token: token == "sess-ok"
    try:
        ws = _DummyWebSocket(cookies={"inframatik_session": "sess-ok"})
        _run(ws_routes.websocket_endpoint(ws))
    finally:
        ws_routes.validate_session = original_validate_session

    assert ws.accepted is True
    assert ws.closed is None
    assert len(ws_routes._connections) == 0


def test_websocket_rejects_invalid_session_cookie():
    original_validate_session = ws_routes.validate_session
    ws_routes.validate_session = lambda token: token == "sess-ok"
    try:
        ws = _DummyWebSocket(cookies={"inframatik_session": "bad"})
        _run(ws_routes.websocket_endpoint(ws))
    finally:
        ws_routes.validate_session = original_validate_session
        ws_routes._connections.clear()

    assert ws.accepted is False
    assert ws.closed == (4001, "Authentication required")


def test_worker_event_payload_forwards_only_inference_events():
    config = {
        "role": "worker",
        "master_url": "http://master:9000/",
        "api_key": "worker-key",
        "node_id": "worker-real",
    }

    payload = ws_routes._worker_event_payload(
        {"type": "inference_operation", "operation": {"id": "op-1"}},
        config=config,
    )

    assert payload["master_url"] == "http://master:9000"
    assert payload["api_key"] == "worker-key"
    assert payload["payload"]["node_id"] == "worker-real"
    assert payload["payload"]["event"]["type"] == "inference_operation"
    assert ws_routes._worker_event_payload({"type": "progress"}, config=config) is None
    assert ws_routes._worker_event_payload({"type": "model_job"}, config={"role": "master"}) is None


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
    print("Running websocket route tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
