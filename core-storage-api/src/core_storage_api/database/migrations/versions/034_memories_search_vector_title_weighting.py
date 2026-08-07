"""Index ``memories.title`` in ``search_vector``, at the same weight as content.

Revision ID: 034
Revises: 033
Create Date: 2026-08-07

``title`` has never been in the tsvector. Migration 001's trigger builds it as
``to_tsvector('english', coalesce(NEW.content, ''))``, so a memory whose title
carries the distinguishing words — and the LLM enrichment writes a title on every
enriched memory — **cannot be found by FTS on those words at all** unless the
content happens to repeat them. Measured against real Postgres, query
"pool sizing" against title "Postgres connection pool sizing" with those terms
absent from the content: ``ts_rank_cd`` 0.0000 before, 0.1000 after. Unreachable,
not merely ranked low. That is the whole reason for this change.

SAME WEIGHT, DELIBERATELY. The two fields are concatenated and tokenised
together, so every lexeme keeps the weight D it has always had. ``setweight``
with title A / content B was the other candidate and is NOT what shipped: it
multiplies every content rank by 4 (D=0.1 -> B=0.4), which forces
``FTS_RANK_SCALE`` to be re-derived, and it moves the modal ENRICHED row a long
way — titles are LLM summaries that reuse the content's salient terms, so most
rows match in both fields, and there the weights add. Measured:

                              content-only   title-only   title-echo
    today                        0.1000        0.0000       0.1000
    title A / content B          0.4000        1.0000       1.5429
    this migration               0.1000        0.1000       0.2250

Keeping one weight means **a content-only match scores exactly what it scored
before** — 0.1000, so ``FTS_RANK_SCALE`` stays 6.0 and no bound, default or
calibration moves anywhere in the stack. The only rows whose score changes are
ones that could not be found at all before, plus rows whose title repeats a
content term (0.1 -> 0.225). That second effect is not a field-weighting
decision: it is the ordinary boost ``ts_rank_cd`` gives ANY repeated term, the
same as if the content itself had said the word twice.

Preferring the field split would be a relevance claim — that a title hit beats a
content hit — and nothing available here can test it: the LoCoMo benchmark
corpus is dialogue turns with no titles.

WHY THE TRIGGER ALSO CHANGES ITS FIRE CONDITION. 001 declared
``BEFORE INSERT OR UPDATE OF content``. With ``title`` in the vector, an update
that changes only the title would leave a stale ``search_vector`` behind — the
enrichment path writes the title, and on the async-enrich flow it lands after the
row already exists, which is the common case rather than an edge one.
``OF content, title`` covers both.

A ``GENERATED ALWAYS AS (...) STORED`` column would make the stale-vector class
unrepresentable — no ``UPDATE OF`` list to fall out of sync — and produces a
byte-identical vector. Not done here: converting needs DROP + ADD COLUMN, a full
table rewrite under ACCESS EXCLUSIVE plus a GIN rebuild, strictly worse than this
migration. The moment for it is a future migration already rewriting ``memories``.

BACKFILL. Existing rows keep the content-only vector until next written, so their
titles stay unsearchable — the bug persists for them, but nothing they could
already do gets worse, because content lexemes and their weight are unchanged.
It runs in COMMITTED BATCHES inside ``autocommit_block``, the shape 012 uses:
this holds the startup advisory lock, and a single-transaction rewrite of a
multi-million-row table would exceed Cloud Run's 240 s startup probe, get
SIGKILLed, roll back every batch and restart from zero — the non-converging crash
loop that took out six writer boots on 2026-06-16. Committed batches make it
resumable, and the ``IS DISTINCT FROM`` predicate skips rows already done.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "034"
down_revision: str | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 5000


def _vector(prefix: str = "") -> str:
    """Title and content tokenised together, one weight throughout.

    One definition: the trigger and the backfill computing different vectors
    would be a silent indexing bug. ``coalesce`` because ``title`` is nullable —
    only the enrichment path sets it — and ``NULL || text`` is NULL, which would
    empty the whole vector rather than contribute nothing.
    """
    return f"to_tsvector('english', coalesce({prefix}title, '') || ' ' || coalesce({prefix}content, ''))"


def _content_only(prefix: str = "") -> str:
    """001's expression, for the downgrade."""
    return f"to_tsvector('english', coalesce({prefix}content, ''))"


def _rebuild(build) -> None:
    """Rewrite every row whose stored vector differs, in committed batches.

    Walks the PRIMARY KEY with a keyset cursor rather than re-running the filter
    from the top each pass. ``search_vector IS DISTINCT FROM <expr>`` is not
    indexable — the GIN index serves ``@@``, not equality — so a bare
    ``WHERE ... LIMIT n`` loop rescans and re-evaluates a growing prefix of
    already-rebuilt rows every iteration, which is O(n^2 / batch) tsvector
    computations. That is the same blow-up this migration's backfill exists to
    avoid, just relocated from lock duration into CPU. ``id > :last`` with
    ``ORDER BY id`` seeks forward instead, making the whole backfill one pass.

    The predicate stays as a FILTER: a row with no title tokenises identically
    either side of this migration, so it needs no rewrite and this skips it.

    ``build`` is called twice with different prefixes — the filter reads the bare
    columns, the UPDATE reads them through the ``m`` alias.

    ``search_vector`` is not in the trigger's ``UPDATE OF`` list, so writing it
    here does not recurse.
    """
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        last = None
        while True:
            # ``sa.text`` rather than ``exec_driver_sql``: the bind is asyncpg,
            # whose paramstyle is positional ``$1``, and driver-level SQL would
            # need that spelled correctly per driver. One statement per batch,
            # returning the ids it wrote so the cursor advances without a second
            # round trip. ``CAST(:last AS uuid)`` because the first pass binds
            # NULL and asyncpg needs the type.
            rows = bind.execute(
                sa.text(f"""
                    WITH batch AS (
                        SELECT id FROM memories
                         WHERE search_vector IS DISTINCT FROM ({build("")})
                           AND (CAST(:last AS uuid) IS NULL OR id > CAST(:last AS uuid))
                         ORDER BY id
                         LIMIT {_BATCH}
                    )
                    UPDATE memories m SET search_vector = ({build("m.")})
                      FROM batch WHERE m.id = batch.id
                    RETURNING m.id
                """),
                {"last": last},
            ).fetchall()
            if not rows:
                break
            # RETURNING has no defined order; the CTE's ORDER BY makes the batch a
            # contiguous id range, so its max is the cursor.
            last = max(r[0] for r in rows)


def _set_trigger(vector: str, update_of: str) -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION memories_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector := {vector};
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    # Recreated rather than altered: the fire condition is part of the CREATE, and
    # ``CREATE OR REPLACE TRIGGER`` needs PG 14+ while this schema's floor is 13.
    op.execute("DROP TRIGGER IF EXISTS memories_search_vector_trigger ON memories")
    op.execute(f"""
        CREATE TRIGGER memories_search_vector_trigger
        BEFORE INSERT OR UPDATE OF {update_of} ON memories
        FOR EACH ROW EXECUTE FUNCTION memories_search_vector_update();
    """)


def upgrade() -> None:
    _set_trigger(_vector("NEW."), "content, title")
    _rebuild(_vector)


def downgrade() -> None:
    _set_trigger(_content_only("NEW."), "content")
    _rebuild(_content_only)
