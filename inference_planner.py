import json
import re
import shlex
import socket
from pathlib import Path
from typing import Optional

import inference_launchers
import model_storage
import services
import system
from node_config import get_node_config


CONFIG_DIR = Path.home() / ".config" / "inframatik"
INFERENCE_PROFILES_FILE = CONFIG_DIR / "inference_profiles.json"
SCHEMA_VERSION = 1
DEFAULT_PORT_RANGE = (10000, 10999)

_PROFILE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|api[_-]?key|credential|auth|bearer)", re.IGNORECASE)
_TEXT_CONTROL_RE = re.compile(r"[\x00\r\n]")
_RUNNING_STATES = {"running", "starting", "restarting", "active"}


def preview_profile(body: dict, existing_profile_id: Optional[str] = None) -> dict:
    """Validate and render an inference profile draft without side effects."""
    blockers = []
    warnings = []
    if not isinstance(body, dict):
        return _response({}, None, blockers=[_blocker("", "Profile preview body must be an object")], warnings=warnings)

    profile = body.get("profile") if isinstance(body.get("profile"), dict) else body
    existing_profile_id = existing_profile_id or body.get("existing_profile_id") or profile.get("existing_profile_id")
    profile_id = _profile_id(profile, existing_profile_id, blockers)
    common = _object(profile.get("common"), "common", blockers)
    deployment = _object(profile.get("deployment"), "deployment", blockers)
    engine_config = _object(profile.get("engine_config"), "engine_config", blockers)
    advanced = _object(profile.get("advanced"), "advanced", blockers)
    exposure = _object(profile.get("exposure"), "exposure", blockers)

    engine = _normalize_engine(profile.get("engine"), blockers)
    launcher = _resolve_launcher(profile, engine, blockers)
    model = _resolve_model(profile, blockers)

    raw_args = _normalize_args(advanced.get("args", profile.get("raw_args")), "advanced.args", blockers)
    profile_env = _normalize_env(advanced.get("env", profile.get("env")), "advanced.env", blockers)

    existing_profiles = _load_profiles()
    existing_profile = existing_profiles.get(existing_profile_id) if existing_profile_id else None
    exposure_plan, host = _plan_exposure(exposure, common, deployment, blockers, warnings)
    port_plan, ports = _plan_ports(profile_id, common, deployment, host, existing_profile_id, existing_profiles, blockers, warnings)
    gpu_plan, gpu_assignments = _plan_gpus(common, deployment, existing_profile_id, existing_profiles, blockers, warnings)

    resolved_instances = []
    for index, port in enumerate(ports):
        gpu_ids = gpu_assignments[index]["gpu_ids"] if index < len(gpu_assignments) else []
        resolved_instances.append({
            "index": index,
            "host": host,
            "port": port,
            "gpu_ids": gpu_ids,
            "unit": _unit_name(profile_id, index, len(ports)),
        })

    command_items = []
    units = []
    if launcher and model and engine and resolved_instances:
        for instance in resolved_instances:
            command = _render_command(
                engine=engine,
                launcher=launcher,
                model=model,
                profile=profile,
                common=common,
                engine_config=engine_config,
                advanced_args=raw_args,
                profile_env=profile_env,
                instance=instance,
                blockers=blockers,
                warnings=warnings,
            )
            command_items.append(command)
            units.append({
                "index": instance["index"],
                "name": instance["unit"],
                "content": _render_unit(profile_id, instance, command),
            })

    restart_required = _restart_required(existing_profile, profile)
    return _response(
        profile,
        profile_id,
        blockers=blockers,
        warnings=warnings,
        resolved_instances=resolved_instances,
        port_plan=port_plan,
        gpu_plan=gpu_plan,
        command_preview=command_items,
        systemd_preview={"units": units},
        cloudflare_plan=exposure_plan,
        restart_required=restart_required,
        engine=engine,
    )


def _response(
    profile: dict,
    profile_id: Optional[str],
    blockers: list,
    warnings: list,
    resolved_instances: Optional[list] = None,
    port_plan: Optional[dict] = None,
    gpu_plan: Optional[dict] = None,
    command_preview: Optional[list] = None,
    systemd_preview: Optional[dict] = None,
    cloudflare_plan: Optional[dict] = None,
    restart_required: Optional[dict] = None,
    engine: Optional[str] = None,
) -> dict:
    valid = not blockers
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "engine": engine,
        "valid": valid,
        "valid_for_save": valid,
        "blockers": blockers,
        "warnings": warnings,
        "resolved_instances": resolved_instances or [],
        "port_plan": port_plan or {"mode": None, "range": "inference", "allocated": [], "persisted": False},
        "gpu_plan": gpu_plan or {"mode": None, "claim_mode": "exclusive", "assignments": []},
        "command_preview": command_preview or [],
        "systemd_preview": systemd_preview or {"units": []},
        "cloudflare_plan": cloudflare_plan or {"would_provision": False, "resources": []},
        "restart_required": restart_required or {"required": False, "fields": []},
    }


def _blocker(field: str, message: str) -> dict:
    return {"field": field, "message": message}


def _warning(field: str, message: str) -> dict:
    return {"field": field, "message": message}


def _object(value, field: str, blockers: list) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        blockers.append(_blocker(field, f"{field} must be an object"))
        return {}
    return dict(value)


