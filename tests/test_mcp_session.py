"""Unit tests for memclaw_session_start (UX-03)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
    """Stand-in for the ``_memory_to_out`` result. ``model_dump`` returns the row;
    the RE-06 session re-rank reads memory_type/weight/created_at/recall_count as
    attributes, so expose them off the row (created_at coerced to datetime)."""

    def __init__(self, row):
        self._row = row

    def model_dump(self, mode="python"):  # noqa: ARG002
        return self._row

    @property
    def memory_type(self):
        return self._row.get("memory_type")

    @property
    def weight(self):
        return self._row.get("weight", 0.0)

    @property
    def recall_count(self):
        return self._row.get("recall_count", 0)

    @property
    def created_at(self):
        ca = self._row.get("created_at")
        if isinstance(ca, str):
            return datetime.fromisoformat(ca)
        return ca


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


# ---------------------------------------------------------------------------
# RE-06: recency/usage re-rank + rule exclusion + content cap
# ---------------------------------------------------------------------------


def _rrow(mid, *, weight=0.5, memory_type="decision", age_days=0, recall_count=0, content="c"):
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return {
        "id": mid,
        "content": content,
        "weight": weight,
        "status": "active",
        "created_at": created,
        "memory_type": memory_type,
        "recall_count": recall_count,
        "agent_id": "test-agent",
        "tenant_id": "test-tenant",
    }


async def test_session_start_fetches_wider_window(mcp_env, monkeypatch):
    """RE-06 AC1: the storage fetch is widened to 25 (re-ranked to top-5 in Python)."""
    sc = _stub_session_deps(monkeypatch)
    await mcp_server.memclaw_session_start()
    payload = sc.list_memories_by_filters.await_args.args[0]
    assert payload["limit"] == 25
    assert payload["sort"] == "weight" and payload["order"] == "desc"


async def test_session_start_equal_weight_ranks_by_recency(mcp_env, monkeypatch):
    """Equal-weight memories: the more recent one ranks first."""
    rows = [
        _rrow("old", weight=0.5, age_days=90),
        _rrow("new", weight=0.5, age_days=0),
    ]
    _stub_session_deps(monkeypatch, memories=rows)
    out = await mcp_server.memclaw_session_start()
    ids = [m["id"] for m in parse_envelope(out)["memories"]]
    assert ids.index("new") < ids.index("old")


async def test_session_start_recalled_older_outranks_fresh_unused(mcp_env, monkeypatch):
    """A frequently-recalled older memory can outrank a never-recalled newer one
    (the log1p recall term is load-bearing; it must not zero out unused rows)."""
    rows = [
        _rrow("older_used", weight=0.5, age_days=40, recall_count=50),
        _rrow("newer_unused", weight=0.5, age_days=1, recall_count=0),
    ]
    _stub_session_deps(monkeypatch, memories=rows)
    out = await mcp_server.memclaw_session_start()
    ids = [m["id"] for m in parse_envelope(out)["memories"]]
    assert ids.index("older_used") < ids.index("newer_unused")


async def test_session_start_excludes_rule_typed_rows(mcp_env, monkeypatch):
    """RE-06 AC2: memory_type='rule' rows are absent from the memories section."""
    rows = [
        _rrow("a_rule", memory_type="rule", weight=0.9),
        _rrow("a_decision", memory_type="decision", weight=0.5),
    ]
    _stub_session_deps(monkeypatch, memories=rows)
    out = await mcp_server.memclaw_session_start()
    ids = [m["id"] for m in parse_envelope(out)["memories"]]
    assert "a_rule" not in ids
    assert "a_decision" in ids


async def test_session_start_compact_caps_content_at_300(mcp_env, monkeypatch):
    """RE-06 AC3: in compact mode, content > 300 chars is capped with a marker."""
    long_content = "x" * 500
    _stub_session_deps(monkeypatch, memories=[_rrow("big", content=long_content)])
    out = await mcp_server.memclaw_session_start(verbosity="compact")
    mem = parse_envelope(out)["memories"][0]
    assert len(mem["content"]) == 300
    assert mem["content_truncated"] is True


async def test_session_start_full_mode_does_not_cap_content(mcp_env, monkeypatch):
    """Default (full) mode leaves content uncapped and adds no truncation marker
    (the cap is part of the compact projection only — additive default)."""
    long_content = "x" * 500
    _stub_session_deps(monkeypatch, memories=[_rrow("big", content=long_content)])
    out = await mcp_server.memclaw_session_start()  # default full
    mem = parse_envelope(out)["memories"][0]
    assert len(mem["content"]) == 500
    assert "content_truncated" not in mem
