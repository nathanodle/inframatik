import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse, urlunparse

import httpx

from node_config import get_node_config, save_node_config


CONFIG_DIR = Path.home() / ".config" / "inframatik"
DEFAULT_MODEL_STORE_ROOT = Path.home() / ".local" / "share" / "inframatik" / "models"
MODELS_FILE = CONFIG_DIR / "models.json"
MODEL_JOBS_FILE = CONFIG_DIR / "model_jobs.json"
INFERENCE_PROFILES_FILE = CONFIG_DIR / "inference_profiles.json"

SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
ACTIVE_JOB_STATES = {"queued", "running", "hashing", "verifying"}
TERMINAL_JOB_STATES = {"ready", "failed", "failed_interrupted", "canceled"}
DEFAULT_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024 * 1024

_ARTIFACT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,94}[a-z0-9])?$")
_SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_HF_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_SAFE_RELATIVE_RE = re.compile(r"^[^<>:\"|?*\x00-\x1f]+$")
_HASH_CHUNK = 1024 * 1024
_PERSIST_HASH_EVERY = 16 * 1024 * 1024

_json_lock = threading.RLock()
_job_tasks: dict[str, asyncio.Task] = {}
_job_cancel_events: dict[str, asyncio.Event] = {}
_job_secrets: dict[str, dict] = {}


class ModelStorageError(ValueError):
    status_code = 400

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class ModelNotFoundError(ModelStorageError):
    status_code = 404


class ModelConflictError(ModelStorageError):
    status_code = 409


def _now() -> int:
    return int(time.time())


def _atomic_write_json(path: Path, data: dict, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _empty_registry() -> dict:
    return {"schema_version": SCHEMA_VERSION, "artifacts": {}}


def _empty_jobs() -> dict:
    return {"schema_version": SCHEMA_VERSION, "jobs": {}}


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise ModelStorageError(f"Invalid JSON registry {path}: {e}") from e
    if not isinstance(data, dict):
        raise ModelStorageError(f"Invalid JSON registry {path}: expected object")
    return data


def _load_registry() -> dict:
    data = _load_json(MODELS_FILE, _empty_registry())
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("artifacts", {})
    return data


def _save_registry(data: dict):
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("artifacts", {})
    _atomic_write_json(MODELS_FILE, data)


def _load_jobs_registry() -> dict:
    data = _load_json(MODEL_JOBS_FILE, _empty_jobs())
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("jobs", {})
    return data


def _save_jobs_registry(data: dict):
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("jobs", {})
    _atomic_write_json(MODEL_JOBS_FILE, data)


def _get_job(job_id: str) -> dict:
    with _json_lock:
        jobs = _load_jobs_registry()
        job = jobs["jobs"].get(job_id)
        if not job:
            raise ModelNotFoundError(f"Model job not found: {job_id}")
        return job


def _put_job(job: dict):
    with _json_lock:
        jobs = _load_jobs_registry()
        jobs["jobs"][job["id"]] = job
        _save_jobs_registry(jobs)
    _publish_job(job)


def _patch_job(job_id: str, **updates) -> dict:
    with _json_lock:
        jobs = _load_jobs_registry()
        job = jobs["jobs"].get(job_id)
        if not job:
            raise ModelNotFoundError(f"Model job not found: {job_id}")
        job.update(updates)
        jobs["jobs"][job_id] = job
        _save_jobs_registry(jobs)
        updated = dict(job)
    _publish_job(updated)
    return updated


def _publish_job(job: dict):
    try:
        from ws_routes import publish

        publish({"type": "model_job", "job": dict(job)})
    except Exception:
        pass


def _node_config() -> dict:
    return get_node_config() or {}


def get_model_store_root() -> Path:
    raw = _node_config().get("model_store_root")
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_MODEL_STORE_ROOT.expanduser().resolve()


def _staging_root(root: Optional[Path] = None) -> Path:
    return (root or get_model_store_root()) / "staging"


def _staging_path_for_cleanup(job: dict) -> Path:
    raw = job.get("staging_path")
    if not raw:
        raise ModelStorageError("Job has no staging path")
    staging_path = Path(raw)
    root = _staging_root(get_model_store_root())
    if not _path_is_relative_to(staging_path, root):
        recorded_root = staging_path.parent
        if recorded_root.name != "staging":
            raise ModelStorageError("Refusing to delete staging path outside an inframatik staging directory")
    return staging_path


def _remove_job_staging_files(job: dict, reason: str) -> tuple[dict, Path, bool]:
    staging_path = _staging_path_for_cleanup(job)
    existed = staging_path.exists()
    if existed:
        shutil.rmtree(staging_path)
    cleanup = dict(job.get("cleanup") or {})
    cleanup.update(
        {
            "staging_removed_at": _now(),
            "staging_removed_reason": reason,
            "staging_removed": True,
            "staging_existed": existed,
        }
    )
    return cleanup, staging_path, existed


def _artifact_dir(root: Path, artifact_id: str) -> Path:
    return root / "artifacts" / artifact_id


def _snapshot_dir(root: Path, artifact_id: str, snapshot: str) -> Path:
    return _artifact_dir(root, artifact_id) / "snapshots" / snapshot


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def validate_artifact_id(artifact_id: str) -> str:
    value = (artifact_id or "").strip().lower()
    if not _ARTIFACT_ID_RE.fullmatch(value):
        raise ModelStorageError(
            "Artifact ID must be lowercase alphanumeric with hyphens, start and end with alphanumeric, and be 96 characters or less"
        )
    return value


def validate_snapshot(snapshot: str) -> str:
    value = (snapshot or "").strip()
    if not _SNAPSHOT_RE.fullmatch(value):
        raise ModelStorageError("Snapshot must be 1-96 characters: letters, numbers, dots, underscores, or hyphens")
    return value


def slugify_artifact_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:96].strip("-")
    if not slug:
        slug = f"model-{_now()}"
    if len(slug) > 96:
        slug = slug[:96].strip("-")
    if not _ARTIFACT_ID_RE.fullmatch(slug):
        slug = f"model-{hashlib.sha256(value.encode()).hexdigest()[:12]}"
    return slug


def _default_snapshot() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime())