def _profile_id(profile: dict, existing_profile_id: Optional[str], blockers: list) -> str:
    raw = (
        profile.get("id")
        or existing_profile_id
        or profile.get("display_name")
        or (profile.get("common") or {}).get("served_model_name")
        or "inference-profile"
    )
    slug = _slugify(raw)
    if not _PROFILE_ID_RE.fullmatch(slug):
        blockers.append(_blocker("id", "Profile ID must be lowercase alphanumeric, hyphen, or underscore, and 64 characters or less"))
        return "inference-profile"
    return slug


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-_")
    slug = re.sub(r"-+", "-", slug)[:64].strip("-_")
    return slug or "inference-profile"


def _normalize_engine(value, blockers: list) -> Optional[str]:
    if not value:
        blockers.append(_blocker("engine", "Engine is required"))
        return None
    try:
        return inference_launchers.normalize_engine(str(value))
    except inference_launchers.LauncherError as e:
        blockers.append(_blocker("engine", str(e.detail)))
        return None


def _resolve_launcher(profile: dict, engine: Optional[str], blockers: list) -> Optional[dict]:
    launcher_id = (
        profile.get("engine_launcher_id")
        or profile.get("launcher_id")
        or ((profile.get("engine_launcher") or {}) if isinstance(profile.get("engine_launcher"), dict) else {}).get("id")
    )
    if not launcher_id:
        blockers.append(_blocker("engine_launcher_id", "Engine launcher is required"))
        return None
    try:
        launcher = inference_launchers.get_launcher(str(launcher_id), include_secret_env=True)
    except inference_launchers.LauncherError as e:
        blockers.append(_blocker("engine_launcher_id", str(e.detail)))
        return None
    if engine and launcher.get("engine") != engine:
        blockers.append(_blocker("engine_launcher_id", f"Launcher engine {launcher.get('engine')} does not match profile engine {engine}"))
    try:
        validation = inference_launchers.validate_launcher_path(str(launcher_id))
        if not validation.get("valid"):
            for error in validation.get("errors") or ["Launcher path is invalid"]:
                blockers.append(_blocker("engine_launcher_id", error))
    except inference_launchers.LauncherError as e:
        blockers.append(_blocker("engine_launcher_id", str(e.detail)))
    return launcher


def _resolve_model(profile: dict, blockers: list) -> Optional[dict]:
    model_ref = profile.get("model_ref") or profile.get("model")
    if isinstance(model_ref, str):
        model_ref = {"artifact_id": model_ref}
    if not isinstance(model_ref, dict):
        blockers.append(_blocker("model_ref", "Model artifact reference is required"))
        return None
    artifact_id = model_ref.get("artifact_id") or model_ref.get("id")
    if not artifact_id:
        blockers.append(_blocker("model_ref.artifact_id", "Model artifact ID is required"))
        return None
    try:
        return model_storage.resolve_model_runtime(str(artifact_id), snapshot=model_ref.get("snapshot"))
    except model_storage.ModelStorageError as e:
        blockers.append(_blocker("model_ref", str(e.detail)))
        return None


def _normalize_args(value, field: str, blockers: list) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        blockers.append(_blocker(field, "Raw args must be an ordered list of argv tokens"))
        return []
    args = []
    for index, item in enumerate(value):
        token = str(item)
        if token == "" or _TEXT_CONTROL_RE.search(token):
            blockers.append(_blocker(f"{field}.{index}", "Raw args cannot be empty or contain NUL/newline characters"))
            continue
        args.append(token)
    return args


def _normalize_env(value, field: str, blockers: list) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        blockers.append(_blocker(field, "Env must be an object of key/value pairs"))
        return {}
    env = {}
    for key, raw_value in value.items():
        name = str(key).strip()
        text = str(raw_value)
        if not _ENV_KEY_RE.fullmatch(name):
            blockers.append(_blocker(f"{field}.{key}", f"Invalid env key: {key}"))
            continue
        if _TEXT_CONTROL_RE.search(text):
            blockers.append(_blocker(f"{field}.{name}", "Env values cannot contain NUL or newline characters"))
            continue
        env[name] = text
    return env


