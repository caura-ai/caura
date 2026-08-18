"""Durable per-tenant usage-counter increments (billing-grade).

The write half of ``tenant_usage_counters`` (migration 039). core-api reaches
this through ``ServiceHooks.usage_meter``; it holds no DB pool of its own
(storage-boundary rule), which is the same reason ``capability-usage`` lives
here rather than being written directly.

The table is intentionally CROSS-TENANT / RLS-free: one batch carries many
tenants' counters, so this endpoint applies NO per-tenant scoping — each row
names its own ``tenant_id``.

Increments are ADDITIVE and applied in the database, so several core-api
instances metering the same tenant in the same period all land. That is the
property distinguishing this from ``capability-usage``, which appends rows for
consumers to SUM.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/tenant-usage", tags=["TenantUsage"])
_svc = PostgresService()


@router.post("/increment")
async def increment_tenant_usage(request: Request) -> dict:
    """Add to per-tenant, per-period counters.

    Body: ``{"rows": [{tenant_id, operation, period_start, count}, ...]}`` →
    ``{"updated": int}``.

    ``period_start`` may arrive as an ISO-8601 string (JSON has no datetime)
    and is coerced here, because the column is ``DateTime(timezone=True)`` and
    asyncpg rejects a bare string with ``CannotCoerceError`` → 500. Same
    treatment ``capability-usage`` gives ``ts_bucket``, for the same reason.
    """
    body: dict = await request.json()
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise HTTPException(status_code=422, detail="'rows' must be a list")
    if not rows:
        return {"updated": 0}

    coerced: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise HTTPException(status_code=422, detail=f"row {i} must be an object")
        r = dict(row)
        ps = r.get("period_start")
        if isinstance(ps, str):
            try:
                r["period_start"] = datetime.fromisoformat(ps)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"row {i}: invalid ISO datetime for 'period_start': {ps!r}",
                ) from None
        if not r.get("tenant_id") or not r.get("operation"):
            raise HTTPException(
                status_code=422,
                detail=f"row {i}: 'tenant_id' and 'operation' are required",
            )
        # ``.get`` deliberately: a row omitting the key entirely would raise
        # KeyError here, and this loop sits OUTSIDE the try/except below — so
        # the miss surfaced as the 500 this whole coercion block exists to
        # prevent, instead of the intended 422.
        if not isinstance(r.get("period_start"), datetime):
            raise HTTPException(status_code=422, detail=f"row {i}: 'period_start' is required")
        coerced.append(r)

    try:
        updated = await _svc.tenant_usage_increment(coerced)
    except (TypeError, KeyError) as exc:
        # An unexpected column name would raise from the insert construction —
        # surface as a client 422 rather than a 500, and keep the row contents
        # out of the message (mirrors the capability-usage endpoint).
        raise HTTPException(
            status_code=422,
            detail=f"invalid tenant-usage row: {type(exc).__name__}",
        ) from exc
    return {"updated": updated}
