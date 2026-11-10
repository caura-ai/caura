"""Internal writer for reserved skill approval records."""

import asyncio
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core_storage_api.routers._validation import _bounded_write, _require, _require_dict
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/skill-delivery", tags=["Skill delivery"])
_svc = PostgresService()


@router.post("/records")
async def publish_record(request: Request) -> dict[str, bool]:
    body = await request.json()
    _bounded_write(body)
    # Never accept a collection or system/force switch from the caller. This
    # internal route writes only records the core API constructs after approval.
    # A replacement is deliberate even when its approved recipient list shrinks.
    await _svc.document_upsert(
        tenant_id=_require(body, "tenant_id"),
        collection="_skill_delivery_grants",
        doc_id=_require(body, "doc_id"),
        data=_require_dict(body, "data"),
        system=True,
        force=True,
    )
    return {"ok": True}


class ApprovalRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=256)
    doc_ids: list[str] = Field(min_length=1, max_length=64)
    node_id: UUID
    owner_principal: str = Field(min_length=1, max_length=1024)

    @field_validator("doc_ids")
    @classmethod
    def _pointers_only(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"current/[0-9a-f]{64}", key) for key in values):
            raise ValueError("Only current approval pointer IDs are readable")
        return values


@router.post("/records/read")
async def read_records(request: Request, body: ApprovalRead) -> list[dict]:
    # Keep the request's required tenant as an explicit binding value for all
    # primary lookups. ApprovalRead still validates the complete bounded body.
    raw = await request.json()
    tenant_id = _require(raw, "tenant_id")
    # Exact requested keys: approval churn and immutable history cannot grow
    # this scan. Bound both the number of keys and concurrent primary queries.
    semaphore = asyncio.Semaphore(8)
    recipient = {"node_id": str(body.node_id), "owner_principal": body.owner_principal}

    async def lookup(key: str) -> dict | None:
        async with semaphore:
            row = await _svc.document_get_by_doc_id(
                tenant_id=tenant_id,
                collection="_skill_delivery_grants",
                doc_id=key,
            )
        if row is None:
            return None
        if row.tenant_id != tenant_id or row.doc_id != key:
            raise HTTPException(503, "Approval record scope mismatch")
        data = row.data
        if not isinstance(data, dict) or data.get("record_type") != "current":
            return None
        recipients = data.get("recipients")
        if not isinstance(recipients, list) or recipient not in recipients:
            return None
        # Never transfer the full recipient array or approver metadata. The
        # API still checks this projected binding against the primary node.
        projected = {
            k: data.get(k)
            for k in (
                "grant_id",
                "skill_doc_id",
                "artifact_hash",
                "source_hash",
                "delivery_version",
            )
        }
        projected["recipients"] = [recipient]
        return {"tenant_id": tenant_id, "doc_id": key, "data": projected}

    rows = await asyncio.gather(*(lookup(key) for key in dict.fromkeys(body.doc_ids)))
    return [row for row in rows if row is not None]
