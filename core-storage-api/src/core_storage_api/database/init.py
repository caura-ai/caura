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
    from sqlalchemy import text

    engine = get_engine()
    migrations_dir = str(Path(__file__).parent / "migrations")

    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", migrations_dir)

    # Session-scoped advisory lock on a dedicated connection so it survives
    # the per-migration commits Alembic performs — migrations that use
    # ``autocommit_block`` (e.g. ``CREATE INDEX CONCURRENTLY``) commit and
    # reopen the transaction on the work connection, and the transaction-scoped
    # variant would be released by those commits, breaking the multi-worker
    # serialisation guarantee.
    _MIGRATION_LOCK_ID = 8_675_309  # arbitrary unique int
    _LOCK_WAIT_SECONDS = 300
    _LOCK_POLL_INTERVAL = 0.5
    async with engine.connect() as lock_conn:
        # AUTOCOMMIT so the lock_conn never sits "idle in transaction": a
        # blocking ``pg_advisory_lock`` waiter keeps an open tx for the full
        # wait, and ``CREATE INDEX CONCURRENTLY`` waits for all in-flight txs
        # before proceeding — so a blocking waiter would stall the winning
        # worker's migration. ``pg_try_advisory_lock`` polled in autocommit
        # holds a tx for sub-ms per attempt and avoids that interaction.
        lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _LOCK_WAIT_SECONDS
        while True:
            got = await lock_conn.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _MIGRATION_LOCK_ID},
            )
            if got:
                break
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"Migration advisory lock {_MIGRATION_LOCK_ID} not "
                    f"acquired within {_LOCK_WAIT_SECONDS}s — another worker "
                    "still running migrations or stuck"
                )
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
