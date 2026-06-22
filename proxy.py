import httpx
from urllib.parse import parse_qs, urlsplit

from nodes import resolve_node
from node_config import assert_worker_address_allowed

PROXY_TIMEOUT = 10
_NO_MATCH = object()


async def proxy_to_node(node_id: str, method: str, path: str, body: dict = None) -> dict:
    """Forward an API request to a specific node.

    If node_id is the local node, calls local functions directly.
    Otherwise, proxies via HTTP to the remote node.
    """
    target = resolve_node(node_id)

    # Self-node: call local functions directly
    if target is None:
        return await _handle_local(method, path, body)

    try:
        address = assert_worker_address_allowed(target["address"])
    except ValueError as e:
        raise ValueError(f"Invalid worker address: {e}")
    api_key = target["api_key"]
    url = f"{address}{path}"
    headers = {"X-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "POST":
                headers["Content-Type"] = "application/json"
                resp = await client.post(url, headers=headers, json=body)
            elif method == "PUT":
                headers["Content-Type"] = "application/json"
                resp = await client.put(url, headers=headers, json=body)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        raise RuntimeError(f"Node unreachable: {e}")

    if resp.status_code >= 400:
        try:
            data = resp.json()
            if isinstance(data, dict):
                detail = data.get("detail", resp.text)
            else:
                detail = str(data) or resp.text
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"Node returned {resp.status_code}: {detail}")

    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f"Node returned non-JSON response")


def _split_route(path: str):
    parsed = urlsplit(path)
    return parsed.path, parse_qs(parsed.query, keep_blank_values=False)


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    values = query.get(key)
    if not values:
        return default
    try:
        return int(values[-1])
    except (TypeError, ValueError):
        return default


def _query_bool(query: dict[str, list[str]], key: str, default: bool = False) -> bool:
    values = query.get(key)
    if not values:
        return default
    value = str(values[-1]).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def _service_name_from_route(route_path: str) -> str:
    if not route_path.startswith("/api/services/"):
        raise ValueError("Invalid service route")
    tail = route_path[len("/api/services/"):]
    name = tail.split("/", 1)[0]
    if not name:
        raise ValueError("Missing service name")
    return name


def _service_action_suffix(route_path: str):
    if not route_path.startswith("/api/services/"):
        return None
    tail = route_path[len("/api/services/"):]
    if "/" not in tail:
        return ""
    return "/" + tail.split("/", 1)[1]


async def _handle_local_services(method: str, route_path: str, query: dict[str, list[str]], body: dict = None):
    if route_path == "/api/services":
        from services import list_services, register_service

        if method == "GET":
            return await list_services()
        if method == "POST":
            payload = body or {}
            for field in ("name", "command", "working_dir"):
                if field not in payload:
                    raise ValueError(f"Missing required field: {field}")
            return await register_service(
                name=payload["name"],
                command=payload["command"],
                working_dir=payload["working_dir"],
                hostname=payload.get("hostname"),
                access_policy_id=payload.get("access_policy_id"),
                lan=payload.get("lan", False),
            )
        return _NO_MATCH

    if not route_path.startswith("/api/services/"):
        return _NO_MATCH

    from services import (
        deregister_service,
        start_service,
        stop_service,
        restart_service,
        get_service_logs,
    )

    name = _service_name_from_route(route_path)
    suffix = _service_action_suffix(route_path)

    if method == "DELETE" and suffix == "":
        svc = await deregister_service(name)
        return {"deleted": name, **svc}

    if method == "POST" and suffix in ("/start", "/stop", "/restart"):
        if suffix == "/start":
            status = await start_service(name)
        elif suffix == "/stop":
            status = await stop_service(name)
        else:
            status = await restart_service(name)
        return {"name": name, "status": status}

    if method == "GET" and suffix == "/logs":
        lines = _query_int(query, "lines", 100)
        logs = await get_service_logs(name, lines=lines)
        return {"name": name, "logs": logs}

    return _NO_MATCH


