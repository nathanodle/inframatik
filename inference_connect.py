import copy
import json
import platform
import secrets
import shlex
from typing import Optional

import inference_profiles
import tunnel
from node_config import get_node_config, get_tunnel_id


class InferenceConnectError(ValueError):
    status_code = 400

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class InferenceConnectNotFound(InferenceConnectError):
    status_code = 404


class InferenceConnectConflict(InferenceConnectError):
    status_code = 409


def _now() -> int:
    return inference_profiles._now()


def _profile(profile_id: str) -> dict:
    try:
        return inference_profiles.get_profile_raw(profile_id)
    except inference_profiles.ProfileNotFoundError as e:
        raise InferenceConnectNotFound(e.detail) from e
    except inference_profiles.ProfileError as e:
        raise InferenceConnectError(e.detail) from e


def _single_instance(profile: dict) -> dict:
    instances = profile.get("instances") or []
    if len(instances) != 1:
        raise InferenceConnectConflict("Cloudflare exposure and profile-level bundles require exactly one resolved instance in MVP")
    return instances[0]


def _normalize_hostname(hostname: str) -> str:
    value = str(hostname or "").strip().lower().strip(".")
    if not value:
        raise InferenceConnectError("Hostname is required")
    if "/" in value or " " in value or "." not in value:
        raise InferenceConnectError("Hostname must be a DNS name")
    return value


def _service_url(instance: dict) -> str:
    return f"http://localhost:{int(instance['port'])}"


def _token_metadata(token: dict, owned: bool = True, state: str = "active") -> dict:
    now = _now()
    return {
        "id": token.get("id"),
        "name": token.get("name") or token.get("id"),
        "client_id": token.get("client_id"),
        "duration": token.get("duration"),
        "expires_at": token.get("expires_at"),
        "created_at": token.get("created_at") or now,
        "rotated_at": token.get("rotated_at") or now,
        "state": state,
        "owned_by_inframatik": owned,
    }


def _active_service_token_ids(profile: dict) -> list[str]:
    cloudflare = profile.get("cloudflare") if isinstance(profile.get("cloudflare"), dict) else {}
    ids = []
    for token in cloudflare.get("service_tokens") or []:
        if token.get("id") and token.get("state", "active") == "active":
            ids.append(token["id"])
    return ids


def _cloudflare_summary(profile: dict) -> dict:
    cloudflare = copy.deepcopy(profile.get("cloudflare") if isinstance(profile.get("cloudflare"), dict) else {})
    cloudflare.setdefault("service_tokens", [])
    return cloudflare


def _update_profile_cloudflare(profile_id: str, cloudflare: dict, exposure_mode: Optional[str] = None, hostname: Optional[str] = None):
    def _mutate(profile: dict):
        profile["cloudflare"] = copy.deepcopy(cloudflare)
        if exposure_mode:
            exposure = profile.get("exposure") if isinstance(profile.get("exposure"), dict) else {}
            exposure = copy.deepcopy(exposure)
            exposure["mode"] = exposure_mode
            if hostname is not None:
                exposure["hostname"] = hostname
            profile["exposure"] = exposure
        return inference_profiles._public_profile(profile)

    return inference_profiles.mutate_profile(profile_id, _mutate)


def _cleanup_id(profile_id: str, kind: str, hostname: str = "") -> str:
    suffix = (hostname or kind).replace(".", "-").replace("_", "-")[:80]
    return f"{profile_id}-{kind}-{suffix}"


def _record_cleanup(profile_id: str, kind: str, action: str, payload: dict, error: Exception | str) -> dict:
    now = _now()
    record_id = _cleanup_id(profile_id, kind, payload.get("hostname") or payload.get("id") or "")
    with inference_profiles._lock:
        registry = inference_profiles._load_cleanup_registry()
        existing = registry.get("cleanup", {}).get(record_id) or {}
        record = {
            "id": record_id,
            "profile_id": profile_id,
            "kind": kind,
            "action": action,
            "payload": copy.deepcopy(payload),
            "error": str(error),
            "state": "pending",
            "attempts": int(existing.get("attempts") or 0),
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
        }
        registry["cleanup"][record_id] = record
        inference_profiles._save_cleanup_registry(registry)
    return record


