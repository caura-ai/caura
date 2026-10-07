"""``POST /search`` reports its retrieval strategy without ``diagnostic=true``.

SIDE-59 — the strategy ClassifyQuery resolved (keyword_search, semantic_search,
temporal, recent_context, entity_lookup) used to be visible only inside the
diagnostic block, which also dumps every candidate. It is now always sent as the
``X-Caura-Retrieval-Strategy`` response header, plus ``X-Caura-Effective-Top-K``
when a strategy cut the caller's budget. The response BODY is unchanged.

SIDE-57 — RECENT_CONTEXT's 5-row cap no longer overrides a ``top_k`` the caller
named explicitly; pinned end-to-end below.
"""

from __future__ import annotations

import uuid

import pytest

from core_api.services import memory_service
from tests.conftest import get_test_auth


@pytest.fixture
def pipeline_search(monkeypatch):
    """Pin the pipeline search path — the only one that resolves a strategy.

    ``tests/pipeline/test_search_pipeline.py`` flips this flag off without
    restoring it; see ``test_search_recall_tracked_flag.py`` for the full story.
    """
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)


def _tenant() -> str:
    return f"test-tenant-strategy-signal-{uuid.uuid4().hex[:8]}"


async def _write(client, headers, tenant_id, content):
    resp = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={"tenant_id": tenant_id, "agent_id": "strategy-bot", "content": content},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.integration
async def test_non_diagnostic_search_names_semantic_strategy(client, pipeline_search):
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    content = f"The staging cluster is rebuilt every Sunday night from scratch {uuid.uuid4().hex}"
    await _write(client, headers, tenant_id, content)

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": "how often is the staging cluster rebuilt from scratch",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Caura-Retrieval-Strategy"] in {
        "semantic_search",
        "keyword_search",
    }
    # No strategy cut the budget, so no effective-top_k header.
    assert "X-Caura-Effective-Top-K" not in resp.headers
    body = resp.json()
    # The body contract is untouched: the signal lives in headers only.
    assert body["diagnostic"] is None
    assert "retrieval_strategy" not in body


@pytest.mark.integration
async def test_non_diagnostic_search_names_recent_context_strategy(
    client, pipeline_search
):
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    await _write(
        client,
        headers,
        tenant_id,
        f"I bought a blue kayak last weekend {uuid.uuid4().hex}",
    )

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={"tenant_id": tenant_id, "query": "what did I buy most recently"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Caura-Retrieval-Strategy"] == "recent_context"
    # Default top_k (5) is already at the cap — nothing was cut, nothing reported.
    assert "X-Caura-Effective-Top-K" not in resp.headers


@pytest.mark.integration
async def test_recent_context_honours_explicit_top_k(client, pipeline_search):
    """SIDE-57 end-to-end: top_k=8 on a recency query returns more than 5 rows."""
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    query = "what did I buy most recently"
    # The query text itself, so every row is an exact lexical and embedding
    # match and survives the similarity floor under the fake embedder.
    for i in range(8):
        await _write(
            client,
            headers,
            tenant_id,
            f"{query} — receipt number {i} {uuid.uuid4().hex}",
        )

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={"tenant_id": tenant_id, "query": query, "top_k": 8},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Caura-Retrieval-Strategy"] == "recent_context"
    assert "X-Caura-Effective-Top-K" not in resp.headers
    assert len(resp.json()["items"]) > 5


@pytest.mark.unit
def test_retrieval_headers_report_cap_only_when_applied():
    """The effective-top_k header appears only when a strategy cut the budget."""
    from fastapi import Response

    from core_api.routes.memories import _set_retrieval_headers

    capped = Response()
    _set_retrieval_headers(
        capped, {"retrieval_strategy": "recent_context", "effective_top_k": 5}
    )
    assert capped.headers["X-Caura-Retrieval-Strategy"] == "recent_context"
    assert capped.headers["X-Caura-Effective-Top-K"] == "5"

    uncapped = Response()
    _set_retrieval_headers(uncapped, {"retrieval_strategy": "semantic_search"})
    assert uncapped.headers["X-Caura-Retrieval-Strategy"] == "semantic_search"
    assert "X-Caura-Effective-Top-K" not in uncapped.headers

    legacy = Response()
    _set_retrieval_headers(legacy, {})
    assert "X-Caura-Retrieval-Strategy" not in legacy.headers
