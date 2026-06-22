import asyncio
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference_launchers


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
def _temp_launchers(tmpdir: Path):
    config_dir = tmpdir / "config"
    patches = [
        (inference_launchers, "CONFIG_DIR", config_dir),
        (inference_launchers, "LAUNCHERS_FILE", config_dir / "inference_engine_launchers.json"),
        (inference_launchers, "INFERENCE_PROFILES_FILE", config_dir / "inference_profiles.json"),
    ]
    with _Patch(patches):
        inference_launchers.initialize_launcher_registry()
        yield config_dir


def _run(coro):
    return asyncio.run(coro)


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def _make_executable(path: Path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | 0o111)


def test_launcher_create_lists_redacted_env_and_argv_tokens(tmp_path: Path):
    with _temp_launchers(tmp_path):
        exe = tmp_path / "vllm"
        _make_executable(exe)
        launcher = inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vLLM main",
            engine="vllm",
            executable=str(exe),
            base_args=["serve"],
            env={"VLLM_USE_V1": "1", "API_TOKEN": "secret"},
        )
        assert launcher["id"] == "vllm-main"
        assert launcher["base_args"] == ["serve"]
        assert launcher["env"]["API_TOKEN"] == "<redacted>"
        assert launcher["command_preview"] == [str(exe), "serve"]

        raw = inference_launchers.get_launcher("vllm-main", include_secret_env=True)
        assert raw["env"]["API_TOKEN"] == "secret"

        listed = inference_launchers.list_launchers()
        assert listed["schema_version"] == 1
        assert listed["launchers"][0]["env_count"] == 2
        assert listed["launchers"][0]["redacted_env_keys"] == ["API_TOKEN", "VLLM_USE_V1"]


def test_launcher_validation_reports_missing_nonexec_and_valid(tmp_path: Path):
    with _temp_launchers(tmp_path):
        missing = tmp_path / "missing"
        inference_launchers.create_launcher(
            launcher_id="missing",
            display_name="missing",
            engine="llama-cpp",
            executable=str(missing),
        )
        result = inference_launchers.validate_launcher_path("missing")
        assert result["valid"] is False
        assert "does not exist" in result["errors"][0]

        nonexec = tmp_path / "python"
        nonexec.write_text("#!/bin/sh\n")
        inference_launchers.create_launcher(
            launcher_id="nonexec",
            display_name="nonexec",
            engine="sglang",
            executable=str(nonexec),
            base_args=["-m", "sglang.launch_server"],
        )
        result = inference_launchers.validate_launcher_path("nonexec")
        assert result["valid"] is False
        assert "not executable" in result["errors"][0]

        nonexec.chmod(nonexec.stat().st_mode | 0o111)
        result = inference_launchers.validate_launcher_path("nonexec")
        assert result["valid"] is True
        assert result["executable"]["executable"] is True


def test_launcher_runtime_validation_reports_probe_failure(tmp_path: Path):
    with _temp_launchers(tmp_path):
        exe = tmp_path / "vllm"
        exe.write_text("#!/bin/sh\necho 'HF_TOKEN=secret'\necho 'ImportError: libcudart.so.12' >&2\nexit 7\n")
        exe.chmod(exe.stat().st_mode | 0o111)
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vllm",
            engine="vllm",
            executable=str(exe),
            base_args=["serve"],
        )

        result = _run(inference_launchers.validate_launcher_runtime("vllm-main", timeout=2))

        assert result["valid"] is False
        assert result["runtime"]["checked"] is True
        assert result["runtime"]["code"] == 7
        assert result["runtime"]["command_preview"] == [str(exe), "serve", "--help"]
        assert "Runtime probe exited with code 7" in result["errors"]
        assert "libcudart.so.12" in result["runtime"]["output"]
        assert "HF_TOKEN=<redacted>" in result["runtime"]["output"]


def test_launcher_runtime_validation_suggests_venv_library_path(tmp_path: Path):
    with _temp_launchers(tmp_path):
        venv = tmp_path / "venv"
        exe = venv / "bin" / "vllm"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\necho 'ImportError: libcudart.so.12: cannot open shared object file' >&2\nexit 7\n")
        exe.chmod(exe.stat().st_mode | 0o111)
        cuda_lib = venv / "lib" / "python3.12" / "site-packages" / "nvidia" / "cuda_runtime" / "lib"
        cuda_lib.mkdir(parents=True)
        (cuda_lib / "libcudart.so.12").write_text("")
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vllm",
            engine="vllm",
            executable=str(exe),
            base_args=["serve"],
        )

        result = _run(inference_launchers.validate_launcher_runtime("vllm-main", timeout=2))

        assert result["valid"] is False
        assert result["runtime"]["suggested_env"]["LD_LIBRARY_PATH"] == str(cuda_lib)
        assert any("suggested launcher env" in error for error in result["errors"])


