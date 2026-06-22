import asyncio
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference_launchers
import inference_planner
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
def _temp_inference(tmpdir: Path, cf_configured: bool = False):
    config_dir = tmpdir / "config"
    store = tmpdir / "models"
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
        (node_config, "CONFIG_FILE", config_file),
        (services, "SERVICES_FILE", config_dir / "services.json"),
        (services, "PORTS_ENV_FILE", config_dir / "ports.env"),
    ]
    with _Patch(patches):
        node_config.invalidate_cache()
        config = {
            "role": "standalone",
            "node_id": "node-a",
            "node_name": "node-a",
            "model_store_root": str(store),
            "port_ranges": {"inference": "10000-10010"},
        }
        if cf_configured:
            config.update({"cf_token": "tok", "cf_account_id": "acct", "cf_zone_id": "zone"})
        node_config.save_node_config(config)
        model_storage.initialize_model_storage()
        inference_launchers.initialize_launcher_registry()
        try:
            yield {"config_dir": config_dir, "store": store, "profiles_file": profiles_file}
        finally:
            node_config.invalidate_cache()


def _run(coro):
    return asyncio.run(coro)


def _make_executable(path: Path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | 0o111)


def _write_model(store: Path, artifact_id="qwen", snapshot="v1", kind="hf_snapshot", fmt="safetensors", file_name="model.safetensors"):
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
        "kind": kind,
        "format": fmt,
        "source": {"type": "test"},
        "files": [{"path": file_name, "size": payload.stat().st_size, "sha256": digest}],
        "metadata": {},
        "created_at": 1,
        "size_bytes": payload.stat().st_size,
    }
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    runtime_path = snapshot_dir / file_name if fmt == "gguf" else snapshot_dir
    registry = model_storage._load_registry()
    registry.setdefault("artifacts", {})[artifact_id] = {
        "id": artifact_id,
        "kind": kind,
        "format": fmt,
        "display_name": artifact_id,
        "active_snapshot": snapshot,
        "artifact_path": str(store / "artifacts" / artifact_id),
        "runtime_path": str(runtime_path),
        "snapshots": {
            snapshot: {
                "state": "ready",
                "manifest_path": str(manifest_path),
                "snapshot_path": str(snapshot_dir),
                "runtime_path": str(runtime_path),
                "size_bytes": payload.stat().st_size,
                "created_at": 1,
            }
        },
    }
    model_storage._save_registry(registry)
    return runtime_path


def _fake_gpus(count=4):
    return {
        "gpus": [
            {"index": index, "name": f"GPU {index}", "mem_total_mb": 49152, "mem_used_mb": 1024}
            for index in range(count)
        ]
    }