def list_cleanup_records() -> dict:
    inference_profiles.initialize_profile_registries()
    with inference_profiles._lock:
        registry = inference_profiles._load_cleanup_registry()
        records = list(registry.get("cleanup", {}).values())
    records.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return {"cleanup": records}


async def _run_cleanup_action(record: dict):
    kind = record.get("kind")
    payload = record.get("payload") or {}
    if kind == "tunnel_route":
        return await tunnel.remove_tunnel_route(payload["hostname"], tunnel_id=payload.get("tunnel_id"))
    if kind == "dns_record":
        return await tunnel.delete_dns_record(payload["hostname"], zone_id=payload.get("zone_id"))
    if kind == "access_app":
        return await tunnel.delete_access_app(payload["hostname"])
    if kind == "access_policy":
        return await tunnel.delete_access_policy(payload["id"])
    if kind == "service_token":
        return await tunnel.delete_access_service_token(payload["id"])
    raise InferenceConnectError(f"Unsupported cleanup kind: {kind}")


async def retry_cleanup_record(record_id: str) -> dict:
    with inference_profiles._lock:
        registry = inference_profiles._load_cleanup_registry()
        record = registry.get("cleanup", {}).get(record_id)
    if not record:
        raise InferenceConnectNotFound(f"Cleanup record not found: {record_id}")
    try:
        await _run_cleanup_action(record)
    except Exception as e:
        with inference_profiles._lock:
            registry = inference_profiles._load_cleanup_registry()
            current = registry.get("cleanup", {}).get(record_id, record)
            current["attempts"] = int(current.get("attempts") or 0) + 1
            current["updated_at"] = _now()
            current["error"] = str(e)
            registry["cleanup"][record_id] = current
            inference_profiles._save_cleanup_registry(registry)
        raise InferenceConnectError(f"Cleanup retry failed: {e}") from e
    with inference_profiles._lock:
        registry = inference_profiles._load_cleanup_registry()
        registry.get("cleanup", {}).pop(record_id, None)
        inference_profiles._save_cleanup_registry(registry)
    return {"status": "cleaned", "id": record_id}


def forget_cleanup_record(record_id: str) -> dict:
    with inference_profiles._lock:
        registry = inference_profiles._load_cleanup_registry()
        if record_id not in registry.get("cleanup", {}):
            raise InferenceConnectNotFound(f"Cleanup record not found: {record_id}")
        registry["cleanup"].pop(record_id, None)
        inference_profiles._save_cleanup_registry(registry)
    return {"status": "forgotten", "id": record_id}


def rotate_engine_api_key(profile_id: str, body: Optional[dict] = None) -> dict:
    body = body if isinstance(body, dict) else {}
    raw_key = "llm_" + secrets.token_hex(32)
    result = inference_profiles.set_engine_api_key(profile_id, raw_key)
    response = {
        "status": "rotated",
        "profile": result["profile"],
        "engine_api_key": raw_key,
        "secret": result["secret"],
        "one_time_secret": True,
    }
    if body.get("render_bundle"):
        response["client_bundle"] = render_client_bundle(
            profile_id,
            {**body.get("bundle", {}), "engine_api_key": raw_key},
        )
    return response


def disable_engine_api_key(profile_id: str) -> dict:
    try:
        result = inference_profiles.disable_engine_api_key(profile_id)
    except inference_profiles.ProfileNotFoundError as e:
        raise InferenceConnectNotFound(e.detail) from e
    except inference_profiles.ProfileError as e:
        raise InferenceConnectError(e.detail) from e
    return {"status": "disabled", **result}


