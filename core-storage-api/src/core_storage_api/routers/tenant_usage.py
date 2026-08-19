"""Durable per-tenant usage counters (billing-grade): write and read.

``tenant_usage_counters`` (migration 039). core-api writes through
``ServiceHooks.usage_meter``; it holds no DB pool of its own
(storage-boundary rule), which is the same reason ``capability-usage`` lives
here rather than being written directly.

WHY THE READ IS HERE TOO, AND NOT A CROSS-SCHEMA QUERY IN THE PLATFORM
----------------------------------------------------------------------
The consumer is the enterprise platform, and `enterprise.*` and `public.*` do
share one database — so the platform *could* join straight across. It does not,
because this service owns ``public.*``: ``platform-storage-api`` is documented
as owning ``enterprise.*`` only, and the org purge already reaches OSS data
through core-storage-api rather than reaching into the schema (CAURA-689).
platform-admin-api holds a ``core_storage_client`` for exactly that. One HTTP
hop on a dashboard read is the price of not having two services owning one
table's shape.

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
from pydantic import BaseModel, Field

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/tenant-usage", tags=["TenantUsage"])
_svc = PostgresService()

#: A list longer than this is a caller sending its whole tenant table by
#: accident, not a large org. It does NOT bound the query's cost — that scales
#: with how much history the named tenants have, which nothing here caps.
MAX_TENANTS_PER_QUERY = 1000


class TenantUsageQuery(BaseModel):
    """Which tenants, and which periods, to total.

    A POST for a read: the tenant list is the org's whole tenant set, which has
    no fixed bound, and a query string does. Validated by pydantic rather than
    hand-parsed — the write endpoint below hand-parses because it coerces
    per-row datetimes, and that is exactly where a missing key turned into a
    500 in review.
    """

    tenant_ids: list[str] = Field(min_length=1, max_length=MAX_TENANTS_PER_QUERY)
    period_start: datetime | None = None
    periods: int = Field(default=6, ge=1, le=24)


@router.post("/query")
async def query_tenant_usage(body: TenantUsageQuery) -> dict:
    """Total the counters for a set of tenants, per period, per operation.

    Body: ``{tenant_ids, period_start?, periods?}`` →
    ``{"periods": [{"period_start": iso, "operations": {op: total}}, ...]}``,
    newest first.

    The operation names are passed through as stored rather than mapped onto a
    fixed writes/searches/recalls triple. core-api also meters ``insights`` and
    ``evolve`` (see ``core_api.services.usage_service.OperationType``), which
    have no such column — mapping here would silently discard counts the write
    path is already paying for.
    """
    return {
        "periods": await _svc.tenant_usage_query(
            body.tenant_ids,
            period_start=body.period_start,
            periods=body.periods,
        )
    }


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
