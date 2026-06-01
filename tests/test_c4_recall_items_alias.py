"""C4: ``/recall`` response shape — add ``items`` as an alias of ``memories``.

Two existing API surfaces return search results but use different top-
level key names:

* ``/search`` returns ``{"items": [...], ...}``
* ``/recall`` returns ``{"memories": [...], ...}``

The C4 fix adds an ``items`` key to the recall response that aliases the
same list ``memories`` points at. Consumers built against ``/search``'s
shape can now read either key and get the same data.

The canonical source of the recall summary dict is
``core_api.services.recall_service.summarize_memories``. The REST
``/api/v1/recall`` endpoint wraps it directly; the MCP
``memclaw_recall(include_brief=True)`` tool surfaces it as
``payload["brief"]``. After C4, every dict produced by
``summarize_memories`` carries both keys.

These tests pin the contract at all three layers:

1. Service: ``summarize_memories`` itself, across all three branches
   (empty input, recall disabled, normal LLM brief).
2. REST: ``POST /api/v1/recall`` surfaces the alias to the wire.
3. MCP: ``memclaw_recall(include_brief=True)`` surfaces it under
   ``brief``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api.services.recall_service import summarize_memories
from tests._mcp_test_helpers import parse_envelope
from tests.conftest import get_test_auth

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_memory(mid: str | None = None, content: str = "fact") -> MagicMock:
    """Lightweight stand-in for a ``MemoryOut`` (or a search result row).

    The recall summary path only consumes ``model_dump(mode="json")``
    on each element, so the rest of the ``MemoryOut`` surface can be
    cheap-stubbed via ``MagicMock`` attribute access.
    """
    m = MagicMock(name=f"mem-{mid or 'x'}")
    m.id = uuid.UUID(mid) if mid else uuid.uuid4()
    m.tenant_id = "tenant-A"
    m.agent_id = "agent-A"
    m.memory_type = "fact"
    m.content = content
    m.title = None
    m.status = "active"
    m.ts_valid_start = None
    m.created_at = datetime.now(timezone.utc)
    m.model_dump = MagicMock(
        return_value={"id": str(m.id), "content": content, "memory_type": "fact"}
    )
    return m


def _minimal_config(recall_enabled: bool = True) -> SimpleNamespace:
    """Cheapest object that exposes the attributes ``summarize_memories``
    reads off ``config``. Subset deliberately permissive — extra attrs
    won't hurt, missing ones might."""
    return SimpleNamespace(
        recall_enabled=recall_enabled,
        recall_provider="fake",
        recall_model="fake-model",
        recall_boost=False,
        graph_expand=False,
    )


def _fake_brief_text(*_args, **_kwargs):
    """Stub LLM call: return the ``summary`` string the helper would
    have synthesised. Patched into whichever module-level helper
    ``summarize_memories`` invokes to produce the brief."""
    return "synthesised brief"


_EXPECTED_TOP_LEVEL_KEYS = {
    "query",
    "summary",
    "memory_count",
    "memories",
    "items",
    "recall_ms",
}


# ---------------------------------------------------------------------------
# Service-level: ``summarize_memories`` directly
#
# The helper has three branches; each must carry ``items`` AND ``memories``
# with equal content.
# ---------------------------------------------------------------------------


async def _call_summarize(memories, *, recall_enabled, monkeypatch):
    """Invoke ``summarize_memories`` with the brief LLM call stubbed.

    The brief branch fires only when ``recall_enabled=True`` AND
    ``memories`` is non-empty. We patch every plausible LLM entry-point
    on ``core_api.services.recall_service`` so the path is hermetic
    regardless of which helper the implementation actually delegates
    to. ``raising=False`` because not all names exist on every revision.
    """
    import core_api.services.recall_service as rs_mod

    for name in (
        "_call_llm",
        "_summarize",
        "_synthesise_brief",
        "_llm_brief",
        "_recall_brief",
        "_brief",
        "openai_complete",
        "complete_with_provider",
        "_complete",
    ):
        monkeypatch.setattr(
            rs_mod, name, AsyncMock(return_value="synthesised brief"), raising=False
        )

    config = _minimal_config(recall_enabled=recall_enabled)
    return await summarize_memories(memories, "what do I know?", config, top_k=5)


@pytest.mark.parametrize("recall_enabled", [True, False])
async def test_empty_input_both_keys_present_and_equal_empty(
    recall_enabled, monkeypatch
):
    """Empty-input branch: both keys present, both empty, regardless of
    ``recall_enabled``. ``memory_count`` is zero."""
    result = await _call_summarize(
        [], recall_enabled=recall_enabled, monkeypatch=monkeypatch
    )

    assert "memories" in result, "legacy `memories` key dropped"
    assert "items" in result, "C4: `items` alias missing on empty branch"
    assert result["memories"] == []
    assert result["items"] == []
    assert result["memory_count"] == 0
    assert "query" in result
    assert "summary" in result
    assert "recall_ms" in result


async def test_recall_disabled_branch_keys_equal(monkeypatch):
    """Recall-disabled branch: both keys present and equal in content.

    The helper returns the raw ``model_dump`` of each memory without
    invoking the LLM brief.
    """
    mems = [_fake_memory(content="alpha"), _fake_memory(content="beta")]
    result = await _call_summarize(mems, recall_enabled=False, monkeypatch=monkeypatch)

    assert "memories" in result and "items" in result
    assert len(result["memories"]) == 2
    assert len(result["items"]) == 2
    assert result["memories"] == result["items"], (
        "`items` must alias the same content `memories` carries"
    )
    assert result["memory_count"] == 2
    assert result["memory_count"] == len(result["memories"])
    assert result["memory_count"] == len(result["items"])


