from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api.agent_ids import (
    INSIGHTER_AGENT_ID,
    LEGACY_INSIGHTER_AGENT_ID,
    AgentIdentity,
)
from core_api.auth import AuthContext
from core_api.routes import reports
from core_api.services import agent_service

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_auth_context_treats_service_aliases_as_one_identity() -> None:
    auth = AuthContext(tenant_id="tenant", agent_id=LEGACY_INSIGHTER_AGENT_ID)

    auth.enforce_self_agent(INSIGHTER_AGENT_ID)
    assert auth.effective_agent_id(LEGACY_INSIGHTER_AGENT_ID) == INSIGHTER_AGENT_ID


async def test_scope_agent_access_accepts_legacy_owner() -> None:
    assert await agent_service.authorize_memory_access(
        "tenant",
        AgentIdentity(INSIGHTER_AGENT_ID),
        visibility="scope_agent",
        owner_agent_id=LEGACY_INSIGHTER_AGENT_ID,
        fleet_id=None,
    )


async def test_fleet_gate_uses_legacy_registration(monkeypatch) -> None:
    storage = SimpleNamespace(
        get_agent=AsyncMock(
            side_effect=lambda agent_id, tenant_id, read=True: (
                {"agent_id": agent_id, "fleet_id": "home", "trust_level": 1}
                if agent_id == LEGACY_INSIGHTER_AGENT_ID
                else None
            )
        )
    )
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: storage)

    assert await agent_service.resolve_read_fleet_gate(
        "tenant", INSIGHTER_AGENT_ID, "fleet", None
    ) == (1, "home")


async def test_trust_update_targets_resolved_legacy_row(monkeypatch) -> None:
    legacy = {
        "agent_id": LEGACY_INSIGHTER_AGENT_ID,
        "trust_level": 1,
        "fleet_id": "home",
    }
    storage = SimpleNamespace(
        get_agent=AsyncMock(
            side_effect=lambda agent_id, tenant_id, read=True: (
                legacy if agent_id == LEGACY_INSIGHTER_AGENT_ID else None
            )
        ),
        update_trust_level=AsyncMock(),
    )
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: storage)

    await agent_service.update_trust_level("tenant", INSIGHTER_AGENT_ID, 3)

    storage.update_trust_level.assert_awaited_once_with(
        LEGACY_INSIGHTER_AGENT_ID,
        {"tenant_id": "tenant", "trust_level": 3},
    )


async def test_cached_digest_prefers_canonical_row_when_aliases_coexist(
    monkeypatch,
) -> None:
    common = {
        "generated_at": "2026-09-21T03:00:00+00:00",
        "window_start": "2026-09-20T00:00:00+00:00",
        "window_end": "2026-09-21T00:00:00+00:00",
        "model": "test-model",
    }
    storage = SimpleNamespace(
        get_agent_activity_digest=AsyncMock(
            return_value=[
                {**common, "agent_id": LEGACY_INSIGHTER_AGENT_ID, "summary": "legacy"},
                {**common, "agent_id": INSIGHTER_AGENT_ID, "summary": "canonical"},
            ]
        )
    )
    monkeypatch.setattr(reports, "get_storage_client", lambda: storage)
    auth = AuthContext(tenant_id="tenant", readable_tenant_ids=["tenant", "other"])

    response = await reports.get_agent_activity_digest(
        period="day",
        scope="own",
        tenant_id="tenant",
        agent_id=INSIGHTER_AGENT_ID,
        as_of=None,
        readable_tenant_ids=None,
        auth=auth,
    )

    assert response["meta"]["agents"] == 1
    assert response["digests"] == [
        {**common, "agent_id": INSIGHTER_AGENT_ID, "summary": "canonical"}
    ]
