"""RemoteRanker + registry tests — HTTP /rerank via httpx.MockTransport.

No network: a MockTransport stands in for the sidecar so we verify the
request body we send, the response parse (TEI bare-list AND Cohere-wrapped),
re-projection onto INPUT order, and the registry's remote wiring / no-URL
guard. This tests the provider CODE against the assumed contract — a real
TEI wet test still happens at sidecar-deploy time (see PR notes).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from common.ranking import RankCandidate, get_ranking
from common.ranking.protocols import RankProvider
from common.ranking.providers.remote import RemoteRanker


def _cands(*contents):
    return [
        RankCandidate(id=str(i), content=c, similarity=0.5)
        for i, c in enumerate(contents)
    ]


def _client_with(handler):
    """Build a RemoteRanker whose httpx client uses a MockTransport handler."""
    r = RemoteRanker(base_url="http://sidecar:80", model="test-model")
    r._client = httpx.AsyncClient(
        base_url="http://sidecar:80", transport=httpx.MockTransport(handler)
    )
    return r


def test_remote_conforms_to_protocol():
    assert isinstance(RemoteRanker(base_url="http://x"), RankProvider)


@pytest.mark.asyncio
async def test_tei_bare_list_response_reprojected_to_input_order():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        # TEI-native: bare list, ranked (best first), index into input texts.
        return httpx.Response(
            200, json=[{"index": 2, "score": 0.9}, {"index": 0, "score": 0.1}]
        )

    r = _client_with(handler)
    scores = await r.rank("q", _cands("a", "b", "c"))
    # request shape
    assert captured["path"] == "/rerank"
    assert captured["body"] == {"query": "q", "texts": ["a", "b", "c"]}
    # scores re-projected to INPUT order: idx0=0.1, idx1=missing→0.0, idx2=0.9
    assert scores == [0.1, 0.0, 0.9]


@pytest.mark.asyncio
async def test_cohere_wrapped_response_and_relevance_score():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.8},
                    {"index": 1, "relevance_score": 0.3},
                ]
            },
        )

    r = _client_with(handler)
    scores = await r.rank("q", _cands("x", "y"))
    assert scores == [0.8, 0.3]


@pytest.mark.asyncio
async def test_empty_candidates_short_circuits():
    r = _client_with(lambda req: httpx.Response(500))  # would error if called
    assert await r.rank("q", []) == []


@pytest.mark.asyncio
async def test_get_ranking_remote_degrades_on_http_error():
    # 503 from the sidecar → provider raises → service returns None (keep order).
    def handler(request):
        return httpx.Response(503, text="unavailable")

    import common.ranking._service as svc

    r = _client_with(handler)
    # Force the service to use our mock-backed remote ranker.
    orig = svc.get_rank_provider
    svc.get_rank_provider = lambda name, tc=None: r
    try:
        out = await get_ranking(
            "q", _cands("a", "b"), SimpleNamespace(rank_provider="remote")
        )
    finally:
        svc.get_rank_provider = orig
    assert out is None
    await r._client.aclose()


def test_registry_remote_requires_base_url():
    from common.ranking._registry import get_rank_provider

    # No RANK_BASE_URL and no tenant override → ValueError (→ service degrades).
    with pytest.raises(ValueError, match="requires a base URL"):
        get_rank_provider("remote", SimpleNamespace(rank_base_url=None))


def test_registry_remote_builds_with_tenant_base_url():
    from common.ranking._registry import get_rank_provider

    p = get_rank_provider(
        "remote", SimpleNamespace(rank_base_url="http://tei:80", rank_model="m")
    )
    assert isinstance(p, RemoteRanker)
    assert p.model == "m"
