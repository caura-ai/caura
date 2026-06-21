"""Unit tests for ``memclaw_recall`` (replaces the prior search + brief).

Covers:
- Happy path with results (no brief).
- ``include_brief=True`` merges the summary into the response.
- Empty results yield an empty array (not "No memories found.").
- Input validation (invalid memory_type / status → 422).
- ``top_k`` is capped at ``MAX_SEARCH_TOP_K``.
- ``HTTPException`` from the service → ``Error (…)`` envelope.
- Auth failure short-circuits.

Fix 2 Phase 4: ``memclaw_recall`` routes its DB access through the storage
client (``sc.get_agent`` replaces ``agent_repo.get_by_id``) and resolves
``resolve_config`` / ``summarize_memories`` via top-level imports bound on the
``mcp_server`` module, so tests patch those names on ``mcp_server`` and stub the
storage client's ``get_agent`` rather than the legacy repo path. No
``_mcp_session`` is opened on this path anymore.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from tests._mcp_test_helpers import as_text, parse_envelope, stub_storage_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _MemoryStub:
    """Minimal stand-in for a search result Pydantic model."""

    def __init__(self, mid: str, score: float = 0.9):
        self.mid = mid
        self.score = score

    def model_dump(self, mode: str = "python"):  # noqa: ARG002
        return {"id": self.mid, "score": self.score}


def _wire_recall_deps(monkeypatch):
    """Patch the storage-routed deps recall now resolves on ``mcp_server``.

    - ``resolve_config`` (top-level import on mcp_server) → fake config.
    - storage client ``get_agent`` → None (no agent profile / fleet scope).
    """
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    return stub_storage_client(monkeypatch, get_agent=None)


async def test_recall_happy_path(mcp_env, monkeypatch):
    """Standard query returns JSON with `results` and no `brief`."""
    search_mock = mcp_env["service"]("search_memories")
    search_mock.return_value = [_MemoryStub("m-1"), _MemoryStub("m-2")]

    _wire_recall_deps(monkeypatch)

    out = await mcp_server.memclaw_recall(query="what do I know about onboarding?")
    payload = parse_envelope(out)
    assert "results" in payload
    assert len(payload["results"]) == 2
    assert "brief" not in payload
    search_mock.assert_called_once()


async def test_recall_with_include_brief(mcp_env, monkeypatch):
    """`include_brief=True` runs the brief call and merges it.

    Patch ``summarize_memories`` (the LLM-only helper) on ``mcp_server`` — it's
    a top-level import there, so patching the source module would not rebind the
    name the handler calls.
    """
    mcp_env["service"]("search_memories").return_value = [_MemoryStub("m-1")]

    brief_mock = _fake_async_return({"summary": "alice onboarded last quarter"})
    monkeypatch.setattr(mcp_server, "summarize_memories", brief_mock)
    _wire_recall_deps(monkeypatch)

    out = await mcp_server.memclaw_recall(query="status?", include_brief=True)
    payload = parse_envelope(out)
    assert "brief" in payload
    assert payload["brief"]["summary"].startswith("alice")


async def test_recall_empty_results(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").return_value = []
    _wire_recall_deps(monkeypatch)

    out = await mcp_server.memclaw_recall(query="nothing matches")
    payload = parse_envelope(out)
    assert payload["results"] == []


async def test_recall_invalid_memory_type_returns_422(mcp_env):
    out = await mcp_server.memclaw_recall(query="x", memory_type="garbage")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid memory_type 'garbage'" in as_text(out)


async def test_recall_invalid_status_returns_422(mcp_env):
    out = await mcp_server.memclaw_recall(query="x", status="badstatus")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid status 'badstatus'" in as_text(out)


async def test_recall_top_k_is_capped(mcp_env, monkeypatch):
    """Passing top_k > MAX_SEARCH_TOP_K passes the cap to the service, not the raw value."""
    from core_api.constants import MAX_SEARCH_TOP_K

    search_mock = mcp_env["service"]("search_memories")
    search_mock.return_value = []
    _wire_recall_deps(monkeypatch)

    await mcp_server.memclaw_recall(query="x", top_k=1000)
    kwargs = search_mock.await_args.kwargs
    assert kwargs["top_k"] == MAX_SEARCH_TOP_K


async def test_recall_http_exception_becomes_error_envelope(mcp_env, monkeypatch):
    """Service raises HTTPException → handler returns `Error (status): detail`."""
    mcp_env["service"]("search_memories").side_effect = HTTPException(
        status_code=429, detail="rate limited"
    )
    _wire_recall_deps(monkeypatch)

    out = await mcp_server.memclaw_recall(query="x")
    assert "RATE_LIMITED" in as_text(out)
    assert "rate limited" in as_text(out)


async def test_recall_auth_failure_shortcircuits(monkeypatch):
    """Auth failure skips the handler body entirely."""
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)

    out = await mcp_server.memclaw_recall(query="x")
    assert out == mcp_server._AUTH_ERROR


async def test_recall_brief_runs_after_search_no_db_held(mcp_env, monkeypatch):
    """Audit P3 (post-Fix-2-Phase-4): recall no longer opens ``_mcp_session``,
    so a pooled DB connection can never be pinned across the multi-second LLM
    brief. We assert the structural invariant directly: the brief runs strictly
    AFTER ``search_memories`` returns, and ``_mcp_session`` is never entered."""
    import contextlib
    import time as _time

    search_returned_at: dict = {"t": None}
    brief_started_at: dict = {"t": None}
    session_opened: dict = {"v": False}

    async def _tracking_search(*_args, **_kwargs):
        search_returned_at["t"] = _time.perf_counter()
        return [_MemoryStub("m-1")]

    async def _tracking_summarize(*_args, **_kwargs):
        brief_started_at["t"] = _time.perf_counter()
        return {"summary": "anything"}

    @contextlib.asynccontextmanager
    async def _tracking_session():
        session_opened["v"] = True
        yield None

    monkeypatch.setattr(mcp_server, "search_memories", _tracking_search)
    monkeypatch.setattr(mcp_server, "summarize_memories", _tracking_summarize)
    monkeypatch.setattr(mcp_server, "_mcp_session", _tracking_session)
    _wire_recall_deps(monkeypatch)

    await mcp_server.memclaw_recall(query="x", include_brief=True)

    assert search_returned_at["t"] is not None, "search never ran"
    assert brief_started_at["t"] is not None, "brief never ran"
    assert brief_started_at["t"] >= search_returned_at["t"], (
        "brief LLM call started before search returned"
    )
    assert session_opened["v"] is False, (
        "recall must not open _mcp_session post-Phase-4"
    )


async def test_recall_brief_skipped_when_include_brief_false(mcp_env, monkeypatch):
    """When ``include_brief=False`` (default), ``summarize_memories``
    must NOT be invoked at all — no LLM cost on the bare-recall path."""
    mcp_env["service"]("search_memories").return_value = [_MemoryStub("m-1")]
    calls: list = []

    async def _spy(*args, **kwargs):  # noqa: ARG001
        calls.append(1)
        return {"summary": "should not run"}

    monkeypatch.setattr(mcp_server, "summarize_memories", _spy)
    _wire_recall_deps(monkeypatch)

    out = await mcp_server.memclaw_recall(query="x")
    payload = parse_envelope(out)
    assert calls == []
    assert "brief" not in payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConfig:
    recall_boost = False
    graph_expand = False


async def _fake_resolve_config(db, tenant_id):  # noqa: ARG001
    return _FakeConfig()


async def _async_none(*args, **kwargs):  # noqa: ARG001
    return None


def _fake_async_return(value):
    async def _fn(*args, **kwargs):  # noqa: ARG001
        return value

    return _fn
