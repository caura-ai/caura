"""Database initialization — runs Alembic migrations on startup."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core_storage_api.config import settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_read_engine: AsyncEngine | None = None


def _schema_is_at_head(connection: Connection, head: str | None) -> bool:
    """True when the DB's Alembic revision already equals ``head``.

    Lets a replica that isn't the one running the migration start serving as
    soon as the schema is current — WITHOUT acquiring the migration advisory
    lock — instead of queueing behind every other booting replica to acquire
    the lock and run a redundant no-op ``upgrade``. That serialized
    acquire-then-no-op is what pushed tail replicas past the Cloud Run startup
    probe during multi-replica scale-ups (prod 2026-06-24 02:04: storage-writer
    instances killed on a 240s STARTUP TCP probe DEADLINE_EXCEEDED).

    ``head`` (the script head) is computed once by the caller and passed in —
    it's constant for the process lifetime, so the per-poll follower check stays
    a single ``alembic_version`` SELECT rather than re-walking the migrations
    directory each poll. Returns False on a fresh DB (no ``alembic_version`` row
    → current is None), so the bootstrap/stamp path still runs under the lock.
    """
    # Deferred alembic import to match this module's existing convention
    # (``init_database`` imports ``command``/``Config`` inside the function).
    from alembic.runtime.migration import MigrationContext

    current = MigrationContext.configure(connection).get_current_revision()
    return current is not None and current == head


def _build_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
    )


def get_engine() -> AsyncEngine:
    """Return the writer engine (primary DB), creating on first call."""
    global _engine
    if _engine is None:
        _engine = _build_engine(settings.database_url)
    return _engine


def get_read_engine() -> AsyncEngine:
    """Return the reader engine. Same as writer when ``read_database_url``
    isn't configured (OSS standalone); otherwise its own pool against
    the replica so read traffic doesn't share primary's connection
    budget."""
    global _read_engine
    if not settings.read_database_url:
        return get_engine()
    if _read_engine is None:
        _read_engine = _build_engine(settings.read_database_url)
    return _read_engine


async def get_session():
    """Yield an async session for writes / read-after-write work."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(get_engine(), expire_on_commit=False) as session:
        yield session


