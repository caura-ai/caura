"""Migration 057's backfill: entities written before it gain their aliases in FTS.

057 changes the trigger, so a row's vector picks up its aliases the next time
its name or attributes are written. Rows nobody touches keep a name-only vector
until ``core_storage_api.scripts.backfill_057_entity_search_vector`` rebuilds
them, out of band, as 034's backfill does for memories.

The script imports nothing from the migration, so the two expressions are pinned
equal here: if they diverged, the backfill would write vectors the trigger never
produces, and the difference would show only as a missed search.
"""

import importlib
import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import PostgresService, get_session

_svc = PostgresService()
_VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "src/core_storage_api/database/migrations/versions"


def _backfill():
    """The script, imported when a test runs rather than at collection, so a tree
    without it fails these tests instead of stopping the whole suite."""
    return importlib.import_module("core_storage_api.scripts.backfill_057_entity_search_vector")


def _migration_057():
    [path] = _VERSIONS.glob("057_*.py")
    spec = importlib.util.spec_from_file_location("migration_057", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.usefixtures("_ensure_schema")
async def test_the_backfill_indexes_the_aliases_of_rows_written_before_057():
    tenant = f"test-backfill-057-{uuid.uuid4().hex[:8]}"
    entity = await _svc.entity_add(
        {
            "tenant_id": tenant,
            "entity_type": "organization",
            "canonical_name": "international business machines",
            "attributes": {"_aliases": ["IBM"]},
        }
    )
    # What a row written under 001's trigger holds. Writing search_vector alone
    # does not fire the trigger, which watches the name and attributes.
    async with get_session() as session:
        await session.execute(
            text("UPDATE entities SET search_vector = to_tsvector('english', canonical_name) WHERE id = :id"),
            {"id": entity.id},
        )
    assert await _svc.entity_fts_search(["ibm"], tenant) == []

    assert await _backfill()._run(batch=500, from_id=None, dry_run=False, revert=False) == 0

    assert await _svc.entity_fts_search(["ibm"], tenant) == [entity.id]


def test_the_backfill_and_the_trigger_build_the_same_vector():
    migration, backfill = _migration_057(), _backfill()
    assert migration._vector("e.") == backfill._VECTOR
    assert migration._name_only("e.") == backfill._NAME_ONLY
