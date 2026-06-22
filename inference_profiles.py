import copy
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import inference_planner


CONFIG_DIR = Path.home() / ".config" / "inframatik"
INFERENCE_PROFILES_FILE = CONFIG_DIR / "inference_profiles.json"
INFERENCE_SECRETS_FILE = CONFIG_DIR / "inference_secrets.json"
INFERENCE_CLEANUP_FILE = CONFIG_DIR / "inference_cleanup.json"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

SCHEMA_VERSION = 1
_PROFILE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|api[_-]?key|credential|auth|bearer)", re.IGNORECASE)
_RUNNING_STATES = {"running", "starting", "restarting", "active"}
_lock = threading.RLock()


class ProfileError(ValueError):
    status_code = 400

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class ProfileNotFoundError(ProfileError):
    status_code = 404


class ProfileConflictError(ProfileError):
    status_code = 409


class ProfileValidationError(ProfileError):
    status_code = 400


def _now() -> int:
    return int(time.time())


def _empty_profiles() -> dict:
    return {"schema_version": SCHEMA_VERSION, "profiles": {}}


def _empty_secrets() -> dict:
    return {"schema_version": SCHEMA_VERSION, "secrets": {}}


def _empty_cleanup() -> dict:
    return {"schema_version": SCHEMA_VERSION, "cleanup": {}}


def _atomic_write_text(path: Path, text: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _atomic_write_json(path: Path, data: dict, mode: int = 0o600):
    _atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)


