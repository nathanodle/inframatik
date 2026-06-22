import asyncio
import hashlib
import json
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
def _temp_inference(tmpdir: Path):
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
                "port_ranges": {"inference": "10000-10010"},
            }
        )
        model_storage.initialize_model_storage()
        inference_launchers.initialize_launcher_registry()
        inference_profiles.initialize_profile_registries()
        inference_operations.initialize_operations_registry()
        original_metrics = system.get_system_metrics
        system.get_system_metrics = lambda: {
            "gpus": [
                {"index": index, "name": f"GPU {index}", "mem_total_mb": 49152, "mem_used_mb": 512}
                for index in range(4)
            ]
        }
        try:
            yield {"config_dir": config_dir, "store": store, "unit_dir": unit_dir, "profiles_file": profiles_file}
        finally:
            system.get_system_metrics = original_metrics
            node_config.invalidate_cache()


def _run(coro):
    return asyncio.run(coro)


def _make_executable(path: Path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | 0o111)


def _write_model(store: Path, artifact_id="qwen", snapshot="v1", file_name="model.safetensors"):
    snapshot_dir = store / "artifacts" / artifact_id / "snapshots" / snapshot
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    payload = snapshot_dir / file_name
    payload.write_bytes(b"model bytes")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "id": artifact_id,
        "snapshot": snapshot,
        "display_name": artifact_id,
        "kind": "hf_snapshot",
        "format": "safetensors",
        "source": {"type": "test"},
        "files": [{"path": file_name, "size": payload.stat().st_size, "sha256": digest}],
        "metadata": {},
        "created_at": 1,
        "size_bytes": payload.stat().st_size,
    }
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    registry = model_storage._load_registry()
    registry.setdefault("artifacts", {})[artifact_id] = {
        "id": artifact_id,
        "kind": "hf_snapshot",
        "format": "safetensors",
        "display_name": artifact_id,
        "active_snapshot": snapshot,
        "artifact_path": str(store / "artifacts" / artifact_id),
        "runtime_path": str(snapshot_dir),
        "snapshots": {
            snapshot: {
                "state": "ready",
                "manifest_path": str(manifest_path),
                "snapshot_path": str(snapshot_dir),
                "runtime_path": str(snapshot_dir),
                "size_bytes": payload.stat().st_size,
                "created_at": 1,
            }
        },
    }
    model_storage._save_registry(registry)
    return snapshot_dir


def _setup_launcher(tmp_path: Path):
    exe = tmp_path / "vllm"
    _make_executable(exe)
    inference_launchers.create_launcher(
        launcher_id="vllm-main",
        display_name="vLLM",
        engine="vllm",
        executable=str(exe),
        env={"VISIBLE": "yes", "LAUNCHER_TOKEN": "launcher-secret"},
    )
    return exe


def _profile(replicas=1):
    deployment = {
        "gpu_policy": {"mode": "profile", "gpu_ids": [0]},
    }
    if replicas > 1:
        deployment = {
            "mode": "replicated",
            "replicas": replicas,
            "port_policy": {"mode": "contiguous"},
            "gpu_policy": {"mode": "one_per_instance", "gpu_ids": list(range(replicas))},
        }
    return {
        "id": "qwen",
        "display_name": "Qwen",
        "engine": "vllm",
        "engine_launcher_id": "vllm-main",
        "model": {"artifact_id": "qwen", "snapshot": "v1"},
        "common": {"served_model_name": "qwen", "context_length": 4096},
        "advanced": {"env": {"PROFILE_TOKEN": "profile-secret"}},
        "deployment": deployment,
    }


