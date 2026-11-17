"""A trust-1 agent with no home fleet reads by search what it may read by id.

The fleet pin on ``caura_recall``, ``POST /search`` and ``POST /recall`` scopes
a trust < 2 agent that names no fleet to its own fleet. An agent with no home
fleet has nothing to pin to, so its search reached storage unfiltered and came
back with other fleets' ``scope_team`` rows, while a by-id read of the same
memory answered NOT_FOUND (``authorize_memory_access``: another fleet needs
trust 2). Agents connected by MCP OAuth start without a fleet, so this was the
default case for them.

The search hits now go through the by-id rule: fleet-less rows (tenant-shared
by contract D4) and ``scope_org`` rows stay, other fleets' rows go.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from core_api.services.agent_service import bound_to_no_fleet, readable_without_a_fleet
from tests._mcp_test_helpers import parse_envelope

# ---------------------------------------------------------------------------
# The two helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent", "fleet_ids", "bound"),
    [
        ({"fleet_id": None, "trust_level": 1}, None, True),
        ({"fleet_id": None, "trust_level": 1}, [], True),
        ({"trust_level": 1}, None, True),
        ({"fleet_id": None}, None, True),  # default trust is below 2
        ({"fleet_id": None, "trust_level": 2}, None, False),
        ({"fleet_id": None, "trust_level": 3}, None, False),
        ({"fleet_id": "own", "trust_level": 1}, None, False),  # pinned instead
        ({"fleet_id": None, "trust_level": 1}, ["named"], False),  # ladder gates it
        (None, None, False),  # unregistered keeps its soft-pass, as by id
    ],
)
def test_bound_to_no_fleet(agent, fleet_ids, bound) -> None:
    assert bound_to_no_fleet(agent, fleet_ids) is bound


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fleet_id", "visibility", "readable"),
    [
        (None, "scope_team", True),
        (None, "scope_agent", True),  # storage already limits these to the author
        ("other", "scope_team", False),
        ("other", "scope_agent", False),
        ("other", "scope_org", True),
    ],
)
def test_readable_without_a_fleet(fleet_id, visibility, readable) -> None:
    hit = SimpleNamespace(fleet_id=fleet_id, visibility=visibility)
    assert readable_without_a_fleet(hit) is readable


# ---------------------------------------------------------------------------
# caura_recall
# ---------------------------------------------------------------------------


class _Hit:
    def __init__(self, mid: str, fleet_id: str | None, visibility: str = "scope_team"):
        self.mid, self.fleet_id, self.visibility = mid, fleet_id, visibility

    def model_dump(self, mode: str = "python"):
        return {
            "id": self.mid,
            "fleet_id": self.fleet_id,
            "visibility": self.visibility,
        }


async def _fake_resolve_config(_tenant_id):
    return SimpleNamespace(
        recall_boost=False, graph_expand=False, entity_retrieval=False
    )


async def _recall(mcp_env, monkeypatch, agent: dict, **kwargs) -> set[str]:
    search = mcp_env["service"]("search_memories")
    search.return_value = [
        _Hit("shared", None),
        _Hit("other-team", "fleet-x"),
        _Hit("other-org", "fleet-x", "scope_org"),
    ]
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    monkeypatch.setattr(mcp_server, "lookup_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(
        mcp_server, "enforce_fleet_read_many", AsyncMock(return_value=None)
    )
    payload = parse_envelope(await mcp_server.caura_recall(query="merger", **kwargs))
    return {row["id"] for row in payload["results"]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_drops_other_fleets_rows_for_a_fleetless_trust_1_agent(
    mcp_env, monkeypatch
):
    ids = await _recall(mcp_env, monkeypatch, {"fleet_id": None, "trust_level": 1})
    assert ids == {"shared", "other-org"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_keeps_every_row_for_a_fleetless_trust_2_agent(
    mcp_env, monkeypatch
):
    ids = await _recall(mcp_env, monkeypatch, {"fleet_id": None, "trust_level": 2})
    assert ids == {"shared", "other-team", "other-org"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_leaves_a_named_fleet_to_the_ladder(mcp_env, monkeypatch):
    """A fleet the agent names goes through ``enforce_fleet_read_many``; what
    that admits is not filtered again here."""
    ids = await _recall(
        mcp_env,
        monkeypatch,
        {"fleet_id": None, "trust_level": 1},
        fleet_ids=["fleet-x"],
    )
    assert ids == {"shared", "other-team", "other-org"}


# ---------------------------------------------------------------------------
# POST /search and POST /recall, against storage
# ---------------------------------------------------------------------------


@pytest.fixture
def as_agent(monkeypatch):
    """An agent-scoped credential: the admin key skips the fleet block."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id, agent_id=agent_id, readable_tenant_ids=[tenant_id]
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _seed(client, sc, content: str, fleet_id: str | None) -> None:
    from tests.conftest import get_test_auth
    from tests.conftest import uid as _uid

    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": content,
            "agent_id": f"writer-{_uid()}",
            "fleet_id": fleet_id,
            "visibility": "scope_team",
            "memory_type": "fact",
            "write_mode": "strong",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


async def _search_as(client, sc, as_agent, *, trust: int, query: str) -> str:
    from tests.conftest import get_test_auth
    from tests.conftest import uid as _uid

    tenant_id, _ = get_test_auth()
    reader = f"fleetless-reader-{_uid()}"
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": reader,
            "fleet_id": None,
            "trust_level": trust,
        }
    )
    as_agent(tenant_id, reader)
    resp = await client.post(
        "/api/v1/search", json={"tenant_id": tenant_id, "query": query}
    )
    assert resp.status_code == 200, resp.text
    return resp.text


@pytest.mark.integration
async def test_rest_search_hides_another_fleets_row_from_a_fleetless_trust_1_agent(
    client, sc, as_agent
):
    """The trust-2 search is the control: the row is findable by this query,
    so its absence from the trust-1 answer is the filter, not a miss."""
    from tests.conftest import uid as _uid

    secret = f"fleetless {uuid.uuid4().hex} fleet-x team secret"
    await _seed(client, sc, secret, f"fleet-x-{_uid()}")
    assert secret in await _search_as(client, sc, as_agent, trust=2, query=secret)
    assert secret not in await _search_as(client, sc, as_agent, trust=1, query=secret)


@pytest.mark.integration
async def test_rest_search_keeps_a_fleetless_row_for_a_fleetless_trust_1_agent(
    client, sc, as_agent
):
    shared = f"fleetless {uuid.uuid4().hex} tenant shared note"
    await _seed(client, sc, shared, None)
    assert shared in await _search_as(client, sc, as_agent, trust=1, query=shared)
