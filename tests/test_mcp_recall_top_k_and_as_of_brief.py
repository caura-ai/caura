"""caura_recall honours the caller's top_k and anchors its brief to valid_at.

M-19: caura_recall never told ``search_memories`` that its ``top_k`` was the
caller's, so an agent profile's or the tenant default's ``top_k`` replaced it,
and a recency query was cut to 5 rows, while ``effective_top_k`` echoed the
request. REST passes ``top_k_explicit`` (SIDE-57/60); MCP now does too, and
leaves a ``top_k`` it was not given to the profile, as REST does. The pipeline
reports the ``top_k`` it resolved, and ``effective_top_k`` reads it.

M-20: an as-of recall filtered its rows by ``valid_at`` but built the brief
without it, so the summary answered relative dates against today.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core_api import mcp_server
from core_api.constants import DEFAULT_SEARCH_TOP_K
from core_api.services import memory_service
from core_api.services.organization_settings import ResolvedConfig
from tests._mcp_test_helpers import parse_envelope, stub_storage_client

_PROFILE_TOP_K = 10


class _Row:
    def model_dump(self, mode="python"):
        return {"id": "m-1"}


def _search(mcp_env, monkeypatch, **retrieval):
    """``search_memories``, filling ``retrieval_ctx`` as the pipeline does."""
    stub_storage_client(monkeypatch, get_agent=None)
    mock = mcp_env["service"]("search_memories")

    async def run(**kwargs):
        if kwargs.get("retrieval_ctx") is not None:
            kwargs["retrieval_ctx"].update(retrieval)
        return [_Row()]

    mock.side_effect = run
    return mock


# ── M-19: whose top_k it is ──────────────────────────────────────────────


@pytest.mark.unit
async def test_a_top_k_the_caller_names_is_explicit(mcp_env, monkeypatch):
    search = _search(mcp_env, monkeypatch)

    await mcp_server.caura_recall(query="x", top_k=12)

    assert search.await_args.kwargs["top_k"] == 12
    assert search.await_args.kwargs["top_k_explicit"] is True


@pytest.mark.unit
async def test_an_omitted_top_k_is_left_to_the_profile(mcp_env, monkeypatch):
    """Control: REST's ladder, request then agent profile then tenant default."""
    search = _search(mcp_env, monkeypatch)

    await mcp_server.caura_recall(query="x")

    assert search.await_args.kwargs["top_k"] == DEFAULT_SEARCH_TOP_K
    assert not search.await_args.kwargs.get("top_k_explicit", False)


@pytest.mark.unit
async def test_effective_top_k_reports_the_profiles_top_k(mcp_env, monkeypatch):
    _search(mcp_env, monkeypatch, resolved_top_k=_PROFILE_TOP_K)

    payload = parse_envelope(await mcp_server.caura_recall(query="x"))

    assert payload["effective_top_k"] == _PROFILE_TOP_K
    assert payload["requested_top_k"] is None
    assert payload["truncated"] is False


@pytest.mark.unit
async def test_effective_top_k_reports_a_strategy_cut(mcp_env, monkeypatch):
    """RECENT_CONTEXT's cap on a recall that named no top_k."""
    _search(mcp_env, monkeypatch, resolved_top_k=_PROFILE_TOP_K, effective_top_k=5)

    payload = parse_envelope(await mcp_server.caura_recall(query="what did I do last"))

    assert payload["effective_top_k"] == 5


@pytest.mark.integration
@pytest.mark.parametrize(
    ("top_k", "explicit", "resolved"),
    [(3, True, 3), (DEFAULT_SEARCH_TOP_K, False, _PROFILE_TOP_K)],
    ids=["named", "not-named"],
)
async def test_the_pipeline_reports_the_top_k_it_resolved(
    tenant_id, monkeypatch, top_k, explicit, resolved
):
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)
    retrieval_ctx: dict = {}

    await memory_service.search_memories(
        tenant_id=tenant_id,
        query="which region hosts the billing database",
        top_k=top_k,
        tenant_config=ResolvedConfig(
            {"search": {"default_profile": {"top_k": _PROFILE_TOP_K}}}
        ),
        top_k_explicit=explicit,
        retrieval_ctx=retrieval_ctx,
    )

    assert retrieval_ctx["resolved_top_k"] == resolved


# ── M-20: the brief's reference date ──────────────────────────────────────


@pytest.mark.unit
async def test_the_brief_is_anchored_to_valid_at(mcp_env, monkeypatch):
    _search(mcp_env, monkeypatch)
    brief = mcp_env["service"]("summarize_memories")
    brief.return_value = {"summary": "s"}

    await mcp_server.caura_recall(
        query="who owns billing now",
        include_brief=True,
        valid_at="2025-03-01T00:00:00Z",
    )

    assert brief.await_args.kwargs["valid_at"] == datetime(2025, 3, 1, tzinfo=UTC)