def test_create_profile_writes_registry_and_raw_unit_redacts_response(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        _setup_launcher(tmp_path)

        result = inference_profiles.create_profile(_profile())
        registry = json.loads(ctx["profiles_file"].read_text())
        stored = registry["profiles"]["qwen"]
        unit_path = ctx["unit_dir"] / "infra-llm-qwen.service"
        unit_content = unit_path.read_text()

        assert result["status"] == "created"
        assert stored["instances"][0]["port"] == 10000
        assert unit_path.exists()
        assert "PROFILE_TOKEN=profile-secret" in unit_content
        assert "LAUNCHER_TOKEN=launcher-secret" in unit_content
        assert result["profile"]["advanced"]["env"]["PROFILE_TOKEN"] == "<redacted>"
        assert result["profile"]["command_preview"][0]["env"]["PROFILE_TOKEN"] == "<redacted>"
        assert "_env_raw" not in result["profile"]["command_preview"][0]
        assert "profile-secret" not in json.dumps(result)


def test_update_preserves_assignments_removes_stale_unit_and_marks_restart(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        _setup_launcher(tmp_path)

        created = inference_profiles.create_profile(_profile(replicas=2))
        assert [item["port"] for item in created["profile"]["instances"]] == [10000, 10001]
        assert (ctx["unit_dir"] / "infra-llm-qwen@1.service").exists()

        display_update = inference_profiles.update_profile("qwen", {"display_name": "Qwen renamed"})
        assert display_update["profile"]["display_name"] == "Qwen renamed"
        assert display_update["profile"]["restart_required"] is False
        assert [item["port"] for item in display_update["profile"]["instances"]] == [10000, 10001]

        registry = json.loads(ctx["profiles_file"].read_text())
        registry["profiles"]["qwen"]["state"] = "running"
        ctx["profiles_file"].write_text(json.dumps(registry))

        runtime_update = inference_profiles.update_profile(
            "qwen",
            {
                "common": {"served_model_name": "qwen", "context_length": 8192},
                "deployment": {
                    "mode": "replicated",
                    "replicas": 1,
                    "port_policy": {"mode": "contiguous"},
                    "gpu_policy": {"mode": "one_per_instance"},
                },
                "advanced": {"env": {"PROFILE_TOKEN": "<redacted>"}},
            },
        )
        assert runtime_update["profile"]["restart_required"] is True
        assert "common" in runtime_update["profile"]["restart_required_fields"]
        assert [item["port"] for item in runtime_update["profile"]["instances"]] == [10000]
        assert [item["gpu_ids"] for item in runtime_update["profile"]["instances"]] == [[0]]
        assert not (ctx["unit_dir"] / "infra-llm-qwen@1.service").exists()
        assert "PROFILE_TOKEN=profile-secret" in (ctx["unit_dir"] / "infra-llm-qwen.service").read_text()


def test_validation_failure_does_not_write_registry_or_units(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _setup_launcher(tmp_path)
        try:
            inference_profiles.create_profile(_profile())
        except inference_profiles.ProfileValidationError as e:
            assert "blockers" in e.detail
        else:
            raise AssertionError("Expected validation error")
        registry = json.loads(ctx["profiles_file"].read_text())
        assert registry["profiles"] == {}
        assert list(ctx["unit_dir"].iterdir()) == []


def test_http_profile_api_create_list_render_delete(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        _setup_launcher(tmp_path)

        import auth
        import main

        async def auth_true(_request):
            return True

        async def scenario():
            with _Patch([(auth, "check_auth", auth_true)]):
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    created = await client.post("/api/inference/profiles", json=_profile())
                    overview = await client.get("/api/inference/overview")
                    listed = await client.get("/api/inference/profiles")
                    detail = await client.get("/api/inference/profiles/qwen")
                    rendered = await client.post("/api/inference/profiles/qwen/render")
                    exported = await client.get("/api/inference/profiles/qwen/export")
                    deleted = await client.delete("/api/inference/profiles/qwen")
                    return created, overview, listed, detail, rendered, exported, deleted

        created, overview, listed, detail, rendered, exported, deleted = _run(scenario())
        assert created.status_code == 201
        overview_body = overview.json()
        assert overview.status_code == 200
        assert overview_body["profiles"]["profiles"][0]["id"] == "qwen"
        assert overview_body["models"]["artifacts"][0]["id"] == "qwen"
        assert overview_body["launchers"]["launchers"][0]["id"] == "vllm-main"
        assert overview_body["operations"]["operations"] == []
        assert overview_body["system"]["gpus"][0]["name"] == "GPU 0"
        assert overview_body["partial_errors"] == {}
        assert listed.json()["profiles"][0]["id"] == "qwen"
        assert detail.json()["units"][0]["exists"] is True
        assert rendered.json()["valid_for_save"] is True
        assert exported.status_code == 200
        assert exported.json()["profile"]["id"] == "qwen"
        assert exported.json()["validation"]["valid_for_save"] is True
        assert "node-local" in exported.json()["warning"]
        assert "profile-secret" not in json.dumps(exported.json())
        assert "launcher-secret" not in json.dumps(exported.json())
        assert deleted.json()["deleted"] == "qwen"
        assert not (ctx["unit_dir"] / "infra-llm-qwen.service").exists()


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
    print("Running inference profile tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
