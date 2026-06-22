from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import model_storage


model_router = APIRouter()


class StorageUpdateBody(BaseModel):
    root: str


class ModelImportBody(BaseModel):
    path: str
    artifact_id: Optional[str] = None
    display_name: Optional[str] = None
    snapshot: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class ModelDownloadBody(BaseModel):
    source: dict
    artifact_id: Optional[str] = None
    display_name: Optional[str] = None
    snapshot: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class ModelResolveBody(BaseModel):
    source: dict


def _raise_http_error(exc: model_storage.ModelStorageError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@model_router.get("/api/models")
async def api_models():
    try:
        return await model_storage.list_models()
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.post("/api/models/resolve")
async def api_models_resolve(body: ModelResolveBody):
    try:
        return await model_storage.resolve_source(body.source)
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.get("/api/models/storage")
async def api_models_storage():
    try:
        return model_storage.get_storage_info()
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.put("/api/models/storage")
async def api_models_update_storage(body: StorageUpdateBody):
    try:
        return model_storage.update_storage_root(body.root)
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.post("/api/models/import", status_code=202)
async def api_models_import(body: ModelImportBody):
    try:
        return await model_storage.start_import_job(
            path=body.path,
            artifact_id=body.artifact_id,
            display_name=body.display_name,
            snapshot=body.snapshot,
            metadata=body.metadata,
        )
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.post("/api/models/download", status_code=202)
async def api_models_download(body: ModelDownloadBody):
    try:
        return await model_storage.start_download_job(
            source=body.source,
            artifact_id=body.artifact_id,
            display_name=body.display_name,
            snapshot=body.snapshot,
            metadata=body.metadata,
        )
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.get("/api/models/jobs/{job_id}")
async def api_models_job(job_id: str):
    try:
        return await model_storage.get_job_status(job_id)
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.post("/api/models/jobs/{job_id}/cancel")
async def api_models_cancel_job(job_id: str):
    try:
        return await model_storage.cancel_job(job_id)
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.delete("/api/models/jobs/{job_id}/staging")
async def api_models_clean_job_staging(job_id: str):
    try:
        return await model_storage.clean_job_staging(job_id)
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.get("/api/models/{artifact_id}/manifest")
async def api_models_manifest(artifact_id: str, snapshot: Optional[str] = None):
    try:
        return model_storage.get_manifest(artifact_id, snapshot=snapshot)
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.post("/api/models/{artifact_id}/verify")
async def api_models_verify(artifact_id: str, snapshot: Optional[str] = None):
    try:
        return model_storage.verify_artifact(artifact_id, snapshot=snapshot)
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)


@model_router.delete("/api/models/{artifact_id}")
async def api_models_delete(
    artifact_id: str,
    snapshot: Optional[str] = None,
    force_stopped_references: bool = False,
    new_active_snapshot: Optional[str] = None,
):
    try:
        return model_storage.delete_artifact(
            artifact_id,
            snapshot=snapshot,
            force_stopped_references=force_stopped_references,
            new_active_snapshot=new_active_snapshot,
        )
    except model_storage.ModelStorageError as e:
        _raise_http_error(e)
