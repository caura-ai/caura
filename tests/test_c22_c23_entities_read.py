"""C22 + C23 — the entities read path actually works.

C22: ``GET /entities`` declared ``search`` and ``entity_type`` since day one
but never forwarded them — core-api called the storage client without them
and the storage router didn't accept them, so ``?search=foo`` silently
returned the unfiltered list (the storage service supported both all along).

C23: ``get_entity`` read ``entity.get("relations", [])`` off the
with-memories payload, which has never carried a relations key — relations
were structurally ``[]`` on REST and MCP since v1.0.0 while /graph showed the
edges. They now come from the (previously unused) relations endpoint, and the
S5/C14 scope filter finally operates on real rows
(see test_entity_relations_scope.py for the scope behavior itself).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# --- C22: filters forwarded through every hop ---------------------------------


async def test_storage_client_sends_filters():
    from core_api.clients.storage_client import CoreStorageClient

    sc = CoreStorageClient()
    seen = {}

    async def fake_get_list(path, **params):
        seen["path"] = path
        seen["params"] = params
        return []

    sc._get_list = fake_get_list  # type: ignore[method-assign]
    await sc.list_entities("t1", fleet_id="f1", entity_type="person", search="astra", limit=7)
    assert seen["path"] == "/entities"
    assert seen["params"] == {
        "tenant_id": "t1",
        "fleet_id": "f1",
        "entity_type": "person",
        "search": "astra",
        "limit": 7,
        "offset": 0,
    }


async def test_storage_client_omits_absent_filters():
    from core_api.clients.storage_client import CoreStorageClient

    sc = CoreStorageClient()
    seen = {}

    async def fake_get_list(path, **params):
        seen["params"] = params
        return []

    sc._get_list = fake_get_list  # type: ignore[method-assign]
    await sc.list_entities("t1")
    assert "search" not in seen["params"] and "entity_type" not in seen["params"]


def test_route_forwards_filters_to_client():
    """Grep-guard: the route must pass search/entity_type into list_entities —
    the exact one-hop drop that made entity search non-functional (N2)."""
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "core-api/src/core_api/routes/entities.py"
    ).read_text()
    call = src.split("await sc.list_entities(")[1].split(")")[0]
    assert "entity_type=entity_type" in call
    assert "search=search" in call


def test_storage_router_accepts_and_forwards_filters():
    import inspect

    from core_storage_api.routers import entities as storage_entities

    sig = inspect.signature(storage_entities.list_entities)
    assert "search" in sig.parameters and "entity_type" in sig.parameters
    src = inspect.getsource(storage_entities.list_entities)
    assert "entity_type=entity_type" in src and "search=search" in src


# --- C23: relations come from the relations endpoint ---------------------------


def _entity_payload():
    return {
        "entity": {
            "id": str(uuid.uuid4()),
            "tenant_id": "t",
            "fleet_id": None,
            "entity_type": "system",
            "canonical_name": "Astra SSO",
            "attributes": {},
        },
        "linked_memories": [],
    }


async def test_get_entity_returns_relations_from_endpoint(monkeypatch):
    from core_api.services import entity_service

    to_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    sc = MagicMock()
    sc.get_entity_with_linked_memories = AsyncMock(return_value=_entity_payload())
    sc.get_outgoing_relations = AsyncMock(
        return_value=[
            {
                "relation": {
                    "id": rel_id,
                    "relation_type": "authenticates",
                    "to_entity_id": to_id,
                    "weight": 0.9,
                    "evidence_memory_id": None,
                },
                "target": {"canonical_name": "Webhook Service"},
            }
        ]
    )
    monkeypatch.setattr(entity_service, "get_storage_client", lambda: sc)

    out = await entity_service.get_entity(uuid.uuid4(), "t", caller_agent_id=None)
    assert len(out.relations) == 1
    r = out.relations[0]
    assert r.relation_type == "authenticates"
    assert str(r.to_entity_id) == to_id
    assert r.to_entity_name == "Webhook Service"
    assert r.weight == 0.9
    # tenant scoping is passed through to the storage endpoint
    sc.get_outgoing_relations.assert_awaited_once()
    _, kwargs = sc.get_outgoing_relations.await_args
    assert kwargs.get("tenant_id") == "t"


async def test_get_entity_degrades_to_empty_relations_on_endpoint_failure(monkeypatch):
    """Relations-endpoint failure must not fail the whole entity read —
    degrade to the pre-C23 behavior ([]) instead."""
    from core_api.services import entity_service

    sc = MagicMock()
    sc.get_entity_with_linked_memories = AsyncMock(return_value=_entity_payload())
    sc.get_outgoing_relations = AsyncMock(side_effect=RuntimeError("storage down"))
    monkeypatch.setattr(entity_service, "get_storage_client", lambda: sc)

    out = await entity_service.get_entity(uuid.uuid4(), "t", caller_agent_id=None)
    assert out is not None
    assert out.relations == []


async def test_get_entity_ignores_legacy_entity_relations_key(monkeypatch):
    """A with-memories payload that (hypothetically) carried a relations key
    must not double-count: the endpoint is the single source of truth."""
    from core_api.services import entity_service

    payload = _entity_payload()
    payload["entity"]["relations"] = [{"id": "ghost", "relation_type": "ghost"}]
    sc = MagicMock()
    sc.get_entity_with_linked_memories = AsyncMock(return_value=payload)
    sc.get_outgoing_relations = AsyncMock(return_value=[])
    monkeypatch.setattr(entity_service, "get_storage_client", lambda: sc)

    out = await entity_service.get_entity(uuid.uuid4(), "t", caller_agent_id=None)
    assert out.relations == []
