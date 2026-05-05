"""Unit tests for ``memclaw_recall`` (replaces the prior search + brief).

Covers:
- Happy path with results (no brief).
- ``include_brief=True`` merges the summary into the response.
- Empty results yield an empty array (not "No memories found.").
- Input validation (invalid memory_type / status → 422).
- ``top_k`` is capped at ``MAX_SEARCH_TOP_K``.
- ``HTTPException`` from the service → ``Error (…)`` envelope.
- Auth failure short-circuits.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _MemoryStub:
    """Minimal stand-in for a search result Pydantic model."""

    def __init__(self, mid: str, score: float = 0.9):
        self.mid = mid
        self.score = score

    def model_dump(self, mode: str = "python"):  # noqa: ARG002
        return {"id": self.mid, "score": self.score}


async def test_recall_happy_path(mcp_env, monkeypatch):
    """Standard query returns JSON with `results` and no `brief`."""
    search_mock = mcp_env["service"]("search_memories")
    search_mock.return_value = [_MemoryStub("m-1"), _MemoryStub("m-2")]

    # Patch the late-imported config/profile dependencies.
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        _fake_resolve_config,
    )
    monkeypatch.setattr("core_api.repositories.agent_repo.get_by_id", _async_none)

    out = await mcp_server.memclaw_recall(query="what do I know about onboarding?")
    payload = parse_envelope(out)
    assert "results" in payload
    assert len(payload["results"]) == 2
    assert "brief" not in payload
    search_mock.assert_called_once()


async def test_recall_with_include_brief(mcp_env, monkeypatch):
    """`include_brief=True` runs the brief call and merges it."""
    mcp_env["service"]("search_memories").return_value = [_MemoryStub("m-1")]

    brief_mock = _fake_async_return({"summary": "alice onboarded last quarter"})
    monkeypatch.setattr("core_api.services.recall_service.recall", brief_mock)
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _fake_resolve_config
    )
    monkeypatch.setattr("core_api.repositories.agent_repo.get_by_id", _async_none)

    out = await mcp_server.memclaw_recall(
        query="status?", include_brief=True
    )
    payload = parse_envelope(out)
    assert "brief" in payload
    assert payload["brief"]["summary"].startswith("alice")


async def test_recall_empty_results(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").return_value = []
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _fake_resolve_config
    )
    monkeypatch.setattr("core_api.repositories.agent_repo.get_by_id", _async_none)

    out = await mcp_server.memclaw_recall(query="nothing matches")
    payload = parse_envelope(out)
    assert payload["results"] == []


async def test_recall_invalid_memory_type_returns_422(mcp_env):
    out = await mcp_server.memclaw_recall(query="x", memory_type="garbage")
    assert "INVALID_ARGUMENTS" in out
    assert "Invalid memory_type 'garbage'" in out


async def test_recall_invalid_status_returns_422(mcp_env):
    out = await mcp_server.memclaw_recall(query="x", status="badstatus")
    assert "INVALID_ARGUMENTS" in out
    assert "Invalid status 'badstatus'" in out


async def test_recall_top_k_is_capped(mcp_env, monkeypatch):
    """Passing top_k > MAX_SEARCH_TOP_K passes the cap to the service, not the raw value."""
    from core_api.constants import MAX_SEARCH_TOP_K

    search_mock = mcp_env["service"]("search_memories")
    search_mock.return_value = []
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _fake_resolve_config
    )
    monkeypatch.setattr("core_api.repositories.agent_repo.get_by_id", _async_none)

    await mcp_server.memclaw_recall(query="x", top_k=1000)
    kwargs = search_mock.await_args.kwargs
    assert kwargs["top_k"] == MAX_SEARCH_TOP_K


async def test_recall_http_exception_becomes_error_envelope(mcp_env, monkeypatch):
    """Service raises HTTPException → handler returns `Error (status): detail`."""
    mcp_env["service"]("search_memories").side_effect = HTTPException(
        status_code=429, detail="rate limited"
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _fake_resolve_config
    )
    monkeypatch.setattr("core_api.repositories.agent_repo.get_by_id", _async_none)

    out = await mcp_server.memclaw_recall(query="x")
    assert "RATE_LIMITED" in out
    assert "rate limited" in out


async def test_recall_auth_failure_shortcircuits(monkeypatch):
    """Auth failure skips the handler body entirely."""
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)

    out = await mcp_server.memclaw_recall(query="x")
    assert out == mcp_server._AUTH_ERROR


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
