"""Partial schema states the boot path has to survive, on a real database.

Two states, both produced by an upgrade that did not finish the way the code
assumed:

* A migration interrupted inside its ``autocommit_block``. Entering the block
  commits the column the migration added before it, while ``alembic_version``
  moves only when ``upgrade()`` returns — so the retry re-runs ``upgrade()``
  over a column that already exists. A plain ``ADD COLUMN`` failed it with
  ``DuplicateColumn`` on every boot from then on.
* A database the chain built but that lost its ``alembic_version`` row (a
  selective restore, or an operator dropping the table to clear the wedge
  above). ``init_database`` stamped it at head on the evidence of migration
  019 alone, skipping every migration it had not run.

Each test builds its own throwaway database next to the suite's — the states
under test are a schema stopped part-way through the chain, which the shared
database (always at head) cannot be put into and back out of.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core_storage_api.config import settings
from core_storage_api.database import init as init_mod

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MIGRATIONS = Path(init_mod.__file__).parent / "migrations"


def _alembic_cfg() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    return cfg


def _script_head() -> str | None:
    return ScriptDirectory.from_config(_alembic_cfg()).get_current_head()


@pytest.fixture
async def scratch_engine() -> AsyncIterator[AsyncEngine]:
    """An engine on a fresh, empty database (pgvector installed), dropped after."""
    base = make_url(settings.database_url.get_secret_value())
    name = f"{base.database}_mig_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(base.set(database=name))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _upgrade(engine: AsyncEngine, revision: str) -> None:
    cfg = _alembic_cfg()

    def run(connection) -> None:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)

    async with engine.connect() as conn:
        await conn.run_sync(run)


async def _scalar(engine: AsyncEngine, sql: str):
    async with engine.connect() as conn:
        return await conn.scalar(text(sql))


async def _execute(engine: AsyncEngine, sql: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql))


async def _has_column(engine: AsyncEngine, table: str, column: str) -> bool:
    return bool(
        await _scalar(
            engine,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
            f"AND table_name = '{table}' AND column_name = '{column}')",
        )
    )


async def _has_valid_index(engine: AsyncEngine, name: str) -> bool:
    return bool(
        await _scalar(
            engine,
            "SELECT EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            f"WHERE c.relname = '{name}' AND i.indisvalid)",
        )
    )


async def _revision(engine: AsyncEngine) -> str | None:
    if not await _scalar(engine, "SELECT to_regclass('public.alembic_version') IS NOT NULL"):
        return None
    return await _scalar(engine, "SELECT version_num FROM alembic_version")


@pytest.fixture
def scratch_init(monkeypatch: pytest.MonkeyPatch, scratch_engine: AsyncEngine):
    """``init_database`` pointed at the scratch database."""
    monkeypatch.setattr(init_mod, "_engine", scratch_engine)
    monkeypatch.setattr(settings, "core_storage_role", "writer")
    return init_mod.init_database


# (revision, the revision before it, the column its upgrade() adds before its
# autocommit block, the DDL the interrupted attempt had already committed, the
# index the block builds). The DDL matches what each migration's add_column
# emits, so the retry meets exactly the state a killed first attempt leaves.
_INTERRUPTED = [
    (
        "007",
        "006",
        ("memories", "client_request_id"),
        "ALTER TABLE memories ADD COLUMN client_request_id text",
        "ix_memories_attempt_unique",
    ),
    (
        "037",
        "036",
        ("memories", "embedded_content_hash"),
        "ALTER TABLE memories ADD COLUMN embedded_content_hash text",
        "ix_memories_stale_embedding",
    ),
    (
        "045",
        "044",
        ("memories", "status_changed_at"),
        "ALTER TABLE memories ADD COLUMN status_changed_at timestamptz",
        "ix_memories_status_changed_at",
    ),
    (
        "048",
        "047",
        ("memory_entity_links", "source"),
        "ALTER TABLE memory_entity_links ADD COLUMN source text NOT NULL DEFAULT 'caller'",
        "ix_memory_entity_links_extraction",
    ),
    (
        "050",
        "049",
        ("documents", "agent_id"),
        "ALTER TABLE documents ADD COLUMN agent_id text",
        "ix_documents_tenant_agent",
    ),
]


@pytest.mark.parametrize(
    "revision,previous,column,committed_ddl,index", _INTERRUPTED, ids=[case[0] for case in _INTERRUPTED]
)
async def test_an_upgrade_interrupted_in_its_autocommit_block_is_retryable(
    scratch_engine: AsyncEngine,
    revision: str,
    previous: str,
    column: tuple[str, str],
    committed_ddl: str,
    index: str,
) -> None:
    """Stop before the migration, put the database in the state a killed first
    attempt leaves — column committed, index not built, revision unrecorded —
    and retry it, the way the next boot does."""
    table, name = column
    await _upgrade(scratch_engine, previous)
    assert await _revision(scratch_engine) == previous
    await _execute(scratch_engine, committed_ddl)

    await _upgrade(scratch_engine, revision)

    assert await _revision(scratch_engine) == revision, f"{revision} did not complete on retry"
    assert await _has_column(scratch_engine, table, name)
    assert await _has_valid_index(scratch_engine, index), f"{revision}'s retry did not build {index}"


async def test_a_chain_built_database_short_of_head_is_not_stamped(
    scratch_engine: AsyncEngine, scratch_init
) -> None:
    """Built through 036 by the chain, then its version row lost. The 019
    sentinel is present, and that alone used to earn a stamp at head — after
    which 037 onwards never ran and ``memories.embedded_content_hash``, which
    the ORM selects on every read, was never added."""
    await _upgrade(scratch_engine, "036")
    await _execute(scratch_engine, "DROP TABLE alembic_version")

    with pytest.raises(RuntimeError, match="cannot be shown to be at head"):
        await scratch_init()

    assert await _revision(scratch_engine) is None, "the refusal still recorded a revision"

    # The documented way out: stamp the revision it really is at, and boot
    # runs the rest.
    cfg = _alembic_cfg()

    def stamp(connection) -> None:
        cfg.attributes["connection"] = connection
        command.stamp(cfg, "036")

    async with scratch_engine.connect() as conn:
        await conn.run_sync(stamp)
        await conn.commit()
    await scratch_init()

    assert await _revision(scratch_engine) == _script_head()
    assert await _has_column(scratch_engine, "memories", "embedded_content_hash")


async def test_a_chain_built_database_at_head_is_still_stamped(
    scratch_engine: AsyncEngine, scratch_init
) -> None:
    """The case the stamp branch exists for keeps working: a full chain-built
    schema restored without ``alembic_version`` is recorded at head, and
    nothing is re-run over it."""
    await _upgrade(scratch_engine, "head")
    await _execute(scratch_engine, "DROP TABLE alembic_version")

    await scratch_init()

    assert await _revision(scratch_engine) == _script_head()