async def provision_cloudflare_exposure(profile_id: str, body: Optional[dict] = None) -> dict:
    body = body if isinstance(body, dict) else {}
    profile = _profile(profile_id)
    instance = _single_instance(profile)
    hostname = _normalize_hostname(body.get("hostname") or (profile.get("exposure") or {}).get("hostname"))
    token_name = body.get("service_token_name") or f"inframatik-{profile_id}-client"
    duration = body.get("duration") or "8760h"
    created_token = None
    existing_token_id = str(body.get("service_token_id") or "").strip()
    resources = []
    cf = _cloudflare_summary(profile)
    if cf.get("hostname") and cf.get("hostname") != hostname:
        raise InferenceConnectConflict("Profile already has Cloudflare exposure; remove it before changing hostnames")

    try:
        await tunnel.add_tunnel_route(hostname, _service_url(instance), tunnel_id=body.get("tunnel_id"))
        resources.append(("tunnel_route", {"hostname": hostname, "tunnel_id": body.get("tunnel_id") or get_tunnel_id()}))
        dns_record_id = await tunnel.create_dns_record(hostname, tunnel_id=body.get("tunnel_id"), zone_id=body.get("zone_id"))
        resources.append(("dns_record", {"hostname": hostname, "zone_id": body.get("zone_id")}))
        if existing_token_id:
            token_meta = {
                "id": existing_token_id,
                "name": body.get("service_token_name") or existing_token_id,
                "client_id": body.get("client_id"),
                "state": "active",
                "owned_by_inframatik": False,
                "created_at": _now(),
                "rotated_at": None,
            }
        else:
            created_token = await tunnel.create_access_service_token(token_name, duration=duration)
            token_meta = _token_metadata(created_token, owned=True)
            resources.append(("service_token", {"id": token_meta["id"], "hostname": hostname}))
        active_ids = [token_meta["id"]]
        policy = await tunnel.create_service_auth_policy(f"inframatik-{profile_id}-service-auth", active_ids)
        resources.append(("access_policy", {"id": policy.get("id")}))
        app = await tunnel.create_access_app(f"inframatik inference {profile_id}", hostname, policy["id"])
        resources.append(("access_app", {"hostname": hostname, "id": app.get("id")}))
    except Exception as e:
        for kind, payload in reversed(resources):
            try:
                await _run_cleanup_action({"kind": kind, "payload": payload})
            except Exception as cleanup_error:
                _record_cleanup(profile_id, kind, "delete", payload, cleanup_error)
        raise InferenceConnectError(str(e)) from e

    cf.update({
        "hostname": hostname,
        "tunnel_id": body.get("tunnel_id") or get_tunnel_id(),
        "dns_record_id": dns_record_id,
        "access_app_id": app.get("id"),
        "access_aud": app.get("aud"),
        "access_policy_id": policy.get("id"),
        "access_policy_name": policy.get("name"),
        "protection": "service_token",
        "service_tokens": [token_meta],
    })
    updated_profile = _update_profile_cloudflare(profile_id, cf, exposure_mode="cloudflare", hostname=hostname)
    response = {
        "status": "provisioned",
        "profile": updated_profile,
        "cloudflare": cf,
        "client_id": token_meta.get("client_id"),
        "one_time_secret": bool(created_token and created_token.get("client_secret")),
    }
    if created_token and created_token.get("client_secret"):
        response["client_secret"] = created_token["client_secret"]
    if body.get("render_bundle"):
        response["client_bundle"] = render_client_bundle(
            profile_id,
            {
                **body.get("bundle", {}),
                "exposure_mode": "cloudflare",
                "service_token_id": token_meta.get("id"),
                "cf_client_secret": created_token.get("client_secret") if created_token else body.get("cf_client_secret"),
            },
        )
    return response


