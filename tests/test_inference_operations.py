import asyncio
import hashlib
import json
import socket
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference_launchers
import inference_operations
import inference_planner
import inference_profiles
import model_storage
import node_config
import services
import system


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


@contextmanager
def _temp_inference(tmpdir: Path, port: int = 10000):
    config_dir = tmpdir / "config"
    store = tmpdir / "models"
    unit_dir = tmpdir / "units"
    config_file = config_dir / "node.json"
    profiles_file = config_dir / "inference_profiles.json"
    patches = [
        (model_storage, "CONFIG_DIR", config_dir),
        (model_storage, "MODELS_FILE", config_dir / "models.json"),
        (model_storage, "MODEL_JOBS_FILE", config_dir / "model_jobs.json"),
        (model_storage, "INFERENCE_PROFILES_FILE", profiles_file),
        (model_storage, "DEFAULT_MODEL_STORE_ROOT", store),
        (inference_launchers, "CONFIG_DIR", config_dir),
        (inference_launchers, "LAUNCHERS_FILE", config_dir / "inference_engine_launchers.json"),
        (inference_launchers, "INFERENCE_PROFILES_FILE", profiles_file),
        (inference_planner, "CONFIG_DIR", config_dir),
        (inference_planner, "INFERENCE_PROFILES_FILE", profiles_file),
        (inference_profiles, "CONFIG_DIR", config_dir),
        (inference_profiles, "INFERENCE_PROFILES_FILE", profiles_file),
        (inference_profiles, "INFERENCE_SECRETS_FILE", config_dir / "inference_secrets.json"),
        (inference_profiles, "INFERENCE_CLEANUP_FILE", config_dir / "inference_cleanup.json"),
        (inference_profiles, "UNIT_DIR", unit_dir),
        (inference_operations, "CONFIG_DIR", config_dir),
        (inference_operations, "INFERENCE_OPERATIONS_FILE", config_dir / "inference_operations.json"),
        (inference_operations, "POLL_INTERVAL_SECONDS", 0.01),
        (inference_operations, "STARTUP_STATUS_INTERVAL_SECONDS", 0.01),
        (node_config, "CONFIG_FILE", config_file),
        (services, "SERVICES_FILE", config_dir / "services.json"),
        (services, "PORTS_ENV_FILE", config_dir / "ports.env"),
    ]
    with _Patch(patches):
        node_config.invalidate_cache()
        node_config.save_node_config(
            {
                "role": "standalone",
                "node_id": "node-a",
                "node_name": "node-a",
                "model_store_root": str(store),
                "port_ranges": {"inference": f"{port}-{port + 10}"},
            }
        )
        model_storage.initialize_model_storage()
        inference_launchers.initialize_launcher_registry()
        inference_profiles.initialize_profile_registries()
        inference_operations.initialize_operations_registry()
        original_metrics = system.get_system_metrics
        system.get_system_metrics = lambda: {"gpus": [{"index": 0, "name": "GPU 0", "mem_total_mb": 49152, "mem_used_mb": 512}]}
        try:
            yield {"config_dir": config_dir, "store": store, "unit_dir": unit_dir, "profiles_file": profiles_file, "port": port}
        finally:
            system.get_system_metrics = original_metrics
            node_config.invalidate_cache()


def _run(coro):
    return asyncio.run(coro)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_executable(path: Path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | 0o111)


def _write_model(store: Path):
    snapshot_dir = store / "artifacts" / "qwen" / "snapshots" / "v1"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    payload = snapshot_dir / "model.safetensors"
    payload.write_bytes(b"model bytes")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "id": "qwen",
        "snapshot": "v1",
        "display_name": "qwen",
        "kind": "hf_snapshot",
        "format": "safetensors",
        "source": {"type": "test"},
        "files": [{"path": payload.name, "size": payload.stat().st_size, "sha256": digest}],
        "metadata": {},
        "created_at": 1,
        "size_bytes": payload.stat().st_size,
    }
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    model_storage._save_registry(
        {
            "schema_version": 1,
            "artifacts": {
                "qwen": {
                    "id": "qwen",
                    "kind": "hf_snapshot",
                    "format": "safetensors",
                    "display_name": "qwen",
                    "active_snapshot": "v1",
                    "artifact_path": str(store / "artifacts" / "qwen"),
                    "runtime_path": str(snapshot_dir),
                    "snapshots": {
                        "v1": {
                            "state": "ready",
                            "manifest_path": str(manifest_path),
                            "snapshot_path": str(snapshot_dir),
                            "runtime_path": str(snapshot_dir),
                            "size_bytes": payload.stat().st_size,
                            "created_at": 1,
                        }
                    },
                }
            },
        }
    )


