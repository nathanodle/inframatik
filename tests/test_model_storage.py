import asyncio
import functools
import http.server
import inspect
import json
import sys
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import model_storage
import node_config


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
def _temp_storage(tmpdir: Path):
    config_dir = tmpdir / "config"
    store = tmpdir / "models"
    config_file = config_dir / "node.json"
    patches = [
        (model_storage, "CONFIG_DIR", config_dir),
        (model_storage, "MODELS_FILE", config_dir / "models.json"),
        (model_storage, "MODEL_JOBS_FILE", config_dir / "model_jobs.json"),
        (model_storage, "INFERENCE_PROFILES_FILE", config_dir / "inference_profiles.json"),
        (model_storage, "DEFAULT_MODEL_STORE_ROOT", store),
        (node_config, "CONFIG_FILE", config_file),
    ]
    with _Patch(patches):
        node_config.invalidate_cache()
        node_config.save_node_config(
            {
                "role": "standalone",
                "node_id": "node-a",
                "node_name": "node-a",
                "model_store_root": str(store),
                "model_import_allowlist_roots": [str(tmpdir)],
                "model_download_allow_private_networks": True,
                "model_max_download_bytes": 20 * 1024 * 1024,
            }
        )
        model_storage.initialize_model_storage()
        try:
            yield store
        finally:
            node_config.invalidate_cache()


async def _wait_for_job(job_id: str, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = await model_storage.get_job_status(job_id)
        if job["state"] in model_storage.TERMINAL_JOB_STATES:
            return job
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"Timed out waiting for job {job_id}: {job}")
        await asyncio.sleep(0.02)


def _run(coro):
    return asyncio.run(coro)


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def test_storage_root_update_writes_node_config_and_blocks_active_jobs(tmp_path: Path):
    with _temp_storage(tmp_path):
        new_root = tmp_path / "new-models"
        info = model_storage.update_storage_root(str(new_root))
        assert info["root"] == str(new_root.resolve())
        assert node_config.get_node_config()["model_store_root"] == str(new_root.resolve())

        jobs = {
            "schema_version": 1,
            "jobs": {
                "mdl_running": {
                    "id": "mdl_running",
                    "state": "running",
                    "staging_path": str(new_root / "staging" / "mdl_running"),
                }
            },
        }
        model_storage._save_jobs_registry(jobs)
        exc = _assert_raises(model_storage.ModelConflictError, model_storage.update_storage_root, str(tmp_path / "blocked"))
        assert "active jobs" in str(exc)


def test_local_import_copies_gguf_and_writes_manifest(tmp_path: Path):
    with _temp_storage(tmp_path):
        source = tmp_path / "Tiny Model.gguf"
        source.write_bytes(b"gguf test bytes")

        async def scenario():
            job = await model_storage.start_import_job(
                str(source),
                artifact_id="tiny-gguf",
                display_name="Tiny GGUF",
                snapshot="v1",
            )
            finished = await _wait_for_job(job["id"])
            assert finished["state"] == "ready"
            manifest = model_storage.get_manifest("tiny-gguf", "v1")
            assert manifest["schema_version"] == 1
            assert manifest["kind"] == "gguf"
            assert manifest["format"] == "gguf"
            assert manifest["files"][0]["path"] == "Tiny Model.gguf"
            assert len(manifest["files"][0]["sha256"]) == 64
            inventory = await model_storage.list_models()
            assert inventory["artifacts"][0]["id"] == "tiny-gguf"
            assert Path(inventory["artifacts"][0]["runtime_path"]).name == "Tiny Model.gguf"

        _run(scenario())


def test_interrupted_jobs_are_marked_failed_on_startup(tmp_path: Path):
    with _temp_storage(tmp_path):
        staging = tmp_path / "models" / "staging" / "mdl_a"
        staging.mkdir(parents=True)
        model_storage._save_jobs_registry(
            {
                "schema_version": 1,
                "jobs": {
                    "mdl_a": {"id": "mdl_a", "state": "hashing", "staging_path": str(staging)},
                    "mdl_b": {"id": "mdl_b", "state": "ready", "staging_path": str(tmp_path / "x")},
                },
            }
        )
        result = model_storage.mark_interrupted_jobs()
        assert result["interrupted"] == ["mdl_a"]
        job = _run(model_storage.get_job_status("mdl_a"))
        assert job["state"] == "failed_interrupted"
        assert staging.exists()


