"""Internal fetch evidence; protected by storage service authentication."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from common.keystone_fetches import FetchRecord, ScopedFetchMatchRequest
from core_storage_api.routers._validation import _require
from core_storage_api.services import keystone_fetches

router = APIRouter(prefix="/keystone-fetches", tags=["Keystone Fetches"])


@router.post("")
async def record_fetch(request: Request) -> dict:
    try:
        body: dict = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="expected a JSON object")
    tenant_id = _require(body, "tenant_id")
    try:
        record = FetchRecord.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid fetch record") from exc
    return await keystone_fetches.record_fetch(tenant_id=tenant_id, record=record)


@router.post("/match")
async def match_fetches(request: Request) -> dict:
    try:
        body: dict = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="expected a JSON object")
    tenant_id = _require(body, "tenant_id")
    try:
        batch = ScopedFetchMatchRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid fetch match request") from exc
    if any(check.tenant_id != tenant_id for check in batch.checks):
        raise HTTPException(status_code=422, detail="check tenant does not match scope")
    # core-api groups the authorized admin batch by tenant. Storage never
    # executes a tenantless query, even for an internal service caller.
    return await keystone_fetches.match_fetches(tenant_id=tenant_id, checks=batch.checks)
