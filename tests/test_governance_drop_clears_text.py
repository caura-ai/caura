"""A governance drop keeps no copy of the text it dropped.

Fast-mode remediation drops a row that its tenant's PII or non-business policy
forbids. A plain soft delete keeps the text, title, embedding and metadata for
the undo window, and ``include_deleted`` reads return them until the purge tick
removes the row. ``drop_memory`` clears them instead, and keeps the lineage and
verdicts that the cascade, the graph purge and the audit trail rely on.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from common.constants import VECTOR_DIM
from common.models import Memory
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TEXT = "Dana's direct line is 555-0100"


def _tenant() -> str:
    return f"test-drop-{uuid.uuid4().hex[:8]}"


async def _seed(tenant: str, **overrides) -> uuid.UUID:
    data = {
        "tenant_id": tenant,
        "agent_id": "drop-tester",
        "content": _TEXT,
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "visibility": "scope_team",
        "title": "Dana's direct line",
        "embedding": [0.01] * VECTOR_DIM,
        "content_hash": "sha256:seed",
        "embedded_content_hash": "sha256:seed",
        "predicate": "has_phone",
        "object_value": "555-0100",
        "metadata_": {
            "summary": "Dana's phone number",
            "tags": ["contacts"],
            "contains_pii": True,
            "pii_types": ["phone"],
            "auto_chunked": True,
            "_system": {
                "atomic_facts": [_TEXT],
                "retrieval_hint": "Dana phone",
                "contains_pii": True,
            },
        },
    }
    data.update(overrides)
    return (await PostgresService().memory_add(data)).id


async def _row(memory_id: uuid.UUID) -> Memory:
    async with get_session() as session:
        return (
            await session.execute(select(Memory).where(Memory.id == memory_id))
        ).scalar_one()


async def test_a_drop_clears_the_text_and_keeps_the_lineage(sc):
    tenant = _tenant()
    mid = await _seed(tenant)

    assert await sc.drop_memory(str(mid), tenant) is True

    row = await _row(mid)
    assert row.deleted_at is not None
    assert row.status == "deleted"
    assert row.content == ""
    assert row.title is None
    assert row.embedding is None
    assert row.content_hash is None
    assert row.embedded_content_hash is None
    assert row.predicate is None
    assert row.object_value is None
    assert row.metadata_ == {
        "contains_pii": True,
        "pii_types": ["phone"],
        "auto_chunked": True,
        "_system": {"contains_pii": True},
    }


async def test_a_drop_clears_a_row_deleted_before_the_verdict(sc):
    # The author deleted it while enrichment ran; the soft delete kept the
    # text for the undo window, and the policy's verdict still has to clear it.
    tenant = _tenant()
    mid = await _seed(tenant)
    await PostgresService().memory_soft_delete_by_ids(tenant, [mid])
    deleted_at = (await _row(mid)).deleted_at

    assert await sc.drop_memory(str(mid), tenant) is True

    row = await _row(mid)
    assert row.content == ""
    assert row.deleted_at == deleted_at


async def test_a_drop_leaves_held_rows_and_other_tenants_alone(sc):
    tenant = _tenant()
    held = await _seed(tenant, status="quarantined")
    elsewhere = await _seed(_tenant())

    assert await sc.drop_memory(str(held), tenant) is False
    assert await sc.drop_memory(str(elsewhere), tenant) is False

    for mid in (held, elsewhere):
        row = await _row(mid)
        assert row.content == _TEXT
        assert row.deleted_at is None
