"""``memory_update`` must not let the caller rewrite the row's identity columns.

``memory_update``'s tenant predicate is correct and has been for a while: every
statement carries ``Memory.tenant_id == tenant_id``. What it did not have was
any restriction on *which* columns the patch could set --- ``values`` was built
with ``hasattr(Memory, key)``, which is true of ``id`` and ``tenant_id``. The
statement then became ``UPDATE memories SET id = <caller's choice> WHERE
id = :memory_id AND tenant_id = :tenant_id``: the predicate is satisfied, and
the row's primary key is rewritten to a value the caller picked. Every
``memory_entity_links`` row, ``supersedes_id`` reference, and client-held id
still points at the old value.

A scoped predicate is worth exactly as much as the immutability of the column
it filters on. This is the same defect that was closed on the entity side in
caura-ai/caura#1119, found while fixing #1081, and filed from there as #1118.
``_MEMORY_UPDATABLE_FIELDS`` carries the reasoning for the writable set and for
why it is derived by subtraction rather than listed like the entity one.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

import core_storage_api.services.postgres_service as ps
from common.models import Memory
from core_storage_api.services.postgres_service import PostgresService, get_session
from tests.test_integration import PREFIX

# Deliberately NOT a module-level ``asyncio`` mark: the schema-drift guard at
# the bottom is a plain sync ``def``, and a module-level mark would flag it.
# Same reason ``tests/test_patch_null_non_nullable.py`` keeps its module clean.


async def _memory(client: AsyncClient, tenant_id: str, content: str = "canary") -> str:
    resp = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "immutable-tester",
            "content": f"{content} {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
class TestMemoryUpdateImmutableColumns:
    async def test_the_primary_key_is_not_caller_writable(self, client: AsyncClient) -> None:
        """The reported defect, stated as the request that exercised it.

        Before: 200, the old id 404s, and the row answers at ``stolen``.
        """
        tenant = f"imm-{uuid.uuid4().hex[:8]}"
        memory_id = await _memory(client, tenant)
        stolen = str(uuid.uuid4())

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={"tenant_id": tenant, "id": stolen, "title": "kept"},
        )

        assert resp.status_code == 200, resp.text
        still_there = await client.get(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant})
        assert still_there.status_code == 200, "the row was moved off its primary key"
        assert still_there.json()["id"] == memory_id
        # The writable field in the same body still applied, so the unlisted key
        # was dropped rather than the whole patch being rejected.
        assert still_there.json()["title"] == "kept"
        moved = await client.get(f"{PREFIX}/memories/{stolen}", params={"tenant_id": tenant})
        assert moved.status_code == 404

    async def test_the_row_cannot_be_moved_to_another_tenant(self, client: AsyncClient) -> None:
        """Driven at the service, because the route pops ``tenant_id`` first.

        ``PATCH /memories/{memory_id}`` takes the tenant from the body and
        removes it before the rest becomes the patch, so over HTTP this key can
        never reach the column set. ``memory_update`` is a public service method
        that a future route could call without that pop, which is what this
        pins --- and what makes the exclusion more than decoration.
        """
        svc = PostgresService()
        owner = f"imm-owner-{uuid.uuid4().hex[:8]}"
        other = f"imm-other-{uuid.uuid4().hex[:8]}"
        memory_id = uuid.UUID(await _memory(client, owner))

        applied = await svc.memory_update(memory_id, owner, {"tenant_id": other, "title": "kept"})

        assert applied is True
        row = await svc.memory_get_by_id_for_tenant(memory_id, owner)
        assert row is not None, "the row was handed to another tenant"
        assert row.tenant_id == owner
        assert row.title == "kept"

    async def test_the_search_vector_is_not_caller_writable(self, client: AsyncClient) -> None:
        """Writing the vector alone evades the trigger and erases keyword recall.

        ``memories_search_vector_trigger`` is ``BEFORE INSERT OR UPDATE OF
        content, title``, so a patch naming only ``search_vector`` never fires
        it and the caller's value is what persists. Before this change that
        returned 200 and the row silently stopped matching its own content ---
        no content change, nothing in the row to explain the disappearance.
        """
        tenant = f"imm-{uuid.uuid4().hex[:8]}"
        word = f"zebrafish{uuid.uuid4().hex[:6]}"
        memory_id = await _memory(client, tenant, content=f"the {word} swims")

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={"tenant_id": tenant, "search_vector": ""},
        )

        assert resp.status_code == 200, resp.text
        async with get_session() as session:
            hit = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM memories "
                        "WHERE id = :i AND search_vector @@ plainto_tsquery('english', :w)"
                    ),
                    {"i": memory_id, "w": word},
                )
            ).scalar_one()
        assert hit == 1, "the row was dropped from keyword recall by a patch that changed no content"

    async def test_the_fleet_scope_is_not_caller_writable(self, client: AsyncClient) -> None:
        """``fleet_id`` is scope, not content, and no caller patches it."""
        tenant = f"imm-{uuid.uuid4().hex[:8]}"
        memory_id = await _memory(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={"tenant_id": tenant, "fleet_id": "attacker-fleet", "title": "kept"},
        )

        assert resp.status_code == 200, resp.text
        row = (await client.get(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant})).json()
        assert row["fleet_id"] != "attacker-fleet", "the memory was moved between fleet scopes"
        assert row["title"] == "kept"

    async def test_a_normal_patch_still_applies(self, client: AsyncClient) -> None:
        """Regression guard: narrowing the writable set must not drop real writes."""
        tenant = f"imm-{uuid.uuid4().hex[:8]}"
        memory_id = await _memory(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={
                "tenant_id": tenant,
                "title": "retitled",
                "weight": 0.9,
                "memory_type": "preference",
            },
        )

        assert resp.status_code == 200, resp.text
        body = (await client.get(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant})).json()
        assert body["title"] == "retitled"
        assert body["weight"] == 0.9
        assert body["memory_type"] == "preference"


def test_no_identity_column_is_writable() -> None:
    """Schema-drift guard: a new identity-ish column must not arrive writable.

    Asserts against ``_MEMORY_UPDATABLE_FIELDS`` --- the set ``memory_update``
    actually reads --- rather than the exclusion list, so it stays honest if the
    two ever stop lining up. Derived from the model, so widening the primary key
    or renaming a column fails here rather than in production.

    Reached through the module object: the attribute lookup happens at call
    time, so this file still *collects* against a commit where the constant does
    not exist yet, which keeps the fails-on-parent check readable as one failing
    test rather than an uncollectible module.
    """
    updatable = ps._MEMORY_UPDATABLE_FIELDS
    pk = {c.key for c in Memory.__table__.primary_key.columns}
    assert pk.isdisjoint(updatable), f"primary key {pk & updatable} is caller-writable"
    assert updatable.isdisjoint({"tenant_id", "fleet_id"}), "a scope column is caller-writable"
    assert "search_vector" not in updatable, "the trigger-maintained vector is caller-writable"
    assert ps._MEMORY_IMMUTABLE_FIELDS <= ps._MEMORY_VALID_FIELDS, (
        "an immutable name that is not a real column protects nothing: "
        f"stale={sorted(ps._MEMORY_IMMUTABLE_FIELDS - ps._MEMORY_VALID_FIELDS)}"
    )
