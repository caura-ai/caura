"""Tenant-wide skills (``fleet_id=NULL``) reach fleet-bound plugin nodes.

The nightly Skill Factory mints skills with no fleet. ``/skills/installable``
and the plugin's ``caura_doc`` query filtered with exact fleet equality, so a
node configured with ``CAURA_FLEET_ID`` never received any of them. Other
collections keep exact fleet matching.

H-06: caura PR #1772 fixed that for a read of the skills collection. The other
fleet-filtered document reads kept exact equality, so a fleet-bound caller still
missed tenant-wide skills in a collection-less search, the collections listing
and its counts, and the skills listing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from common.embedding import fake_embedding
from common.models import Document
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_svc = PostgresService()


async def _doc(
    tenant: str,
    collection: str,
    doc_id: str,
    fleet_id: str | None,
    *,
    indexed: bool = False,
) -> None:
    async with get_session() as s:
        s.add(
            Document(
                id=uuid4(),
                tenant_id=tenant,
                fleet_id=fleet_id,
                collection=collection,
                doc_id=doc_id,
                data={"name": doc_id, "status": "active"},
                embedding=fake_embedding(doc_id) if indexed else None,
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


async def _mixed_tenant(*, indexed: bool = False) -> str:
    """A tenant-wide skill, a fleet-a skill, a fleet-b skill, and notes both
    tenant-wide and fleet-a. Fleet a should see two skills and one note."""
    tenant = f"test-tenant-skills-{uuid4().hex[:8]}"
    for collection, doc_id, fleet in (
        ("skills", "tenant-wide-skill", None),
        ("skills", "fleet-a-skill", "fleet-a"),
        ("skills", "fleet-b-skill", "fleet-b"),
        ("notes", "tenant-wide-note", None),
        ("notes", "fleet-a-note", "fleet-a"),
    ):
        await _doc(tenant, collection, doc_id, fleet, indexed=indexed)
    return tenant


FLEET_A_SEES = {"tenant-wide-skill", "fleet-a-skill", "fleet-a-note"}


async def test_a_collection_less_search_finds_tenant_wide_skills():
    tenant = await _mixed_tenant(indexed=True)
    hits = await _svc.document_search(
        tenant_id=tenant,
        query_embedding=fake_embedding("skill"),
        collection=None,
        top_k=10,
        fleet_id="fleet-a",
    )
    assert {doc.doc_id for doc, _ in hits} == FLEET_A_SEES


async def test_a_collection_less_unindexed_count_includes_tenant_wide_skills():
    tenant = await _mixed_tenant()
    count = await _svc.document_count_unindexed(
        tenant_id=tenant, collection=None, fleet_id="fleet-a"
    )
    assert count == len(FLEET_A_SEES)


async def test_the_collections_listing_counts_tenant_wide_skills():
    tenant = await _mixed_tenant()
    listing = await _svc.document_list_collections(tenant_id=tenant, fleet_id="fleet-a")
    assert dict(listing) == {"notes": 1, "skills": 2}


async def test_the_skills_count_includes_tenant_wide_skills():
    tenant = await _mixed_tenant()
    count = await _svc.document_count_in_collection(
        tenant_id=tenant, collection="skills", fleet_id="fleet-a"
    )
    assert count == 2


async def test_the_skills_listing_includes_tenant_wide_skills():
    tenant = await _mixed_tenant()
    docs = await _svc.document_list_by_collection(
        tenant_id=tenant, collection="skills", fleet_id="fleet-a"
    )
    assert {d.doc_id for d in docs} == {"tenant-wide-skill", "fleet-a-skill"}


async def test_notes_keep_exact_fleet_matching_in_the_listing_and_counts():
    """Control: only skills are tenant-wide by convention."""
    tenant = await _mixed_tenant()
    notes = await _svc.document_list_by_collection(
        tenant_id=tenant, collection="notes", fleet_id="fleet-a"
    )
    count = await _svc.document_count_in_collection(
        tenant_id=tenant, collection="notes", fleet_id="fleet-a"
    )
    assert {d.doc_id for d in notes} == {"fleet-a-note"}
    assert count == 1
