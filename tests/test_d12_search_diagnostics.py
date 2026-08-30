"""D12 — search diagnostics wired end-to-end.

Covers the pieces this change added:

- ``ResolveSearchProfile`` honours a per-request ``min_similarity`` override
  (request beats profile beats tenant beats constant).
- ``PostFilterResults`` captures the full candidate trace + exclusion tallies
  when ``diagnostic`` is set, without changing the returned rows.
- ``TrackRecalls`` never bumps ``recall_count`` on a diagnostic call —
  inspection must not reinforce the signal being inspected.
- ``LoadAndSerialize`` surfaces the ranking composite (``score``) and its
  factor breakdown (``score_parts``) on scored hits, and omits them on
  unscored rows.
"""

from types import SimpleNamespace
from uuid import uuid4

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import track_recalls as tr
from core_api.pipeline.steps.search.load_and_serialize import (
    LoadAndSerialize,
    _score_parts,
)
from core_api.pipeline.steps.search.post_filter_results import PostFilterResults
from core_api.pipeline.steps.search.resolve_search_profile import ResolveSearchProfile


def _scored_row(vec_sim, score=1.0, status="active", has_embedding=True, **factors):
    return SimpleNamespace(
        Memory=SimpleNamespace(
            id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            agent_display_name=None,
            memory_type="fact",
            title="row",
            content="c",
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


# --- ResolveSearchProfile: request-level min_similarity override --------------


async def test_min_similarity_override_beats_profile():
    ctx = PipelineContext(
        data={
            "query": "q",
            "top_k": 5,
            "search_profile": {"min_similarity": 0.5},
            "min_similarity_override": 0.9,
        },
        tenant_config=None,
    )
    await ResolveSearchProfile().execute(ctx)
    assert ctx.data["search_params"]["min_similarity"] == 0.9
    assert ctx.data["allow_fts_global_floor_bypass"] is False


async def test_profile_min_similarity_kept_without_override():
    ctx = PipelineContext(
        data={
            "query": "q",
            "top_k": 5,
            "search_profile": {"min_similarity": 0.5},
            "min_similarity_override": None,
        },
        tenant_config=None,
    )
    await ResolveSearchProfile().execute(ctx)
    assert ctx.data["search_params"]["min_similarity"] == 0.5
    assert ctx.data["allow_fts_global_floor_bypass"] is False


# --- PostFilterResults: diagnostic trace --------------------------------------


async def test_diagnostic_capture_reasons_and_counts():
    rows = [
        _scored_row(0.9, score=1.2),  # kept
        _scored_row(0.8, score=1.1),  # kept
        _scored_row(0.7, score=1.0),  # trimmed by top_k
        _scored_row(0.1, score=0.2),  # below floor
    ]
    ctx = PipelineContext(
        data={
            "search_params": {"min_similarity": 0.3},
            "raw_rows": rows,
            "final_top_k": 2,
            "diagnostic": True,
        }
    )
    await PostFilterResults().execute(ctx)

    assert len(ctx.data["filtered_rows"]) == 2  # results identical to a normal call
    trace = ctx.data["diagnostic_results"]
    assert [c["excluded"] for c in trace] == [
        None,
        None,
        "trimmed_by_top_k",
        "below_min_similarity",
    ]
    # score factors survive into the trace
    assert trace[0]["score"] == 1.2 and trace[0]["vec_sim"] == 0.9
    counts = ctx.data["diagnostic_counts"]
    assert counts == {
        "candidates_considered": 4,
        "returned": 2,
        "excluded_below_min_similarity": 1,
        "excluded_by_top_k_trim": 1,
    }


async def test_no_diagnostic_keys_on_normal_call():
    ctx = PipelineContext(
        data={
            "search_params": {"min_similarity": 0.3},
            "raw_rows": [_scored_row(0.9)],
            "final_top_k": 5,
            "diagnostic": False,
        }
    )
    await PostFilterResults().execute(ctx)
    assert "diagnostic_results" not in ctx.data
    assert "diagnostic_counts" not in ctx.data


# --- TrackRecalls: diagnostic never reinforces --------------------------------


async def test_track_recalls_skips_diagnostic(monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "track_task", lambda c: (calls.append(c), c.close()))
    ctx = PipelineContext(
        data={
            "caller_agent_id": "brandclaw",
            "diagnostic": True,
            "filtered_rows": [SimpleNamespace(Memory=SimpleNamespace(id=uuid4()))],
        }
    )
    await tr.TrackRecalls().execute(ctx)
    assert calls == []  # diagnostic is inspection, not use


async def test_track_recalls_still_counts_normal(monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "track_task", lambda c: (calls.append(c), c.close()))
    ctx = PipelineContext(
        data={
            "caller_agent_id": "brandclaw",
            "diagnostic": False,
            "filtered_rows": [SimpleNamespace(Memory=SimpleNamespace(id=uuid4()))],
        }
    )
    await tr.TrackRecalls().execute(ctx)
    assert len(calls) == 1


# --- LoadAndSerialize: score + score_parts on the wire -------------------------


async def test_serialize_exposes_score_and_parts():
    row = _scored_row(
        0.87,
        score=1.4321,
        fts_score=0.2,
        freshness=0.9,
        status_penalty=1.0,
    )
    ctx = PipelineContext(data={"filtered_rows": [row], "tenant_id": "t1"})
    await LoadAndSerialize().execute(ctx)
    out = ctx.data["results"][0]
    assert out.similarity == 0.87
    assert out.score == 1.4321
    assert out.score_parts is not None
    assert out.score_parts.vec_sim == 0.87
    assert out.score_parts.fts_score == 0.2
    assert out.score_parts.freshness == 0.9
    assert out.score_parts.status_penalty == 1.0
    assert out.score_parts.entity_boost is None


def test_score_parts_none_when_unscored():
    unscored = SimpleNamespace(
        vec_sim=None,
        fts_score=None,
        freshness=None,
        entity_boost=None,
        recall_boost=None,
        temporal_boost=None,
        status_penalty=None,
    )
    assert _score_parts(unscored) is None