async def remove_cloudflare_exposure(profile_id: str, delete_owned_tokens: bool = False) -> dict:
    profile = _profile(profile_id)
    cf = _cloudflare_summary(profile)
    hostname = cf.get("hostname") or (profile.get("exposure") or {}).get("hostname")
    if not hostname:
        raise InferenceConnectNotFound("Cloudflare exposure is not configured for this profile")
    warnings = []
    cleanup_actions = [
        ("tunnel_route", {"hostname": hostname, "tunnel_id": cf.get("tunnel_id")}),
        ("dns_record", {"hostname": hostname}),
        ("access_app", {"hostname": hostname, "id": cf.get("access_app_id")}),
    ]
    if cf.get("access_policy_id"):
        cleanup_actions.append(("access_policy", {"id": cf["access_policy_id"]}))
    if delete_owned_tokens:
        for token in cf.get("service_tokens") or []:
            if token.get("owned_by_inframatik") and token.get("id"):
                cleanup_actions.append(("service_token", {"id": token["id"], "hostname": hostname}))
    for kind, payload in cleanup_actions:
        try:
            await _run_cleanup_action({"kind": kind, "payload": payload})
        except Exception as e:
            warnings.append(f"{kind}: {e}")
            _record_cleanup(profile_id, kind, "delete", payload, e)

    def _mutate(stored: dict):
        stored["cloudflare"] = {
            "hostname": None,
            "access_app_id": None,
            "access_policy_id": None,
            "service_tokens": [],
        }
        exposure = stored.get("exposure") if isinstance(stored.get("exposure"), dict) else {}
        exposure = copy.deepcopy(exposure)
        exposure["mode"] = "local"
        exposure.pop("hostname", None)
        stored["exposure"] = exposure
        return inference_profiles._public_profile(stored)

    updated = inference_profiles.mutate_profile(profile_id, _mutate)
    return {"status": "removed", "profile": updated, "warnings": warnings}


async def generate_cloudflare_service_token(profile_id: str, body: Optional[dict] = None) -> dict:
    body = body if isinstance(body, dict) else {}
    profile = _profile(profile_id)
    cf = _cloudflare_summary(profile)
    if not cf.get("access_policy_id"):
        raise InferenceConnectConflict("Cloudflare Service Auth policy is not configured for this profile")
    token = await tunnel.create_access_service_token(
        body.get("name") or f"inframatik-{profile_id}-client-{secrets.token_hex(3)}",
        duration=body.get("duration") or "8760h",
    )
    token_meta = _token_metadata(token, owned=True)
    cf.setdefault("service_tokens", []).append(token_meta)
    await tunnel.update_service_auth_policy_tokens(cf["access_policy_id"], [*(_active_service_token_ids({"cloudflare": cf}))])
    updated_profile = _update_profile_cloudflare(profile_id, cf)
    response = {
        "status": "created",
        "profile": updated_profile,
        "service_token": token_meta,
        "client_id": token.get("client_id"),
        "client_secret": token.get("client_secret"),
        "one_time_secret": True,
    }
    if body.get("render_bundle"):
        response["client_bundle"] = render_client_bundle(
            profile_id,
            {
                **body.get("bundle", {}),
                "exposure_mode": "cloudflare",
                "service_token_id": token_meta["id"],
                "cf_client_secret": token.get("client_secret"),
            },
        )
    return response


async def rotate_cloudflare_service_token(profile_id: str, token_id: str, body: Optional[dict] = None) -> dict:
    body = body if isinstance(body, dict) else {}
    profile = _profile(profile_id)
    cf = _cloudflare_summary(profile)
    tokens = cf.get("service_tokens") or []
    match = next((item for item in tokens if item.get("id") == token_id), None)
    if not match:
        raise InferenceConnectNotFound(f"Service token not attached to profile: {token_id}")
    rotated = await tunnel.rotate_access_service_token(token_id)
    match.update(_token_metadata({**match, **rotated}, owned=bool(match.get("owned_by_inframatik")), state=match.get("state", "active")))
    updated_profile = _update_profile_cloudflare(profile_id, cf)
    response = {
        "status": "rotated",
        "profile": updated_profile,
        "service_token": match,
        "client_id": rotated.get("client_id"),
        "client_secret": rotated.get("client_secret"),
        "one_time_secret": True,
    }
    if body.get("render_bundle"):
        response["client_bundle"] = render_client_bundle(
            profile_id,
            {
                **body.get("bundle", {}),
                "exposure_mode": "cloudflare",
                "service_token_id": token_id,
                "cf_client_secret": rotated.get("client_secret"),
            },
        )
    return response


