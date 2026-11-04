"""EC-19 — optional ranking boost for verified memories (Evidence contract v1).

A per-tenant search knob, ``search.default_profile.verified_boost``, OFF by
default. On, the scored search multiplies each row's score by a bounded factor
for its ``verification_status`` (``human_verified`` 1.10, ``tool_observed`` and
``corroborated`` 1.05, ``disputed`` 0.90, ``unverified`` 1.0 — all tunable per
tenant within the ``SEARCH_KNOBS`` bounds). It stays off until the EC-18
regression-bench A/B clears it: a ranking change has cost 13 points in one
category before (A50).

Pinned here:

* the knob table, its resolution and the tenant-settings round trip;
* OFF ranks byte-for-byte as before — same ids, same order, same scores as a
  payload that never heard of the knob (the statement-level twin of this is
  ``core-storage-api/tests/test_verified_boost_statement.py``);
* ON reorders two otherwise-tied rows, and reports its factor in
  ``score_parts.verification_boost`` and the ``why`` phrase.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import update

import core_storage_api.services.postgres_service as ps
from common.constants import (
    AGENT_TUNABLE_KEYS,
    SEARCH_KNOBS,
    SQL_SCORING_PARAM_KEYS,
    SQL_SCORING_REQUIRED_KEYS,
    VERIFIED_BOOST_DEFAULT_FACTORS,
)
from common.embedding import fake_embedding
from common.models.memory import Memory

_EC19_KEYS = ("verified_boost", *VERIFIED_BOOST_DEFAULT_FACTORS)


# ── knob table, resolution, settings round trip ─────────────────────────────


@pytest.mark.unit
def test_knobs_cross_the_wire_optionally_and_are_not_agent_tunable():
    for key in _EC19_KEYS:
        assert key in SQL_SCORING_PARAM_KEYS
        assert key not in SQL_SCORING_REQUIRED_KEYS
        assert key not in AGENT_TUNABLE_KEYS
    assert SEARCH_KNOBS["verified_boost"].value_type is int
    assert SEARCH_KNOBS["verified_boost"].bounds == (0, 1)


@pytest.mark.unit
def test_default_factors_sit_inside_their_bounds():
    for key, factor in VERIFIED_BOOST_DEFAULT_FACTORS.items():
        lo, hi = SEARCH_KNOBS[key].bounds
        assert lo <= factor <= hi
    assert VERIFIED_BOOST_DEFAULT_FACTORS["verified_boost_disputed"] < 1.0


def _resolve(tenant_config=None):
    from core_api.services.memory_service import resolve_search_params

    return resolve_search_params(None, query="q", top_k=5, tenant_config=tenant_config)


@pytest.mark.unit
def test_resolves_off_with_default_factors():
    params = _resolve()
    assert params["verified_boost"] == 0
    for key, factor in VERIFIED_BOOST_DEFAULT_FACTORS.items():
        assert params[key] == factor


@pytest.mark.unit
def test_settings_round_trip_turns_it_on_and_reset_turns_it_off():
    from common.organization_settings_merge import deep_merge
    from core_api.services.organization_settings import (
        ResolvedConfig,
        _validate_default_search_profile,
    )

    update_ = {
        "search": {
            "default_profile": {"verified_boost": 1, "verified_boost_human": 1.2}
        }
    }
    _validate_default_search_profile(update_)
    stored = deep_merge({}, update_)

    params = _resolve(ResolvedConfig(tenant_settings=stored))
    assert params["verified_boost"] == 1
    assert params["verified_boost_human"] == 1.2
    assert (
        params["verified_boost_tool"]
        == VERIFIED_BOOST_DEFAULT_FACTORS["verified_boost_tool"]
    )

    reset = deep_merge(
        stored, {"search": {"default_profile": {"verified_boost": None}}}
    )
    assert _resolve(ResolvedConfig(tenant_settings=reset))["verified_boost"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("verified_boost", 2, r"in \[0, 1\]"),
        ("verified_boost", True, "must be int"),
        ("verified_boost_human", 2.0, r"in \[1.0, 1.5\]"),
        ("verified_boost_tool", 0.9, r"in \[1.0, 1.5\]"),
        ("verified_boost_disputed", 1.2, r"in \[0.5, 1.0\]"),
    ],
)
def test_tenant_default_rejects_out_of_bounds(key, value, match):
    from core_api.services.organization_settings import _validate_default_search_profile

    with pytest.raises(ValueError, match=match):
        _validate_default_search_profile({"search": {"default_profile": {key: value}}})


# ── score_parts and the why phrase ──────────────────────────────────────────


@pytest.mark.unit
def test_score_parts_carries_the_factor_only_when_reported():
    from core_api.pipeline.steps.search.load_and_serialize import _score_parts

    off = SimpleNamespace(vec_sim=0.8, fts_score=0.0, has_embedding=True)
    on = SimpleNamespace(
        vec_sim=0.8, fts_score=0.0, has_embedding=True, verification_boost=1.05
    )
    assert _score_parts(off).verification_boost is None
    assert _score_parts(on).verification_boost == 1.05


@pytest.mark.unit
def test_why_phrase_says_verified_only_for_a_boosted_row():
    from core_api.schemas import ScoreParts
    from core_api.services.evidence import why_phrase

    assert (
        why_phrase(ScoreParts(vec_sim=0.8, verification_boost=1.05))
        == "vector match, verified"
    )
    assert why_phrase(ScoreParts(vec_sim=0.8, verification_boost=1.0)) == "vector match"
    assert why_phrase(ScoreParts(vec_sim=0.8, verification_boost=0.9)) == "vector match"
    assert why_phrase(ScoreParts(vec_sim=0.8)) == "vector match"


# ── against Postgres ─────────────────────────────────────────────────────────

_SP = {
    "fts_weight": 0.3,
    "freshness_floor": 0.7,
    "freshness_decay_days": 90,
    "recall_boost_cap": 1.1,
    "recall_decay_window_days": 14,
    "similarity_blend": 0.85,
    "fts_rank_scale": 6.0,
}
# One vector for every row and a query that matches none lexically, so the base
# score ties and only the factor under test can separate the rows.
_SHARED = fake_embedding("shared vector so only verification can separate the rows")
_NEUTRAL_QUERY = "zzznomatchzzz"
_AS_OF = datetime(2030, 1, 1, tzinfo=UTC)


async def _insert(tenant_id: str, content: str, status: str) -> str:
    from core_api.clients.storage_client import get_storage_client

    mem = await get_storage_client().create_memory(
        {
            "tenant_id": tenant_id,
            "fleet_id": None,
            "agent_id": "test-agent",
            "memory_type": "fact",
            "content": content,
            "weight": 0.5,
            "embedding": _SHARED,
            "content_hash": hashlib.sha256(
                f"{tenant_id}:{content}".encode()
            ).hexdigest(),
            "status": "active",
            "visibility": "scope_team",
        }
    )
    # Server-owned: no write sets it, so the test moves it directly on the row.
    async with ps.get_session() as session:
        await session.execute(
            update(Memory)
            .where(Memory.id == mem["id"])
            .values(verification_status=status)
        )
    return str(mem["id"])


async def _search(tenant_id: str, valid_at=None, **extra_sp):
    rows = await ps.PostgresService().memory_scored_search(
        tenant_id=tenant_id,
        embedding=_SHARED,
        query=_NEUTRAL_QUERY,
        search_params={**_SP, **extra_sp},
        top_k=10,
        valid_at=valid_at,
    )
    return rows


def _shape(rows):
    return [(str(r.Memory.id), float(r.score)) for r in rows]


@pytest.fixture
async def seeded(tenant_id):
    # Inserted oldest first: on a score tie the newest row ranks first, so with
    # the boost off the unverified rows lead and the verified ones trail.
    ids = {}
    for status in ("human_verified", "tool_observed", "disputed", "unverified"):
        ids[status] = await _insert(
            tenant_id, f"the release checklist note ({status})", status
        )
    return tenant_id, ids


@pytest.mark.integration
@pytest.mark.parametrize("formula", [0, 1], ids=["legacy", "unified"])
async def test_off_ranks_exactly_as_without_the_knob(seeded, formula):
    """Same ids, same order, same scores — to the last bit.

    Freshness is measured against a clock, and under the default clock that is
    ``now()``, which moves between two queries. Pinning the clock to a fixed
    ``valid_at`` (``freshness_reference=1``) makes the score a pure function of
    the row, so equality can be exact rather than approximate; the default
    clock is checked below with a tolerance for those microseconds.
    """
    tenant, _ = seeded
    off_keys = {
        "verified_boost": 0,
        **{k: SEARCH_KNOBS[k].bounds[1] for k in VERIFIED_BOOST_DEFAULT_FACTORS},
    }
    pinned = {"score_formula": formula, "freshness_reference": 1, "valid_at": _AS_OF}

    absent = _shape(await _search(tenant, **pinned))
    off = _shape(await _search(tenant, **pinned, **off_keys))
    assert off == absent

    absent_now = _shape(await _search(tenant, score_formula=formula))
    off_now = _shape(await _search(tenant, score_formula=formula, **off_keys))
    assert [i for i, _ in off_now] == [i for i, _ in absent_now]
    assert [s for _, s in off_now] == pytest.approx(
        [s for _, s in absent_now], rel=1e-6
    )

    assert all(r.verification_boost is None for r in await _search(tenant, **off_keys))


@pytest.mark.integration
@pytest.mark.parametrize("formula", [0, 1], ids=["legacy", "unified"])
async def test_on_orders_by_verification_and_scales_by_the_factor(seeded, formula):
    tenant, ids = seeded
    off = dict(_shape(await _search(tenant, score_formula=formula)))
    assert len({round(s, 9) for s in off.values()}) == 1, "base scores must tie"
    base = next(iter(off.values()))

    rows = await _search(tenant, score_formula=formula, verified_boost=1)
    by_id = {str(r.Memory.id): r for r in rows}
    assert [str(r.Memory.id) for r in rows] == [
        ids["human_verified"],
        ids["tool_observed"],
        ids["unverified"],
        ids["disputed"],
    ]
    expected = {
        "human_verified": VERIFIED_BOOST_DEFAULT_FACTORS["verified_boost_human"],
        "tool_observed": VERIFIED_BOOST_DEFAULT_FACTORS["verified_boost_tool"],
        "unverified": 1.0,
        "disputed": VERIFIED_BOOST_DEFAULT_FACTORS["verified_boost_disputed"],
    }
    for status, factor in expected.items():
        row = by_id[ids[status]]
        assert float(row.verification_boost) == pytest.approx(factor)
        assert float(row.score) == pytest.approx(base * factor)


@pytest.mark.integration
async def test_on_reads_the_tenant_factors(seeded):
    tenant, ids = seeded
    rows = await _search(
        tenant, verified_boost=1, verified_boost_tool=1.4, verified_boost_human=1.0
    )
    assert str(rows[0].Memory.id) == ids["tool_observed"]
    assert float(rows[0].verification_boost) == pytest.approx(1.4)
