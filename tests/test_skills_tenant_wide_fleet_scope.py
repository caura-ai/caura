"""Tenant-wide skills (``fleet_id=NULL``) reach fleet-bound plugin nodes.

The nightly Skill Factory mints skills with no fleet. ``/skills/installable``
and the plugin's ``caura_doc`` query filtered with exact fleet equality, so a
node configured with ``CAURA_FLEET_ID`` never received any of them. Other
collections keep exact fleet matching.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from common.models import Document
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_svc = PostgresService()


async def _doc(tenant: str, collection: str, doc_id: str, fleet_id: str | None) -> None:
    async with get_session() as s:
        s.add(
            Document(
                id=uuid4(),
                tenant_id=tenant,
                fleet_id=fleet_id,
                collection=collection,
                doc_id=doc_id,
                data={"name": doc_id, "status": "active"},
            )
        )


async def _ids(tenant: str, collection: str, fleet_id: str | None) -> set[str]:
    rows = await _svc.document_query(
        tenant_id=tenant, collection=collection, fleet_id=fleet_id, limit=50
    )
    return {r.doc_id if hasattr(r, "doc_id") else r["doc_id"] for r in rows}


async def test_a_fleet_node_receives_tenant_wide_skills_but_not_other_fleets():
    tenant = f"test-tenant-skills-{uuid4().hex[:8]}"
    await _doc(tenant, "skills", "tenant-wide", None)
    await _doc(tenant, "skills", "fleet-a-only", "fleet-a")
    await _doc(tenant, "skills", "fleet-b-only", "fleet-b")
    assert await _ids(tenant, "skills", "fleet-a") == {"tenant-wide", "fleet-a-only"}


async def test_no_fleet_still_returns_every_skill():
    tenant = f"test-tenant-skills-{uuid4().hex[:8]}"
    await _doc(tenant, "skills", "tenant-wide", None)
    await _doc(tenant, "skills", "fleet-a-only", "fleet-a")
    assert await _ids(tenant, "skills", None) == {"tenant-wide", "fleet-a-only"}


async def test_other_collections_keep_exact_fleet_matching():
    tenant = f"test-tenant-skills-{uuid4().hex[:8]}"
    await _doc(tenant, "notes", "tenant-wide", None)
    await _doc(tenant, "notes", "fleet-a-only", "fleet-a")
    assert await _ids(tenant, "notes", "fleet-a") == {"fleet-a-only"}
