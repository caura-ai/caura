"""The ``/memories/{id}/relations`` storage endpoints.

``tests/test_memory_relations_schema.py`` covers what the DATABASE refuses. These cover
what the SERVICE has to get right on top of it, and the interesting half is idempotency:

  * a repeated link is a no-op, not a 409. ``relation_add`` (the entity equivalent) was
    once a plain INSERT, and the resulting IntegrityError cluster became 5xx storms with
    rows committed while the caller retried. An agent re-issuing ``memory_link`` must not
    reproduce that.
  * for a SYMMETRIC type the REVERSED pair is the same link. The schema accepts both rows
    — pinned deliberately in the schema tests — so only the service can collapse them.
  * for an ASYMMETRIC type the reversed pair is a DIFFERENT claim and must not collapse.
  * ``supersedes`` is a 422 naming the right field, not a 500 from the CHECK constraint.
  * a soft-deleted far endpoint leaves the row but drops out of the listing.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

PREFIX = "/api/v1/storage"


def _memory_payload(tenant_id: str, fleet_id: str, content: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "agent_id": "rel-agent",
        "memory_type": "fact",
        "content": content,
    }


async def _memory(client: AsyncClient, tenant_id: str, fleet_id: str, content: str) -> str:
    r = await client.post(
        f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id, f"{content} {uuid.uuid4().hex[:8]}")
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _body(tenant_id: str, to_id: str, rel: str, **kw) -> dict:
    return {"tenant_id": tenant_id, "to_memory_id": to_id, "relation_type": rel, **kw}


async def _relations(client: AsyncClient, mid: str, tenant_id: str, **params) -> list[dict]:
    r = await client.get(f"{PREFIX}/memories/{mid}/relations", params={"tenant_id": tenant_id, **params})
    assert r.status_code == 200, r.text
    return r.json()["relations"]


async def test_link_then_relink_is_idempotent(client: AsyncClient, tenant_id, fleet_id) -> None:
    a = await _memory(client, tenant_id, fleet_id, "elaborate source")
    b = await _memory(client, tenant_id, fleet_id, "elaborate target")

    first = await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, b, "elaborates"))
    assert first.status_code == 200, first.text
    again = await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, b, "elaborates"))
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"], "the row id must survive a re-link"
    assert len(await _relations(client, a, tenant_id)) == 1, "a repeated link must not add an edge"


async def test_a_symmetric_link_is_idempotent_in_the_REVERSED_direction(
    client: AsyncClient, tenant_id, fleet_id
) -> None:
    """The schema cannot enforce this, so the service must — this is the proof."""
    a = await _memory(client, tenant_id, fleet_id, "claim one")
    b = await _memory(client, tenant_id, fleet_id, "claim two")

    forward = await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, b, "contradicts"))
    assert forward.status_code == 200, forward.text
    reverse = await client.post(f"{PREFIX}/memories/{b}/relations", json=_body(tenant_id, a, "contradicts"))
    assert reverse.status_code == 200, reverse.text

    assert reverse.json()["id"] == forward.json()["id"], (
        "(A contradicts B) and (B contradicts A) are the same claim; the reversed call "
        "must return the existing row rather than storing a second edge"
    )
    assert reverse.json()["from_memory_id"] == a, (
        "the FIRST caller's direction is preserved — a reversed call does not re-point the row"
    )
    for endpoint in (a, b):
        assert len(await _relations(client, endpoint, tenant_id)) == 1


async def test_an_asymmetric_link_is_NOT_collapsed_when_reversed(
    client: AsyncClient, tenant_id, fleet_id
) -> None:
    """``depends_on`` reversed is a different claim, so it is a second edge."""
    a = await _memory(client, tenant_id, fleet_id, "the dependent")
    b = await _memory(client, tenant_id, fleet_id, "the dependency")

    one = await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, b, "depends_on"))
    two = await client.post(f"{PREFIX}/memories/{b}/relations", json=_body(tenant_id, a, "depends_on"))
    assert one.status_code == 200 and two.status_code == 200, (one.text, two.text)
    assert one.json()["id"] != two.json()["id"], (
        "direction carries meaning for depends_on; collapsing it would lose the claim"
    )
    assert len(await _relations(client, a, tenant_id)) == 2


async def test_supersedes_is_422_naming_the_right_field(client: AsyncClient, tenant_id, fleet_id) -> None:
    a = await _memory(client, tenant_id, fleet_id, "older")
    b = await _memory(client, tenant_id, fleet_id, "newer")
    r = await client.post(f"{PREFIX}/memories/{b}/relations", json=_body(tenant_id, a, "supersedes"))
    assert r.status_code == 422, r.text
    assert "supersedes_id" in r.text, "the error must point the caller at the right field"


async def test_an_unknown_relation_type_is_422(client: AsyncClient, tenant_id, fleet_id) -> None:
    a = await _memory(client, tenant_id, fleet_id, "one")
    b = await _memory(client, tenant_id, fleet_id, "two")
    r = await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, b, "caused_by"))
    assert r.status_code == 422, r.text


async def test_self_link_is_422(client: AsyncClient, tenant_id, fleet_id) -> None:
    a = await _memory(client, tenant_id, fleet_id, "lonely")
    r = await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, a, "related_to"))
    assert r.status_code == 422, r.text


async def test_an_absent_endpoint_is_404(client: AsyncClient, tenant_id, fleet_id) -> None:
    a = await _memory(client, tenant_id, fleet_id, "mine")
    r = await client.post(
        f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, str(uuid.uuid4()), "related_to")
    )
    assert r.status_code == 404, r.text


async def test_a_cross_tenant_endpoint_is_404(client: AsyncClient, tenant_id, fleet_id) -> None:
    """A link must not be usable to probe whether another tenant's memory id exists."""
    a = await _memory(client, tenant_id, fleet_id, "mine")
    other = await _memory(client, f"{tenant_id}-other", fleet_id, "theirs")
    r = await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, other, "related_to"))
    assert r.status_code == 404, r.text


