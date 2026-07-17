"""Broker ownership boundary on the /ingest/commit and /stm/promote routes.

Both routes attribute a memory under a caller-supplied ``agent_id`` (via
``create_memory`` / ``create_memories_bulk``) with no trust/registration gate —
so, like evolve/insights, an install-credential (broker) caller could name
another install's agent. The route handlers degrade a foreign / reserved
``broker:`` id to the caller's own ``broker:<install>`` fallback via
``broker_owned_agent_id`` before the write. Gate-only: these paths never
first-touch an agent, so there's no owner stamp (the gate function itself is
unit-tested in ``test_broker_owned_agent_id.py``).

Not broker-reachable by the current plugin client — this is defense-in-depth
completing the boundary across every broker-reachable memory-write surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api.auth import AuthContext
from core_api.routes import memories, stm
from core_api.schemas import IngestCommitRequest, IngestFact

pytestmark = pytest.mark.unit


def _broker_auth(
    install_uuid: str | None = "install-1", *, is_install: bool = True
) -> AuthContext:
    return AuthContext(
        tenant_id="tenant-1",
        is_install_credential=is_install,
        install_uuid=install_uuid,
    )


# ── /ingest/commit ──────────────────────────────────────────────────────


async def _drive_ingest(monkeypatch, *, agent_id, auth):
    gate = AsyncMock(return_value="broker:install-1")
    monkeypatch.setattr(memories, "broker_owned_agent_id", gate)
    monkeypatch.setattr(memories, "check_and_increment", AsyncMock(return_value=None))
    captured: dict[str, str] = {}

    async def _ingest(body_):
        captured["agent_id"] = body_.agent_id
        return {"committed": 1}

    monkeypatch.setattr(memories, "ingest_commit", _ingest)
    body = IngestCommitRequest(
        tenant_id="tenant-1",
        agent_id=agent_id,
        facts=[IngestFact(content="a durable fact", suggested_type="fact")],
    )
    await memories.ingest_commit_endpoint(SimpleNamespace(), body, auth)
    return captured["agent_id"], gate


async def test_ingest_commit_broker_foreign_agent_degraded(monkeypatch):
    agent_id, gate = await _drive_ingest(
        monkeypatch, agent_id="victim-agent", auth=_broker_auth("install-1")
    )
    assert agent_id == "broker:install-1"
    assert gate.await_args.args == ("victim-agent", "install-1", "tenant-1")


async def test_ingest_commit_non_broker_not_degraded(monkeypatch):
    agent_id, gate = await _drive_ingest(
        monkeypatch, agent_id="dash-agent", auth=_broker_auth(None, is_install=False)
    )
    assert agent_id == "dash-agent"
    gate.assert_not_awaited()


# ── /stm/promote ────────────────────────────────────────────────────────


async def _drive_stm(monkeypatch, *, agent_id, auth):
    monkeypatch.setattr(stm, "_check_stm_enabled", lambda: None)
    gate = AsyncMock(return_value="broker:install-1")
    monkeypatch.setattr(stm, "broker_owned_agent_id", gate)
    captured: dict[str, str] = {}

    async def _promote(
        *, content, tenant_id, agent_id, fleet_id, memory_type, visibility
    ):
        captured["agent_id"] = agent_id
        return {"id": "m-1"}

    monkeypatch.setattr("core_api.services.stm_service.promote", _promote)
    body = stm.PromoteRequest(agent_id=agent_id, content="a durable note")
    await stm.promote_stm(body, auth)
    return captured["agent_id"], gate


async def test_stm_promote_broker_foreign_agent_degraded(monkeypatch):
    agent_id, gate = await _drive_stm(
        monkeypatch, agent_id="victim-agent", auth=_broker_auth("install-1")
    )
    assert agent_id == "broker:install-1"
    assert gate.await_args.args == ("victim-agent", "install-1", "tenant-1")


async def test_stm_promote_non_broker_not_degraded(monkeypatch):
    agent_id, gate = await _drive_stm(
        monkeypatch, agent_id="dash-agent", auth=_broker_auth(None, is_install=False)
    )
    assert agent_id == "dash-agent"
    gate.assert_not_awaited()
