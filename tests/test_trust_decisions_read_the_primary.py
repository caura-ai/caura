"""Reads that decide what a caller may do come from the primary, not a replica.

A demotion (``PATCH /agents/{id}/trust``), an org suppression, a keystone set or
deleted: each lands on the primary. A gate that reads the replica keeps
honouring the previous answer until replication catches up, so a demoted agent
keeps its old reach, a suppressed tenant keeps working and a revoked keystone
keeps being served, for that window. These pin the reads to the primary:

- ``require_trust``, the gate behind ``caura_list`` / ``caura_stats``, the scoped
  REST list and stats, insights, evolve, conflict review and keystone writes;
- the edit, delete and crystallization gates (``enforce_update``,
  ``enforce_delete``, ``enforce_crystallize``);
- the gates that refuse a reader at trust 0, the level that disables an agent:
  the list, stats, count and field routes, the by-id read and its linked rows,
  and ``caura_recall``;
- storage's tenant-suppression check and keystone list.

REST ``/search`` and ``/recall`` take the caller's row from
``get_or_create_agent``, which keeps a found row on the replica on purpose: it
runs on every search and every write, so a demotion reaches them after the
replica's lag.

Storage's by-id memory read, which a single-service deployment (role ``hybrid``)
with ``READ_DATABASE_URL`` served from the replica even when core-api had asked
for the writer, is pinned in ``tests/test_contradiction_fresh_read.py``.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api.agent_ids import AgentIdentity
from core_api.services import agent_service
from core_api.services.trust_service import require_trust
from core_storage_api.services import keystones
from core_storage_api.services import postgres_service as ps

# Not marked ``unit``: the suite stubs the suppression check out of every unit
# test (tests/conftest.py), and this file reads the real one. No test here
# reaches the database; each replaces the sessions or the storage client.


def _agents(monkeypatch, trust: int = 3) -> AsyncMock:
    """Serve one agent row; return the storage ``get_agent`` to inspect."""
    get_agent = AsyncMock(
        return_value={"agent_id": "a", "fleet_id": "f", "trust_level": trust}
    )
    sc = MagicMock(name="storage_client", get_agent=get_agent)
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: sc)
    return get_agent


def _reads_primary(get_agent: AsyncMock) -> None:
    assert get_agent.await_args_list, "the gate read no agent row"
    for call in get_agent.await_args_list:
        assert call.kwargs.get("read") is False, f"read from the replica: {call}"


@pytest.mark.parametrize("min_level", [1, 2, 3])
async def test_require_trust_reads_the_agent_from_the_primary(monkeypatch, min_level):
    get_agent = _agents(monkeypatch)

    await require_trust("t1", "a", min_level=min_level)

    _reads_primary(get_agent)


async def test_the_edit_gate_reads_the_agent_from_the_primary(monkeypatch):
    get_agent = _agents(monkeypatch)

    await agent_service.enforce_update("t1", "a", "a")

    _reads_primary(get_agent)


async def test_the_delete_gate_reads_the_agent_from_the_primary(monkeypatch):
    get_agent = _agents(monkeypatch)

    await agent_service.enforce_delete("t1", AgentIdentity("a"))

    _reads_primary(get_agent)


async def test_the_crystallization_gate_reads_the_agent_from_the_primary(monkeypatch):
    get_agent = _agents(monkeypatch)

    await agent_service.enforce_crystallize("t1", AgentIdentity("a"))

    _reads_primary(get_agent)


@pytest.mark.parametrize("fleet_id", [None, "f"])
async def test_the_list_read_gate_reads_the_agent_from_the_primary(
    monkeypatch, fleet_id
):
    """Behind ``GET /memories``, ``/memories/stats``, ``/count`` and ``/field``."""
    from core_api.auth import AuthContext
    from core_api.routes.memories import _gate_fleet_read

    get_agent = _agents(monkeypatch, trust=1)

    await _gate_fleet_read(
        AuthContext(tenant_id="t1", readable_tenant_ids=["t1"]),
        "t1",
        AgentIdentity("a"),
        fleet_id,
    )

    _reads_primary(get_agent)


async def test_the_by_id_read_gate_reads_the_agent_from_the_primary(monkeypatch):
    get_agent = _agents(monkeypatch, trust=1)

    await agent_service.authorize_memory_access(
        "t1",
        AgentIdentity("a"),
        visibility="scope_team",
        owner_agent_id="b",
        fleet_id="f",
    )

    _reads_primary(get_agent)


async def test_the_linked_row_reader_reads_the_agent_from_the_primary(monkeypatch):
    get_agent = _agents(monkeypatch, trust=1)

    await agent_service.memory_reader("t1", AgentIdentity("a"))

    _reads_primary(get_agent)


async def test_caura_recall_reads_its_agent_from_the_primary(mcp_env, monkeypatch):
    from core_api import mcp_server
    from core_api.errors import AUTH_AGENT_TRUST_TOO_LOW
    from tests._mcp_test_helpers import parse_envelope, stub_storage_client

    stub_storage_client(
        monkeypatch,
        get_agent={"agent_id": "reader", "fleet_id": "f", "trust_level": 0},
    )

    out = await mcp_server.caura_recall(query="deploy window", agent_id="reader")

    assert parse_envelope(out)["error"]["code"] == AUTH_AGENT_TRUST_TOO_LOW
    _reads_primary(mcp_server.get_storage_client().get_agent)


# --- core-storage-api ---------------------------------------------------------


def _sessions(monkeypatch, module, result: MagicMock) -> list[str]:
    """Replace ``module``'s two session factories; return which ones were opened."""
    opened: list[str] = []

    def _factory(name):
        @contextlib.asynccontextmanager
        async def _session():
            opened.append(name)
            session = AsyncMock()
            session.execute.return_value = result
            yield session

        return _session

    monkeypatch.setattr(module, "get_session", _factory("primary"))
    monkeypatch.setattr(module, "get_read_session", _factory("replica"))
    return opened


async def test_the_suppression_check_reads_the_primary(monkeypatch):
    result = MagicMock()
    result.first.return_value = None
    opened = _sessions(monkeypatch, ps, result)

    assert await ps.PostgresService().is_tenant_suppressed("t1") is False

    assert opened == ["primary"]


async def test_the_keystone_list_reads_the_primary(monkeypatch):
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    opened = _sessions(monkeypatch, keystones, result)

    assert await keystones.list_keystones(tenant_id="t1") == ([], False)

    assert opened == ["primary"]
