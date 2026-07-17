"""Broker-write agent-ownership boundary — the shared ``resolve_write_agent``
helper and its wiring into BOTH write routes.

The ownership boundary (a broker/install-credential caller may only attribute a
memory to an agent it owns) lives in one place — ``resolve_write_agent`` — so an
install can't bypass it by choosing the single- vs bulk-write endpoint. This
module tests:

- ``resolve_write_agent`` directly: the gate (degrade a foreign / reserved
  ``broker:`` id), the first-touch owner stamp, the post-create TOCTOU
  re-check, non-broker passthrough, and ``require_approval`` passthrough.
- End-to-end that BOTH ``_write_memory_inner`` (single) and
  ``_write_memories_bulk_inner`` (bulk) route attribution through the helper, so
  a broker naming another install's agent is degraded to ``broker:<install>``
  before the memory is written.

The pure gate branches are additionally unit-tested in
``test_broker_owned_agent_id.py``. Storage/metering are mocked here; only the
agent id that ends up attributed to the write is asserted.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response

from core_api.auth import AuthContext
from core_api.config import settings as app_settings
from core_api.routes import memories
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem, MemoryCreate

pytestmark = pytest.mark.unit


def _broker_auth(
    install_uuid: str | None = "install-1", *, is_install: bool = True
) -> AuthContext:
    return AuthContext(
        tenant_id="tenant-1",
        is_install_credential=is_install,
        install_uuid=install_uuid,
    )


class _Sentinel(Exception):
    """Raised by the mocked create_* to short-circuit after attribution is set."""


# ── resolve_write_agent (shared helper) ─────────────────────────────────


async def test_resolve_non_broker_passthrough(monkeypatch):
    # Non-broker caller: no gate lookup, no owner stamp, id unchanged.
    lookup = AsyncMock()
    goc = AsyncMock(return_value={"owner_install_uuid": None, "fleet_id": None})
    monkeypatch.setattr(memories, "lookup_agent", lookup)
    monkeypatch.setattr(memories, "get_or_create_agent", goc)
    agent, agent_id = await memories.resolve_write_agent(
        "dash-agent", _broker_auth(None, is_install=False), "tenant-1", None
    )
    assert agent_id == "dash-agent"
    lookup.assert_not_awaited()
    assert goc.await_args.kwargs["owner_install_uuid"] is None


async def test_resolve_broker_foreign_agent_degraded(monkeypatch):
    # Gate: named agent owned by a different install -> degrade to own fallback.
    monkeypatch.setattr(
        memories,
        "lookup_agent",
        AsyncMock(return_value={"owner_install_uuid": "install-2"}),
    )
    goc = AsyncMock(return_value={"owner_install_uuid": "install-1", "fleet_id": None})
    monkeypatch.setattr(memories, "get_or_create_agent", goc)
    _agent, agent_id = await memories.resolve_write_agent(
        "victim", _broker_auth("install-1"), "tenant-1", None
    )
    assert agent_id == "broker:install-1"
    assert goc.await_args_list[0].args[1] == "broker:install-1"


async def test_resolve_reserved_namespace_degraded_without_lookup(monkeypatch):
    # Gate: another install's reserved broker:<x> id -> degrade, no lookup.
    lookup = AsyncMock()
    monkeypatch.setattr(memories, "lookup_agent", lookup)
    monkeypatch.setattr(
        memories,
        "get_or_create_agent",
        AsyncMock(return_value={"owner_install_uuid": "install-1", "fleet_id": None}),
    )
    _agent, agent_id = await memories.resolve_write_agent(
        "broker:install-9", _broker_auth("install-1"), "tenant-1", None
    )
    assert agent_id == "broker:install-1"
    lookup.assert_not_awaited()


async def test_resolve_self_owned_kept(monkeypatch):
    monkeypatch.setattr(
        memories,
        "lookup_agent",
        AsyncMock(return_value={"owner_install_uuid": "install-1"}),
    )
    monkeypatch.setattr(
        memories,
        "get_or_create_agent",
        AsyncMock(return_value={"owner_install_uuid": "install-1", "fleet_id": None}),
    )
    _agent, agent_id = await memories.resolve_write_agent(
        "my-agent", _broker_auth("install-1"), "tenant-1", None
    )
    assert agent_id == "my-agent"


async def test_resolve_post_create_race_lost_degrades(monkeypatch):
    # Gate passes (row absent), but the committed row is owned by another
    # install (lost first-touch race) -> post-create re-check degrades + recreates.
    monkeypatch.setattr(memories, "lookup_agent", AsyncMock(return_value=None))
    goc = AsyncMock(
        side_effect=[
            {
                "owner_install_uuid": "install-2",
                "fleet_id": None,
            },  # winner is install-2
            {
                "owner_install_uuid": "install-1",
                "fleet_id": None,
            },  # recreate under broker id
        ]
    )
    monkeypatch.setattr(memories, "get_or_create_agent", goc)
    _agent, agent_id = await memories.resolve_write_agent(
        "contested", _broker_auth("install-1"), "tenant-1", None
    )
    assert agent_id == "broker:install-1"
    assert goc.await_count == 2
    assert goc.await_args_list[1].args[1] == "broker:install-1"


async def test_resolve_passes_require_approval(monkeypatch):
    goc = AsyncMock(return_value={"owner_install_uuid": None, "fleet_id": None})
    monkeypatch.setattr(memories, "get_or_create_agent", goc)
    monkeypatch.setattr(memories, "lookup_agent", AsyncMock())
    await memories.resolve_write_agent(
        "a",
        _broker_auth(None, is_install=False),
        "tenant-1",
        None,
        require_approval=True,
    )
    assert goc.await_args.kwargs["require_approval"] is True


# ── End-to-end: both routes attribute through resolve_write_agent ────────


async def _drive_single(monkeypatch, *, agent_id, auth, gate_owner, created_owner):
    """Drive ``_write_memory_inner`` with storage/metering mocked; return the
    agent id that reaches ``create_memory``."""
    monkeypatch.setattr(app_settings, "bind_write_identity_to_auth", False)
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace(require_agent_approval=False)),
    )
    monkeypatch.setattr(
        memories,
        "lookup_agent",
        AsyncMock(
            return_value=None
            if gate_owner is None
            else {"owner_install_uuid": gate_owner}
        ),
    )
    monkeypatch.setattr(
        memories,
        "get_or_create_agent",
        AsyncMock(
            return_value={
                "owner_install_uuid": created_owner,
                "trust_level": 1,
                "fleet_id": None,
            }
        ),
    )
    monkeypatch.setattr(memories, "enforce_fleet_write", AsyncMock(return_value={}))
    monkeypatch.setattr(memories, "check_and_increment", AsyncMock(return_value=None))
    captured: dict[str, str | None] = {}

    async def _create(body_):
        captured["agent_id"] = body_.agent_id
        raise _Sentinel()

    monkeypatch.setattr(memories, "create_memory", _create)
    body = MemoryCreate(tenant_id="tenant-1", agent_id=agent_id, content="hello world")
    with pytest.raises(_Sentinel):
        await memories._write_memory_inner(body, Response(), auth, None)
    return captured["agent_id"]


async def _drive_bulk(monkeypatch, *, agent_id, auth, gate_owner, created_owner):
    """Drive ``_write_memories_bulk_inner`` with storage/metering mocked; return
    the agent id that reaches ``create_memories_bulk``."""
    monkeypatch.setattr(app_settings, "bind_write_identity_to_auth", False)
    monkeypatch.setattr(
        memories,
        "lookup_agent",
        AsyncMock(
            return_value=None
            if gate_owner is None
            else {"owner_install_uuid": gate_owner}
        ),
    )
    monkeypatch.setattr(
        memories,
        "get_or_create_agent",
        AsyncMock(return_value={"owner_install_uuid": created_owner, "fleet_id": None}),
    )
    monkeypatch.setattr(memories, "enforce_fleet_write", AsyncMock(return_value={}))
    monkeypatch.setattr(
        memories, "bulk_check_and_increment", AsyncMock(return_value=None)
    )
    captured: dict[str, str | None] = {}

    async def _create(body_, *, bulk_attempt_id=None):
        captured["agent_id"] = body_.agent_id
        raise _Sentinel()

    monkeypatch.setattr(memories, "create_memories_bulk", _create)
    body = BulkMemoryCreate(
        tenant_id="tenant-1",
        agent_id=agent_id,
        items=[BulkMemoryItem(content="hello world")],
    )
    with pytest.raises(_Sentinel):
        await memories._write_memories_bulk_inner(
            body, Response(), auth, None, "attempt-1"
        )
    return captured["agent_id"]


async def test_single_write_broker_foreign_agent_degraded(monkeypatch):
    # The gap this PR closes: a broker single-write naming an agent owned by a
    # different install is degraded to broker:<install>, not mis-attributed.
    agent_id = await _drive_single(
        monkeypatch,
        agent_id="victim-agent",
        auth=_broker_auth("install-1"),
        gate_owner="install-2",
        created_owner="install-1",
    )
    assert agent_id == "broker:install-1"


async def test_single_write_non_broker_passthrough(monkeypatch):
    agent_id = await _drive_single(
        monkeypatch,
        agent_id="dash-agent",
        auth=_broker_auth(None, is_install=False),
        gate_owner=None,
        created_owner=None,
    )
    assert agent_id == "dash-agent"


async def test_bulk_write_broker_foreign_agent_degraded(monkeypatch):
    # Same boundary still holds on the bulk path via the shared helper.
    agent_id = await _drive_bulk(
        monkeypatch,
        agent_id="victim-agent",
        auth=_broker_auth("install-1"),
        gate_owner="install-2",
        created_owner="install-1",
    )
    assert agent_id == "broker:install-1"