def _setup_profile(tmp_path: Path, port: int):
    _write_model(tmp_path / "models")
    exe = tmp_path / "vllm"
    _make_executable(exe)
    inference_launchers.create_launcher(
        launcher_id="vllm-main",
        display_name="vLLM",
        engine="vllm",
        executable=str(exe),
    )
    return inference_profiles.create_profile(
        {
            "id": "qwen",
            "display_name": "Qwen",
            "engine": "vllm",
            "engine_launcher_id": "vllm-main",
            "model": {"artifact_id": "qwen", "snapshot": "v1"},
            "common": {"served_model_name": "qwen", "startup_grace_seconds": 0.12, "port": port},
            "deployment": {"port_policy": {"mode": "explicit", "ports": [port]}, "gpu_policy": {"mode": "profile", "gpu_ids": [0]}},
        }
    )


@contextmanager
def _fake_runtime(tcp_ok=True, journal_text="TOKEN=secret\nready", restart_count=None, unit_state="active"):
    actions = []

    async def fake_systemctl(action, unit):
        actions.append((action, unit))
        return {"ok": True, "code": 0, "output": action}

    async def fake_state(_unit):
        return unit_state() if callable(unit_state) else unit_state

    async def fake_tcp(_host, _port):
        return tcp_ok() if callable(tcp_ok) else tcp_ok

    async def fake_journal(_unit, lines=300):
        return journal_text

    async def fake_restart_count(_unit):
        if callable(restart_count):
            return restart_count()
        return restart_count if restart_count is not None else 0

    patches = [
        (inference_operations, "systemctl_user", fake_systemctl),
        (inference_operations, "unit_active_state", fake_state),
        (inference_operations, "tcp_ready", fake_tcp),
        (inference_operations, "read_journal", fake_journal),
        (inference_operations, "unit_restart_count", fake_restart_count),
    ]
    with _Patch(patches):
        yield actions


def test_operation_start_success_and_profile_state(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)
        with _fake_runtime() as actions:
            async def scenario():
                op = await inference_operations.start_profile("qwen")
                done = await inference_operations.wait_for_operation(op["id"])
                profile = inference_profiles.get_profile_raw("qwen")
                return op, done, profile

            op, done, profile = _run(scenario())

        assert op["state"] == "queued"
        assert done["state"] == "succeeded"
        assert done["result"]["state"] == "running"
        assert profile["state"] == "running"
        assert profile["instances"][0]["state"] == "running"
        assert actions[0][0] == "start"


def test_profile_start_publishes_live_readiness_status(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)
        checks = {"count": 0}

        def tcp_after_probe():
            checks["count"] += 1
            return checks["count"] >= 3

        with _fake_runtime(tcp_ok=tcp_after_probe, restart_count=0):
            async def scenario():
                op = await inference_operations.start_profile("qwen")
                return await inference_operations.wait_for_operation(op["id"], timeout=1.0)

            done = _run(scenario())

        assert done["state"] == "succeeded"
        status = done["runtime_status"]
        assert status["phase"] == "waiting_ready"
        assert status["instance_index"] == 0
        assert status["unit"] == "infra-llm-qwen.service"
        assert status["host"] == "127.0.0.1"
        assert status["port"] == port
        assert status["systemd_state"] == "active"
        assert status["tcp_reachable"] is True
        assert status["restart_count"] == 0
        assert status["wait_position"] == 1
        assert status["wait_total"] == 1
        assert status["elapsed_seconds"] >= 0
        assert status["log_tail"] == "TOKEN=<redacted>\nready"
        assert status["log_tail_lines"] == 2


def test_profile_start_fails_before_systemd_when_launcher_runtime_invalid(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)

        async def fake_validate_runtime(_launcher_id):
            return {
                "valid": False,
                "errors": ["Runtime probe exited with code 7"],
                "runtime": {
                    "checked": True,
                    "valid": False,
                    "code": 7,
                    "output": "ImportError: libcudart.so.12: cannot open shared object file",
                    "suggested_env": {"LD_LIBRARY_PATH": "/venv/nvidia/cuda_runtime/lib"},
                },
            }

        with _Patch([(inference_launchers, "validate_launcher_runtime", fake_validate_runtime)]):
            with _fake_runtime() as actions:
                async def scenario():
                    op = await inference_operations.start_profile("qwen")
                    done = await inference_operations.wait_for_operation(op["id"], timeout=1.0)
                    profile = inference_profiles.get_profile_raw("qwen")
                    return done, profile

                done, profile = _run(scenario())

        assert done["state"] == "failed"
        assert done["result"]["message"] == "Launcher runtime validation failed"
        assert done["result"]["launcher_id"] == "vllm-main"
        assert done["result"]["validation"]["runtime"]["code"] == 7
        assert done["result"]["suggested_env"]["LD_LIBRARY_PATH"] == "/venv/nvidia/cuda_runtime/lib"
        assert done["steps"][0]["name"] == "validate"
        assert done["steps"][0]["state"] == "failed"
        assert actions == []
        assert profile["state"] == "stopped"


