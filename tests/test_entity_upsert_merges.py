"""An entity upsert merges into the stored row and never removes from it (L-46, L-181).

Both upsert paths used to read the entity, merge in core-api, and write the
whole ``attributes`` object back:

- the extraction worker built each update item from its ``bulk_resolve``
  snapshot, and ``entity_bulk_upsert`` assigned it with a plain UPDATE;
- the REST ``upsert_entity`` merged its ``find_exact`` snapshot and PATCHed it.

So two writers resolving to one entity each wrote their own alias list and the
last one won, and a key another writer added after the snapshot was deleted.
Storage now merges under a row lock: keys an upsert names take its value, every
other key stays, and ``_aliases`` is the union.

L-181's storage half: an update also rewrote the entity's HNSW-indexed
``name_embedding`` with the vector of whichever surface form mentioned it last.
The first-seen vector now stays; an unembedded entity still gets one.

Real storage throughout, through the in-process bridge.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest

from common.constants import VECTOR_DIM
from core_api.schemas import EntityUpsert
from core_api.services.entity_service import upsert_entity

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _tenant() -> str:
    return f"test-l46-{uuid4().hex[:8]}"


def _unit(i: int) -> list[float]:
    vec = [0.0] * VECTOR_DIM
    vec[i] = 1.0
    return vec


async def _entity(sc, tenant, name, attributes, **extra) -> str:
    row = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "organization",
            "canonical_name": name,
            "attributes": attributes,
            **extra,
        }
    )
    return str(row["id"])


def _update(tenant: str, entity_id: str, name: str, attributes: dict, **extra) -> dict:
    return {
        "input_idx": 0,
        "action": "update",
        "entity_id": entity_id,
        "tenant_id": tenant,
        "fleet_id": None,
        "entity_type": "organization",
        "canonical_name": name,
        "attributes": attributes,
        **extra,
    }


async def _attributes(sc, tenant: str, entity_id: str) -> dict:
    return (await sc.get_entity(entity_id, tenant))["attributes"]


async def test_two_updates_from_one_snapshot_keep_both_aliases(sc):
    """Two extractions resolve to the same row from the same snapshot."""
    tenant = _tenant()
    name = "ibm corporation"
    eid = await _entity(sc, tenant, name, {"_aliases": [name]})

    for alias in ("ibm", "i.b.m."):
        [row] = await sc.bulk_upsert_entities(
            items=[_update(tenant, eid, name, {"_aliases": [name, alias]})]
        )
        assert row["action"] == "updated"

    aliases = (await _attributes(sc, tenant, eid))["_aliases"]
    assert aliases == [name, "ibm", "i.b.m."]


async def test_an_update_adds_attributes_and_never_removes_one(sc):
    tenant = _tenant()
    eid = await _entity(sc, tenant, "acme", {"hq": "Paris", "founded": "1911"})

    await sc.bulk_upsert_entities(
        items=[_update(tenant, eid, "acme", {"hq": "Berlin", "_aliases": ["acme"]})]
    )

    assert await _attributes(sc, tenant, eid) == {
        "hq": "Berlin",
        "founded": "1911",
        "_aliases": ["acme"],
    }


async def test_a_create_that_finds_the_row_merges_into_it(sc):
    """The create branch's "already there" path wrote the item's attributes
    over the row's through ``entity_update``."""
    tenant = _tenant()
    eid = await _entity(sc, tenant, "acme", {"founded": "1911"})
    item = {**_update(tenant, eid, "acme", {"_aliases": ["acme"]}), "action": "create"}
    del item["entity_id"]

    [row] = await sc.bulk_upsert_entities(items=[item])

    assert (row["entity_id"], row["action"]) == (eid, "merged")
    assert await _attributes(sc, tenant, eid) == {
        "founded": "1911",
        "_aliases": ["acme"],
    }


async def test_a_rest_upsert_merges_into_the_row_not_its_snapshot(sc):
    """Another writer adds a key between the upsert's read and its write."""
    tenant = _tenant()
    eid = await _entity(sc, tenant, "acme", {"a": 1})
    find_exact = sc.find_exact_entity

    async def read_then_concurrent_write(*args, **kwargs):
        snapshot = await find_exact(*args, **kwargs)
        await sc.update_entity(eid, tenant, {"attributes": {"a": 1, "b": 2}})
        return snapshot

    with patch.object(sc, "find_exact_entity", new=read_then_concurrent_write):
        out = await upsert_entity(
            EntityUpsert(
                tenant_id=tenant,
                entity_type="organization",
                canonical_name="acme",
                attributes={"c": 3},
            )
        )

    stored = await _attributes(sc, tenant, eid)
    assert str(out.id) == eid
    assert (stored["a"], stored["b"], stored["c"]) == (1, 2, 3)
    assert stored["_aliases"] == ["acme"]
    assert out.attributes == stored


async def test_an_update_keeps_the_first_seen_name_embedding(sc):
    """L-181: a later mention's vector no longer replaces the stored one."""
    tenant = _tenant()
    eid = await _entity(sc, tenant, "acme", {}, name_embedding=_unit(0))

    await sc.bulk_upsert_entities(
        items=[_update(tenant, eid, "acme", {}, name_embedding=_unit(1))]
    )

    [top] = await sc.find_by_embedding_similarity(
        tenant, _unit(0), limit=1, entity_type="organization"
    )
    assert top["id"] == eid
    assert top["similarity"] > 0.99


async def test_an_update_embeds_an_entity_that_has_no_vector(sc):
    """The control for the one above: only an existing vector is kept."""
    tenant = _tenant()
    eid = await _entity(sc, tenant, "acme", {})

    await sc.bulk_upsert_entities(
        items=[_update(tenant, eid, "acme", {}, name_embedding=_unit(1))]
    )

    [top] = await sc.find_by_embedding_similarity(
        tenant, _unit(1), limit=1, entity_type="organization"
    )
    assert top["id"] == eid
    assert top["similarity"] > 0.99
