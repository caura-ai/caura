"""Audit P3 regression test for ``memclaw_insights``.

The handler previously held a single ``_mcp_session()`` open across
the multi-second LLM round-trip in ``_run_llm_analysis``, pinning a
pooled DB connection. The refactor splits the work into three phases:

  1. Session 1 — trust + usage gates, query memories, resolve config.
  2. No DB     — ``synthesize_insights`` (LLM-only).
  3. Session 2 — ``_persist_findings`` + commit.

This module asserts the timing invariant with patched
``_mcp_session`` (capture enter/exit timestamps) and patched
``synthesize_insights`` (capture entry timestamp). A regression that
moves the LLM call back inside the session would flip the order
and fail.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api import mcp_server

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_insights_closes_first_session_before_llm(mcp_env, monkeypatch):
    """Phase 1 session must close BEFORE ``synthesize_insights`` runs.

    Captures interleaved enter/exit events on every ``_mcp_session``
    entry plus a sentinel when the LLM helper fires. The expected
    event order is:

        session-enter (phase 1)
        session-exit  (phase 1)
        llm-start
        session-enter (phase 3)
        session-exit  (phase 3)

    If a future change re-merges phases 1+2, the order flips and the
    assertion fails.
    """
    events: list[str] = []

    @asynccontextmanager
    async def _captured_session():
        events.append("session-enter")
        try:
            yield MagicMock(name="db")
        finally:
            events.append("session-exit")

    monkeypatch.setattr(mcp_server, "_mcp_session", _captured_session)

    # Phase-1 collaborators
    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None))
    )
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock())

    # Phase-1 dispatch — return one fake "memory" with the .id attribute
    # the format helpers expect. The downstream synthesize_insights is
    # patched, so the format helpers never actually run on this stub.
    fake_memory = SimpleNamespace(
        id="m1",
        content="x",
        memory_type="fact",
        title=None,
        status="active",
        ts_valid_start=None,
    )
    fake_query = AsyncMock(return_value=[fake_memory])
    monkeypatch.setattr(
        "core_api.services.insights_service._QUERY_DISPATCH",
        {
            "contradictions": fake_query,
            "patterns": fake_query,
            "discover": fake_query,
            "failures": fake_query,
            "stale": fake_query,
            "divergence": fake_query,
        },
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace()),
    )

    # The LLM helper — record entry timestamp via the events list.
    async def _capturing_synthesize(*_args, **_kwargs):
        events.append("llm-start")
        return {
            "findings": [],
            "summary": "synthesized in test",
            "memories_analyzed": 1,
        }

    monkeypatch.setattr(
        "core_api.services.insights_service.synthesize_insights",
        _capturing_synthesize,
    )

    # Phase-3 persistence — return an empty list of ids.
    monkeypatch.setattr(
        "core_api.services.insights_service._persist_findings",
        AsyncMock(return_value=[]),
    )

    await mcp_server.memclaw_insights(
        focus="contradictions", scope="agent", agent_id="a1"
    )

    # Locate the events. We expect the LLM start strictly between two
    # session entries (phase 1 closed, phase 3 not yet open).
    assert "llm-start" in events, "LLM helper never ran"
    llm_idx = events.index("llm-start")
    # The most recent "session-exit" before the LLM corresponds to phase 1.
    prior_exits = [i for i, e in enumerate(events[:llm_idx]) if e == "session-exit"]
    assert prior_exits, "no session closed before the LLM call — P3 fix regressed"
    # The next "session-enter" after the LLM is phase 3.
    next_enters = [
        i
        for i, e in enumerate(events[llm_idx + 1 :], start=llm_idx + 1)
        if e == "session-enter"
    ]
    assert next_enters, (
        "no second session opened after LLM — persistence phase missing or merged"
    )


async def test_insights_short_circuits_when_no_memories(mcp_env, monkeypatch):
    """When the query returns zero rows, the handler must NOT open a
    second session and must NOT invoke ``synthesize_insights``. The
    empty-result fast path lives entirely in phase 1."""
    session_entries: list[int] = []

    @asynccontextmanager
    async def _counting_session():
        session_entries.append(1)
        try:
            yield MagicMock(name="db")
        finally:
            pass

    monkeypatch.setattr(mcp_server, "_mcp_session", _counting_session)
    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None))
    )
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock())

    empty_query = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "core_api.services.insights_service._QUERY_DISPATCH",
        {
            "contradictions": empty_query,
            "patterns": empty_query,
            "discover": empty_query,
            "failures": empty_query,
            "stale": empty_query,
            "divergence": empty_query,
        },
    )

    synth_calls: list = []

    async def _spy(*_a, **_kw):
        synth_calls.append(1)
        return {"findings": [], "summary": "", "memories_analyzed": 0}

    monkeypatch.setattr("core_api.services.insights_service.synthesize_insights", _spy)

    await mcp_server.memclaw_insights(
        focus="contradictions", scope="agent", agent_id="a1"
    )

    # Exactly one session opened (phase 1). No LLM invocation.
    assert sum(session_entries) == 1
    assert synth_calls == []
