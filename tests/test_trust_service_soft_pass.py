"""Unit tests for ``trust_service.require_trust`` soft-pass behavior.

Locks in the contract that a missing Agent row is NOT a permission
failure — API-key admission already proved authorization, so a fresh
agent that hasn't yet been materialised by a write must still be
allowed any tool the tenant's default policy permits.

Pre-fix, ``require_trust`` returned ``(0, True, "Error (403): Agent 'X'
not found ...")``: any read tool gated at ``min_level >= 1`` (e.g.
``memclaw_list`` with ``scope='agent'``) 403'd a brand-new agent until
they happened to write something first to register themselves.

Post-fix:
  * ``not_found=True`` AND ``DEFAULT_TRUST_LEVEL >= min_level`` → pass.
  * ``not_found=True`` AND ``DEFAULT_TRUST_LEVEL < min_level``  → fail
    with the standard insufficient-trust message (no 'not found' wording).
  * Existing behaviour for known rows is unchanged.
"""

from __future__ import annotations

import pytest

from core_api.constants import DEFAULT_TRUST_LEVEL
from core_api.services import trust_service

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers — patch ``lookup_agent`` since require_trust imports it lazily.
# ---------------------------------------------------------------------------


def _patch_lookup(monkeypatch, return_value):
    async def _fake_lookup(db, tenant_id, agent_id):  # noqa: ARG001
        return return_value

    # require_trust does ``from core_api.services.agent_service import lookup_agent``
    # inside the function body, so patch the source module.
    from core_api.services import agent_service

    monkeypatch.setattr(agent_service, "lookup_agent", _fake_lookup)


# ---------------------------------------------------------------------------
# Soft-pass for missing rows
# ---------------------------------------------------------------------------


async def test_missing_row_passes_when_default_meets_min_level(monkeypatch):
    """``min_level=1`` and no Agent row → pass with not_found=True."""
    _patch_lookup(monkeypatch, None)
    trust, not_found, terr = await trust_service.require_trust(
        db=None, tenant_id="t1", agent_id="ghost", min_level=1
    )
    assert terr is None
    assert not_found is True
    assert trust == DEFAULT_TRUST_LEVEL


async def test_missing_row_fails_when_default_below_min_level(monkeypatch):
    """``min_level=2`` and no Agent row → fail (DEFAULT_TRUST_LEVEL=1 < 2).

    Error message distinguishes "no row" from a real ``trust_level``
    denial so an operator debugging the 403 doesn't go looking for a
    row to upgrade that doesn't exist. Recovery hint points at the
    standard ``write one memory first`` path.
    """
    _patch_lookup(monkeypatch, None)
    trust, not_found, terr = await trust_service.require_trust(
        db=None, tenant_id="t1", agent_id="ghost", min_level=2
    )
    assert terr is not None
    assert "not registered" in terr
    assert "(no row;" in terr
    assert "< required 2" in terr
    assert "writing one memory first" in terr
    assert not_found is True
    assert trust == DEFAULT_TRUST_LEVEL


async def test_missing_row_admin_min_level_fails(monkeypatch):
    """``min_level=3`` (admin) and no Agent row → fail. Same message shape."""
    _patch_lookup(monkeypatch, None)
    _, not_found, terr = await trust_service.require_trust(
        db=None, tenant_id="t1", agent_id="ghost", min_level=3
    )
    assert terr is not None
    assert "not registered" in terr
    assert "< required 3" in terr
    assert not_found is True


# ---------------------------------------------------------------------------
# Existing behaviour for known rows must not regress
# ---------------------------------------------------------------------------


async def test_known_agent_passes_at_meeting_trust(monkeypatch):
    _patch_lookup(monkeypatch, {"trust_level": 2})
    trust, not_found, terr = await trust_service.require_trust(
        db=None, tenant_id="t1", agent_id="alice", min_level=2
    )
    assert terr is None
    assert not_found is False
    assert trust == 2


async def test_known_agent_fails_below_trust(monkeypatch):
    _patch_lookup(monkeypatch, {"trust_level": 1})
    trust, not_found, terr = await trust_service.require_trust(
        db=None, tenant_id="t1", agent_id="alice", min_level=2
    )
    assert terr is not None
    assert "trust_level=1" in terr
    assert "< required 2" in terr
    assert not_found is False
    assert trust == 1


async def test_zero_trust_known_agent_blocked_at_min_1(monkeypatch):
    """A known agent at trust_level=0 (require_approval pending) is
    blocked even at min_level=1 — the soft-pass only applies when the
    row is missing entirely."""
    _patch_lookup(monkeypatch, {"trust_level": 0})
    _, not_found, terr = await trust_service.require_trust(
        db=None, tenant_id="t1", agent_id="alice", min_level=1
    )
    assert terr is not None
    assert "trust_level=0" in terr
    assert not_found is False
