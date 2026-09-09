"""``search.graph_retrieval`` (``graph_expand``) must gate the ENTITY_LOOKUP short-circuit.

Regression tests for the setting being ignored by ``ClassifyQuery``: the
short-circuit called ``expand_graph`` unconditionally, so a tenant with
``search.graph_retrieval=False`` still paid the traversal and got hop-1/2
neighbourhood memories as the ENTIRE answer (the short-circuit replaces
scored search). The temporal-decline path had the same hole with a twist:
the expanded hops it stashes in ``_classified_entity_hops`` feed the boost
step's ``precomputed_hops`` input, which bypasses that step's own
``graph_expand`` gate by design — so the gating has to happen where the
hops are produced.

The contract under test mirrors ``_entity_boost_via_storage``: with the
flag off, matched entities stay retrievable as hop-0 seeds (entity lookup
itself is governed by ``search.entity_retrieval``, not this flag), but no
``expand_graph`` roundtrip is issued and no hop>0 entity can enter the
pool. Absent key defaults to expansion ON, so internal callers that never
set it are unchanged.

Pure unit tests, no DB.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.classify_query import ClassifyQuery
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy

pytestmark = [pytest.mark.unit]

_CLASSIFY_SC_PATH = "core_api.pipeline.steps.search.classify_query.get_storage_client"

#: The short-circuit only takes the exclusive route when the entity pool can
#: fill top_k (H-03), so the fixture links exactly this many memories.
_TOP_K = 10

# Tokens that hit the fake entity index — a passing assertion then means the
# flag gated expansion, not that the tokenizer found nothing to look up.
_ENTITY_QUERY = "What is Comet 0002's launch_date?"


def _classify_ctx(**extra) -> PipelineContext:
    data = {
        "query": _ENTITY_QUERY,
        "tenant_id": "t1",
        "fleet_ids": ["fleet-1"],
        "search_params": {
            "graph_max_hops": 2,
            "top_k": _TOP_K,
            "fts_weight": 0.3,
        },
        **extra,
    }
    return PipelineContext(data=data)


def _entity_match_sc(matched_entity_id: UUID) -> AsyncMock:
    """Storage client where one entity matches FTS and links ``_TOP_K`` memories.

    ``expand_graph`` is armed to return a hop-1 neighbour so a test that
    reaches it would visibly change the pool — the assertions below are on
    it never being awaited, and on the neighbour's absence.
    """
    neighbour_id = uuid4()
    mids = [str(uuid4()) for _ in range(_TOP_K)]
    sc = AsyncMock()
    sc.fts_search_entities = AsyncMock(return_value=[str(matched_entity_id)])
    sc.expand_graph = AsyncMock(
        return_value={
            str(matched_entity_id): {"hop": 0, "weight": 1.0},
            str(neighbour_id): {"hop": 1, "weight": 0.8},
        }
    )
    sc.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[
            {"memory_id": mid, "entity_id": str(matched_entity_id), "role": "subject"}
            for mid in mids
        ]
    )
    sc.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": mid,
                "tenant_id": "t1",
                "content": f"Comet {i:04d} launch_date is 2026-01-01",
                "memory_type": "fact",
            }
            for i, mid in enumerate(mids)
        ]
    )
    return sc


async def test_short_circuit_with_graph_expand_off_uses_hop0_seeds():
    """Flag off → ENTITY_LOOKUP still fires, but from hop-0 seeds: zero
    ``expand_graph`` roundtrips, and only the matched entity feeds the pool."""
    eid = uuid4()
    sc = _entity_match_sc(eid)

    ctx = _classify_ctx(graph_expand=False)
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    plan = ctx.data["retrieval_plan"]
    assert plan.strategy is RetrievalStrategy.ENTITY_LOOKUP
    sc.expand_graph.assert_not_called()
    # The pool was collected from the hop-0 seed alone — the armed hop-1
    # neighbour could only appear via an expand_graph call.
    (link_call,) = sc.get_memory_ids_by_entity_ids.await_args_list
    assert set(link_call.args[0]) == {str(eid)}
    assert len(ctx.data["filtered_rows"]) == _TOP_K


async def test_short_circuit_expands_when_flag_absent():
    """No ``graph_expand`` key (internal callers) → expansion runs as before."""
    sc = _entity_match_sc(uuid4())

    ctx = _classify_ctx()
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["retrieval_plan"].strategy is RetrievalStrategy.ENTITY_LOOKUP
    sc.expand_graph.assert_awaited()


async def test_temporal_decline_stash_is_hop0_when_graph_expand_off():
    """Dated entity query with the flag off → the short-circuit declines as
    before (temporal constraints need scored search), and the hop stash left
    for the boost step is hop-0 only. That stash is consumed via
    ``precomputed_hops``, which skips the boost step's own gate — so anything
    other than hop-0 here would smuggle expansion past a disabled flag."""
    eid = uuid4()
    sc = _entity_match_sc(eid)

    ctx = _classify_ctx(graph_expand=False, temporal_window=timedelta(days=30))
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["retrieval_plan"].strategy is not RetrievalStrategy.ENTITY_LOOKUP
    sc.expand_graph.assert_not_called()
    assert ctx.data["_classified_entity_hops"] == {eid: (0, 1.0)}


async def test_temporal_decline_stash_expands_when_flag_on():
    """Same dated query with the flag on → the stash carries the expanded
    neighbourhood (hop-1 entity present), pinning that the gate did not
    over-reach into the enabled path."""
    eid = uuid4()
    sc = _entity_match_sc(eid)

    ctx = _classify_ctx(graph_expand=True, temporal_window=timedelta(days=30))
    with patch(_CLASSIFY_SC_PATH, return_value=sc):
        await ClassifyQuery().execute(ctx)

    sc.expand_graph.assert_awaited()
    stash = ctx.data["_classified_entity_hops"]
    assert stash[eid] == (0, 1.0)
    assert any(hop == 1 for hop, _w in stash.values())
