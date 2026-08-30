"""``PATCH /entities/{entity_id}`` must be told which tenant it writes for.

The write form of GHSA-wgvw-28pq-jc36 on the entity graph. The route took a
bare UUID and ``entity_update`` fetched by primary key, so a caller who knew an
id could rewrite another tenant's entity through a service that authenticates
nothing.

Scoping the fetch alone would not have closed it. The old body loop was
``if hasattr(entity, key): setattr(entity, key, value)``, which reaches every
mapped column --- so a caller who satisfied the predicate could still write
``tenant_id`` and hand the row to another namespace, or repoint ``id``. Two
things now stop the tenant case, and they fail independently: the route removes
``tenant_id`` from the body to use it as the predicate, and
``_ENTITY_UPDATABLE_FIELDS`` would drop it regardless. Because either alone
suffices, no test here can isolate one of them --- what is pinned below is the
observable outcome, which needed both to be absent to occur. The primary key is
the case reachable through the allowlist alone, and
``test_scope_columns_are_not_caller_writable`` is what pins it.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _entity(client: AsyncClient, tenant_id: str, name: str) -> str:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": name,
            "attributes": {"role": "engineer"},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _read(client: AsyncClient, entity_id: str) -> dict:
    """Read the row back regardless of tenant, to assert what actually persisted.

    Deliberately goes through ``GET /entities/{entity_id}``, which is still
    unscoped (allowlisted ``id-addressed-read``) and so answers for any tenant
    --- that is what makes it a usable oracle here. When that route gains a
    required tenant, this helper needs one too.
    """
    resp = await client.get(f"{PREFIX}/entities/{entity_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestEntityUpdateTenantScope:
    async def test_another_tenants_entity_is_not_updated(self, client: AsyncClient) -> None:
        """The attack, stated as the request an attacker would send."""
        victim = f"ent-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"ent-attacker-{uuid.uuid4().hex[:8]}"
        name = f"Victim-{uuid.uuid4().hex[:8]}"
        victim_id = await _entity(client, victim, name)

        resp = await client.patch(
            f"{PREFIX}/entities/{victim_id}",
            json={"tenant_id": attacker, "canonical_name": "OWNED", "attributes": {"role": "attacker"}},
        )

        assert resp.status_code == 404, resp.text
        row = await _read(client, victim_id)
        assert row["canonical_name"] == name, "the victim's entity was rewritten"
        assert row["attributes"] == {"role": "engineer"}
        # Naming a tenant you don't own re-points the predicate; it never moves
        # the row into the tenant you named.
        assert row["tenant_id"] == victim

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "update by primary key"; it is now a 422."""
        tenant = f"ent-{uuid.uuid4().hex[:8]}"
        name = f"Mine-{uuid.uuid4().hex[:8]}"
        entity_id = await _entity(client, tenant, name)

        resp = await client.patch(f"{PREFIX}/entities/{entity_id}", json={"canonical_name": "CHANGED"})

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
        assert (await _read(client, entity_id))["canonical_name"] == name

    async def test_own_entity_is_updated(self, client: AsyncClient) -> None:
        """The supported call still works."""
        tenant = f"ent-{uuid.uuid4().hex[:8]}"
        entity_id = await _entity(client, tenant, f"Mine-{uuid.uuid4().hex[:8]}")

        resp = await client.patch(
            f"{PREFIX}/entities/{entity_id}",
            json={"tenant_id": tenant, "canonical_name": "Renamed", "attributes": {"role": "staff"}},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["canonical_name"] == "Renamed"
        row = await _read(client, entity_id)
        assert row["canonical_name"] == "Renamed"
        assert row["attributes"] == {"role": "staff"}
        assert row["tenant_id"] == tenant

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both 404, so the endpoint is not an existence oracle for entity UUIDs."""
        victim = f"ent-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"ent-attacker-{uuid.uuid4().hex[:8]}"
        victim_id = await _entity(client, victim, f"Victim-{uuid.uuid4().hex[:8]}")

        body = {"tenant_id": attacker, "canonical_name": "OWNED"}
        foreign = await client.patch(f"{PREFIX}/entities/{victim_id}", json=body)
        missing = await client.patch(f"{PREFIX}/entities/{uuid.uuid4()}", json=body)

        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    async def test_scope_columns_are_not_caller_writable(self, client: AsyncClient) -> None:
        """``id`` and ``fleet_id`` are outside ``_ENTITY_UPDATABLE_FIELDS``.

        The primary key was genuinely rewritable through this route: the row
        moved to the caller's chosen UUID, the old id 404'd, and every
        ``memory_entity_links`` row still pointing at the old value was
        stranded. Unlisted keys are dropped rather than rejected, matching
        ``memory_update`` --- so the request succeeds and the columns hold.
        """
        tenant = f"ent-{uuid.uuid4().hex[:8]}"
        entity_id = await _entity(client, tenant, f"Mine-{uuid.uuid4().hex[:8]}")
        stolen = str(uuid.uuid4())

        resp = await client.patch(
            f"{PREFIX}/entities/{entity_id}",
            json={
                "tenant_id": tenant,
                "id": stolen,
                "fleet_id": "fleet-attacker",
                "canonical_name": "Renamed",
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["canonical_name"] == "Renamed", "the writable field should still apply"
        row = await _read(client, entity_id)
        assert row["id"] == entity_id, "the primary key was repointed"
        assert row["fleet_id"] is None
        assert (await client.get(f"{PREFIX}/entities/{stolen}")).status_code == 404
