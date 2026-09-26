"""08/14 L-25 + 09/02 L-13 + 09/02 L-14 — the models and the migration chain
disagreed, and ``alembic revision --autogenerate`` turned that into DROPs.

Autogenerate compares ``Base.metadata`` against the live schema and emits a
removal for anything it cannot see. Three separate gaps fed it:

* ``env.py`` imported thirteen model modules by hand and omitted ``recall_log``,
  so ``recall_event`` and ``recall_candidate`` — created by migration 027 and
  present in every deployed database — were invisible. Measured before the fix:
  a plain autogenerate run emitted ``op.drop_table('recall_candidate')`` and
  ``op.drop_table('recall_event')``.
* Seven indexes existed only in the models. Every one came from ``index=True``
  on a column, which generates SQLAlchemy's own ``ix_<table>_<column>`` name
  beside a differently-named ``Index(...)`` the migration actually created — so
  the model described two indexes where the database had one.
* A GIN index on ``documents.data`` that no query could use: every access is
  ``data['k'].astext`` equality, which a ``jsonb_ops`` GIN index does not serve.

These tests use Alembic's own ``compare_metadata`` rather than comparing index
NAMES, because a name match is not agreement. ``ix_analysis_reports_tenant_started``
is declared ``started_at.desc()`` in the model; migration 049 first created it
ASC, which a name comparison called identical and ``compare_metadata`` correctly
reported as a drop-and-rebuild. That is how the direction was caught.

The suite's schema comes from the real migration chain (``init_database()`` in
``conftest``), not ``create_all``, so "the migrated schema" here is the thing a
deployment actually has.
"""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

import common.models  # noqa: F401  — registers every model; the fix under test
from common.models.base import Base
from core_storage_api.database.init import get_engine

# No blanket ``pytest.mark.asyncio``: ``asyncio_mode = auto`` already covers the
# async cases and the RLS sweep below touches no database.

# Tables the migration chain owns with no ORM model at all. Autogenerate cannot
# be taught about these by importing anything, so they are listed rather than
# fixed: the point of the list is that it is SHORT and explicit, and that a new
# entry has to be added deliberately.
_TABLES_WITHOUT_MODELS = {
    # Created by migration 019 (CAURA-694) and read through raw SQL.
    "tenant_suppression",
}

# Indexes the migrations create that the models do not express. Left undeclared
# rather than back-filled: these are partial, GIN and HNSW indexes whose exact
# predicates and operator classes are load-bearing, and transcribing seventeen
# of them into the models is a change with its own risk profile. Listed so that
# an EIGHTEENTH — a genuinely new drift — fails this test instead of joining a
# crowd nobody is counting.
_INDEXES_ONLY_IN_MIGRATIONS = {
    "ix_audit_log_tenant_event_hash",
    "ix_audit_log_tenant_id",
    "uq_audit_log_tenant_seq",
    "ix_documents_embedding_hnsw",
    "ix_entities_name_embedding_hnsw",
    "ix_entities_search_vector",
    "ix_fleet_commands_tenant_id",
    "ix_fleet_nodes_tenant_id",
    "ix_memories_agent_id_active",
    "ix_memories_embedding_hnsw",
    "ix_memories_run_id",
    "ix_memories_search_vector",
    "ix_memories_stale_embedding",
    "ix_memories_status",
    "ix_memories_status_changed_at",
    "ix_memories_tenant_id_active",
    "ix_memories_visibility",
}


def _compare(sync_conn) -> list:
    return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)


async def _diffs() -> list:
    conn: AsyncConnection
    async with get_engine().connect() as conn:
        return await conn.run_sync(_compare)


def _named(diffs: list, kind: str) -> set[str]:
    return {d[1].name for d in diffs if d[0] == kind}


