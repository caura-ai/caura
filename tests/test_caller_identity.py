"""Unit tests for the shared evolve/insights caller-identity resolver (§2)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core_api.agent_ids import DEFAULT_AGENT_ID
from core_api.services import caller_identity
from core_api.services.caller_identity import resolve_caller_and_gate

pytestmark = pytest.mark.asyncio


def _auth(*, agent_id=None, is_admin=False):
    return SimpleNamespace(agent_id=agent_id, is_admin=is_admin)


async def test_standalone_operator_bypasses_gate(monkeypatch):
    """IS_STANDALONE + non-admin + no asserted identity → reserved default,
    trust gate skipped. This is the F-7 standalone tail, now closed for
    evolve/insights (the test env runs IS_STANDALONE=true)."""
    gate = AsyncMock()
    monkeypatch.setattr(caller_identity, "require_trust", gate)
    result = await resolve_caller_and_gate(
        AsyncMock(),
        _auth(),
        tenant_id="default",
        body_agent_id=None,
        scope="agent",
        action="evolve",
    )
    assert result == DEFAULT_AGENT_ID
    gate.assert_not_awaited()


async def test_admin_bypasses_gate(monkeypatch):
    """Admins are tenant-free system callers — gate skipped before the
    standalone check even runs."""
    gate = AsyncMock()
    monkeypatch.setattr(caller_identity, "require_trust", gate)
    result = await resolve_caller_and_gate(
        AsyncMock(),
        _auth(is_admin=True),
        tenant_id="t",
        body_agent_id=None,
        scope="fleet",
        action="insights",
    )
    assert result == DEFAULT_AGENT_ID
    gate.assert_not_awaited()


async def test_asserted_unregistered_identity_403(monkeypatch):
    """An explicit (non-default) identity still goes through the gate — the
    bypass only applies when NO identity is asserted. Unregistered → 403."""
    monkeypatch.setattr(caller_identity, "require_trust", AsyncMock(return_value=(1, True, None)))
    with pytest.raises(HTTPException) as exc:
        await resolve_caller_and_gate(
            AsyncMock(),
            _auth(),
            tenant_id="t",
            body_agent_id="ghost",
            scope="agent",
            action="evolve",
        )
    assert exc.value.status_code == 403
    assert "is not registered" in str(exc.value.detail)


async def test_verified_registered_identity_passes(monkeypatch):
    """Gateway-verified identity is gated and, when registered at trust, wins."""
    monkeypatch.setattr(caller_identity, "require_trust", AsyncMock(return_value=(2, False, None)))
    result = await resolve_caller_and_gate(
        AsyncMock(),
        _auth(agent_id="backend-dev"),
        tenant_id="t",
        body_agent_id=None,
        scope="fleet",
        action="evolve",
    )
    assert result == "backend-dev"
