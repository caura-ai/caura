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
                    "relations": relations,
                },
                "linked_memories": linked,
            }
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
            return {"agent_id": agent_id, "fleet_id": fleet_id, "trust_level": trust_level}

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
    linked = [_mem(i, f"author-{n}", "scope_team", "fleet-alpha") for n, i in enumerate(ids)]
    relations = [_rel(i) for i in ids]
    fake_storage(linked, relations)
    calls = patch_lookup(fleet_id="fleet-alpha", trust_level=1)

    out = await _get("caller")
    assert calls["n"] == 1
    assert len(out.linked_memories) == 5
    assert len(out.relations) == 5
