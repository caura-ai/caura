"""``GET /entities`` and ``GET /graph`` send an agent reader scope to storage.

Storage narrows entity names and ``memory_count`` to the memories the reader
may open (core-storage-api/tests/test_entity_reader_scope.py). These tests pin
the core-api half: the scope is resolved from the authenticated agent — its
home tenant and the cross-fleet trust ladder of
``memory_access_allowed_for_agent`` — and tenant credentials send none.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _patch_agent(monkeypatch, agent):
    from core_api.services import agent_service

    monkeypatch.setattr(agent_service, "lookup_agent", AsyncMock(return_value=agent))


@pytest.mark.parametrize(
    ("agent", "fleets"),
    [
        ({"fleet_id": "fx", "trust_level": 1}, ["fx"]),
        ({"fleet_id": None, "trust_level": 0}, []),
        ({"fleet_id": "fx", "trust_level": 2}, None),
        (None, None),  # unregistered: allow-on-unknown, as the by-id helper does
    ],
)
async def test_reader_scope_mirrors_the_trust_ladder(monkeypatch, agent, fleets):
    from core_api.services.entity_service import entity_reader_scope

    _patch_agent(monkeypatch, agent)
    scope = await entity_reader_scope("t", "a1", "home")
    assert scope == {
        "caller_agent_id": "a1",
        "caller_tenant_id": "home",
        "caller_fleet_ids": fleets,
    }


async def test_tenant_credential_has_no_reader_scope():
    from core_api.services.entity_service import entity_reader_scope

    assert await entity_reader_scope("t", None, "t") is None


async def test_query_string_encodes_a_reader_bound_to_no_fleet():
    from core_api.clients.storage_client import _entity_reader_params

    params = _entity_reader_params(
        {"caller_agent_id": "a1", "caller_tenant_id": "t", "caller_fleet_ids": []}
    )
    assert params == {
        "caller_agent_id": "a1",
        "caller_tenant_id": "t",
        "caller_fleet_bound": "true",
    }
    assert _entity_reader_params(None) == {}


@pytest.mark.parametrize("agent_id", ["a1", None])
async def test_list_and_graph_forward_the_reader_scope(monkeypatch, agent_id):
    from core_api.auth import AuthContext
    from core_api.routes import entities as routes

    _patch_agent(monkeypatch, {"fleet_id": "fx", "trust_level": 1})
    sc = MagicMock()
    sc.list_entities = AsyncMock(return_value=[{"id": "e1"}])
    sc.get_full_graph = AsyncMock(
        return_value={"entities": [{"id": "e1"}], "relations": []}
    )
    sc.count_memories_per_entity = AsyncMock(return_value={})
    monkeypatch.setattr(routes, "get_storage_client", lambda: sc)
    auth = AuthContext(tenant_id="t", agent_id=agent_id)

    await routes.list_entities(
        tenant_id="t", fleet_id=None, entity_type=None, search=None, limit=10, auth=auth
    )
    await routes.get_graph(tenant_id="t", fleet_id=None, auth=auth)

    expected = (
        {"caller_agent_id": "a1", "caller_tenant_id": "t", "caller_fleet_ids": ["fx"]}
        if agent_id
        else None
    )
    assert sc.list_entities.await_args.kwargs["reader"] == expected
    assert sc.get_full_graph.await_args.kwargs["reader"] == expected
    for call in sc.count_memories_per_entity.await_args_list:
        assert call.kwargs["reader"] == expected
