"""Test-only time-warp endpoint for E2E temporal manipulation.

Gated behind TESTING=1 — route is not registered at all in production.
"""

import logging
import os
import uuid as _uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core_api.auth import AuthContext, get_auth_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/testing", tags=["Testing"])


# ── Allowlisted tables and fields (defense in depth) ──

ALLOWED_FIELDS: dict[str, set[str]] = {
    "memories": {
        "created_at",
    },
}


# ── Request schemas ──


class TimeWarpRequest(BaseModel):
    action: str

    # For set_field
    table: str | None = None
    id: str | None = None
    field: str | None = None
    value: str | None = None  # ISO-8601


class TimeWarpResponse(BaseModel):
    ok: bool
    affected: int


# ── Helpers ──


def _require_testing_mode() -> None:
    """Fail fast if TESTING env var is not set."""
    if os.getenv("TESTING") != "1":
        raise HTTPException(status_code=403, detail="Testing endpoints require TESTING=1")


# ── Endpoint ──


@router.post("/time-warp", response_model=TimeWarpResponse)
async def time_warp(
    body: TimeWarpRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> TimeWarpResponse:
    """Manipulate temporal state for E2E tests.

    Actions:
    - set_field: Update a timestamp column on an allowlisted table
    """
    _require_testing_mode()

    if body.action == "set_field":
        return await _handle_set_field(body, auth)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")


async def _handle_set_field(body: TimeWarpRequest, auth: AuthContext) -> TimeWarpResponse:
    """Update a single allowlisted field on a row."""
    from core_api.clients.storage_client import get_storage_client

    if not body.table or not body.id or not body.field or body.value is None:
        raise HTTPException(
            status_code=400,
            detail="set_field requires: table, id, field, value",
        )

    allowed = ALLOWED_FIELDS.get(body.table)
    if allowed is None:
        raise HTTPException(
            status_code=400,
            detail=f"Table '{body.table}' is not in the allowlist. Allowed: {sorted(ALLOWED_FIELDS.keys())}",
        )
    if body.field not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{body.field}' is not allowed on '{body.table}'. Allowed: {sorted(allowed)}",
        )

    try:
        _uuid.UUID(body.id)
    except ValueError:
        raise HTTPException(status_code=400, detail="id must be a valid UUID")

    # Parse ISO value to datetime
    try:
        parsed_value = datetime.fromisoformat(body.value.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid ISO-8601 value: {e}")

    sc = get_storage_client()
    if body.table == "memories":
        # Tenant isolation: verify the memory belongs to the caller's tenant
        row = await sc.get_memory(body.id)
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        if row.get("tenant_id") != auth.tenant_id:
            raise HTTPException(status_code=403, detail="Tenant mismatch")
        result = await sc.update_memory(body.id, {body.field: parsed_value.isoformat()})
        affected = 1 if result else 0
    else:
        affected = 0

    logger.info(
        "time-warp set_field: %s.%s = %s (id=%s, affected=%d)",
        body.table,
        body.field,
        body.value,
        body.id,
        affected,
    )
    return TimeWarpResponse(ok=True, affected=affected)
