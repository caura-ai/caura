"""Crystallization report endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

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
    report_type: str | None = None,
) -> dict:
    report_id = await _svc.report_find_running(tenant_id, fleet_id)
    if report_id is None:
        raise HTTPException(status_code=404, detail="No running report found")
    report = await _svc.report_get_by_id(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="No running report found")
    return orm_to_dict(report, REPORT_FIELDS)


@router.get("/latest")
async def get_latest_report(
    tenant_id: str,
    fleet_id: str | None = None,
    report_type: str | None = None,
) -> dict:
    report = await _svc.report_get_latest_completed(tenant_id)
    if report is None:
        raise HTTPException(status_code=404, detail="No completed report found")
    return orm_to_dict(report, REPORT_FIELDS)


@router.get("")
async def list_reports(tenant_id: str) -> list[dict]:
    reports = await _svc.report_list_by_tenant(tenant_id)
    return [orm_to_dict(r, REPORT_FIELDS) for r in reports]


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
async def get_report(report_id: UUID) -> dict:
    report = await _svc.report_get_by_id(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return orm_to_dict(report, REPORT_FIELDS)


@router.patch("/{report_id}")
async def update_report(report_id: UUID, request: Request) -> dict:
    body: dict = await request.json()
    from datetime import datetime

    await _svc.report_update_completed(
        report_id,
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
    return {"ok": True}
