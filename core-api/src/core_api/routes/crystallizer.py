"""Memory Crystallizer routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core_api import openapi_responses as _oar
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.schemas import STRICT_WRITE_BODY
from core_api.services.crystallizer_service import start_crystallization

router = APIRouter(tags=["Memory Crystallizer"])


# --- Schemas ---


class CrystallizeRequest(BaseModel):
    model_config = STRICT_WRITE_BODY

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
    # H-07: the run is scheduled, not awaited. This response has always said
    # ``status="running"``; awaiting the run made that false, and — once the run
    # stopped aborting on the first duplicate — made the request exceed its
    # timeout on any non-trivial tenant. Poll ``GET /crystallize/reports``.
    report_id = await start_crystallization(
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
        # Scheduled per tenant for the same reason, and more so: this endpoint
        # fans out, so awaiting each run in turn makes the request's cost the SUM
        # of them.
        report_id = await start_crystallization(
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


@router.get(
    "/crystallize/reports/{report_id}",
    responses={200: {"model": _oar.CrystallizeReport}},
)
async def get_report(
    report_id: UUID,
    tenant_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get a full crystallization report by ID, from ``tenant_id``.

    The caller names the tenant; the credential decides whether it may read it.
    ``enforce_readable_tenant`` — not ``enforce_tenant``, which is the WRITE
    gate and pins to the home tenant — so a cross-tenant read key still reaches
    any tenant in its ``readable_tenant_ids``, and an admin key any tenant at
    all. That set is derived from the credential, never from the request, which
    is what keeps this from being the "caller names its own scope" shape.

    **Report existence still collapses to 404** (audit finding #22). The 403
    raised below is keyed on the NAMED TENANT and is independent of
    ``report_id``: it says only "this credential may not read tenant T", which
    the caller already knows about its own key. Every question about a report —
    absent, or present in a tenant this request did not name — answers 404, so
    a random UUID reveals nothing. There is a regression test for exactly this
    matrix in ``tests/test_api_crystallizer.py``.

    ``tenant_id`` is required for admin callers too. ``AuthContext`` for an
    admin key has ``tenant_id=None``, so there is no tenant to infer, and
    storage now demands one; naming it is the API change this endpoint takes in
    exchange for storage no longer answering to a bare primary key.
    """
    auth.enforce_readable_tenant(tenant_id)
    sc = get_storage_client()
    # No post-fetch tenant comparison: storage scopes the fetch to the tenant
    # authorized above, so a report outside it is already a 404 on the line
    # below. The comparison that stood here ran AFTER storage had handed the
    # row over — it protected this route's callers while leaving the storage
    # route itself answering to a bare id, which is the half an attacker who
    # can reach port 8002 does not go through.
    report = await sc.get_report(str(report_id), tenant_id)
    if not report:
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


@router.get(
    "/crystallize/latest",
    responses={200: {"model": _oar.CrystallizeReport | None}},
)
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
