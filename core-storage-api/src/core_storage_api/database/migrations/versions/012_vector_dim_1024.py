"""Bump pgvector embedding columns from ``Vector(768)`` to ``Vector(1024)``.

Follows the local-embedder bench-off (``local_emb_res/RESULTS.md``)
that selected ``BAAI/bge-m3`` as the new default embedder. bge-m3 is
natively 1024-dim — running it against the previous 768-dim schema
would require Matryoshka-style truncation that loses retrieval signal
(verified at N=30: native 1024 holds R@5 ≈ 0.96 on LongMemEval
``single-session-user``; truncated 768 trails by ~3 pp in earlier
spot checks).

Affects three columns across two services:
- ``memories.embedding``         — primary memory vectors
- ``entities.name_embedding``    — canonical-name embeddings
- ``documents.embedding``        — opt-in document semantic search

Migration semantics
-------------------
**Existing 768-dim data is destroyed.** pgvector cannot widen a vector
column in place — the dim is part of the column type, and a row's
existing 768-element value is not coercible to 1024. We therefore:

  1. ``DROP INDEX CONCURRENTLY`` the HNSW indexes (so ``ALTER TYPE``
     doesn't have to rebuild them inline). Same pattern used in
     migrations 008/009 for index churn on AlloyDB primary.
  2. ``UPDATE ... SET <embedding column> = NULL`` for every row that
     still has a 768-dim value. Without this, ``ALTER TYPE`` fails
     with ``ERROR: expected 1024 dimensions, not 768``. Setting to
     NULL is acceptable: pgvector treats NULL as "no embedding",
     queries naturally exclude these rows from semantic search, and
     the application re-embeds on next write.
  3. ``ALTER TABLE ... ALTER COLUMN ... TYPE vector(1024)`` —
     succeeds now that the column is empty.
  4. ``CREATE INDEX CONCURRENTLY`` the HNSW indexes against the new
     1024-dim column. Same operator (``vector_cosine_ops``) and build
     params (``m = 16, ef_construction = 64``) as the originals in
     001 / 003.

After upgrade, all existing memories/entities/documents have NULL
embeddings until the application re-embeds them. Two paths to recover:
- **Lazy**: any new write or any read path that calls
  ``get_or_cache_embedding`` will re-embed the row's content via the
  configured TEI sidecar. Steady-state OSS deployments converge over
  hours/days as memories are touched.
- **Eager**: run the backfill task in ``core-worker`` (separate PR)
  to scan rows ``WHERE embedding IS NULL`` and embed in batches.
  Recommended for production cutovers.

Operationally
-------------
- ``CONCURRENTLY`` keeps reads/writes serving through the index churn
  on AlloyDB primary — same constraint that drove migrations 008/009.
- The ``ALTER TYPE`` itself takes a brief ``ACCESS EXCLUSIVE`` lock on
  each table. On a multi-million-row table this is sub-second once
  the column is empty (the lock is metadata-only, no row rewrites).
- Each ``ALTER TABLE`` is preceded by ``SET LOCAL lock_timeout = '3s'``
  so an unfortunately-timed migration can't queue behind a long-running
  transaction and gate every other writer for minutes. If the lock
  isn't acquired within 3 s the statement raises ``lock_timeout`` and
  the surrounding alembic transaction rolls back — the migration fails
  fast and can be retried during a quieter window. ``SET LOCAL`` scopes
  the timeout to the current transaction only, so subsequent migrations
  inherit the cluster default.
- ``UPDATE ... = NULL`` rewrites every affected row — long-running on
  production-sized tables, but no exclusive lock and no app downtime.
  Safe to run during traffic. The three UPDATEs are **committed before
  the ALTERs** (each runs in its own auto-committed transaction via
  ``autocommit_block``), so a ``lock_timeout`` on a later ALTER does
  NOT roll the UPDATE work back. Retries pick up where the previous
  run left off — the ``WHERE ... IS NOT NULL`` filter makes each
  UPDATE idempotent, so already-NULLed rows are skipped. Without this
  separation, a late lock_timeout would force a complete redo of the
  UPDATEs on retry and could loop indefinitely under write load.

Safety gate
-----------
``upgrade()`` runs a pre-flight check that aborts the migration if
existing 768-dim rows are present and
``MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS`` is not set to ``"true"``. On a
fresh DB (no rows) the gate is a no-op and the migration runs
unchanged. On a populated DB an operator must explicitly opt in by
setting the env var for the migration run (and unset it afterwards).
Combined with the documented backfill task, this prevents the OSS
auto-migrate path (``core_storage_api.app.lifespan`` →
``init_database()`` → ``alembic upgrade head``) from silently
destroying embeddings on a ``docker compose pull && up``.

The gate is **not** applied to ``downgrade()``. Downgrade is
operator-driven (manual ``alembic downgrade``) and adding friction
there serves no purpose; the operator running it is already in
deliberate-recovery mode.

Downgrade
---------
Symmetric: drop indexes, NULL the 1024-dim values, ALTER TYPE back to
``Vector(768)``, recreate indexes. Same data-loss tradeoff in reverse.

Revision ID: 012
Revises: 011
Create Date: 2026-04-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Pre-flight safety gate ──────────────────────────────────────
    # This migration NULLs every existing 768-dim embedding before
    # widening the column to vector(1024) — pgvector cannot widen a
    # vector column in place. On a non-empty DB that's destructive
    # and irreversible. ``core-storage-api/...app.py`` runs
    # ``alembic upgrade head`` automatically in its FastAPI lifespan,
    # so without this gate a ``docker compose pull && up`` on an
    # existing install would silently destroy embeddings the moment
    # the container starts. Refuse to proceed unless the operator has
    # opted in explicitly via ``MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS=true``.
    #
    # On a fresh DB (no existing 768-dim rows), the gate is a no-op:
    # there is nothing to destroy. The check is row-count-based, not
    # purely env-based, so a fresh ``docker compose up`` for the first
    # time still works without operator intervention.
    import os

    from sqlalchemy import text as _sql_text

    _opt_in = os.environ.get("MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS", "").lower() == "true"
    if not _opt_in:
        bind = op.get_bind()
        # Count rows that would be destroyed. Single round-trip; cast
        # to ``bigint`` so sums beyond 32-bit don't overflow on very
        # large tables. Each sub-select is independently safe to run
        # before the schema change because the columns still exist
        # (we haven't widened them yet).
        existing = bind.execute(
            _sql_text(
                "SELECT "
                "  (SELECT COUNT(*) FROM memories  WHERE embedding      IS NOT NULL)::bigint "
                "+ (SELECT COUNT(*) FROM entities  WHERE name_embedding IS NOT NULL)::bigint "
                "+ (SELECT COUNT(*) FROM documents WHERE embedding      IS NOT NULL)::bigint "
                "AS n"
            )
        ).scalar_one()
        if existing > 0:
            raise RuntimeError(
                f"alembic 012_vector_dim_1024 is destructive: "
                f"{existing} row(s) currently hold 768-dim embeddings that "
                f"will be NULL'd. To proceed, set "
                f"MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS=true and ensure a "
                f"backfill is queued to re-embed the rows after migration "
                f"completes (see the 'Upgrading from v1.x' section of "
                f"README.md). Refusing to run automatically."
            )
    # ────────────────────────────────────────────────────────────────

    # 1. Drop HNSW indexes — must be CONCURRENTLY (read traffic stays
    # served on AlloyDB primary). ``CONCURRENTLY`` cannot run inside a
    # transaction, so we use alembic's autocommit_block. Same idiom as
    # migrations 008 / 009.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_embedding_hnsw")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_documents_embedding_hnsw")
    # ``entities.name_embedding`` has no HNSW index (per 001) — no-op.

    # 2. Clear existing 768-dim values so ``ALTER TYPE`` accepts the
    # new dim. NULL is the only value compatible with both old and
    # new column types. Application is expected to re-embed
    # post-migration (see module docstring).
    #
    # Each UPDATE runs in its own auto-committed transaction. On a
    # multi-million-row table these rewrites can take many minutes;
    # if a later ALTER TABLE then trips its 3 s ``lock_timeout`` and
    # rolls back, we want the NULL-scan work *already done* to stay
    # done. Without ``autocommit_block`` here, the entire UPDATE +
    # ALTER chain is one alembic transaction — a lock_timeout late in
    # the chain would discard hours of UPDATE work and force a
    # complete redo on retry, potentially looping indefinitely under
    # write load. The ``WHERE ... IS NOT NULL`` filter makes each
    # UPDATE idempotent: a row already NULLed by a partial prior
    # attempt is simply skipped on the next run.
    with op.get_context().autocommit_block():
        op.execute("UPDATE memories SET embedding = NULL WHERE embedding IS NOT NULL")
        op.execute("UPDATE entities SET name_embedding = NULL WHERE name_embedding IS NOT NULL")
        op.execute("UPDATE documents SET embedding = NULL WHERE embedding IS NOT NULL")

    # 3. Widen each column to 1024 dim. Sub-second metadata-only
    # change once the column is empty.
    #
    # ``SET LOCAL lock_timeout = '3s'`` before each ALTER scopes a
    # bounded wait to the transaction: if the AccessExclusive lock
    # can't be acquired in 3 s (because a long-running transaction
    # holds AccessShare on the table), the statement raises
    # ``lock_timeout`` and the migration aborts cleanly instead of
    # queuing behind the holder and freezing every other writer in
    # the meantime. Alembic's surrounding transaction rolls back; the
    # migration can be retried during a quieter window.
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE memories ALTER COLUMN embedding TYPE vector(1024)")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE entities ALTER COLUMN name_embedding TYPE vector(1024)")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE documents ALTER COLUMN embedding TYPE vector(1024)")

    # 4. Rebuild HNSW indexes against the new dim. Same operator + build
    # params as the originals in 001 / 003.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_embedding_hnsw
            ON memories
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_embedding_hnsw
            ON documents
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
            """
        )


