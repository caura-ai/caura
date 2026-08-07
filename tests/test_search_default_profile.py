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


# ── fts_rank_scale (#687) ──


def _profile(**kw) -> dict:
    """The shape ``_validate_default_search_profile`` actually reads.

    It takes the whole settings payload and digs out
    ``search.default_profile``; handed a bare dict it finds nothing and returns
    without validating, which is how a bounds test can pass while asserting
    nothing.
    """
    return {"search": {"default_profile": dict(kw)}}


def test_fts_rank_scale_is_tenant_overridable():
    """It joins the other ranking knobs rather than being a hardcoded literal."""
    _validate_default_search_profile(_profile(fts_rank_scale=3.0))
    rc = ResolvedConfig({"search": {"default_profile": {"fts_rank_scale": 3.0}}})
    assert rc.default_search_profile["fts_rank_scale"] == 3.0


def test_fts_rank_scale_floor_is_one_not_zero():
    """1.0 is the pre-#687 formula — the documented revert — and the floor.

    Below 1.0 would weaken keyword relevance below where it has always been,
    which nothing measured supports and no caller should reach by accident.
    """
    with pytest.raises(ValueError, match="fts_rank_scale"):
        _validate_default_search_profile(_profile(fts_rank_scale=0.5))
    _validate_default_search_profile(_profile(fts_rank_scale=1.0))  # the revert, allowed


def test_fts_rank_scale_ceiling_matches_what_was_measured():
    """The ceiling is the largest value the LoCoMo sweep covered; above is untested."""
    _validate_default_search_profile(_profile(fts_rank_scale=20.0))
    with pytest.raises(ValueError, match="fts_rank_scale"):
        _validate_default_search_profile(_profile(fts_rank_scale=20.001))


# ── agent-facing ingress vs the knob table ──


def test_agent_tunable_bounds_match_the_knob_table():
    """``SearchProfileUpdate``'s bounds must equal ``SEARCH_KNOBS``'.

    The request model still writes its own ``ge``/``le`` per field rather than
    deriving them, and it drifted: ``graph_max_hops`` was capped at 3 here while
    the table allowed 5, so a tenant-wide default could hold a depth no agent
    profile could ever set. Resolved to 3 on 2026-08-07 — the ingress value, not
    the widest of the three, because depth drives graph-expansion cost.

    Asserts the bounds rather than merely the names, since matching names with
    different limits is exactly the failure that hid for months.
    """
    from common.constants import SEARCH_KNOBS
    from core_api.schemas import SearchProfileUpdate

    checked = 0
    for name, field in SearchProfileUpdate.model_fields.items():
        assert name in SEARCH_KNOBS, (
            f"SearchProfileUpdate exposes {name!r}, which is not a declared search knob"
        )
        lo = next((m.ge for m in field.metadata if hasattr(m, "ge")), None)
        hi = next((m.le for m in field.metadata if hasattr(m, "le")), None)
        assert (lo, hi) == SEARCH_KNOBS[name].bounds, (
            f"{name}: SearchProfileUpdate allows {(lo, hi)} but SEARCH_KNOBS declares "
            f"{SEARCH_KNOBS[name].bounds} — the tenant-default path and the agent path would "
            f"accept different values for the same knob"
        )
        checked += 1

    assert checked == 9, f"expected 9 agent-tunable knobs, checked {checked}"


def test_the_three_ab_knobs_are_not_agent_tunable():
    """``fts_rank_scale`` / ``candidate_pool_size`` / ``score_formula`` stay off the ingress.

    They are the A/B knobs — held at their global defaults until the offline
    comparison validates them, and flipped per TENANT via ``default_profile``,
    not per agent. The 9-of-12 split is deliberate; this records which three and
    why, so a future reader does not "fix" the omission.
    """
    from common.constants import SEARCH_KNOBS
    from core_api.schemas import SearchProfileUpdate

    assert set(SEARCH_KNOBS) - set(SearchProfileUpdate.model_fields) == {
        "fts_rank_scale",
        "candidate_pool_size",
        "score_formula",
    }
