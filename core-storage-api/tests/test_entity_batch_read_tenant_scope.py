"""``POST /entities/by-ids`` — the batch entity read, and its tenant predicate.

Added for oss-0902-m-05: the contradiction detector hydrated every entity link
with its own ``GET /entities/{id}``, so a Path C pass made one HTTP call per
link per candidate. This route collapses that into one query. A batch read is
the SHARPER form of the primitive GHSA-wgvw-28pq-jc36 describes — one request
can name many ids — so it ships with the same ``tenant_id`` predicate
``GET /entities/{entity_id}`` got in #1174, applied in SQL rather than after
the rows cross the boundary.

**Filter, not reject**, on a partial match. An id outside the tenant is absent
from the mapping, which is already the answer for an id that does not exist.
The alternative — 404 the whole request — would tell the caller that one of
the ids it named exists somewhere else, i.e. turn the route into the existence
oracle the per-id 404 deliberately is not. #1162 made the same choice for the
entity-link batch read. ``test_a_mixed_request_returns_only_the_callers_rows``
is the load-bearing one: it is the request an attacker actually sends, padding
their own ids around a guessed foreign one.

Each assertion checks the victim's data is ABSENT from the body, not merely
that the key is missing — a key-only check would still pass if a refactor
re-keyed the mapping while leaking the row.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


def _tenant() -> str:
    """Same ``test-tenant-`` prefix contract as the sibling suites — the root
    suite's teardown only reclaims tenants matching it (see #858)."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _entity(
    client: AsyncClient,
    tenant_id: str,
    name: str | None = None,
    secret: str = "victim-only",
) -> dict:
    """``secret`` is per-row on purpose: the mixed-request test returns the
    caller's OWN row, so a shared marker string would be present legitimately
    and the absence assertion would be checking nothing."""
    resp = await client.post(
        f"{PREFIX}/entities",
        json={
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": name or f"BatchRead-{uuid.uuid4().hex[:8]}",
            "attributes": {"secret": secret},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _by_ids(client: AsyncClient, ids: list[str], tenant_id: str | None) -> object:
    body: dict = {"entity_ids": ids}
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    return await client.post(f"{PREFIX}/entities/by-ids", json=body)


class TestBatchEntityRead:
    async def test_own_rows_come_back_keyed_by_id(self, client: AsyncClient) -> None:
        tenant = _tenant()
        a, b = await _entity(client, tenant), await _entity(client, tenant)

        resp = await _by_ids(client, [a["id"], b["id"]], tenant)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {a["id"], b["id"]}
        assert body[a["id"]]["canonical_name"] == a["canonical_name"]
        assert body[b["id"]]["entity_type"] == "person"

    async def test_a_foreign_id_is_absent_rather_than_returned(self, client: AsyncClient) -> None:
        victim, attacker = _tenant(), _tenant()
        row = await _entity(client, victim, name=f"Victim-{uuid.uuid4().hex[:8]}")

        resp = await _by_ids(client, [row["id"]], attacker)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {}
        assert row["canonical_name"] not in resp.text
        assert "victim-only" not in resp.text

    async def test_a_mixed_request_returns_only_the_callers_rows(self, client: AsyncClient) -> None:
        """The real attack shape: own ids padding a guessed foreign one."""
        victim, attacker = _tenant(), _tenant()
        stolen = await _entity(client, victim, name=f"Victim-{uuid.uuid4().hex[:8]}")
        mine = await _entity(client, attacker, secret="attacker-own")

        resp = await _by_ids(client, [mine["id"], stolen["id"]], attacker)

        assert resp.status_code == 200, resp.text
        assert set(resp.json()) == {mine["id"]}
        assert stolen["canonical_name"] not in resp.text
        assert "victim-only" not in resp.text

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Absent either way, so this is not an existence oracle for UUIDs."""
        victim, attacker = _tenant(), _tenant()
        row = await _entity(client, victim)

        foreign = await _by_ids(client, [row["id"]], attacker)
        missing = await _by_ids(client, [str(uuid.uuid4())], attacker)

        assert foreign.status_code == missing.status_code == 200
        assert foreign.json() == missing.json() == {}

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant must never mean "fetch by primary key" — the shape #1174
        removed from the per-id route must not reappear on the batch one."""
        row = await _entity(client, _tenant(), name=f"Victim-{uuid.uuid4().hex[:8]}")

        resp = await _by_ids(client, [row["id"]], None)

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
        assert row["canonical_name"] not in resp.text

    async def test_a_malformed_id_is_a_422_not_a_500(self, client: AsyncClient) -> None:
        """The per-id route gets this from FastAPI's path coercion; a list in
        the body has to validate itself or ``UUID(...)`` raises into a 500."""
        resp = await _by_ids(client, ["not-a-uuid"], _tenant())

        assert resp.status_code == 422, resp.text

    async def test_a_non_list_payload_is_a_422(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/entities/by-ids",
            json={"tenant_id": _tenant(), "entity_ids": "e1"},
        )

        assert resp.status_code == 422, resp.text

    async def test_an_empty_list_is_an_empty_mapping(self, client: AsyncClient) -> None:
        """The detector calls this only when links exist, but a caller that
        batches an empty set must get ``{}`` rather than every row."""
        tenant = _tenant()
        await _entity(client, tenant)

        resp = await _by_ids(client, [], tenant)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {}
