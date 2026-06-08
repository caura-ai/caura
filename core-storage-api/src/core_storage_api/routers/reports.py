"""Crystallization report endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.schemas import REPORT_FIELDS, orm_to_dict
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