def _sanitize_filename(name: str, fallback: str = "download") -> str:
    candidate = Path(name or "").name.strip()
    candidate = re.sub(r"[\x00-\x1f/\\]+", "-", candidate).strip(". ")
    if not candidate:
        candidate = fallback
    return candidate[:180]


def _safe_relative_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ModelStorageError("File path cannot be empty")
    raw = raw.replace("\\", "/")
    if not _SAFE_RELATIVE_RE.fullmatch(raw):
        raise ModelStorageError(f"Unsafe file path: {raw}")
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise ModelStorageError(f"Unsafe file path: {raw}")
    return rel


def _get_import_allowlist_roots() -> list[Path]:
    roots = _node_config().get("model_import_allowlist_roots")
    if not roots:
        roots = [str(Path.home())]
    result = []
    for raw in roots:
        try:
            result.append(Path(raw).expanduser().resolve())
        except (TypeError, OSError):
            continue
    return result


def _get_max_download_bytes() -> int:
    raw = _node_config().get("model_max_download_bytes", DEFAULT_MAX_DOWNLOAD_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_DOWNLOAD_BYTES
    return max(1, value)


def _allow_private_download_urls() -> bool:
    return bool(_node_config().get("model_download_allow_private_networks"))


def _assert_import_allowed(path: Path):
    resolved = path.expanduser().resolve()
    roots = _get_import_allowlist_roots()
    if not roots or not any(_path_is_relative_to(resolved, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots) or "(none)"
        raise ModelStorageError(f"Import path must be under an allowed root: {allowed}")
    return resolved


def _active_jobs(jobs: Optional[dict] = None) -> list[dict]:
    jobs = jobs or _load_jobs_registry()
    return [
        job
        for job in jobs.get("jobs", {}).values()
        if job.get("state") in ACTIVE_JOB_STATES
    ]


def _ensure_no_active_jobs():
    jobs = _load_jobs_registry()
    active = _active_jobs(jobs)
    if active:
        ids = ", ".join(job["id"] for job in active)
        raise ModelConflictError(f"Model storage has active jobs: {ids}")


def initialize_model_storage():
    root = get_model_store_root()
    root.mkdir(parents=True, exist_ok=True)
    _staging_root(root).mkdir(parents=True, exist_ok=True)
    with _json_lock:
        if not MODELS_FILE.exists():
            _save_registry(_empty_registry())
        if not MODEL_JOBS_FILE.exists():
            _save_jobs_registry(_empty_jobs())


def mark_interrupted_jobs() -> dict:
    with _json_lock:
        jobs = _load_jobs_registry()
        changed = False
        now = _now()
        interrupted = []
        for job in jobs.get("jobs", {}).values():
            if job.get("state") in ACTIVE_JOB_STATES:
                job["state"] = "failed_interrupted"
                job["finished_at"] = now
                job["error"] = "inframatik restarted before this model job completed"
                interrupted.append(job["id"])
                changed = True
        if changed:
            _save_jobs_registry(jobs)
        return {"interrupted": interrupted}


def _job_list() -> list[dict]:
    jobs = _load_jobs_registry()
    return sorted(jobs.get("jobs", {}).values(), key=lambda item: item.get("created_at", 0), reverse=True)


def _artifact_list(registry: Optional[dict] = None) -> list[dict]:
    registry = registry or _load_registry()
    root = get_model_store_root()
    result = []
    for artifact in registry.get("artifacts", {}).values():
        item = dict(artifact)
        active_snapshot = item.get("active_snapshot")
        active_meta = (item.get("snapshots") or {}).get(active_snapshot) if active_snapshot else None
        if active_meta:
            item["active_snapshot_state"] = active_meta.get("state")
            item["snapshot_path"] = active_meta.get("snapshot_path")
            try:
                manifest = json.loads(Path(active_meta.get("manifest_path", "")).read_text())
                item["source"] = manifest.get("source") or {}
                item["files_count"] = len(manifest.get("files") or [])
                item["manifest_display_name"] = manifest.get("display_name")
            except (json.JSONDecodeError, OSError, TypeError):
                item["source"] = {}
                item["files_count"] = None
        artifact_path = item.get("artifact_path")
        if artifact_path:
            path = Path(artifact_path).expanduser()
            item["current_root"] = _path_is_relative_to(path, root)
            item["path_exists"] = path.exists()
        else:
            item["current_root"] = True
            item["path_exists"] = False
        result.append(item)
    return sorted(result, key=lambda item: item.get("id", ""))


async def list_models() -> dict:
    initialize_model_storage()
    with _json_lock:
        registry = _load_registry()
        return {
            "schema_version": registry.get("schema_version", SCHEMA_VERSION),
            "store_root": str(get_model_store_root()),
            "registry_path": str(MODELS_FILE),
            "artifacts": _artifact_list(registry),
            "jobs": _job_list(),
        }


def get_storage_info() -> dict:
    initialize_model_storage()
    root = get_model_store_root()
    disk_root = root if root.exists() else root.parent
    disk = None
    try:
        usage = shutil.disk_usage(disk_root)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        disk = None
    jobs = _load_jobs_registry()
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "default_root": str(DEFAULT_MODEL_STORE_ROOT),
        "registry_path": str(MODELS_FILE),
        "jobs_path": str(MODEL_JOBS_FILE),
        "active_jobs": _active_jobs(jobs),
        "allowlist_roots": [str(root) for root in _get_import_allowlist_roots()],
        "max_download_bytes": _get_max_download_bytes(),
        "disk": disk,
    }


def update_storage_root(root: str) -> dict:
    if not root or not str(root).strip():
        raise ModelStorageError("Model store root is required")
    with _json_lock:
        _ensure_no_active_jobs()
        new_root = Path(root).expanduser().resolve()
        new_root.mkdir(parents=True, exist_ok=True)
        (new_root / "artifacts").mkdir(parents=True, exist_ok=True)
        (new_root / "staging").mkdir(parents=True, exist_ok=True)
        config = get_node_config() or {}
        config["model_store_root"] = str(new_root)
        save_node_config(config)
    return get_storage_info()


def _new_job_id(prefix: str = "mdl") -> str:
    return f"{prefix}_{hashlib.sha256(os.urandom(32)).hexdigest()[:16]}"


def _new_job(kind: str, artifact_id: str, snapshot: str, source: dict, display_name: Optional[str]) -> dict:
    root = get_model_store_root()
    job_id = _new_job_id("mdl")
    staging_path = _staging_root(root) / job_id
    return {
        "id": job_id,
        "kind": kind,
        "artifact_id": artifact_id,
        "snapshot": snapshot,
        "display_name": display_name,
        "source": source,
        "state": "queued",
        "progress": 0.0,
        "current_file": None,
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "hashed_bytes": 0,
        "hash_total_bytes": 0,
        "staging_path": str(staging_path),
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "error": None,
    }


def _assert_artifact_snapshot_available(artifact_id: str, snapshot: str):
    registry = _load_registry()
    artifact = registry.get("artifacts", {}).get(artifact_id)
    if artifact and snapshot in artifact.get("snapshots", {}):
        raise ModelConflictError(f"Artifact snapshot already exists: {artifact_id}@{snapshot}")
    root = get_model_store_root()
    if _snapshot_dir(root, artifact_id, snapshot).exists():
        raise ModelConflictError(f"Artifact snapshot path already exists: {artifact_id}@{snapshot}")


def _spawn_job(job: dict, runner):
    cancel_event = asyncio.Event()
    _job_cancel_events[job["id"]] = cancel_event
    task = asyncio.create_task(runner(job["id"], cancel_event))
    _job_tasks[job["id"]] = task

    def _cleanup(_task):
        _job_tasks.pop(job["id"], None)
        _job_cancel_events.pop(job["id"], None)
        _job_secrets.pop(job["id"], None)

    task.add_done_callback(_cleanup)


async def start_import_job(
    path: str,
    artifact_id: Optional[str] = None,
    display_name: Optional[str] = None,
    snapshot: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    initialize_model_storage()
    source_path = _assert_import_allowed(Path(path))
    if not source_path.exists():
        raise ModelStorageError(f"Import path does not exist: {source_path}")
    if source_path.is_symlink():
        raise ModelStorageError("Import path cannot be a symlink")
    resolved_artifact_id = validate_artifact_id(artifact_id) if artifact_id else slugify_artifact_id(source_path.stem or source_path.name)
    resolved_snapshot = validate_snapshot(snapshot) if snapshot else _default_snapshot()
    with _json_lock:
        _assert_artifact_snapshot_available(resolved_artifact_id, resolved_snapshot)
        job = _new_job(
            "import",
            resolved_artifact_id,
            resolved_snapshot,
            {"type": "local", "path": str(source_path)},
            display_name,
        )
        job["request"] = {
            "path": str(source_path),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        _put_job(job)
    _spawn_job(job, _run_import_job)
    return job


async def start_download_job(
    source: dict,
    artifact_id: Optional[str] = None,
    display_name: Optional[str] = None,
    snapshot: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    initialize_model_storage()
    if not isinstance(source, dict):
        raise ModelStorageError("Download source is required")
    source_type = (source.get("type") or "").strip().lower()
    if source_type not in {"url", "huggingface"}:
        raise ModelStorageError("Download source type must be 'url' or 'huggingface'")

    secrets = {}
    if source_type == "huggingface":
        repo = _validate_hf_repo(source.get("repo"))
        base_name = repo.split("/")[-1]
        token = source.get("token") or os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
        if token:
            secrets["hf_token"] = token
        persisted_source = _sanitize_hf_source(source)
    else:
        url = str(source.get("url") or "")
        assert_url_allowed(url, allow_private=_allow_private_download_urls())
        base_name = Path(urlparse(url).path).stem or "downloaded-model"
        persisted_source = _sanitize_url_source(source)
        secrets["download_url"] = url

    resolved_artifact_id = validate_artifact_id(artifact_id) if artifact_id else slugify_artifact_id(base_name)
    resolved_snapshot = validate_snapshot(snapshot) if snapshot else _default_snapshot()
    with _json_lock:
        _assert_artifact_snapshot_available(resolved_artifact_id, resolved_snapshot)
        job = _new_job("download", resolved_artifact_id, resolved_snapshot, persisted_source, display_name)
        job["request"] = {
            "source": persisted_source,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        _put_job(job)
        if secrets:
            _job_secrets[job["id"]] = secrets
    _spawn_job(job, _run_download_job)
    return job


async def get_job_status(job_id: str) -> dict:
    return _get_job(job_id)


async def cancel_job(job_id: str) -> dict:
    job = _get_job(job_id)
    if job.get("state") not in ACTIVE_JOB_STATES:
        return job
    cancel_event = _job_cancel_events.get(job_id)
    if cancel_event:
        cancel_event.set()
    task = _job_tasks.get(job_id)
    if task:
        task.cancel()
    return _patch_job(job_id, state="canceled", finished_at=_now(), error="canceled by user")


async def clean_job_staging(job_id: str) -> dict:
    job = _get_job(job_id)
    if job.get("state") in ACTIVE_JOB_STATES:
        raise ModelConflictError("Cannot clean staging for an active model job")
    cleanup, staging_path, removed = _remove_job_staging_files(job, "manual")
    job["cleanup"] = cleanup
    _put_job(job)
    return {"job_id": job_id, "staging_path": str(staging_path), "removed": removed}


def _check_canceled(cancel_event: asyncio.Event):
    if cancel_event.is_set():
        raise asyncio.CancelledError()


async def _run_import_job(job_id: str, cancel_event: asyncio.Event):
    try:
        job = _patch_job(job_id, state="running", started_at=_now(), progress=1.0)
        staging = Path(job["staging_path"])
        payload = staging / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        request = job.get("request", {})
        source_path = Path(request["path"])
        _check_canceled(cancel_event)
        copied_bytes = await asyncio.to_thread(_copy_local_source, source_path, payload, job_id, cancel_event)
        _check_canceled(cancel_event)
        source = {"type": "local", "path": str(source_path)}
        await _hash_and_commit_payload(
            job_id=job_id,
            payload=payload,
            source=source,
            display_name=job.get("display_name"),
            metadata=request.get("metadata") if isinstance(request.get("metadata"), dict) else {},
            preferred_kind=None,
            preferred_format=None,
            total_bytes=copied_bytes,
            cancel_event=cancel_event,
        )
    except asyncio.CancelledError:
        _patch_job(job_id, state="canceled", finished_at=_now(), error="canceled by user")
    except Exception as e:
        _patch_job(job_id, state="failed", finished_at=_now(), error=str(e))


async def _run_download_job(job_id: str, cancel_event: asyncio.Event):
    try:
        job = _patch_job(job_id, state="running", started_at=_now(), progress=1.0)
        source = job.get("source") or {}
        staging = Path(job["staging_path"])
        payload = staging / "payload"
        downloads = staging / "downloads"
        payload.mkdir(parents=True, exist_ok=True)
        downloads.mkdir(parents=True, exist_ok=True)
        if source.get("type") == "url":
            total = await _download_direct_url(job_id, source, downloads, payload, cancel_event)
            preferred_kind = "url_archive" if source.get("extract") else "url_file"
        elif source.get("type") == "huggingface":
            total = await _download_huggingface(job_id, source, payload, cancel_event)
            preferred_kind = "hf_snapshot"
        else:
            raise ModelStorageError("Unsupported download source")
        _check_canceled(cancel_event)
        request = job.get("request", {})
        await _hash_and_commit_payload(
            job_id=job_id,
            payload=payload,
            source=source,
            display_name=job.get("display_name"),
            metadata=request.get("metadata") if isinstance(request.get("metadata"), dict) else {},
            preferred_kind=preferred_kind,
            preferred_format=None,
            total_bytes=total,
            cancel_event=cancel_event,
        )
    except asyncio.CancelledError:
        _patch_job(job_id, state="canceled", finished_at=_now(), error="canceled by user")
    except Exception as e:
        _patch_job(job_id, state="failed", finished_at=_now(), error=str(e))


def _copy_local_source(source: Path, payload: Path, job_id: str, cancel_event: asyncio.Event) -> int:
    source = source.resolve()
    if source.is_file():
        total = source.stat().st_size
        dest = payload / source.name
        _copy_file_with_progress(source, dest, job_id, total, 0, cancel_event)
        return total
    if not source.is_dir():
        raise ModelStorageError("Import path must be a file or directory")
    files = _scan_local_import_files(source)
    total = sum(path.stat().st_size for path in files)
    copied = 0
    _patch_job(job_id, total_bytes=total)
    for file_path in files:
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        rel = file_path.relative_to(source)
        dest = payload / rel
        _copy_file_with_progress(file_path, dest, job_id, total, copied, cancel_event)
        copied += file_path.stat().st_size
    return total


def _scan_local_import_files(source: Path) -> list[Path]:
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ModelStorageError(f"Import contains symlink, which is not allowed: {path}")
        if path.is_file():
            files.append(path)
    if not files:
        raise ModelStorageError("Import directory contains no files")
    return files


def _copy_file_with_progress(
    source: Path,
    dest: Path,
    job_id: str,
    total: int,
    offset: int,
    cancel_event: asyncio.Event,
):
    dest.parent.mkdir(parents=True, exist_ok=True)
    _patch_job(job_id, current_file=str(source.name), total_bytes=total)
    copied = offset
    with source.open("rb") as src, dest.open("wb") as out:
        while True:
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            chunk = src.read(_HASH_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            copied += len(chunk)
        out.flush()
        os.fsync(out.fileno())
    progress = 5.0 + (45.0 * copied / total) if total else 50.0
    _patch_job(job_id, downloaded_bytes=copied, progress=round(progress, 2))


def _sanitize_url_for_manifest(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _sanitize_url_source(source: dict) -> dict:
    url = str(source.get("url") or "")
    filename = source.get("filename") or Path(urlparse(url).path).name or None
    sanitized = {
        "type": "url",
        "url": _sanitize_url_for_manifest(url),
        "filename": _sanitize_filename(filename or "download"),
        "sha256": (source.get("sha256") or "").strip() or None,
        "extract": bool(source.get("extract")),
    }
    if source.get("expected_filename"):
        sanitized["expected_filename"] = _sanitize_filename(str(source.get("expected_filename")))
    return sanitized


def _validate_hf_repo(repo: str) -> str:
    value = (repo or "").strip()
    if not _HF_REPO_RE.fullmatch(value):
        raise ModelStorageError("Hugging Face repo must look like 'namespace/model' or 'model'")
    return value


def _sanitize_hf_source(source: dict) -> dict:
    repo = _validate_hf_repo(source.get("repo"))
    revision = (source.get("revision") or "main").strip()
    if not revision or any(ch in revision for ch in "\x00\r\n"):
        raise ModelStorageError("Invalid Hugging Face revision")
    files = source.get("files")
    clean_files = None
    if files is not None:
        if not isinstance(files, list) or not files:
            raise ModelStorageError("Hugging Face files must be a non-empty list when provided")
        clean_files = [str(_safe_relative_path(str(item))) for item in files]
    return {
        "type": "huggingface",
        "repo": repo,
        "revision": revision,
        "preset": (source.get("preset") or "full").strip().lower(),
        "files": clean_files,
    }


def assert_url_allowed(url: str, allow_private: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise ModelStorageError("Download URL must use https")
    if parsed.scheme == "http" and not allow_private:
        raise ModelStorageError("Download URL must use https")
    if parsed.username or parsed.password:
        raise ModelStorageError("Download URL must not include embedded credentials")
    if not parsed.hostname:
        raise ModelStorageError("Download URL must include a hostname")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as e:
        raise ModelStorageError(f"Could not resolve download host: {e}") from e
    addresses = {info[4][0] for info in infos}
    for addr in addresses:
        try:
            ip = _ip_address(addr)
        except ValueError as e:
            raise ModelStorageError(f"Invalid resolved download address: {addr}") from e
        if not allow_private and (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ModelStorageError("Download URL resolves to a private or unsafe network address")
    return url


def _ip_address(addr: str):
    import ipaddress

    return ipaddress.ip_address(addr)


async def _download_direct_url(
    job_id: str,
    source: dict,
    downloads: Path,
    payload: Path,
    cancel_event: asyncio.Event,
) -> int:
    url = _job_secrets.get(job_id, {}).get("download_url") or source["url"]
    assert_url_allowed(url, allow_private=_allow_private_download_urls())
    max_bytes = _get_max_download_bytes()
    filename = _sanitize_filename(source.get("expected_filename") or source.get("filename") or Path(urlparse(url).path).name)
    download_path = downloads / filename
    headers = {}
    total = 0
    timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            assert_url_allowed(str(resp.url), allow_private=_allow_private_download_urls())
            content_length = resp.headers.get("content-length")
            if content_length:
                try:
                    total = int(content_length)
                except ValueError:
                    total = 0
                if total > max_bytes:
                    raise ModelStorageError("Download exceeds configured maximum size")
            _patch_job(job_id, total_bytes=total, current_file=filename)
            downloaded = 0
            with download_path.open("wb") as out:
                async for chunk in resp.aiter_bytes():
                    _check_canceled(cancel_event)
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise ModelStorageError("Download exceeds configured maximum size")
                    out.write(chunk)
                    if downloaded % _PERSIST_HASH_EVERY < len(chunk):
                        progress = 5.0 + (45.0 * downloaded / total) if total else 10.0
                        _patch_job(
                            job_id,
                            downloaded_bytes=downloaded,
                            total_bytes=total,
                            progress=round(min(progress, 50.0), 2),
                        )
                out.flush()
                os.fsync(out.fileno())
    _patch_job(job_id, downloaded_bytes=downloaded, total_bytes=total or downloaded, progress=50.0)
    if source.get("sha256"):
        actual = _sha256_file(download_path)
        if actual != source["sha256"]:
            raise ModelStorageError("Downloaded file SHA-256 does not match the expected checksum")
    if _should_extract_archive(filename, bool(source.get("extract"))):
        await asyncio.to_thread(safe_extract_archive, download_path, payload, max_bytes=max_bytes)
        source["extract"] = True
        return sum(path.stat().st_size for path in _iter_files(payload))
    shutil.copy2(download_path, payload / filename)
    return download_path.stat().st_size


def _should_extract_archive(filename: str, requested: bool) -> bool:
    if requested:
        return True
    lower = filename.lower()
    return lower.endswith((".zip", ".tar", ".tar.gz", ".tgz"))


async def _download_huggingface(job_id: str, source: dict, payload: Path, cancel_event: asyncio.Event) -> int:
    repo = _validate_hf_repo(source.get("repo"))
    revision = source.get("revision") or "main"
    files = source.get("files")
    token = _job_secrets.get(job_id, {}).get("hf_token")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if not files:
        files = await _resolve_hf_files(repo, revision, source.get("preset") or "full", headers)
        source["files"] = files
    if not files:
        raise ModelStorageError("No Hugging Face files selected for download")

    quoted_repo = quote(repo, safe="/")
    quoted_revision = quote(revision, safe="")
    total = 0
    downloaded = 0
    timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        for rel_raw in files:
            _check_canceled(cancel_event)
            rel = _safe_relative_path(str(rel_raw))
            dest = payload / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://huggingface.co/{quoted_repo}/resolve/{quoted_revision}/{quote(str(rel), safe='/')}"
            _patch_job(job_id, current_file=str(rel), downloaded_bytes=downloaded, total_bytes=total)
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                length = resp.headers.get("content-length")
                if length:
                    try:
                        total += int(length)
                    except ValueError:
                        pass
                with dest.open("wb") as out:
                    async for chunk in resp.aiter_bytes():
                        _check_canceled(cancel_event)
                        downloaded += len(chunk)
                        if downloaded > _get_max_download_bytes():
                            raise ModelStorageError("Download exceeds configured maximum size")
                        out.write(chunk)
                        if downloaded % _PERSIST_HASH_EVERY < len(chunk):
                            progress = 5.0 + (45.0 * downloaded / total) if total else 10.0
                            _patch_job(
                                job_id,
                                downloaded_bytes=downloaded,
                                total_bytes=total,
                                progress=round(min(progress, 50.0), 2),
                            )
                    out.flush()
                    os.fsync(out.fileno())
    _patch_job(job_id, downloaded_bytes=downloaded, total_bytes=total or downloaded, progress=50.0)
    return downloaded


async def _resolve_hf_files(repo: str, revision: str, preset: str, headers: dict) -> list[str]:
    quoted_repo = quote(repo, safe="/")
    quoted_revision = quote(revision, safe="")
    url = f"https://huggingface.co/api/models/{quoted_repo}/tree/{quoted_revision}?recursive=1"
    timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list):
        raise ModelStorageError("Unexpected Hugging Face file listing response")
    files = [item.get("path") for item in data if isinstance(item, dict) and item.get("type") == "file" and item.get("path")]
    preset = (preset or "full").lower()
    if preset in {"safetensors", "safetensors_only"}:
        return [path for path in files if _is_hf_companion(path) or path.endswith(".safetensors")]
    if preset in {"tokenizer", "tokenizer_config", "config"}:
        return [path for path in files if _is_hf_companion(path) and not _is_weight_file(path)]
    if preset in {"gguf", "gguf_file"}:
        return [path for path in files if path.endswith(".gguf")]
    return files


def _is_hf_companion(path: str) -> bool:
    lower = path.lower()
    companion_suffixes = (
        ".json",
        ".txt",
        ".model",
        ".tiktoken",
        ".py",
        ".md",
        ".jinja",
    )
    return lower.endswith(companion_suffixes)


def _is_weight_file(path: str) -> bool:
    lower = path.lower()
    return lower.endswith((".safetensors", ".bin", ".gguf", ".pt", ".pth"))


def _iter_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _hash_and_commit_payload(
    job_id: str,
    payload: Path,
    source: dict,
    display_name: Optional[str],
    metadata: dict,
    preferred_kind: Optional[str],
    preferred_format: Optional[str],
    total_bytes: int,
    cancel_event: asyncio.Event,
):
    files = _iter_files(payload)
    if not files:
        raise ModelStorageError("Model payload contains no files")
    hash_total = sum(path.stat().st_size for path in files)
    _patch_job(
        job_id,
        state="hashing",
        progress=50.0,
        current_file=None,
        hash_total_bytes=hash_total,
        hashed_bytes=0,
        total_bytes=total_bytes or hash_total,
    )
    manifest_files = []
    hashed = 0
    for path in files:
        _check_canceled(cancel_event)
        rel = path.relative_to(payload)
        digest, size = await asyncio.to_thread(_hash_file_with_progress, path, job_id, hash_total, hashed, cancel_event)
        hashed += size
        manifest_files.append({"path": str(rel), "size": size, "sha256": digest})
    kind, fmt = _detect_kind_format(manifest_files, source, preferred_kind, preferred_format)
    job = _get_job(job_id)
    root = get_model_store_root()
    artifact_id = job["artifact_id"]
    snapshot = job["snapshot"]
    final_snapshot = _snapshot_dir(root, artifact_id, snapshot)
    artifact_path = _artifact_dir(root, artifact_id)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "id": artifact_id,
        "snapshot": snapshot,
        "display_name": display_name or artifact_id,
        "kind": kind,
        "format": fmt,
        "source": source,
        "files": manifest_files,
        "metadata": metadata or {},
        "created_at": _now(),
        "size_bytes": sum(item["size"] for item in manifest_files),
    }
    _patch_job(job_id, state="verifying", progress=92.0, current_file=None)
    await asyncio.to_thread(_commit_payload, payload, final_snapshot, artifact_path, manifest)
    manifest_path = final_snapshot / MANIFEST_FILENAME
    runtime_path = _runtime_path_from_manifest(final_snapshot, manifest)
    cleanup = None
    cleanup_error = None
    try:
        job = _get_job(job_id)
        cleanup, _staging_path, _removed = _remove_job_staging_files(job, "completed")
    except Exception as e:
        cleanup_error = str(e)
    with _json_lock:
        registry = _load_registry()
        artifacts = registry.setdefault("artifacts", {})
        now = _now()
        artifact = artifacts.get(artifact_id) or {
            "id": artifact_id,
            "created_at": now,
            "snapshots": {},
        }
        artifact.update(
            {
                "kind": kind,
                "format": fmt,
                "display_name": display_name or artifact.get("display_name") or artifact_id,
                "active_snapshot": snapshot,
                "updated_at": now,
                "size_bytes": manifest["size_bytes"],
                "artifact_path": str(artifact_path),
                "runtime_path": str(runtime_path),
            }
        )
        artifact.setdefault("snapshots", {})[snapshot] = {
            "state": "ready",
            "manifest_path": str(manifest_path),
            "snapshot_path": str(final_snapshot),
            "runtime_path": str(runtime_path),
            "size_bytes": manifest["size_bytes"],
            "created_at": now,
        }
        artifacts[artifact_id] = artifact
        _save_registry(registry)
    _patch_job(
        job_id,
        state="ready",
        progress=100.0,
        current_file=None,
        hashed_bytes=hash_total,
        hash_total_bytes=hash_total,
        finished_at=_now(),
        error=None,
        manifest_path=str(manifest_path),
        cleanup=cleanup,
        cleanup_error=cleanup_error,
    )


def _hash_file_with_progress(
    path: Path,
    job_id: str,
    hash_total: int,
    hashed_before: int,
    cancel_event: asyncio.Event,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    since_persist = 0
    _patch_job(job_id, current_file=path.name)
    with path.open("rb") as f:
        while True:
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            since_persist += len(chunk)
            if since_persist >= _PERSIST_HASH_EVERY:
                hashed = hashed_before + size
                progress = 50.0 + (40.0 * hashed / hash_total) if hash_total else 90.0
                _patch_job(job_id, hashed_bytes=hashed, progress=round(min(progress, 90.0), 2))
                since_persist = 0
    hashed = hashed_before + size
    progress = 50.0 + (40.0 * hashed / hash_total) if hash_total else 90.0
    _patch_job(job_id, hashed_bytes=hashed, progress=round(min(progress, 90.0), 2))
    return digest.hexdigest(), size


def _commit_payload(payload: Path, final_snapshot: Path, artifact_path: Path, manifest: dict):
    if final_snapshot.exists():
        raise ModelConflictError(f"Artifact snapshot path already exists: {final_snapshot}")
    artifact_path.mkdir(parents=True, exist_ok=True)
    final_snapshot.parent.mkdir(parents=True, exist_ok=True)
    os.replace(payload, final_snapshot)
    _atomic_write_json(final_snapshot / MANIFEST_FILENAME, manifest)


def _detect_kind_format(
    files: list[dict],
    source: dict,
    preferred_kind: Optional[str],
    preferred_format: Optional[str],
) -> tuple[str, str]:
    paths = [item["path"].lower() for item in files]
    suffixes = {Path(path).suffix for path in paths}
    has_gguf = any(path.endswith(".gguf") for path in paths)
    has_safetensors = any(path.endswith(".safetensors") for path in paths)
    has_pytorch = any(path.endswith((".bin", ".pt", ".pth")) for path in paths)
    has_adapter = "adapter_config.json" in paths or any(path.endswith("adapter_model.safetensors") for path in paths)
    if preferred_format:
        fmt = preferred_format
    elif has_gguf and len(files) == 1:
        fmt = "gguf"
    elif has_gguf and all(path.endswith(".gguf") for path in paths):
        fmt = "gguf"
    elif has_safetensors and not has_pytorch:
        fmt = "safetensors"
    elif has_pytorch and not has_safetensors:
        fmt = "pytorch"
    elif len(suffixes) > 1 or (has_safetensors and has_pytorch):
        fmt = "mixed"
    else:
        fmt = "unknown"

    if preferred_kind == "url_archive":
        kind = "url_archive"
    elif has_adapter:
        kind = "adapter"
    elif has_gguf and (len(files) == 1 or preferred_kind == "url_file"):
        kind = "gguf"
    elif preferred_kind == "hf_snapshot" or source.get("type") == "huggingface":
        kind = "hf_snapshot"
    elif preferred_kind == "url_file":
        kind = "url_file"
    elif source.get("type") == "local" and len(files) == 1 and has_gguf:
        kind = "gguf"
    elif source.get("type") == "local":
        kind = "local_dir"
    else:
        kind = preferred_kind or "local_dir"
    return kind, fmt


def _runtime_path_from_manifest(snapshot_path: Path, manifest: dict) -> Path:
    files = manifest.get("files", [])
    if manifest.get("format") == "gguf":
        gguf_files = [item["path"] for item in files if str(item.get("path", "")).lower().endswith(".gguf")]
        if gguf_files:
            return snapshot_path / gguf_files[0]
    if manifest.get("kind") in {"url_file"} and len(files) == 1:
        return snapshot_path / files[0]["path"]
    return snapshot_path


def safe_extract_archive(archive_path: Path, dest: Path, max_bytes: Optional[int] = None):
    archive_path = archive_path.resolve()
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    max_bytes = max_bytes or _get_max_download_bytes()
    lower = archive_path.name.lower()
    if lower.endswith(".zip"):
        _safe_extract_zip(archive_path, dest, max_bytes)
    elif lower.endswith((".tar", ".tar.gz", ".tgz")):
        _safe_extract_tar(archive_path, dest, max_bytes)
    else:
        raise ModelStorageError("Unsupported archive type")


def _safe_extract_zip(archive_path: Path, dest: Path, max_bytes: int):
    total = 0
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            rel = _safe_relative_path(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ModelStorageError("Archive contains a symlink, which is not allowed")
            if info.is_dir():
                (dest / rel).mkdir(parents=True, exist_ok=True)
                continue
            total += info.file_size
            if total > max_bytes:
                raise ModelStorageError("Extracted archive exceeds configured maximum size")
            target = (dest / rel).resolve()
            if not _path_is_relative_to(target, dest):
                raise ModelStorageError("Archive entry escapes destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _safe_extract_tar(archive_path: Path, dest: Path, max_bytes: int):
    total = 0
    with _open_tar(archive_path) as tf:
        for member in tf.getmembers():
            rel = _safe_relative_path(member.name)
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise ModelStorageError("Archive contains a link or device entry, which is not allowed")
            target = (dest / rel).resolve()
            if not _path_is_relative_to(target, dest):
                raise ModelStorageError("Archive entry escapes destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ModelStorageError("Archive contains an unsupported entry type")
            total += member.size
            if total > max_bytes:
                raise ModelStorageError("Extracted archive exceeds configured maximum size")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(member)
            if source is None:
                raise ModelStorageError("Archive member could not be read")
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)


def _open_tar(archive_path: Path):
    import tarfile

    return tarfile.open(archive_path, "r:*")


def get_manifest(artifact_id: str, snapshot: Optional[str] = None) -> dict:
    artifact_id = validate_artifact_id(artifact_id)
    registry = _load_registry()
    artifact = registry.get("artifacts", {}).get(artifact_id)
    if not artifact:
        raise ModelNotFoundError(f"Model artifact not found: {artifact_id}")
    snapshot = validate_snapshot(snapshot) if snapshot else artifact.get("active_snapshot")
    snap = artifact.get("snapshots", {}).get(snapshot)
    if not snap:
        raise ModelNotFoundError(f"Model snapshot not found: {artifact_id}@{snapshot}")
    manifest_path = Path(snap.get("manifest_path", ""))
    if not manifest_path.exists():
        raise ModelStorageError(f"Manifest is missing for {artifact_id}@{snapshot}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ModelStorageError("Unsupported manifest schema version")
    return manifest


def resolve_model_runtime(artifact_id: str, snapshot: Optional[str] = None) -> dict:
    """Resolve a managed model reference without mutating model storage state."""
    artifact_id = validate_artifact_id(artifact_id)
    registry = _load_registry()
    artifact = registry.get("artifacts", {}).get(artifact_id)
    if not artifact:
        raise ModelNotFoundError(f"Model artifact not found: {artifact_id}")
    snapshot_id = validate_snapshot(snapshot) if snapshot else artifact.get("active_snapshot")
    snap = artifact.get("snapshots", {}).get(snapshot_id)
    if not snap:
        raise ModelNotFoundError(f"Model snapshot not found: {artifact_id}@{snapshot_id}")
    manifest = get_manifest(artifact_id, snapshot_id)
    snapshot_path = Path(snap.get("snapshot_path", ""))
    runtime_raw = snap.get("runtime_path") or artifact.get("runtime_path")
    runtime_path = Path(runtime_raw) if runtime_raw else _runtime_path_from_manifest(snapshot_path, manifest)
    return {
        "artifact_id": artifact_id,
        "snapshot": snapshot_id,
        "artifact": dict(artifact),
        "snapshot_meta": dict(snap),
        "manifest": manifest,
        "manifest_path": snap.get("manifest_path"),
        "snapshot_path": str(snapshot_path),
        "runtime_path": str(runtime_path),
    }


def verify_artifact(artifact_id: str, snapshot: Optional[str] = None) -> dict:
    artifact_id = validate_artifact_id(artifact_id)
    manifest = get_manifest(artifact_id, snapshot)
    registry = _load_registry()
    artifact = registry["artifacts"][artifact_id]
    snapshot_id = manifest["snapshot"]
    snap = artifact["snapshots"][snapshot_id]
    snapshot_path = Path(snap["snapshot_path"])
    expected = {item["path"]: item for item in manifest.get("files", [])}
    checked = []
    missing = []
    changed = []
    for rel, item in expected.items():
        path = snapshot_path / rel
        if not path.exists():
            missing.append(rel)
            continue
        size = path.stat().st_size
        digest = _sha256_file(path)
        checked.append(rel)
        if size != item.get("size") or digest != item.get("sha256"):
            changed.append({"path": rel, "expected_sha256": item.get("sha256"), "actual_sha256": digest})
    actual_files = {
        str(path.relative_to(snapshot_path))
        for path in _iter_files(snapshot_path)
        if path.name != MANIFEST_FILENAME
    }
    extra = sorted(actual_files - set(expected.keys()))
    valid = not missing and not changed and not extra
    snap["state"] = "ready" if valid else "degraded"
    snap["last_verified_at"] = _now()
    artifact["snapshots"][snapshot_id] = snap
    registry["artifacts"][artifact_id] = artifact
    _save_registry(registry)
    return {
        "artifact_id": artifact_id,
        "snapshot": snapshot_id,
        "valid": valid,
        "checked": checked,
        "missing": missing,
        "changed": changed,
        "extra": extra,
    }


def _load_profile_refs(artifact_id: str, snapshot: Optional[str] = None) -> list[dict]:
    if not INFERENCE_PROFILES_FILE.exists():
        return []
    try:
        data = json.loads(INFERENCE_PROFILES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    profiles = data.get("profiles", data if isinstance(data, dict) else {})
    if not isinstance(profiles, dict):
        return []
    refs = []
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        matches = _profile_mentions_model(profile, artifact_id, snapshot)
        if not matches:
            continue
        state = str(profile.get("state") or profile.get("status") or profile.get("desired_state") or "").lower()
        running = state in {"running", "starting", "restarting", "active"}
        refs.append(
            {
                "profile_id": profile_id,
                "name": profile.get("name") or profile_id,
                "state": state or "unknown",
                "running": running,
            }
        )
    return refs


def _profile_mentions_model(value, artifact_id: str, snapshot: Optional[str]) -> bool:
    if isinstance(value, dict):
        direct_id = value.get("artifact_id") or value.get("model_artifact_id")
        direct_snapshot = value.get("snapshot") or value.get("model_snapshot")
        if direct_id == artifact_id and (snapshot is None or direct_snapshot in {None, "", snapshot}):
            return True
        model = value.get("model")
        if isinstance(model, dict):
            model_id = model.get("artifact_id") or model.get("id")
            model_snapshot = model.get("snapshot")
            if model_id == artifact_id and (snapshot is None or model_snapshot in {None, "", snapshot}):
                return True
        return any(_profile_mentions_model(item, artifact_id, snapshot) for item in value.values())
    if isinstance(value, list):
        return any(_profile_mentions_model(item, artifact_id, snapshot) for item in value)
    return False


def check_delete_references(artifact_id: str, snapshot: Optional[str] = None) -> dict:
    refs = _load_profile_refs(artifact_id, snapshot)
    running = [ref for ref in refs if ref.get("running")]
    stopped = [ref for ref in refs if not ref.get("running")]
    return {"running": running, "stopped": stopped, "has_references": bool(refs)}


def delete_artifact(
    artifact_id: str,
    snapshot: Optional[str] = None,
    force_stopped_references: bool = False,
    new_active_snapshot: Optional[str] = None,
) -> dict:
    artifact_id = validate_artifact_id(artifact_id)
    snapshot = validate_snapshot(snapshot) if snapshot else None
    with _json_lock:
        registry = _load_registry()
        artifacts = registry.get("artifacts", {})
        artifact = artifacts.get(artifact_id)
        if not artifact:
            raise ModelNotFoundError(f"Model artifact not found: {artifact_id}")
        refs = check_delete_references(artifact_id, snapshot)
        if refs["running"]:
            raise ModelConflictError({"message": "Running profiles reference this model", "references": refs["running"]})
        if refs["stopped"] and not force_stopped_references:
            raise ModelConflictError({"message": "Stopped profiles reference this model", "references": refs["stopped"], "requires_force": True})
        if snapshot:
            deleted = _delete_snapshot_from_registry(artifact, snapshot, new_active_snapshot)
            if not artifact.get("snapshots"):
                artifacts.pop(artifact_id, None)
            else:
                artifact["updated_at"] = _now()
                artifacts[artifact_id] = artifact
        else:
            deleted = _delete_artifact_files(artifact)
            artifacts.pop(artifact_id, None)
        registry["artifacts"] = artifacts
        _save_registry(registry)
        return {"deleted": artifact_id, "snapshot": snapshot, "paths": deleted, "references": refs}


def _delete_snapshot_from_registry(artifact: dict, snapshot: str, new_active_snapshot: Optional[str]) -> list[str]:
    snapshots = artifact.get("snapshots", {})
    snap = snapshots.get(snapshot)
    if not snap:
        raise ModelNotFoundError(f"Model snapshot not found: {artifact['id']}@{snapshot}")
    get_manifest(artifact["id"], snapshot)
    if artifact.get("active_snapshot") == snapshot and len(snapshots) > 1:
        if not new_active_snapshot:
            raise ModelConflictError("Deleting the active snapshot requires a replacement active snapshot")
        new_active_snapshot = validate_snapshot(new_active_snapshot)
        if new_active_snapshot not in snapshots or new_active_snapshot == snapshot:
            raise ModelConflictError("Replacement active snapshot does not exist")
        artifact["active_snapshot"] = new_active_snapshot
    snapshot_path = Path(snap["snapshot_path"])
    _safe_delete_managed_path(snapshot_path)
    snapshots.pop(snapshot, None)
    return [str(snapshot_path)]


def _delete_artifact_files(artifact: dict) -> list[str]:
    paths = []
    for snapshot_id in list(artifact.get("snapshots", {}).keys()):
        get_manifest(artifact["id"], snapshot_id)
    artifact_path = Path(artifact.get("artifact_path") or "")
    if not artifact_path:
        raise ModelStorageError("Artifact registry entry has no artifact path")
    _safe_delete_managed_path(artifact_path)
    paths.append(str(artifact_path))
    return paths


def _safe_delete_managed_path(path: Path):
    path = path.expanduser().resolve()
    parts = path.parts
    if "artifacts" not in parts:
        raise ModelStorageError("Refusing to delete a path outside an inframatik artifact directory")
    if not path.exists():
        return
    if path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


async def resolve_source(source: dict) -> dict:
    if not isinstance(source, dict):
        raise ModelStorageError("Source is required")
    source_type = (source.get("type") or "").strip().lower()
    if source_type == "local":
        path = _assert_import_allowed(Path(source.get("path") or ""))
        if not path.exists():
            raise ModelStorageError(f"Import path does not exist: {path}")
        files = _scan_local_import_files(path) if path.is_dir() else [path]
        rel_root = path if path.is_dir() else path.parent
        plan_files = [{"path": str(file.relative_to(rel_root)), "size": file.stat().st_size} for file in files]
        kind, fmt = _detect_kind_format(
            [{"path": item["path"], "size": item["size"], "sha256": ""} for item in plan_files],
            {"type": "local"},
            None,
            None,
        )
        return {"type": "local", "files": plan_files, "kind": kind, "format": fmt, "total_bytes": sum(item["size"] for item in plan_files)}
    if source_type == "url":
        sanitized = _sanitize_url_source(source)
        assert_url_allowed(str(source.get("url") or ""), allow_private=_allow_private_download_urls())
        return {"type": "url", "source": sanitized}
    if source_type == "huggingface":
        sanitized = _sanitize_hf_source(source)
        headers = {}
        token = source.get("token") or os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        files = sanitized.get("files") or await _resolve_hf_files(sanitized["repo"], sanitized["revision"], sanitized["preset"], headers)
        return {"type": "huggingface", "source": sanitized, "files": files}
    raise ModelStorageError("Source type must be 'local', 'url', or 'huggingface'")
