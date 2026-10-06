from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.constants import AUDIT_NEXT_CURSOR_HEADER, DEFAULT_AUDIT_LIMIT, MAX_AUDIT_LIMIT
from core_api.pagination import decode_cursor, encode_cursor

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
    # The row's place in the tenant's hash chain, which names a broken row by
    # ``seq``. Null for a row written before the chain existed (M-27).
    seq: int | None = None

    model_config = {"from_attributes": True}


@router.get(
    "/audit-log",
    response_model=list[AuditEntry],
    responses={
        200: {
            "headers": {
                AUDIT_NEXT_CURSOR_HEADER: {
                    "description": (
                        "Present when more entries follow this page: pass it back as "
                        "``cursor`` to read them. Absent on the last page."
                    ),
                    "schema": {"type": "string"},
                },
            },
        },
    },
)
async def list_audit_log(
    response: Response,
    tenant_id: str = Query(...),
    limit: int = Query(default=DEFAULT_AUDIT_LIMIT, ge=1, le=MAX_AUDIT_LIMIT),
    since: datetime | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    resource_id: UUID | None = Query(default=None),
    cursor: str | None = Query(
        default=None,
        description=f"The {AUDIT_NEXT_CURSOR_HEADER} value of the previous page.",
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    auth.enforce_tenant(tenant_id)
    cursor_ts: datetime | None = None
    cursor_id: UUID | None = None
    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")
    sc = get_storage_client()
    # OSS 08/14 M-11 — ``since`` is forwarded. It was declared here and dropped
    # on the floor, so a caller asking "what happened since X" got the
    # unfiltered tail and no indication the filter had not been applied. On an
    # AUDIT log that is the worst shape of wrong: the extra entries look like
    # events inside the requested window.
    #
    # One row past ``limit`` says whether another page exists without a count.
    rows = await sc.list_audit_logs(
        tenant_id,
        limit=limit + 1,
        since=since,
        agent_id=agent_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
    )
    if len(rows) > limit:
        last = rows[limit - 1]
        response.headers[AUDIT_NEXT_CURSOR_HEADER] = encode_cursor(
            datetime.fromisoformat(last["created_at"]), UUID(str(last["id"]))
        )
    return rows[:limit]
