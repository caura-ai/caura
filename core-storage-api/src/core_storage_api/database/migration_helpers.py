"""Helpers shared by Alembic version scripts.

Lives outside ``migrations/`` because that directory is not a package —
Alembic loads version files by path — while ``core_storage_api.database`` is
importable from any of them, the same way ``env.py`` imports
``core_storage_api.config``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# An index left behind by an interrupted build, by name. Restricted to the
# current search_path so a same-named index in another schema is not touched.
_INVALID_INDEX = sa.text(
    """
    SELECT 1 FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = :name
      AND n.nspname = current_schema()
      AND NOT i.indisvalid
    """
)


def drop_invalid_indexes(*names: str) -> None:
    """Drop any of *names* left INVALID by an interrupted CONCURRENTLY build.

    Call this inside the ``autocommit_block`` immediately before the
    ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` statements that build them.

    A killed ``CREATE INDEX CONCURRENTLY`` leaves the index in ``pg_index``
    with ``indisvalid = false``. ``IF NOT EXISTS`` then SKIPS it — "already
    exists, skipping" — and the planner refuses to use it, so the migration
    stamps green over an index that is permanently useless and still charges
    write overhead on every insert. Nothing later repairs it: every re-run
    takes the same skip.

    Not theoretical, and the numbers are migration 035's, measured rather than
    assumed: the invalid index was 547x slower than the valid one on the very
    query the FK trigger runs. Reachable because ``CONCURRENTLY`` waits on
    concurrent transactions with no upper bound (7.7 s behind a single open
    writer) while Cloud Run kills the container at the 240 s startup probe.

    Extracted from the copies in 005 / 007 / 026 / 035 / 040 / 041 / 046 / 048.
    Having no shared spelling is why the other seven concurrent-index
    migrations never got one.
    """
    connection = op.get_context().connection
    if connection is None:
        raise RuntimeError("online migration requires a connection")
    for name in names:
        if connection.execute(_INVALID_INDEX, {"name": name}).fetchone():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