def test_profile_start_rolls_back_when_tcp_never_ready(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)
        with _fake_runtime(tcp_ok=False) as actions:
            async def scenario():
                op = await inference_operations.start_profile("qwen")
                done = await inference_operations.wait_for_operation(op["id"], timeout=1.0)
                profile = inference_profiles.get_profile_raw("qwen")
                return done, profile

            done, profile = _run(scenario())

        assert done["state"] == "failed"
        assert "Start failed" in done["result"]["message"]
        cause = done["result"]["cause"]
        assert cause["systemd_state"] == "active"
        assert cause["tcp_reachable"] is False
        assert cause["timeout_seconds"] > 0
        assert cause["elapsed_seconds"] >= cause["timeout_seconds"]
        assert cause["runtime_status"]["unit"] == "infra-llm-qwen.service"
        assert cause["runtime_status"]["log_tail"] == "TOKEN=<redacted>\nready"
        assert done["runtime_status"]["elapsed_seconds"] == cause["elapsed_seconds"]
        assert profile["state"] == "failed"
        assert ("stop", "infra-llm-qwen.service") in actions


def test_profile_start_reports_restart_loop_logs(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)
        restarts = {"count": -1}

        def next_restart_count():
            restarts["count"] += 1
            return restarts["count"]

        with _fake_runtime(
            tcp_ok=False,
            journal_text="API_TOKEN=secret\nImportError: libcudart.so.12: cannot open shared object file",
            restart_count=next_restart_count,
        ):
            async def scenario():
                op = await inference_operations.start_profile("qwen")
                return await inference_operations.wait_for_operation(op["id"], timeout=1.0)

            done = _run(scenario())

        assert done["state"] == "failed"
        cause = done["result"]["cause"]
        assert "restarted" in cause["message"]
        assert cause["systemd_state"] == "active"
        assert cause["tcp_reachable"] is False
        assert cause["runtime_status"]["restart_count"] == cause["restart_count"]
        assert "libcudart.so.12" in cause["runtime_status"]["log_tail"]
        assert "libcudart.so.12" in cause["logs"]
        assert "API_TOKEN=<redacted>" in cause["logs"]


def test_profile_start_reports_fast_exit_logs(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)
        with _fake_runtime(
            tcp_ok=False,
            unit_state="inactive",
            journal_text="HF_TOKEN=secret\nRuntimeError: CUDA_HOME is not set",
        ):
            async def scenario():
                op = await inference_operations.start_profile("qwen")
                return await inference_operations.wait_for_operation(op["id"], timeout=1.0)

            done = _run(scenario())

        assert done["state"] == "failed"
        cause = done["result"]["cause"]
        assert "inactive" in cause["message"]
        assert cause["systemd_state"] == "inactive"
        assert cause["tcp_reachable"] is False
        assert cause["runtime_status"]["systemd_state"] == "inactive"
        assert cause["runtime_status"]["log_tail"] == "HF_TOKEN=<redacted>\nRuntimeError: CUDA_HOME is not set"
        assert "CUDA_HOME is not set" in cause["logs"]
        assert "HF_TOKEN=<redacted>" in cause["logs"]


def test_active_operation_conflict_and_interrupted_reconciliation(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)
        existing = inference_operations._create_operation("profile_start", "qwen")
        try:
            _run(inference_operations.start_profile("qwen"))
        except inference_operations.OperationConflictError as e:
            assert e.detail["active_operation_id"] == existing["id"]
        else:
            raise AssertionError("Expected active operation conflict")

        marked = inference_operations.mark_interrupted_operations()
        interrupted = inference_operations.get_operation(existing["id"])
        assert marked["interrupted"] == [existing["id"]]
        assert interrupted["state"] == "failed_interrupted"


def test_logs_health_and_manual_test(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)

        async def handle(reader, writer):
            await reader.read(4096)
            body = b'{"data":[]}'
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Content-Type: application/json\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async def scenario():
            server = await asyncio.start_server(handle, "127.0.0.1", port)
            async with server:
                with _fake_runtime(tcp_ok=True, journal_text="API_TOKEN=secret\nok"):
                    logs = await inference_operations.get_profile_logs("qwen")
                    health = await inference_operations.get_profile_health("qwen")
                    tested = await inference_operations.test_profile("qwen")
                    return logs, health, tested

        logs, health, tested = _run(scenario())
        assert "API_TOKEN=<redacted>" in logs["logs"]
        assert health["health"] == "healthy"
        assert tested["status_code"] == 200
        assert tested["body_preview"] == '{"data":[]}'


def test_http_start_endpoint_returns_operation_and_allows_poll(tmp_path: Path):
    port = _free_port()
    with _temp_inference(tmp_path, port=port):
        _setup_profile(tmp_path, port)
        import auth
        import main

        async def auth_true(_request):
            return True

        async def scenario():
            with _Patch([(auth, "check_auth", auth_true)]):
                with _fake_runtime():
                    transport = httpx.ASGITransport(app=main.app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                        started = await client.post("/api/inference/profiles/qwen/start")
                        op_id = started.json()["id"]
                        done = await inference_operations.wait_for_operation(op_id)
                        fetched = await client.get(f"/api/inference/operations/{op_id}")
                        return started, done, fetched

        started, done, fetched = _run(scenario())
        assert started.status_code == 202
        assert done["state"] == "succeeded"
        assert fetched.json()["state"] == "succeeded"


def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for test in tests:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                test(Path(tmpdir))
            print(f"  OK {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    print("Running inference operation tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
