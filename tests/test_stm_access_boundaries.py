"""Authorization boundaries on the private STM REST surfaces."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core_api.agent_ids import AgentIdentity
from core_api.auth import AuthContext
from core_api.routes import stm
from core_api.services import agent_service

pytestmark = pytest.mark.unit

TENANT = "tenant-1"


def _agent(agent_id: str) -> AuthContext:
    return AuthContext(tenant_id=TENANT, agent_id=AgentIdentity(agent_id))


def _install(install_uuid: str) -> AuthContext:
    return AuthContext(
        tenant_id=TENANT,
        is_install_credential=True,
        install_uuid=install_uuid,
    )


def _tenant_admin() -> AuthContext:
    return AuthContext(tenant_id=TENANT)


def _enable_stm(monkeypatch) -> None:
    monkeypatch.setattr(stm, "_check_stm_enabled", lambda: None)


async def test_install_credential_cannot_clear_another_installs_notes(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"owner_install_uuid": "install-owner"}),
    )
    clear = AsyncMock(return_value=True)
    monkeypatch.setattr("core_api.services.stm_service.clear_notes", clear)

    with pytest.raises(HTTPException) as exc:
        await stm.clear_notes(
            auth=_install("install-attacker"),
            agent_id="private-agent",
            tenant_id=None,
        )

    assert exc.value.status_code == 403
    clear.assert_not_awaited()


async def test_install_credential_can_clear_its_owned_notes(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"owner_install_uuid": "install-owner"}),
    )
    clear = AsyncMock(return_value=True)
    monkeypatch.setattr("core_api.services.stm_service.clear_notes", clear)

    result = await stm.clear_notes(
        auth=_install("install-owner"),
        agent_id="private-agent",
        tenant_id=None,
    )

    assert result["ok"] is True
    clear.assert_awaited_once_with(TENANT, "private-agent")


async def test_install_credential_cannot_read_another_installs_notes(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"owner_install_uuid": "install-owner"}),
    )
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("core_api.services.stm_service.read_notes", read)

    with pytest.raises(HTTPException) as exc:
        await stm.get_notes(
            auth=_install("install-attacker"),
            agent_id="private-agent",
            tenant_id=None,
            limit=50,
        )

    assert exc.value.status_code == 403
    read.assert_not_awaited()


async def test_install_credential_can_read_its_owned_notes(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"owner_install_uuid": "install-owner"}),
    )
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("core_api.services.stm_service.read_notes", read)

    result = await stm.get_notes(
        auth=_install("install-owner"),
        agent_id="private-agent",
        tenant_id=None,
        limit=50,
    )

    assert result["agent_id"] == "private-agent"
    read.assert_awaited_once_with(TENANT, "private-agent", limit=50)


async def test_trust_one_agent_cannot_read_another_fleets_bulletin(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"fleet_id": "own-fleet", "trust_level": 1}),
    )
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("core_api.services.stm_service.read_bulletin", read)

    with pytest.raises(HTTPException) as exc:
        await stm.get_bulletin(
            auth=_agent("agent-1"),
            fleet_id="other-fleet",
            tenant_id=None,
            limit=100,
        )

    assert exc.value.status_code == 403
    read.assert_not_awaited()


async def test_trust_one_agent_can_read_own_fleet_bulletin(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"fleet_id": "own-fleet", "trust_level": 1}),
    )
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("core_api.services.stm_service.read_bulletin", read)

    result = await stm.get_bulletin(
        auth=_agent("agent-1"),
        fleet_id="own-fleet",
        tenant_id=None,
        limit=100,
    )

    assert result["fleet_id"] == "own-fleet"
    read.assert_awaited_once_with(TENANT, "own-fleet", limit=100)


async def test_unregistered_agent_cannot_read_a_fleet_bulletin(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(agent_service, "lookup_agent", AsyncMock(return_value=None))
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("core_api.services.stm_service.read_bulletin", read)

    with pytest.raises(HTTPException) as exc:
        await stm.get_bulletin(
            auth=_agent("unknown-agent"),
            fleet_id="fleet-1",
            tenant_id=None,
            limit=100,
        )

    assert exc.value.status_code == 403
    read.assert_not_awaited()


async def test_trust_two_agent_can_read_another_fleets_bulletin(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"fleet_id": "own-fleet", "trust_level": 2}),
    )
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("core_api.services.stm_service.read_bulletin", read)

    result = await stm.get_bulletin(
        auth=_agent("agent-1"),
        fleet_id="other-fleet",
        tenant_id=None,
        limit=100,
    )

    assert result["fleet_id"] == "other-fleet"
    read.assert_awaited_once_with(TENANT, "other-fleet", limit=100)


async def test_tenant_admin_can_read_any_fleet_bulletin(monkeypatch):
    _enable_stm(monkeypatch)
    lookup = AsyncMock()
    monkeypatch.setattr(agent_service, "lookup_agent", lookup)
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("core_api.services.stm_service.read_bulletin", read)

    result = await stm.get_bulletin(
        auth=_tenant_admin(),
        fleet_id="any-fleet",
        tenant_id=None,
        limit=100,
    )

    assert result["fleet_id"] == "any-fleet"
    lookup.assert_not_awaited()
    read.assert_awaited_once_with(TENANT, "any-fleet", limit=100)


async def test_install_credential_cannot_read_fleet_bulletins(monkeypatch):
    _enable_stm(monkeypatch)
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("core_api.services.stm_service.read_bulletin", read)

    with pytest.raises(HTTPException) as exc:
        await stm.get_bulletin(
            auth=_install("install-1"),
            fleet_id="fleet-1",
            tenant_id=None,
            limit=100,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "AGENT_CREDENTIAL_FORBIDDEN"
    read.assert_not_awaited()


async def test_install_credential_cannot_clear_fleet_bulletins(monkeypatch):
    _enable_stm(monkeypatch)
    clear = AsyncMock(return_value=True)
    monkeypatch.setattr("core_api.services.stm_service.clear_bulletin", clear)

    with pytest.raises(HTTPException) as exc:
        await stm.clear_bulletin(
            auth=_install("install-1"),
            fleet_id="fleet-1",
            tenant_id=None,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "AGENT_CREDENTIAL_FORBIDDEN"
    clear.assert_not_awaited()


async def test_non_admin_agent_cannot_clear_a_bulletin(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"fleet_id": "own-fleet", "trust_level": 2}),
    )
    clear = AsyncMock(return_value=True)
    monkeypatch.setattr("core_api.services.stm_service.clear_bulletin", clear)

    with pytest.raises(HTTPException) as exc:
        await stm.clear_bulletin(
            auth=_agent("agent-1"),
            fleet_id="own-fleet",
            tenant_id=None,
        )

    assert exc.value.status_code == 403
    clear.assert_not_awaited()


async def test_tenant_admin_can_clear_any_fleet_bulletin(monkeypatch):
    _enable_stm(monkeypatch)
    lookup = AsyncMock()
    monkeypatch.setattr(agent_service, "lookup_agent", lookup)
    clear = AsyncMock(return_value=True)
    monkeypatch.setattr("core_api.services.stm_service.clear_bulletin", clear)

    result = await stm.clear_bulletin(
        auth=_tenant_admin(),
        fleet_id="any-fleet",
        tenant_id=None,
    )

    assert result["ok"] is True
    lookup.assert_not_awaited()
    clear.assert_awaited_once_with(TENANT, "any-fleet")


async def test_trust_three_agent_can_clear_a_bulletin(monkeypatch):
    _enable_stm(monkeypatch)
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"fleet_id": "own-fleet", "trust_level": 3}),
    )
    clear = AsyncMock(return_value=True)
    monkeypatch.setattr("core_api.services.stm_service.clear_bulletin", clear)

    result = await stm.clear_bulletin(
        auth=_agent("agent-1"),
        fleet_id="own-fleet",
        tenant_id=None,
    )

    assert result["ok"] is True
    clear.assert_awaited_once_with(TENANT, "own-fleet")
