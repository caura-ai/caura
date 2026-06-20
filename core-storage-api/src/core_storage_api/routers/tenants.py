"""Tenant-discovery endpoints (Fix 2 Phase 1).

The lifecycle-fanout entry point (core-api ``routes/lifecycle.py``) needs the
list of orgs to publish a per-org message to, per action. Those discovery
queries used to run on core-api's own DB pool; they move here behind
core-storage-api per the "no DB outside core-storage-api" rule. All reads
(reader replica); core-api calls them via its storage_client.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/tenants", tags=["Tenants"])
_svc = PostgresService()


class TenantIdsResponse(BaseModel):
    """Memory-backed discovery lists are keyed by ``tenant_id``."""

    tenant_ids: list[str]


class OrgIdsResponse(BaseModel):
    """Settings-backed discovery list is keyed by ``org_id``."""

    org_ids: list[str]


@router.get("/active")
async def list_active_tenants() -> TenantIdsResponse:
    """Orgs with at least one live (non-soft-deleted) memory. Archive/lifecycle
    fanout target."""
    return TenantIdsResponse(tenant_ids=await _svc.tenants_list_active())


@router.get("/purgeable")
async def list_purgeable_tenants() -> TenantIdsResponse:
    """Orgs with soft-deleted memories older than the max retention window
    (CAURA-656 purge fanout target)."""
    return TenantIdsResponse(tenant_ids=await _svc.tenants_list_purgeable())


@router.get("/skills-factory-enabled")
async def list_skills_factory_enabled_orgs() -> OrgIdsResponse:
    """Orgs whose ``skills_factory.enabled`` setting is True (forge-distill
    fanout target)."""
    return OrgIdsResponse(org_ids=await _svc.tenants_list_skills_factory_enabled())
