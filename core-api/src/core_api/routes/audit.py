from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.constants import DEFAULT_AUDIT_LIMIT, MAX_AUDIT_LIMIT

router = APIRouter(tags=["Admin"])


class AuditEntry(BaseModel):
    id: UUID
    tenant_id: str
    agent_id: str | None
    action: str
    resource_type: str
    resource_id: UUID | None
    detail: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/audit-log", response_model=list[AuditEntry])
async def list_audit_log(
    tenant_id: str = Query(...),
    limit: int = Query(default=DEFAULT_AUDIT_LIMIT, ge=1, le=MAX_AUDIT_LIMIT),
    since: datetime | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    # OSS 08/14 M-11 — ``since`` is forwarded. It was declared here and dropped
    # on the floor, so a caller asking "what happened since X" got the
    # unfiltered tail and no indication the filter had not been applied. On an
    # AUDIT log that is the worst shape of wrong: the extra entries look like
    # events inside the requested window.
    return await sc.list_audit_logs(tenant_id, limit=limit, since=since)
