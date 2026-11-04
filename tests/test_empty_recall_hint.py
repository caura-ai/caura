"""EC-05 — an empty search names its closest below-cut-off result.

A search that returns nothing used to read, to an agent, as "nothing was ever
saved". The usual truth is that a memory scored just under the similarity
cut-off. ``empty_hint`` names that memory, its similarity and the cut-off, on
REST /search, REST /recall and MCP ``caura_recall`` — and only when the result
set is empty, so the ordinary recall carries no new bytes.

The hint must never reveal a memory the caller could not read: it is drawn
from storage's scored set, which already applied every scope filter, and the
derived-row opt-out is re-applied. The isolation cases below pin that.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core_api import mcp_server
from core_api.empty_hint import build_empty_hint
from core_api.services import memory_service
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_test_auth


def _row(
    mid: str,
    sim: float | None,
    *,
    title="t",
    content="c",
    has_embedding=True,
    metadata=None,
):
    return SimpleNamespace(
        Memory=SimpleNamespace(
            id=mid, title=title, content=content, metadata_=metadata
        ),
        vec_sim=sim,
        has_embedding=has_embedding,
    )


# ---------------------------------------------------------------------------
# build_empty_hint
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_picks_highest_similarity_not_first_row():
    """raw_rows is ordered by the composite score, not by cosine."""
    hint = build_empty_hint(
        [
            _row("a", 0.31, title="first"),
            _row("b", 0.46, title="closest"),
            _row("c", 0.2),
        ],
        min_similarity=0.5,
    )
    assert hint is not None
    assert hint["memory_id"] == "b"
    assert hint["title"] == "closest"
    assert hint["similarity"] == 0.46
    assert hint["min_similarity"] == 0.5
    assert "0.46" in hint["message"] and "0.50" in hint["message"]
    assert "read it by id" in hint["message"]


@pytest.mark.unit
def test_wording_does_not_suggest_lowering_the_cut_off():
    hint = build_empty_hint([_row("a", 0.4)], min_similarity=0.5)
    assert hint is not None
    assert "lower" not in hint["message"].lower()
    assert "min_similarity" not in hint["message"]


@pytest.mark.unit
def test_no_candidates_means_no_hint():
    assert build_empty_hint([], min_similarity=0.5) is None
    assert build_empty_hint(None, min_similarity=0.5) is None
    assert build_empty_hint([_row("a", 0.4)], min_similarity=None) is None


@pytest.mark.unit
def test_unembedded_and_unscored_rows_are_not_reported():
    """A missing vector's 0.0 is a sentinel, not a similarity."""
    rows = [_row("a", 0.0, has_embedding=False), _row("b", None)]
    assert build_empty_hint(rows, min_similarity=0.5) is None


@pytest.mark.unit
def test_derived_rows_skipped_when_caller_excluded_them():
    derived = _row(
        "child",
        0.49,
        metadata={"parent_memory_id": "parent", "source": "atomic_fact_fanout"},
    )
    parent = _row("parent", 0.3)
    assert build_empty_hint(
        [derived, parent], min_similarity=0.5, include_derived=False
    )["memory_id"] == ("parent")
    assert (
        build_empty_hint([derived, parent], min_similarity=0.5)["memory_id"] == "child"
    )


@pytest.mark.unit
def test_title_is_sanitized_and_capped():
    nasty = "Line one\n\x00\x1b[31mred\u202e  " + "x" * 200
    hint = build_empty_hint([_row("a", 0.4, title=nasty)], min_similarity=0.5)
    assert hint is not None
    title = hint["title"]
    assert len(title) <= 80
    assert (
        "\n" not in title
        and "\x00" not in title
        and "\x1b" not in title
        and "\u202e" not in title
    )
    assert title.startswith("Line one")


@pytest.mark.unit
def test_falls_back_to_content_when_untitled():
    hint = build_empty_hint(
        [_row("a", 0.4, title=None, content="the deploy key rotates")],
        min_similarity=0.5,
    )
    assert hint is not None
    assert hint["title"] == "the deploy key rotates"


# ---------------------------------------------------------------------------
# MCP caura_recall forwards the hint only when present
# ---------------------------------------------------------------------------


class _MemoryStub:
    def model_dump(self, mode: str = "python"):
        return {"id": "m-1"}


class _FakeConfig:
    recall_boost = False
    graph_expand = False
    entity_retrieval = True


