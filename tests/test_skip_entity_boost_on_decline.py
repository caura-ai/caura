"""Skip entity boost when ClassifyQuery declined the entity match (CAURA-698).

``ClassifyQuery`` and ``ParallelEmbedAndEntityBoost`` run the same tokenizer
and the same OR-semantics entity FTS. When the classifier declines the match
as over-broad (``> ENTITY_LOOKUP_MAX_MATCHES``), the boost step used to
re-derive the identical match and hand ``GRAPH_HOP_BOOST`` to an arbitrary
``GRAPH_MAX_BOOSTED_MEMORIES``-sized subset of the sibling pool — a lottery
that outranks rows pure scoring puts first. Measured on the S1 bench: the
gold row ranks #1 on unboosted score yet falls out of top-10 whenever it
misses a boost slot (P ≈ 50/N_siblings: 57% at K=1000, 6% at K=10000).

The fix threads the classifier's verdict through ``ctx.data``:

- ``ClassifyQuery`` sets ``entity_match_declined=True`` when it declines.
- ``ParallelEmbedAndEntityBoost`` skips the boost leg entirely on that flag
  (embedding still runs — it is the critical path).

These tests pin both halves plus the no-flag default. Pure unit tests, no DB.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import ENTITY_LOOKUP_MAX_MATCHES
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.classify_query import ClassifyQuery
from core_api.pipeline.steps.search.parallel_embed_entity_boost import (
    ParallelEmbedAndEntityBoost,
)
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_EMBED_PATH = "core_api.pipeline.steps.search.parallel_embed_entity_boost._get_or_cache_embedding"
_BOOST_PATH = "core_api.pipeline.steps.search.parallel_embed_entity_boost._entity_boost_via_storage"
_CLASSIFY_SC_PATH = "core_api.pipeline.steps.search.classify_query.get_storage_client"

_BOOST_LOGGER = "core_api.pipeline.steps.search.parallel_embed_entity_boost"

_DUMMY_EMBED = [0.1] * 1536


def _boost_ctx(extra: dict | None = None) -> PipelineContext:
    data = {
        "query": "test",
        "tenant_id": "t1",
        "tenant_config": None,
        "search_params": {"graph_max_hops": 2},
        "graph_expand": True,
        "fleet_ids": ["fleet-1"],
    }
    if extra:
        data.update(extra)
    return PipelineContext(data=data)


def _classify_ctx() -> PipelineContext:
    return PipelineContext(
                data={
            "query": "What is Comet 0002's launch_date?",
            "tenant_id": "t1",
            "fleet_ids": ["fleet-1"],
            "search_params": {"graph_max_hops": 2, "top_k": 10, "fts_weight": 0.3},
        },
    )


# ---------------------------------------------------------------------------
# ParallelEmbedAndEntityBoost honours the flag
# ---------------------------------------------------------------------------


async def test_declined_flag_skips_entity_boost(caplog):
    """Flag set → boost helper never invoked; embedding still populated."""
    caplog.set_level(logging.INFO, logger=_BOOST_LOGGER)

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    boost_mock = AsyncMock(return_value=({uuid4()}, {uuid4(): 1.3}))

    ctx = _boost_ctx({"entity_match_declined": True})
    with (
        patch(_EMBED_PATH, side_effect=fast_embed),
        patch(_BOOST_PATH, boost_mock),
    ):
        await ParallelEmbedAndEntityBoost().execute(ctx)

    boost_mock.assert_not_called()
    assert ctx.data["embedding"] == _DUMMY_EMBED
    assert ctx.data["boosted_memory_ids"] == set()
    assert ctx.data["memory_boost_factor"] == {}
    assert any("entity boost skipped" in r.message for r in caplog.records)


async def test_without_flag_entity_boost_still_runs():
    """No flag → prior behaviour intact: boost results flow into ctx.data."""
    boosted = {uuid4()}
    factors = {next(iter(boosted)): 1.3}

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    boost_mock = AsyncMock(return_value=(boosted, factors))

    ctx = _boost_ctx()
    with (
        patch(_EMBED_PATH, side_effect=fast_embed),
        patch(_BOOST_PATH, boost_mock),
    ):
        await ParallelEmbedAndEntityBoost().execute(ctx)

    boost_mock.assert_called_once()
    assert ctx.data["boosted_memory_ids"] == boosted
    assert ctx.data["memory_boost_factor"] == factors


# ---------------------------------------------------------------------------
# ClassifyQuery sets the flag
# ---------------------------------------------------------------------------


async def test_classifier_sets_flag_on_overbroad_decline():
    """> ENTITY_LOOKUP_MAX_MATCHES entity matches → decline + flag + fallthrough."""
    over_broad = [uuid4() for _ in range(ENTITY_LOOKUP_MAX_MATCHES + 1)]

    ctx = _classify_ctx()
    with (
        patch(_CLASSIFY_SC_PATH, return_value=AsyncMock()),
        patch.object(ClassifyQuery, "_entity_fts", AsyncMock(return_value=over_broad)),
    ):
        await ClassifyQuery().execute(ctx)

    assert ctx.data.get("entity_match_declined") is True
    assert ctx.data["retrieval_plan"].strategy is RetrievalStrategy.SEMANTIC_SEARCH


async def test_classifier_leaves_flag_unset_when_no_entity_match():
    """Zero entity matches → ordinary fallthrough, flag absent."""
    ctx = _classify_ctx()
    with (
        patch(_CLASSIFY_SC_PATH, return_value=AsyncMock()),
        patch.object(ClassifyQuery, "_entity_fts", AsyncMock(return_value=[])),
    ):
        await ClassifyQuery().execute(ctx)

    assert "entity_match_declined" not in ctx.data
    assert ctx.data["retrieval_plan"].strategy is RetrievalStrategy.SEMANTIC_SEARCH