async def test_normal_brief_branch_keys_equal(monkeypatch):
    """Normal brief branch: ``recall_enabled=True`` + non-empty memories.

    The LLM call is stubbed; both keys must still be present and
    carry the same dumps.
    """
    mems = [_fake_memory(content="alpha"), _fake_memory(content="beta")]
    result = await _call_summarize(mems, recall_enabled=True, monkeypatch=monkeypatch)

    assert "memories" in result and "items" in result
    assert len(result["memories"]) == 2
    assert len(result["items"]) == 2
    assert result["memories"] == result["items"]
    assert result["memory_count"] == len(result["memories"])
    # Other expected keys still present — the alias must not displace them.
    assert _EXPECTED_TOP_LEVEL_KEYS.issubset(result.keys()), (
        f"missing keys: {_EXPECTED_TOP_LEVEL_KEYS - result.keys()}"
    )


async def test_items_and_memories_have_equal_elements(monkeypatch):
    """The two keys must yield equal elements (content-equality, not
    identity — leaves room for future memoisation or list-copying)."""
    mems = [_fake_memory(content="alpha"), _fake_memory(content="beta")]
    result = await _call_summarize(mems, recall_enabled=True, monkeypatch=monkeypatch)

    assert result["items"] == result["memories"]
    # element-wise sanity: each dict in `items` should deep-equal the
    # matching `memories` dict.
    for a, b in zip(result["items"], result["memories"], strict=True):
        assert a == b


@pytest.mark.parametrize(
    "recall_enabled,memories_factory",
    [
        (True, lambda: []),
        (False, lambda: [_fake_memory(content="x"), _fake_memory(content="y")]),
        (True, lambda: [_fake_memory(content="x"), _fake_memory(content="y")]),
    ],
    ids=["empty", "recall-disabled", "normal-brief"],
)
async def test_unchanged_surface_other_keys_still_present(
    recall_enabled, memories_factory, monkeypatch
):
    """Every branch keeps the existing top-level keys (``query``,
    ``summary``, ``memory_count``, ``recall_ms``) alongside the new alias."""
    result = await _call_summarize(
        memories_factory(), recall_enabled=recall_enabled, monkeypatch=monkeypatch
    )
    assert _EXPECTED_TOP_LEVEL_KEYS.issubset(result.keys()), (
        f"missing keys for branch ({recall_enabled},{len(memories_factory())}): "
        f"{_EXPECTED_TOP_LEVEL_KEYS - result.keys()}"
    )
    # ``memory_count`` matches both list lengths.
    assert result["memory_count"] == len(result["memories"])
    assert result["memory_count"] == len(result["items"])


# ---------------------------------------------------------------------------
# REST: POST /api/v1/recall surfaces both keys to the wire
# ---------------------------------------------------------------------------


async def test_rest_recall_response_carries_both_memories_and_items(
    client, monkeypatch
):
    """Wire-level smoke: POST /api/v1/recall returns JSON with BOTH
    ``memories`` and ``items``.

    We mock ``search_memories`` to return zero results (no embedding
    match required) but let the real ``summarize_memories`` run — that
    way the alias has to come out of the actual helper, not from a
    test stub. The empty branch deterministically produces both keys
    without needing the LLM provider.
    """
    monkeypatch.setattr(
        "core_api.services.memory_service.search_memories",
        AsyncMock(return_value=[]),
    )

    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": "capital of france", "top_k": 5},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "memories" in body, "legacy `memories` key dropped from REST response"
    assert "items" in body, "C4: `items` alias missing from REST response"
    assert body["memories"] == body["items"]
    assert body["memories"] == []
    assert body["memory_count"] == 0
    assert body["memory_count"] == len(body["memories"]) == len(body["items"])


# ---------------------------------------------------------------------------
# MCP: memclaw_recall(include_brief=True) surfaces the alias under `brief`
# ---------------------------------------------------------------------------


async def test_mcp_recall_brief_contains_both_keys(mcp_env, monkeypatch):
    """``memclaw_recall(include_brief=True)`` wraps the
    ``summarize_memories`` dict at ``payload["brief"]``. After C4 that
    dict carries both ``memories`` and ``items``.

    We mock ``search_memories`` to return zero results so the REAL
    ``summarize_memories`` empty-branch runs end-to-end — no LLM round-
    trip, no provider config required, and the alias has to come from
    the actual helper (not a test stub)."""
    mcp_env["service"]("search_memories").return_value = []
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=_minimal_config(recall_enabled=True)),
    )
    monkeypatch.setattr(
        "core_api.repositories.agent_repo.get_by_id", AsyncMock(return_value=None)
    )

    from core_api import mcp_server

    out = await mcp_server.memclaw_recall(query="status?", include_brief=True)
    payload = parse_envelope(out)

    assert "brief" in payload, "MCP recall did not surface a brief"
    brief = payload["brief"]
    assert "memories" in brief, "legacy `memories` key missing from MCP brief"
    assert "items" in brief, "C4: `items` alias missing from MCP brief"
    assert brief["items"] == brief["memories"]
    assert brief["memory_count"] == len(brief["memories"]) == len(brief["items"])
