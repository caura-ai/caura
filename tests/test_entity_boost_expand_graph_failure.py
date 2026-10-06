"""oss-0909-l-03 — ``expand_graph`` failure must not discard the FTS seeds.

``_entity_boost_via_storage`` ran ``expand_graph`` inside the same broad
``try`` as every other lookup, so a storage error there fell to the
catch-all ("falling back to pure vector search") and threw away the
entity-FTS matches already in hand: no hop-boost at all for the request.
Since #1444 ``ClassifyQuery._expand_per_fleet`` degrades the same failure to
hop-0 seeds; this path now does too.

Pure unit tests, no DB.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import GRAPH_HOP_BOOST
from core_api.pipeline.steps.search.parallel_embed_entity_boost import (
    _entity_boost_via_storage,
)

pytestmark = [pytest.mark.unit]

_SC_PATH = (
    "core_api.pipeline.steps.search.parallel_embed_entity_boost.get_storage_client"
)
_ENTITY_QUERY = "What is Comet 0002's launch_date?"


async def _run(sc: AsyncMock):
    with patch(_SC_PATH, return_value=sc):
        return await _entity_boost_via_storage(
            _ENTITY_QUERY,
            "t1",
            None,
            graph_expand=True,
            graph_max_hops=2,
        )


def _sc(seed: str, memory_id: str) -> AsyncMock:
    sc = AsyncMock()
    sc.fts_search_entities = AsyncMock(return_value=[seed])
    sc.expand_graph = AsyncMock(side_effect=RuntimeError("storage down"))
    sc.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[{"memory_id": memory_id, "entity_id": seed, "role": "subject"}]
    )
    return sc


async def test_expand_graph_failure_keeps_seeds_boosted_at_hop0(caplog):
    seed, memory_id = str(uuid4()), str(uuid4())
    sc = _sc(seed, memory_id)

    with caplog.at_level(logging.WARNING):
        boosted, factor = await _run(sc)

    sc.expand_graph.assert_awaited_once()
    # The link lookup is made for the seed, not skipped.
    sc.get_memory_ids_by_entity_ids.assert_awaited_once_with([seed], "t1")
    assert {str(m) for m in boosted} == {memory_id}
    assert [factor[m] for m in boosted] == [GRAPH_HOP_BOOST[0]]
    # The log names the sub-call that failed.
    assert "expand_graph failed" in caplog.text
    assert "pure vector search" not in caplog.text


async def test_link_lookup_failure_still_falls_back_and_names_it(caplog):
    seed = str(uuid4())
    sc = _sc(seed, str(uuid4()))
    sc.expand_graph = AsyncMock(return_value={seed: {"hop": 0, "weight": 1.0}})
    sc.get_memory_ids_by_entity_ids = AsyncMock(side_effect=RuntimeError("down"))

    with caplog.at_level(logging.WARNING):
        boosted, factor = await _run(sc)

    assert (boosted, factor) == (set(), {})
    assert "memory-link lookup failed" in caplog.text
