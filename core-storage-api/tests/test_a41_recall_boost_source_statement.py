"""A41 — ``recall_boost_source`` statement-shape pins, no database.

Same capture-and-compile technique as ``test_fts_score_single_render`` /
``test_ann_pool_statement``: statements are intercepted on their way to
``session.execute``, so these are pure unit tests. Three shapes matter:

* knob 0 (default): the scored-search statement must be byte-free of the
  confirmed-use machinery — "off means unchanged" is the rollback story, and
  the boost keeps dividing ``recall_count`` (bump-on-return) exactly as today.
* knob 1: the ``ingredients`` CTE additionally extracts
  ``metadata._system.recall_used_count`` / ``recall_used_at``, and the
  ``recall_boost`` term divides the USED counter — merely being returned no
  longer moves the number the score reads, which is what breaks the
  returned→boosted→returned loop (A26/A41). The boost's shape (cap, window,
  saturation scale) is untouched.
* ``evolve_apply_weights``: without ``mark_used`` it issues exactly the
  statements it issues today; with it, one extra UPDATE in the SAME
  transaction bumps the counter under the ``_system`` platform namespace,
  tenant-scoped and skipping soft-deleted rows.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy.dialects import postgresql

import core_storage_api.services.postgres_service as ps
from common.constants import SEARCH_KNOBS

pytestmark = pytest.mark.asyncio


async def _capture_scored_search(monkeypatch: pytest.MonkeyPatch, **extra_sp) -> str:
    """Run memory_scored_search against a capturing session; return compiled SQL."""
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

    search_params = {k: kn.value_type(kn.bounds[0]) for k, kn in SEARCH_KNOBS.items()}
    search_params["fts_rank_scale"] = 6.0
    search_params.update(extra_sp)

    with contextlib.suppress(_Stop):
        await ps.PostgresService().memory_scored_search(
            tenant_id="t",
            embedding=[0.1] * 1024,
            query="connection pool sizing",
            search_params=search_params,
            top_k=10,
        )
    assert captured, "no statement reached session.execute"
    return str(captured[0].compile(dialect=postgresql.dialect()))


# ── scored search: knob off = byte-free of the used-counter machinery ───────


# The saturation term ``count / (count + SCALE)`` compiles with a CAST-wrapped
# denominator (int/int → NUMERIC coercion): ``…count) / CAST((ingredients.…count +``.
_USED_SATURATION = ") / CAST((ingredients.recall_used_count +"
_RETURNED_SATURATION = ") / CAST((ingredients.recall_count +"
_USED_RECENCY = "EXTRACT(epoch FROM now() - ingredients.recall_used_at)"
_RETURNED_RECENCY = (
    "EXTRACT(epoch FROM now() - coalesce(ingredients.last_recalled_at, ingredients.created_at))"
)


async def test_default_statement_reads_recall_count_only(monkeypatch) -> None:
    sql = await _capture_scored_search(monkeypatch)
    assert "recall_used_count" not in sql
    assert "recall_used_at" not in sql
    assert "#>>" not in sql, "no JSONB path extraction on the default path"
    # The boost still saturates over the return-fed counter, recency anchored
    # on last_recalled_at — today's expression, untouched.
    assert _RETURNED_SATURATION in sql
    assert _RETURNED_RECENCY in sql


async def test_source_one_boost_divides_the_used_counter(monkeypatch) -> None:
    sql = await _capture_scored_search(monkeypatch, recall_boost_source=1)
    # Ingredient extraction present…
    assert "recall_used_count" in sql
    assert "recall_used_at" in sql
    assert "#>>" in sql
    # …and it is what the saturation term divides, with recency anchored on the
    # confirmation stamp. The return-fed counter no longer feeds the boost (it
    # stays projected for the row contract, but appears in no saturation term).
    assert _USED_SATURATION in sql
    assert _USED_RECENCY in sql
    assert _RETURNED_SATURATION not in sql
    assert _RETURNED_RECENCY not in sql


async def test_source_one_keeps_the_render_counts(monkeypatch) -> None:
    """The used-counter columns ride the fenced ``ingredients`` CTE like every
    other ingredient: the heavy primitives render exactly as many times as on
    the default path (no extra ``<=>`` / ``ts_rank_cd`` evaluations)."""
    sql0 = await _capture_scored_search(monkeypatch)
    sql1 = await _capture_scored_search(monkeypatch, recall_boost_source=1)
    assert sql1.count("<=>") == sql0.count("<=>") == 1
    assert sql1.count("ts_rank_cd") == sql0.count("ts_rank_cd")


async def test_boost_disabled_beats_the_source_knob(monkeypatch) -> None:
    """``recall_boost_enabled=False`` (the org's ``recall_boost`` setting) still
    short-circuits to a constant 1.0 — the source knob only picks a counter for
    a boost that is on."""
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
    search_params = {k: kn.value_type(kn.bounds[0]) for k, kn in SEARCH_KNOBS.items()}
    search_params["fts_rank_scale"] = 6.0
    search_params["recall_boost_source"] = 1
    with contextlib.suppress(_Stop):
        await ps.PostgresService().memory_scored_search(
            tenant_id="t",
            embedding=[0.1] * 1024,
            query="q",
            search_params=search_params,
            top_k=10,
            recall_boost_enabled=False,
        )
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    assert _USED_SATURATION not in sql
    assert _RETURNED_SATURATION not in sql


# ── evolve_apply_weights: the counter bump is opt-in and rides the same txn ──


class _FakeResult:
    def __init__(self) -> None:
        self.rowcount = 0

    def fetchall(self):
        return []


async def _capture_evolve(monkeypatch: pytest.MonkeyPatch, **kwargs) -> list[str]:
    captured: list = []

    class _Session:
        async def execute(self, stmt, *args, **params):
            captured.append(str(stmt))
            return _FakeResult()

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ps, "get_session", _fake_session)
    await ps.PostgresService().evolve_apply_weights(
        tenant_id="t1",
        ids=["00000000-0000-0000-0000-000000000001"],
        delta=0.1,
        floor=0.0,
        cap=1.0,
        **kwargs,
    )
    return captured


async def test_evolve_default_issues_no_counter_statement(monkeypatch) -> None:
    """Without ``mark_used`` the call issues exactly today's statements — the
    weight clamp alone (plus the backfill only when rule+outcome arrive)."""
    stmts = await _capture_evolve(monkeypatch)
    assert len(stmts) == 1
    assert "_system" not in stmts[0]
    assert "recall_used_count" not in stmts[0]


async def test_evolve_mark_used_bumps_the_counter_in_the_same_txn(monkeypatch) -> None:
    stmts = await _capture_evolve(monkeypatch, mark_used=True)
    assert len(stmts) == 2, "weight clamp + counter bump, one session"
    bump = stmts[1]
    assert "recall_used_count" in bump
    assert "recall_used_at" in bump
    # Platform namespace, merged — sibling ``_system`` keys must survive.
    assert "'{_system}'" in bump
    assert "|| jsonb_build_object" in bump
    # Scoped like every sibling statement.
    assert "tenant_id = :tid" in bump
    assert "deleted_at IS NULL" in bump


async def test_evolve_mark_used_orders_after_the_backfill(monkeypatch) -> None:
    stmts = await _capture_evolve(
        monkeypatch,
        mark_used=True,
        rule_id="00000000-0000-0000-0000-000000000002",
        outcome_id="00000000-0000-0000-0000-000000000003",
    )
    assert len(stmts) == 3, "clamp, rule→outcome backfill, counter bump"
    assert "source_outcome_id" in stmts[1]
    assert "recall_used_count" in stmts[2]
