"""L-49: re-upserting a relation without a weight keeps the weight it has.

``relation_add`` upserted on the natural key with ``weight =
EXCLUDED.weight``, and ``RelationUpsert.weight`` defaulted to 1.0, so every
caller sent one: the extraction worker never names a weight, and neither need a
caller of ``POST /relations/upsert``. ``entity_infer_relations`` grades
``related_to`` weights by co-occurrence, in (0, 1.0]; one re-upsert of the same
pair snapped that signal to 1.0, and the response echoed 1.0 as authoritative.

An omitted weight now keeps the stored one, a new relation still starts at 1.0,
and a caller that names a weight still sets it.

Real storage through the in-process bridge.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from core_api.schemas import RelationUpsert
from core_api.services.entity_service import upsert_relation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _pair(sc) -> tuple[str, UUID, UUID]:
    tenant = f"test-l49-{uuid4().hex[:8]}"
    ids = []
    for name in ("atlas", "helios"):
        row = await sc.create_entity(
            {
                "tenant_id": tenant,
                "entity_type": "project",
                "canonical_name": name,
                "attributes": {},
            }
        )
        ids.append(UUID(str(row["id"])))
    return tenant, ids[0], ids[1]


async def _inferred(sc, tenant: str, a: UUID, b: UUID, weight: float) -> None:
    """A graded edge, as ``entity_infer_relations`` leaves one."""
    await sc.create_relation(
        {
            "tenant_id": tenant,
            "from_entity_id": str(a),
            "relation_type": "related_to",
            "to_entity_id": str(b),
            "weight": weight,
        }
    )


async def test_a_re_upsert_without_a_weight_keeps_the_stored_one(sc):
    tenant, a, b = await _pair(sc)
    await _inferred(sc, tenant, a, b, 0.3)

    out = await upsert_relation(
        RelationUpsert(
            tenant_id=tenant,
            from_entity_id=a,
            relation_type="related_to",
            to_entity_id=b,
        )
    )

    assert out.weight == pytest.approx(0.3)


async def test_a_named_weight_still_replaces_it(sc):
    """The control: a caller that states a weight still sets it."""
    tenant, a, b = await _pair(sc)
    await _inferred(sc, tenant, a, b, 0.3)

    out = await upsert_relation(
        RelationUpsert(
            tenant_id=tenant,
            from_entity_id=a,
            relation_type="related_to",
            to_entity_id=b,
            weight=0.7,
        )
    )

    assert out.weight == pytest.approx(0.7)


async def test_a_new_relation_without_a_weight_starts_at_one(sc):
    """The control: only an existing edge keeps its weight."""
    tenant, a, b = await _pair(sc)

    out = await upsert_relation(
        RelationUpsert(
            tenant_id=tenant,
            from_entity_id=a,
            relation_type="related_to",
            to_entity_id=b,
        )
    )

    assert out.weight == pytest.approx(1.0)