def test_verify_and_delete_enforce_manifest_and_profile_references(tmp_path: Path):
    with _temp_storage(tmp_path):
        source = tmp_path / "model.gguf"
        source.write_bytes(b"model bytes")

        async def import_model():
            job = await model_storage.start_import_job(str(source), artifact_id="ref-model", snapshot="v1")
            return await _wait_for_job(job["id"])

        _run(import_model())
        verify = model_storage.verify_artifact("ref-model", "v1")
        assert verify["valid"] is True

        model_storage.INFERENCE_PROFILES_FILE.write_text(
            json.dumps(
                {
                    "profiles": {
                        "profile-a": {
                            "state": "stopped",
                            "model": {"artifact_id": "ref-model", "snapshot": "v1"},
                        }
                    }
                }
            )
        )
        exc = _assert_raises(model_storage.ModelConflictError, model_storage.delete_artifact, "ref-model")
        assert "Stopped profiles reference" in str(exc)

        result = model_storage.delete_artifact("ref-model", force_stopped_references=True)
        assert result["deleted"] == "ref-model"
        inventory = _run(model_storage.list_models())
        assert inventory["artifacts"] == []


def test_direct_url_download_uses_staging_then_ready_artifact(tmp_path: Path):
    with _temp_storage(tmp_path):
        served = tmp_path / "served"
        served.mkdir()
        (served / "remote.gguf").write_bytes(b"remote gguf bytes")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/remote.gguf"

            async def scenario():
                job = await model_storage.start_download_job(
                    {"type": "url", "url": url},
                    artifact_id="remote-gguf",
                    snapshot="v1",
                )
                finished = await _wait_for_job(job["id"])
                assert finished["state"] == "ready"
                manifest = model_storage.get_manifest("remote-gguf", "v1")
                assert manifest["source"]["url"].startswith("http://127.0.0.1")
                assert manifest["files"][0]["path"] == "remote.gguf"
                assert Path(finished["staging_path"]).exists()
                final_path = Path(finished["manifest_path"]).parent / "remote.gguf"
                assert final_path.read_bytes() == b"remote gguf bytes"

            _run(scenario())
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_model_storage_http_api_import_verify_and_delete(tmp_path: Path):
    with _temp_storage(tmp_path):
        import auth
        import main

        source = tmp_path / "api-model.gguf"
        source.write_bytes(b"api model bytes")

        async def auth_true(_request):
            return True

        async def scenario():
            with _Patch([(auth, "check_auth", auth_true)]):
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    storage = await client.get("/api/models/storage")
                    assert storage.status_code == 200
                    assert storage.json()["root"] == str((tmp_path / "models").resolve())

                    new_root = tmp_path / "api-store"
                    updated = await client.put("/api/models/storage", json={"root": str(new_root)})
                    assert updated.status_code == 200
                    assert updated.json()["root"] == str(new_root.resolve())

                    started = await client.post(
                        "/api/models/import",
                        json={"path": str(source), "artifact_id": "api-model", "snapshot": "v1"},
                    )
                    assert started.status_code == 202
                    job_id = started.json()["id"]

                    deadline = asyncio.get_running_loop().time() + 5
                    while True:
                        job_resp = await client.get(f"/api/models/jobs/{job_id}")
                        assert job_resp.status_code == 200
                        job = job_resp.json()
                        if job["state"] in model_storage.TERMINAL_JOB_STATES:
                            break
                        assert asyncio.get_running_loop().time() <= deadline
                        await asyncio.sleep(0.02)
                    assert job["state"] == "ready"

                    verify = await client.post("/api/models/api-model/verify?snapshot=v1")
                    assert verify.status_code == 200
                    assert verify.json()["valid"] is True

                    deleted = await client.delete("/api/models/api-model")
                    assert deleted.status_code == 200
                    assert deleted.json()["deleted"] == "api-model"

        _run(scenario())


def test_url_safety_rejects_private_http_without_override():
    exc = _assert_raises(
        model_storage.ModelStorageError,
        model_storage.assert_url_allowed,
        "http://127.0.0.1:9000/model.gguf",
        allow_private=False,
    )
    assert "https" in str(exc)


def test_safe_archive_extraction_rejects_path_traversal(tmp_path: Path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "bad")
    dest = tmp_path / "dest"
    exc = _assert_raises(model_storage.ModelStorageError, model_storage.safe_extract_archive, archive, dest)
    assert "Unsafe file path" in str(exc)


def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for test in tests:
        try:
            if inspect.signature(test).parameters:
                with tempfile.TemporaryDirectory() as tmpdir:
                    test(Path(tmpdir))
            else:
                test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    print("Running model storage tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
