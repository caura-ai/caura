"""Who may change a memory's status, over REST and over MCP.

``PATCH /memories/{id}/status`` and ``caura_manage(op="transition")`` are one
operation on two surfaces, so every case below runs on both.

A status change edits the row, so an agent credential needs what an edit needs
(``enforce_update``): its own row, a peer's only at trust 3, and nothing at all
at trust 0. A status outside ``LIVE_MEMORY_STATUSES`` also takes the row out of
search and recall for every reader, which is what a delete does, so it needs
what a delete needs as well (``enforce_delete``, trust 3). Without both, the
status verb could do what the edit and delete routes refuse.

A credential with no agent identity (a tenant key, a person, an install
credential) has no trust level to compare and keeps the authority it already
holds, as ``enforce_delete``'s contract says; the row check
(``authorize_memory_access``) still applies to it, and is covered in
``tests/test_memory_byid_authz.py``.
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
from core_api.errors import AUTH_AGENT_NOT_REGISTERED, AUTH_AGENT_TRUST_TOO_LOW
from core_api.routes import memories as memories_routes
from core_api.services import agent_service
from tests._mcp_test_helpers import stub_storage_client
from tests.conftest import parse_envelope

pytestmark = pytest.mark.unit

_CLIENT = "core_api.clients.storage_client.CoreStorageClient"
_TENANT = "test-tenant"
_FLEET = "fleet-alpha"

# Every registered agent sits in one fleet, so the fleet check passes and the
# trust ladder is the only thing deciding.
_TRUST = {"zero": 0, "alice": 1, "bob": 1, "dana": 2, "carol": 3}

_NOT_LIVE = tuple(s for s in MEMORY_STATUSES if s not in LIVE_MEMORY_STATUSES)


class _Outcome(NamedTuple):
    code: str | None  # the refusal's error code, None when the change went through
    stored: AsyncMock  # the storage status write


Transition = Callable[[str | None, dict, str], Awaitable[_Outcome]]


def _row(owner: str, visibility: str = "scope_team") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "status": "active",
        "agent_id": owner,
        "fleet_id": _FLEET,
        "visibility": visibility,
        "metadata_": {},
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


async def _via_rest(
    monkeypatch, caller: str | None, row: dict, status: str
) -> _Outcome:
    stored = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(f"{_CLIENT}.get_memory", AsyncMock(return_value=row))
    monkeypatch.setattr(f"{_CLIENT}.update_memory_status", stored)
    monkeypatch.setattr(memories_routes, "log_action", AsyncMock())
    auth = AuthContext(tenant_id=_TENANT, agent_id=caller)
    try:
        await memories_routes.update_memory_status(
            uuid.UUID(row["id"]), {"status": status}, tenant_id=_TENANT, auth=auth
        )
    except HTTPException as refused:
        assert refused.status_code == 403, refused.detail
        return _Outcome(refused.detail["code"], stored)
    return _Outcome(None, stored)


async def _via_mcp(monkeypatch, caller: str | None, row: dict, status: str) -> _Outcome:
    from core_api import mcp_server

    sc = stub_storage_client(monkeypatch, get_memory=row, update_memory_status=None)
    monkeypatch.setattr(mcp_server, "log_action", AsyncMock())
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: caller)
    env = parse_envelope(
        await mcp_server.caura_manage(
            op="transition", memory_id=row["id"], status=status
        )
    )
    return _Outcome((env.get("error") or {}).get("code"), sc.update_memory_status)


@pytest.fixture(params=["rest", "mcp"])
def transition(request, monkeypatch, mcp_env, lookups) -> Transition:
    surface = {"rest": _via_rest, "mcp": _via_mcp}[request.param]

    async def _call(caller: str | None, row: dict, status: str) -> _Outcome:
        return await surface(monkeypatch, caller, row, status)

    return _call


def _refused(outcome: _Outcome, code: str) -> None:
    assert outcome.code == code
    outcome.stored.assert_not_awaited()


def _changed(outcome: _Outcome) -> None:
    assert outcome.code is None
    outcome.stored.assert_awaited_once()


@pytest.mark.parametrize("visibility", ["scope_team", "scope_org"])
@pytest.mark.parametrize("status", ["confirmed", "archived"])
async def test_an_agent_below_trust_3_cannot_change_a_peers_memory_status(
    transition, visibility, status
):
    _refused(
        await transition("bob", _row("alice", visibility), status),
        AUTH_AGENT_TRUST_TOO_LOW,
    )


async def test_a_trust_0_agent_cannot_change_its_own_memory_status(transition):
    _refused(
        await transition("zero", _row("zero"), "confirmed"), AUTH_AGENT_TRUST_TOO_LOW
    )


@pytest.mark.parametrize("caller", ["alice", "dana"])
@pytest.mark.parametrize("status", _NOT_LIVE)
async def test_taking_a_memory_out_of_the_live_statuses_needs_delete_trust(
    transition, caller, status
):
    _refused(await transition(caller, _row(caller), status), AUTH_AGENT_TRUST_TOO_LOW)


async def test_an_unregistered_agent_cannot_change_a_memory_status(transition):
    _refused(
        await transition("ghost", _row("alice"), "confirmed"), AUTH_AGENT_NOT_REGISTERED
    )


@pytest.mark.parametrize("status", LIVE_MEMORY_STATUSES)
async def test_an_agent_moves_its_own_memory_between_live_statuses(transition, status):
    _changed(await transition("alice", _row("alice"), status))


@pytest.mark.parametrize("status", ["confirmed", "archived", "deleted"])
async def test_a_trust_3_agent_may_change_a_peers_memory_status(transition, status):
    _changed(await transition("carol", _row("alice"), status))


async def test_a_credential_without_an_agent_is_not_held_to_the_trust_ladder(
    transition, lookups
):
    _changed(await transition(None, _row("alice"), "archived"))
    assert lookups == []


def test_the_live_statuses_are_a_proper_subset_of_the_vocabulary():
    """The not-live cases above are derived, so they must not derive to nothing."""
    assert set(LIVE_MEMORY_STATUSES) < set(MEMORY_STATUSES)
    assert {"outdated", "cancelled", "archived", "deleted"} <= set(_NOT_LIVE)
