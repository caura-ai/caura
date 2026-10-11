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


class AuditChainBreak(BaseModel):
    """Where a verification stopped trusting the chain, and why."""

    reason: str
    seq: int | None = None
    id: str | None = None
    created_at: str | None = None
    head_seq: int | None = None
    chain_seq: int | None = None


class AuditChainVerdict(BaseModel):
    """The verdict on one window of a tenant's audit hash chain.

    ``valid`` alone does not mean the chain is intact: a ``truncated`` window
    skips the tail-against-head check. Read ``next_seq`` and verify the next
    window until one comes back un-truncated.
    """

    tenant_id: str
    valid: bool
    verified_count: int
    head_seq: int | None = None
    truncated: bool | None = None
    next_seq: int | None = None
    first_broken: AuditChainBreak | None = None


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


@router.get("/audit-log/verify", response_model=AuditChainVerdict, response_model_exclude_none=True)
async def verify_audit_log(
    tenant_id: str = Query(...),
    # The storage verifier's bound: one window is read into memory.
    limit: int = Query(default=100_000, ge=1, le=500_000),
    start_seq: int = Query(
        default=1, ge=1, description="Where the window starts: the previous window's ``next_seq``."
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    """Verify the tenant's tamper-evident audit hash chain (L-207).

    Every audit event is chained, and storage can recompute the chain, but no
    route reached that check, so nothing could tell an intact log from one with
    rows edited or deleted. An admin-plane read like the settings: agent
    credentials and org members are refused.
    """
    auth.enforce_tenant(tenant_id)
    auth.enforce_not_agent_credential("verify the audit chain")
    auth.enforce_not_org_member("verify the audit chain")
    return await get_storage_client().verify_audit_chain(tenant_id, limit=limit, start_seq=start_seq)
