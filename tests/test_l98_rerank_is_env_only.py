"""L-98: reranking is configured by the environment only.

The rerank code read five per-tenant overrides off ``tenant_config``
(``rank_enabled``, ``rank_provider``, ``rank_model``, ``rank_base_url``,
``rank_api_key``) and its comments advertised them, but no tenant could set
one: ``ResolvedConfig`` has no such properties and the settings schema rejects
the keys. Eldad decided on 2026-10-09 to delete the reads rather than make them
settings. A tenant-set ``rank_base_url`` would point the server at any URL,
with the platform's ``RANK_API_KEY`` as the fallback credential.

Driven through the rerank step, which is where a search's tenant config
arrives: whatever object that is, it steers nothing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core_api.pipeline.steps.search.rerank_results as rerank_mod
from common.ranking.constants import RANK_MODEL
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.rerank_results import RerankResults

pytestmark = [pytest.mark.unit]

# Everything a tenant config used to be read for.
_STEERING = SimpleNamespace(
    rank_enabled=True,
    rank_provider="fake",
    rank_model="tenant-model",
    rank_base_url="http://tenant-chosen.example",
    rank_api_key="tenant-key",
)


def _row(content: str, similarity: float):
    return SimpleNamespace(
        Memory=SimpleNamespace(id=uuid.uuid4(), content=content, memory_type="fact"),
        similarity=similarity,
        vec_sim=similarity,
        freshness=1.0,
        score=similarity,
    )


async def _rerank(rows: list, tenant_config=_STEERING):
    """Run the step; return its result and the rows in their new order."""
    ctx = PipelineContext(
        data={"raw_rows": rows, "query": "alpha", "tenant_config": tenant_config}
    )
    result = await RerankResults().execute(ctx)
    return result, ctx.data["raw_rows"]


@pytest.mark.asyncio
async def test_a_tenant_config_cannot_switch_reranking_on(monkeypatch):
    monkeypatch.setattr(rerank_mod, "RANK_ENABLED", False)
    result, _ = await _rerank([_row("x", 0.5)])
    assert result is not None and result.outcome.name == "SKIPPED"


@pytest.mark.asyncio
async def test_a_tenant_config_cannot_pick_the_provider(monkeypatch):
    # Enabled, with no RANK_PROVIDER: the noop ranker keeps first-stage order.
    # The tenant's "fake" would have moved the "alpha" row to the front.
    monkeypatch.setattr(rerank_mod, "RANK_ENABLED", True)
    monkeypatch.delenv("RANK_PROVIDER", raising=False)
    first, match = _row("totally unrelated", 0.9), _row("alpha alpha", 0.1)
    _, rows = await _rerank([first, match])
    assert rows == [first, match]


@pytest.mark.asyncio
async def test_a_tenant_config_cannot_point_the_ranker_elsewhere(monkeypatch):
    monkeypatch.setattr(rerank_mod, "RANK_ENABLED", True)
    monkeypatch.setenv("RANK_PROVIDER", "remote")
    monkeypatch.setattr("common.ranking._registry.RANK_BASE_URL", "http://sidecar:80")
    built: list[tuple] = []

    class _Ranker:
        provider_name = "remote"
        model = "m"

        async def rank(self, query, candidates):
            return [0.0] * len(candidates)

    def _build(base_url, api_key, model):
        built.append((base_url, api_key, model))
        return _Ranker()

    monkeypatch.setattr("common.ranking._registry._get_or_create_remote_ranker", _build)
    # No rank_provider here: RANK_PROVIDER picks "remote", and only the
    # endpoint, key and model are offered.
    endpoint = SimpleNamespace(
        rank_base_url=_STEERING.rank_base_url,
        rank_api_key=_STEERING.rank_api_key,
        rank_model=_STEERING.rank_model,
    )
    await _rerank([_row("x", 0.5)], endpoint)
    assert built, "the remote ranker was never built"
    base_url, api_key, model = built[0]
    assert base_url == "http://sidecar:80"
    assert api_key != "tenant-key"
    assert model == RANK_MODEL
