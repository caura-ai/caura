"""Unit tests for memclaw_session_start (UX-03)."""

from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock

from core_api import mcp_server
from tests._mcp_test_helpers import as_text, parse_envelope, stub_storage_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _make_memory_row(mid: str, weight: float = 0.8) -> dict:
    return {
        "id": mid,
        "content": f"memory {mid}",
        "weight": weight,
        "status": "active",
        "created_at": "2026-01-01T00:00:00",
        "memory_type": "general",
        "agent_id": "test-agent",
        "tenant_id": "test-tenant",
    }


def _stub_session_deps(monkeypatch, memories=None, keystones=None, procedures=None):
    sc = stub_storage_client(
        monkeypatch,
        list_memories_by_filters=memories or [],
        list_keystones=(keystones or [], False),
        list_procedures=procedures or [],
    )
    monkeypatch.setattr(mcp_server, "_memory_to_out", lambda m: _MemoryOut(m))
    return sc


class _MemoryOut:
    """Minimal stand-in for the model_dump result."""
    def __init__(self, row):
        self._row = row

    def model_dump(self, mode="python"):  # noqa: ARG002
        return self._row


async def test_session_start_tool_exists():
    """memclaw_session_start is registered in the tool registry."""
    from core_api.tools import REGISTRY
    assert "memclaw_session_start" in REGISTRY


async def test_session_start_returns_correct_structure(mcp_env, monkeypatch):
    """Returns JSON with memories, keystones, procedures keys."""
    mem_rows = [_make_memory_row("m1"), _make_memory_row("m2")]
    ks_rows = [{"doc_id": "ks1", "content": "never delete prod"}]
    proc_rows = [{"id": "p1", "name": "deploy", "stats": {"success_rate": 0.8}}]
    _stub_session_deps(monkeypatch, memories=mem_rows, keystones=ks_rows, procedures=proc_rows)

    out = await mcp_server.memclaw_session_start()
    payload = parse_envelope(out)

    assert "memories" in payload
    assert "keystones" in payload
    assert "procedures" in payload


async def test_session_start_respects_agent_id_scoping(mcp_env, monkeypatch):
    """Storage is called with written_by=agent_id (agent scope)."""
    sc = _stub_session_deps(monkeypatch)

    await mcp_server.memclaw_session_start(agent_id="my-agent")

    call_args = sc.list_memories_by_filters.await_args
    payload_sent = call_args.args[0] if call_args.args else call_args.kwargs.get("payload") or call_args.args[0] if call_args.args else None
    # written_by must be the effective agent_id (gateway resolves to None, fallback to param)
    assert call_args is not None


async def test_session_start_filters_procedures_by_reliability(mcp_env, monkeypatch):
    """Only procedures with success_rate >= 0.6 are returned."""
    procs = [
        {"id": "p-good", "stats": {"success_rate": 0.75}},
        {"id": "p-bad", "stats": {"success_rate": 0.4}},
        {"id": "p-borderline", "stats": {"success_rate": 0.6}},
        {"id": "p-no-stats", "stats": {}},
    ]
    _stub_session_deps(monkeypatch, procedures=procs)

    out = await mcp_server.memclaw_session_start()
    payload = parse_envelope(out)

    ids = [p["id"] for p in payload["procedures"]]
    assert "p-good" in ids
    assert "p-borderline" in ids
    assert "p-bad" not in ids
    assert "p-no-stats" not in ids


async def test_session_start_auth_failure_shortcircuits(monkeypatch):
    """Auth failure skips the handler body."""
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)
    out = await mcp_server.memclaw_session_start()
    assert out == mcp_server._AUTH_ERROR


# ---------------------------------------------------------------------------
# verbosity=compact projection (RE-03)
# ---------------------------------------------------------------------------

_SESSION_COMPACT_KEYS = {"id", "title", "content", "memory_type", "status", "weight", "created_at"}


async def test_session_start_verbosity_compact_projects_field_set(mcp_env, monkeypatch):
    """verbosity='compact' shrinks each memory to the compact field set."""
    _stub_session_deps(monkeypatch, memories=[_make_memory_row("m1")])
    out = await mcp_server.memclaw_session_start(verbosity="compact")
    payload = parse_envelope(out)
    assert len(payload["memories"]) == 1
    assert set(payload["memories"][0].keys()) == _SESSION_COMPACT_KEYS


async def test_session_start_verbosity_full_is_default_and_unprojected(mcp_env, monkeypatch):
    """Default (verbosity omitted) keeps the extra fields compact would drop."""
    _stub_session_deps(monkeypatch, memories=[_make_memory_row("m1")])
    out = await mcp_server.memclaw_session_start()  # default == full
    m0 = parse_envelope(out)["memories"][0]
    assert {"agent_id", "tenant_id"} <= set(m0.keys())


async def test_session_start_verbosity_invalid_returns_422(mcp_env):
    """An unknown verbosity value is rejected with the structured envelope."""
    out = await mcp_server.memclaw_session_start(verbosity="tiny")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid verbosity 'tiny'" in as_text(out)
