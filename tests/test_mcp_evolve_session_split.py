"""Audit P3 regression test for ``memclaw_evolve``.

The handler previously held a single ``_mcp_session()`` open across
the rule-generation LLM round-trip in ``_generate_rule``, pinning a
pooled DB connection. The refactor splits the work into three phases:

  1. Session 1 — trust + usage gates, ``_filter_by_scope``, resolve config.
  2. No DB     — ``_maybe_generate_rule`` (LLM).
  3. Session 2 — ``_apply_outcome_to_db`` (weights + persist + backfill + commit).

This module asserts the timing invariant with a patched
``_mcp_session`` capturing enter/exit events and a patched
``_maybe_generate_rule`` recording its entry. A regression that
re-merges phases 1+2 would flip the order and fail.
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api import mcp_server

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


_APPLY_OUTCOME_RESULT = {
    "outcome_id": "00000000-0000-0000-0000-000000000001",
    "outcome_type": "failure",
    "scope": "agent",
    "weight_adjustments": [],
    "rules_generated": [],
    "rule_skipped_reason": "below_confidence_threshold",
    "out_of_scope_count": 0,
    "weight_adjustment_skipped_reason": None,
    "evolve_ms": 1,
}


async def _run_evolve_capturing_apply_kwargs(monkeypatch, *, related_ids, filter_result):
    """Drive memclaw_evolve with mocked collaborators, returning the kwargs
    the handler passed to ``_apply_outcome_to_db``."""
    captured: dict = {}

    @asynccontextmanager
    async def _session():
        yield MagicMock(name="db")

    async def _spy_apply(_db, **kwargs):
        captured.update(kwargs)
        return _APPLY_OUTCOME_RESULT

    monkeypatch.setattr(mcp_server, "_mcp_session", _session)
    monkeypatch.setattr(mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None)))
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock())
    monkeypatch.setattr(
        "core_api.services.evolve_service._filter_by_scope",
        AsyncMock(return_value=filter_result),
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "core_api.services.evolve_service._maybe_generate_rule",
        AsyncMock(return_value=(None, "not_failure_or_partial")),
    )
    monkeypatch.setattr("core_api.services.evolve_service._apply_outcome_to_db", _spy_apply)

    await mcp_server.memclaw_evolve(
        outcome="a thing happened",
        outcome_type="failure",
        related_ids=related_ids,
        scope="agent",
        agent_id="a1",
    )
    return captured


async def test_evolve_passes_all_required_apply_outcome_kwargs(mcp_env, monkeypatch):
    """memclaw_evolve must pass every required kwarg of _apply_outcome_to_db.

    The handler patches _apply_outcome_to_db with a mock that swallows any
    signature, so a missing required kwarg (e.g. weight_adjustment_skipped_reason)
    only surfaces in prod as a TypeError. Bind the captured kwargs against the
    REAL signature so that class of bug fails here instead.
    """
    captured = await _run_evolve_capturing_apply_kwargs(
        monkeypatch,
        related_ids=["11111111-1111-1111-1111-111111111111"],
        filter_result=(["11111111-1111-1111-1111-111111111111"], 0),
    )

    real_sig = inspect.signature(
        __import__(
            "core_api.services.evolve_service", fromlist=["_apply_outcome_to_db"]
        )._apply_outcome_to_db
    )
    # Raises TypeError "missing a required argument" if any required kwarg
    # (incl. weight_adjustment_skipped_reason) is absent from the call.
    real_sig.bind(MagicMock(name="db"), **captured)
    assert "weight_adjustment_skipped_reason" in captured


async def test_evolve_weight_skip_reason_no_related_ids(mcp_env, monkeypatch):
    """Empty related_ids → 'no_related_ids' slug passed through (A15)."""
    captured = await _run_evolve_capturing_apply_kwargs(
        monkeypatch,
        related_ids=[],
        filter_result=([], 0),
    )
    assert captured["weight_adjustment_skipped_reason"] == "no_related_ids"


async def test_evolve_weight_skip_reason_all_out_of_scope(mcp_env, monkeypatch):
    """Scope filter drops every id → scope-mapped slug passed through (A15)."""
    captured = await _run_evolve_capturing_apply_kwargs(
        monkeypatch,
        related_ids=["11111111-1111-1111-1111-111111111111"],
        filter_result=([], 1),  # all dropped
    )
    assert captured["weight_adjustment_skipped_reason"] == "agent_id_mismatch"


async def test_evolve_closes_first_session_before_llm(mcp_env, monkeypatch):
    """Phase 1 session must close BEFORE ``_maybe_generate_rule`` runs.

    Expected event order:

        session-enter (phase 1)
        session-exit  (phase 1)
        llm-start
        session-enter (phase 3)
        session-exit  (phase 3)

    A future change that re-merges phases 1+2 flips this order and
    the assertion fails.
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
    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None))
    )
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock())

    # Phase-1 collaborators — return one in-scope id so the rule path
    # actually exercises the LLM step (skipped when related_ids empty).
    monkeypatch.setattr(
        "core_api.services.evolve_service._filter_by_scope",
        AsyncMock(return_value=(["11111111-1111-1111-1111-111111111111"], 0)),
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace()),
    )

    # Phase-2 LLM — record entry into the events list.
    async def _capturing_llm(*_a, **_kw):
        events.append("llm-start")
        return ({"condition": "x", "action": "y", "confidence": 0.5}, None)

    monkeypatch.setattr(
        "core_api.services.evolve_service._maybe_generate_rule", _capturing_llm
    )

    # Phase-3 commit — return a deterministic result.
    monkeypatch.setattr(
        "core_api.services.evolve_service._apply_outcome_to_db",
        AsyncMock(
            return_value={
                "outcome_id": "00000000-0000-0000-0000-000000000001",
                "outcome_type": "failure",
                "scope": "agent",
                "weight_adjustments": [],
                "rules_generated": [],
                "rule_skipped_reason": "below_confidence_threshold",
                "out_of_scope_count": 0,
                "evolve_ms": 1,
            }
        ),
    )

    await mcp_server.memclaw_evolve(
        outcome="a thing happened",
        outcome_type="failure",
        related_ids=["11111111-1111-1111-1111-111111111111"],
        scope="agent",
        agent_id="a1",
    )

    assert "llm-start" in events, "LLM helper never ran"
    llm_idx = events.index("llm-start")
    prior_exits = [i for i, e in enumerate(events[:llm_idx]) if e == "session-exit"]
    assert prior_exits, "no session closed before the LLM call — P3 fix regressed"
    next_enters = [
        i
        for i, e in enumerate(events[llm_idx + 1 :], start=llm_idx + 1)
        if e == "session-enter"
    ]
    assert next_enters, (
        "no second session opened after LLM — persist phase missing or merged"
    )


