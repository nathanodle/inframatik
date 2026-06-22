from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import inference_launchers
import inference_planner
import inference_profiles
import inference_operations
import inference_connect


inference_router = APIRouter()


class LauncherCreateBody(BaseModel):
    id: Optional[str] = None
    display_name: Optional[str] = None
    engine: str
    executable: str
    base_args: list[str] = Field(default_factory=list)
    working_dir: Optional[str] = None
    env: dict = Field(default_factory=dict)


class LauncherUpdateBody(BaseModel):
    display_name: Optional[str] = None
    engine: Optional[str] = None
    executable: Optional[str] = None
    base_args: Optional[list[str]] = None
    working_dir: Optional[str] = None
    env: Optional[dict] = None


def _raise_http_error(exc: inference_launchers.LauncherError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _raise_profile_error(exc: inference_profiles.ProfileError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _raise_operation_error(exc: inference_operations.OperationError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _raise_connect_error(exc: inference_connect.InferenceConnectError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@inference_router.post("/api/inference/profiles/preview")
async def api_preview_inference_profile(body: dict):
    return inference_planner.preview_profile(body)


@inference_router.get("/api/inference/profiles")
async def api_inference_profiles():
    try:
        return inference_profiles.list_profiles()
    except inference_profiles.ProfileError as e:
        _raise_profile_error(e)


@inference_router.post("/api/inference/profiles", status_code=201)
async def api_create_inference_profile(body: dict):
    try:
        return inference_profiles.create_profile(body)
    except inference_profiles.ProfileError as e:
        _raise_profile_error(e)


@inference_router.get("/api/inference/profiles/{profile_id}")
async def api_get_inference_profile(profile_id: str):
    try:
        return inference_profiles.get_profile(profile_id)
    except inference_profiles.ProfileError as e:
        _raise_profile_error(e)


@inference_router.put("/api/inference/profiles/{profile_id}")
async def api_update_inference_profile(profile_id: str, body: dict):
    try:
        return inference_profiles.update_profile(profile_id, body)
    except inference_profiles.ProfileError as e:
        _raise_profile_error(e)


@inference_router.delete("/api/inference/profiles/{profile_id}")
async def api_delete_inference_profile(profile_id: str, force: bool = False, delete_owned_tokens: bool = False):
    try:
        return await inference_connect.delete_profile_with_cleanup(
            profile_id,
            force=force,
            delete_owned_tokens=delete_owned_tokens,
        )
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/render")
async def api_render_inference_profile(profile_id: str):
    try:
        return inference_profiles.render_profile(profile_id)
    except inference_profiles.ProfileError as e:
        _raise_profile_error(e)


@inference_router.get("/api/inference/profiles/{profile_id}/export")
async def api_export_inference_profile(profile_id: str):
    try:
        return inference_profiles.export_profile(profile_id)
    except inference_profiles.ProfileError as e:
        _raise_profile_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/api-key")
async def api_rotate_inference_api_key(profile_id: str, body: dict = None):
    try:
        return inference_connect.rotate_engine_api_key(profile_id, body or {})
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)
    except inference_profiles.ProfileError as e:
        _raise_profile_error(e)


@inference_router.delete("/api/inference/profiles/{profile_id}/api-key")
async def api_disable_inference_api_key(profile_id: str):
    try:
        return inference_connect.disable_engine_api_key(profile_id)
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/cloudflare/exposure")
async def api_provision_inference_cloudflare(profile_id: str, body: dict = None):
    try:
        return await inference_connect.provision_cloudflare_exposure(profile_id, body or {})
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.delete("/api/inference/profiles/{profile_id}/cloudflare/exposure")
async def api_remove_inference_cloudflare(profile_id: str, delete_owned_tokens: bool = False):
    try:
        return await inference_connect.remove_cloudflare_exposure(profile_id, delete_owned_tokens=delete_owned_tokens)
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/cloudflare/service-tokens")
async def api_generate_inference_cloudflare_token(profile_id: str, body: dict = None):
    try:
        return await inference_connect.generate_cloudflare_service_token(profile_id, body or {})
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/cloudflare/service-tokens/{token_id}/rotate")
async def api_rotate_inference_cloudflare_token(profile_id: str, token_id: str, body: dict = None):
    try:
        return await inference_connect.rotate_cloudflare_service_token(profile_id, token_id, body or {})
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.delete("/api/inference/profiles/{profile_id}/cloudflare/service-tokens/{token_id}")
async def api_retire_inference_cloudflare_token(profile_id: str, token_id: str, delete_if_owned: bool = False):
    try:
        return await inference_connect.retire_cloudflare_service_token(profile_id, token_id, delete_if_owned=delete_if_owned)
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.get("/api/inference/profiles/{profile_id}/client-bundles")
async def api_list_inference_client_bundles(profile_id: str):
    try:
        return inference_connect.list_client_bundles(profile_id)
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/client-bundles/render")
async def api_render_inference_client_bundle(profile_id: str, body: dict = None):
    try:
        return inference_connect.render_client_bundle(profile_id, body or {})
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/client-bundles")
async def api_save_inference_client_bundle(profile_id: str, body: dict):
    try:
        return inference_connect.save_client_bundle(profile_id, body or {})
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.put("/api/inference/profiles/{profile_id}/client-bundles/{bundle_id}")
async def api_update_inference_client_bundle(profile_id: str, bundle_id: str, body: dict):
    try:
        return inference_connect.save_client_bundle(profile_id, {**(body or {}), "id": bundle_id})
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.delete("/api/inference/profiles/{profile_id}/client-bundles/{bundle_id}")
async def api_delete_inference_client_bundle(profile_id: str, bundle_id: str):
    try:
        return inference_connect.delete_client_bundle(profile_id, bundle_id)
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/start", status_code=202)
async def api_start_inference_profile(profile_id: str):
    try:
        return await inference_operations.start_profile(profile_id)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/stop", status_code=202)
async def api_stop_inference_profile(profile_id: str):
    try:
        return await inference_operations.stop_profile(profile_id)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/restart", status_code=202)
async def api_restart_inference_profile(profile_id: str):
    try:
        return await inference_operations.restart_profile(profile_id)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.get("/api/inference/profiles/{profile_id}/instances")
async def api_inference_profile_instances(profile_id: str):
    try:
        return await inference_operations.get_profile_instances(profile_id)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/instances/{instance_index}/start", status_code=202)
async def api_start_inference_instance(profile_id: str, instance_index: int):
    try:
        return await inference_operations.start_instance(profile_id, instance_index)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/instances/{instance_index}/stop", status_code=202)
async def api_stop_inference_instance(profile_id: str, instance_index: int):
    try:
        return await inference_operations.stop_instance(profile_id, instance_index)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/instances/{instance_index}/restart", status_code=202)
async def api_restart_inference_instance(profile_id: str, instance_index: int):
    try:
        return await inference_operations.restart_instance(profile_id, instance_index)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.get("/api/inference/profiles/{profile_id}/logs")
async def api_inference_profile_logs(profile_id: str, lines: int = 150, instance: Optional[int] = None):
    try:
        return await inference_operations.get_profile_logs(profile_id, lines=lines, instance_index=instance)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.get("/api/inference/profiles/{profile_id}/instances/{instance_index}/logs")
async def api_inference_instance_logs(profile_id: str, instance_index: int, lines: int = 300):
    try:
        return await inference_operations.get_instance_logs(profile_id, instance_index, lines=lines)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.get("/api/inference/profiles/{profile_id}/health")
async def api_inference_profile_health(profile_id: str):
    try:
        return await inference_operations.get_profile_health(profile_id)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.get("/api/inference/profiles/{profile_id}/instances/{instance_index}/health")
async def api_inference_instance_health(profile_id: str, instance_index: int):
    try:
        return await inference_operations.get_instance_health(profile_id, instance_index)
    except (inference_operations.OperationError, inference_profiles.ProfileError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        _raise_profile_error(e)


@inference_router.post("/api/inference/profiles/{profile_id}/test")
async def api_test_inference_profile(profile_id: str, body: dict = None):
    try:
        return await inference_operations.test_profile(profile_id, body or {})
    except (inference_operations.OperationError, inference_profiles.ProfileError, httpx.HTTPError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        if isinstance(e, inference_profiles.ProfileError):
            _raise_profile_error(e)
        raise HTTPException(502, str(e))


@inference_router.post("/api/inference/profiles/{profile_id}/instances/{instance_index}/test")
async def api_test_inference_instance(profile_id: str, instance_index: int, body: dict = None):
    try:
        return await inference_operations.test_instance(profile_id, instance_index, body or {})
    except (inference_operations.OperationError, inference_profiles.ProfileError, httpx.HTTPError) as e:
        if isinstance(e, inference_operations.OperationError):
            _raise_operation_error(e)
        if isinstance(e, inference_profiles.ProfileError):
            _raise_profile_error(e)
        raise HTTPException(502, str(e))


@inference_router.get("/api/inference/cleanup")
async def api_list_inference_cleanup():
    return inference_connect.list_cleanup_records()


@inference_router.post("/api/inference/cleanup/{record_id}/retry")
async def api_retry_inference_cleanup(record_id: str):
    try:
        return await inference_connect.retry_cleanup_record(record_id)
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.delete("/api/inference/cleanup/{record_id}")
async def api_forget_inference_cleanup(record_id: str):
    try:
        return inference_connect.forget_cleanup_record(record_id)
    except inference_connect.InferenceConnectError as e:
        _raise_connect_error(e)


@inference_router.get("/api/inference/operations")
async def api_inference_operations(profile_id: Optional[str] = None, state: Optional[str] = None):
    try:
        return inference_operations.list_operations(profile_id=profile_id, state=state)
    except inference_operations.OperationError as e:
        _raise_operation_error(e)


@inference_router.get("/api/inference/operations/{operation_id}")
async def api_get_inference_operation(operation_id: str):
    try:
        return inference_operations.get_operation(operation_id)
    except inference_operations.OperationError as e:
        _raise_operation_error(e)


@inference_router.post("/api/inference/operations/{operation_id}/cancel")
async def api_cancel_inference_operation(operation_id: str):
    try:
        return inference_operations.cancel_operation(operation_id)
    except inference_operations.OperationError as e:
        _raise_operation_error(e)


@inference_router.get("/api/inference/launchers")
async def api_inference_launchers(include_validation: bool = False):
    try:
        return inference_launchers.list_launchers(include_validation=include_validation)
    except inference_launchers.LauncherError as e:
        _raise_http_error(e)


@inference_router.post("/api/inference/launchers", status_code=201)
async def api_create_inference_launcher(body: LauncherCreateBody):
    try:
        return inference_launchers.create_launcher(
            launcher_id=body.id,
            display_name=body.display_name,
            engine=body.engine,
            executable=body.executable,
            base_args=body.base_args,
            working_dir=body.working_dir,
            env=body.env,
        )
    except inference_launchers.LauncherError as e:
        _raise_http_error(e)


@inference_router.put("/api/inference/launchers/{launcher_id}")
async def api_update_inference_launcher(launcher_id: str, body: LauncherUpdateBody):
    if hasattr(body, "model_dump"):
        updates = body.model_dump(exclude_unset=True)
    else:
        updates = body.dict(exclude_unset=True)
    try:
        return inference_launchers.update_launcher(launcher_id, updates)
    except inference_launchers.LauncherError as e:
        _raise_http_error(e)


@inference_router.delete("/api/inference/launchers/{launcher_id}")
async def api_delete_inference_launcher(launcher_id: str, force_stopped_references: bool = False):
    try:
        return inference_launchers.delete_launcher(
            launcher_id,
            force_stopped_references=force_stopped_references,
        )
    except inference_launchers.LauncherError as e:
        _raise_http_error(e)


@inference_router.post("/api/inference/launchers/{launcher_id}/validate")
async def api_validate_inference_launcher(launcher_id: str, runtime: bool = True):
    try:
        if runtime:
            return await inference_launchers.validate_launcher_runtime(launcher_id)
        return inference_launchers.validate_launcher_path(launcher_id)
    except inference_launchers.LauncherError as e:
        _raise_http_error(e)
