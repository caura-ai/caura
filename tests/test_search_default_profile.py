"""Unit tests for the tenant-wide default search profile (A47).

Covers the three pieces of the change:
  * ``ResolvedConfig.default_search_profile`` accessor (read + sanitise)
  * ``_check_keys`` / ``_validate_default_search_profile`` (write validation)
  * ``ResolveSearchProfile`` precedence: agent > tenant default > constant

Pure logic — no DB.
"""

import pytest

from core_api.constants import MIN_SEARCH_SIMILARITY
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.resolve_search_profile import ResolveSearchProfile
from core_api.services.organization_settings import (
    DEFAULT_SETTINGS,
    ResolvedConfig,
    _check_keys,
    _validate_default_search_profile,
)


# ── ResolvedConfig.default_search_profile ──


def test_default_profile_empty_by_default():
    rc = ResolvedConfig({})
    assert rc.default_search_profile == {}


def test_default_profile_reads_override():
    rc = ResolvedConfig({"search": {"default_profile": {"min_similarity": 0.42}}})
    assert rc.default_search_profile == {"min_similarity": 0.42}


def test_default_profile_sanitises_out_of_range_on_read():
    # 0.99 is above the validate_search_profile ceiling (0.9) → clamped, never crashes.
    rc = ResolvedConfig({"search": {"default_profile": {"min_similarity": 0.99}}})
    assert rc.default_search_profile["min_similarity"] == 0.9


# ── write-path validation ──


def test_check_keys_allows_default_profile():
    # Must not raise: default_profile is a declared (open) sub-object.
    _check_keys(
        {"search": {"default_profile": {"min_similarity": 0.4, "top_k": 10}}},
        DEFAULT_SETTINGS,
    )


def test_validate_default_profile_accepts_valid():
    _validate_default_search_profile(
        {"search": {"default_profile": {"min_similarity": 0.4, "top_k": 8}}}
    )


def test_validate_default_profile_accepts_int_for_float():
    # min_similarity is float-typed; a bare int (0) must be accepted (→ 0.0-ish),
    # but 0 is below the 0.1 floor so it should raise on range, not on type.
    with pytest.raises(ValueError, match="in \\[0.1, 0.9\\]"):
        _validate_default_search_profile(
            {"search": {"default_profile": {"min_similarity": 0}}}
        )


def test_validate_default_profile_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown key"):
        _validate_default_search_profile({"search": {"default_profile": {"bogus": 1}}})


def test_validate_default_profile_rejects_wrong_type():
    with pytest.raises(ValueError, match="must be float"):
        _validate_default_search_profile(
            {"search": {"default_profile": {"min_similarity": "hi"}}}
        )


def test_validate_default_profile_rejects_bool_for_float():
    with pytest.raises(ValueError, match="must be float"):
        _validate_default_search_profile(
            {"search": {"default_profile": {"min_similarity": True}}}
        )


def test_validate_default_profile_rejects_out_of_range():
    with pytest.raises(ValueError, match="in \\[0.1, 0.9\\]"):
        _validate_default_search_profile(
            {"search": {"default_profile": {"min_similarity": 0.95}}}
        )


def test_validate_default_profile_noop_when_absent():
    _validate_default_search_profile({"search": {"recall_boost": True}})
    _validate_default_search_profile({})


# ── ResolveSearchProfile precedence: agent > tenant default > constant ──


async def _resolve(tenant_config, agent_profile):
    step = ResolveSearchProfile()
    ctx = PipelineContext(
        data={
            "query": "what is the compliance deadline",
            "top_k": 5,
            "search_profile": agent_profile,
        },
        tenant_config=tenant_config,
    )
    await step.execute(ctx)
    return ctx.data["search_params"]


async def test_precedence_no_tenant_config_uses_constant():
    params = await _resolve(tenant_config=None, agent_profile=None)
    assert params["min_similarity"] == MIN_SEARCH_SIMILARITY


async def test_precedence_tenant_default_fills_gap():
    tc = ResolvedConfig({"search": {"default_profile": {"min_similarity": 0.42}}})
    params = await _resolve(tenant_config=tc, agent_profile=None)
    assert params["min_similarity"] == 0.42


async def test_precedence_agent_profile_overrides_tenant_default():
    tc = ResolvedConfig({"search": {"default_profile": {"min_similarity": 0.42}}})
    params = await _resolve(tenant_config=tc, agent_profile={"min_similarity": 0.55})
    assert params["min_similarity"] == 0.55


async def test_precedence_empty_tenant_default_is_neutral():
    tc = ResolvedConfig({})  # no default_profile
    params = await _resolve(tenant_config=tc, agent_profile=None)
    assert params["min_similarity"] == MIN_SEARCH_SIMILARITY
