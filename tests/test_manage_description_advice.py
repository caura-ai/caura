"""The advice in ``caura_manage``'s description must work for the agent reading it.

The description is served in every session's ``tools/list`` and to the plugin.
Where it recommends a transition in place of a delete, an agent at the default
trust level must be able to make that transition on its own memory, or the
advice leads it straight to a refusal.
"""

from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from core_api.constants import DEFAULT_TRUST_LEVEL
from core_api.services import agent_service
from core_api.tools._registry import get_spec
from tests._mcp_test_helpers import stub_storage_client
from tests.conftest import parse_envelope

pytestmark = pytest.mark.unit

_AGENT = "agent-default"
_ADVICE = re.compile(r"prefer transition to ([a-z]+(?:/[a-z]+)*)")


def _recommended() -> list[str]:
    match = _ADVICE.search(get_spec("caura_manage").description)
    assert match, (
        "the description no longer recommends a transition; retire this test with it"
    )
    return match.group(1).split("/")


@pytest.mark.parametrize("status", _recommended())
async def test_a_default_trust_agent_can_follow_the_transition_advice(
    mcp_env, monkeypatch, status
):
    async def _lookup(tenant_id, agent_id, *, read=True):
        return {
            "agent_id": agent_id,
            "fleet_id": None,
            "trust_level": DEFAULT_TRUST_LEVEL,
        }

    monkeypatch.setattr(agent_service, "lookup_agent", _lookup)
    row = {
        "id": str(uuid.uuid4()),
        "status": "active",
        "agent_id": _AGENT,
        "fleet_id": None,
        "visibility": "scope_team",
        "metadata_": {},
    }
    sc = stub_storage_client(monkeypatch, get_memory=row, update_memory_status=None)
    monkeypatch.setattr(mcp_server, "log_action", AsyncMock())
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: _AGENT)

    env = parse_envelope(
        await mcp_server.caura_manage(
            op="transition", memory_id=row["id"], status=status
        )
    )

    assert env.get("error") is None, env
    sc.update_memory_status.assert_awaited_once()