async def init_database() -> None:
    """Run all pending Alembic migrations to initialize/update the database schema.

    If tables already exist (e.g., created by the legacy backend), stamps the
    current revision so Alembic skips the initial migration.

    Uses a PostgreSQL advisory lock so that when multiple uvicorn workers start
    concurrently, only one runs migrations; the others wait and then no-op.

    Role=reader (CAURA-591 Part B): no-op. The writer owns schema; reader-role
    services connect to the read-replica pool which rejects DDL anyway, so
    attempting to migrate would just fail with a confusing error.
    """
    if settings.core_storage_role == "reader":
        logger.info("Skipping Alembic (role=reader — writer owns schema)")
        return

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    engine = get_engine()
    migrations_dir = str(Path(__file__).parent / "migrations")

    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", migrations_dir)

    # The script head is constant for the process lifetime — compute it once
    # (a migrations-dir walk) and reuse it for every at-head check, so the
    # follower poll loop below stays a single ``alembic_version`` SELECT per
    # poll rather than re-walking the migrations directory each time.
    schema_head = ScriptDirectory.from_config(alembic_cfg).get_current_head()

    # Fast path: if the schema is already at head there's no migration to run,
    # so start serving immediately without contending on the advisory lock.
    # This is the steady-state boot (nothing pending), and it lets
    # simultaneously-booting replicas skip the lock entirely when there's no
    # work — only a genuine pending migration falls through to the serialized
    # leader/follower path below.
    async with engine.connect() as check_conn:
        if await check_conn.run_sync(_schema_is_at_head, schema_head):
            logger.info("Schema already at head — starting without migration lock")
            return

    # Session-scoped advisory lock on a dedicated connection so it survives
    # the per-migration commits Alembic performs — migrations that use
    # ``autocommit_block`` (e.g. ``CREATE INDEX CONCURRENTLY``) commit and
    # reopen the transaction on the work connection, and the transaction-scoped
    # variant would be released by those commits, breaking the multi-worker
    # serialisation guarantee.
    _MIGRATION_LOCK_ID = 8_675_309  # arbitrary unique int
    _LOCK_POLL_INTERVAL = 0.5
    _LOCK_LOG_EVERY_SECONDS = 30.0
    # The winning replica holds this lock for the WHOLE ``alembic upgrade head``
    # (including minutes-long ``CREATE INDEX CONCURRENTLY`` builds / backfills),
    # so the wait is a stuck-migration backstop, not a routine cap. It must
    # exceed the slowest real migration or a slow-but-progressing one crashes the
    # N-1 booting replicas waiting on it (2026-06-16: a hardcoded 300s cap that
    # migration 025 blew past, failing 6 writer boots). Tunable via env.
    lock_wait_seconds = settings.migration_lock_wait_seconds
    async with engine.connect() as lock_conn:
        # AUTOCOMMIT so the lock_conn never sits "idle in transaction": a
        # blocking ``pg_advisory_lock`` waiter keeps an open tx for the full
        # wait, and ``CREATE INDEX CONCURRENTLY`` waits for all in-flight txs
        # before proceeding — so a blocking waiter would stall the winning
        # worker's migration. ``pg_try_advisory_lock`` polled in autocommit
        # holds a tx for sub-ms per attempt and avoids that interaction.
        lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
        loop = asyncio.get_event_loop()
        start = loop.time()
        deadline = start + lock_wait_seconds
        next_log = start + _LOCK_LOG_EVERY_SECONDS
        while True:
            got = await lock_conn.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _MIGRATION_LOCK_ID},
            )
            if got:
                break
            # Not the leader. Start as soon as the leader's migration lands
            # (schema reaches head) WITHOUT acquiring the lock — so every
            # waiting follower unblocks together when the migration completes,
            # instead of serializing through acquire→no-op-upgrade→release one
            # at a time (whose tail exceeded the startup probe). Reuse the
            # AUTOCOMMIT ``lock_conn`` for the read — no per-poll connection churn.
            if await lock_conn.run_sync(_schema_is_at_head, schema_head):
                logger.info("Migration completed by another replica — starting without lock")
                return
            now = loop.time()
            if now >= deadline:
                raise TimeoutError(
                    f"Migration advisory lock {_MIGRATION_LOCK_ID} not acquired "
                    f"within {lock_wait_seconds}s — another worker is likely stuck "
                    "running migrations. A healthy slow migration should finish "
                    "within this window; if a legitimate migration needs longer, "
                    "raise MIGRATION_LOCK_WAIT_SECONDS (keeping it <= the Cloud Run "
                    "startup-probe deadline)."
                )
            # Surface the wait so a long-but-healthy migration looks like progress
            # in the logs, not a silently hung boot.
            if now >= next_log:
                logger.info(
                    "Waiting on migration advisory lock (%.0fs of %ds elapsed) — "
                    "another worker is running migrations",
                    now - start,
                    lock_wait_seconds,
                )
                next_log = now + _LOCK_LOG_EVERY_SECONDS
            await asyncio.sleep(_LOCK_POLL_INTERVAL)
        try:
            async with engine.connect() as work_conn:
                has_tables = await work_conn.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'memories')"
                    )
                )
                has_alembic = await work_conn.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'alembic_version')"
                    )
                )
                # End the implicit read tx so Alembic owns transaction lifecycle
                # on this connection — required for ``autocommit_block``.
                await work_conn.commit()

                def _run_upgrade(connection: Connection) -> None:
                    alembic_cfg.attributes["connection"] = connection
                    if has_tables and not has_alembic:
                        # Tables exist from legacy backend — stamp as current, skip creation
                        logger.info("Existing tables detected, stamping Alembic at head")
                        command.stamp(alembic_cfg, "head")
                    else:
                        command.upgrade(alembic_cfg, "head")

                await work_conn.run_sync(_run_upgrade)
        finally:
            # Must release before the connection returns to the pool —
            # SQLAlchemy's rollback-on-return doesn't release session-scoped
            # advisory locks, so a pooled session could otherwise hand the
            # lock to the next caller.
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _MIGRATION_LOCK_ID},
            )

    logger.info("Database initialization complete")
