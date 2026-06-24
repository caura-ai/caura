"""Memory Crystallizer routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.services.crystallizer_service import run_crystallization

router = APIRouter(tags=["Memory Crystallizer"])


# --- Schemas ---


class CrystallizeRequest(BaseModel):
    tenant_id: str
    fleet_id: str | None = None


class CrystallizeResult(BaseModel):
    report_id: str
    status: str


class CrystallizeAllResult(BaseModel):
    reports: list[dict]


class ReportSummaryOut(BaseModel):
    id: str
    tenant_id: str
    fleet_id: str | None
    trigger: str
    status: str
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None
    summary: dict


# --- Endpoints ---


@router.post("/crystallize", response_model=CrystallizeResult)
async def trigger_crystallization(
    body: CrystallizeRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Trigger crystallization for a tenant (analysis + auto-curate)."""
    auth.enforce_tenant(body.tenant_id)
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(body.tenant_id)
    report_id = await run_crystallization(
        body.tenant_id,
        body.fleet_id,
        trigger="manual",
        auto_crystallize=config.auto_crystallize_enabled,
    )
    return CrystallizeResult(report_id=str(report_id), status="running")


@router.post("/crystallize/all", response_model=CrystallizeAllResult)
async def trigger_crystallization_all(
    auth: AuthContext = Depends(get_auth_context),
):
    """Trigger crystallization for ALL tenants (nightly batch)."""
    auth.enforce_admin()
    # In OSS standalone mode, only one tenant exists
    from core_api.standalone import get_standalone_tenant_id

    tenant_ids = [get_standalone_tenant_id()]
    reports = []
    for tid in tenant_ids:
        from core_api.services.organization_settings import resolve_config

        config = await resolve_config(tid)
        report_id = await run_crystallization(
            tid,
            fleet_id=None,
            trigger="scheduled",
            auto_crystallize=config.auto_crystallize_enabled,
        )
        reports.append({"tenant_id": tid, "report_id": str(report_id)})
    return CrystallizeAllResult(reports=reports)


@router.get("/crystallize/reports", response_model=list[ReportSummaryOut])
async def list_reports(
    tenant_id: str = Query(...),
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
):
    """List crystallization reports for a tenant."""
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    reports = await sc.list_reports(tenant_id)
    return [
        ReportSummaryOut(
            id=str(r.get("id", "")),
            tenant_id=r.get("tenant_id", ""),
            fleet_id=r.get("fleet_id"),
            trigger=r.get("trigger", ""),
            status=r.get("status", ""),
            started_at=r.get("started_at"),
            completed_at=r.get("completed_at"),
            duration_ms=r.get("duration_ms"),
            summary=r.get("summary") or {},
        )
        for r in reports
    ]


@router.get("/crystallize/reports/{report_id}")
async def get_report(
    report_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get a full crystallization report by ID."""
    sc = get_storage_client()
    report = await sc.get_report(str(report_id))
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    # Collapse foreign-tenant (403) into not-found (404) so callers
    # can't probe for the existence of reports in other tenants by
    # distinguishing 403 from 404 on random UUIDs (audit finding #22).
    # Cross-tenant read keys (``readable_tenant_ids`` widened past the
    # home tenant) are honoured here — the set always contains
    # ``auth.tenant_id`` by construction (``AuthContext`` line 85-90),
    # so the 404 mask still hides reports the caller has no read
    # access to.
    if not auth.is_admin and report.get("tenant_id") not in auth.readable_tenant_ids:
        raise HTTPException(status_code=404, detail="Report not found")
    return {
        "id": str(report.get("id", "")),
        "tenant_id": report.get("tenant_id"),
        "fleet_id": report.get("fleet_id"),
        "trigger": report.get("trigger"),
        "status": report.get("status"),
        "started_at": report.get("started_at"),
        "completed_at": report.get("completed_at"),
        "duration_ms": report.get("duration_ms"),
        "summary": report.get("summary") or {},
        "hygiene": report.get("hygiene") or {},
        "health": report.get("health") or {},
        "usage_data": report.get("usage_data") or {},
        "issues": report.get("issues") or [],
        "crystallization": report.get("crystallization") or {},
    }


@router.get("/crystallize/latest")
async def get_latest_report(
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Get the most recent completed crystallization report for a tenant.

    Returns ``200`` with the report body when a completed report exists;
    returns ``200`` with body ``null`` when the tenant has none yet. The
    URL itself is the well-defined "give me my latest report" resource —
    the fact that no completed report exists yet is *empty state*, not a
    missing resource. ``404`` would conflate the two and force every
    client to special-case it as "actually empty"; see CAURA-646. The
    sibling ``/crystallize/reports/{report_id}`` keeps its 404 because
    *that* endpoint genuinely points at an opaque id that may not exist.
    """
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    report = await sc.get_latest_report(tenant_id)
    # Identity check, not truthiness — the storage client's contract is
    # ``dict | None``. ``not {}`` would also be True, which would
    # silently null-return an empty (but otherwise valid) report dict
    # if storage ever changed to return ``{}`` instead of ``None`` on a
    # miss. ``is None`` is the precise guard the contract supports.
    if report is None:
        return None
    return {
        "id": str(report.get("id", "")),
        "tenant_id": report.get("tenant_id"),
        "fleet_id": report.get("fleet_id"),
        "trigger": report.get("trigger"),
        "status": report.get("status"),
        "started_at": report.get("started_at"),
        "completed_at": report.get("completed_at"),
        "duration_ms": report.get("duration_ms"),
        "summary": report.get("summary") or {},
        "hygiene": report.get("hygiene") or {},
        "health": report.get("health") or {},
        "usage_data": report.get("usage_data") or {},
        "issues": report.get("issues") or [],
        "crystallization": report.get("crystallization") or {},
    }
