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


async def test_keyword_strategy_keeps_the_global_semantic_floor():
    step = ResolveSearchProfile()
    ctx = PipelineContext(
        data={"query": "JWT expiry", "top_k": 5, "search_profile": None},
        tenant_config=None,
    )

    await step.execute(ctx)

    assert ctx.data["search_params"]["min_similarity"] == MIN_SEARCH_SIMILARITY
    assert ctx.data["allow_fts_global_floor_bypass"] is True


async def test_keyword_strategy_keeps_a_tuned_floor():
    step = ResolveSearchProfile()
    ctx = PipelineContext(
        data={
            "query": "JWT expiry",
            "top_k": 5,
            "search_profile": {"min_similarity": 0.55},
        },
        tenant_config=None,
    )

    await step.execute(ctx)

    assert ctx.data["search_params"]["min_similarity"] == 0.55
    assert ctx.data["allow_fts_global_floor_bypass"] is False


async def test_keyword_strategy_keeps_a_tenant_floor_strict():
    step = ResolveSearchProfile()
    ctx = PipelineContext(
        data={"query": "JWT expiry", "top_k": 5, "search_profile": None},
        tenant_config=ResolvedConfig(
            {"search": {"default_profile": {"min_similarity": 0.42}}}
        ),
    )

    await step.execute(ctx)

    assert ctx.data["search_params"]["min_similarity"] == 0.42
    assert ctx.data["allow_fts_global_floor_bypass"] is False


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


def test_search_profile_update_is_derived_from_the_knob_table():
    """The request model must expose exactly the agent-tunable knobs, with their bounds.

    It is now built by ``create_model`` over ``SEARCH_KNOBS``, so this cannot
    drift the way it did before (#730: ``graph_max_hops`` was capped at 3 here
    while the table allowed 5). Kept as a cheap check that the derivation still
    produces a usable model — a botched comprehension would yield a model with
    the wrong field set or no constraints at all, which nothing else would catch.
    """
    from common.constants import AGENT_TUNABLE_KEYS, SEARCH_KNOBS
    from core_api.schemas import SearchProfileUpdate

    assert set(SearchProfileUpdate.model_fields) == set(AGENT_TUNABLE_KEYS)
    for name, field in SearchProfileUpdate.model_fields.items():
        lo = next((m.ge for m in field.metadata if hasattr(m, "ge")), None)
        hi = next((m.le for m in field.metadata if hasattr(m, "le")), None)
        assert (lo, hi) == SEARCH_KNOBS[name].bounds, f"{name}: {(lo, hi)} != {SEARCH_KNOBS[name].bounds}"


def test_caura_tune_signature_matches_the_knob_table():
    """The MCP tool is the LAST hand-written copy of this surface — pin it.

    ``caura_tune`` declares its knobs as named parameters, which a reusable
    model cannot express: the signature IS the tool schema that ships to clients
    in ``plugin/tools.json``. So it stays hand-written, and this is what stops it
    drifting from the table the way ``SearchProfileUpdate`` did.

    Also checks the human-readable ranges in each parameter's description, since
    those are what an agent actually reads before choosing a value — a bound that
    moves in the table while the description still advertises the old one is a
    silently wrong instruction, not just stale prose.
    """
    import inspect
    import re

    from common.constants import AGENT_TUNABLE_KEYS, SEARCH_KNOBS
    from core_api.mcp_server import caura_tune

    params = [p for p in inspect.signature(caura_tune).parameters if p != "agent_id"]
    assert set(params) == set(AGENT_TUNABLE_KEYS), (
        f"caura_tune exposes {sorted(set(params) ^ set(AGENT_TUNABLE_KEYS))} "
        f"differently from the knob table"
    )

    hints = inspect.get_annotations(caura_tune, eval_str=True)
    checked = 0
    for name in params:
        meta = getattr(hints[name], "__metadata__", ())
        desc = next((getattr(m, "description", "") for m in meta), "") or ""
        # Only parameters whose description IS a range, e.g. "1-20." or "0.1-0.9.".
        m = re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\.?", desc.strip())
        if not m:
            continue
        lo, hi = SEARCH_KNOBS[name].bounds
        assert (float(m.group(1)), float(m.group(2))) == (float(lo), float(hi)), (
            f"caura_tune documents {name} as {desc!r} but the knob table says {(lo, hi)}"
        )
        checked += 1
    assert checked >= 6, f"only {checked} descriptions parsed as ranges — the format changed"


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
