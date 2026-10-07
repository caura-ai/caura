"""A signed-in person sees every memory scope; agents and machine keys don't.

The Prism decision record (§3, signed off 2026-10-06): scopes isolate agents
from each other, not from the people who govern the tenant. ``GET
/memories/{id}`` already opens a ``scope_agent`` row for a caller with no agent
identity, but ``GET /memories`` hid every such row from a dashboard session.

Pinned here, the core-api half:

- ``AuthContext.is_person`` is set on Path 4 only, when the gateway stamped
  ``X-Org-Role`` and named no agent. The standalone paths give every caller
  ``org_role="admin"``, agents included, so they never set it.
- For a person, the list asks storage for every scope with no visibility
  identity, even when ``agent_id`` names an agent; and stats, count and the
  fleet counts drop the ``scope_agent`` filter with it, so no number disagrees
  with the list.
- Every other caller sends exactly what it sent before.

The storage half is ``core-storage-api/tests/test_list_include_scope_agent.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core_api import auth as auth_mod
from core_api.auth import AuthContext
from core_api.routes import memories as memories_routes
from tests._legacy_contracts import LEGACY_API_KEY_FIELD

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_CLIENT = "core_api.clients.storage_client.CoreStorageClient"


class _Req:
    def __init__(self, headers):
        self.headers = headers


async def _resolve(monkeypatch, headers, *, standalone=False, api_key=None, key=None):
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", None)
    monkeypatch.setattr(auth_mod.settings, "is_standalone", standalone)
    monkeypatch.setattr(auth_mod.settings, LEGACY_API_KEY_FIELD, api_key)
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)
    if standalone:
        monkeypatch.setattr("core_api.standalone.get_standalone_tenant_id", lambda: "t")
    return await auth_mod.get_auth_context(_Req(headers), key=key)


# ---------------------------------------------------------------------------
# Who is a person
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "member"])
async def test_gateway_session_is_a_person(monkeypatch, role):
    ctx = await _resolve(monkeypatch, {"x-tenant-id": "t", "x-org-role": role})
    assert ctx.is_person


async def test_a_request_naming_an_agent_is_not_a_person(monkeypatch):
    ctx = await _resolve(
        monkeypatch, {"x-tenant-id": "t", "x-org-role": "admin", "x-agent-id": "a1"}
    )
    assert not ctx.is_person


@pytest.mark.parametrize("role", [None, "owner"])
async def test_no_valid_org_role_is_not_a_person(monkeypatch, role):
    headers = {"x-tenant-id": "t"}
    if role:
        headers["x-org-role"] = role
    ctx = await _resolve(monkeypatch, headers)
    assert not ctx.is_person


async def test_standalone_callers_are_not_people(monkeypatch):
    """Standalone gives every caller org_role='admin', so it can't tell."""
    ctx = await _resolve(monkeypatch, {"x-org-role": "admin"}, standalone=True)
    assert ctx.org_role == "admin"
    assert not ctx.is_person
    ctx = await _resolve(
        monkeypatch, {"x-org-role": "admin"}, standalone=True, api_key="k", key="k"
    )
    assert ctx.org_role == "admin"
    assert not ctx.is_person


async def test_the_shared_api_key_path_is_not_a_person(monkeypatch):
    ctx = await _resolve(
        monkeypatch, {"x-tenant-id": "t", "x-org-role": "admin"}, api_key="k", key="k"
    )
    assert not ctx.is_person


# ---------------------------------------------------------------------------
# What each read sends storage
# ---------------------------------------------------------------------------

_PERSON = AuthContext(tenant_id="t", org_role="member", is_person=True)
_TENANT_KEY = AuthContext(tenant_id="t")
_AGENT = AuthContext(tenant_id="t", agent_id="a1")


