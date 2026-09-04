"""get_entity relations scope filter (audit S5) + single agent lookup (P1).

S5: ``get_entity`` filtered linked *memories* through the fleet/scope
contract but emitted ``relations`` straight from the raw entity — an agent
credential could enumerate relation edges and ``evidence_memory_id``s
pointing at memories it cannot read. Relations are now visible iff their
evidence memory is readable by the caller (no-evidence relations stay;
tenant/user credentials are unchanged).

P1: the per-memory loop issued one identical ``lookup_agent`` round-trip
per scope_team row (N+1). The caller's agent row is now resolved once.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _mem(mem_id, agent_id, visibility, fleet_id, content="c"):
    return {
        "memory": {
            "id": mem_id,
            "tenant_id": "t",
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "visibility": visibility,
            "memory_type": "fact",
            "content": content,
            "weight": 0.5,
            "source_uri": None,
            "run_id": None,
            "metadata": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
            "entity_links": [],
            "recall_count": 0,
            "last_recalled_at": None,
        }
    }


def _rel(evidence_memory_id, name="peer-entity"):
    return {
        "id": str(uuid.uuid4()),
        "relation_type": "works_with",
        "to_entity_id": str(uuid.uuid4()),
        "to_entity_name": name,
        "weight": 0.7,
        "evidence_memory_id": evidence_memory_id,
    }


@pytest.fixture
def fake_storage(monkeypatch):
    """Install a fake storage client; returns it for per-test configuration."""

    def _set(linked, relations, bulk_rows=None):
        sc = MagicMock()
        sc.get_entity_with_linked_memories = AsyncMock(
            return_value={
                "entity": {
                    "id": str(uuid.uuid4()),
                    "tenant_id": "t",
                    "fleet_id": "fleet-alpha",
                    "entity_type": "person",
                    "canonical_name": "X",
                    "attributes": {},
                    # C23: with-memories NEVER carried relations; get_entity now
                    # fetches them from the relations endpoint below. Kept absent
                    # here to mirror the real payload.
                },
                "linked_memories": linked,
            }
        )
        # C23 — relations arrive as {"relation": ..., "target": ...} rows from
        # GET /entities/{id}/relations; adapt the flat test dicts.
        sc.get_outgoing_relations = AsyncMock(
            return_value=[
                {
                    "relation": {
                        "id": r["id"],
                        "relation_type": r["relation_type"],
                        "to_entity_id": r["to_entity_id"],
                        "weight": r["weight"],
                        "evidence_memory_id": r["evidence_memory_id"],
                    },
                    "target": {"canonical_name": r["to_entity_name"]},
                }
                for r in relations
            ]
        )
        sc.bulk_get_memories = AsyncMock(return_value=bulk_rows or [])
        from core_api.services import entity_service

        monkeypatch.setattr(entity_service, "get_storage_client", lambda: sc)
        return sc

    return _set


@pytest.fixture
def patch_lookup(monkeypatch):
    """Fake lookup_agent returning a controlled agent row; counts calls."""

    def _set(*, fleet_id=None, trust_level=0, exists=True):
        from core_api.services import agent_service

        calls = {"n": 0}

        async def fake(tenant_id, agent_id):
            calls["n"] += 1
            if not exists:
                return None
            return {
                "agent_id": agent_id,
                "fleet_id": fleet_id,
                "trust_level": trust_level,
            }

        monkeypatch.setattr(agent_service, "lookup_agent", fake)
        return calls

    return _set


async def _get(caller_agent_id):
    from core_api.services.entity_service import get_entity

    return await get_entity(uuid.uuid4(), "t", caller_agent_id=caller_agent_id)


async def test_relations_filtered_for_agent_credential(fake_storage, patch_lookup):
    """An agent sees only relations whose evidence memory it may read."""
    own_id = str(uuid.uuid4())
    peer_secret_id = str(uuid.uuid4())
    foreign_id = str(uuid.uuid4())

    linked = [
        _mem(own_id, "caller", "scope_team", "fleet-alpha"),
        _mem(peer_secret_id, "peer", "scope_agent", "fleet-alpha"),
    ]
    relations = [
        _rel(own_id, name="visible-via-own-evidence"),
        _rel(peer_secret_id, name="hidden-peer-secret"),
        _rel(None, name="no-evidence-kept"),
        _rel(foreign_id, name="hidden-foreign"),
    ]
    # peer_secret_id is linked but unauthorized → re-fetched in bulk along
    # with foreign_id; foreign returns None (deleted / cross-tenant).
    bulk_rows_by_id = {
        peer_secret_id: {
            "id": peer_secret_id,
            "visibility": "scope_agent",
            "agent_id": "peer",
            "fleet_id": "fleet-alpha",
        },
        foreign_id: None,
    }
    sc = fake_storage(linked, relations)

    async def bulk(ids, tenant_id=None):
        return [bulk_rows_by_id.get(i) for i in ids]

    sc.bulk_get_memories = AsyncMock(side_effect=bulk)
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)

    out = await _get("caller")
    names = {r.to_entity_name for r in out.relations}
    assert names == {"visible-via-own-evidence", "no-evidence-kept"}
    # Linked memories keep the existing scope filter.
    assert [m.id for m in out.linked_memories] == [uuid.UUID(own_id)]


async def test_relations_unfiltered_for_tenant_credential(fake_storage):
    """Tenant/user credentials (caller_agent_id None) keep full visibility."""
    mem_id = str(uuid.uuid4())
    sc = fake_storage(
        [_mem(mem_id, "peer", "scope_agent", "fleet-alpha")],
        [_rel(mem_id), _rel(None)],
    )

    out = await _get(None)
    assert len(out.relations) == 2
    assert len(out.linked_memories) == 1
    sc.bulk_get_memories.assert_not_awaited()


async def test_agent_row_resolved_once(fake_storage, patch_lookup):
    """N scope_team memories + relations must cost exactly ONE agent lookup."""
    ids = [str(uuid.uuid4()) for _ in range(5)]
    linked = [
        _mem(i, f"author-{n}", "scope_team", "fleet-alpha") for n, i in enumerate(ids)
    ]
    relations = [_rel(i) for i in ids]
    fake_storage(linked, relations)
    calls = patch_lookup(fleet_id="fleet-alpha", trust_level=1)

    out = await _get("caller")
    assert calls["n"] == 1
    assert len(out.linked_memories) == 5
    assert len(out.relations) == 5


# ---------------------------------------------------------------------------
# H-03 — the same filter, on the /graph surface
#
# S5 fixed ``get_entity``. ``GET /graph`` read the same edges from a different
# storage call and had no filter at all: ``enforce_readable_tenant`` and then
# every relation for the tenant, ``evidence_memory_id`` verbatim. The filter now
# lives in one shared function both readers call, because a guarantee that
# lives in one caller is not a guarantee — which is what H-03 demonstrated.
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_storage(monkeypatch):
    """Fake storage for the shared filter: only ``bulk_get_memories`` matters."""

    def _set(bulk_rows):
        sc = MagicMock()
        sc.bulk_get_memories = AsyncMock(return_value=bulk_rows)
        from core_api.services import entity_service

        monkeypatch.setattr(entity_service, "get_storage_client", lambda: sc)
        return sc

    return _set


async def _filter(relations, caller_agent_id, **kwargs):
    from core_api.services.entity_service import (
        filter_relations_by_evidence_visibility,
    )

    return await filter_relations_by_evidence_visibility(
        relations, tenant_id="t", caller_agent_id=caller_agent_id, **kwargs
    )


async def test_graph_drops_an_edge_whose_evidence_is_a_peers_private_memory(
    graph_storage, patch_lookup
):
    """THE ATTACK from H-03, at the filter both surfaces now share.

    Agent A writes a ``scope_agent`` memory; extraction persists an edge whose
    evidence is that memory. Peer agent B must not read the derived triple or
    the private memory's id. The raw text was never exposed — the triple is
    the leak: "(anna) -negotiates-> (zenith acquisition)" carries the secret
    without carrying the sentence.
    """
    secret_id = str(uuid.uuid4())
    graph_storage(
        [
            {
                "id": secret_id,
                "visibility": "scope_agent",
                "agent_id": "agent-a",
                "fleet_id": "fleet-alpha",
            }
        ]
    )
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)

    out = await _filter([_rel(secret_id, name="zenith acquisition")], "agent-b")
    assert out == []


async def test_graph_keeps_the_owners_own_private_edge(graph_storage, patch_lookup):
    """OVER-REFUSAL GUARD. The author of the evidence still sees their edge."""
    secret_id = str(uuid.uuid4())
    graph_storage(
        [
            {
                "id": secret_id,
                "visibility": "scope_agent",
                "agent_id": "agent-a",
                "fleet_id": "fleet-alpha",
            }
        ]
    )
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)

    out = await _filter([_rel(secret_id)], "agent-a")
    assert len(out) == 1


async def test_graph_drops_a_cross_fleet_edge_below_the_trust_bar(
    graph_storage, patch_lookup
):
    """``fleet_id`` is optional on /graph, so omitting it asked for every fleet.

    Cross-fleet evidence needs trust >= 2, the same bar ``enforce_fleet_read``
    applies. A trust-1 agent in another fleet must not get the edge.
    """
    other_id = str(uuid.uuid4())
    graph_storage(
        [
            {
                "id": other_id,
                "visibility": "scope_team",
                "agent_id": "agent-x",
                "fleet_id": "fleet-beta",
            }
        ]
    )
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)

    out = await _filter([_rel(other_id)], "agent-b")
    assert out == []


async def test_graph_keeps_a_cross_fleet_edge_at_or_above_the_trust_bar(
    graph_storage, patch_lookup
):
    """OVER-REFUSAL GUARD. trust >= 2 is entitled to cross-fleet reads."""
    other_id = str(uuid.uuid4())
    graph_storage(
        [
            {
                "id": other_id,
                "visibility": "scope_team",
                "agent_id": "agent-x",
                "fleet_id": "fleet-beta",
            }
        ]
    )
    patch_lookup(fleet_id="fleet-alpha", trust_level=2)

    out = await _filter([_rel(other_id)], "agent-b")
    assert len(out) == 1


async def test_graph_drops_an_edge_with_unresolvable_evidence(
    graph_storage, patch_lookup
):
    """Deleted / nonexistent / cross-tenant evidence fails CLOSED.

    An id that does not resolve is the one case where we cannot establish
    that the caller may read it, so the edge and the id are both withheld.
    """
    graph_storage([None])
    patch_lookup(fleet_id="fleet-alpha", trust_level=3)

    out = await _filter([_rel(str(uuid.uuid4()))], "agent-b")
    assert out == []


async def test_graph_keeps_an_edge_with_no_evidence(graph_storage, patch_lookup):
    """OVER-REFUSAL GUARD. No evidence ⇒ no memory-derived content, no id."""
    sc = graph_storage([])
    patch_lookup(fleet_id="fleet-alpha", trust_level=0)

    out = await _filter([_rel(None)], "agent-b")
    assert len(out) == 1
    # Nothing to look up, so no round-trip at all.
    sc.bulk_get_memories.assert_not_awaited()


async def test_graph_keeps_scope_org_evidence(graph_storage, patch_lookup):
    """OVER-REFUSAL GUARD. scope_org is readable tenant-wide by contract."""
    org_id = str(uuid.uuid4())
    graph_storage(
        [
            {
                "id": org_id,
                "visibility": "scope_org",
                "agent_id": "agent-x",
                "fleet_id": "fleet-beta",
            }
        ]
    )
    patch_lookup(fleet_id="fleet-alpha", trust_level=0)

    out = await _filter([_rel(org_id)], "agent-b")
    assert len(out) == 1


async def test_graph_is_unfiltered_for_a_tenant_credential(graph_storage):
    """OVER-REFUSAL GUARD, and the deliberate limit of this fix.

    A tenant / user / admin credential carries no ``agent_id`` and sees the
    whole graph, matching the linked-memory filter's contract in the same
    module. Narrowing /graph for dashboards was the finding's other suggested
    remedy and would have broken them; this keeps the surface intact and
    filters only the credential class the contract is about.
    """
    sc = graph_storage([])
    out = await _filter([_rel(str(uuid.uuid4())), _rel(str(uuid.uuid4()))], None)
    assert len(out) == 2
    sc.bulk_get_memories.assert_not_awaited()


async def test_graph_filter_resolves_the_agent_row_once(graph_storage, patch_lookup):
    """One lookup for the whole graph, not one per edge.

    /graph returns every relation in the tenant, so a per-edge lookup would be
    N+1 across the entire graph rather than across one entity's edges.
    """
    ids = [str(uuid.uuid4()) for _ in range(6)]
    graph_storage(
        [
            {
                "id": i,
                "visibility": "scope_team",
                "agent_id": "agent-x",
                "fleet_id": "fleet-alpha",
            }
            for i in ids
        ]
    )
    calls = patch_lookup(fleet_id="fleet-alpha", trust_level=1)

    out = await _filter([_rel(i) for i in ids], "agent-b")
    assert len(out) == 6
    assert calls["n"] == 1


async def test_the_graph_route_itself_withholds_the_private_edge(
    monkeypatch, patch_lookup
):
    """END-TO-END through the route, which is what H-03 was actually about.

    The filter tests above exercise the shared function. This one proves the
    ROUTE is wired to it: a peer's ``scope_agent`` evidence must not appear in
    the response body, neither as an edge nor as an ``evidence_memory_id``.
    Without this, every test above would still pass with the route unchanged —
    which is precisely the state H-03 described.
    """
    import json

    from core_api.auth import AuthContext
    from core_api.routes import entities as entities_routes

    secret_id = str(uuid.uuid4())
    visible_id = str(uuid.uuid4())
    sc = MagicMock(name="storage_client")
    sc.get_full_graph = AsyncMock(
        return_value={
            "entities": [
                {"id": "e1", "canonical_name": "anna", "entity_type": "person"}
            ],
            "relations": [
                {
                    "id": "r-secret",
                    "from_entity_id": "e1",
                    "to_entity_id": "e2",
                    "relation_type": "negotiates",
                    "weight": 0.9,
                    "evidence_memory_id": secret_id,
                },
                {
                    "id": "r-open",
                    "from_entity_id": "e1",
                    "to_entity_id": "e3",
                    "relation_type": "works_with",
                    "weight": 0.5,
                    "evidence_memory_id": visible_id,
                },
            ],
        }
    )
    sc.count_memories_per_entity = AsyncMock(return_value={})
    sc.bulk_get_memories = AsyncMock(
        return_value=[
            {
                "id": secret_id,
                "visibility": "scope_agent",
                "agent_id": "agent-a",
                "fleet_id": "fleet-alpha",
            },
            {
                "id": visible_id,
                "visibility": "scope_org",
                "agent_id": "agent-a",
                "fleet_id": "fleet-alpha",
            },
        ]
    )
    monkeypatch.setattr(entities_routes, "get_storage_client", lambda: sc)
    from core_api.services import entity_service

    monkeypatch.setattr(entity_service, "get_storage_client", lambda: sc)
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)

    resp = await entities_routes.get_graph(
        tenant_id="t",
        fleet_id=None,
        auth=AuthContext(tenant_id="t", agent_id="agent-b", readable_tenant_ids=["t"]),
    )
    payload = json.loads(resp.body)
    edge_ids = {e["id"] for e in payload["edges"]}
    assert edge_ids == {"r-open"}, f"private edge leaked: {payload['edges']}"
    # The id itself must not appear anywhere in the serialised response.
    assert secret_id not in resp.body.decode()