async def _fake_resolve_config(tenant_id):
    return _FakeConfig()


_HINT = {
    "memory_id": "m-9",
    "title": "deploy key rotation",
    "similarity": 0.46,
    "min_similarity": 0.5,
    "message": "…",
}


def _search(results, hint):
    async def _s(*args, empty_hint_ctx=None, **kwargs):
        if hint is not None and empty_hint_ctx is not None:
            empty_hint_ctx["hint"] = hint
        return results

    return _s


@pytest.mark.unit
async def test_mcp_recall_carries_hint_on_empty(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").side_effect = _search([], _HINT)
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    stub_storage_client(monkeypatch, get_agent=None)

    payload = parse_envelope(await mcp_server.caura_recall(query="anything"))
    assert payload["count"] == 0
    assert payload["empty_hint"] == _HINT


@pytest.mark.unit
async def test_mcp_recall_omits_hint_when_absent(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").side_effect = _search([_MemoryStub()], None)
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    stub_storage_client(monkeypatch, get_agent=None)

    payload = parse_envelope(await mcp_server.caura_recall(query="anything"))
    assert "empty_hint" not in payload


# ---------------------------------------------------------------------------
# REST, end to end through the real pipeline and storage
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline_search(monkeypatch):
    # tests/pipeline/test_search_pipeline.py flips this off without restoring it.
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)


def _tenant() -> str:
    return f"test-tenant-empty-hint-{uuid.uuid4().hex[:8]}"


async def _write(client, headers, tenant_id, agent_id, content, **extra):
    resp = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "content": content,
            **extra,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.integration
async def test_search_empty_result_names_closest_memory(client, pipeline_search):
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    mid = await _write(
        client,
        headers,
        tenant_id,
        "agent-a",
        f"Deploy keys rotate monthly {uuid.uuid4().hex}",
    )

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        # An unreachable cut-off makes the miss deterministic under fake embeddings.
        json={
            "tenant_id": tenant_id,
            "query": "deploy key rotation",
            "min_similarity": 0.999,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    hint = body["empty_hint"]
    assert hint["memory_id"] == mid
    assert hint["min_similarity"] == 0.999
    assert hint["similarity"] < 0.999


@pytest.mark.integration
async def test_search_with_results_has_null_hint(client, pipeline_search):
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    content = f"The oncall rotation hands over on Tuesdays {uuid.uuid4().hex}"
    await _write(client, headers, tenant_id, "agent-a", content)

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={"tenant_id": tenant_id, "query": content},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"]
    assert body["empty_hint"] is None


@pytest.mark.integration
async def test_hint_never_names_another_agents_private_memory(client, pipeline_search):
    """Scope comes from storage: a scope_agent row of a peer is not a candidate."""
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    mid = await _write(
        client,
        headers,
        tenant_id,
        "agent-private",
        f"Private: the vault code is in the drawer {uuid.uuid4().hex}",
        visibility="scope_agent",
    )

    async def _search_as(agent_id):
        resp = await client.post(
            "/api/v1/search",
            headers=headers,
            json={
                "tenant_id": tenant_id,
                "query": "vault code",
                "min_similarity": 0.999,
                "caller_agent_id": agent_id,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["items"] == []
        return body["empty_hint"]

    assert await _search_as("agent-other") is None
    # Control: the owner sees its own row as the closest miss, so the null
    # above is the scope filter at work, not an absent candidate.
    assert (await _search_as("agent-private"))["memory_id"] == mid


@pytest.mark.integration
async def test_hint_never_crosses_tenants(client, pipeline_search):
    other_tenant = _tenant()
    await _write(
        client,
        get_test_auth(other_tenant)[1],
        other_tenant,
        "agent-a",
        f"Other tenant fact {uuid.uuid4().hex}",
    )
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": "Other tenant fact",
            "min_similarity": 0.999,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["empty_hint"] is None


@pytest.mark.integration
async def test_recall_empty_result_names_closest_memory(client, pipeline_search):
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    mid = await _write(
        client,
        headers,
        tenant_id,
        "agent-a",
        f"Backups run nightly at 02:00 {uuid.uuid4().hex}",
    )

    resp = await client.post(
        "/api/v1/recall",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": "backup schedule",
            "min_similarity": 0.999,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["memory_count"] == 0
    assert body["empty_hint"]["memory_id"] == mid
