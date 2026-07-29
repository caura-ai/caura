"""Unit tests for the pluggable ranking component (common/ranking).

Covers the provider Protocol conformance, the noop true-identity
contract, the fake provider's overlap signal, and the service's
degrade-to-first-stage (None) behaviour on empty input, misconfig, and a
provider that violates the length contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.ranking import RankCandidate, get_ranking
from common.ranking.protocols import RankProvider
from common.ranking.providers.fake import FakeRanker
from common.ranking.providers.noop import NoopRanker


def _cand(cid: str, content: str, similarity: float) -> RankCandidate:
    return RankCandidate(id=cid, content=content, similarity=similarity)


def test_providers_conform_to_protocol():
    assert isinstance(NoopRanker(), RankProvider)
    assert isinstance(FakeRanker(), RankProvider)


@pytest.mark.asyncio
async def test_noop_is_true_identity_regardless_of_similarity():
    # similarity is NOT descending in input order — a naive "return
    # similarity" noop would reorder these; the true-identity noop must not.
    cands = [_cand("a", "x", 0.2), _cand("b", "y", 0.9), _cand("c", "z", 0.5)]
    scores = await NoopRanker().rank("q", cands)
    # scores strictly decreasing by position → sort(reverse) keeps order
    assert scores == sorted(scores, reverse=True)
    assert len(scores) == 3


@pytest.mark.asyncio
async def test_fake_rewards_word_overlap():
    cands = [
        _cand("a", "totally unrelated text", 0.5),
        _cand("b", "alpha matches the query", 0.5),
    ]
    scores = await FakeRanker().rank("alpha", cands)
    assert scores[1] > scores[0]  # overlap with "alpha" scores higher


@pytest.mark.asyncio
async def test_get_ranking_empty_returns_none():
    assert await get_ranking("q", []) is None


@pytest.mark.asyncio
async def test_get_ranking_defaults_to_noop_identity():
    # No tenant_config, no RANK_PROVIDER env → noop → strictly descending.
    cands = [_cand("a", "x", 0.1), _cand("b", "y", 0.9)]
    scores = await get_ranking("q", cands)
    assert scores is not None
    assert scores[0] > scores[1]  # position 0 ranked first (identity)


@pytest.mark.asyncio
async def test_get_ranking_fake_via_tenant_config():
    tenant = SimpleNamespace(rank_provider="fake")
    cands = [_cand("a", "nope", 0.5), _cand("b", "alpha", 0.5)]
    scores = await get_ranking("alpha", cands, tenant)
    assert scores is not None
    assert scores[1] > scores[0]


@pytest.mark.asyncio
async def test_get_ranking_unknown_provider_degrades_to_none():
    tenant = SimpleNamespace(rank_provider="does-not-exist")
    cands = [_cand("a", "x", 0.5)]
    assert await get_ranking("q", cands, tenant) is None


@pytest.mark.asyncio
async def test_get_ranking_length_contract_violation_degrades(monkeypatch):
    class BadRanker:
        provider_name = "bad"
        model = "bad"

        async def rank(self, query, candidates):
            return [0.1]  # wrong length vs 2 candidates

    monkeypatch.setattr(
        "common.ranking._service.get_rank_provider", lambda name, tc=None: BadRanker()
    )
    cands = [_cand("a", "x", 0.5), _cand("b", "y", 0.5)]
    assert await get_ranking("q", cands, SimpleNamespace(rank_provider="bad")) is None
