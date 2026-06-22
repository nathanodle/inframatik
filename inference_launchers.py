import asyncio
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional


CONFIG_DIR = Path.home() / ".config" / "inframatik"
LAUNCHERS_FILE = CONFIG_DIR / "inference_engine_launchers.json"
INFERENCE_PROFILES_FILE = CONFIG_DIR / "inference_profiles.json"

SCHEMA_VERSION = 1
ENGINE_FAMILIES = {"llama.cpp", "vllm", "sglang"}

_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|api[_-]?key|credential|auth|bearer)", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:secret|token|password|passwd|api[_-]?key|credential|auth|bearer)[A-Za-z0-9_]*)=([^\s]+)"
)
_MISSING_SHARED_LIBRARY_RE = re.compile(
    r"(?P<library>[A-Za-z0-9_.+-]+\.so(?:\.\d+)*)[:\s]+cannot open shared object file",
    re.IGNORECASE,
)
RUNTIME_PROBE_TIMEOUT_SECONDS = 12
_lock = threading.RLock()


class LauncherError(ValueError):
    status_code = 400

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class LauncherNotFoundError(LauncherError):
    status_code = 404


class LauncherConflictError(LauncherError):
    status_code = 409


def _now() -> int:
    return int(time.time())


def _empty_registry() -> dict:
    return {"schema_version": SCHEMA_VERSION, "launchers": {}}


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


def _load_registry() -> dict:
    if not LAUNCHERS_FILE.exists():
        return _empty_registry()
    try:
        data = json.loads(LAUNCHERS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise LauncherError(f"Invalid launcher registry {LAUNCHERS_FILE}: {e}") from e
    if not isinstance(data, dict):
        raise LauncherError("Launcher registry root must be a JSON object")
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("launchers", {})
    return data


def _save_registry(data: dict):
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("launchers", {})
    _atomic_write_json(LAUNCHERS_FILE, data)


def initialize_launcher_registry():
    with _lock:
        if not LAUNCHERS_FILE.exists():
            _save_registry(_empty_registry())


def validate_launcher_id(value: str) -> str:
    launcher_id = (value or "").strip().lower()
    if not _ID_RE.fullmatch(launcher_id):
        raise LauncherError("Launcher ID must be lowercase alphanumeric, hyphen, or underscore, and 64 characters or less")
    return launcher_id


def slugify_launcher_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (value or "").lower()).strip("-_")
    slug = re.sub(r"-+", "-", slug)[:64].strip("-_")
    if not slug or not _ID_RE.fullmatch(slug):
        slug = f"launcher-{_now()}"
    return slug


def normalize_engine(value: str) -> str:
    engine = (value or "").strip().lower()
    aliases = {
        "llama": "llama.cpp",
        "llama-cpp": "llama.cpp",
        "llamacpp": "llama.cpp",
        "llama_cpp": "llama.cpp",
        "vllm": "vllm",
        "sglang": "sglang",
    }
    engine = aliases.get(engine, engine)
    if engine not in ENGINE_FAMILIES:
        raise LauncherError("Engine must be one of: llama.cpp, vllm, sglang")
    return engine