def downgrade() -> None:
    # Mirror image of upgrade. Same data-loss tradeoff: existing
    # 1024-dim embeddings cannot be coerced back to 768; rows are
    # NULLed and the application is expected to re-embed against
    # whatever 768-dim provider it switches back to.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_embedding_hnsw")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_documents_embedding_hnsw")

    # Same ``autocommit_block`` rationale as upgrade: NULL-scan work
    # commits independently so a later ALTER hitting ``lock_timeout``
    # doesn't force the UPDATEs to re-run on retry. Idempotent under
    # ``WHERE ... IS NOT NULL``.
    with op.get_context().autocommit_block():
        op.execute("UPDATE memories SET embedding = NULL WHERE embedding IS NOT NULL")
        op.execute("UPDATE entities SET name_embedding = NULL WHERE name_embedding IS NOT NULL")
        op.execute("UPDATE documents SET embedding = NULL WHERE embedding IS NOT NULL")

    # Same ``lock_timeout`` guard as upgrade — an unfortunately-timed
    # rollback shouldn't be able to queue behind a long-running reader
    # and freeze the whole cluster.
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE memories ALTER COLUMN embedding TYPE vector(768)")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE entities ALTER COLUMN name_embedding TYPE vector(768)")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE documents ALTER COLUMN embedding TYPE vector(768)")

    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_embedding_hnsw
            ON memories
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_embedding_hnsw
            ON documents
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
            """
        )
