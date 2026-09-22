"""L-37 — ``POST /entities/relations/bulk``: one round-trip, N outcomes.

The entity-extraction worker batched its resolve, its entity upsert and its link
upsert, then spent one sequential ``POST /entities/relations`` per edge. This
route is the missing batch, and its contract has two halves that pull against
each other:

* one HTTP for N relations — the point of the exercise; and
* one VERDICT per relation, not one for the batch.

The second half is not a nicety. core-api guards every relation individually
because before #1495 a single failing upsert threw out of the whole extraction
and skipped the A65 predicate write-back and the ``Trigger.ENTITY`` fire — the
only caller of A40's deterministic RDF pass — leaving that memory out of the
non-stochastic contradiction path for good. A batch that succeeded or failed as
a unit would hand that back, so storage runs each item in its own session and
answers per item.

Tenant scope is inherited from the singular route (M-64) and re-asserted here
rather than assumed: the endpoints of an edge are entities, ``Relation.tenant_id``
describes the EDGE, and the FKs only require the rows to exist in some tenant.
A bulk route that skipped the ownership test would be a second door to the same
disclosure.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


def _tenant() -> str:
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _entity(client: AsyncClient, tenant_id: str) -> str:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": f"RelBulk-{uuid.uuid4().hex[:8]}",
            "attributes": {},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _item(idx: int, from_id: str, to_id: str, rel_type: str = "knows", **extra) -> dict:
    return {
        "input_idx": idx,
        "from_entity_id": from_id,
        "relation_type": rel_type,
        "to_entity_id": to_id,
        **extra,
    }


async def _bulk(client: AsyncClient, tenant_id: str, items: list[dict]):
    return await client.post(
        f"{PREFIX}/entities/relations/bulk",
        json={"tenant_id": tenant_id, "items": items},
    )


class TestBulkRelationUpsert:
    async def test_many_relations_land_in_one_call(self, client: AsyncClient) -> None:
        tenant = _tenant()
        a, b, c = [await _entity(client, tenant) for _ in range(3)]

        resp = await _bulk(
            client,
            tenant,
            [_item(0, a, b, "knows"), _item(1, a, c, "works_with"), _item(2, b, c, "reports_to")],
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [r["input_idx"] for r in body] == [0, 1, 2]
        assert all(r.get("error") is None for r in body), body
        assert [r["relation"]["relation_type"] for r in body] == [
            "knows",
            "works_with",
            "reports_to",
        ]

    async def test_the_response_is_aligned_to_the_input(self, client: AsyncClient) -> None:
        """Alignment is the whole interface. The caller maps outcome ``i`` back
        onto its own relation ``i``, so a response that merely has the right
        LENGTH would attribute one edge's verdict to another."""
        tenant = _tenant()
        a, b = await _entity(client, tenant), await _entity(client, tenant)

        resp = await _bulk(
            client, tenant, [_item(0, a, b, "alpha"), _item(1, b, a, "beta"), _item(2, a, b, "gamma")]
        )

        body = resp.json()
        for i, expected in enumerate(["alpha", "beta", "gamma"]):
            assert body[i]["input_idx"] == i
            assert body[i]["relation"]["relation_type"] == expected

    async def test_one_bad_endpoint_costs_one_item_not_the_batch(self, client: AsyncClient) -> None:
        """The property the per-item sessions exist for.

        A shared transaction would roll the good items back with the bad one,
        and core-api would lose every relation for that memory to one dead
        endpoint.
        """
        tenant = _tenant()
        a, b = await _entity(client, tenant), await _entity(client, tenant)
        ghost = str(uuid.uuid4())

        resp = await _bulk(
            client,
            tenant,
            [_item(0, a, b, "good_first"), _item(1, a, ghost, "bad"), _item(2, b, a, "good_last")],
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body[0]["error"] is None and body[0]["relation"] is not None
        assert body[1]["error"] == "fk_violation"
        assert body[1]["relation"] is None
        assert body[2]["error"] is None and body[2]["relation"] is not None

    async def test_a_foreign_endpoint_is_refused(self, client: AsyncClient) -> None:
        """M-64, through the new door. ``Relation.tenant_id`` describes the
        edge; the FKs only require the endpoints to exist SOMEWHERE."""
        attacker, victim = _tenant(), _tenant()
        mine = await _entity(client, attacker)
        theirs = await _entity(client, victim)

        resp = await _bulk(client, attacker, [_item(0, mine, theirs), _item(1, theirs, mine, "other_way")])

        body = resp.json()
        assert [r["error"] for r in body] == ["fk_violation", "fk_violation"]

    async def test_a_foreign_endpoint_is_indistinguishable_from_a_missing_one(
        self, client: AsyncClient
    ) -> None:
        """No existence oracle. This service authenticates nothing, so a
        distinguishable answer would confirm any UUID a caller cared to guess —
        in BULK, which is strictly worse than the singular route's one-at-a-time
        version of the same leak (GHSA-wgvw-28pq-jc36)."""
        attacker, victim = _tenant(), _tenant()
        mine = await _entity(client, attacker)
        theirs = await _entity(client, victim)
        nonexistent = str(uuid.uuid4())

        resp = await _bulk(client, attacker, [_item(0, mine, theirs), _item(1, mine, nonexistent)])

        body = resp.json()
        assert body[0] == {"input_idx": 0, "relation": None, "error": "fk_violation"}
        assert body[1] == {"input_idx": 1, "relation": None, "error": "fk_violation"}

    async def test_a_repeated_natural_key_upserts_rather_than_duplicating(self, client: AsyncClient) -> None:
        """Same idempotence the singular route promises, within one batch.

        The natural key is ``(tenant_id, from, relation_type, to)``, and the
        extraction worker re-sends the same edges every time a memory mentions
        the same pair — so a batch route that INSERTed would turn the
        ``uq_relations_natural_key`` violations #1088 removed back into 5xx.
        """
        tenant = _tenant()
        a, b = await _entity(client, tenant), await _entity(client, tenant)

        first = await _bulk(client, tenant, [_item(0, a, b, "knows", weight=0.4)])
        second = await _bulk(client, tenant, [_item(0, a, b, "knows", weight=0.9)])

        assert first.status_code == 200 and second.status_code == 200, second.text
        assert second.json()[0]["relation"]["id"] == first.json()[0]["relation"]["id"]
        assert second.json()[0]["relation"]["weight"] == pytest.approx(0.9)

    async def test_evidence_is_not_wiped_by_a_later_write_that_omits_it(self, client: AsyncClient) -> None:
        """The COALESCE in the ON CONFLICT clause, exercised through the batch.

        Shared with the singular route via ``_relation_upsert_and_fetch`` — this
        is the assertion that the sharing is real rather than a second copy that
        will drift.
        """
        tenant = _tenant()
        a, b = await _entity(client, tenant), await _entity(client, tenant)
        memory_id = await _memory(client, tenant)

        await _bulk(client, tenant, [_item(0, a, b, "knows", evidence_memory_id=memory_id)])
        second = await _bulk(client, tenant, [_item(0, a, b, "knows")])

        assert second.json()[0]["relation"]["evidence_memory_id"] == memory_id

    async def test_an_empty_batch_is_an_empty_answer(self, client: AsyncClient) -> None:
        tenant = _tenant()
        resp = await _bulk(client, tenant, [])
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_a_missing_tenant_is_refused(self, client: AsyncClient) -> None:
        """One tenant per request is what stops a batch spanning namespaces
        (#1124); without it there is nothing to scope the endpoints to."""
        resp = await client.post(
            f"{PREFIX}/entities/relations/bulk",
            json={"items": [_item(0, str(uuid.uuid4()), str(uuid.uuid4()))]},
        )
        assert resp.status_code == 422, resp.text

    async def test_a_malformed_uuid_is_a_422_not_a_500(self, client: AsyncClient) -> None:
        """Validated at the router boundary. Unvalidated, the service's
        ``UUID(...)`` raises from inside and the caller cannot tell a bad id
        from storage being broken."""
        tenant = _tenant()
        good = await _entity(client, tenant)

        resp = await _bulk(client, tenant, [_item(0, good, "not-a-uuid")])

        assert resp.status_code == 422, resp.text

    async def test_a_missing_field_is_a_422_not_a_500(self, client: AsyncClient) -> None:
        tenant = _tenant()
        good = await _entity(client, tenant)

        resp = await _bulk(client, tenant, [{"input_idx": 0, "from_entity_id": good, "to_entity_id": good}])

        assert resp.status_code == 422, resp.text
        assert "relation_type" in resp.text

    async def test_a_duplicate_input_idx_is_refused(self, client: AsyncClient) -> None:
        """``_validate_input_idxs``, the same gate the other bulk routes use.
        A duplicate index makes the aligned response a lie."""
        tenant = _tenant()
        a, b = await _entity(client, tenant), await _entity(client, tenant)

        resp = await _bulk(client, tenant, [_item(0, a, b, "x"), _item(0, b, a, "y")])

        assert resp.status_code == 422, resp.text

    async def test_the_batch_is_capped(self, client: AsyncClient) -> None:
        tenant = _tenant()
        ghost = str(uuid.uuid4())
        items = [_item(i, ghost, ghost) for i in range(501)]

        resp = await _bulk(client, tenant, items)

        assert resp.status_code == 422, resp.text
        assert "501" in resp.text


async def _memory(client: AsyncClient, tenant_id: str) -> str:
    from tests.test_integration import _memory_payload

    fleet_id = f"test-fleet-{uuid.uuid4().hex[:8]}"
    resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]