async def retire_cloudflare_service_token(profile_id: str, token_id: str, delete_if_owned: bool = False) -> dict:
    profile = _profile(profile_id)
    cf = _cloudflare_summary(profile)
    tokens = cf.get("service_tokens") or []
    match = next((item for item in tokens if item.get("id") == token_id), None)
    if not match:
        raise InferenceConnectNotFound(f"Service token not attached to profile: {token_id}")
    active_ids = [
        item["id"]
        for item in tokens
        if item.get("id") and item.get("state") == "active" and item.get("id") != token_id
    ]
    if not active_ids:
        raise InferenceConnectConflict("Cannot retire the last active Cloudflare service token; remove Cloudflare exposure instead")
    match["state"] = "retired"
    match["retired_at"] = _now()
    if cf.get("access_policy_id"):
        await tunnel.update_service_auth_policy_tokens(cf["access_policy_id"], active_ids)
    if delete_if_owned and match.get("owned_by_inframatik"):
        await tunnel.delete_access_service_token(token_id)
        match["deleted_at"] = _now()
    updated_profile = _update_profile_cloudflare(profile_id, cf)
    return {"status": "retired", "profile": updated_profile, "service_token": match}


def _bundle_registry(profile: dict) -> dict:
    bundles = profile.get("client_bundles")
    return copy.deepcopy(bundles if isinstance(bundles, dict) else {})


def list_client_bundles(profile_id: str) -> dict:
    profile = _profile(profile_id)
    bundles = list(_bundle_registry(profile).values())
    bundles.sort(key=lambda item: item.get("name") or item.get("id") or "")
    instance_bundles = _instance_bundle_options(profile_id, profile)
    try:
        default = render_client_bundle(profile_id, {})
    except InferenceConnectConflict as e:
        default = {
            "id": "default",
            "profile_id": profile_id,
            "requires_instance": True,
            "message": str(e),
        }
    return {
        "profile_id": profile_id,
        "bundles": bundles,
        "default": default,
        "instance_bundles": instance_bundles,
    }


def _instance_bundle_options(profile_id: str, profile: dict) -> list[dict]:
    instances = [item for item in profile.get("instances") or [] if isinstance(item, dict)]
    if len(instances) <= 1:
        return []
    exposure = profile.get("exposure") if isinstance(profile.get("exposure"), dict) else {}
    mode = exposure.get("mode") or "local"
    if mode not in {"local", "lan"}:
        mode = "local"
    options = []
    for instance in sorted(instances, key=lambda item: item.get("index", 0)):
        try:
            index = int(instance.get("index"))
        except (TypeError, ValueError):
            continue
        bundle = render_client_bundle(
            profile_id,
            {
                "target_type": "instance",
                "instance_index": index,
                "exposure_mode": mode,
            },
        )
        bundle["instance"] = {
            "index": index,
            "host": instance.get("host"),
            "port": instance.get("port"),
            "gpu_ids": copy.deepcopy(instance.get("gpu_ids") or []),
            "unit": instance.get("unit"),
            "state": instance.get("state"),
        }
        options.append(bundle)
    return options


def _selected_instance(profile: dict, target_type: str, instance_index) -> dict:
    instances = profile.get("instances") or []
    if target_type == "profile":
        return _single_instance(profile)
    try:
        wanted = int(instance_index)
    except (TypeError, ValueError):
        raise InferenceConnectError("instance_index is required for instance bundles")
    for instance in instances:
        if int(instance.get("index")) == wanted:
            return instance
    raise InferenceConnectNotFound(f"Profile instance not found: {wanted}")


def _lan_host(body: dict) -> str:
    host = str(body.get("lan_host") or body.get("host") or "").strip()
    if host:
        return host
    config = get_node_config() or {}
    return config.get("lan_host") or platform.node() or "127.0.0.1"


def _bundle_base_url(profile: dict, instance: dict, mode: str, body: dict) -> str:
    if mode == "cloudflare":
        hostname = (_cloudflare_summary(profile).get("hostname") or (profile.get("exposure") or {}).get("hostname"))
        if not hostname:
            raise InferenceConnectError("Cloudflare bundle requires a configured hostname")
        _single_instance(profile)
        return f"https://{hostname}/v1"
    if mode == "lan":
        return f"http://{_lan_host(body)}:{int(instance['port'])}/v1"
    return f"http://127.0.0.1:{int(instance['port'])}/v1"