async def test_evolve_uses_two_distinct_sessions(mcp_env, monkeypatch):
    """The refactor opens exactly two sessions per successful call:
    one for the read phase, one for the persist + commit phase. A
    regression to a single session, or a third session, would change
    this count."""
    session_count = 0

    @asynccontextmanager
    async def _counting_session():
        nonlocal session_count
        session_count += 1
        try:
            yield MagicMock(name=f"db-{session_count}")
        finally:
            pass

    monkeypatch.setattr(mcp_server, "_mcp_session", _counting_session)
    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None))
    )
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock())
    monkeypatch.setattr(
        "core_api.services.evolve_service._filter_by_scope",
        AsyncMock(return_value=(["11111111-1111-1111-1111-111111111111"], 0)),
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "core_api.services.evolve_service._maybe_generate_rule",
        AsyncMock(return_value=(None, "not_failure_or_partial")),
    )
    monkeypatch.setattr(
        "core_api.services.evolve_service._apply_outcome_to_db",
        AsyncMock(
            return_value={
                "outcome_id": "00000000-0000-0000-0000-000000000001",
                "outcome_type": "success",
                "scope": "agent",
                "weight_adjustments": [],
                "rules_generated": [],
                "rule_skipped_reason": "not_failure_or_partial",
                "out_of_scope_count": 0,
                "evolve_ms": 1,
            }
        ),
    )

    await mcp_server.memclaw_evolve(
        outcome="all good",
        outcome_type="success",
        related_ids=["11111111-1111-1111-1111-111111111111"],
        scope="agent",
        agent_id="a1",
    )

    assert session_count == 2, (
        f"expected 2 distinct sessions (read + write), got {session_count}"
    )
