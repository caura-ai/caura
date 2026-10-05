"""M-94: a cross-tenant credential is not the author of a sibling tenant's
same-named agent's private rows.

``agent_id`` is unique per tenant only. A credential bound to ``rollup-bot`` in
its home tenant, reading a sibling tenant it may read, is not the sibling's
``rollup-bot``. Memory reads and the by-id gate already pair the identity with
the caller's home tenant (caura PR #1775). Four surfaces still matched the bare
name and handed back the sibling's ``scope_agent`` data:

- ``GET /entities/{id}``: the linked memories, in full, and the edges they are
  evidence for;
- ``GET /graph``: those edges;
- ``GET /memories/stats`` and ``GET /memories/count``: their count.

Real storage through the in-process bridge, so the predicates that decide are
the ones a deployment runs. The caller's own private note in its home tenant is
the control: it stays visible and counted.
"""

from __future__ import annotations

import pytest

from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.tenant_context import set_current_tenant
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio

AGENT = "rollup-bot"


@pytest.fixture
def as_cross_tenant():
    """An agent-bound credential for ``AGENT`` at home, reading ``readable``."""

    def _install(home: str, readable: list[str]) -> None:
        async def _dep():
            set_current_tenant(home)
            return AuthContext(
                tenant_id=home,
                agent_id=AGENT,
                agent_id_verified=True,
                readable_tenant_ids=readable,
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _tenant_with_notes(sc, visibilities) -> tuple[str, str, dict[str, str]]:
    """A tenant whose ``AGENT`` wrote one note per visibility, each linked to one
    entity and the evidence for one edge out of it. No agent row is needed: the
    rows carry the name."""
    tenant = new_tenant_id()

    async def _entity(name: str) -> str:
        created = await sc.create_entity(
            {"tenant_id": tenant, "entity_type": "concept", "canonical_name": name}
        )
        return str(created["id"])

    entity = await _entity("zenith acquisition")
    notes = {}
    for visibility in visibilities:
        memory = await sc.create_memory(
            {
                "tenant_id": tenant,
                "agent_id": AGENT,
                "memory_type": "fact",
                "content": f"Zenith acquisition terms, {visibility}.",
                "status": "active",
                "visibility": visibility,
            }
        )
        notes[visibility] = str(memory["id"])
        await sc.create_entity_link(
            tenant,
            {"memory_id": notes[visibility], "entity_id": entity, "role": "subject"},
        )
        await sc.create_relation(
            {
                "tenant_id": tenant,
                "from_entity_id": entity,
                "to_entity_id": await _entity(f"party {visibility}"),
                "relation_type": "involves",
                "evidence_memory_id": notes[visibility],
            }
        )
    return tenant, entity, notes


async def _setup(sc, as_cross_tenant) -> list[tuple[str, str, str]]:
    """``(tenant, entity, the one note the caller may read)``, home then sibling:
    its own private note at home, and the shared note in the sibling."""
    home, home_entity, home_notes = await _tenant_with_notes(sc, ["scope_agent"])
    sibling, entity, notes = await _tenant_with_notes(sc, ["scope_agent", "scope_team"])
    as_cross_tenant(home, [home, sibling])
    return [
        (home, home_entity, home_notes["scope_agent"]),
        (sibling, entity, notes["scope_team"]),
    ]


async def test_the_entity_read_withholds_the_siblings_private_note(
    client, sc, as_cross_tenant
):
    for tenant, entity, readable in await _setup(sc, as_cross_tenant):
        resp = await client.get(
            f"/api/v1/entities/{entity}", params={"tenant_id": tenant}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [m["id"] for m in body["linked_memories"]] == [readable]
        assert [r["evidence_memory_id"] for r in body["relations"]] == [readable]


async def test_the_graph_withholds_the_edge_the_private_note_is_evidence_for(
    client, sc, as_cross_tenant
):
    for tenant, _, readable in await _setup(sc, as_cross_tenant):
        resp = await client.get("/api/v1/graph", params={"tenant_id": tenant})
        assert resp.status_code == 200, resp.text
        edges = resp.json()["edges"]
        assert [e["evidence_memory_id"] for e in edges] == [readable]


@pytest.mark.parametrize(
    ("path", "params", "field"),
    [
        ("/api/v1/memories/stats", {"agent_id": AGENT}, "total"),
        ("/api/v1/memories/count", {}, "count"),
    ],
    ids=["stats", "count"],
)
async def test_counts_leave_out_the_siblings_private_note(
    client, sc, as_cross_tenant, path, params, field
):
    for tenant, _, _ in await _setup(sc, as_cross_tenant):
        resp = await client.get(path, params={"tenant_id": tenant, **params})
        assert resp.status_code == 200, resp.text
        assert resp.json()[field] == 1, (tenant, resp.json())