def _token_for_bundle(profile: dict, service_token_id: Optional[str]) -> Optional[dict]:
    tokens = (_cloudflare_summary(profile).get("service_tokens") or [])
    active = [item for item in tokens if item.get("state", "active") == "active"]
    if service_token_id:
        return next((item for item in tokens if item.get("id") == service_token_id), None)
    return active[0] if active else None


def _curl_example(base_url: str, model_name: str, headers: dict) -> str:
    parts = ["curl", "-sS", shlex.quote(f"{base_url}/chat/completions")]
    for key, value in headers.items():
        parts.extend(["-H", shlex.quote(f"{key}: {value}")])
    payload = json.dumps({
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
    })
    parts.extend(["-H", shlex.quote("Content-Type: application/json"), "-d", shlex.quote(payload)])
    return " ".join(parts)


def _python_example(base_url: str, model_name: str, headers: dict) -> str:
    header_repr = json.dumps(headers, indent=4, sort_keys=True)
    api_key = headers.get("Authorization", "Bearer not-required").replace("Bearer ", "", 1)
    return (
        "from openai import OpenAI\n\n"
        f"client = OpenAI(base_url={base_url!r}, api_key={api_key!r}, default_headers={header_repr})\n"
        "response = client.chat.completions.create(\n"
        f"    model={model_name!r},\n"
        "    messages=[{\"role\": \"user\", \"content\": \"ping\"}],\n"
        "    max_tokens=16,\n"
        ")\n"
        "print(response.choices[0].message.content)\n"
    )


def _litellm_example(base_url: str, model_name: str, headers: dict) -> str:
    lines = [
        "model_list:",
        f"  - model_name: {model_name}",
        "    litellm_params:",
        f"      model: openai/{model_name}",
        f"      api_base: {base_url}",
    ]
    if headers.get("Authorization"):
        lines.append(f"      api_key: {headers['Authorization'].replace('Bearer ', '', 1)}")
    extra = {k: v for k, v in headers.items() if k != "Authorization"}
    if extra:
        lines.append("      extra_headers:")
        for key, value in sorted(extra.items()):
            lines.append(f"        {key}: {value}")
    return "\n".join(lines) + "\n"


def render_client_bundle(profile_id: str, body: Optional[dict] = None) -> dict:
    body = body if isinstance(body, dict) else {}
    profile = _profile(profile_id)
    target_type = body.get("target_type") or body.get("target", {}).get("type") or "profile"
    instance = _selected_instance(profile, target_type, body.get("instance_index"))
    exposure = profile.get("exposure") if isinstance(profile.get("exposure"), dict) else {}
    mode = body.get("exposure_mode") or exposure.get("mode") or "local"
    base_url = _bundle_base_url(profile, instance, mode, body)
    model_name = (profile.get("common") or {}).get("served_model_name") or profile_id
    engine_key = body.get("engine_api_key")
    has_engine_key = bool((profile.get("secrets") or {}).get("engine_api_key_id"))
    headers = {}
    if engine_key:
        headers["Authorization"] = f"Bearer {engine_key}"
    elif has_engine_key:
        headers["Authorization"] = "Bearer <rotate_inference_api_key_to_show_once>"
    cf_token = None
    if mode == "cloudflare":
        cf_token = _token_for_bundle(profile, body.get("service_token_id"))
        if cf_token and cf_token.get("client_id"):
            headers["CF-Access-Client-Id"] = cf_token["client_id"]
        if body.get("cf_client_secret"):
            headers["CF-Access-Client-Secret"] = body["cf_client_secret"]
        elif cf_token:
            headers["CF-Access-Client-Secret"] = "<rotate_or_generate_cloudflare_service_token_to_show_once>"
    missing = []
    if has_engine_key and not engine_key:
        missing.append("rotate_inference_api_key")
    if mode == "cloudflare" and cf_token and not body.get("cf_client_secret"):
        missing.append("rotate_cloudflare_service_token")
    examples = {
        "curl": _curl_example(base_url, model_name, headers),
        "python_openai": _python_example(base_url, model_name, headers),
        "litellm": _litellm_example(base_url, model_name, headers),
    }
    return {
        "id": body.get("id") or "default",
        "name": body.get("name") or "Default",
        "profile_id": profile_id,
        "target": {"type": target_type, "instance_index": instance.get("index") if target_type == "instance" else None},
        "exposure_mode": mode,
        "base_url": base_url,
        "model": model_name,
        "headers": headers,
        "service_token_id": cf_token.get("id") if cf_token else None,
        "engine_api_key_id": (profile.get("secrets") or {}).get("engine_api_key_id"),
        "secret_state": {
            "engine_api_key_configured": has_engine_key,
            "engine_api_key_available": bool(engine_key),
            "cf_client_secret_available": bool(body.get("cf_client_secret")),
            "missing_secret_actions": missing,
        },
        "examples": examples,
    }


