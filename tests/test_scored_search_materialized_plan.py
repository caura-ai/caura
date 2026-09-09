"""The ingredients CTE must reach the PLANNER as a materialised node.

``core-storage-api/tests/test_fts_score_single_render.py`` pins the compiled
TEXT: ``AS MATERIALIZED`` present, one ``<=>`` render. Text cannot prove what
the planner does with it: if a future PostgreSQL or SQLAlchemy change stopped
the modifier short of the planner, the single-branch (blank-query) statement
— a single-reference CTE, exactly the shape PostgreSQL 12+ inlines by
default — would be folded into its consumer, and every ``vec_sim`` column
reference would re-expand into a fresh 1024-dim cosine computation: a silent
~6x hot-path regression no compiled-text assertion can see (427ms -> 90ms was
the measured cost of exactly that expansion on a 50k-row corpus).

So this asks the planner itself. ``EXPLAIN (FORMAT JSON, VERBOSE)`` of the
statement the service actually issues must show the CTE as its own plan node
(a ``CTE Scan`` over ``ingredients``), and the distance operator must appear
exactly once in the whole plan — in the CTE's own target list, never
re-expanded in a consumer node. An inlined CTE has no ``CTE Scan`` node at
all, so either assertion alone would catch the fold; together they also catch
a partial re-expansion.

Needs a database, like the rest of the DB-backed tests in this tree (CI
always has one; locally set TEST_DATABASE_URL). No rows are needed — the
property under test is plan shape, not results.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

import core_storage_api.services.postgres_service as ps
from common.constants import SEARCH_KNOBS, VECTOR_DIM


class _ExplainJSON(Executable, ClauseElement):
    """EXPLAIN wrapper that keeps the inner statement's bound parameters."""

    inherit_cache = False

    def __init__(self, stmt) -> None:
        self.stmt = stmt


@compiles(_ExplainJSON, "postgresql")
def _compile_explain_json(element, compiler, **kw):
    return "EXPLAIN (FORMAT JSON, VERBOSE) " + compiler.process(element.stmt, **kw)


async def _captured_scored_search_stmt(monkeypatch: pytest.MonkeyPatch, query: str):
    """The statement ``memory_scored_search`` would execute, uncompiled."""
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

    with contextlib.suppress(_Stop):
        await ps.PostgresService().memory_scored_search(
            tenant_id="test-tenant-plan-shape",
            embedding=[0.1] * VECTOR_DIM,
            query=query,
            search_params=search_params,
            top_k=10,
        )
    assert captured, "no statement reached session.execute"
    return captured[0]


def _walk(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from _walk(child)


async def test_single_branch_statement_plans_the_cte_as_a_materialised_node(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank query → one CTE reference → the fence is all that stops inlining."""
    stmt = await _captured_scored_search_stmt(monkeypatch, query="")

    result = await db.execute(_ExplainJSON(stmt))
    raw = result.scalar()
    plan_doc = json.loads(raw) if isinstance(raw, str) else raw
    root = plan_doc[0]["Plan"]

    cte_scans = [
        n
        for n in _walk(root)
        if n.get("Node Type") == "CTE Scan" and n.get("CTE Name") == "ingredients"
    ]
    assert cte_scans, (
        "no CTE Scan over 'ingredients' in the plan — PostgreSQL inlined the "
        "CTE despite AS MATERIALIZED (or the fence was dropped), so every "
        "vec_sim reference re-expands into its own cosine distance computation:\n"
        + json.dumps(root, indent=1)[:4000]
    )

    distance_renders = json.dumps(plan_doc).count("<=>")
    assert distance_renders == 1, (
        f"the cosine distance appears {distance_renders} times in the plan, "
        f"expected exactly 1 (inside the materialised CTE's target list). More "
        f"than one means a consumer node re-expanded an ingredient column:\n"
        + json.dumps(root, indent=1)[:4000]
    )


async def test_reserved_branch_statement_also_plans_a_single_distance(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text query → two CTE references → materialised regardless; pin it anyway.

    This is the common production shape. It cannot regress by inlining (two
    references), but a refactor that split the branches back onto separate
    scans of ``memories`` would re-introduce a second distance evaluation —
    the plan-wide count catches that.
    """
    stmt = await _captured_scored_search_stmt(
        monkeypatch, query="connection pool sizing"
    )

    result = await db.execute(_ExplainJSON(stmt))
    raw = result.scalar()
    plan_doc = json.loads(raw) if isinstance(raw, str) else raw
    root = plan_doc[0]["Plan"]

    assert any(
        n.get("Node Type") == "CTE Scan" and n.get("CTE Name") == "ingredients"
        for n in _walk(root)
    ), "no CTE Scan over 'ingredients' in the two-branch plan"

    distance_renders = json.dumps(plan_doc).count("<=>")
    assert distance_renders == 1, (
        f"the cosine distance appears {distance_renders} times in the two-branch plan, expected 1"
    )
