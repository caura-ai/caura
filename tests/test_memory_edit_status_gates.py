"""Who may change a memory's status through an edit, over REST and over MCP.

``PATCH /memories/{id}`` and ``caura_manage(op="update")`` take a ``status``
field beside the content, so an edit can change a status as
``PATCH /memories/{id}/status`` does. When an agent's edit sets a status the
row does not already have, it meets that route's rule
(``tests/test_memory_status_transition_gates.py``): an agent may retire its own
memory as ``outdated``, ``conflicted`` or ``cancelled`` at the edit bar, while
archiving or deleting it needs delete trust (3), and a peer's row needs trust 3
for any edit. An edit that leaves the status out, or names the one the row
already has, needs what any edit needs, so a client that sends the whole
record back is not refused for echoing its status.

A credential with no agent identity keeps the authority it already holds.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import NamedTuple
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from common.constants import LIVE_MEMORY_STATUSES
from common.enrichment.constants import MEMORY_STATUSES
from core_api.auth import AuthContext
from core_api.errors import AUTH_AGENT_TRUST_TOO_LOW
from core_api.routes import memories as memories_routes
from core_api.schemas import MemoryUpdate
from core_api.services import agent_service, memory_service
from tests.conftest import close_scheduled_coro, parse_envelope

pytestmark = pytest.mark.unit

_CLIENT = "core_api.clients.storage_client.CoreStorageClient"
_TENANT = "test-tenant"
_FLEET = "fleet-alpha"

# Every registered agent sits in one fleet, so the fleet check passes and the
# trust ladder is the only thing deciding.
_TRUST = {"zero": 0, "alice": 1, "bob": 1, "dana": 2, "carol": 3}

_NOT_LIVE = tuple(s for s in MEMORY_STATUSES if s not in LIVE_MEMORY_STATUSES)
# The decision under test, written out rather than imported from the gate, so a
# change to the gate's own list cannot change what these tests expect.
_OWNER_RETIRES = ("outdated", "conflicted", "cancelled")
_DELETE_BAR = tuple(s for s in _NOT_LIVE if s not in _OWNER_RETIRES)


class _Outcome(NamedTuple):
    code: str | None  # the refusal's error code, None when the edit went through
    stored: AsyncMock  # the storage write


Edit = Callable[[str | None, dict, dict], Awaitable[_Outcome]]


def _row(owner: str, status: str = "active") -> dict:
    mid = str(uuid.uuid4())
    return {
        "id": mid,
        "tenant_id": _TENANT,
        "fleet_id": _FLEET,
        "agent_id": owner,
        "content": f"the harbour opens at six, {mid}",
        "memory_type": "fact",
        "status": status,
        "visibility": "scope_team",
        "supersedes_id": None,
        "metadata_": {},
        "weight": 0.5,
        "recall_count": 0,
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:00:00+00:00",
        "deleted_at": None,
    }


@pytest.fixture
def lookups(monkeypatch) -> list[str]:
    """Serve ``_TRUST`` as the agent table; return the ids looked up."""
    seen: list[str] = []

    async def _lookup(tenant_id, agent_id, *, read=True):
        seen.append(agent_id)
        if agent_id not in _TRUST:
            return None
        return {
            "agent_id": agent_id,
            "fleet_id": _FLEET,
            "trust_level": _TRUST[agent_id],
        }

    monkeypatch.setattr(agent_service, "lookup_agent", _lookup)
    return seen


def _storage(monkeypatch, row: dict) -> AsyncMock:
    """Serve ``row`` from storage and apply edits to it; return the edit write."""

    async def _get(memory_id, tenant_id, *, read=True, **kw):
        return dict(row) if str(memory_id) == row["id"] else None

    async def _update(memory_id, tenant_id, patch):
        row.update({k: patch[k] for k in ("content", "status") if k in patch})
        return {}

    stored = AsyncMock(side_effect=_update)
    for name, method in {
        "get_memory": AsyncMock(side_effect=_get),
        "update_memory": stored,
        "find_duplicate_hash": AsyncMock(return_value=None),
        "reset_entity_artifacts": AsyncMock(return_value={}),
        "list_memory_conflicts": AsyncMock(return_value=[]),
        "get_entity_links_for_memories": AsyncMock(return_value={}),
    }.items():
        monkeypatch.setattr(f"{_CLIENT}.{name}", method)

    # A content edit re-embeds and schedules its follow-up work; none of it
    # bears on the gate, so it runs against nothing.
    class _Config:
        semantic_dedup_enabled = False
        entity_extraction_enabled = False
        enrichment_enabled = False
        enrichment_provider = "none"

    async def _resolve_config(_tenant):
        return _Config()

    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _resolve_config
    )
    monkeypatch.setattr(
        memory_service, "get_embedding", AsyncMock(return_value=[0.1, 0.2])
    )
    monkeypatch.setattr(memory_service, "track_task", close_scheduled_coro)
    monkeypatch.setattr(memory_service, "tracked_task", lambda coro, *a, **k: coro)
    return stored


async def _via_rest(
    monkeypatch, caller: str | None, row: dict, fields: dict
) -> _Outcome:
    stored = _storage(monkeypatch, row)
    monkeypatch.setattr(memories_routes, "check_and_increment", AsyncMock())
    auth = AuthContext(tenant_id=_TENANT, agent_id=caller)
    try:
        await memories_routes.update_memory_endpoint(
            uuid.UUID(row["id"]),
            MemoryUpdate(**fields),
            tenant_id=_TENANT,
            agent_id=None,
            metadata_mode=None,
            auth=auth,
        )
    except HTTPException as refused:
        assert refused.status_code == 403, refused.detail
        return _Outcome(refused.detail["code"], stored)
    return _Outcome(None, stored)


async def _via_mcp(
    monkeypatch, caller: str | None, row: dict, fields: dict
) -> _Outcome:
    from core_api import mcp_server

    stored = _storage(monkeypatch, row)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: caller)
    env = parse_envelope(
        await mcp_server.caura_manage(op="update", memory_id=row["id"], **fields)
    )
    return _Outcome((env.get("error") or {}).get("code"), stored)


@pytest.fixture(params=["rest", "mcp"])
def edit(request, monkeypatch, mcp_env, lookups) -> Edit:
    surface = {"rest": _via_rest, "mcp": _via_mcp}[request.param]

    async def _call(caller: str | None, row: dict, fields: dict) -> _Outcome:
        return await surface(monkeypatch, caller, row, fields)

    return _call


def _refused(outcome: _Outcome, code: str) -> None:
    assert outcome.code == code
    outcome.stored.assert_not_awaited()


def _changed(outcome: _Outcome) -> dict:
    """Assert the edit went through; return what it wrote."""
    assert outcome.code is None
    outcome.stored.assert_awaited_once()
    return outcome.stored.await_args.args[2]


@pytest.mark.parametrize("caller", ["alice", "dana"])
@pytest.mark.parametrize("status", _DELETE_BAR)
async def test_archiving_or_deleting_its_own_memory_by_edit_needs_delete_trust(
    edit, caller, status
):
    _refused(
        await edit(caller, _row(caller), {"status": status}), AUTH_AGENT_TRUST_TOO_LOW
    )


@pytest.mark.parametrize("status", _DELETE_BAR)
async def test_an_edit_with_content_cannot_archive_or_delete_below_delete_trust(
    edit, status
):
    _refused(
        await edit(
            "alice",
            _row("alice"),
            {"content": "the harbour opens at seven", "status": status},
        ),
        AUTH_AGENT_TRUST_TOO_LOW,
    )


@pytest.mark.parametrize("caller", ["alice", "dana"])
@pytest.mark.parametrize("status", _OWNER_RETIRES)
async def test_an_agent_may_retire_its_own_memory_by_edit_below_delete_trust(
    edit, caller, status
):
    written = _changed(await edit(caller, _row(caller), {"status": status}))
    assert written["status"] == status


@pytest.mark.parametrize("status", ["archived", "deleted"])
async def test_a_trust_3_agent_may_archive_or_delete_its_own_memory_by_edit(
    edit, status
):
    written = _changed(await edit("carol", _row("carol"), {"status": status}))
    assert written["status"] == status


@pytest.mark.parametrize("status", LIVE_MEMORY_STATUSES)
async def test_an_agent_moves_its_own_memory_between_live_statuses_by_edit(
    edit, status
):
    current = "active" if status == "pending" else "pending"
    written = _changed(
        await edit("alice", _row("alice", status=current), {"status": status})
    )
    assert written["status"] == status


async def test_a_content_edit_without_a_status_is_unchanged(edit):
    written = _changed(
        await edit("alice", _row("alice"), {"content": "the harbour opens at seven"})
    )
    assert written["content"] == "the harbour opens at seven"
    assert "status" not in written


@pytest.mark.parametrize("current", ["active", "archived"])
async def test_an_edit_that_echoes_the_current_status_needs_only_edit_trust(
    edit, current
):
    """A client may send the record back whole. Echoing a status the row
    already has changes nothing, so it is not held to the status's own bar."""
    written = _changed(
        await edit(
            "alice",
            _row("alice", status=current),
            {"content": "the harbour opens at seven", "status": current},
        )
    )
    assert written["content"] == "the harbour opens at seven"
    assert "status" not in written


