"""SIDE-61 (lme-0929-l-08) — a tenant ``search.default_profile`` knob can be unset.

Before: after ``PUT {"search": {"default_profile": {"top_k": 10}}}`` there was
no way back to the global default. ``{"default_profile": {}}`` merges nothing
(documented no-op), ``{"default_profile": {"top_k": null}}`` was a 422 (the
strict profile validator type-checked ``None`` as a wrong-typed int), and the
documented ``{"default_profile": null}`` was a 422 too (``_check_keys`` demanded
an object).

After: ``null`` on a known knob (or on ``default_profile`` itself) is accepted,
and the storage merge DELETES the key instead of storing ``null``, so GET shows
it gone and the resolver falls back to the global default.
"""

import pytest

from common.organization_settings_merge import deep_merge, merge_settings_update
from core_api.constants import MIN_SEARCH_SIMILARITY
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.resolve_search_profile import ResolveSearchProfile
from core_api.services import organization_settings as ts_svc
from core_api.services.organization_settings import (
    DEFAULT_SETTINGS,
    ResolvedConfig,
    _check_keys,
    _validate_default_search_profile,
    validate_search_profile,
)
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid


@pytest.fixture(autouse=True)
def _reset_cache():
    ts_svc._settings_cache.clear()
    yield
    ts_svc._settings_cache.clear()


def _url(tid: str) -> str:
    return f"/api/v1/settings?tenant_id={tid}"