def _load_json(path: Path, default: dict, label: str) -> dict:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise ProfileError(f"Invalid {label} registry {path}: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"Invalid {label} registry {path}: expected object")
    return data


def _load_profiles_registry() -> dict:
    data = _load_json(INFERENCE_PROFILES_FILE, _empty_profiles(), "profile")
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("profiles", {})
    return data


def _load_secrets_registry() -> dict:
    data = _load_json(INFERENCE_SECRETS_FILE, _empty_secrets(), "secret")
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("secrets", {})
    return data


def _load_cleanup_registry() -> dict:
    data = _load_json(INFERENCE_CLEANUP_FILE, _empty_cleanup(), "cleanup")
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("cleanup", {})
    return data


def _save_profiles_registry(data: dict):
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("profiles", {})
    _atomic_write_json(INFERENCE_PROFILES_FILE, data)


def _save_secrets_registry(data: dict):
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("secrets", {})
    _atomic_write_json(INFERENCE_SECRETS_FILE, data)


def _save_cleanup_registry(data: dict):
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("cleanup", {})
    _atomic_write_json(INFERENCE_CLEANUP_FILE, data)


def initialize_profile_registries():
    with _lock:
        if not INFERENCE_PROFILES_FILE.exists():
            _save_profiles_registry(_empty_profiles())
        if not INFERENCE_SECRETS_FILE.exists():
            _save_secrets_registry(_empty_secrets())
        if not INFERENCE_CLEANUP_FILE.exists():
            _save_cleanup_registry(_empty_cleanup())
        UNIT_DIR.mkdir(parents=True, exist_ok=True)


def _validate_profile_id(profile_id: str) -> str:
    value = (profile_id or "").strip().lower()
    if not _PROFILE_ID_RE.fullmatch(value):
        raise ProfileError("Profile ID must be lowercase alphanumeric, hyphen, or underscore, and 64 characters or less")
    return value


def list_profiles() -> dict:
    initialize_profile_registries()
    with _lock:
        registry = _load_profiles_registry()
        profiles = [_public_profile(profile) for profile in registry.get("profiles", {}).values()]
        profiles.sort(key=lambda item: item.get("id", ""))
        return {
            "schema_version": registry.get("schema_version", SCHEMA_VERSION),
            "registry_path": str(INFERENCE_PROFILES_FILE),
            "secrets_path": str(INFERENCE_SECRETS_FILE),
            "cleanup_path": str(INFERENCE_CLEANUP_FILE),
            "unit_dir": str(UNIT_DIR),
            "profiles": profiles,
        }


def get_profile(profile_id: str) -> dict:
    profile_id = _validate_profile_id(profile_id)
    initialize_profile_registries()
    with _lock:
        profile = _load_profiles_registry().get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        return _public_profile(profile)


def get_profile_raw(profile_id: str) -> dict:
    profile_id = _validate_profile_id(profile_id)
    initialize_profile_registries()
    with _lock:
        profile = _load_profiles_registry().get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        return copy.deepcopy(profile)


def mutate_profile(profile_id: str, mutator):
    """Apply a small metadata mutation to one profile under the registry lock."""
    profile_id = _validate_profile_id(profile_id)
    initialize_profile_registries()
    with _lock:
        registry = _load_profiles_registry()
        profile = registry.get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        result = mutator(profile)
        profile["updated_at"] = _now()
        registry["profiles"][profile_id] = profile
        _save_profiles_registry(registry)
        return result if result is not None else _public_profile(profile)


def get_engine_api_key_value(profile_id: str) -> Optional[str]:
    profile = get_profile_raw(profile_id)
    secret_id = (profile.get("secrets") or {}).get("engine_api_key_id")
    if not secret_id:
        return None
    with _lock:
        secret = _load_secrets_registry().get("secrets", {}).get(secret_id)
    if not isinstance(secret, dict):
        return None
    return secret.get("value")


def set_engine_api_key(profile_id: str, raw_key: str) -> dict:
    profile_id = _validate_profile_id(profile_id)
    key = str(raw_key or "").strip()
    if not key:
        raise ProfileError("Engine API key cannot be empty")
    initialize_profile_registries()
    with _lock:
        profiles = _load_profiles_registry()
        profile = profiles.get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        secrets = _load_secrets_registry()
        secret_id = f"engine-api-key-{profile_id}"
        existing_secret = secrets.get("secrets", {}).get(secret_id) or {}
        now = _now()
        secrets["secrets"][secret_id] = {
            "id": secret_id,
            "kind": "engine_api_key",
            "profile_id": profile_id,
            "value": key,
            "created_at": existing_secret.get("created_at") or now,
            "rotated_at": now,
        }
        profile.setdefault("secrets", {})["engine_api_key_id"] = secret_id
        profile["restart_required"] = _profile_is_running(profile)
        if profile["restart_required"]:
            fields = set(profile.get("restart_required_fields") or [])
            fields.add("secrets.engine_api_key_id")
            profile["restart_required_fields"] = sorted(fields)
        profile["updated_at"] = now
        plan = _plan_or_raise(
            _profile_with_secret_values(profile, secret_values={secret_id: key}),
            existing_profile_id=profile_id,
        )
        old_registry = copy.deepcopy(profiles)
        old_secrets = copy.deepcopy(secrets)
        profiles["profiles"][profile_id] = profile
        old_units = _unit_names_from_profile(profile)
        try:
            _save_secrets_registry(secrets)
            _save_profiles_registry(profiles)
            _sync_unit_files(plan, old_unit_names=old_units)
        except Exception:
            _save_secrets_registry(old_secrets)
            _save_profiles_registry(old_registry)
            raise
        return {"profile": _public_profile(profile), "secret": _public_secret_metadata(secrets["secrets"][secret_id])}


def disable_engine_api_key(profile_id: str) -> dict:
    profile_id = _validate_profile_id(profile_id)
    initialize_profile_registries()
    with _lock:
        profiles = _load_profiles_registry()
        profile = profiles.get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        secret_id = (profile.get("secrets") or {}).pop("engine_api_key_id", None)
        secrets = _load_secrets_registry()
        old_registry = copy.deepcopy(profiles)
        old_secrets = copy.deepcopy(secrets)
        if secret_id:
            secrets.get("secrets", {}).pop(secret_id, None)
        profile["restart_required"] = _profile_is_running(profile)
        if profile["restart_required"]:
            fields = set(profile.get("restart_required_fields") or [])
            fields.add("secrets.engine_api_key_id")
            profile["restart_required_fields"] = sorted(fields)
        profile["updated_at"] = _now()
        plan = _plan_with_profile_secrets(profile, existing_profile_id=profile_id)
        profiles["profiles"][profile_id] = profile
        old_units = _unit_names_from_profile(profile)
        try:
            _save_secrets_registry(secrets)
            _save_profiles_registry(profiles)
            _sync_unit_files(plan, old_unit_names=old_units)
        except Exception:
            _save_secrets_registry(old_secrets)
            _save_profiles_registry(old_registry)
            raise
        return {"profile": _public_profile(profile), "removed_secret_id": secret_id}


def list_profile_instances(profile_id: str) -> list[dict]:
    profile = get_profile_raw(profile_id)
    return copy.deepcopy(profile.get("instances") or [])


def update_profile_runtime_state(profile_id: str, state: str, instance_updates: Optional[dict[int, dict]] = None) -> dict:
    profile_id = _validate_profile_id(profile_id)
    initialize_profile_registries()
    with _lock:
        registry = _load_profiles_registry()
        profile = registry.get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        profile["state"] = state
        profile["updated_at"] = _now()
        if instance_updates:
            instances = []
            for instance in profile.get("instances") or []:
                item = copy.deepcopy(instance)
                try:
                    update = instance_updates.get(int(item.get("index")))
                except (TypeError, ValueError):
                    update = None
                if update:
                    item.update(copy.deepcopy(update))
                instances.append(item)
            profile["instances"] = instances
        registry["profiles"][profile_id] = profile
        _save_profiles_registry(registry)
        return _public_profile(profile)


def render_profile(profile_id: str) -> dict:
    profile_id = _validate_profile_id(profile_id)
    initialize_profile_registries()
    with _lock:
        profile = _load_profiles_registry().get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        return _public_plan(
            inference_planner.preview_profile(
                _profile_with_secret_values(profile),
                existing_profile_id=profile_id,
            )
        )


def export_profile(profile_id: str) -> dict:
    profile_id = _validate_profile_id(profile_id)
    initialize_profile_registries()
    with _lock:
        profile = _load_profiles_registry().get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        public_profile = _public_profile(profile)
        validation = _public_plan(
            inference_planner.preview_profile(
                _profile_with_secret_values(profile),
                existing_profile_id=profile_id,
            )
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": _now(),
        "profile_id": profile_id,
        "warning": "Launcher paths, model artifacts, ports, GPU IDs, and Cloudflare resources are node-local.",
        "profile": public_profile,
        "validation": validation,
    }


def create_profile(body: dict) -> dict:
    draft = _extract_profile_body(body)
    _reject_raw_engine_api_key(draft)
    initialize_profile_registries()
    with _lock:
        registry = _load_profiles_registry()
        plan = _plan_or_raise(draft, existing_profile_id=None)
        profile_id = plan["profile_id"]
        if profile_id in registry.get("profiles", {}):
            raise ProfileConflictError(f"Inference profile already exists: {profile_id}")
        profile = _profile_from_plan(draft, plan)
        old_registry = copy.deepcopy(registry)
        registry["profiles"][profile_id] = profile
        new_unit_names = _unit_names_from_plan(plan)
        try:
            _save_profiles_registry(registry)
            _sync_unit_files(plan, old_unit_names=[])
        except Exception:
            _save_profiles_registry(old_registry)
            _remove_unit_files(new_unit_names)
            raise
        return _result("created", profile, plan)


def update_profile(profile_id: str, body: dict) -> dict:
    profile_id = _validate_profile_id(profile_id)
    updates = _extract_profile_body(body)
    initialize_profile_registries()
    with _lock:
        registry = _load_profiles_registry()
        existing = registry.get("profiles", {}).get(profile_id)
        if not existing:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        draft = _merge_profile_update(existing, updates)
        draft["id"] = profile_id
        _reject_raw_engine_api_key(draft)
        plan = _plan_or_raise(_profile_with_secret_values(draft, existing), existing_profile_id=profile_id)
        profile = _profile_from_plan(draft, plan, existing=existing)
        old_registry = copy.deepcopy(registry)
        old_units = _unit_names_from_profile(existing)
        registry["profiles"][profile_id] = profile
        try:
            _save_profiles_registry(registry)
            _sync_unit_files(plan, old_unit_names=old_units)
        except Exception:
            _save_profiles_registry(old_registry)
            raise
        return _result("updated", profile, plan)


def delete_profile(profile_id: str, force: bool = False) -> dict:
    profile_id = _validate_profile_id(profile_id)
    initialize_profile_registries()
    with _lock:
        registry = _load_profiles_registry()
        profile = registry.get("profiles", {}).get(profile_id)
        if not profile:
            raise ProfileNotFoundError(f"Inference profile not found: {profile_id}")
        state = str(profile.get("state") or profile.get("status") or "").lower()
        if state in _RUNNING_STATES and not force:
            raise ProfileConflictError("Stop the inference profile before deleting it")
        old_registry = copy.deepcopy(registry)
        secrets = _load_secrets_registry()
        old_secrets = copy.deepcopy(secrets)
        unit_names = _unit_names_from_profile(profile)
        unit_backups = _read_unit_backups(unit_names)
        for secret_id in (profile.get("secrets") or {}).values():
            if secret_id:
                secrets.get("secrets", {}).pop(secret_id, None)
        del registry["profiles"][profile_id]
        try:
            _save_secrets_registry(secrets)
            _save_profiles_registry(registry)
            _remove_unit_files(unit_names)
        except Exception:
            _save_secrets_registry(old_secrets)
            _save_profiles_registry(old_registry)
            _restore_unit_backups(unit_backups)
            raise
        return {"deleted": profile_id, "removed_units": unit_names}


def _extract_profile_body(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ProfileError("Profile body must be an object")
    profile = body.get("profile") if isinstance(body.get("profile"), dict) else body
    return copy.deepcopy(profile)


def _reject_raw_engine_api_key(draft: dict):
    common = draft.get("common") if isinstance(draft.get("common"), dict) else {}
    if common.get("api_key") or common.get("engine_api_key"):
        raise ProfileValidationError("Raw engine API keys are not persisted in profiles; use the profile API-key action when it is available")


def _merge_profile_update(existing: dict, updates: dict) -> dict:
    merged = copy.deepcopy(existing)
    for key in ("display_name", "name", "engine", "engine_launcher_id", "launcher_id", "model", "model_ref", "common", "engine_config", "advanced", "deployment", "exposure"):
        if key in updates:
            merged[key] = copy.deepcopy(updates[key])
    if "engine_launcher" in updates:
        merged["engine_launcher"] = copy.deepcopy(updates["engine_launcher"])
    _preserve_redacted_profile_env(merged, existing)
    return merged


def _preserve_redacted_profile_env(merged: dict, existing: dict):
    merged_adv = merged.get("advanced") if isinstance(merged.get("advanced"), dict) else None
    existing_adv = existing.get("advanced") if isinstance(existing.get("advanced"), dict) else None
    if not merged_adv or not existing_adv:
        return
    merged_env = merged_adv.get("env")
    existing_env = existing_adv.get("env")
    if not isinstance(merged_env, dict) or not isinstance(existing_env, dict):
        return
    for key, value in list(merged_env.items()):
        if value == "<redacted>" and key in existing_env:
            merged_env[key] = existing_env[key]


def _plan_or_raise(draft: dict, existing_profile_id: Optional[str]) -> dict:
    plan = inference_planner.preview_profile(
        copy.deepcopy(draft),
        existing_profile_id=existing_profile_id,
        include_raw=True,
    )
    if not plan.get("valid_for_save"):
        raise ProfileValidationError({
            "message": "Inference profile has validation blockers",
            "blockers": plan.get("blockers") or [],
            "warnings": plan.get("warnings") or [],
        })
    return plan


def _plan_with_profile_secrets(profile: dict, existing_profile_id: Optional[str]) -> dict:
    return _plan_or_raise(_profile_with_secret_values(profile), existing_profile_id=existing_profile_id)


def _profile_with_secret_values(profile: dict, existing: Optional[dict] = None, secret_values: Optional[dict] = None) -> dict:
    draft = copy.deepcopy(profile)
    source = existing if existing is not None else profile
    secret_id = (source.get("secrets") or {}).get("engine_api_key_id")
    if not secret_id:
        return draft
    secret_values = secret_values or {}
    value = secret_values.get(secret_id)
    if value is None:
        secret = _load_secrets_registry().get("secrets", {}).get(secret_id)
        value = secret.get("value") if isinstance(secret, dict) else None
    if not value:
        return draft
    common = draft.get("common") if isinstance(draft.get("common"), dict) else {}
    common = copy.deepcopy(common)
    common["api_key"] = value
    draft["common"] = common
    return draft


def _profile_is_running(profile: dict) -> bool:
    return str(profile.get("state") or profile.get("status") or "").lower() in _RUNNING_STATES


def _public_secret_metadata(secret: dict) -> dict:
    return {
        "id": secret.get("id"),
        "kind": secret.get("kind"),
        "profile_id": secret.get("profile_id"),
        "created_at": secret.get("created_at"),
        "rotated_at": secret.get("rotated_at"),
    }


def _profile_from_plan(draft: dict, plan: dict, existing: Optional[dict] = None) -> dict:
    existing = existing or {}
    now = _now()
    profile_id = plan["profile_id"]
    engine_launcher_id = _launcher_id(draft)
    model = _model_ref(draft)
    profile = {
        "schema_version": SCHEMA_VERSION,
        "id": profile_id,
        "display_name": _safe_text(draft.get("display_name") or draft.get("name") or profile_id),
        "engine": plan.get("engine"),
        "engine_launcher_id": engine_launcher_id,
        "model": model,
        "common": copy.deepcopy(draft.get("common") if isinstance(draft.get("common"), dict) else {}),
        "engine_config": copy.deepcopy(draft.get("engine_config") if isinstance(draft.get("engine_config"), dict) else {}),
        "advanced": copy.deepcopy(draft.get("advanced") if isinstance(draft.get("advanced"), dict) else {}),
        "deployment": copy.deepcopy(draft.get("deployment") if isinstance(draft.get("deployment"), dict) else {}),
        "exposure": copy.deepcopy(draft.get("exposure") if isinstance(draft.get("exposure"), dict) else {}),
        "instances": copy.deepcopy(plan.get("resolved_instances") or []),
        "secrets": copy.deepcopy(existing.get("secrets") or {}),
        "cloudflare": _cloudflare_metadata(draft, existing),
        "state": existing.get("state") or existing.get("status") or "stopped",
        "restart_required": bool((plan.get("restart_required") or {}).get("required")),
        "restart_required_fields": list((plan.get("restart_required") or {}).get("fields") or []),
        "command_preview": _public_command_preview(plan.get("command_preview") or []),
        "unit_names": _unit_names_from_plan(plan),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    return profile


def _launcher_id(draft: dict) -> str:
    launcher = draft.get("engine_launcher") if isinstance(draft.get("engine_launcher"), dict) else {}
    return draft.get("engine_launcher_id") or draft.get("launcher_id") or launcher.get("id")


def _model_ref(draft: dict) -> dict:
    model = draft.get("model") or draft.get("model_ref")
    if isinstance(model, str):
        return {"artifact_id": model}
    return copy.deepcopy(model if isinstance(model, dict) else {})


def _safe_text(value) -> str:
    text = str(value or "").strip()
    return text.replace("\x00", "").replace("\n", " ").replace("\r", " ")[:160]


def _cloudflare_metadata(draft: dict, existing: dict) -> dict:
    if isinstance(existing.get("cloudflare"), dict):
        return copy.deepcopy(existing["cloudflare"])
    exposure = draft.get("exposure") if isinstance(draft.get("exposure"), dict) else {}
    cloudflare = exposure.get("cloudflare") if isinstance(exposure.get("cloudflare"), dict) else {}
    return {
        "hostname": exposure.get("hostname") if exposure.get("mode") == "cloudflare" else None,
        "access_app_id": cloudflare.get("access_app_id"),
        "access_policy_id": cloudflare.get("access_policy_id"),
        "service_tokens": copy.deepcopy(cloudflare.get("service_tokens") or []),
    }


def _result(action: str, profile: dict, plan: dict) -> dict:
    public_plan = _public_plan(plan)
    return {
        "status": action,
        "profile": _public_profile(profile),
        "plan": public_plan,
        "units": _unit_records(profile),
    }


def _public_profile(profile: dict) -> dict:
    public = copy.deepcopy(profile)
    advanced = public.get("advanced") if isinstance(public.get("advanced"), dict) else {}
    if isinstance(advanced.get("env"), dict):
        advanced["env"] = _redact_env(advanced["env"])["env"]
        advanced["redacted_env_keys"] = _redact_env(advanced.get("env") or {})["redacted_env_keys"]
    common = public.get("common") if isinstance(public.get("common"), dict) else {}
    for key in ("api_key", "engine_api_key"):
        if key in common:
            common[key] = "<redacted>"
    public["command_preview"] = _public_command_preview(public.get("command_preview") or [])
    public["units"] = _unit_records(public)
    return public


def _public_plan(plan: dict) -> dict:
    public = copy.deepcopy(plan)
    public["command_preview"] = _public_command_preview(public.get("command_preview") or [])
    units = []
    for unit in (public.get("systemd_preview") or {}).get("units") or []:
        name = unit.get("name")
        units.append({
            "index": unit.get("index"),
            "name": name,
            "path": str(_unit_path(name)) if name else None,
            "content": "[generated unit written to disk]",
        })
    public["systemd_preview"] = {"units": units}
    return public


def _public_command_preview(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        clean = copy.deepcopy(item)
        clean.pop("_env_raw", None)
        clean.pop("_argv_raw", None)
        result.append(clean)
    return result


def _redact_env(env: dict) -> dict:
    redacted = {}
    redacted_keys = []
    for key, value in sorted((env or {}).items()):
        if _SECRET_KEY_RE.search(key):
            redacted[key] = "<redacted>"
            redacted_keys.append(key)
        else:
            redacted[key] = value
    return {"env": redacted, "redacted_env_keys": redacted_keys}


def _unit_names_from_plan(plan: dict) -> list[str]:
    return [
        unit["name"]
        for unit in (plan.get("systemd_preview") or {}).get("units") or []
        if isinstance(unit, dict) and unit.get("name")
    ]


def _unit_names_from_profile(profile: dict) -> list[str]:
    names = [str(name) for name in profile.get("unit_names") or [] if name]
    if names:
        return names
    return [
        str(instance["unit"])
        for instance in profile.get("instances") or []
        if isinstance(instance, dict) and instance.get("unit")
    ]


def _unit_records(profile: dict) -> list[dict]:
    records = []
    for name in _unit_names_from_profile(profile):
        path = _unit_path(name)
        records.append({"name": name, "path": str(path), "exists": path.exists()})
    return records


def _unit_path(name: str) -> Path:
    if not name or Path(name).name != name or "/" in name:
        raise ProfileError(f"Invalid generated unit name: {name}")
    return UNIT_DIR / name


def _sync_unit_files(plan: dict, old_unit_names: list[str]):
    new_units = (plan.get("systemd_preview") or {}).get("units") or []
    new_names = [unit["name"] for unit in new_units if unit.get("name")]
    names_to_backup = sorted(set(old_unit_names) | set(new_names))
    backups = _read_unit_backups(names_to_backup)
    try:
        for unit in new_units:
            name = unit.get("name")
            content = unit.get("content")
            if not name or not isinstance(content, str):
                raise ProfileError("Generated unit preview is missing unit content")
            _atomic_write_text(_unit_path(name), content, mode=0o600)
        for name in sorted(set(old_unit_names) - set(new_names)):
            _unit_path(name).unlink(missing_ok=True)
    except Exception:
        _restore_unit_backups(backups)
        raise


def _read_unit_backups(unit_names: list[str]) -> dict[str, Optional[str]]:
    backups = {}
    for name in unit_names:
        path = _unit_path(name)
        backups[name] = path.read_text() if path.exists() else None
    return backups


def _restore_unit_backups(backups: dict[str, Optional[str]]):
    for name, content in backups.items():
        path = _unit_path(name)
        if content is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write_text(path, content, mode=0o600)


def _remove_unit_files(unit_names: list[str]):
    for name in unit_names:
        _unit_path(name).unlink(missing_ok=True)
