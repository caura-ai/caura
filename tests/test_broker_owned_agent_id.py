"""Unit tests for the broker-attribution ownership gate (_broker_owned_agent_id).

A broker (install-credential) write may name an agent (REST item metadata /
body.agent_id, or the MCP agent id); the gate keeps that attribution only when
this install may claim the agent, and otherwise degrades to the bare
``broker:<install>`` identity so one install can't write under another install's
agent id. Lenient — never raises. Lives in ``agent_service`` so every write
entry point (REST + MCP) shares it via ``resolve_write_agent``.
"""

from unittest.mock import AsyncMock

import pytest

from core_api.services import agent_service

pytestmark = pytest.mark.unit


async def test_owned_by_this_install_kept(monkeypatch):
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(
            return_value={"agent_id": "agent-a", "owner_install_uuid": "install-1"}
        ),
    )
    assert (
        await agent_service._broker_owned_agent_id("agent-a", "install-1", "t")
        == "agent-a"
    )


async def test_owned_by_different_install_degraded(monkeypatch):
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(
            return_value={"agent_id": "agent-a", "owner_install_uuid": "install-2"}
        ),
    )
    assert (
        await agent_service._broker_owned_agent_id("agent-a", "install-1", "t")
        == "broker:install-1"
    )


async def test_null_owner_kept(monkeypatch):
    # Grandfathered / unclaimed agent — this write first-touches it.
    monkeypatch.setattr(
        agent_service,
        "lookup_agent",
        AsyncMock(return_value={"agent_id": "agent-a", "owner_install_uuid": None}),
    )
    assert (
        await agent_service._broker_owned_agent_id("agent-a", "install-1", "t")
        == "agent-a"
    )


async def test_nonexistent_agent_kept(monkeypatch):
    # No row yet — this write creates + owns it via get_or_create_agent.
    monkeypatch.setattr(agent_service, "lookup_agent", AsyncMock(return_value=None))
    assert (
        await agent_service._broker_owned_agent_id("agent-a", "install-1", "t")
        == "agent-a"
    )


async def test_already_fallback_short_circuits(monkeypatch):
    # The chosen id is already the install fallback — no lookup needed.
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_service, "lookup_agent", spy)
    result = await agent_service._broker_owned_agent_id(
        "broker:install-1", "install-1", "t"
    )
    assert result == "broker:install-1"
    spy.assert_not_awaited()


async def test_foreign_broker_label_degraded_without_lookup(monkeypatch):
    # Reserved ``broker:`` namespace: naming ANOTHER install's fallback degrades
    # to THIS install's own fallback with NO lookup, so the deterministic
    # (guessable) fallback id can't be pre-claimed to capture a victim's writes.
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_service, "lookup_agent", spy)
    result = await agent_service._broker_owned_agent_id(
        "broker:install-1", "install-3", "t"
    )
    assert result == "broker:install-3"
    spy.assert_not_awaited()
