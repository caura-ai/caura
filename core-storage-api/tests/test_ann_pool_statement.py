"""ANN candidate pool (``ann_pool_size``) — statement-shape pins, no database.

Same capture-and-compile technique as ``test_fts_score_single_render``: the
statement is intercepted on its way to ``session.execute`` and compiled, so
these are pure unit tests. Three shapes matter:

* knob 0 (default): the statement must be byte-free of pool machinery —
  the two-stage path ships dark, and "off means unchanged" is the whole
  rollback story.
* knob > 0 with pgvector >= 0.8: two ``set_config`` executes pin the HNSW
  scan behaviour, the ``candidate_pool`` CTE unions one index-served arm per
  admission signal, every arm carries the full row-filter set, and the
  ingredients CTE is gated on the pool.
* knob > 0 with pgvector < 0.8: the probe declines and the statement keeps
  the exact default shape — an on-prem box that predates iterative scans
  must see zero change.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy.dialects import postgresql

import core_storage_api.services.postgres_service as ps
from common.constants import SEARCH_KNOBS

pytestmark = pytest.mark.asyncio


async def _capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    query: str,
    boosted=None,
    expected_executes: int | None = None,
    **extra_sp,
):
    """Run memory_scored_search against a capturing session; return compiled SQL list.

    ``expected_executes`` is how many session.execute calls to allow before
    stopping — 3 in ann-mode (two set_config + the statement), 1 otherwise.
    Tests that expect ann-mode to DECLINE (probe fallback) pass 1 explicitly.
    """
    captured: list = []
    stop_after = (
        expected_executes if expected_executes is not None else (3 if extra_sp.get("ann_pool_size") else 1)
    )

    class _Stop(Exception):
        pass

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            captured.append(stmt)
            if len(captured) >= stop_after:
                raise _Stop
            return None

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
            query=query,
            search_params=search_params,
            top_k=10,
            boosted_memory_ids=set(boosted or ()),
            memory_boost_factor=dict.fromkeys(boosted or (), 1.2),
        )
    assert captured, "no statement reached session.execute"
    return [str(s.compile(dialect=postgresql.dialect())) for s in captured]


@pytest.fixture(autouse=True)
def _fresh_probe_cache(monkeypatch: pytest.MonkeyPatch):
    """Isolate the process-wide pgvector probe cache per test."""
    monkeypatch.setattr(ps, "_pgvector_version", None)
    yield


def _probe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ps, "_pgvector_version", (0, 8, 1))


# ── knob off: byte-free of pool machinery ───────────────────────────────────


async def test_default_statement_has_no_pool_machinery(monkeypatch) -> None:
    sqls = await _capture(monkeypatch, query="connection pool sizing")
    assert len(sqls) == 1, "default path must not issue GUC set_config calls"
    sql = sqls[0]
    assert "candidate_pool" not in sql
    assert "set_config" not in sql
    assert "string_agg" not in sql, "arm provenance must not leak into the default path"
    assert "NULL AS pool_arms" in sql, "the row contract carries pool_arms as NULL off-pool"
    assert sql.count("<=>") == 1, "default path keeps the single-render invariant"


# ── knob on: pool arms, filters, GUCs ───────────────────────────────────────


async def test_ann_mode_pins_gucs_then_issues_one_pooled_statement(monkeypatch) -> None:
    _probe_ok(monkeypatch)
    sqls = await _capture(monkeypatch, query="connection pool sizing", ann_pool_size=200)
    assert len(sqls) == 3, "expected set_config(ef_search), set_config(iterative_scan), statement"
    assert "set_config" in sqls[0] and "set_config" in sqls[1]
    assert "candidate_pool" in sqls[2]


async def test_ann_mode_builds_one_arm_per_admission_signal(monkeypatch) -> None:
    _probe_ok(monkeypatch)
    boosted = [__import__("uuid").uuid4()]
    sql = (
        await _capture(
            monkeypatch,
            query="connection pool sizing",
            ann_pool_size=200,
            boosted=boosted,
        )
    )[-1]
    # ann arm: ORDER BY the raw distance — the one extra, deliberate render.
    assert sql.count("<=>") == 2, "vec_sim ingredient + the ANN arm's ORDER BY"
    # fts arm rides ts_rank_cd; ingredients' fts_score is the other call.
    assert sql.count("ts_rank_cd(") == 2
    # pool = ann + fts + recency + boosted (no date range here) → 3 pool
    # UNIONs + 1 for the FTS-reserved scored branch.
    assert sql.count("UNION") == 4
    # ingredients gated on the pool.
    assert "IN (SELECT candidate_pool.id" in sql
    # D12 arm provenance: arms tagged, deduped by GROUP BY, joined out.
    assert "string_agg" in sql and "GROUP BY" in sql
    assert "LEFT OUTER JOIN candidate_pool" in sql
    # Every arm carries the tenant predicate: 4 arms + ingredients = 5.
    assert sql.count("memories.tenant_id =") == 5, (
        "an arm lost the row filters — pool admission must never widen visibility"
    )


async def test_ann_mode_blank_query_drops_the_fts_arm(monkeypatch) -> None:
    _probe_ok(monkeypatch)
    sql = (await _capture(monkeypatch, query="", ann_pool_size=200))[-1]
    # arms: ann + recency (no fts — no query text; no boosted — none passed;
    # no date). One UNION in the pool; no reserved branch on a blank query.
    assert sql.count("UNION") == 1
    assert "candidate_pool" in sql
    assert "AS MATERIALIZED" in sql, "the ingredients fence must survive pool mode"


async def test_ann_mode_supersedes_a49_candidate_window(monkeypatch) -> None:
    _probe_ok(monkeypatch)
    sql = (
        await _capture(
            monkeypatch,
            query="connection pool sizing",
            ann_pool_size=200,
            candidate_pool_size=50,
        )
    )[-1]
    assert "candidate_pool" in sql
    # A49's window orders the main branch by similarity; superseded means the
    # main branch keeps the score ordering.
    assert "ORDER BY score DESC" in sql


async def test_date_range_adds_a_date_arm(monkeypatch) -> None:
    _probe_ok(monkeypatch)
    captured: list = []

    class _Stop(Exception):
        pass

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            captured.append(stmt)
            if len(captured) >= 3:
                raise _Stop
            return None

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ps, "get_read_session", _fake_session)
    search_params = {k: kn.value_type(kn.bounds[0]) for k, kn in SEARCH_KNOBS.items()}
    search_params["fts_rank_scale"] = 6.0
    search_params["ann_pool_size"] = 200

    with contextlib.suppress(_Stop):
        await ps.PostgresService().memory_scored_search(
            tenant_id="t",
            embedding=[0.1] * 1024,
            query="what shipped",
            search_params=search_params,
            top_k=10,
            date_range_start="2026-08-01",
            date_range_end="2026-08-15",
        )
    sql = str(captured[-1].compile(dialect=postgresql.dialect()))
    # ann + fts + recency + date → 3 pool UNIONs + 1 reserved-branch union.
    assert sql.count("UNION") == 4


# ── pgvector probe: gate and fallback ───────────────────────────────────────


async def test_old_pgvector_falls_back_to_the_default_shape(monkeypatch) -> None:
    monkeypatch.setattr(ps, "_pgvector_version", (0, 7, 4))
    sqls = await _capture(monkeypatch, query="connection pool sizing", ann_pool_size=200, expected_executes=1)
    assert len(sqls) == 1, "fallback must not issue GUC set_config calls"
    assert "candidate_pool" not in sqls[0]
    assert sqls[0].count("<=>") == 1


async def test_concurrent_probes_coalesce_to_one_lookup(monkeypatch) -> None:
    """First-call stampede protection: N concurrent probes, ONE pg_extension read.

    Without the probe lock, every search arriving between process start and
    the first probe completing would issue its own lookup — a thundering herd
    exactly when a big tenant with the knob on comes back after a deploy.
    """
    import asyncio

    opened = 0

    class _Result:
        def scalar(self):
            return "0.8.1"

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            # Widen the race window so all gathered coroutines are in flight
            # before the first probe completes.
            await asyncio.sleep(0.02)
            return _Result()

    @contextlib.asynccontextmanager
    async def _counting_session():
        nonlocal opened
        opened += 1
        yield _Session()

    monkeypatch.setattr(ps, "get_read_session", _counting_session)
    results = await asyncio.gather(*(ps._ann_pool_available() for _ in range(8)))
    assert all(results)
    assert opened == 1, f"expected one coalesced probe, saw {opened}"


async def test_probe_failure_is_not_cached(monkeypatch) -> None:
    """A transient probe error must fall back for THIS call and re-probe later."""

    @contextlib.asynccontextmanager
    async def _broken_session():
        raise RuntimeError("db unreachable")
        yield  # pragma: no cover

    monkeypatch.setattr(ps, "get_read_session", _broken_session)
    assert await ps._ann_pool_available() is False
    assert ps._pgvector_version is None, "a failed probe must not stick the process on fallback"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.8.1", True), ("0.8.0", True), ("0.10.2", True), ("1.0.0", True), ("0.7.4", False)],
)
async def test_probe_parses_extversion(monkeypatch, raw: str, expected: bool) -> None:
    class _Result:
        def scalar(self):
            return raw

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            return _Result()

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ps, "get_read_session", _fake_session)
    assert await ps._ann_pool_available() is expected
