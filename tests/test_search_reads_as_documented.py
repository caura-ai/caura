"""Search answers by the rules its contract states (audit 2026-10-01, B33).

The core-api half of the batch's search-correctness PR. Each case here failed
on main:

- the entity-lookup route dropped a tuned ``min_similarity`` floor (L-112) and
  ``include_derived=false`` (L-16);
- a row with no embedding answered ``similarity: 0.0``, storage's sentinel,
  where the contract says null (L-113);
- a reranked row carried no trace of the score that ordered it (L-115);
- STM rows were prepended past ``top_k`` and every filter, at a cosine of 1.0
  nothing measured (L-14);
- the hop boost expanded a multi-fleet request tenant-wide (L-114), and asked
  storage again for what the classifier had just been told (L-175);
- the successor lookup read the home tenant only (L-43), and the link lookup
  went to the writer (L-190).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.classify_query import ClassifyQuery
from core_api.pipeline.steps.search.inject_stm_context import InjectSTMContext
from core_api.pipeline.steps.search.load_and_serialize import (
    LoadAndSerialize,
    _score_parts,
)
from core_api.pipeline.steps.search.parallel_embed_entity_boost import (
    ParallelEmbedAndEntityBoost,
    _entity_boost_via_storage,
)
from core_api.pipeline.steps.search.rerank_results import RerankResults
from core_api.pipeline.steps.search.resolve_search_profile import (
    ResolveSearchProfile,
)
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy

pytestmark = [pytest.mark.unit]

_CLASSIFY_SC = "core_api.pipeline.steps.search.classify_query.get_storage_client"
_BOOST_SC = (
    "core_api.pipeline.steps.search.parallel_embed_entity_boost.get_storage_client"
)
_EMBED = (
    "core_api.pipeline.steps.search.parallel_embed_entity_boost._get_or_cache_embedding"
)
_SERIALIZE_SC = "core_api.pipeline.steps.search.load_and_serialize.get_storage_client"

_TOP_K = 3
# Tokens the entity index would match, as in test_entity_retrieval_flag.
_QUERY = "What is Comet 0002's launch_date?"
_FANOUT = {"parent_memory_id": "p1", "source": "atomic_fact_fanout"}


def _entity_sc(pool: int = _TOP_K, *, derived: int = 0) -> AsyncMock:
    """A storage client whose entity index links ``pool`` memories, the first
    ``derived`` of them atomic-fact fan-out children."""
    eid = str(uuid4())
    mids = [str(uuid4()) for _ in range(pool)]
    sc = AsyncMock()
    sc.fts_search_entities = AsyncMock(return_value=[eid])
    sc.expand_graph = AsyncMock(return_value={eid: {"hop": 0, "weight": 1.0}})
    sc.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[
            {"memory_id": m, "entity_id": eid, "role": "subject"} for m in mids
        ]
    )
    sc.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": m,
                "tenant_id": "t1",
                "content": f"Comet {i:04d} launch_date is 2026-01-01",
                "memory_type": "fact",
                "metadata_": dict(_FANOUT) if i < derived else {},
            }
            for i, m in enumerate(mids)
        ]
    )
    return sc


def _search_ctx(**data) -> PipelineContext:
    return PipelineContext(
        data={
            "query": _QUERY,
            "tenant_id": "t1",
            "tenant_config": None,
            "search_params": {"graph_max_hops": 2, "top_k": _TOP_K, "fts_weight": 0.3},
            **data,
        }
    )


def _scored_row(vec_sim, score=1.0, status="active", has_embedding=True, **factors):
    """A row as ExecuteScoredSearch builds one (after test_d12_search_diagnostics)."""
    return SimpleNamespace(
        Memory=SimpleNamespace(
            id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            agent_display_name=None,
            memory_type="fact",
            title="row",
            content=factors.pop("content", "c"),
            weight=0.5,
            source_uri=None,
            run_id=None,
            metadata_=None,
            created_at="2026-08-25T00:00:00Z",
            expires_at=None,
            subject_entity_id=None,
            predicate=None,
            object_value=None,
            ts_valid_start=None,
            ts_valid_end=None,
            status=status,
            visibility="scope_fleet",
            recall_count=0,
            last_recalled_at=None,
            supersedes_id=None,
        ),
        score=score,
        similarity=None,
        vec_sim=vec_sim,
        fts_score=factors.get("fts_score"),
        freshness=factors.get("freshness"),
        entity_boost=factors.get("entity_boost"),
        recall_boost=factors.get("recall_boost"),
        temporal_boost=factors.get("temporal_boost"),
        status_penalty=factors.get("status_penalty"),
        has_embedding=has_embedding,
        entity_links=[],
    )


# ── L-112: a tuned floor is not skipped by the entity route ─────────────────


async def _resolved_and_classified(
    sc, *, tenant_config=None, **data
) -> PipelineContext:
    ctx = PipelineContext(
        data={
            "query": _QUERY,
            "tenant_id": "t1",
            "top_k": _TOP_K,
            "top_k_explicit": True,
            **data,
        },
        tenant_config=tenant_config,
    )
    with patch(_CLASSIFY_SC, return_value=sc):
        await ResolveSearchProfile().execute(ctx)
        await ClassifyQuery().execute(ctx)
    return ctx


@pytest.mark.parametrize(
    "floor",
    [
        {"min_similarity_override": 0.4},
        {"search_profile": {"min_similarity": 0.4}},
        {
            "tenant_config": SimpleNamespace(
                default_search_profile={"min_similarity": 0.4}
            )
        },
    ],
    ids=["request", "agent-profile", "tenant-default"],
)
async def test_l112_a_tuned_floor_falls_through_to_scored_search(floor):
    """The entity route builds rows with no cosine, so it cannot apply a floor:
    the caller who named one gets scored search, with the matched entities as a
    hop boost, as a dated query does."""
    ctx = await _resolved_and_classified(_entity_sc(), **floor)

    assert ctx.data["retrieval_plan"].strategy != RetrievalStrategy.ENTITY_LOOKUP
    assert ctx.data["_classified_entity_hops"], "the match still boosts"


async def test_l112_the_untuned_global_floor_keeps_the_entity_route():
    ctx = await _resolved_and_classified(_entity_sc())

    assert ctx.data["retrieval_plan"].strategy == RetrievalStrategy.ENTITY_LOOKUP


# ── L-16: include_derived holds on the entity route ─────────────────────────


async def test_l16_the_entity_route_leaves_out_fanout_children_when_asked():
    """PostFilterResults, the only step that applied it, is skipped on this
    route. The pool here has one row to spare, so it still fills top_k."""
    ctx = _search_ctx(include_derived=False)
    with patch(_CLASSIFY_SC, return_value=_entity_sc(_TOP_K + 1, derived=1)):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["retrieval_plan"].strategy == RetrievalStrategy.ENTITY_LOOKUP
    rows = ctx.data["filtered_rows"]
    assert len(rows) == _TOP_K
    assert all(r.Memory.metadata_ != _FANOUT for r in rows)


# ── L-113: no embedding, no cosine ──────────────────────────────────────────


async def test_l113_a_hit_with_no_embedding_has_no_similarity():
    """Storage scores a missing vector 0.0, a sentinel rather than a measurement,
    and ``has_embedding`` says which it is."""
    row = _scored_row(0.0, score=0.3, has_embedding=False, fts_score=0.4)
    ctx = PipelineContext(data={"filtered_rows": [row], "tenant_id": "t1"})
    await LoadAndSerialize().execute(ctx)

    out = ctx.data["results"][0]
    assert out.similarity is None
    assert out.score_parts.vec_sim is None
    assert out.score_parts.fts_score == 0.4


async def test_l113_the_legacy_path_answers_the_same():
    """``_USE_PIPELINE_SEARCH = False`` is the hotfix lever; flipping it must not
    bring the sentinel back."""
    from core_api.services import memory_service

    sc = AsyncMock()
    sc.scored_search = AsyncMock(
        return_value=[
            {
                "id": str(uuid4()),
                "tenant_id": "t1",
                "agent_id": "a1",
                "memory_type": "fact",
                "content": "comet launch notes",
                "weight": 0.5,
                "source_uri": None,
                "run_id": None,
                "metadata_": {},
                "created_at": "2026-08-25T00:00:00Z",
                "expires_at": None,
                "status": "active",
                "visibility": "scope_team",
                "recall_count": 0,
                "score": 0.3,
                "vec_sim": 0.0,
                "has_embedding": False,
                "fts_match": True,
            }
        ]
    )
    sc.get_entity_links_for_memories = AsyncMock(return_value={})

    async def fast_embed(*args, **kwargs):
        return [0.1] * 1536

    with (
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch.object(memory_service, "_get_or_cache_embedding", side_effect=fast_embed),
    ):
        results = await memory_service._search_memories_legacy(
            "t1", "comet launch", entity_retrieval=False
        )

    assert [r.similarity for r in results] == [None]


# ── L-115: a reranked row says what ordered it ──────────────────────────────


async def test_l115_a_reranked_row_carries_the_rerankers_score():
    first = _scored_row(0.9, score=1.2, content="totally unrelated")
    match = _scored_row(0.1, score=0.3, content="alpha alpha alpha")
    ctx = PipelineContext(
        data={
            "raw_rows": [first, match],
            "query": "alpha",
            "tenant_config": SimpleNamespace(rank_enabled=True, rank_provider="fake"),
        }
    )
    await RerankResults().execute(ctx)

    assert ctx.data["raw_rows"] == [match, first]
    parts = [_score_parts(row) for row in ctx.data["raw_rows"]]
    assert parts[0].rerank > parts[1].rerank
    # ``score`` stays the first-stage composite, as its description says.
    assert [row.score for row in ctx.data["raw_rows"]] == [0.3, 1.2]


async def test_l115_an_unreranked_row_has_no_rerank_score():
    assert _score_parts(_scored_row(0.9)).rerank is None


# ── L-14: STM rows fit the page and its filters ─────────────────────────────


async def _stm_ctx(**data) -> PipelineContext:
    import core_api.services.stm_service as svc

    svc._stm_instance = None
    stm = svc.get_stm_backend_instance()
    for i in range(10):
        await stm.post_note(
            "t1",
            "agent-1",
            {
                "id": f"note-{i}",
                "agent_id": "agent-1",
                "content": f"STM note {i}",
                "memory_type": "fact",
                "metadata": {},
                "posted_at": "2026-04-07T14:00:00Z",
            },
        )
    ctx = PipelineContext(
        data={
            "tenant_id": "t1",
            "caller_agent_id": "agent-1",
            "fleet_ids": None,
            "results": [],
            "search_params": {"top_k": _TOP_K},
            **data,
        }
    )
    with patch("core_api.config.settings") as mock_settings:
        mock_settings.use_stm = True
        await InjectSTMContext().execute(ctx)
    return ctx


def _stm_rows(ctx: PipelineContext) -> list:
    return [r for r in ctx.data["results"] if (r.metadata or {}).get("source") == "stm"]


async def test_l14_stm_rows_fit_top_k_and_claim_no_cosine():
    ctx = await _stm_ctx()

    rows = _stm_rows(ctx)
    assert len(rows) == _TOP_K
    assert all(r.similarity is None for r in rows)


@pytest.mark.parametrize(
    "search_filter",
    [{"memory_type_filter": "decision"}, {"status_filter": "active"}],
    ids=["memory-type", "status"],
)
async def test_l14_a_filtered_search_gets_no_stm_rows(search_filter):
    """An STM row has type ``stm`` and no status, so it matches neither."""
    assert _stm_rows(await _stm_ctx(**search_filter)) == []


# ── L-114: the hop boost expands each requested fleet on its own ────────────


async def test_l114_the_boost_expands_each_fleet_as_the_classifier_does():
    """A two-fleet request expanded tenant-wide (``fleet_id`` None), through
    relations of fleets the caller did not name."""
    sc = _entity_sc()
    with patch(_BOOST_SC, return_value=sc):
        await _entity_boost_via_storage(
            _QUERY, "t1", ["f1", "f2"], True, 2, use_union=True
        )

    fleets = sorted(c.args[0]["fleet_id"] for c in sc.expand_graph.await_args_list)
    assert fleets == ["f1", "f2"]


async def test_l114_the_legacy_boost_does_too():
    from core_api.services import memory_service

    sc = _entity_sc()
    with patch.object(memory_service, "get_storage_client", return_value=sc):
        await memory_service._entity_boost_pipeline(
            _QUERY, "t1", ["f1", "f2"], True, 2, use_union=True
        )

    fleets = sorted(c.args[0]["fleet_id"] for c in sc.expand_graph.await_args_list)
    assert fleets == ["f1", "f2"]


# ── L-175: the boost reuses what the classifier was told ────────────────────


async def _classified_and_boosted(sc, **data) -> PipelineContext:
    ctx = _search_ctx(**data)
    with (
        patch(_CLASSIFY_SC, return_value=sc),
        patch(_BOOST_SC, return_value=sc),
        patch(_EMBED, AsyncMock(return_value=[0.1] * 1536)),
    ):
        await ClassifyQuery().execute(ctx)
        await ParallelEmbedAndEntityBoost().execute(ctx)
    return ctx


async def test_l175_an_entity_search_that_matched_nothing_asks_once():
    sc = _entity_sc()
    sc.fts_search_entities = AsyncMock(return_value=[])

    ctx = await _classified_and_boosted(sc)

    assert sc.fts_search_entities.await_count == 1
    assert ctx.data["boosted_memory_ids"] == set()


async def test_l175_an_underfilled_pool_reuses_its_links():
    """One linked memory cannot fill top_k 3, so the search falls through, and
    the boost needs the links the classifier fetched a moment earlier."""
    sc = _entity_sc(pool=1)

    ctx = await _classified_and_boosted(sc)

    assert sc.get_memory_ids_by_entity_ids.await_count == 1
    assert len(ctx.data["boosted_memory_ids"]) == 1


# ── L-43: successors are looked for where the search read ───────────────────


async def test_l43_the_successor_lookup_reads_the_callers_tenants():
    """A cross-tenant key searches every readable tenant; the stale rows it gets
    back are corrected only if their successors are looked for there too."""
    sc = AsyncMock()
    sc.find_successors = AsyncMock(return_value=[])
    ctx = PipelineContext(
        data={
            "filtered_rows": [_scored_row(0.9, status="outdated")],
            "tenant_id": "t1",
            "readable_tenant_ids": ["t1", "t2"],
        }
    )
    with patch(_SERIALIZE_SC, return_value=sc):
        await LoadAndSerialize().execute(ctx)

    assert sc.find_successors.await_args.args[0]["readable_tenant_ids"] == ["t1", "t2"]


# ── L-190: the link lookup is a read ────────────────────────────────────────


async def test_l190_the_link_lookup_goes_to_the_reader():
    from core_api.clients.storage_client import CoreStorageClient

    client = CoreStorageClient.__new__(CoreStorageClient)
    client._post = AsyncMock(return_value=[])

    await client.get_memory_ids_by_entity_ids(["e1"], "t1")

    assert client._post.await_args.kwargs.get("read") is True