def save_client_bundle(profile_id: str, body: dict) -> dict:
    profile = _profile(profile_id)
    target_type = body.get("target_type") or "profile"
    if len(profile.get("instances") or []) != 1 and target_type != "instance":
        raise InferenceConnectError("Replicated profiles require an explicit instance bundle")
    bundle_id = str(body.get("id") or body.get("name") or f"bundle-{secrets.token_hex(4)}").strip().lower()
    bundle_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in bundle_id).strip("-_")[:64]
    if not bundle_id:
        raise InferenceConnectError("Bundle ID is required")
    now = _now()
    metadata = {
        "id": bundle_id,
        "name": body.get("name") or bundle_id,
        "profile_id": profile_id,
        "target_type": target_type,
        "instance_index": body.get("instance_index"),
        "exposure_mode": body.get("exposure_mode") or (profile.get("exposure") or {}).get("mode") or "local",
        "service_token_id": body.get("service_token_id"),
        "engine_api_key_id": (profile.get("secrets") or {}).get("engine_api_key_id") if body.get("include_engine_api_key", True) else None,
        "notes": body.get("notes"),
        "created_at": now,
        "updated_at": now,
    }

    def _mutate(stored: dict):
        bundles = stored.get("client_bundles") if isinstance(stored.get("client_bundles"), dict) else {}
        if bundle_id in bundles:
            metadata["created_at"] = bundles[bundle_id].get("created_at") or now
        bundles[bundle_id] = metadata
        stored["client_bundles"] = bundles
        return {"status": "saved", "bundle": metadata}

    return inference_profiles.mutate_profile(profile_id, _mutate)


def delete_client_bundle(profile_id: str, bundle_id: str) -> dict:
    bundle_id = str(bundle_id or "").strip()
    if not bundle_id:
        raise InferenceConnectError("Bundle ID is required")

    def _mutate(stored: dict):
        bundles = stored.get("client_bundles") if isinstance(stored.get("client_bundles"), dict) else {}
        if bundle_id not in bundles:
            raise InferenceConnectNotFound(f"Client bundle not found: {bundle_id}")
        bundles.pop(bundle_id, None)
        stored["client_bundles"] = bundles
        return {"status": "deleted", "bundle_id": bundle_id}

    return inference_profiles.mutate_profile(profile_id, _mutate)


async def delete_profile_with_cleanup(profile_id: str, force: bool = False, delete_owned_tokens: bool = False) -> dict:
    profile = _profile(profile_id)
    cf = _cloudflare_summary(profile)
    cleanup = None
    if cf.get("hostname"):
        try:
            cleanup = await remove_cloudflare_exposure(profile_id, delete_owned_tokens=delete_owned_tokens)
        except InferenceConnectNotFound:
            cleanup = None
    try:
        deleted = inference_profiles.delete_profile(profile_id, force=force)
    except inference_profiles.ProfileNotFoundError as e:
        raise InferenceConnectNotFound(e.detail) from e
    except inference_profiles.ProfileError as e:
        raise InferenceConnectError(e.detail) from e
    if cleanup:
        deleted["cloudflare_cleanup"] = cleanup
    return deleted
