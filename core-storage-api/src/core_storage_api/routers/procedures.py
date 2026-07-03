"""Procedure CRUD + reliability-stats endpoints (Procedural Memory PM-01).

The storage-side surface for the procedural-memory domain. core-api's
``procedure_service`` (PM-02) is the only intended caller; like the rest
of core-storage-api there is no router-level auth — trust is applied at
the core-api gateway. Mirrors ``routers/memories.py`` shape.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.schemas import (
    PROCEDURE_FIELDS,
    PROCEDURE_STATS_FIELDS,
    orm_to_dict,
)
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/procedures", tags=["Procedures"])
_svc = PostgresService()


_DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "last_success_at",
    "last_failure_at",
}


def _parse_datetimes(body: dict) -> dict:
    for key in _DATETIME_FIELDS:
        val = body.get(key)
        if isinstance(val, str):
            try:
                body[key] = datetime.fromisoformat(val)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid ISO datetime for field {key!r}: {val!r}",
                )
    return body


def _with_stats(procedure, stats) -> dict:
    """Serialise a procedure with its stats nested under ``stats``."""
    out = orm_to_dict(procedure, PROCEDURE_FIELDS)
    out["stats"] = orm_to_dict(stats, PROCEDURE_STATS_FIELDS) if stats else None
    return out


@router.post("")
async def create_procedure(request: Request) -> dict:
    body: dict = await request.json()
    stats_seed = body.pop("stats", None)
    _parse_datetimes(body)
    if isinstance(stats_seed, dict):
        body["stats"] = _parse_datetimes(stats_seed)
    procedure = await _svc.procedure_add(body)
    return _with_stats(procedure, procedure.stats)


@router.get("/{procedure_id}")
async def get_procedure(procedure_id: UUID) -> dict:
    procedure = await _svc.procedure_get_by_id(procedure_id)
    if procedure is None:
        raise HTTPException(status_code=404, detail="procedure not found")
    stats = await _svc.procedure_get_stats(procedure_id)
    return _with_stats(procedure, stats)


@router.get("")
async def list_procedures(
    tenant_id: str,
    fleet_id: str | None = None,
    include_quarantined: bool = False,
    limit: int = 200,
) -> list[dict]:
    rows = await _svc.procedure_list_for_tenant(
        tenant_id,
        fleet_id=fleet_id,
        include_quarantined=include_quarantined,
        limit=limit,
    )
    return [_with_stats(proc, stats) for proc, stats in rows]


@router.patch("/{procedure_id}/stats")
async def update_procedure_stats(procedure_id: UUID, request: Request) -> dict:
    patch: dict = await request.json()
    _parse_datetimes(patch)
    stats = await _svc.procedure_update_stats(procedure_id, patch)
    if stats is None:
        raise HTTPException(status_code=404, detail="procedure or stats not found")
    return orm_to_dict(stats, PROCEDURE_STATS_FIELDS)


@router.patch("/{procedure_id}")
async def patch_procedure(procedure_id: UUID, request: Request) -> dict:
    """Set a procedure's lifecycle ``status`` (used by invalidate).

    Body: ``{"status": "invalidated"}``. Distinct from the stats PATCH —
    this is the procedure-level lifecycle marker, not the reversible
    quarantine flag.
    """
    body: dict = await request.json()
    status = body.get("status")
    if not isinstance(status, str) or not status:
        raise HTTPException(status_code=422, detail="status (str) is required")
    procedure = await _svc.procedure_set_status(procedure_id, status)
    if procedure is None:
        raise HTTPException(status_code=404, detail="procedure not found")
    stats = await _svc.procedure_get_stats(procedure_id)
    return _with_stats(procedure, stats)


@router.delete("/{procedure_id}")
async def delete_procedure(procedure_id: UUID) -> dict:
    """Hard-delete a procedure (its 1:1 stats row CASCADEs)."""
    deleted = await _svc.procedure_delete(procedure_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="procedure not found")
    return {"deleted": str(procedure_id)}