@pytest.mark.parametrize(
    "fields", [{"status": "confirmed"}, {"status": "outdated"}, {"status": "archived"}]
)
async def test_an_agent_below_trust_3_cannot_edit_a_peers_memory_status(edit, fields):
    _refused(await edit("bob", _row("alice"), fields), AUTH_AGENT_TRUST_TOO_LOW)


async def test_a_trust_0_agent_cannot_retire_its_own_memory_by_edit(edit):
    _refused(
        await edit("zero", _row("zero"), {"status": "outdated"}),
        AUTH_AGENT_TRUST_TOO_LOW,
    )


async def test_a_credential_without_an_agent_is_not_held_to_the_trust_ladder(
    monkeypatch, mcp_env, lookups
):
    """REST only: over MCP the tool's ``agent_id`` argument names the agent an
    edit is made as, so a tool call always has one."""
    _changed(await _via_rest(monkeypatch, None, _row("alice"), {"status": "archived"}))
    assert lookups == []


def test_the_status_cases_are_drawn_from_the_vocabulary():
    """The cases above are partly derived, so they must not derive to nothing,
    and the owner's statuses must be real non-live ones."""
    assert set(LIVE_MEMORY_STATUSES) < set(MEMORY_STATUSES)
    assert set(_OWNER_RETIRES) < set(_NOT_LIVE)
    assert {"archived", "deleted"} <= set(_DELETE_BAR)
