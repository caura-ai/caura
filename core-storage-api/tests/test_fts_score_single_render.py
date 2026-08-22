"""``fts_score`` must not name ``ts_rank_cd`` more times than it has to.

The property under test is invisible in the Python source: ``x / (1 + x)`` and
``1 - 1/(1 + x)`` read almost identically, but when ``x`` is an *expression*
rather than a bound value, SQLAlchemy inlines it at every site that names it and
Postgres evaluates the ranking function once per site. Only the compiled SQL
shows it, so that is what these assert — and they assert it on the statement the
service actually issues, not just on the factor in isolation, because the factor
being clean is not the same as the query being clean. See ``_saturate_rank`` for
the measurements.

No database: the statement is captured on its way to ``session.execute`` and
compiled, so this is a pure unit test despite living beside the integration
suite.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import func
from sqlalchemy.dialects import postgresql

import core_storage_api.services.postgres_service as ps
from common.constants import SEARCH_KNOBS
from common.models import Memory
from core_storage_api.services.postgres_service import _saturate_rank

# What the shipped ``memory_scored_search`` statement renders today. Both
# directions matter: a rise means someone added a reference and put the per-row
# cost back, a fall means the inner-projection work landed. Either way,
# re-measure and move the number deliberately rather than loosening the check.
_EXPECTED_TS_RANK_CD_RENDERS = 9
_EXPECTED_TSQUERY_RENDERS = 18


def _scaled_rank():
    """The production argument: a real ``ts_rank_cd`` call, not a bound value."""
    return 6.0 * func.ts_rank_cd(
        Memory.search_vector, func.plainto_tsquery("english", "connection pool sizing")
    )


def _render(expr) -> str:
    # No ``literal_binds``: the regconfig 'english' has no literal renderer, and
    # only the shape matters here, not the parameter values.
    return str(expr.compile(dialect=postgresql.dialect()))


def _calls(sql: str, fn: str = "ts_rank_cd") -> int:
    """Count real CALLS of ``fn``.

    Not a bare substring count: SQLAlchemy names the scale's bind parameter
    after the function it multiplies (``%(ts_rank_cd_1)s``), so a plain
    ``sql.count("ts_rank_cd")`` double-counts every call. The trailing paren is
    what distinguishes an invocation from a parameter name.
    """
    return sql.count(f"{fn}(")


# ── the saturating factor in isolation ─────────────────────────────────────


def test_the_saturating_factor_names_the_rank_once() -> None:
    """Reverting ``_saturate_rank`` to ``x / (1 + x)`` fails here."""
    sql = _render(_saturate_rank(_scaled_rank()))
    assert _calls(sql) == 1, (
        f"the saturating factor must name ts_rank_cd once; compiled SQL calls it {_calls(sql)} times:\n{sql}"
    )


def test_the_naive_form_really_does_render_twice() -> None:
    """Control: proves ``_calls`` discriminates 1 from 2 for the right reason.

    Without it the test above could pass vacuously — if SQLAlchemy common-
    subexpression-eliminated repeated references, or if ``_calls`` were simply
    blind to the second one, a genuinely double-rendering expression would still
    count 1. This pins that the difference is real and observable.
    """
    scaled = _scaled_rank()
    naive = _render(scaled / (1.0 + scaled))
    assert _calls(naive) == 2, f"the pre-fix expression should double-render; got {_calls(naive)}"


@pytest.mark.parametrize("rank", [0.0, 1e-9, 0.1, 1.0, 1e6])
def test_the_two_forms_agree_to_within_one_ulp(rank: float) -> None:
    """Algebraically equal, so the change must not move any score meaningfully.

    Deliberately NOT asserted as exact equality: measured over every matching row
    of the benchmark corpus the forms differ by up to 5.55e-17, one ULP near 0.5.
    The bound here is the tightest tolerance anything in the stack asserts on this
    value (1e-12), which the difference clears by ~4 orders of magnitude. Goes
    through ``_saturate_rank`` rather than a retyped copy of its body, so editing
    the helper actually breaks this.
    """
    got = _saturate_rank(rank)
    assert got == pytest.approx(rank / (1.0 + rank), abs=1e-12)
    # fts_score shares a scale with cosine vec_sim; it must stay in [0, 1).
    assert 0.0 <= got < 1.0, f"rank {rank} mapped outside [0, 1): {got}"


# ── the statement the service actually issues ──────────────────────────────


async def _compiled_scored_search_sql(monkeypatch: pytest.MonkeyPatch) -> str:
    """Compile ``memory_scored_search``'s statement without touching a DB."""
    captured: list = []

    class _Stop(Exception):
        pass

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            captured.append(stmt)
            raise _Stop

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ps, "get_read_session", _fake_session)

    # The lower bound of each knob's range is enough — the statement's SHAPE is
    # under test, not its constants — but the scale must be a real one, since
    # 1.0 is a documented revert path that could compile differently.
    search_params = {k: kn.value_type(kn.bounds[0]) for k, kn in SEARCH_KNOBS.items()}
    search_params["fts_rank_scale"] = 6.0

    with contextlib.suppress(_Stop):
        await ps.PostgresService().memory_scored_search(
            tenant_id="t",
            embedding=[0.1] * 1536,
            query="connection pool sizing",
            search_params=search_params,
            top_k=10,
        )
    assert captured, "no statement reached session.execute"
    return str(captured[0].compile(dialect=postgresql.dialect()))


async def test_the_shipped_statement_holds_its_ts_rank_cd_render_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant that actually protects the hot path.

    The isolated-factor test above passes whenever ``_saturate_rank`` is clean,
    which says nothing about how many times the assembled query names
    ``fts_score``. This counts the real thing: 18 before the change, 9 after.
    """
    sql = await _compiled_scored_search_sql(monkeypatch)
    assert _calls(sql) == _EXPECTED_TS_RANK_CD_RENDERS, (
        f"memory_scored_search renders ts_rank_cd {_calls(sql)} times, expected "
        f"{_EXPECTED_TS_RANK_CD_RENDERS}. Up means a new reference to fts_score "
        f"(or to similarity/score, which inline it) put the per-row cost back; "
        f"down means it improved. Re-measure and update the constant either way."
    )


async def test_the_shipped_statement_holds_its_tsquery_render_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``plainto_tsquery`` rides along with every ``ts_rank_cd`` reference.

    Tracked separately because it also appears in the FTS guard and the
    exact-lexical-match CASE, so the two counts move independently.
    """
    sql = await _compiled_scored_search_sql(monkeypatch)
    got = _calls(sql, "plainto_tsquery")
    assert got == _EXPECTED_TSQUERY_RENDERS, (
        f"memory_scored_search renders plainto_tsquery {got} times, expected "
        f"{_EXPECTED_TSQUERY_RENDERS} (27 before the single-render change)."
    )