@pytest.fixture
def storage(monkeypatch):
    listed = AsyncMock(return_value=[])
    stats = AsyncMock(return_value={})
    count = AsyncMock(return_value=0)
    fleets = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{_CLIENT}.list_memories_by_filters", listed)
    monkeypatch.setattr(f"{_CLIENT}.memory_stats_breakdown", stats)
    monkeypatch.setattr(f"{_CLIENT}.count_active", count)
    monkeypatch.setattr(f"{_CLIENT}.memory_fleet_distribution", fleets)
    monkeypatch.setattr(memories_routes, "_gate_fleet_read", AsyncMock())
    monkeypatch.setattr(memories_routes, "log_cross_tenant_read", AsyncMock())
    return {"list": listed, "stats": stats, "count": count, "fleets": fleets}


async def _list(auth, agent_id=None, written_by=None):
    # Every parameter explicit: calling the endpoint function directly skips
    # FastAPI's dependency resolution (see test_scope_agent_home_tenant).
    await memories_routes.list_memories(
        tenant_id=None,
        fleet_id=None,
        agent_id=agent_id,
        scope=None,
        written_by=written_by,
        weight_min=None,
        weight_max=None,
        memory_type=None,
        exclude_memory_types=None,
        created_after=None,
        created_before=None,
        status=None,
        visibility=None,
        run_id=None,
        cursor=None,
        sort="created_at",
        order="desc",
        offset=0,
        limit=25,
        include_deleted=False,
        auth=auth,
    )


async def _stats(auth, agent_id=None):
    await memories_routes.memory_stats(
        tenant_id=None,
        fleet_id=None,
        agent_id=agent_id,
        scope=None,
        memory_type=None,
        status=None,
        include_deleted=False,
        auth=auth,
    )


@pytest.mark.parametrize("agent_id", [None, "a2"])
async def test_a_persons_list_asks_for_every_scope(storage, agent_id):
    await _list(_PERSON, agent_id=agent_id, written_by="a3" if agent_id else None)
    payload = storage["list"].await_args.args[-1]
    assert payload["include_scope_agent"] is True
    assert payload["caller_agent_id"] is None
    # agent_id stays the author filter, unless written_by names another.
    assert payload["written_by"] == ("a3" if agent_id else None)


@pytest.mark.parametrize(
    ("auth", "agent_id", "identity"),
    [
        (_TENANT_KEY, None, None),
        (_TENANT_KEY, "a2", "a2"),
        (_AGENT, None, "a1"),
        (_AGENT, "a2", "a1"),
    ],
)
async def test_everyone_elses_list_is_scoped_as_before(
    storage, auth, agent_id, identity
):
    await _list(auth, agent_id=agent_id)
    payload = storage["list"].await_args.args[-1]
    assert payload["include_scope_agent"] is False
    assert payload["caller_agent_id"] == identity


async def test_a_persons_stats_count_every_scope(storage):
    await _stats(_PERSON)
    payload = storage["stats"].await_args.args[-1]
    assert payload["include_scope_agent"] is True
    assert payload["caller_agent_id"] is None


@pytest.mark.parametrize(("auth", "identity"), [(_TENANT_KEY, None), (_AGENT, "a1")])
async def test_everyone_elses_stats_are_scoped_as_before(storage, auth, identity):
    await _stats(auth)
    payload = storage["stats"].await_args.args[-1]
    assert payload["include_scope_agent"] is False
    assert payload["caller_agent_id"] == identity


@pytest.mark.parametrize(
    ("auth", "exclude"), [(_PERSON, False), (_TENANT_KEY, True), (_AGENT, True)]
)
async def test_count_and_fleet_counts_follow_the_list(storage, auth, exclude):
    await memories_routes.memory_count(
        tenant_id=None, fleet_id=None, status=None, auth=auth
    )
    assert storage["count"].await_args.kwargs["exclude_scope_agent"] is exclude
    await memories_routes.list_fleets(tenant_id=None, auth=auth)
    assert storage["fleets"].await_args.kwargs["exclude_scope_agent"] is exclude