def test_launcher_env_merge_preserves_existing_env(tmp_path: Path):
    with _temp_launchers(tmp_path):
        exe = tmp_path / "vllm"
        _make_executable(exe)
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vllm",
            engine="vllm",
            executable=str(exe),
            env={"TOKEN": "hidden", "VLLM_USE_V1": "1"},
        )

        merged = inference_launchers.merge_launcher_env(
            "vllm-main",
            {"LD_LIBRARY_PATH": "/opt/cuda/lib", "VLLM_USE_V1": "0"},
        )
        raw = inference_launchers.get_launcher("vllm-main", include_secret_env=True)

        assert merged["env"]["TOKEN"] == "<redacted>"
        assert merged["env"]["LD_LIBRARY_PATH"] == "<redacted>"
        assert raw["env"] == {
            "TOKEN": "hidden",
            "VLLM_USE_V1": "0",
            "LD_LIBRARY_PATH": "/opt/cuda/lib",
        }


def test_launcher_update_and_delete_reference_checks(tmp_path: Path):
    with _temp_launchers(tmp_path) as config_dir:
        exe = tmp_path / "llama-server"
        _make_executable(exe)
        inference_launchers.create_launcher(
            launcher_id="llama-main",
            display_name="llama",
            engine="llama.cpp",
            executable=str(exe),
        )
        updated = inference_launchers.update_launcher(
            "llama-main",
            {"display_name": "llama prod", "base_args": ["--log-disable"]},
        )
        assert updated["display_name"] == "llama prod"
        assert updated["base_args"] == ["--log-disable"]

        profiles = {
            "profiles": {
                "p1": {
                    "state": "stopped",
                    "engine_launcher_id": "llama-main",
                }
            }
        }
        (config_dir / "inference_profiles.json").write_text(json.dumps(profiles))
        exc = _assert_raises(inference_launchers.LauncherConflictError, inference_launchers.delete_launcher, "llama-main")
        assert "Stopped profiles reference" in str(exc)

        deleted = inference_launchers.delete_launcher("llama-main", force_stopped_references=True)
        assert deleted["deleted"] == "llama-main"
        assert inference_launchers.list_launchers()["launchers"] == []


def test_launcher_delete_blocks_running_profile_reference(tmp_path: Path):
    with _temp_launchers(tmp_path) as config_dir:
        exe = tmp_path / "vllm"
        _make_executable(exe)
        inference_launchers.create_launcher(
            launcher_id="vllm-main",
            display_name="vllm",
            engine="vllm",
            executable=str(exe),
        )
        (config_dir / "inference_profiles.json").write_text(
            json.dumps({"profiles": {"p1": {"status": "running", "engine_launcher": {"id": "vllm-main"}}}})
        )
        exc = _assert_raises(inference_launchers.LauncherConflictError, inference_launchers.delete_launcher, "vllm-main")
        assert "Running profiles reference" in str(exc)
        assert inference_launchers.get_launcher("vllm-main")["id"] == "vllm-main"


def test_launcher_http_api_create_validate_update_delete(tmp_path: Path):
    with _temp_launchers(tmp_path):
        import auth
        import main

        exe = tmp_path / "python"
        _make_executable(exe)

        async def auth_true(_request):
            return True

        async def scenario():
            with _Patch([(auth, "check_auth", auth_true)]):
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    created = await client.post(
                        "/api/inference/launchers",
                        json={
                            "id": "sglang-python",
                            "display_name": "SGLang",
                            "engine": "sglang",
                            "executable": str(exe),
                            "base_args": ["-m", "sglang.launch_server"],
                            "env": {"TOKEN": "hidden"},
                        },
                    )
                    assert created.status_code == 201
                    assert created.json()["env"]["TOKEN"] == "<redacted>"

                    listed = await client.get("/api/inference/launchers")
                    assert listed.status_code == 200
                    assert listed.json()["launchers"][0]["id"] == "sglang-python"

                    validated = await client.post("/api/inference/launchers/sglang-python/validate")
                    assert validated.status_code == 200
                    assert validated.json()["valid"] is True
                    assert validated.json()["runtime"]["checked"] is True

                    updated = await client.put(
                        "/api/inference/launchers/sglang-python",
                        json={"display_name": "SGLang prod", "base_args": ["-m", "sglang.launch_server", "--quiet"]},
                    )
                    assert updated.status_code == 200
                    assert updated.json()["display_name"] == "SGLang prod"
                    assert updated.json()["base_args"][-1] == "--quiet"

                    merged = await client.post(
                        "/api/inference/launchers/sglang-python/env",
                        json={"env": {"LD_LIBRARY_PATH": "/opt/cuda/lib"}},
                    )
                    assert merged.status_code == 200
                    assert merged.json()["env"]["LD_LIBRARY_PATH"] == "<redacted>"
                    raw = inference_launchers.get_launcher("sglang-python", include_secret_env=True)
                    assert raw["env"]["TOKEN"] == "hidden"
                    assert raw["env"]["LD_LIBRARY_PATH"] == "/opt/cuda/lib"

                    deleted = await client.delete("/api/inference/launchers/sglang-python")
                    assert deleted.status_code == 200
                    assert deleted.json()["deleted"] == "sglang-python"

        _run(scenario())


def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for test in tests:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                test(Path(tmpdir))
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    print("Running inference launcher tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
