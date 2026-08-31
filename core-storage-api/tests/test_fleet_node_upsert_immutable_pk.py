"""``POST /fleet/nodes`` must not let the caller choose an existing node's id.

``fleet_upsert_node`` builds its ``ON CONFLICT DO UPDATE`` SET clause from the
caller's dict with a denylist that named the conflict key but not the primary
key::

    set_={k: v for k, v in values.items() if k not in ("tenant_id", "node_name")}

The route hands it ``await request.json()`` with no schema and no key filter, so
``id`` reached the SET and a second POST for the same ``(tenant_id, node_name)``
rewrote the row's primary key to whatever the caller named.

Two outcomes, both pinned below, because they depend on whether anything
references the node:

* **No referencing commands** --- the repoint succeeds silently and the node's
  identity becomes caller-controlled.
* **A referencing command** --- ``FleetCommand.node_id`` is
  ``ForeignKey("fleet_nodes.id", ondelete="CASCADE")`` with no ``onupdate``, so
  ``NO ACTION`` applies and Postgres refuses. The ``ForeignKeyViolationError``
  escapes as an unhandled ``IntegrityError``, which turns a routine heartbeat
  upsert into a 500 the moment a command is queued for that node.

Third instance of one class --- a caller-controlled dict becoming an
``UPDATE ... SET`` with no restriction on identity columns. The others were
``entity_update`` (#1119) and ``memory_update`` (#1122). Note this is not a
tenant-scope hole: ``tenant_id`` was already excluded, which is exactly why the
tenant-scope gate never saw it.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

import core_storage_api.services.postgres_service as ps
from common.models import FleetNode
from tests.test_integration import PREFIX

# Deliberately NOT a module-level ``asyncio`` mark: the schema-drift guard at
# the bottom is a plain sync ``def``, which such a mark would flag.


async def _node(client: AsyncClient, tenant_id: str, node_name: str, **extra: object) -> dict:
    resp = await client.post(
        f"{PREFIX}/fleet/nodes",
        json={
            "tenant_id": tenant_id,
            "fleet_id": "f1",
            "node_name": node_name,
            "hostname": "h1",
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
class TestFleetNodeUpsertImmutablePk:
    async def test_the_primary_key_is_not_caller_writable(self, client: AsyncClient) -> None:
        """The reported defect: a re-POST silently moved the node to a chosen id."""
        tenant = f"fp-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        original = (await _node(client, tenant, name))["id"]
        chosen = str(uuid.uuid4())

        again = await client.post(
            f"{PREFIX}/fleet/nodes",
            json={
                "tenant_id": tenant,
                "fleet_id": "f1",
                "node_name": name,
                "hostname": "h2",
                "id": chosen,
            },
        )

        assert again.status_code == 200, again.text
        assert again.json()["id"] == original, "the node was moved to the caller's id"
        listed = await client.get(f"{PREFIX}/fleet/nodes", params={"tenant_id": tenant})
        ids = {n["id"] for n in listed.json()}
        assert original in ids
        assert chosen not in ids

    async def test_a_referencing_command_does_not_make_the_upsert_a_500(self, client: AsyncClient) -> None:
        """The availability half: the FK refused, and the error was unhandled.

        Once any command references the node, the old SET clause tried to move a
        primary key the child row points at. ``NO ACTION`` blocks it and the
        ``ForeignKeyViolationError`` propagated, so the heartbeat upsert 500'd
        rather than ignoring a key it should never have honoured.
        """
        tenant = f"fp-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        node_id = (await _node(client, tenant, name))["id"]
        queued = await client.post(
            f"{PREFIX}/fleet/commands",
            json={"tenant_id": tenant, "node_id": node_id, "command": "deploy", "payload": {}},
        )
        assert queued.status_code == 200, queued.text

        again = await client.post(
            f"{PREFIX}/fleet/nodes",
            json={
                "tenant_id": tenant,
                "fleet_id": "f1",
                "node_name": name,
                "hostname": "h3",
                "id": str(uuid.uuid4()),
            },
        )

        assert again.status_code == 200, again.text
        assert again.json()["id"] == node_id

    async def test_a_normal_upsert_still_updates_the_node(self, client: AsyncClient) -> None:
        """Regression guard: the conflict path must still apply real columns."""
        tenant = f"fp-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        original = (await _node(client, tenant, name))["id"]

        again = await _node(client, tenant, name, hostname="h2", ip="10.0.0.9")

        assert again["id"] == original, "the upsert should match the existing row, not insert"
        listed = (await client.get(f"{PREFIX}/fleet/nodes", params={"tenant_id": tenant})).json()
        row = next(n for n in listed if n["id"] == original)
        assert row["hostname"] == "h2"
        assert row["ip"] == "10.0.0.9"


def test_the_conflict_update_cannot_touch_identity_columns() -> None:
    """Schema-drift guard, derived from the model rather than echoing the literal.

    Reached through the module object so this file still *collects* against a
    commit where the constant does not exist yet, which keeps the fails-on-parent
    check readable as one failing test rather than an uncollectible module.
    """
    immutable = ps._FLEET_NODE_IMMUTABLE_FIELDS
    pk = {c.key for c in FleetNode.__table__.primary_key.columns}
    assert pk <= immutable, f"primary key {pk - immutable} is rewritable on conflict"
    # The conflict target itself: rewriting either half would move the row out
    # from under the constraint the statement just matched on.
    conflict_key = {"tenant_id", "node_name"}
    assert conflict_key <= immutable, f"conflict key {conflict_key - immutable} is rewritable"
    columns = {c.key for c in FleetNode.__table__.columns}
    assert immutable <= columns, (
        f"an immutable name that is not a real column protects nothing: stale={sorted(immutable - columns)}"
    )
