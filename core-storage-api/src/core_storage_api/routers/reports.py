"""Crystallization report endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.routers._validation import _require
from core_storage_api.schemas import AGENT_DIGEST_FIELDS, REPORT_FIELDS, orm_to_dict
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/reports", tags=["Reports"])
_svc = PostgresService()


@router.post("")
async def create_report(request: Request) -> dict:
    body: dict = await request.json()
    report = await _svc.report_add(body)
    return orm_to_dict(report, REPORT_FIELDS)


@router.get("/running")
async def find_running_report(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    """08/14 L-30 + 09/02 L-48 — ``report_type`` was a filter that filtered nothing.

    Both report routes declared it, core-api's storage client forwarded it, and
    ``_reserve_report`` even wrote ``"report_type": "crystallization"`` into the
    create body. ``analysis_reports`` has no such column, so ``report_add``'s
    ``_filter_fields`` dropped it on insert and neither lookup ever mentioned
    it. A parameter that is accepted, plumbed through three layers and ignored
    reads as a scoping guarantee to everyone downstream — there is one report
    type and the model is named after it, so the honest form is to not claim a
    filter exists. Removed rather than implemented: FastAPI ignores unknown
    query params, so an older core-api still sending ``?report_type=`` is
    unaffected.
    """
    report_id = await _svc.report_find_running(tenant_id, fleet_id)
    if report_id is None:
        raise HTTPException(status_code=404, detail="No running report found")
    # ``report_find_running`` already matched on this tenant, so the id is this
    # tenant's by construction. Passed anyway rather than relied on: a predicate
    # derived from the row you are addressing is satisfied by construction, and
    # this route is not where the next reader will look for the guarantee.
    report = await _svc.report_get_by_id(report_id, tenant_id)
    if report is None:
        raise HTTPException(status_code=404, detail="No running report found")
    return orm_to_dict(report, REPORT_FIELDS)


@router.get("/latest")
async def get_latest_report(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    """09/02 L-48 — ``report_type`` removed here too, and ``fleet_id`` made real.

    ``fleet_id`` was the more damaging of the two: also accepted, also ignored,
    but with a caller that depends on it. ``_type_ii_watermark`` reads this
    route's ``completed_at`` to decide which subjects the nightly sweep may
    skip, so answering with another fleet's newer run makes a fleet with an
    older sweep skip subjects that have changed since. See
    ``report_get_latest_completed`` for why absent means "any fleet" here and
    "IS NULL" in the sibling route.
    """
    report = await _svc.report_get_latest_completed(tenant_id, fleet_id)
    if report is None:
        raise HTTPException(status_code=404, detail="No completed report found")
    return orm_to_dict(report, REPORT_FIELDS)


@router.get("")
async def list_reports(tenant_id: str, limit: int = 10, offset: int = 0) -> list[dict]:
    """09/02 M-12 — forward the window instead of silently taking the default.

    ``report_list_by_tenant`` has always paginated (ORDER BY started_at DESC,
    OFFSET, LIMIT). This route did not pass anything, so every caller got the
    function's DEFAULT first 10 rows — which meant core-api's own ``limit`` and
    ``offset`` query params, validated and advertised, could not reach the query
    that implements them. Paging returned page 1 forever.

    Bounds are enforced at the core-api edge (``ge=1, le=100`` / ``ge=0``); this
    is an internal route and mirrors the service's own defaults so an unpaged
    caller sees exactly what it saw before.
    """
    reports = await _svc.report_list_by_tenant(tenant_id, limit=limit, offset=offset)
    return [orm_to_dict(r, REPORT_FIELDS) for r in reports]


@router.post("/activity-gate")
async def crystallizer_activity_gate(request: Request) -> dict:
    """A72 — has anything been written since the last COMPLETED sweep?

    Body ``{tenant_id, fleet_id?}``. Returns ``{latest_memory_at,
    last_sweep_at}`` (ISO or null), leaving the comparison to the caller, the
    same shape as ``/insights/activity-gate``.

    POST rather than GET to match that sibling: both take a body and both are
    reads, and splitting the convention across two gates answering the same kind
    of question would be the more surprising choice.
    """
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    return await _svc.crystallizer_activity_gate(tenant_id=tenant_id, fleet_id=body.get("fleet_id"))


@router.get("/agent-activity")
async def get_agent_activity_digest(
    tenant_id: str,
    period: str = "day",
    agent_id: str | None = None,
    as_of: str | None = None,
) -> list[dict]:
    """Latest run's per-agent digest rows for a tenant/period.

    Read-only; returns ``[]`` when no run has been generated yet. Cross-tenant
    authorization is enforced upstream in core-api (this internal endpoint is
    reached only via the storage client). ``as_of`` (ISO date/datetime) views a
    past snapshot; absent ⇒ latest.
    """
    if period not in ("day", "week"):
        raise HTTPException(status_code=422, detail="'period' must be 'day' or 'week'")
    as_of_dt: datetime | None = None
    if as_of is not None:
        try:
            as_of_dt = datetime.fromisoformat(as_of)
        except ValueError:
            raise HTTPException(status_code=422, detail="'as_of' must be a valid ISO date/datetime")
        # A date-only / tz-less ISO string parses naive; window_start is
        # timestamptz, so assume UTC to avoid a naive-vs-aware asyncpg error.
        if as_of_dt.tzinfo is None:
            as_of_dt = as_of_dt.replace(tzinfo=UTC)
    rows = await _svc.agent_activity_digest_get_latest(tenant_id, period, agent_id=agent_id, as_of=as_of_dt)
    return [orm_to_dict(r, AGENT_DIGEST_FIELDS) for r in rows]


@router.post("/agent-activity")
async def upsert_agent_activity_digest(request: Request) -> dict:
    """Insert or replace one agent-activity digest row (internal; written by the
    digest generator in core-api). Idempotent on the run window — see
    ``agent_activity_digest_upsert``. Body is the full row; ISO datetime strings
    are parsed to datetimes.
    """
    body: dict = await request.json()
    for field in ("tenant_id", "run_id", "agent_id", "period", "window_start", "window_end", "status"):
        if not body.get(field):
            raise HTTPException(status_code=422, detail=f"'{field}' is required")
    if body["period"] not in ("day", "week"):
        raise HTTPException(status_code=422, detail="'period' must be 'day' or 'week'")
    for k in ("window_start", "window_end", "generated_at"):
        v = body.get(k)
        if isinstance(v, str):
            try:
                body[k] = datetime.fromisoformat(v)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"'{k}' must be a valid ISO datetime")
    row = await _svc.agent_activity_digest_upsert(body)
    return orm_to_dict(row, AGENT_DIGEST_FIELDS)


@router.post("/agent-activity/prune")
async def prune_agent_activity_digests(request: Request) -> dict:
    """Delete a tenant's digest rows older than ``older_than`` (retention sweep;
    internal). Body: ``{tenant_id, older_than (ISO)}``. Returns ``{deleted}``."""
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    older_than = body.get("older_than")
    if not tenant_id:
        raise HTTPException(status_code=422, detail="'tenant_id' is required")
    if not older_than:
        raise HTTPException(status_code=422, detail="'older_than' is required")
    if isinstance(older_than, str):
        try:
            older_than = datetime.fromisoformat(older_than)
        except ValueError:
            raise HTTPException(status_code=422, detail="'older_than' must be a valid ISO datetime")
    return {"deleted": await _svc.agent_activity_digest_prune(tenant_id, older_than)}


@router.get("/{report_id}")
async def get_report(report_id: UUID, tenant_id: str) -> dict:
    """One report, by id. ``tenant_id`` is the report's owning tenant.

    Required, not optional: this took a bare UUID, so anyone who could reach the
    port and knew an id read another tenant's report — its ``summary``,
    ``hygiene``, ``health``, ``usage_data``, ``issues`` and ``crystallization``
    blobs, plus its ``tenant_id`` and ``fleet_id``. Absent ⇒ 422, never "answer
    for whichever tenant owns the row".

    One 404 for both "no such report" and "not this tenant's report", so the
    route cannot be used to test whether a report id exists somewhere else. That
    collapse is the same one core-api's ``GET /crystallize/reports/{id}`` makes
    deliberately (audit finding #22); it now holds one layer lower too.
    """
    report = await _svc.report_get_by_id(report_id, tenant_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return orm_to_dict(report, REPORT_FIELDS)


@router.patch("/{report_id}")
async def update_report(report_id: UUID, request: Request) -> dict:
    """Finalize a report. Body: ``{tenant_id, status, completed_at, duration_ms, …}``.

    ``tenant_id`` is the report's owning tenant and scopes the UPDATE. It is
    required rather than optional: without it this took a bare primary key, so
    anyone who could reach the port and knew a UUID could finalize another
    tenant's report and substitute its entire contents.
    """
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    from datetime import datetime

    updated = await _svc.report_update_completed(
        report_id,
        tenant_id=tenant_id,
        status=body["status"],
        completed_at=datetime.fromisoformat(body["completed_at"])
        if isinstance(body.get("completed_at"), str)
        else body["completed_at"],
        duration_ms=body["duration_ms"],
        summary=body.get("summary", {}),
        hygiene=body.get("hygiene", {}),
        health=body.get("health", {}),
        usage_data=body.get("usage_data", {}),
        issues=body.get("issues", []),
        crystallization=body.get("crystallization", {}),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"ok": True}