async def _handle_local_cf_service(
    method: str,
    route_path: str,
    query: dict[str, list[str]],
    body: dict = None,
):
    if not route_path.startswith("/api/internal/cf/service/"):
        return _NO_MATCH

    from cloudflared import (
        get_cloudflared_user_service_status,
        get_cloudflared_user_service_logs,
        restart_cloudflared_user_service,
        update_cloudflared_user_binary,
    )

    if route_path == "/api/internal/cf/service/status" and method == "GET":
        return await get_cloudflared_user_service_status()

    if route_path == "/api/internal/cf/service/logs" and method == "GET":
        lines = _query_int(query, "lines", 80)
        logs = await get_cloudflared_user_service_logs(lines=lines)
        return {"lines": lines, "logs": logs}

    if route_path == "/api/internal/cf/service/restart" and method == "POST":
        service = await restart_cloudflared_user_service()
        return {"status": "restarted", "service": service}

    if route_path == "/api/internal/cf/service/update" and method == "POST":
        payload = body if isinstance(body, dict) else {}
        version = payload.get("version")
        result = await update_cloudflared_user_binary(version=version)
        return {"status": "updated", "cloudflared": result}

    return _NO_MATCH


async def _handle_local_models(method: str, route_path: str, query: dict[str, list[str]], body: dict = None):
    if not route_path.startswith("/api/models"):
        return _NO_MATCH

    import model_storage

    if route_path == "/api/models" and method == "GET":
        return await model_storage.list_models()

    if route_path == "/api/models/resolve" and method == "POST":
        payload = body or {}
        return await model_storage.resolve_source(payload.get("source") or {})

    if route_path == "/api/models/storage":
        if method == "GET":
            return model_storage.get_storage_info()
        if method == "PUT":
            payload = body or {}
            return model_storage.update_storage_root(payload.get("root"))
        return _NO_MATCH

    if route_path == "/api/models/import" and method == "POST":
        payload = body or {}
        return await model_storage.start_import_job(
            path=payload.get("path"),
            artifact_id=payload.get("artifact_id"),
            display_name=payload.get("display_name"),
            snapshot=payload.get("snapshot"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )

    if route_path == "/api/models/download" and method == "POST":
        payload = body or {}
        return await model_storage.start_download_job(
            source=payload.get("source") or {},
            artifact_id=payload.get("artifact_id"),
            display_name=payload.get("display_name"),
            snapshot=payload.get("snapshot"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )

    if route_path.startswith("/api/models/jobs/"):
        tail = route_path[len("/api/models/jobs/"):]
        parts = tail.split("/")
        job_id = parts[0]
        suffix = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
        if method == "GET" and suffix == "":
            return await model_storage.get_job_status(job_id)
        if method == "POST" and suffix == "/cancel":
            return await model_storage.cancel_job(job_id)
        if method == "DELETE" and suffix == "/staging":
            return await model_storage.clean_job_staging(job_id)
        return _NO_MATCH

    if route_path.startswith("/api/models/"):
        tail = route_path[len("/api/models/"):]
        parts = tail.split("/")
        artifact_id = parts[0]
        suffix = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
        snapshot_values = query.get("snapshot") or []
        snapshot = snapshot_values[-1] if snapshot_values else None
        if method == "GET" and suffix == "/manifest":
            return model_storage.get_manifest(artifact_id, snapshot=snapshot)
        if method == "POST" and suffix == "/verify":
            return model_storage.verify_artifact(artifact_id, snapshot=snapshot)
        if method == "DELETE" and suffix == "":
            force = _query_bool(query, "force_stopped_references")
            new_active_values = query.get("new_active_snapshot") or []
            return model_storage.delete_artifact(
                artifact_id,
                snapshot=snapshot,
                force_stopped_references=force,
                new_active_snapshot=new_active_values[-1] if new_active_values else None,
            )
        return _NO_MATCH

    return _NO_MATCH


async def _handle_local_inference(method: str, route_path: str, query: dict[str, list[str]], body: dict = None):
    if not route_path.startswith("/api/inference"):
        return _NO_MATCH

    if route_path == "/api/inference/profiles/preview" and method == "POST":
        import inference_planner

        return inference_planner.preview_profile(body or {})

    if route_path == "/api/inference/profiles":
        import inference_profiles

        if method == "GET":
            return inference_profiles.list_profiles()
        if method == "POST":
            return inference_profiles.create_profile(body or {})
        return _NO_MATCH

    if route_path.startswith("/api/inference/profiles/"):
        import inference_connect
        import inference_profiles
        import inference_operations

        tail = route_path[len("/api/inference/profiles/"):]
        parts = tail.split("/")
        profile_id = parts[0]
        suffix = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
        if method == "GET" and suffix == "":
            return inference_profiles.get_profile(profile_id)
        if method == "PUT" and suffix == "":
            return inference_profiles.update_profile(profile_id, body or {})
        if method == "DELETE" and suffix == "":
            return await inference_connect.delete_profile_with_cleanup(
                profile_id,
                force=_query_bool(query, "force"),
                delete_owned_tokens=_query_bool(query, "delete_owned_tokens"),
            )
        if method == "POST" and suffix == "/render":
            return inference_profiles.render_profile(profile_id)
        if method == "POST" and suffix == "/api-key":
            return inference_connect.rotate_engine_api_key(profile_id, body or {})
        if method == "DELETE" and suffix == "/api-key":
            return inference_connect.disable_engine_api_key(profile_id)
        if method == "POST" and suffix == "/cloudflare/exposure":
            return await inference_connect.provision_cloudflare_exposure(profile_id, body or {})
        if method == "DELETE" and suffix == "/cloudflare/exposure":
            return await inference_connect.remove_cloudflare_exposure(
                profile_id,
                delete_owned_tokens=_query_bool(query, "delete_owned_tokens"),
            )
        if method == "POST" and suffix == "/cloudflare/service-tokens":
            return await inference_connect.generate_cloudflare_service_token(profile_id, body or {})
        if len(parts) >= 4 and parts[1] == "cloudflare" and parts[2] == "service-tokens":
            token_id = parts[3]
            token_suffix = "/" + "/".join(parts[4:]) if len(parts) > 4 else ""
            if method == "POST" and token_suffix == "/rotate":
                return await inference_connect.rotate_cloudflare_service_token(profile_id, token_id, body or {})
            if method == "DELETE" and token_suffix == "":
                return await inference_connect.retire_cloudflare_service_token(
                    profile_id,
                    token_id,
                    delete_if_owned=_query_bool(query, "delete_if_owned"),
                )
        if method == "GET" and suffix == "/client-bundles":
            return inference_connect.list_client_bundles(profile_id)
        if method == "POST" and suffix == "/client-bundles/render":
            return inference_connect.render_client_bundle(profile_id, body or {})
        if method == "POST" and suffix == "/client-bundles":
            return inference_connect.save_client_bundle(profile_id, body or {})
        if len(parts) >= 3 and parts[1] == "client-bundles":
            bundle_id = parts[2]
            bundle_suffix = "/" + "/".join(parts[3:]) if len(parts) > 3 else ""
            if method == "PUT" and bundle_suffix == "":
                return inference_connect.save_client_bundle(profile_id, {**(body or {}), "id": bundle_id})
            if method == "DELETE" and bundle_suffix == "":
                return inference_connect.delete_client_bundle(profile_id, bundle_id)
        if method == "POST" and suffix == "/start":
            return await inference_operations.start_profile(profile_id)
        if method == "POST" and suffix == "/stop":
            return await inference_operations.stop_profile(profile_id)
        if method == "POST" and suffix == "/restart":
            return await inference_operations.restart_profile(profile_id)
        if method == "GET" and suffix == "/instances":
            return await inference_operations.get_profile_instances(profile_id)
        if method == "GET" and suffix == "/logs":
            lines = _query_int(query, "lines", 150)
            instance_values = query.get("instance") or []
            instance_index = int(instance_values[-1]) if instance_values else None
            return await inference_operations.get_profile_logs(profile_id, lines=lines, instance_index=instance_index)
        if method == "GET" and suffix == "/health":
            return await inference_operations.get_profile_health(profile_id)
        if method == "POST" and suffix == "/test":
            return await inference_operations.test_profile(profile_id, body or {})
        if len(parts) >= 3 and parts[1] == "instances":
            instance_index = int(parts[2])
            instance_suffix = "/" + "/".join(parts[3:]) if len(parts) > 3 else ""
            if method == "POST" and instance_suffix == "/start":
                return await inference_operations.start_instance(profile_id, instance_index)
            if method == "POST" and instance_suffix == "/stop":
                return await inference_operations.stop_instance(profile_id, instance_index)
            if method == "POST" and instance_suffix == "/restart":
                return await inference_operations.restart_instance(profile_id, instance_index)
            if method == "GET" and instance_suffix == "/logs":
                return await inference_operations.get_instance_logs(profile_id, instance_index, lines=_query_int(query, "lines", 300))
            if method == "GET" and instance_suffix == "/health":
                return await inference_operations.get_instance_health(profile_id, instance_index)
            if method == "POST" and instance_suffix == "/test":
                return await inference_operations.test_instance(profile_id, instance_index, body or {})
        return _NO_MATCH

    if route_path == "/api/inference/cleanup":
        import inference_connect

        if method == "GET":
            return inference_connect.list_cleanup_records()
        return _NO_MATCH

    if route_path.startswith("/api/inference/cleanup/"):
        import inference_connect

        tail = route_path[len("/api/inference/cleanup/"):]
        parts = tail.split("/")
        record_id = parts[0]
        suffix = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
        if method == "POST" and suffix == "/retry":
            return await inference_connect.retry_cleanup_record(record_id)
        if method == "DELETE" and suffix == "":
            return inference_connect.forget_cleanup_record(record_id)
        return _NO_MATCH

    if route_path == "/api/inference/operations":
        import inference_operations

        if method == "GET":
            profile_values = query.get("profile_id") or []
            state_values = query.get("state") or []
            return inference_operations.list_operations(
                profile_id=profile_values[-1] if profile_values else None,
                state=state_values[-1] if state_values else None,
            )
        return _NO_MATCH

    if route_path.startswith("/api/inference/operations/"):
        import inference_operations

        tail = route_path[len("/api/inference/operations/"):]
        parts = tail.split("/")
        operation_id = parts[0]
        suffix = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
        if method == "GET" and suffix == "":
            return inference_operations.get_operation(operation_id)
        if method == "POST" and suffix == "/cancel":
            return inference_operations.cancel_operation(operation_id)
        return _NO_MATCH

    if not route_path.startswith("/api/inference/launchers"):
        return _NO_MATCH

    import inference_launchers

    if route_path == "/api/inference/launchers":
        if method == "GET":
            return inference_launchers.list_launchers(include_validation=_query_bool(query, "include_validation"))
        if method == "POST":
            payload = body or {}
            return inference_launchers.create_launcher(
                launcher_id=payload.get("id"),
                display_name=payload.get("display_name"),
                engine=payload.get("engine"),
                executable=payload.get("executable"),
                base_args=payload.get("base_args"),
                working_dir=payload.get("working_dir"),
                env=payload.get("env"),
            )
        return _NO_MATCH

    tail = route_path[len("/api/inference/launchers/"):]
    parts = tail.split("/")
    launcher_id = parts[0]
    suffix = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
    if method == "PUT" and suffix == "":
        return inference_launchers.update_launcher(launcher_id, body or {})
    if method == "DELETE" and suffix == "":
        return inference_launchers.delete_launcher(
            launcher_id,
            force_stopped_references=_query_bool(query, "force_stopped_references"),
        )
    if method == "POST" and suffix == "/validate":
        return inference_launchers.validate_launcher_path(launcher_id)
    return _NO_MATCH


async def _handle_local(method: str, path: str, body: dict = None):
    """Handle a proxied request locally by calling the appropriate Python functions."""
    route_path, query = _split_route(path)

    from system import get_system_metrics
    from tunnel import get_tunnel_status, get_tunnel_routes

    if route_path == "/api/system" and method == "GET":
        return get_system_metrics()

    service_response = await _handle_local_services(method, route_path, query, body)
    if service_response is not _NO_MATCH:
        return service_response

    if route_path == "/api/tunnel" and method == "GET":
        status = await get_tunnel_status()
        if _query_bool(query, "include_routes"):
            try:
                status["routes"] = await get_tunnel_routes()
            except ValueError as e:
                status["routes"] = []
                status["routes_error"] = str(e)
        return status

    cf_response = await _handle_local_cf_service(method, route_path, query, body)
    if cf_response is not _NO_MATCH:
        return cf_response

    inference_response = await _handle_local_inference(method, route_path, query, body)
    if inference_response is not _NO_MATCH:
        return inference_response

    model_response = await _handle_local_models(method, route_path, query, body)
    if model_response is not _NO_MATCH:
        return model_response

    raise ValueError(f"Unknown local route: {method} {path}")
