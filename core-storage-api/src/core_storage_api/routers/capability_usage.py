"""Capability-usage adoption-counter flush endpoint.

A single bulk-insert endpoint behind core-api's in-process aggregator
(``services/capability_usage.py``), which collapses many requests into one row
per ``(tenant_id, capability, op, transport, ts_bucket)`` and flushes on a
short interval. The flush used to write directly via ``async_session`` from
core-api, holding its own DB pool — moved here so core-api keeps no pool
(storage-boundary rule).

The table is intentionally CROSS-TENANT / RLS-free (migration 023): one flush
batch carries many tenants' counters, so this endpoint applies NO per-tenant
scoping — each row carries its own ``tenant_id`` grouping dimension.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/capability-usage", tags=["CapabilityUsage"])
_svc = PostgresService()


@router.post("")
async def flush_capability_usage(request: Request) -> dict:
    """Bulk-append adoption counters.

    Body: ``{"rows": [{tenant_id, capability, op?, transport, ts_bucket,
    count, error_count, duration_ms_sum}, ...]}`` → ``{"inserted": int}``.

    ``ts_bucket`` may arrive as an ISO-8601 string (JSON has no datetime); it
    is coerced to ``datetime`` here because the column is
    ``DateTime(timezone=True)`` and asyncpg rejects a bare string with
    ``CannotCoerceError`` (→ 500). Fail-closed 422 on a missing/non-list
    ``rows`` or a malformed ``ts_bucket``.
    """
    body: dict = await request.json()
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise HTTPException(status_code=422, detail="'rows' must be a list")
    if not rows:
        return {"inserted": 0}
    # Coerce ISO ``ts_bucket`` strings to datetime so the asyncpg/timestamptz
    # codec accepts them. Mutate a shallow copy per row so a malformed value
    # surfaces as a clean 422 rather than a 500 deep in the insert.
    coerced: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise HTTPException(status_code=422, detail=f"row {i} must be an object")
        r = dict(row)
        ts = r.get("ts_bucket")
        if isinstance(ts, str):
            try:
                r["ts_bucket"] = datetime.fromisoformat(ts)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"row {i}: invalid ISO datetime for 'ts_bucket': {ts!r}",
                ) from None
        coerced.append(r)
    try:
        inserted = await _svc.capability_usage_insert(coerced)
    except (TypeError, KeyError) as exc:
        # A row with an unexpected/missing column name would raise TypeError
        # from ``CapabilityUsage(**r)`` — surface as a client 422 rather than
        # leaking a 500 (mirrors the entities bulk-endpoint pattern). Generic
        # detail so raw row contents don't echo across the boundary.
        raise HTTPException(
            status_code=422,
            detail=f"invalid capability-usage row: {type(exc).__name__}",
        ) from exc
    return {"inserted": inserted}