async def test_a_soft_deleted_far_endpoint_drops_out_of_the_listing(
    client: AsyncClient, tenant_id, fleet_id
) -> None:
    """The edge survives (soft delete is reversible); the LISTING is what filters."""
    a = await _memory(client, tenant_id, fleet_id, "alt one")
    b = await _memory(client, tenant_id, fleet_id, "alt two")
    await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, b, "alternative_to"))
    assert len(await _relations(client, a, tenant_id)) == 1

    gone = await client.delete(f"{PREFIX}/memories/{b}", params={"tenant_id": tenant_id})
    assert gone.status_code in (200, 204), gone.text

    assert await _relations(client, a, tenant_id) == [], "a soft-deleted endpoint must not be reachable"


async def test_listing_can_filter_by_relation_type(client: AsyncClient, tenant_id, fleet_id) -> None:
    a = await _memory(client, tenant_id, fleet_id, "hub")
    b = await _memory(client, tenant_id, fleet_id, "spoke one")
    c = await _memory(client, tenant_id, fleet_id, "spoke two")
    await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, b, "elaborates"))
    await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, c, "related_to"))

    assert len(await _relations(client, a, tenant_id)) == 2
    only = await _relations(client, a, tenant_id, relation_type="elaborates")
    assert len(only) == 1 and only[0]["relation_type"] == "elaborates"


async def test_metadata_round_trips_and_is_not_wiped_by_a_relink(
    client: AsyncClient, tenant_id, fleet_id
) -> None:
    """Latest non-NULL metadata wins; omitting it must not erase what was recorded."""
    a = await _memory(client, tenant_id, fleet_id, "meta source")
    b = await _memory(client, tenant_id, fleet_id, "meta target")

    first = await client.post(
        f"{PREFIX}/memories/{a}/relations",
        json=_body(tenant_id, b, "related_to", metadata={"why": "same incident"}),
    )
    assert first.json()["metadata"] == {"why": "same incident"}, first.text

    again = await client.post(f"{PREFIX}/memories/{a}/relations", json=_body(tenant_id, b, "related_to"))
    assert again.json()["metadata"] == {"why": "same incident"}, (
        "a relink that omits metadata must COALESCE, not blank the existing value"
    )
