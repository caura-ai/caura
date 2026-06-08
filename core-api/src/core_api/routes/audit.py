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
    return await sc.list_audit_logs(tenant_id, limit=limit)