def test_vllm_preview_renders_argv_env_redaction_and_raw_args(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        runtime_path = _write_model(ctx["store"])
        exe = tmp_path / "vllm"
        _make_executable(exe)
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vLLM",
            engine="vllm",
            executable=str(exe),
            env={"VLLM_USE_V1": "1", "API_TOKEN": "secret"},
        )
        original_metrics = system.get_system_metrics
        system.get_system_metrics = lambda: _fake_gpus(4)
        try:
            result = inference_planner.preview_profile(
                {
                    "id": "qwen-main",
                    "engine": "vllm",
                    "engine_launcher_id": "vllm-main",
                    "model": {"artifact_id": "qwen", "snapshot": "v1"},
                    "common": {
                        "served_model_name": "qwen",
                        "context_length": 8192,
                        "dtype": "auto",
                        "quantization": "fp8",
                        "kv_cache_dtype": "fp8",
                        "kv_cache_memory_bytes": "40G",
                        "tensor_parallel": 2,
                        "pipeline_parallel": 1,
                        "data_parallel": 2,
                        "expert_parallel": 2,
                        "gpu_memory_utilization": 0.9,
                        "cpu_offload_gb": 4,
                        "max_concurrent_requests": 32,
                        "max_batch_tokens": 8192,
                        "enable_prefix_caching": True,
                        "reasoning_parser": "glm45",
                        "tool_call_parser": "glm47",
                        "enable_auto_tool_choice": True,
                        "speculative": {"model": "draft-model", "num_tokens": 5},
                        "lora": {"enabled": True, "paths": [{"name": "style", "path": "/models/style-lora"}]},
                        "host": "127.0.0.1",
                        "port": 10000,
                    },
                    "deployment": {"gpu_policy": {"mode": "profile", "gpu_ids": [0, 1]}},
                    "engine_config": {"vllm": {
                        "enable_expert_parallel": True,
                        "all2all_backend": "deepep_high_throughput",
                        "api_server_count": 2,
                        "data_parallel_backend": "ray",
                        "data_parallel_rank": 1,
                        "data_parallel_lb_mode": "external",
                        "decode_context_parallel_size": 2,
                        "prefill_context_parallel_size": 4,
                        "max_num_partial_prefills": 4,
                        "long_prefill_token_threshold": 32768,
                        "moe_backend": "auto",
                        "linear_backend": "auto",
                    }},
                    "advanced": {"args": ["--max-num-seqs", "16"], "env": {"OPENAI_API_KEY": "sk-test", "VISIBLE": "yes"}},
                }
            )
        finally:
            system.get_system_metrics = original_metrics

        assert result["valid_for_save"] is True
        command = result["command_preview"][0]
        assert command["argv"][:3] == [str(exe), "serve", str(runtime_path)]
        assert "--host" in command["argv"]
        assert command["argv"][-2:] == ["--max-num-seqs", "16"]
        assert command["env"]["CUDA_VISIBLE_DEVICES"] == "0,1"
        assert command["env"]["VISIBLE"] == "yes"
        assert command["env"]["API_TOKEN"] == "<redacted>"
        assert command["env"]["OPENAI_API_KEY"] == "<redacted>"
        assert "OPENAI_API_KEY" in command["redacted_env_keys"]
        assert "--enable-expert-parallel" in command["argv"]
        assert "--all2all-backend" in command["argv"]
        assert "--kv-cache-dtype" in command["argv"]
        assert "--kv-cache-memory-bytes" in command["argv"]
        assert "--data-parallel-size" in command["argv"]
        assert "--max-num-batched-tokens" in command["argv"]
        assert "--reasoning-parser" in command["argv"]
        assert "--tool-call-parser" in command["argv"]
        assert "--enable-auto-tool-choice" in command["argv"]
        assert "--speculative-model" in command["argv"]
        assert "--num-speculative-tokens" in command["argv"]
        assert "--enable-lora" in command["argv"]
        assert "--lora-modules" in command["argv"]
        assert command["argv"].count("--enable-expert-parallel") == 1
        assert "--api-server-count" in command["argv"]
        assert "--data-parallel-backend" in command["argv"]
        assert "--data-parallel-rank" in command["argv"]
        assert "--data-parallel-external-lb" in command["argv"]
        assert command["argv"][command["argv"].index("--decode-context-parallel-size") + 1] == "2"
        assert command["argv"][command["argv"].index("--prefill-context-parallel-size") + 1] == "4"
        assert "--max-num-partial-prefills" in command["argv"]
        assert "--long-prefill-token-threshold" in command["argv"]
        assert "--moe-backend" in command["argv"]
        assert "--linear-backend" in command["argv"]


