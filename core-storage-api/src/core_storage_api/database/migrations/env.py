"""Alembic env.py for core-storage-api migrations.

Supports two modes:
- Programmatic: init_database() passes a connection via config.attributes
- CLI: `alembic upgrade head` reads database_url from core_storage_api.config
"""

import asyncio

from logging.config import fileConfig

from alembic import context
from common.models.base import Base

_alembic_cfg = context.config
if _alembic_cfg.config_file_name is not None:
    fileConfig(_alembic_cfg.config_file_name)

# Import all models so Base.metadata is populated (required for --autogenerate).
#
# The PACKAGE, not a hand-kept list of thirteen modules. 09/02 L-13: that list
# omitted ``recall_log``, so ``recall_event`` and ``recall_candidate`` — both
# created by migration 027, both present in every deployed database — were
# invisible to ``Base.metadata`` here. Autogenerate compares metadata against
# the live schema and emits a DROP for anything it cannot see, so the next
# ``alembic revision --autogenerate`` proposed dropping both tables. Measured,
# not inferred: running it produced ``op.drop_table('recall_candidate')`` and
# ``op.drop_table('recall_event')``.
#
# Importing the package makes that failure impossible to reintroduce by
# forgetting a line here: a new model registers by being exported from
# ``common/models/__init__.py``, which is where a new model is added anyway.
import common.models  # noqa: F401
from sqlalchemy.ext.asyncio import create_async_engine


def do_run_migrations(connection):
    # ``transaction_per_migration=True`` is required so each migration owns its
    # own transaction. Without it Alembic shares one tx across all migrations
    # and ``autocommit_block`` (used by ``CREATE INDEX CONCURRENTLY`` etc.) has
    # no per-migration tx to commit out of and asserts on entry.
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_cli():
    """CLI mode: create engine from settings and run migrations."""
    from core_storage_api.config import db_connect_args, settings

    # Same TLS policy as the app's engines: a migration is the one connection
    # that carries schema changes, so letting it fall back to cleartext while
    # the service runs over TLS would be the worst of the three sites to miss.
    url = settings.database_url.get_secret_value()
    engine = create_async_engine(url, connect_args=db_connect_args(url))
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError(
        "Offline mode (alembic --sql) is not supported. Run 'alembic upgrade head' without --sql."
    )
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_migrations_cli())
