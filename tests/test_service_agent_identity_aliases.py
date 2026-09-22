from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api.agent_ids import INSIGHTER_AGENT_ID
from core_api.auth import AuthContext
from core_api.services import agent_service

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_RETIRED_INSIGHTER_INPUT = "memclaw-insighter"  # legacy-name-ok: supported input alias


async def test_auth_context_treats_retired_input_as_canonical_identity() -> None:
    auth = AuthContext(tenant_id="tenant", agent_id=_RETIRED_INSIGHTER_INPUT)

    auth.enforce_self_agent(INSIGHTER_AGENT_ID)
    assert auth.effective_agent_id(_RETIRED_INSIGHTER_INPUT) == INSIGHTER_AGENT_ID


async def test_agent_registration_normalizes_retired_input_before_lookup(
    monkeypatch,
) -> None:
    canonical = {
        "agent_id": INSIGHTER_AGENT_ID,
        "fleet_id": "fleet",
        "trust_level": 3,
    }
    storage = SimpleNamespace(
        get_agent=AsyncMock(return_value=canonical),
        create_or_update_agent=AsyncMock(),
    )
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: storage)

    result = await agent_service.get_or_create_agent(
        "tenant",
        _RETIRED_INSIGHTER_INPUT,
        "fleet",
    )

    assert result == canonical
    storage.get_agent.assert_awaited_once_with(INSIGHTER_AGENT_ID, "tenant")
    storage.create_or_update_agent.assert_not_awaited()


async def test_agent_registration_never_creates_a_retired_identity(monkeypatch) -> None:
    canonical = {
        "id": "agent-row",
        "agent_id": INSIGHTER_AGENT_ID,
        "fleet_id": "fleet",
        "trust_level": 1,
    }
    storage = SimpleNamespace(
        get_agent=AsyncMock(return_value=None),
        create_or_update_agent=AsyncMock(return_value=canonical),
    )
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: storage)
    monkeypatch.setattr(agent_service, "log_action", AsyncMock())

    await agent_service.get_or_create_agent(
        "tenant",
        _RETIRED_INSIGHTER_INPUT,
        "fleet",
    )

    assert storage.get_agent.await_args_list[0].args == (INSIGHTER_AGENT_ID, "tenant")
    assert storage.get_agent.await_args_list[1].args == (INSIGHTER_AGENT_ID, "tenant")
    assert (
        storage.create_or_update_agent.await_args.args[0]["agent_id"]
        == INSIGHTER_AGENT_ID
    )
