"""Honest planner cost for the pgvector cosine distance function.

``cosine_distance(vector, vector)`` — the function behind ``<=>`` — ships with
PostgreSQL's default function cost of 1, the price of an integer comparison.
The real thing is a 1024-dimension float loop, roughly two orders of magnitude
more. That mispricing is why the planner keeps choosing a full scan-and-sort
over an HNSW index scan for ``ORDER BY embedding <=> :q LIMIT n`` whenever a
WHERE clause is attached: the scan's per-row cost is underestimated ~100x, so
seq scan wins the cost comparison it should lose. Measured on a 50k-row rig:
with COST 100 the planner freely chose the HNSW index for the filtered ANN arm
(6ms) where it previously picked a parallel seq scan + sort (69-129ms).

This is a nudge, not a dependency: the ANN candidate pool (``ann_pool_size``)
also pins per-query GUCs (``hnsw.ef_search``, ``hnsw.iterative_scan``) and
works without this migration. The honest cost helps every OTHER ``<=>`` path
too (dedup, contradiction candidates, neighbor scans, entity resolution) —
all of them ORDER BY distance with LIMIT and want the index, so making seq
scans look expensive is the safe direction everywhere.

Best-effort by design: ``ALTER FUNCTION`` on an extension-owned function needs
ownership, and on managed or on-prem databases the app user often is not the
extension owner. A DO block downgrades ``insufficient_privilege`` to a NOTICE
instead of failing the whole upgrade — the deployment simply keeps the default
cost and the GUC pinning carries the two-stage path alone. (CI creates the
extension as the app user, so CI exercises the applied branch.)

Revision ID: 044
Revises: 043
Create Date: 2026-09-09
"""

from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None

_SET_COST = """
DO $$
BEGIN
    BEGIN
        ALTER FUNCTION cosine_distance(vector, vector) COST {cost};
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'skipping cosine_distance COST {cost} (not the extension owner); '
                'the ANN candidate pool still works via per-query GUC pinning';
        WHEN undefined_function THEN
            RAISE NOTICE 'cosine_distance(vector, vector) not found; skipping COST {cost}';
    END;
END $$;
"""


def upgrade() -> None:
    op.execute(_SET_COST.format(cost=100))


def downgrade() -> None:
    # PostgreSQL's default procost for C-language functions.
    op.execute(_SET_COST.format(cost=1))
