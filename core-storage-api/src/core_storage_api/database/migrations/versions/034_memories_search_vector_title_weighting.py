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

BACKFILL IS OUT OF BAND — ``scripts/backfill_034_search_vector.py``, run once
after deploy. It is NOT in this migration, and that is not a style preference:

An earlier version of 034 backfilled here, in committed batches, and it took
staging down's deploy path on 2026-08-08. Every instance boots, blocks on the
migration advisory lock, and Cloud Run's startup probe kills the container after
240 s; the leader never finishes, so ``Running upgrade 033 -> 034`` appears at
09:30, 09:34, 09:39, 09:43, 09:47 and never completes. Committed batches were
supposed to make that converge, and they do NOT: the keyset cursor lives in
process memory, so each restart resumes from the top and re-evaluates the
``IS DISTINCT FROM`` predicate — one ``to_tsvector`` per row — across an
ever-growing prefix of already-rebuilt rows. The quadratic cost moves from
within a run to across restarts. (Cloud Run kept the previous revision serving
throughout, so it was a blocked deploy, not an outage.)

The house rule already said this: DDL belongs in the migration, non-DDL backfills
belong in one-off scripts. What is left here is instant.

``downgrade()`` is symmetric and equally data-free: it restores the content-only
trigger and rewrites nothing. Reverting rows is the same volume of work as the
forward pass and would reintroduce the same startup-probe failure. Run
``scripts/backfill_034_search_vector.py --revert`` after downgrading if the split
matters — until then, rows the forward backfill already rebuilt keep matching on
title terms while rows written after the downgrade do not.

A PARTIAL BACKFILL IS SAFE, which is what makes out-of-band viable and is a
direct consequence of the equal-weight choice above. A row not yet rebuilt keeps
its content-only vector, and content lexemes and their weight are identical
either side of this migration — so it scores exactly as it always did and merely
has not gained title search yet. There is no window in which two rows are scored
on different scales.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "034"
down_revision: str | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def downgrade() -> None:
    _set_trigger(_content_only("NEW."), "content")