async def test_no_table_in_the_schema_is_invisible_to_autogenerate() -> None:
    """The finding, as an outcome: which tables would be proposed for DROP.

    Anything here that is not a known model-less table is a table whose model
    exists but is not reachable from ``common.models`` — which is the exact
    shape of the ``recall_log`` omission, and is silent until someone runs
    autogenerate and reads the diff carefully.
    """
    dropped = _named(await _diffs(), "remove_table")
    assert dropped <= _TABLES_WITHOUT_MODELS, (
        "autogenerate would emit DROP TABLE for these, and they have models: "
        f"{sorted(dropped - _TABLES_WITHOUT_MODELS)}"
    )


async def test_no_model_declares_an_index_the_schema_does_not_have() -> None:
    """The other direction, and the one with no allowlist.

    A model index missing from the migrated schema is always a defect: either
    the index was wanted and no migration builds it (``ix_memory_derivations_tenant_id``,
    which the org purge needed and never had), or it was never wanted and the
    model should stop claiming it (``ix_documents_data``). There is no third
    case worth tolerating, so this set must be empty.
    """
    added = _named(await _diffs(), "add_index")
    assert added == set(), f"these are declared on a model and exist in no migration: {sorted(added)}"


async def test_the_indexes_only_the_migrations_know_about_are_the_known_ones() -> None:
    """Guard on the residual, not a claim that the residual is fine.

    These seventeen WOULD be dropped by an autogenerate-generated migration —
    including three HNSW vector indexes and two GIN full-text indexes, whose
    loss would not fail a single test and would quietly change search from an
    index scan to a sequential one. Pinning the set means the next reader finds
    a counted, deliberate list rather than discovering the hazard the way this
    PR did.
    """
    removed = _named(await _diffs(), "remove_index")
    assert removed == _INDEXES_ONLY_IN_MIGRATIONS, (
        f"appeared: {sorted(removed - _INDEXES_ONLY_IN_MIGRATIONS)}\n"
        f"disappeared: {sorted(_INDEXES_ONLY_IN_MIGRATIONS - removed)}"
    )


async def test_the_schema_really_has_no_row_level_security() -> None:
    """The ground truth the prose was wrong about, asked of the database.

    09/02 L-46: migration 027 said the recall tables were "RLS-scoped by
    ``tenant_id`` like every other tenant table". A reader auditing tenant
    isolation would have taken that as a database-enforced boundary and stopped
    looking for the query-level one that actually does the work.

    Asserted against ``pg_class`` and ``pg_policies`` rather than against the
    migration text, because the text is the thing that was wrong. If RLS is ever
    genuinely adopted this fails, which is the right moment to revisit every
    docstring that describes tenant scoping.
    """
    async with get_engine().connect() as conn:
        enabled = (
            (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relrowsecurity"
                    )
                )
            )
            .scalars()
            .all()
        )
        policies = (
            (await conn.execute(text("SELECT policyname FROM pg_policies WHERE schemaname = 'public'")))
            .scalars()
            .all()
        )

    assert list(enabled) == [], f"RLS is enabled on {sorted(enabled)} — the docstrings need revisiting"
    assert list(policies) == [], f"row-level policies exist: {sorted(policies)}"


def test_no_migration_claims_the_schema_is_rls_scoped() -> None:
    """The specific false phrase, kept out rather than every mention of RLS.

    A blanket "no migration mentions RLS" sweep is what I wrote first, and it
    failed on ``023_capability_usage``, which mentions RLS precisely to say this
    schema does not use it — an accurate statement, and one worth keeping. An
    assertion and a denial read almost alike to a text search, so this matches
    only the affirmative form that was actually wrong.
    """
    from pathlib import Path

    versions = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "core_storage_api"
        / "database"
        / "migrations"
        / "versions"
    )
    claims = [
        f"{path.name}:{number}"
        for path in sorted(versions.glob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if "RLS-scoped" in line and "said" not in line
    ]
    assert not claims, f"these describe the schema as RLS-scoped, and it is not: {claims}"
