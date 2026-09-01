"""MCP payload shape: capped ``top_k`` is visible, and lists name their rows ``items``.

Two findings from the REST/MCP parity smoke, both instances of a surface doing
something the caller cannot observe.

**Truncation.** ``top_k`` above ``MAX_SEARCH_TOP_K`` is a 422 on REST
``/search`` (``schemas.py``, ``le=MAX_SEARCH_TOP_K``) and a silent cap on
``caura_recall``. Same limit, opposite contract. Silent capping is the right
default for an agent surface — the report is explicit that MCP should not be
"fixed" by making it throw — but with no error, no flag and no total, a client
that asked for 40 and got 20 cannot tell a capped page from an exhausted
result set, and concludes the tenant holds 20 matching memories. The tool
description documents the cap; a description is not a runtime signal.

**Envelope.** REST ``MemoryList`` names its rows ``items``; ``caura_list``
named the same list ``results``, so no client could share a response parser
across the two surfaces. ``caura_recall`` already dual-emits both keys (C31/D1);
this extends the same permanent alias to ``caura_list``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core_api import mcp_server
from core_api.constants import MAX_SEARCH_TOP_K
from tests._mcp_test_helpers import parse_envelope, stub_storage_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _MemoryStub:
    def __init__(self, mid: str):
        self.mid = mid

    def model_dump(self, mode: str = "python"):
        return {"id": self.mid}


class _FakeConfig:
    recall_boost = False
    graph_expand = False
    entity_retrieval = True


async def _fake_resolve_config(tenant_id):
    return _FakeConfig()


def _wire_recall(monkeypatch):
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    return stub_storage_client(monkeypatch, get_agent=None)


def _out_stub(mid: str):
    class _Out:
        def model_dump(self, mode="python"):
            return {"id": mid}

    return _Out()


# ---------------------------------------------------------------------------
# Truncation is reported as data, not only as prose
# ---------------------------------------------------------------------------


async def test_recall_over_cap_reports_structured_truncation(mcp_env, monkeypatch):
    """The divergence the smoke pinned: 40 requested, 20 served, and now it says so."""
    requested = MAX_SEARCH_TOP_K + 20
    search_mock = mcp_env["service"]("search_memories")
    search_mock.return_value = [_MemoryStub(f"m-{i}") for i in range(MAX_SEARCH_TOP_K)]
    _wire_recall(monkeypatch)

    payload = parse_envelope(await mcp_server.caura_recall(query="x", top_k=requested))

    assert payload["truncated"] is True
    assert payload["requested_top_k"] == requested
    assert payload["effective_top_k"] == MAX_SEARCH_TOP_K
    # The cap the service was actually asked for, not just what we reported.
    assert search_mock.await_args.kwargs["top_k"] == MAX_SEARCH_TOP_K


async def test_recall_within_cap_reports_not_truncated(mcp_env, monkeypatch):
    """The contrast case — without it, a hardcoded ``truncated: true`` would pass."""
    mcp_env["service"]("search_memories").return_value = [_MemoryStub("m-1")]
    _wire_recall(monkeypatch)

    payload = parse_envelope(await mcp_server.caura_recall(query="x", top_k=5))

    assert payload["truncated"] is False
    assert payload["requested_top_k"] == 5
    assert payload["effective_top_k"] == 5


async def test_truncation_fields_are_always_present(mcp_env, monkeypatch):
    """Always emitted, so absence never has to be read as false.

    ``caura_keystones`` already returns ``truncated`` unconditionally; this is
    the same shape. A flag that appears only in the truncated case is one a
    client can only interpret correctly if it already knows the contract.
    """
    mcp_env["service"]("search_memories").return_value = []
    _wire_recall(monkeypatch)

    payload = parse_envelope(await mcp_server.caura_recall(query="x"))

    assert {"truncated", "requested_top_k", "effective_top_k"} <= payload.keys()


async def test_recall_over_cap_keeps_the_human_warning(mcp_env, monkeypatch):
    """The prose stays for chat rendering — the structured fields are additive.

    Dropping a field an existing client may read would be a breaking change
    for no gain.
    """
    mcp_env["service"]("search_memories").return_value = []
    _wire_recall(monkeypatch)

    payload = parse_envelope(await mcp_server.caura_recall(query="x", top_k=1000))

    assert (
        f"capped at the maximum allowed value of {MAX_SEARCH_TOP_K}"
        in payload["warning"]
    )


# ---------------------------------------------------------------------------
# ``caura_list`` names its rows both ways
# ---------------------------------------------------------------------------


async def test_list_dual_emits_items_and_results(mcp_env, monkeypatch):
    """Both keys, same rows — a REST-shaped parser reads ``items`` and works."""
    rows = [
        {"id": str(uuid4()), "created_at": datetime.now(UTC).isoformat()}
        for _ in range(2)
    ]
    stub_storage_client(monkeypatch, list_memories_by_filters=rows)
    monkeypatch.setattr(mcp_server, "_memory_to_out", lambda m: _out_stub(m["id"]))

    payload = parse_envelope(await mcp_server.caura_list(limit=5))

    assert payload["items"] == payload["results"]
    assert len(payload["items"]) == 2
    assert payload["count"] == 2


async def test_list_items_present_when_empty(mcp_env, monkeypatch):
    """An empty page still carries both keys, so a parser never KeyErrors."""
    stub_storage_client(monkeypatch, list_memories_by_filters=[])

    payload = parse_envelope(await mcp_server.caura_list())

    assert payload["items"] == []
    assert payload["results"] == []


async def test_list_items_reflects_the_served_slice(mcp_env, monkeypatch):
    """``items`` is the paged slice, not the over-fetched row set.

    ``caura_list`` requests ``limit + 1`` rows to detect ``has_more``. A naive
    alias bound to the raw storage rows would leak the probe row into ``items``
    while ``results`` stayed correct — and the two keys would disagree.
    """
    rows = [
        {"id": str(uuid4()), "created_at": datetime.now(UTC).isoformat()}
        for _ in range(3)
    ]
    stub_storage_client(monkeypatch, list_memories_by_filters=rows)
    monkeypatch.setattr(mcp_server, "_memory_to_out", lambda m: _out_stub(m["id"]))

    payload = parse_envelope(await mcp_server.caura_list(limit=2))

    assert len(payload["items"]) == 2
    assert payload["items"] == payload["results"]
    assert payload["next_cursor"] is not None
