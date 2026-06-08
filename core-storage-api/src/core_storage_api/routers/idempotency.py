"""Idempotency inbox — CRUD for the (tenant_id, key) → response cache."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from core_storage_api.schemas import IDEMPOTENCY_RESPONSE_FIELDS, orm_to_dict
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/idempotency", tags=["Idempotency"])
_svc = PostgresService()


class IdempotencyClaimRequest(BaseModel):
    tenant_id: str
    idempotency_key: str
    request_hash: str
    expires_at: datetime


class IdempotencyUpsertRequest(BaseModel):
    tenant_id: str
    idempotency_key: str
    request_hash: str
    response_body: dict
    status_code: int
    expires_at: datetime


@router.get("")
async def get_idempotency(tenant_id: str, idempotency_key: str) -> dict:
    row = await _svc.idempotency_get(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="idempotency record not found or expired")
    return orm_to_dict(row, IDEMPOTENCY_RESPONSE_FIELDS)


@router.post("/claim", status_code=201, responses={409: {"description": "Already claimed"}})
async def claim_idempotency(body: IdempotencyClaimRequest, response: Response) -> dict:
    """Insert a pending claim for ``(tenant_id, idempotency_key)``.

    Returns 201 with the claim row when the caller wins the race;
    callers proceed with the underlying handler, then call POST ``""``
    (upsert) to record the response. Returns 409 when an existing row
    already holds the key — callers poll GET ``""`` until the previous
    holder completes the response (``is_pending`` flips to False) or
    the row's ``expires_at`` passes.
    """
    row = await _svc.idempotency_claim(
        tenant_id=body.tenant_id,
        idempotency_key=body.idempotency_key,
        request_hash=body.request_hash,
        expires_at=body.expires_at,
    )
    if row is None:
        existing = await _svc.idempotency_get(
            tenant_id=body.tenant_id,
            idempotency_key=body.idempotency_key,
        )
        response.status_code = 409
        # The ``found`` field is the explicit signal callers use to tell
        # "real conflicting row" from "row vanished between conflict and
        # SELECT". Previously the client inferred this by checking for a
        # ``tenant_id`` key in the body — a fragile coupling that would
        # break the moment the error body grew a tenant_id field.
        if existing is not None:
            return {"found": True, **orm_to_dict(existing, IDEMPOTENCY_RESPONSE_FIELDS)}
        return {"found": False, "detail": "idempotency record not found or expired"}
    return orm_to_dict(row, IDEMPOTENCY_RESPONSE_FIELDS)


@router.post("")
async def upsert_idempotency(body: IdempotencyUpsertRequest) -> dict:
    # If a prior ``claim`` inserted a pending row, this UPDATEs it in
    # place; otherwise it falls back to insert-or-existing semantics for
    # legacy callers that skipped the claim step.
    row = await _svc.idempotency_upsert(
        tenant_id=body.tenant_id,
        idempotency_key=body.idempotency_key,
        request_hash=body.request_hash,
        response_body=body.response_body,
        status_code=body.status_code,
        expires_at=body.expires_at,
    )
    return orm_to_dict(row, IDEMPOTENCY_RESPONSE_FIELDS)