async def _get(client, tid, headers) -> dict:
    r = await client.get(_url(tid), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _put(client, tid, headers, body):
    ts_svc._settings_cache.clear()
    return await client.put(_url(tid), json=body, headers=headers)


async def _resolved_min_similarity(raw: dict) -> float:
    step = ResolveSearchProfile()
    ctx = PipelineContext(
        data={
            "query": "what is the compliance deadline",
            "top_k": 5,
            "search_profile": None,
        },
        tenant_config=ResolvedConfig(raw),
    )
    await step.execute(ctx)
    return ctx.data["search_params"]["min_similarity"]


# ── HTTP round-trips (real storage) ───────────────────────────────────────


async def test_null_unsets_one_profile_knob(client):
    tid, headers = get_test_auth(tenant_id=f"side61-{_uid()}")
    r = await _put(
        client,
        tid,
        headers,
        {"search": {"default_profile": {"top_k": 10, "min_similarity": 0.42}}},
    )
    assert r.status_code == 200, r.text

    r = await _put(
        client, tid, headers, {"search": {"default_profile": {"top_k": None}}}
    )
    assert r.status_code == 200, r.text
    # The PUT echo and a fresh GET both show the key GONE (not null) and the
    # sibling knob untouched.
    assert r.json()["search"]["default_profile"] == {"min_similarity": 0.42}
    dp = (await _get(client, tid, headers))["search"]["default_profile"]
    assert dp == {"min_similarity": 0.42}

    raw = await ts_svc.get_raw_settings(tid)
    assert "top_k" not in raw["search"]["default_profile"]
    assert "top_k" not in ResolvedConfig(raw).default_search_profile


async def test_null_unset_falls_back_to_the_global_default(client):
    tid, headers = get_test_auth(tenant_id=f"side61-{_uid()}")
    await _put(
        client, tid, headers, {"search": {"default_profile": {"min_similarity": 0.42}}}
    )
    assert await _resolved_min_similarity(await ts_svc.get_raw_settings(tid)) == 0.42

    r = await _put(
        client, tid, headers, {"search": {"default_profile": {"min_similarity": None}}}
    )
    assert r.status_code == 200, r.text
    raw = await ts_svc.get_raw_settings(tid)
    assert await _resolved_min_similarity(raw) == MIN_SEARCH_SIMILARITY


async def test_null_default_profile_drops_every_knob(client):
    """The shape the route docstring has documented since D17 — it was a 422."""
    tid, headers = get_test_auth(tenant_id=f"side61-{_uid()}")
    await _put(
        client,
        tid,
        headers,
        {"search": {"default_profile": {"top_k": 10, "fts_weight": 0.4}}},
    )

    r = await _put(client, tid, headers, {"search": {"default_profile": None}})
    assert r.status_code == 200, r.text
    assert (await _get(client, tid, headers))["search"]["default_profile"] == {}


async def test_empty_object_is_still_a_no_op(client):
    tid, headers = get_test_auth(tenant_id=f"side61-{_uid()}")
    await _put(client, tid, headers, {"search": {"default_profile": {"top_k": 10}}})

    r = await _put(client, tid, headers, {"search": {"default_profile": {}}})
    assert r.status_code == 200, r.text
    assert (await _get(client, tid, headers))["search"]["default_profile"] == {
        "top_k": 10
    }


async def test_null_unknown_knob_is_still_rejected(client):
    tid, headers = get_test_auth(tenant_id=f"side61-{_uid()}")
    r = await _put(
        client, tid, headers, {"search": {"default_profile": {"not_a_knob": None}}}
    )
    assert r.status_code == 422
    assert "unknown key" in r.text


async def test_wrong_type_is_still_rejected(client):
    tid, headers = get_test_auth(tenant_id=f"side61-{_uid()}")
    r = await _put(
        client, tid, headers, {"search": {"default_profile": {"top_k": "10"}}}
    )
    assert r.status_code == 422
    r = await _put(
        client, tid, headers, {"search": {"default_profile": {"top_k": True}}}
    )
    assert r.status_code == 422


async def test_null_leaf_elsewhere_is_deleted_not_stored(client):
    """The storage rule is general: a concrete-default leaf reset to null reads
    back as its DEFAULT, not as ``None`` (which is what storing null did)."""
    tid, headers = get_test_auth(tenant_id=f"side61-{_uid()}")
    await _put(client, tid, headers, {"skills_factory": {"body_max_bytes": 1234}})
    r = await _put(client, tid, headers, {"skills_factory": {"body_max_bytes": None}})
    assert r.status_code == 200, r.text
    display = await _get(client, tid, headers)
    assert (
        display["skills_factory"]["body_max_bytes"]
        == DEFAULT_SETTINGS["skills_factory"]["body_max_bytes"]
    )
    raw = await ts_svc.get_raw_settings(tid)
    assert "body_max_bytes" not in raw["skills_factory"]


async def test_null_on_a_section_is_still_rejected(client):
    """Only ``search.default_profile`` is a nullable object (resolver-safe)."""
    tid, headers = get_test_auth(tenant_id=f"side61-{_uid()}")
    r = await _put(client, tid, headers, {"search": None})
    assert r.status_code == 422


# ── pure helpers ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_validation_admits_null_for_known_knobs_only():
    _check_keys({"search": {"default_profile": None}}, DEFAULT_SETTINGS)
    _validate_default_search_profile({"search": {"default_profile": {"top_k": None}}})
    with pytest.raises(ValueError, match="unknown key"):
        _validate_default_search_profile(
            {"search": {"default_profile": {"bogus": None}}}
        )
    with pytest.raises(ValueError, match="must be an object"):
        _check_keys({"governance": None}, DEFAULT_SETTINGS)


@pytest.mark.unit
def test_merge_settings_update_deletes_on_null():
    stored = {
        "search": {
            "default_profile": {"top_k": 10, "fts_weight": 0.4},
            "recall_boost": False,
        }
    }
    out = merge_settings_update(
        stored, {"search": {"default_profile": {"top_k": None}, "recall_boost": None}}
    )
    assert out == {"search": {"default_profile": {"fts_weight": 0.4}}}
    # A null inside a brand-new subtree is not stored either.
    assert merge_settings_update(
        {}, {"search": {"default_profile": {"top_k": None}}}
    ) == {"search": {"default_profile": {}}}
    # The input is not mutated.
    assert stored["search"]["default_profile"]["top_k"] == 10


@pytest.mark.unit
def test_display_merge_still_keeps_null_values():
    """``deep_merge`` (display) is unchanged: a legacy stored null must not
    drop a schema key from the display view."""
    assert deep_merge({"a": 1}, {"a": None}) == {"a": None}


@pytest.mark.unit
def test_legacy_stored_null_knob_is_dropped_on_read():
    assert validate_search_profile({"top_k": None, "min_similarity": 0.5}) == {
        "min_similarity": 0.5
    }
