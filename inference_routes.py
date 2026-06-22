from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import inference_launchers


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
async def api_validate_inference_launcher(launcher_id: str):
    try:
        return inference_launchers.validate_launcher_path(launcher_id)
    except inference_launchers.LauncherError as e:
        _raise_http_error(e)
