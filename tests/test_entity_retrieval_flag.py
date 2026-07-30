"""``search.entity_retrieval`` — org switch that blocks query-time entity/graph reads.

Entity retrieval enters the read path at TWO independent points, so a single
gate is not enough:

1. ``ClassifyQuery`` — entity FTS hit short-circuits to ``ENTITY_LOOKUP`` with
   ``skip_embedding`` + ``skip_scored_search``, so semantic/keyword scoring
   never runs at all.
2. ``ParallelEmbedAndEntityBoost`` — applies ``GRAPH_HOP_BOOST`` on top of the
   scored-search ranking.

With the flag off, both are bypassed and every read resolves through the
temporal / recent-context / keyword / semantic cascade alone — with no entity
FTS or graph-expansion roundtrip issued.

The flag is read-side only: entity extraction and linking keep populating the
graph, so it is reversible without a backfill. Default is ON (unblocked) at
every layer — tenant unset ⇒ env default ⇒ ``True`` — and an absent
``ctx.data`` key means enabled, so internal callers are unaffected.

Pure unit tests, no DB.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import FTS_WEIGHT_BOOSTED
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.classify_query import ClassifyQuery
from core_api.pipeline.steps.search.parallel_embed_entity_boost import (
    ParallelEmbedAndEntityBoost,
)
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy
from core_api.services.organization_settings import (
    DEFAULT_SETTINGS,
    ResolvedConfig,
    _check_keys,
    _validate_leaf_types,
)

# ``asyncio_mode = auto`` (pytest.ini) runs the async tests below; the file also
# holds sync settings-resolution tests, so the asyncio mark stays off pytestmark.
pytestmark = [pytest.mark.unit]

_EMBED_PATH = (
    "core_api.pipeline.steps.search.parallel_embed_entity_boost._get_or_cache_embedding"
)
_BOOST_PATH = "core_api.pipeline.steps.search.parallel_embed_entity_boost._entity_boost_via_storage"
_CLASSIFY_SC_PATH = "core_api.pipeline.steps.search.classify_query.get_storage_client"

_BOOST_LOGGER = "core_api.pipeline.steps.search.parallel_embed_entity_boost"
_CLASSIFY_LOGGER = "core_api.pipeline.steps.search.classify_query"

_DUMMY_EMBED = [0.1] * 1536

# A query whose tokens WOULD hit the entity index — every classify test below
# uses it so a passing assertion means the flag suppressed entity retrieval,
# not that the tokenizer found nothing to look up.
_ENTITY_QUERY = "What is Comet 0002's launch_date?"


def _classify_ctx(*, fts_weight: float = 0.3, **extra) -> PipelineContext:
    data = {
        "query": _ENTITY_QUERY,
        "tenant_id": "t1",
        "fleet_ids": ["fleet-1"],
        "search_params": {"graph_max_hops": 2, "top_k": 10, "fts_weight": fts_weight},
        **extra,
    }
    return PipelineContext(data=data)


def _boost_ctx(extra: dict | None = None) -> PipelineContext:
    data = {
        "query": _ENTITY_QUERY,
        "tenant_id": "t1",
        "tenant_config": None,
        "search_params": {"graph_max_hops": 2},
        "graph_expand": True,
        "fleet_ids": ["fleet-1"],
    }
    if extra:
        data.update(extra)
    return PipelineContext(data=data)


def _entity_match_sc() -> AsyncMock:
    """Storage client whose entity index WOULD short-circuit to ENTITY_LOOKUP."""
    eid, mid = str(uuid4()), str(uuid4())
    sc = AsyncMock()
    sc.fts_search_entities = AsyncMock(return_value=[eid])
    sc.expand_graph = AsyncMock(return_value={eid: {"hop": 0, "weight": 1.0}})
    sc.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[{"memory_id": mid, "entity_id": eid, "role": "subject"}]
    )
    sc.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": mid,
                "tenant_id": "t1",
                "content": "Comet 0002 launch_date is 2026-01-01",
                "memory_type": "fact",
            }
        ]
    )
    return sc


# ---------------------------------------------------------------------------
# Gate 1 — ClassifyQuery skips the entity block
# ---------------------------------------------------------------------------


async def test_flag_off_routes_entity_query_to_semantic(caplog):
    """Entity-token query with the flag off → SEMANTIC_SEARCH, zero entity roundtrips."""
    caplog.set_level(logging.INFO, logger=_CLASSIFY_LOGGER)
    sc = _entity_match_sc()

    ctx = _classify_ctx(entity_retrieval=False)
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    plan = ctx.data["retrieval_plan"]
    assert plan.strategy is RetrievalStrategy.SEMANTIC_SEARCH
    # The whole point: scoring is NOT skipped, so semantic search actually runs.
    assert plan.skip_embedding is False
    assert plan.skip_scored_search is False
    # No entity/graph work was issued at all.
    sc.fts_search_entities.assert_not_called()
    sc.expand_graph.assert_not_called()
    sc.get_memory_ids_by_entity_ids.assert_not_called()
    sc.load_memories_by_ids.assert_not_called()
    # Neither fallthrough marker is left behind for the boost step to consume.
    assert "filtered_rows" not in ctx.data
    assert "entity_match_declined" not in ctx.data
    assert "_classified_entity_hops" not in ctx.data
    assert any(
        "entity retrieval disabled by org setting" in r.message for r in caplog.records
    )


async def test_flag_off_routes_specific_query_to_keyword():
    """Boosted fts_weight with the flag off → KEYWORD_SEARCH, not ENTITY_LOOKUP."""
    sc = _entity_match_sc()

    ctx = _classify_ctx(fts_weight=FTS_WEIGHT_BOOSTED, entity_retrieval=False)
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["retrieval_plan"].strategy is RetrievalStrategy.KEYWORD_SEARCH
    sc.fts_search_entities.assert_not_called()


async def test_flag_off_preserves_temporal_strategy():
    """TEMPORAL still wins over the keyword/semantic tail when the flag is off."""
    sc = _entity_match_sc()

    ctx = _classify_ctx(entity_retrieval=False, temporal_window=timedelta(days=30))
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    plan = ctx.data["retrieval_plan"]
    assert plan.strategy is RetrievalStrategy.TEMPORAL
    assert plan.search_param_overrides["freshness_decay_days"] == 30
    sc.fts_search_entities.assert_not_called()


async def test_flag_off_preserves_recent_context_strategy():
    """Recency-intent phrasing still routes to RECENT_CONTEXT when the flag is off."""
    sc = _entity_match_sc()

    ctx = _classify_ctx(entity_retrieval=False)
    ctx.data["query"] = "what did i decide about Comet 0002 recently"
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["retrieval_plan"].strategy is RetrievalStrategy.RECENT_CONTEXT
    sc.fts_search_entities.assert_not_called()


async def test_flag_on_still_short_circuits_to_entity_lookup():
    """Explicit True keeps the ENTITY_LOOKUP short-circuit intact."""
    sc = _entity_match_sc()

    ctx = _classify_ctx(entity_retrieval=True)
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    plan = ctx.data["retrieval_plan"]
    assert plan.strategy is RetrievalStrategy.ENTITY_LOOKUP
    assert plan.skip_embedding is True
    assert plan.skip_scored_search is True
    assert len(ctx.data["filtered_rows"]) > 0


async def test_absent_key_defaults_to_enabled():
    """No ``entity_retrieval`` in ctx.data ⇒ enabled (internal callers unaffected)."""
    sc = _entity_match_sc()

    ctx = _classify_ctx()
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["retrieval_plan"].strategy is RetrievalStrategy.ENTITY_LOOKUP
    sc.fts_search_entities.assert_called_once()


# ---------------------------------------------------------------------------
# Gate 2 — ParallelEmbedAndEntityBoost skips hop-boosting
# ---------------------------------------------------------------------------


async def test_flag_off_skips_entity_boost(caplog):
    """Flag off → boost helper never invoked; embedding still on the critical path."""
    caplog.set_level(logging.INFO, logger=_BOOST_LOGGER)

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    boost_mock = AsyncMock(return_value=({uuid4()}, {uuid4(): 1.3}))

    ctx = _boost_ctx({"entity_retrieval": False})
    with patch(_EMBED_PATH, side_effect=fast_embed), patch(_BOOST_PATH, boost_mock):
        await ParallelEmbedAndEntityBoost().execute(ctx)

    boost_mock.assert_not_called()
    assert ctx.data["embedding"] == _DUMMY_EMBED
    assert ctx.data["boosted_memory_ids"] == set()
    assert ctx.data["memory_boost_factor"] == {}
    assert any(
        "disabled by org setting search.entity_retrieval" in r.message
        for r in caplog.records
    )


async def test_flag_off_log_reason_wins_over_decline(caplog):
    """Flag off + CAURA-698 decline → skipped, and logged as the org disable.

    Both reasons suppress the boost; the log must name the deliberate switch so
    ops don't misread a configured disable as the precision heuristic firing.
    """
    caplog.set_level(logging.INFO, logger=_BOOST_LOGGER)

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    boost_mock = AsyncMock(return_value=(set(), {}))

    ctx = _boost_ctx({"entity_retrieval": False, "entity_match_declined": True})
    with patch(_EMBED_PATH, side_effect=fast_embed), patch(_BOOST_PATH, boost_mock):
        await ParallelEmbedAndEntityBoost().execute(ctx)

    boost_mock.assert_not_called()
    assert any(
        "disabled by org setting search.entity_retrieval" in r.message
        for r in caplog.records
    )
    assert not any("declined as over-broad" in r.message for r in caplog.records)


async def test_flag_on_still_boosts():
    """Explicit True → prior behaviour intact: boost results flow into ctx.data."""
    boosted = {uuid4()}
    factors = {next(iter(boosted)): 1.3}

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    boost_mock = AsyncMock(return_value=(boosted, factors))

    ctx = _boost_ctx({"entity_retrieval": True})
    with patch(_EMBED_PATH, side_effect=fast_embed), patch(_BOOST_PATH, boost_mock):
        await ParallelEmbedAndEntityBoost().execute(ctx)

    boost_mock.assert_called_once()
    assert ctx.data["boosted_memory_ids"] == boosted
    assert ctx.data["memory_boost_factor"] == factors


# ---------------------------------------------------------------------------
# Gate 3 — the deprecated legacy search path honours the flag too
# ---------------------------------------------------------------------------


async def test_legacy_path_flag_off_skips_entity_boost():
    """``_search_memories_legacy`` with the flag off never runs the entity pipeline."""
    from core_api.services import memory_service

    sc = AsyncMock()
    sc.scored_search = AsyncMock(return_value=[])
    sc.get_entity_links_for_memories = AsyncMock(return_value={})

    boost_mock = AsyncMock(return_value=({uuid4()}, {uuid4(): 1.3}))

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    with (
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch.object(memory_service, "_get_or_cache_embedding", side_effect=fast_embed),
        patch.object(memory_service, "_entity_boost_pipeline", boost_mock),
    ):
        results = await memory_service._search_memories_legacy(
            "t1", _ENTITY_QUERY, entity_retrieval=False
        )

    assert results == []
    boost_mock.assert_not_called()
    # No hop-boost payload reaches storage on the disabled path.
    assert sc.scored_search.call_args[0][0]["boosted_memory_ids"] is None


async def test_legacy_path_flag_on_runs_entity_boost():
    """Default/True on the legacy path keeps the entity boost wired up."""
    from core_api.services import memory_service

    sc = AsyncMock()
    sc.scored_search = AsyncMock(return_value=[])
    sc.get_entity_links_for_memories = AsyncMock(return_value={})

    mid = uuid4()
    boost_mock = AsyncMock(return_value=({mid}, {mid: 1.3}))

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    with (
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch.object(memory_service, "_get_or_cache_embedding", side_effect=fast_embed),
        patch.object(memory_service, "_entity_boost_pipeline", boost_mock),
    ):
        await memory_service._search_memories_legacy("t1", _ENTITY_QUERY)

    boost_mock.assert_called_once()
    assert sc.scored_search.call_args[0][0]["boosted_memory_ids"] == {str(mid): 1.3}


# ---------------------------------------------------------------------------
# Org setting — resolution, defaults, validation
# ---------------------------------------------------------------------------


def test_default_is_unblocked():
    """No tenant override ⇒ entity retrieval stays ON."""
    assert ResolvedConfig({}).entity_retrieval is True
    assert ResolvedConfig({"search": {}}).entity_retrieval is True
    assert DEFAULT_SETTINGS["search"]["entity_retrieval"] is None


def test_tenant_override_blocks():
    assert (
        ResolvedConfig({"search": {"entity_retrieval": False}}).entity_retrieval
        is False
    )
    assert (
        ResolvedConfig({"search": {"entity_retrieval": True}}).entity_retrieval is True
    )


def test_env_default_is_the_fallback():
    """Unset tenant key falls back to ``ENTITY_RETRIEVAL_ENABLED`` (fleet-wide switch)."""
    with patch(
        "core_api.services.organization_settings.global_settings.entity_retrieval_enabled",
        False,
    ):
        assert ResolvedConfig({}).entity_retrieval is False
        # An explicit tenant override still wins over the env default.
        assert (
            ResolvedConfig({"search": {"entity_retrieval": True}}).entity_retrieval
            is True
        )


def test_settings_key_accepted_and_type_checked():
    payload = {"search": {"entity_retrieval": False}}
    _check_keys(payload, DEFAULT_SETTINGS)
    _validate_leaf_types(payload)

    with pytest.raises(ValueError, match="entity_retrieval"):
        _validate_leaf_types({"search": {"entity_retrieval": "false"}})

    with pytest.raises(ValueError, match="Unknown settings key"):
        _check_keys({"search": {"entity_retreival": False}}, DEFAULT_SETTINGS)
