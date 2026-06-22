import asyncio
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

import inference_profiles


CONFIG_DIR = Path.home() / ".config" / "inframatik"
INFERENCE_OPERATIONS_FILE = CONFIG_DIR / "inference_operations.json"
SCHEMA_VERSION = 1
TERMINAL_STATES = {"succeeded", "failed", "failed_interrupted", "canceled"}
ACTIVE_STATES = {"queued", "running"}
DEFAULT_STARTUP_GRACE_SECONDS = 600
POLL_INTERVAL_SECONDS = 0.5
MAX_OPERATION_RECORDS = 100
STARTUP_RESTART_FAILURE_THRESHOLD = 3
STARTUP_LOG_LINES = 80
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:secret|token|password|passwd|api[_-]?key|credential|auth|bearer)[A-Za-z0-9_]*)=([^\s]+)"
)

_lock = threading.RLock()
_tasks: dict[str, asyncio.Task] = {}


class OperationError(ValueError):
    status_code = 400

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class OperationNotFoundError(OperationError):
    status_code = 404


class OperationConflictError(OperationError):
    status_code = 409


def _now() -> int:
    return int(time.time())


def _empty_registry() -> dict:
    return {"schema_version": SCHEMA_VERSION, "operations": {}}


def _atomic_write_json(path: Path, data: dict, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _load_registry() -> dict:
    if not INFERENCE_OPERATIONS_FILE.exists():
        return _empty_registry()
    try:
        data = json.loads(INFERENCE_OPERATIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise OperationError(f"Invalid inference operation registry {INFERENCE_OPERATIONS_FILE}: {e}") from e
    if not isinstance(data, dict):
        raise OperationError("Inference operation registry root must be an object")
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("operations", {})
    return data


def _save_registry(data: dict):
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("operations", {})
    _prune_registry(data)
    _atomic_write_json(INFERENCE_OPERATIONS_FILE, data)


def initialize_operations_registry():
    with _lock:
        if not INFERENCE_OPERATIONS_FILE.exists():
            _save_registry(_empty_registry())


def mark_interrupted_operations() -> dict:
    initialize_operations_registry()
    with _lock:
        registry = _load_registry()
        interrupted = []
        now = _now()
        for operation in registry.get("operations", {}).values():
            if operation.get("state") in ACTIVE_STATES:
                operation["state"] = "failed_interrupted"
                operation["finished_at"] = now
                operation["updated_at"] = now
                operation["error"] = "inframatik restarted before this operation completed"
                operation["current_step"] = "interrupted"
                interrupted.append(operation["id"])
        if interrupted:
            _save_registry(registry)
            for operation_id in interrupted:
                _publish_operation(registry["operations"][operation_id])
        return {"interrupted": interrupted}


def list_operations(profile_id: Optional[str] = None, state: Optional[str] = None) -> dict:
    initialize_operations_registry()
    with _lock:
        registry = _load_registry()
        operations = list(registry.get("operations", {}).values())
    if profile_id:
        operations = [item for item in operations if item.get("profile_id") == profile_id]
    if state:
        operations = [item for item in operations if item.get("state") == state]
    operations.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "operations_path": str(INFERENCE_OPERATIONS_FILE),
        "operations": operations,
    }


def get_operation(operation_id: str) -> dict:
    initialize_operations_registry()
    with _lock:
        operation = _load_registry().get("operations", {}).get(operation_id)
        if not operation:
            raise OperationNotFoundError(f"Inference operation not found: {operation_id}")
        return dict(operation)


def cancel_operation(operation_id: str) -> dict:
    initialize_operations_registry()
    with _lock:
        registry = _load_registry()
        operation = registry.get("operations", {}).get(operation_id)
        if not operation:
            raise OperationNotFoundError(f"Inference operation not found: {operation_id}")
        if operation.get("state") != "queued":
            raise OperationConflictError("Only queued inference operations can be canceled")
        now = _now()
        operation.update({"state": "canceled", "finished_at": now, "updated_at": now, "current_step": "canceled"})
        registry["operations"][operation_id] = operation
        _save_registry(registry)
        _publish_operation(operation)
        return dict(operation)


async def start_profile(profile_id: str) -> dict:
    return await _enqueue_operation("profile_start", profile_id)


async def stop_profile(profile_id: str) -> dict:
    return await _enqueue_operation("profile_stop", profile_id)


async def restart_profile(profile_id: str) -> dict:
    return await _enqueue_operation("profile_restart", profile_id)


async def start_instance(profile_id: str, instance_index: int) -> dict:
    return await _enqueue_operation("instance_start", profile_id, instance_index=instance_index)


async def stop_instance(profile_id: str, instance_index: int) -> dict:
    return await _enqueue_operation("instance_stop", profile_id, instance_index=instance_index)


async def restart_instance(profile_id: str, instance_index: int) -> dict:
    return await _enqueue_operation("instance_restart", profile_id, instance_index=instance_index)


async def _enqueue_operation(kind: str, profile_id: str, instance_index: Optional[int] = None) -> dict:
    initialize_operations_registry()
    operation = _create_operation(kind, profile_id, instance_index=instance_index)
    task = asyncio.create_task(_run_operation(operation["id"]))
    _tasks[operation["id"]] = task
    return operation


def _create_operation(kind: str, profile_id: str, instance_index: Optional[int] = None) -> dict:
    with _lock:
        profile = inference_profiles.get_profile_raw(profile_id)
        if instance_index is not None and not _select_instances(profile, instance_index):
            raise OperationNotFoundError(f"Inference instance not found: {profile_id}[{instance_index}]")
        registry = _load_registry()
        active = _active_operation_for_profile(registry, profile_id)
        if active:
            raise OperationConflictError({
                "message": "An inference operation is already active for this profile",
                "active_operation_id": active["id"],
                "kind": active.get("kind"),
                "current_step": active.get("current_step"),
            })
        operation_id = f"op_{secrets.token_hex(8)}"
        now = _now()
        operation = {
            "schema_version": SCHEMA_VERSION,
            "id": operation_id,
            "kind": kind,
            "state": "queued",
            "profile_id": profile_id,
            "instance_index": instance_index,
            "current_step": "queued",
            "steps": _initial_steps(kind),
            "progress": 0,
            "created_at": now,
            "started_at": None,
            "updated_at": now,
            "finished_at": None,
            "error": None,
            "result": None,
        }
        registry.setdefault("operations", {})[operation_id] = operation
        _save_registry(registry)
        _publish_operation(operation)
        return dict(operation)


def _active_operation_for_profile(registry: dict, profile_id: str) -> Optional[dict]:
    for operation in registry.get("operations", {}).values():
        if operation.get("profile_id") == profile_id and operation.get("state") in ACTIVE_STATES:
            return operation
    return None


def _initial_steps(kind: str) -> list[dict]:
    steps = {
        "profile_start": ["validate", "start_units", "waiting_ready", "complete"],
        "profile_stop": ["stop_units", "complete"],
        "profile_restart": ["stop_units", "start_units", "waiting_ready", "complete"],
        "instance_start": ["validate", "start_units", "waiting_ready", "complete"],
        "instance_stop": ["stop_units", "complete"],
        "instance_restart": ["stop_units", "start_units", "waiting_ready", "complete"],
    }.get(kind, ["run", "complete"])
    return [{"name": name, "state": "pending"} for name in steps]


async def _run_operation(operation_id: str):
    operation = get_operation(operation_id)
    kind = operation["kind"]
    profile_id = operation["profile_id"]
    instance_index = operation.get("instance_index")
    _patch_operation(operation_id, state="running", started_at=_now(), current_step="running", progress=5)
    try:
        if kind in {"profile_start", "instance_start"}:
            result = await _run_start(operation_id, profile_id, instance_index=instance_index, rollback_on_failure=kind == "profile_start")
        elif kind in {"profile_stop", "instance_stop"}:
            result = await _run_stop(operation_id, profile_id, instance_index=instance_index)
        elif kind in {"profile_restart", "instance_restart"}:
            result = await _run_restart(operation_id, profile_id, instance_index=instance_index)
        else:
            raise OperationError(f"Unsupported operation kind: {kind}")
        _finish_operation(operation_id, "succeeded", result=result, progress=100)
    except Exception as e:
        _finish_operation(operation_id, "failed", error=_operation_error_message(e), result=_error_result(e), progress=100)
    finally:
        _tasks.pop(operation_id, None)


async def _run_start(operation_id: str, profile_id: str, instance_index: Optional[int], rollback_on_failure: bool) -> dict:
    _set_step(operation_id, "validate", "running", progress=10)
    profile = inference_profiles.get_profile_raw(profile_id)
    instances = _select_instances(profile, instance_index)
    if not instances:
        raise OperationError("No inference instances selected")
    _set_step(operation_id, "validate", "succeeded", progress=20)

    started = []
    results = []
    try:
        _set_step(operation_id, "start_units", "running", progress=30)
        for instance in instances:
            result = await systemctl_user("start", instance["unit"])
            results.append({"index": instance["index"], "unit": instance["unit"], "action": "start", **result})
            if not result["ok"]:
                raise OperationError(f"Failed to start {instance['unit']}: {result['output']}")
            started.append(instance)
        _set_step(operation_id, "start_units", "succeeded", progress=55)

        _set_step(operation_id, "waiting_ready", "running", progress=65)
        grace = _startup_grace(profile)
        for instance in started:
            await wait_instance_ready(instance, timeout=grace)
        _set_step(operation_id, "waiting_ready", "succeeded", progress=90)
    except Exception as e:
        cause = _error_result(e)
        if rollback_on_failure and started:
            rollback = []
            for instance in started:
                rollback.append({"index": instance["index"], "unit": instance["unit"], **await systemctl_user("stop", instance["unit"])})
            inference_profiles.update_profile_runtime_state(profile_id, "failed", _instance_state_updates(started, "failed"))
            raise OperationError({
                "message": "Start failed; started instances were stopped",
                "cause": cause,
                "results": results,
                "rollback": rollback,
            })
        inference_profiles.update_profile_runtime_state(profile_id, "failed", _instance_state_updates(instances, "failed"))
        raise
    state = "running"
    inference_profiles.update_profile_runtime_state(profile_id, state, _instance_state_updates(started, "running"))
    _set_step(operation_id, "complete", "succeeded", progress=100)
    return {"profile_id": profile_id, "instances": results, "state": state}


async def _run_stop(operation_id: str, profile_id: str, instance_index: Optional[int]) -> dict:
    profile = inference_profiles.get_profile_raw(profile_id)
    instances = _select_instances(profile, instance_index)
    if not instances:
        raise OperationError("No inference instances selected")
    _set_step(operation_id, "stop_units", "running", progress=25)
    results = []
    for instance in instances:
        result = await systemctl_user("stop", instance["unit"])
        results.append({"index": instance["index"], "unit": instance["unit"], "action": "stop", **result})
        if not result["ok"]:
            raise OperationError(f"Failed to stop {instance['unit']}: {result['output']}")
    _set_step(operation_id, "stop_units", "succeeded", progress=80)
    profile_state = "stopped" if instance_index is None else _aggregate_state_after_instance_stop(profile, instance_index)
    inference_profiles.update_profile_runtime_state(profile_id, profile_state, _instance_state_updates(instances, "stopped"))
    _set_step(operation_id, "complete", "succeeded", progress=100)
    return {"profile_id": profile_id, "instances": results, "state": profile_state}


async def _run_restart(operation_id: str, profile_id: str, instance_index: Optional[int]) -> dict:
    profile = inference_profiles.get_profile_raw(profile_id)
    instances = _select_instances(profile, instance_index)
    if not instances:
        raise OperationError("No inference instances selected")
    _set_step(operation_id, "stop_units", "running", progress=15)
    stop_results = []
    for instance in instances:
        stop_results.append({"index": instance["index"], "unit": instance["unit"], **await systemctl_user("stop", instance["unit"])})
    _set_step(operation_id, "stop_units", "succeeded", progress=35)
    start_result = await _run_start(operation_id, profile_id, instance_index=instance_index, rollback_on_failure=instance_index is None)
    start_result["stopped"] = stop_results
    return start_result


def _select_instances(profile: dict, instance_index: Optional[int]) -> list[dict]:
    instances = [dict(item) for item in profile.get("instances") or [] if isinstance(item, dict)]
    if instance_index is None:
        return instances
    return [item for item in instances if int(item.get("index", -1)) == int(instance_index)]


def _startup_grace(profile: dict) -> float:
    common = profile.get("common") if isinstance(profile.get("common"), dict) else {}
    raw = common.get("startup_grace_seconds", DEFAULT_STARTUP_GRACE_SECONDS)
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_STARTUP_GRACE_SECONDS


def _instance_state_updates(instances: list[dict], state: str) -> dict[int, dict]:
    return {int(instance["index"]): {"state": state, "last_state_change": _now()} for instance in instances}


def _aggregate_state_after_instance_stop(profile: dict, instance_index: int) -> str:
    states = []
    for instance in profile.get("instances") or []:
        if int(instance.get("index", -1)) == int(instance_index):
            states.append("stopped")
        else:
            states.append(instance.get("state") or profile.get("state") or "unknown")
    if all(state == "stopped" for state in states):
        return "stopped"
    if any(state == "running" for state in states):
        return "running"
    return "degraded"


async def wait_unit_active(unit: str, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        state = await unit_active_state(unit)
        if state == "active":
            return True
        if state in {"failed", "inactive"}:
            raise OperationError(f"Unit {unit} is {state}")
        if time.monotonic() >= deadline:
            raise OperationError(f"Timed out waiting for {unit} to become active")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def wait_tcp_ready(instance: dict, timeout: float):
    deadline = time.monotonic() + timeout
    host = instance.get("host") or "127.0.0.1"
    port = int(instance.get("port"))
    while True:
        if await tcp_ready(host, port):
            return True
        if time.monotonic() >= deadline:
            raise OperationError(f"Timed out waiting for TCP readiness on {host}:{port}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def wait_instance_ready(instance: dict, timeout: float):
    deadline = time.monotonic() + timeout
    host = instance.get("host") or "127.0.0.1"
    port = int(instance.get("port"))
    unit = instance.get("unit")
    initial_restarts = await unit_restart_count(unit) if unit else 0
    while True:
        if await tcp_ready(host, port):
            return True

        state = await unit_active_state(unit) if unit else "unknown"
        if state in {"failed", "inactive"}:
            raise await unit_startup_error(unit, f"Unit {unit} is {state}", host=host, port=port)

        restart_count = await unit_restart_count(unit) if unit else 0
        if restart_count is not None and initial_restarts is not None:
            if restart_count - initial_restarts >= STARTUP_RESTART_FAILURE_THRESHOLD:
                raise await unit_startup_error(
                    unit,
                    f"Unit {unit} restarted {restart_count - initial_restarts} times before TCP readiness",
                    host=host,
                    port=port,
                    restart_count=restart_count,
                )

        if time.monotonic() >= deadline:
            raise await unit_startup_error(
                unit,
                f"Timed out waiting for TCP readiness on {host}:{port}",
                host=host,
                port=port,
                restart_count=restart_count,
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def systemctl_user(action: str, unit: str) -> dict:
    code, output = await _run(["systemctl", "--user", action, unit])
    ok = code == 0 or (action == "stop" and "not loaded" in output.lower())
    return {"ok": ok, "code": code, "output": output}


async def unit_active_state(unit: str) -> str:
    code, output = await _run(["systemctl", "--user", "is-active", unit])
    state = (output or "").strip().splitlines()[-1] if output else ""
    if code == 0 and not state:
        return "active"
    return state or "unknown"


async def unit_restart_count(unit: str) -> Optional[int]:
    if not unit:
        return None
    code, output = await _run(["systemctl", "--user", "show", unit, "-p", "NRestarts", "--value"])
    if code != 0:
        return None
    try:
        return int((output or "").strip().splitlines()[-1])
    except (IndexError, TypeError, ValueError):
        return None


async def unit_startup_error(unit: str, message: str, host: str, port: int, restart_count: Optional[int] = None) -> OperationError:
    logs = ""
    if unit:
        logs = _redact_logs(await read_journal(unit, lines=STARTUP_LOG_LINES))
    detail = {
        "message": message,
        "unit": unit,
        "host": host,
        "port": port,
        "restart_count": restart_count,
        "logs": logs,
    }
    return OperationError(detail)


async def read_journal(unit: str, lines: int = 300) -> str:
    line_count = max(1, min(int(lines), 2000))
    _code, output = await _run(["journalctl", "--user", "-u", unit, "-n", str(line_count), "--no-pager"])
    return output


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace").strip()


async def tcp_ready(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else host
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(target, int(port)), timeout=1.0)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def get_profile_instances(profile_id: str) -> dict:
    profile = inference_profiles.get_profile_raw(profile_id)
    instances = []
    for instance in profile.get("instances") or []:
        item = dict(instance)
        item["systemd_state"] = await unit_active_state(item["unit"])
        item["tcp_reachable"] = await tcp_ready(item.get("host") or "127.0.0.1", int(item["port"]))
        item["health"] = _instance_health_from_facts(item["systemd_state"], item["tcp_reachable"])
        instances.append(item)
    return {"profile_id": profile_id, "instances": instances, "health": _aggregate_health(instances)}


async def get_instance_health(profile_id: str, instance_index: int) -> dict:
    profile = inference_profiles.get_profile_raw(profile_id)
    instances = _select_instances(profile, instance_index)
    if not instances:
        raise OperationNotFoundError(f"Inference instance not found: {profile_id}[{instance_index}]")
    instance = instances[0]
    state = await unit_active_state(instance["unit"])
    reachable = await tcp_ready(instance.get("host") or "127.0.0.1", int(instance["port"]))
    health = _instance_health_from_facts(state, reachable)
    return {"profile_id": profile_id, "instance": instance, "systemd_state": state, "tcp_reachable": reachable, "health": health}


async def get_profile_health(profile_id: str) -> dict:
    return await get_profile_instances(profile_id)


def _instance_health_from_facts(systemd_state: str, tcp_reachable: bool) -> str:
    if systemd_state == "active" and tcp_reachable:
        return "healthy"
    if systemd_state == "active":
        return "degraded"
    if systemd_state in {"activating", "reloading"}:
        return "starting"
    if systemd_state == "failed":
        return "failed"
    if systemd_state in {"inactive", "unknown"}:
        return "stopped"
    return "unknown"


def _aggregate_health(instances: list[dict]) -> str:
    states = [item.get("health") for item in instances]
    if not states:
        return "unknown"
    if all(state == "healthy" for state in states):
        return "healthy"
    if all(state == "stopped" for state in states):
        return "stopped"
    if all(state == "failed" for state in states):
        return "failed"
    if any(state == "healthy" for state in states):
        return "degraded"
    if any(state == "starting" for state in states):
        return "starting"
    return "unhealthy"


async def get_instance_logs(profile_id: str, instance_index: int, lines: int = 300) -> dict:
    profile = inference_profiles.get_profile_raw(profile_id)
    instances = _select_instances(profile, instance_index)
    if not instances:
        raise OperationNotFoundError(f"Inference instance not found: {profile_id}[{instance_index}]")
    instance = instances[0]
    logs = _redact_logs(await read_journal(instance["unit"], lines=lines))
    return {"profile_id": profile_id, "instance_index": instance_index, "unit": instance["unit"], "lines": lines, "logs": logs}


async def get_profile_logs(profile_id: str, lines: int = 150, instance_index: Optional[int] = None) -> dict:
    if instance_index is not None:
        return await get_instance_logs(profile_id, instance_index, lines=lines)
    profile = inference_profiles.get_profile_raw(profile_id)
    entries = []
    for instance in profile.get("instances") or []:
        logs = _redact_logs(await read_journal(instance["unit"], lines=lines))
        prefixed = "\n".join(f"[{instance['index']}] {line}" for line in logs.splitlines())
        entries.append({"index": instance["index"], "unit": instance["unit"], "logs": prefixed})
    merged = "\n".join(entry["logs"] for entry in entries if entry["logs"])
    return {"profile_id": profile_id, "lines_per_instance": lines, "instances": entries, "logs": merged}


async def test_instance(profile_id: str, instance_index: int, body: Optional[dict] = None) -> dict:
    body = body or {}
    profile = inference_profiles.get_profile_raw(profile_id)
    instances = _select_instances(profile, instance_index)
    if not instances:
        raise OperationNotFoundError(f"Inference instance not found: {profile_id}[{instance_index}]")
    instance = instances[0]
    return await _manual_test_request(profile, instance, body)


async def test_profile(profile_id: str, body: Optional[dict] = None) -> dict:
    body = body or {}
    index = body.get("instance")
    if index is None:
        profile = inference_profiles.get_profile_raw(profile_id)
        instances = profile.get("instances") or []
        if not instances:
            raise OperationNotFoundError(f"Inference profile has no instances: {profile_id}")
        index = instances[0].get("index", 0)
    return await test_instance(profile_id, int(index), body)


async def _manual_test_request(profile: dict, instance: dict, body: dict) -> dict:
    path = str(body.get("path") or "/v1/models")
    if not path.startswith("/"):
        raise OperationError("Test path must start with /")
    method = str(body.get("method") or ("POST" if body.get("body") else "GET")).upper()
    timeout = float(body.get("timeout") or 60)
    target = "127.0.0.1" if instance.get("host") in {"0.0.0.0", "localhost"} else instance.get("host")
    url = f"http://{target}:{int(instance['port'])}{path}"
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, json=body.get("body") if method in {"POST", "PUT", "PATCH"} else None)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = _redact_logs(response.text[:2000])
    return {
        "profile_id": profile["id"],
        "instance_index": instance["index"],
        "target_mode": "local_instance",
        "method": method,
        "url": url,
        "status_code": response.status_code,
        "latency_ms": elapsed_ms,
        "body_preview": text,
    }


def _redact_logs(text: str) -> str:
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text or "")


async def wait_for_operation(operation_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        operation = get_operation(operation_id)
        if operation.get("state") in TERMINAL_STATES:
            return operation
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for inference operation {operation_id}")
        await asyncio.sleep(0.02)


def _patch_operation(operation_id: str, **updates) -> dict:
    with _lock:
        registry = _load_registry()
        operation = registry.get("operations", {}).get(operation_id)
        if not operation:
            raise OperationNotFoundError(f"Inference operation not found: {operation_id}")
        operation.update(updates)
        operation["updated_at"] = _now()
        registry["operations"][operation_id] = operation
        _save_registry(registry)
        _publish_operation(operation)
        return dict(operation)


def _finish_operation(operation_id: str, state: str, result=None, error=None, progress: int = 100):
    now = _now()
    _patch_operation(
        operation_id,
        state=state,
        finished_at=now,
        current_step=state,
        result=result,
        error=error,
        progress=progress,
    )


def _set_step(operation_id: str, name: str, state: str, progress: int):
    with _lock:
        registry = _load_registry()
        operation = registry.get("operations", {}).get(operation_id)
        if not operation:
            raise OperationNotFoundError(f"Inference operation not found: {operation_id}")
        for step in operation.get("steps") or []:
            if step.get("name") == name:
                step["state"] = state
        operation["current_step"] = name
        operation["progress"] = progress
        operation["updated_at"] = _now()
        registry["operations"][operation_id] = operation
        _save_registry(registry)
        _publish_operation(operation)


def _error_result(exc: Exception) -> dict:
    detail = exc.detail if isinstance(exc, OperationError) else str(exc)
    if isinstance(detail, dict):
        return detail
    return {"message": str(detail)}


def _operation_error_message(exc: Exception) -> str:
    detail = exc.detail if isinstance(exc, OperationError) else str(exc)
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


def _publish_operation(operation: dict):
    try:
        from ws_routes import publish

        publish({"type": "inference_operation", "operation": dict(operation)})
    except Exception:
        pass


def _prune_registry(data: dict):
    operations = data.get("operations", {})
    if len(operations) <= MAX_OPERATION_RECORDS:
        return
    active = {op_id: op for op_id, op in operations.items() if op.get("state") in ACTIVE_STATES}
    terminal = [op for op in operations.values() if op.get("state") not in ACTIVE_STATES]
    terminal.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    keep = {op["id"]: op for op in terminal[: max(0, MAX_OPERATION_RECORDS - len(active))]}
    keep.update(active)
    data["operations"] = keep
