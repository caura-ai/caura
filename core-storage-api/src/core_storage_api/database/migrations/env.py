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

# Import all models so Base.metadata is populated (required for --autogenerate)
from common.models.memory import Memory  # noqa: F401
from common.models.entity import Entity, MemoryEntityLink, Relation  # noqa: F401
from common.models.audit import AuditLog  # noqa: F401
from common.models.agent import Agent  # noqa: F401
from common.models.agent_activity_digest import AgentActivityDigest  # noqa: F401
from common.models.analysis_report import CrystallizationReport  # noqa: F401
from common.models.fleet import FleetNode, FleetCommand  # noqa: F401
from common.models.document import Document  # noqa: F401
from common.models.background_task import BackgroundTaskLog  # noqa: F401
from common.models.capability_usage import CapabilityUsage  # noqa: F401
from common.models.organization_settings import OrganizationSettings, OrganizationSettingsAudit  # noqa: F401
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
    from core_storage_api.config import settings

    engine = create_async_engine(settings.database_url)
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