def _validate_text_token(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise LauncherError(f"{field} must be a string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise LauncherError(f"{field} cannot contain NUL or newline characters")
    return value


def _normalize_executable(value: str) -> str:
    raw = _validate_text_token((value or "").strip(), "Executable")
    if not raw:
        raise LauncherError("Executable path is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise LauncherError("Executable path must be absolute")
    return str(path)


def _normalize_working_dir(value: Optional[str]) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    raw = _validate_text_token(str(value).strip(), "Working directory")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise LauncherError("Working directory must be absolute")
    return str(path)


def normalize_base_args(args) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list):
        raise LauncherError("Base args must be an ordered list of argv tokens")
    normalized = []
    for index, arg in enumerate(args):
        token = _validate_text_token(str(arg), f"Base arg {index}")
        if token == "":
            raise LauncherError("Base args cannot contain empty tokens")
        normalized.append(token)
    return normalized


def normalize_env(env) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise LauncherError("Env must be an object of key/value pairs")
    normalized = {}
    for key, value in env.items():
        key = str(key).strip()
        if not _ENV_KEY_RE.fullmatch(key):
            raise LauncherError(f"Invalid env key: {key}")
        normalized[key] = _validate_text_token(str(value), f"Env value for {key}")
    return normalized


def _redact_env(env: dict) -> dict:
    redacted = {}
    redacted_keys = []
    for key, value in sorted((env or {}).items()):
        redacted_keys.append(key)
        redacted[key] = "<redacted>" if _SECRET_KEY_RE.search(key) or value else "<redacted>"
    return {"env": redacted, "redacted_env_keys": redacted_keys}


def _public_launcher(launcher: dict, include_validation: bool = False) -> dict:
    item = dict(launcher)
    redacted = _redact_env(item.get("env") or {})
    item["env"] = redacted["env"]
    item["redacted_env_keys"] = redacted["redacted_env_keys"]
    item["env_count"] = len(redacted["redacted_env_keys"])
    item["command_preview"] = [item["executable"], *(item.get("base_args") or [])]
    if include_validation:
        item["validation"] = validate_launcher_path(item["id"])
    return item


def list_launchers(include_validation: bool = False) -> dict:
    initialize_launcher_registry()
    with _lock:
        registry = _load_registry()
        launchers = [
            _public_launcher(launcher, include_validation=include_validation)
            for launcher in registry.get("launchers", {}).values()
        ]
        launchers.sort(key=lambda item: (item.get("engine", ""), item.get("id", "")))
        return {
            "schema_version": registry.get("schema_version", SCHEMA_VERSION),
            "registry_path": str(LAUNCHERS_FILE),
            "launchers": launchers,
        }


def get_launcher(launcher_id: str, include_secret_env: bool = False) -> dict:
    launcher_id = validate_launcher_id(launcher_id)
    registry = _load_registry()
    launcher = registry.get("launchers", {}).get(launcher_id)
    if not launcher:
        raise LauncherNotFoundError(f"Launcher not found: {launcher_id}")
    return dict(launcher) if include_secret_env else _public_launcher(launcher)


def create_launcher(
    display_name: Optional[str],
    engine: str,
    executable: str,
    base_args=None,
    working_dir: Optional[str] = None,
    env=None,
    launcher_id: Optional[str] = None,
) -> dict:
    engine = normalize_engine(engine)
    display_name = _validate_text_token((display_name or "").strip(), "Display name") or f"{engine} launcher"
    launcher_id = validate_launcher_id(launcher_id) if launcher_id else slugify_launcher_id(display_name)
    launcher = {
        "id": launcher_id,
        "display_name": display_name,
        "engine": engine,
        "executable": _normalize_executable(executable),
        "base_args": normalize_base_args(base_args),
        "working_dir": _normalize_working_dir(working_dir),
        "env": normalize_env(env),
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _lock:
        registry = _load_registry()
        if launcher_id in registry.get("launchers", {}):
            raise LauncherConflictError(f"Launcher already exists: {launcher_id}")
        registry.setdefault("launchers", {})[launcher_id] = launcher
        _save_registry(registry)
    return _public_launcher(launcher)


def update_launcher(launcher_id: str, updates: dict) -> dict:
    launcher_id = validate_launcher_id(launcher_id)
    if not isinstance(updates, dict):
        raise LauncherError("Update body must be an object")
    with _lock:
        registry = _load_registry()
        launcher = registry.get("launchers", {}).get(launcher_id)
        if not launcher:
            raise LauncherNotFoundError(f"Launcher not found: {launcher_id}")
        updated = dict(launcher)
        if "display_name" in updates:
            updated["display_name"] = _validate_text_token((updates.get("display_name") or "").strip(), "Display name")
            if not updated["display_name"]:
                raise LauncherError("Display name is required")
        if "engine" in updates:
            updated["engine"] = normalize_engine(updates.get("engine"))
        if "executable" in updates:
            updated["executable"] = _normalize_executable(updates.get("executable"))
        if "base_args" in updates:
            updated["base_args"] = normalize_base_args(updates.get("base_args"))
        if "working_dir" in updates:
            updated["working_dir"] = _normalize_working_dir(updates.get("working_dir"))
        if "env" in updates:
            updated["env"] = normalize_env(updates.get("env"))
        updated["updated_at"] = _now()
        registry["launchers"][launcher_id] = updated
        _save_registry(registry)
    return _public_launcher(updated)


def validate_launcher_path(launcher_id: str) -> dict:
    launcher = get_launcher(launcher_id, include_secret_env=True)
    executable = Path(launcher["executable"]).expanduser()
    working_dir = Path(launcher["working_dir"]).expanduser() if launcher.get("working_dir") else None
    errors = []
    executable_info = {
        "path": str(executable),
        "exists": executable.exists(),
        "is_file": executable.is_file(),
        "executable": os.access(executable, os.X_OK) if executable.exists() else False,
    }
    if not executable_info["exists"]:
        errors.append("Executable path does not exist")
    elif not executable_info["is_file"]:
        errors.append("Executable path is not a file")
    elif not executable_info["executable"]:
        errors.append("Executable path is not executable")

    working_dir_info = None
    if working_dir:
        working_dir_info = {
            "path": str(working_dir),
            "exists": working_dir.exists(),
            "is_dir": working_dir.is_dir(),
        }
        if not working_dir_info["exists"]:
            errors.append("Working directory does not exist")
        elif not working_dir_info["is_dir"]:
            errors.append("Working directory is not a directory")
    return {
        "launcher_id": launcher_id,
        "valid": not errors,
        "errors": errors,
        "executable": executable_info,
        "working_dir": working_dir_info,
    }


def _runtime_probe_argv(launcher: dict) -> list[str]:
    argv = [launcher["executable"], *(launcher.get("base_args") or [])]
    if "--help" not in argv and "-h" not in argv:
        argv.append("--help")
    return argv


def _redact_argv(argv: list[str]) -> list[str]:
    redacted = []
    hide_next = False
    for token in argv:
        text = str(token)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        is_secret_assignment = _SECRET_KEY_RE.search(text) and "=" in text
        redacted.append("<redacted>" if is_secret_assignment else text)
        if _SECRET_KEY_RE.search(text) and not is_secret_assignment:
            hide_next = True
    return redacted


def _redact_output(text: str) -> str:
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text or "")


def _launcher_venv_root(launcher: dict) -> Optional[Path]:
    executable = Path(launcher.get("executable") or "").expanduser()
    return executable.parent.parent if executable.parent.name == "bin" else None


def _runtime_env_suggestions(launcher: dict, output: str) -> dict[str, str]:
    venv_root = _launcher_venv_root(launcher)
    if not venv_root or not venv_root.exists():
        return {}
    library_dirs: list[str] = []
    for match in _MISSING_SHARED_LIBRARY_RE.finditer(output or ""):
        library = match.group("library")
        matches = sorted(venv_root.glob(f"lib/python*/site-packages/nvidia/**/{library}"))
        for path in matches:
            if not path.is_file():
                continue
            directory = str(path.parent)
            if directory not in library_dirs:
                library_dirs.append(directory)
    if not library_dirs:
        return {}
    existing = str((launcher.get("env") or {}).get("LD_LIBRARY_PATH") or "")
    existing_parts = [part for part in existing.split(":") if part]
    next_parts = [part for part in library_dirs if part not in existing_parts]
    if not next_parts:
        return {}
    return {"LD_LIBRARY_PATH": ":".join([*next_parts, *existing_parts])}


async def validate_launcher_runtime(launcher_id: str, timeout: float = RUNTIME_PROBE_TIMEOUT_SECONDS) -> dict:
    launcher = get_launcher(launcher_id, include_secret_env=True)
    path_result = validate_launcher_path(launcher_id)
    result = dict(path_result)
    errors = list(result.get("errors") or [])
    argv = _runtime_probe_argv(launcher)
    runtime = {
        "checked": False,
        "valid": None,
        "command_preview": _redact_argv(argv),
        "code": None,
        "timed_out": False,
        "elapsed_ms": None,
        "output": "",
        "suggested_env": {},
    }
    result["runtime"] = runtime
    if not path_result.get("valid"):
        runtime["output"] = "Runtime probe skipped because launcher path validation failed."
        result["valid"] = False
        return result

    timeout = max(1.0, float(timeout or RUNTIME_PROBE_TIMEOUT_SECONDS))
    env = dict(os.environ)
    env.update(launcher.get("env") or {})
    cwd = launcher.get("working_dir") or str(Path.home())
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            stdout, _ = await proc.communicate()
            runtime["timed_out"] = True
            errors.append(f"Runtime probe timed out after {timeout:g}s")
        runtime["code"] = proc.returncode
        output = stdout.decode(errors="replace").strip()
        runtime["output"] = _redact_output(output[-8000:])
        runtime["suggested_env"] = _runtime_env_suggestions(launcher, runtime["output"])
    except OSError as e:
        runtime["output"] = str(e)
        errors.append(f"Runtime probe could not start: {e}")
    runtime["checked"] = True
    runtime["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if runtime["code"] not in (0, None) and not runtime["timed_out"]:
        errors.append(f"Runtime probe exited with code {runtime['code']}")
    if runtime["suggested_env"]:
        errors.append("Runtime dependency was found inside the venv; add the suggested launcher env and validate again.")
    runtime["valid"] = not errors
    result["errors"] = errors
    result["valid"] = not errors
    return result


def _load_profile_refs(launcher_id: str) -> list[dict]:
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
        if not _profile_mentions_launcher(profile, launcher_id):
            continue
        state = str(profile.get("state") or profile.get("status") or profile.get("desired_state") or "").lower()
        running = state in {"running", "starting", "restarting", "active"}
        refs.append({
            "profile_id": profile_id,
            "name": profile.get("name") or profile_id,
            "state": state or "unknown",
            "running": running,
        })
    return refs


def _profile_mentions_launcher(value, launcher_id: str) -> bool:
    if isinstance(value, dict):
        direct = value.get("engine_launcher_id") or value.get("launcher_id")
        if direct == launcher_id:
            return True
        launcher = value.get("engine_launcher") or value.get("launcher")
        if isinstance(launcher, dict) and (launcher.get("id") == launcher_id):
            return True
        return any(_profile_mentions_launcher(item, launcher_id) for item in value.values())
    if isinstance(value, list):
        return any(_profile_mentions_launcher(item, launcher_id) for item in value)
    return False


def check_launcher_references(launcher_id: str) -> dict:
    launcher_id = validate_launcher_id(launcher_id)
    refs = _load_profile_refs(launcher_id)
    running = [ref for ref in refs if ref.get("running")]
    stopped = [ref for ref in refs if not ref.get("running")]
    return {"running": running, "stopped": stopped, "has_references": bool(refs)}


def delete_launcher(launcher_id: str, force_stopped_references: bool = False) -> dict:
    launcher_id = validate_launcher_id(launcher_id)
    with _lock:
        registry = _load_registry()
        if launcher_id not in registry.get("launchers", {}):
            raise LauncherNotFoundError(f"Launcher not found: {launcher_id}")
        refs = check_launcher_references(launcher_id)
        if refs["running"]:
            raise LauncherConflictError({"message": "Running profiles reference this launcher", "references": refs["running"]})
        if refs["stopped"] and not force_stopped_references:
            raise LauncherConflictError({"message": "Stopped profiles reference this launcher", "references": refs["stopped"], "requires_force": True})
        del registry["launchers"][launcher_id]
        _save_registry(registry)
    return {"deleted": launcher_id, "references": refs}