def _load_profiles() -> dict:
    if not INFERENCE_PROFILES_FILE.exists():
        return {}
    try:
        data = json.loads(INFERENCE_PROFILES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    profiles = data.get("profiles", data if isinstance(data, dict) else {})
    return profiles if isinstance(profiles, dict) else {}


def _node_config() -> dict:
    return get_node_config() or {}


def _port_range() -> tuple[int, int]:
    config = _node_config()
    raw = (config.get("port_ranges") or {}).get("inference") if isinstance(config.get("port_ranges"), dict) else None
    raw = raw or config.get("inference_port_range")
    if isinstance(raw, str) and "-" in raw:
        start, end = raw.split("-", 1)
        try:
            return _valid_port_range(int(start), int(end))
        except ValueError:
            return DEFAULT_PORT_RANGE
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return _valid_port_range(int(raw[0]), int(raw[1]))
        except ValueError:
            return DEFAULT_PORT_RANGE
    if isinstance(raw, dict):
        try:
            return _valid_port_range(int(raw.get("start")), int(raw.get("end")))
        except (TypeError, ValueError):
            return DEFAULT_PORT_RANGE
    return DEFAULT_PORT_RANGE


def _valid_port_range(start: int, end: int) -> tuple[int, int]:
    if start < 1 or end > 65535 or start > end:
        raise ValueError("invalid port range")
    return start, end


def _plan_ports(
    profile_id: str,
    common: dict,
    deployment: dict,
    host: str,
    existing_profile_id: Optional[str],
    profiles: dict,
    blockers: list,
    warnings: list,
) -> tuple[dict, list[int]]:
    replicas = _replicas(deployment, blockers)
    policy = deployment.get("port_policy") if isinstance(deployment.get("port_policy"), dict) else {}
    start, end = _port_range()
    used = _used_ports(existing_profile_id, profiles)
    mode = str(policy.get("mode") or ("explicit" if common.get("port") is not None or policy.get("ports") else "auto")).lower()
    allocated = []
    collisions = []

    if mode == "explicit":
        ports = policy.get("ports")
        if ports is None and common.get("ports") is not None:
            ports = common.get("ports")
        if ports is None and common.get("port") is not None:
            ports = [common.get("port")]
        if not isinstance(ports, list):
            blockers.append(_blocker("deployment.port_policy.ports", "Explicit port policy requires a ports list"))
            ports = []
        for index, value in enumerate(ports):
            port = _parse_port(value, f"deployment.port_policy.ports.{index}", blockers)
            if port is not None:
                allocated.append(port)
        if len(allocated) != replicas:
            blockers.append(_blocker("deployment.port_policy.ports", f"Explicit port count must match replicas ({replicas})"))
    elif mode in {"auto", "contiguous"}:
        contiguous = bool(policy.get("prefer_contiguous")) or mode == "contiguous"
        allocated = _allocate_ports(replicas, used, host, start, end, contiguous)
        if len(allocated) != replicas:
            blockers.append(_blocker("deployment.port_policy", "Could not allocate requested inference ports"))
    else:
        blockers.append(_blocker("deployment.port_policy.mode", "Port policy mode must be auto, contiguous, or explicit"))

    for port in allocated:
        if port < start or port > end:
            warnings.append(_warning("deployment.port_policy", f"Port {port} is outside the configured inference range {start}-{end}"))
        if port in used:
            collisions.append({"port": port, "reason": "already allocated"})
            blockers.append(_blocker("deployment.port_policy", f"Port {port} is already allocated"))
        elif not _port_is_bindable(host, port):
            collisions.append({"port": port, "reason": "already bound"})
            blockers.append(_blocker("deployment.port_policy", f"Port {port} is already bound on {host}"))

    return {
        "mode": mode,
        "range": "inference",
        "range_start": start,
        "range_end": end,
        "allocated": allocated,
        "collisions": collisions,
        "persisted": False,
    }, allocated


def _replicas(deployment: dict, blockers: list) -> int:
    mode = str(deployment.get("mode") or "single").lower()
    if mode not in {"single", "replicated"}:
        blockers.append(_blocker("deployment.mode", "Deployment mode must be single or replicated"))
        return 1
    raw = deployment.get("replicas", 1 if mode == "single" else None)
    if raw is None:
        blockers.append(_blocker("deployment.replicas", "Replicated deployment requires a replica count"))
        return 1
    try:
        replicas = int(raw)
    except (TypeError, ValueError):
        blockers.append(_blocker("deployment.replicas", "Replicas must be a positive integer"))
        return 1
    if mode == "single" and replicas != 1:
        blockers.append(_blocker("deployment.replicas", "Single deployment must have exactly one replica"))
        return 1
    if replicas < 1 or replicas > 128:
        blockers.append(_blocker("deployment.replicas", "Replicas must be between 1 and 128"))
        return 1
    return replicas


def _parse_port(value, field: str, blockers: list) -> Optional[int]:
    try:
        port = int(value)
    except (TypeError, ValueError):
        blockers.append(_blocker(field, "Port must be an integer"))
        return None
    if port < 1 or port > 65535:
        blockers.append(_blocker(field, "Port must be between 1 and 65535"))
        return None
    return port


def _used_ports(existing_profile_id: Optional[str], profiles: dict) -> set[int]:
    used = set()
    try:
        used.update(int(port) for port in services.get_allocated_ports())
    except Exception:
        pass
    for profile_id, profile in profiles.items():
        if existing_profile_id and profile_id == existing_profile_id:
            continue
        if not isinstance(profile, dict):
            continue
        for instance in profile.get("instances") or []:
            if isinstance(instance, dict) and instance.get("port") is not None:
                try:
                    used.add(int(instance["port"]))
                except (TypeError, ValueError):
                    pass
        common = profile.get("common") if isinstance(profile.get("common"), dict) else {}
        if common.get("port") is not None:
            try:
                used.add(int(common["port"]))
            except (TypeError, ValueError):
                pass
    return used


def _allocate_ports(count: int, used: set[int], host: str, start: int, end: int, contiguous: bool) -> list[int]:
    if contiguous:
        run = []
        for port in range(start, end + 1):
            if port in used or not _port_is_bindable(host, port):
                run = []
                continue
            run.append(port)
            if len(run) == count:
                return run
        return []
    allocated = []
    for port in range(start, end + 1):
        if port in used or not _port_is_bindable(host, port):
            continue
        allocated.append(port)
        if len(allocated) == count:
            break
    return allocated


def _port_is_bindable(host: str, port: int) -> bool:
    bind_host = "0.0.0.0" if host == "0.0.0.0" else "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, port))
            return True
        except OSError:
            return False


def _plan_gpus(
    common: dict,
    deployment: dict,
    existing_profile_id: Optional[str],
    profiles: dict,
    blockers: list,
    warnings: list,
) -> tuple[dict, list[dict]]:
    replicas = _replicas(deployment, blockers)
    policy = deployment.get("gpu_policy") if isinstance(deployment.get("gpu_policy"), dict) else {}
    mode = str(policy.get("mode") or "profile").lower()
    claim_mode = str(policy.get("claim_mode") or common.get("gpu_claim_mode") or "exclusive").lower()
    if claim_mode not in {"exclusive", "shared"}:
        blockers.append(_blocker("deployment.gpu_policy.claim_mode", "GPU claim mode must be exclusive or shared"))
        claim_mode = "exclusive"
    gpus = _available_gpus()
    available = sorted(gpus.keys())
    requested = _gpu_ids(policy.get("gpu_ids", common.get("gpu_ids")), "deployment.gpu_policy.gpu_ids", blockers)
    assignments = []

    if mode == "profile":
        gpu_ids = requested or []
        assignments = [{"index": index, "gpu_ids": list(gpu_ids)} for index in range(replicas)]
    elif mode == "one_per_instance":
        candidates = requested or available
        if len(candidates) < replicas:
            blockers.append(_blocker("deployment.gpu_policy", "one_per_instance requires at least one GPU per replica"))
        assignments = [{"index": index, "gpu_ids": [candidates[index]] if index < len(candidates) else []} for index in range(replicas)]
    elif mode == "contiguous_groups":
        group_size = _positive_int(policy.get("group_size") or common.get("tensor_parallel") or 1, "deployment.gpu_policy.group_size", blockers)
        candidates = requested or available
        if group_size < 1 or len(candidates) < replicas * group_size:
            blockers.append(_blocker("deployment.gpu_policy", "contiguous_groups cannot resolve the requested replica/GPU layout"))
        for index in range(replicas):
            start = index * max(group_size, 1)
            assignments.append({"index": index, "gpu_ids": candidates[start:start + max(group_size, 1)]})
    elif mode == "explicit":
        raw_instances = policy.get("instances") or policy.get("instance_gpu_ids") or []
        if not isinstance(raw_instances, list) or len(raw_instances) != replicas:
            blockers.append(_blocker("deployment.gpu_policy.instances", "Explicit GPU policy requires one GPU list per replica"))
        for index in range(replicas):
            raw = raw_instances[index] if isinstance(raw_instances, list) and index < len(raw_instances) else []
            if isinstance(raw, dict):
                raw = raw.get("gpu_ids")
            assignments.append({"index": index, "gpu_ids": _gpu_ids(raw, f"deployment.gpu_policy.instances.{index}.gpu_ids", blockers)})
    else:
        blockers.append(_blocker("deployment.gpu_policy.mode", "GPU policy mode must be profile, one_per_instance, contiguous_groups, or explicit"))
        assignments = [{"index": index, "gpu_ids": []} for index in range(replicas)]

    requested_all = {gpu_id for item in assignments for gpu_id in item["gpu_ids"]}
    if requested_all and not available:
        blockers.append(_blocker("deployment.gpu_policy.gpu_ids", "Requested GPU IDs cannot be validated because no GPUs were detected"))
    missing = sorted(gpu_id for gpu_id in requested_all if available and gpu_id not in gpus)
    if missing:
        blockers.append(_blocker("deployment.gpu_policy.gpu_ids", f"Requested GPU IDs do not exist: {', '.join(map(str, missing))}"))

    _check_gpu_conflicts(assignments, claim_mode, existing_profile_id, profiles, blockers, warnings)
    return {
        "mode": mode,
        "claim_mode": claim_mode,
        "available": [
            {
                "id": gpu_id,
                "name": info.get("name"),
                "mem_total_mb": info.get("mem_total_mb"),
                "mem_used_mb": info.get("mem_used_mb"),
            }
            for gpu_id, info in sorted(gpus.items())
        ],
        "assignments": assignments,
    }, assignments


def _available_gpus() -> dict[int, dict]:
    try:
        metrics = system.get_system_metrics()
    except Exception:
        return {}
    result = {}
    for index, gpu in enumerate(metrics.get("gpus") or []):
        if not isinstance(gpu, dict):
            continue
        try:
            gpu_id = int(gpu.get("index", index))
        except (TypeError, ValueError):
            gpu_id = index
        result[gpu_id] = gpu
    return result


def _gpu_ids(value, field: str, blockers: list) -> list[int]:
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        blockers.append(_blocker(field, "GPU IDs must be a list of integers"))
        return []
    result = []
    for index, raw in enumerate(value):
        try:
            gpu_id = int(raw)
        except (TypeError, ValueError):
            blockers.append(_blocker(f"{field}.{index}", "GPU ID must be an integer"))
            continue
        if gpu_id < 0:
            blockers.append(_blocker(f"{field}.{index}", "GPU ID cannot be negative"))
            continue
        result.append(gpu_id)
    return result


def _positive_int(value, field: str, blockers: list, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        blockers.append(_blocker(field, "Value must be an integer"))
        return minimum
    if parsed < minimum:
        blockers.append(_blocker(field, f"Value must be at least {minimum}"))
        return minimum
    return parsed


def _check_gpu_conflicts(assignments: list[dict], claim_mode: str, existing_profile_id: Optional[str], profiles: dict, blockers: list, warnings: list):
    requested = {gpu_id for assignment in assignments for gpu_id in assignment.get("gpu_ids", [])}
    if not requested:
        return
    for profile_id, profile in profiles.items():
        if existing_profile_id and profile_id == existing_profile_id:
            continue
        if not isinstance(profile, dict):
            continue
        state = str(profile.get("state") or profile.get("status") or profile.get("desired_state") or "").lower()
        if state not in _RUNNING_STATES:
            continue
        other_claim = (((profile.get("deployment") or {}).get("gpu_policy") or {}).get("claim_mode") or "exclusive").lower()
        other_gpus = {
            int(gpu_id)
            for instance in profile.get("instances") or []
            if isinstance(instance, dict)
            for gpu_id in instance.get("gpu_ids") or []
        }
        overlap = sorted(requested & other_gpus)
        if not overlap:
            continue
        message = f"GPU overlap with running profile {profile_id}: {', '.join(map(str, overlap))}"
        if claim_mode == "exclusive" or other_claim == "exclusive":
            blockers.append(_blocker("deployment.gpu_policy", message))
        else:
            warnings.append(_warning("deployment.gpu_policy", message))


def _plan_exposure(exposure: dict, common: dict, deployment: dict, blockers: list, warnings: list) -> tuple[dict, str]:
    mode = str(exposure.get("mode") or "local").lower()
    if mode not in {"local", "lan", "cloudflare"}:
        blockers.append(_blocker("exposure.mode", "Exposure mode must be local, lan, or cloudflare"))
        mode = "local"
    host = common.get("host")
    if host is not None and (not isinstance(host, str) or _TEXT_CONTROL_RE.search(host)):
        blockers.append(_blocker("common.host", "Host must be a single-line string"))
        host = None
    if mode == "local":
        host = host or "127.0.0.1"
        if host not in {"127.0.0.1", "localhost"}:
            blockers.append(_blocker("common.host", "Local exposure must bind to 127.0.0.1 or localhost"))
    elif mode == "lan":
        host = host or "0.0.0.0"
    else:
        host = host or "127.0.0.1"
        if host not in {"127.0.0.1", "localhost"}:
            blockers.append(_blocker("common.host", "Cloudflare exposure must bind to a local host"))
    if mode in {"lan", "cloudflare"} and not _has_engine_api_key(common):
        warnings.append(_warning("common.api_key", "Endpoint is exposed beyond local-only mode without an engine API key"))

    plan = {
        "mode": mode,
        "hostname": exposure.get("hostname"),
        "would_provision": mode == "cloudflare",
        "resources": [],
        "warnings": [],
        "blockers": [],
    }
    if mode == "cloudflare":
        hostname = str(exposure.get("hostname") or "").strip()
        replicas = _replicas(deployment, blockers)
        if replicas != 1:
            message = "Cloudflare exposure for replicated inference profiles is deferred"
            blockers.append(_blocker("exposure.mode", message))
            plan["blockers"].append(message)
        if not hostname:
            message = "Cloudflare exposure requires a hostname"
            blockers.append(_blocker("exposure.hostname", message))
            plan["blockers"].append(message)
        config = _node_config()
        missing = [key for key in ("cf_token", "cf_account_id", "cf_zone_id") if not config.get(key)]
        if missing:
            message = "Cloudflare exposure requires local Cloudflare configuration"
            blockers.append(_blocker("exposure.mode", message))
            plan["blockers"].append(message)
        plan["resources"] = [
            {"kind": "tunnel_route", "hostname": hostname or None},
            {"kind": "dns_record", "hostname": hostname or None},
            {"kind": "access_application", "hostname": hostname or None},
        ]
        if not exposure.get("service_auth_policy_id") and not exposure.get("access_policy_id"):
            message = "Cloudflare exposure has no Access Service Auth policy selected"
            warnings.append(_warning("exposure.service_auth_policy_id", message))
            plan["warnings"].append(message)
        plan["resources"].append({"kind": "access_service_token", "secret": "generated_on_save"})
    return plan, host


def _has_engine_api_key(common: dict) -> bool:
    return bool(common.get("api_key") or common.get("api_key_enabled") or common.get("engine_api_key"))


def _render_command(
    engine: str,
    launcher: dict,
    model: dict,
    profile: dict,
    common: dict,
    engine_config: dict,
    advanced_args: list[str],
    profile_env: dict,
    instance: dict,
    blockers: list,
    warnings: list,
) -> dict:
    argv = [launcher["executable"], *(launcher.get("base_args") or [])]
    model_path = model["runtime_path"]
    block = _engine_block(engine_config, engine)
    if engine == "vllm":
        argv = _render_vllm(argv, model_path, common, block, instance, blockers, warnings)
    elif engine == "sglang":
        argv = _render_sglang(argv, model_path, common, block, instance, blockers, warnings)
    elif engine == "llama.cpp":
        argv = _render_llama_cpp(argv, model_path, common, block, instance, blockers, warnings)
    argv.extend(advanced_args)

    env = {}
    env.update(launcher.get("env") or {})
    env.update(profile_env)
    gpu_ids = instance.get("gpu_ids") or []
    if gpu_ids:
        generated = ",".join(str(gpu_id) for gpu_id in gpu_ids)
        if "CUDA_VISIBLE_DEVICES" in env and env["CUDA_VISIBLE_DEVICES"] != generated:
            warnings.append(_warning("advanced.env.CUDA_VISIBLE_DEVICES", "Profile env overrides inframatik GPU placement"))
        else:
            env["CUDA_VISIBLE_DEVICES"] = generated

    redacted = _redact_env(env)
    return {
        "index": instance["index"],
        "argv": argv,
        "env": redacted["env"],
        "redacted_env_keys": redacted["redacted_env_keys"],
        "working_dir": launcher.get("working_dir"),
    }


def _engine_block(engine_config: dict, engine: str) -> dict:
    keys = {
        "llama.cpp": ("llama_cpp", "llama.cpp", "llamacpp"),
        "vllm": ("vllm",),
        "sglang": ("sglang",),
    }[engine]
    for key in keys:
        if isinstance(engine_config.get(key), dict):
            return dict(engine_config[key])
    known = {"llama_cpp", "llama.cpp", "llamacpp", "vllm", "sglang"}
    return dict(engine_config) if not any(key in engine_config for key in known) else {}


def _append(argv: list[str], flag: str, value):
    if value is None or value == "":
        return
    argv.extend([flag, str(value)])


def _append_bool(argv: list[str], flag: str, value):
    if value is True:
        argv.append(flag)


def _append_json(argv: list[str], flag: str, value):
    if value:
        argv.extend([flag, json.dumps(value, sort_keys=True)])


def _common_value(common: dict, *names):
    for name in names:
        if common.get(name) is not None:
            return common.get(name)
    return None


def _render_common_openai_flags(argv: list[str], common: dict, instance: dict, served_flag: str = "--served-model-name"):
    _append(argv, "--host", instance["host"])
    _append(argv, "--port", instance["port"])
    _append(argv, served_flag, common.get("served_model_name"))
    api_key = common.get("api_key") or common.get("engine_api_key")
    if api_key:
        _append(argv, "--api-key", "<redacted>")


def _render_vllm(argv: list[str], model_path: str, common: dict, cfg: dict, instance: dict, blockers: list, warnings: list) -> list[str]:
    base = argv[1:]
    if "serve" in base:
        argv.append(model_path)
    elif any("vllm.entrypoints.openai.api_server" in token for token in base):
        argv.extend(["--model", model_path])
    else:
        argv.extend(["serve", model_path])
    _render_common_openai_flags(argv, common, instance)
    _append(argv, "--max-model-len", common.get("context_length"))
    _append(argv, "--dtype", common.get("dtype"))
    _append(argv, "--quantization", common.get("quantization"))
    _append(argv, "--kv-cache-dtype", common.get("kv_cache_dtype"))
    _append(argv, "--kv-cache-memory-bytes", common.get("kv_cache_memory_bytes"))
    _append(argv, "--gpu-memory-utilization", common.get("gpu_memory_utilization"))
    _append(argv, "--cpu-offload-gb", common.get("cpu_offload_gb"))
    _append(argv, "--tensor-parallel-size", common.get("tensor_parallel"))
    _append(argv, "--pipeline-parallel-size", common.get("pipeline_parallel"))
    _append(argv, "--data-parallel-size", common.get("data_parallel"))
    _append(argv, "--max-num-seqs", common.get("max_concurrent_requests"))
    _append(argv, "--max-num-batched-tokens", common.get("max_batch_tokens"))
    _append_bool(argv, "--trust-remote-code", common.get("trust_remote_code"))
    _append_bool(argv, "--enable-prefix-caching", common.get("enable_prefix_caching"))
    _append(argv, "--reasoning-parser", common.get("reasoning_parser"))
    _append(argv, "--tool-call-parser", common.get("tool_call_parser"))
    _append_bool(argv, "--enable-auto-tool-choice", common.get("enable_auto_tool_choice"))
    _append(argv, "--chat-template", common.get("chat_template"))
    _append(argv, "--log-level", common.get("log_level"))

    expert = common.get("expert_parallel")
    if expert is True or isinstance(expert, (int, dict)) and expert:
        argv.append("--enable-expert-parallel")
    context = common.get("context_parallel") if isinstance(common.get("context_parallel"), dict) else {}
    speculative = common.get("speculative") if isinstance(common.get("speculative"), dict) else {}
    lora = common.get("lora") if isinstance(common.get("lora"), dict) else {}

    _append(argv, "--load-format", cfg.get("load_format"))
    _append(argv, "--distributed-executor-backend", cfg.get("distributed_executor_backend"))
    _append(argv, "--data-parallel-size-local", cfg.get("data_parallel_size_local"))
    _append(argv, "--data-parallel-start-rank", cfg.get("data_parallel_start_rank"))
    _append(argv, "--data-parallel-address", cfg.get("data_parallel_address"))
    _append(argv, "--data-parallel-rpc-port", cfg.get("data_parallel_rpc_port"))
    _append(argv, "--api-server-count", cfg.get("api_server_count"))
    _append(argv, "--decode-context-parallel-size", cfg.get("decode_context_parallel_size") or context.get("decode_size"))
    _append(argv, "--prefill-context-parallel-size", cfg.get("prefill_context_parallel_size") or context.get("prefill_size"))
    _append(argv, "--context-parallel-backend", cfg.get("context_parallel_backend") or context.get("backend"))
    _append_bool(argv, "--enable-expert-parallel", cfg.get("enable_expert_parallel"))
    _append_bool(argv, "--enable-ep-weight-filter", cfg.get("enable_ep_weight_filter"))
    _append(argv, "--all2all-backend", cfg.get("all2all_backend"))
    _append_bool(argv, "--enable-eplb", cfg.get("enable_eplb"))
    _append_json(argv, "--eplb-config", cfg.get("eplb_config"))
    _append(argv, "--expert-placement-strategy", cfg.get("expert_placement_strategy"))
    _append_bool(argv, "--enable-dbo", cfg.get("enable_dbo"))
    _append(argv, "--kv-offloading-size", cfg.get("kv_offloading_size"))
    _append(argv, "--kv-offloading-backend", cfg.get("kv_offloading_backend"))
    _append(argv, "--offload-backend", cfg.get("offload_backend"))
    _append(argv, "--max-num-partial-prefills", cfg.get("max_num_partial_prefills"))
    _append(argv, "--max-long-partial-prefills", cfg.get("max_long_partial_prefills"))
    _append(argv, "--long-prefill-token-threshold", cfg.get("long_prefill_token_threshold"))
    _append(argv, "--scheduling-policy", cfg.get("scheduling_policy"))
    _append_json(argv, "--compilation-config", cfg.get("compilation_config"))
    _append_json(argv, "--attention-config", cfg.get("attention_config"))
    _append(argv, "--moe-backend", cfg.get("moe_backend"))
    _append(argv, "--linear-backend", cfg.get("linear_backend"))
    _append(argv, "--chat-template-content-format", cfg.get("chat_template_content_format"))
    _append(argv, "--reasoning-parser-plugin", cfg.get("reasoning_parser_plugin"))
    _append(argv, "--tool-parser-plugin", cfg.get("tool_parser_plugin"))
    _append(argv, "--speculative-model", speculative.get("model"))
    _append(argv, "--num-speculative-tokens", speculative.get("num_tokens"))
    _append_bool(argv, "--enable-lora", lora.get("enabled") or bool(lora.get("paths")))
    _append_json(argv, "--lora-modules", lora.get("paths"))
    return argv


def _render_sglang(argv: list[str], model_path: str, common: dict, cfg: dict, instance: dict, blockers: list, warnings: list) -> list[str]:
    argv.extend(["--model-path", model_path])
    _render_common_openai_flags(argv, common, instance)
    _append(argv, "--context-length", common.get("context_length"))
    _append(argv, "--dtype", common.get("dtype"))
    _append(argv, "--quantization", common.get("quantization"))
    _append(argv, "--kv-cache-dtype", common.get("kv_cache_dtype"))
    _append(argv, "--mem-fraction-static", common.get("gpu_memory_utilization"))
    _append(argv, "--cpu-offload-gb", common.get("cpu_offload_gb"))
    _append(argv, "--tp-size", common.get("tensor_parallel"))
    _append(argv, "--pp-size", common.get("pipeline_parallel"))
    _append(argv, "--dp-size", common.get("data_parallel"))
    _append(argv, "--max-running-requests", common.get("max_concurrent_requests"))
    _append(argv, "--max-total-tokens", common.get("max_batch_tokens"))
    _append(argv, "--max-prefill-tokens", common.get("max_prefill_tokens"))
    _append_bool(argv, "--trust-remote-code", common.get("trust_remote_code"))
    _append(argv, "--reasoning-parser", common.get("reasoning_parser"))
    _append(argv, "--tool-call-parser", common.get("tool_call_parser"))
    _append(argv, "--chat-template", common.get("chat_template"))
    _append(argv, "--log-level", common.get("log_level"))

    expert = common.get("expert_parallel")
    if isinstance(expert, int):
        _append(argv, "--ep-size", expert)
    elif isinstance(expert, dict):
        _append(argv, "--ep-size", expert.get("size"))
    context = common.get("context_parallel") if isinstance(common.get("context_parallel"), dict) else {}
    speculative = common.get("speculative") if isinstance(common.get("speculative"), dict) else {}
    lora = common.get("lora") if isinstance(common.get("lora"), dict) else {}

    _append(argv, "--load-format", cfg.get("load_format"))
    _append(argv, "--page-size", cfg.get("page_size"))
    _append(argv, "--ep-size", cfg.get("ep_size"))
    _append_bool(argv, "--enable-dp-attention", cfg.get("enable_dp_attention"))
    _append(argv, "--load-balance-method", cfg.get("load_balance_method"))
    _append(argv, "--moe-a2a-backend", cfg.get("moe_a2a_backend"))
    _append(argv, "--moe-runner-backend", cfg.get("moe_runner_backend"))
    _append(argv, "--attn-cp-size", cfg.get("attn_cp_size") or context.get("attn_cp_size"))
    _append_bool(argv, "--enable-dsa-prefill-context-parallel", cfg.get("enable_dsa_prefill_context_parallel"))
    _append(argv, "--dsa-prefill-cp-mode", cfg.get("dsa_prefill_cp_mode"))
    _append(argv, "--chunked-prefill-size", cfg.get("chunked_prefill_size"))
    _append(argv, "--torchao-config", cfg.get("torchao_config"))
    _append_json(argv, "--sampling-defaults", cfg.get("sampling_defaults"))
    _append_json(argv, "--cuda-graph-config", cfg.get("cuda_graph_config"))
    _append_json(argv, "--hicache-config", cfg.get("hicache"))
    _append(argv, "--grammar-backend", cfg.get("grammar_backend"))
    _append(argv, "--speculative-draft-model-path", speculative.get("model"))
    _append(argv, "--speculative-num-steps", speculative.get("num_tokens"))
    _append_bool(argv, "--enable-lora", lora.get("enabled") or bool(lora.get("paths")))
    _append_json(argv, "--lora-paths", lora.get("paths"))
    return argv


def _render_llama_cpp(argv: list[str], model_path: str, common: dict, cfg: dict, instance: dict, blockers: list, warnings: list) -> list[str]:
    argv.extend(["--model", model_path])
    _append(argv, "--host", instance["host"])
    _append(argv, "--port", instance["port"])
    _append(argv, "--alias", common.get("served_model_name"))
    api_key = common.get("api_key") or common.get("engine_api_key")
    if api_key:
        _append(argv, "--api-key", "<redacted>")
    _append(argv, "--ctx-size", common.get("context_length"))
    _append(argv, "--parallel", common.get("max_concurrent_requests"))
    _append(argv, "--batch-size", common.get("max_batch_tokens"))
    _append(argv, "--chat-template", common.get("chat_template"))
    _append(argv, "--reasoning-format", common.get("reasoning_parser"))
    _append(argv, "--n-gpu-layers", cfg.get("n_gpu_layers"))
    _append(argv, "--main-gpu", cfg.get("main_gpu"))
    _append(argv, "--split-mode", cfg.get("split_mode"))
    tensor_split = cfg.get("tensor_split")
    if isinstance(tensor_split, list):
        tensor_split = ",".join(str(item) for item in tensor_split)
    _append(argv, "--tensor-split", tensor_split)
    _append(argv, "--threads", cfg.get("threads"))
    _append(argv, "--threads-batch", cfg.get("threads_batch"))
    _append(argv, "--batch-size", cfg.get("batch_size"))
    _append(argv, "--ubatch-size", cfg.get("ubatch_size"))
    _append_bool(argv, "--flash-attn", cfg.get("flash_attention"))
    _append(argv, "--cache-type-k", cfg.get("cache_type_k") or common.get("kv_cache_dtype"))
    _append(argv, "--cache-type-v", cfg.get("cache_type_v") or common.get("kv_cache_dtype"))
    _append(argv, "--mmproj", cfg.get("mmproj_ref"))
    return argv


def _redact_env(env: dict) -> dict:
    redacted = {}
    redacted_keys = []
    for key, value in sorted(env.items()):
        if _SECRET_KEY_RE.search(key):
            redacted[key] = "<redacted>"
            redacted_keys.append(key)
        else:
            redacted[key] = value
    return {"env": redacted, "redacted_env_keys": redacted_keys}


def _unit_name(profile_id: str, index: int, count: int) -> str:
    safe = _slugify(profile_id)
    if count == 1:
        return f"infra-llm-{safe}.service"
    return f"infra-llm-{safe}@{index}.service"


def _render_unit(profile_id: str, instance: dict, command: dict) -> str:
    argv = command.get("argv") or []
    working_dir = command.get("working_dir") or str(Path.home())
    env_lines = []
    for key, value in sorted((command.get("env") or {}).items()):
        env_lines.append(f'Environment="{_systemd_escape(key)}={_systemd_escape(str(value))}"')
    env_block = "\n".join(env_lines)
    if env_block:
        env_block += "\n"
    return (
        "[Unit]\n"
        f"Description=infra llm: {profile_id} instance {instance['index']}\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_systemd_escape_arg(working_dir)}\n"
        f"{env_block}"
        f"ExecStart={' '.join(_systemd_escape_arg(arg) for arg in argv)}\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_escape(value: str) -> str:
    return value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')


def _systemd_escape_arg(value: str) -> str:
    return shlex.quote(str(value).replace("%", "%%"))


def _restart_required(existing_profile: Optional[dict], draft: dict) -> dict:
    if not existing_profile:
        return {"required": False, "fields": []}
    fields = []
    for field in ("engine", "engine_launcher_id", "model", "model_ref", "common", "engine_config", "advanced", "deployment", "exposure"):
        if existing_profile.get(field) != draft.get(field):
            fields.append(field)
    state = str(existing_profile.get("state") or existing_profile.get("status") or "").lower()
    return {"required": bool(fields and state in _RUNNING_STATES), "fields": fields}
