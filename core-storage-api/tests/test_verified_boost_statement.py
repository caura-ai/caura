"""EC-19 — ``verified_boost`` statement-shape pins, no database.

Same capture-and-compile technique as ``test_a41_recall_boost_source_statement``:
statements are intercepted on their way to ``session.execute``.

* knob 0 (default): the scored-search statement never mentions
  ``verification_status`` and is identical — text AND bound values — to the
  statement built from a payload with no EC-19 keys at all, i.e. from a core-api
  that predates the knob. That is the "off changes nothing" contract the EC-18
  A/B relies on: until a tenant opts in, nobody's ranking moves.
* knob 1: ``verification_status`` rides the fenced ``ingredients`` CTE, a CASE
  maps it to the configured factors, and that factor multiplies ``score`` under
  BOTH formulas and is projected as ``verification_boost``.
"""

from __future__ import annotations

import contextlib
import re

import pytest
from sqlalchemy.dialects import postgresql

import core_storage_api.services.postgres_service as ps
from common.constants import SEARCH_KNOBS, VERIFIED_BOOST_DEFAULT_FACTORS

pytestmark = pytest.mark.asyncio

_EC19_KEYS = ("verified_boost", *VERIFIED_BOOST_DEFAULT_FACTORS)


def _base_params() -> dict:
    sp = {k: kn.value_type(kn.bounds[0]) for k, kn in SEARCH_KNOBS.items() if k not in _EC19_KEYS}
    sp["fts_rank_scale"] = 6.0
    return sp


async def _capture(monkeypatch: pytest.MonkeyPatch, sp: dict) -> tuple[str, dict]:
    """Run memory_scored_search against a capturing session; return SQL + binds."""
    captured: list = []

    class _Stop(Exception):
        pass

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            if "set_config" in str(stmt):
                return None  # the search's statement_timeout comes first
            captured.append(stmt)
            raise _Stop

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ps, "get_read_session", _fake_session)
    with contextlib.suppress(_Stop):
        await ps.PostgresService().memory_scored_search(
            tenant_id="t",
            embedding=[0.1] * 1024,
            query="connection pool sizing",
            search_params=sp,
            top_k=10,
        )
    assert captured, "no statement reached session.execute"
    compiled = captured[0].compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def _assert_no_boost(sql: str) -> None:
    # The outer query selects the whole ``Memory`` row, so the column name
    # itself appears; what must not is the ingredient or the factor.
    assert "AS verification_status" not in sql
    assert "ingredients.verification_status" not in sql
    assert "verification_boost" not in sql


@pytest.mark.parametrize("formula", [0, 1], ids=["legacy", "unified"])
async def test_off_is_identical_to_a_payload_without_the_knob(monkeypatch, formula) -> None:
    absent = {**_base_params(), "score_formula": formula}
    off = {
        **absent,
        "verified_boost": 0,
        **{k: SEARCH_KNOBS[k].bounds[1] for k in VERIFIED_BOOST_DEFAULT_FACTORS},
    }
    sql_absent, binds_absent = await _capture(monkeypatch, absent)
    sql_off, binds_off = await _capture(monkeypatch, off)
    assert sql_off == sql_absent
    assert binds_off == binds_absent
    _assert_no_boost(sql_off)


@pytest.mark.parametrize("value", [2, -1, None])
async def test_anything_but_one_fails_closed(monkeypatch, value) -> None:
    sql, _ = await _capture(monkeypatch, {**_base_params(), "verified_boost": value})
    _assert_no_boost(sql)


@pytest.mark.parametrize("formula", [0, 1], ids=["legacy", "unified"])
async def test_on_multiplies_the_score_by_the_factor(monkeypatch, formula) -> None:
    sp = {**_base_params(), "score_formula": formula, "verified_boost": 1}
    sql, binds = await _capture(monkeypatch, sp)
    assert "memories.verification_status AS verification_status" in sql
    assert "END AS verification_boost" in sql
    # The CASE multiplies ``score`` under either formula, as its last factor.
    match = re.search(r"SELECT ingredients\.mem_id AS mem_id, (.*?) AS score, ", sql, re.DOTALL)
    assert match, "scored select not found"
    score_expr = match.group(1)
    assert score_expr.count("ingredients.verification_status = %(verification_status_1)s") == 1
    assert score_expr.rstrip().endswith("END")
    assert "scored.verification_boost" in sql
    # Factors absent from the payload fall back to the shared defaults.
    for factor in VERIFIED_BOOST_DEFAULT_FACTORS.values():
        assert factor in binds.values()


async def test_on_uses_the_tenant_factors(monkeypatch) -> None:
    sp = {
        **_base_params(),
        "verified_boost": 1,
        "verified_boost_human": 1.37,
        "verified_boost_disputed": 0.61,
    }
    _, binds = await _capture(monkeypatch, sp)
    assert 1.37 in binds.values()
    assert 0.61 in binds.values()
    assert VERIFIED_BOOST_DEFAULT_FACTORS["verified_boost_human"] not in binds.values()


async def test_on_keeps_the_render_counts(monkeypatch) -> None:
    """The status column rides the ``ingredients`` CTE; the heavy primitives
    render exactly as often as on the default path."""
    sql0, _ = await _capture(monkeypatch, _base_params())
    sql1, _ = await _capture(monkeypatch, {**_base_params(), "verified_boost": 1})
    assert sql1.count("<=>") == sql0.count("<=>") == 1
    assert sql1.count("ts_rank_cd") == sql0.count("ts_rank_cd")
