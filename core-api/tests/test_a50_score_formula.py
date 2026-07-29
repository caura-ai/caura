"""A50 unified — score_formula flag: core-api plumbing.

Storage behaviour (the unified formula actually re-orders results) is covered by
core-storage-api/tests/test_integration.py::test_scored_search_unified_formula.
"""

from __future__ import annotations

import pytest

from core_api.constants import SCORE_FORMULA
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.resolve_search_profile import ResolveSearchProfile
from core_api.services.organization_settings import validate_search_profile


def test_default_legacy() -> None:
    assert SCORE_FORMULA == 0, "must default to the legacy formula (no behaviour change)"


def test_validate_search_profile_score_formula() -> None:
    assert validate_search_profile({"score_formula": 1}) == {"score_formula": 1}
    assert validate_search_profile({"score_formula": 9})["score_formula"] == 1  # clamped
    assert validate_search_profile({"score_formula": -3})["score_formula"] == 0  # clamped


@pytest.mark.asyncio
async def test_resolve_defaults_legacy() -> None:
    ctx = PipelineContext(data={"query": "what is my role", "top_k": 5})
    await ResolveSearchProfile().execute(ctx)
    assert ctx.data["search_params"]["score_formula"] == 0


@pytest.mark.asyncio
async def test_resolve_honours_profile() -> None:
    ctx = PipelineContext(
        data={"query": "what is my role", "top_k": 5, "search_profile": {"score_formula": 1}}
    )
    await ResolveSearchProfile().execute(ctx)
    assert ctx.data["search_params"]["score_formula"] == 1
