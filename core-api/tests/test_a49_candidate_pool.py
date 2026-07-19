"""A49 — cosine-dominant candidate pool: core-api plumbing.

Covers the two core-api touch points:
  * ``validate_search_profile`` accepts / clamps the new ``candidate_pool_size`` knob.
  * ``ResolveSearchProfile`` threads it into ``search_params`` (default 0 = off).

The storage-side behaviour (pool selected by similarity) is covered by
core-storage-api/tests/test_integration.py::test_scored_search_candidate_pool_selects_by_similarity.
"""

from __future__ import annotations

import pytest

from core_api.constants import CANDIDATE_POOL_SIZE
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.resolve_search_profile import ResolveSearchProfile
from core_api.services.organization_settings import validate_search_profile


def test_default_off() -> None:
    assert CANDIDATE_POOL_SIZE == 0, "A49 must ship OFF by default (no behaviour change)"


def test_validate_search_profile_accepts_valid_pool_size() -> None:
    assert validate_search_profile({"candidate_pool_size": 50}) == {"candidate_pool_size": 50}


def test_validate_search_profile_clamps_out_of_range() -> None:
    assert validate_search_profile({"candidate_pool_size": 500})["candidate_pool_size"] == 200
    assert validate_search_profile({"candidate_pool_size": -5})["candidate_pool_size"] == 0


def test_validate_search_profile_drops_wrong_type() -> None:
    # rule default is None -> a wrong-typed value is dropped, not defaulted
    assert "candidate_pool_size" not in validate_search_profile({"candidate_pool_size": "big"})


@pytest.mark.asyncio
async def test_resolve_defaults_pool_size_off() -> None:
    ctx = PipelineContext(data={"query": "what is my role", "top_k": 5})
    await ResolveSearchProfile().execute(ctx)
    assert ctx.data["search_params"]["candidate_pool_size"] == 0


@pytest.mark.asyncio
async def test_resolve_honours_profile_pool_size() -> None:
    ctx = PipelineContext(
        data={"query": "what is my role", "top_k": 5, "search_profile": {"candidate_pool_size": 50}}
    )
    await ResolveSearchProfile().execute(ctx)
    assert ctx.data["search_params"]["candidate_pool_size"] == 50