def test_sglang_and_llama_command_renderers(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        hf_path = _write_model(ctx["store"], artifact_id="sg", file_name="config.json")
        gguf_path = _write_model(ctx["store"], artifact_id="llama", kind="gguf", fmt="gguf", file_name="tiny.gguf")
        py = tmp_path / "python"
        llama = tmp_path / "llama-server"
        _make_executable(py)
        _make_executable(llama)
        inference_launchers.create_launcher(
            launcher_id="sglang-python",
            display_name="SGLang",
            engine="sglang",
            executable=str(py),
            base_args=["-m", "sglang.launch_server"],
        )
        inference_launchers.create_launcher(
            launcher_id="llama-main",
            display_name="llama",
            engine="llama.cpp",
            executable=str(llama),
        )
        original_metrics = system.get_system_metrics
        system.get_system_metrics = lambda: _fake_gpus(2)
        try:
            sg = inference_planner.preview_profile(
                {
                    "id": "sg",
                    "engine": "sglang",
                    "engine_launcher_id": "sglang-python",
                    "model": {"artifact_id": "sg", "snapshot": "v1"},
                    "common": {
                        "context_length": 4096,
                        "tensor_parallel": 2,
                        "data_parallel": 2,
                        "max_prefill_tokens": 2048,
                        "max_queued_requests": 64,
                        "max_batch_tokens": 8192,
                        "expert_parallel": 4,
                        "enable_metrics": True,
                        "reasoning_parser": "deepseek-r1",
                        "tool_call_parser": "hermes",
                        "speculative": {"model": "draft", "num_tokens": 3},
                        "lora": {"paths": [{"name": "tools", "path": "/models/tools-lora"}]},
                        "port": 10001,
                    },
                    "engine_config": {"sglang": {
                        "ep_size": 2,
                        "enable_dp_attention": True,
                        "attn_cp_size": 2,
                        "chunked_prefill_size": 4096,
                        "moe_a2a_backend": "deepep",
                        "moe_runner_backend": "triton",
                        "hf_chat_template_name": "tool_use",
                        "dist_init_addr": "sgl-0:50000",
                        "nnodes": 2,
                        "node_rank": 1,
                    }},
                }
            )
            llama_result = inference_planner.preview_profile(
                {
                    "id": "llama",
                    "engine": "llama.cpp",
                    "engine_launcher_id": "llama-main",
                    "model": {"artifact_id": "llama", "snapshot": "v1"},
                    "common": {"context_length": 2048, "max_batch_tokens": 4096, "enable_metrics": True, "port": 10002},
                    "engine_config": {"llama_cpp": {
                        "n_gpu_layers": -1,
                        "threads": 8,
                        "threads_batch": 4,
                        "batch_size": 1024,
                        "tensor_split": [1, 1],
                        "cache_type_k": "q8_0",
                        "cache_type_v": "q8_0",
                        "flash_attention": True,
                    }},
                }
            )
        finally:
            system.get_system_metrics = original_metrics

        sg_argv = sg["command_preview"][0]["argv"]
        assert sg_argv[:5] == [str(py), "-m", "sglang.launch_server", "--model-path", str(hf_path)]
        assert "--tp-size" in sg_argv
        assert "--enable-dp-attention" in sg_argv
        assert "--dp-size" in sg_argv
        assert "--max-prefill-tokens" in sg_argv
        assert "--max-queued-requests" in sg_argv
        assert "--enable-metrics" in sg_argv
        assert "--chunked-prefill-size" in sg_argv
        assert "--moe-a2a-backend" in sg_argv
        assert "--moe-runner-backend" in sg_argv
        assert "--hf-chat-template-name" in sg_argv
        assert "--dist-init-addr" in sg_argv
        assert "--nnodes" in sg_argv
        assert "--node-rank" in sg_argv
        assert sg_argv.count("--ep-size") == 1
        assert sg_argv[sg_argv.index("--ep-size") + 1] == "2"
        assert "--speculative-draft-model-path" in sg_argv
        assert "--speculative-num-steps" in sg_argv
        assert "--enable-lora" in sg_argv
        assert "--lora-paths" in sg_argv

        llama_argv = llama_result["command_preview"][0]["argv"]
        assert llama_argv[:3] == [str(llama), "--model", str(gguf_path)]
        assert "--ctx-size" in llama_argv
        assert "--flash-attn" in llama_argv
        assert "--tensor-split" in llama_argv
        assert "--threads-batch" in llama_argv
        assert "--metrics" in llama_argv
        assert llama_argv.count("--batch-size") == 1
        assert llama_argv[llama_argv.index("--batch-size") + 1] == "1024"
        assert "--cache-type-k" in llama_argv
        assert "--cache-type-v" in llama_argv


def test_vllm_headless_dp_validation(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        exe = tmp_path / "vllm"
        _make_executable(exe)
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vLLM",
            engine="vllm",
            executable=str(exe),
        )

        valid = inference_planner.preview_profile(
            {
                "id": "vllm-headless",
                "engine": "vllm",
                "engine_launcher_id": "vllm-main",
                "model": {"artifact_id": "qwen", "snapshot": "v1"},
                "common": {"port": 10000, "data_parallel": 2},
                "engine_config": {"vllm": {
                    "headless": True,
                    "data_parallel_size_local": 2,
                    "data_parallel_backend": "mp",
                }},
            }
        )
        assert valid["valid_for_save"] is True
        argv = valid["command_preview"][0]["argv"]
        assert "--headless" in argv
        assert "--data-parallel-backend" in argv

        blocked = inference_planner.preview_profile(
            {
                "id": "vllm-headless-blocked",
                "engine": "vllm",
                "engine_launcher_id": "vllm-main",
                "model": {"artifact_id": "qwen", "snapshot": "v1"},
                "common": {"port": 10001, "data_parallel": 2},
                "engine_config": {"vllm": {
                    "headless": True,
                    "api_server_count": 1,
                    "data_parallel_lb_mode": "hybrid",
                }},
            }
        )
        assert blocked["valid_for_save"] is False
        blocker_fields = {item["field"] for item in blocked["blockers"]}
        assert "engine_config.vllm.api_server_count" in blocker_fields
        assert "engine_config.vllm.data_parallel_lb_mode" in blocker_fields


def test_planner_blocks_invalid_refs_ports_and_gpus(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        exe = tmp_path / "vllm"
        _make_executable(exe)
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vLLM",
            engine="vllm",
            executable=str(exe),
        )
        services._save_registry({"web": {"port": 10000}})
        original_metrics = system.get_system_metrics
        system.get_system_metrics = lambda: _fake_gpus(1)
        try:
            result = inference_planner.preview_profile(
                {
                    "id": "bad",
                    "engine": "vllm",
                    "engine_launcher_id": "vllm-main",
                    "model": {"artifact_id": "missing"},
                    "common": {"port": 10000},
                    "deployment": {"gpu_policy": {"mode": "profile", "gpu_ids": [2]}},
                }
            )
        finally:
            system.get_system_metrics = original_metrics

        messages = " ".join(item["message"] for item in result["blockers"])
        assert result["valid_for_save"] is False
        assert "Model artifact not found" in messages
        assert "Port 10000 is already allocated" in messages
        assert "Requested GPU IDs do not exist" in messages


def test_replicated_gpu_layout_and_gpu_claim_conflicts(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        exe = tmp_path / "vllm"
        _make_executable(exe)
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vLLM",
            engine="vllm",
            executable=str(exe),
        )
        original_metrics = system.get_system_metrics
        system.get_system_metrics = lambda: _fake_gpus(4)
        try:
            good = inference_planner.preview_profile(
                {
                    "id": "rep",
                    "engine": "vllm",
                    "engine_launcher_id": "vllm-main",
                    "model": {"artifact_id": "qwen", "snapshot": "v1"},
                    "deployment": {
                        "mode": "replicated",
                        "replicas": 3,
                        "port_policy": {"mode": "contiguous"},
                        "gpu_policy": {"mode": "one_per_instance", "gpu_ids": [0, 1, 2]},
                    },
                }
            )
            ctx["profiles_file"].write_text(
                json.dumps(
                    {
                        "profiles": {
                            "running": {
                                "state": "running",
                                "deployment": {"gpu_policy": {"claim_mode": "exclusive"}},
                                "instances": [{"index": 0, "gpu_ids": [0], "port": 10005}],
                            }
                        }
                    }
                )
            )
            conflict = inference_planner.preview_profile(
                {
                    "id": "conflict",
                    "engine": "vllm",
                    "engine_launcher_id": "vllm-main",
                    "model": {"artifact_id": "qwen", "snapshot": "v1"},
                    "deployment": {"gpu_policy": {"mode": "profile", "gpu_ids": [0], "claim_mode": "exclusive"}},
                }
            )
            ctx["profiles_file"].write_text(
                json.dumps(
                    {
                        "profiles": {
                            "running": {
                                "state": "running",
                                "deployment": {"gpu_policy": {"claim_mode": "shared"}},
                                "instances": [{"index": 0, "gpu_ids": [0], "port": 10005}],
                            }
                        }
                    }
                )
            )
            shared = inference_planner.preview_profile(
                {
                    "id": "shared",
                    "engine": "vllm",
                    "engine_launcher_id": "vllm-main",
                    "model": {"artifact_id": "qwen", "snapshot": "v1"},
                    "deployment": {"gpu_policy": {"mode": "profile", "gpu_ids": [0], "claim_mode": "shared"}},
                }
            )
        finally:
            system.get_system_metrics = original_metrics

        assert good["valid_for_save"] is True
        assert [item["port"] for item in good["resolved_instances"]] == [10000, 10001, 10002]
        assert [item["gpu_ids"] for item in good["resolved_instances"]] == [[0], [1], [2]]
        assert any("GPU overlap" in item["message"] for item in conflict["blockers"])
        assert any("GPU overlap" in item["message"] for item in shared["warnings"])
        assert not any("GPU overlap" in item["message"] for item in shared["blockers"])


def test_cloudflare_plan_and_api_preview_has_no_side_effects(tmp_path: Path):
    with _temp_inference(tmp_path, cf_configured=False) as ctx:
        _write_model(ctx["store"])
        exe = tmp_path / "vllm"
        _make_executable(exe)
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vLLM",
            engine="vllm",
            executable=str(exe),
        )
        original_metrics = system.get_system_metrics
        system.get_system_metrics = lambda: _fake_gpus(1)

        import auth
        import main

        async def auth_true(_request):
            return True

        async def scenario():
            with _Patch([(auth, "check_auth", auth_true)]):
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    response = await client.post(
                        "/api/inference/profiles/preview",
                        json={
                            "id": "cf",
                            "engine": "vllm",
                            "engine_launcher_id": "vllm-main",
                            "model": {"artifact_id": "qwen", "snapshot": "v1"},
                            "exposure": {"mode": "cloudflare", "hostname": "llm.example.com"},
                        },
                    )
                    return response

        try:
            assert not ctx["profiles_file"].exists()
            response = _run(scenario())
            assert not ctx["profiles_file"].exists()
        finally:
            system.get_system_metrics = original_metrics

        assert response.status_code == 200
        result = response.json()
        assert result["cloudflare_plan"]["would_provision"] is True
        assert any("Cloudflare exposure requires local Cloudflare configuration" in item["message"] for item in result["blockers"])


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
    print("Running inference planner tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
