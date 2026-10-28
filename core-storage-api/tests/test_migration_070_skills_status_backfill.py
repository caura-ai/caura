"""Migration 070 keeps the skills already on nodes' disks installed.

``POST /skills/installable`` now serves ``status='active'`` skills to every
tenant, where it used to serve every row to a tenant that had not turned the
Skill Factory on. Rows such a tenant wrote after migration 022 can have no
``status`` at all; without this backfill they would leave every node's disk on
its next heartbeat.

The backfill marks them ``active``, only in tenants that never turned the Skill
Factory on: an opted-in tenant's catalog was already active-only, so a
status-less row there was never installed and is not installed now. Rows that
carry a status keep it. The downgrade removes only what the upgrade added, and
keeps a status an admin set since.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid

import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _load_migration_070():
    path = (
        pathlib.Path("core-storage-api/src/core_storage_api/database/migrations/versions")
        / "070_skills_status_backfill.py"
    )
    spec = importlib.util.spec_from_file_location("migration_070", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(session, statements: tuple[str, ...]) -> None:
    for statement in statements:
        await session.execute(text(statement))


async def _statuses(tenant: str) -> dict[str, dict]:
    async with get_session() as session:
        rows = await session.execute(
            text(
                "SELECT doc_id, data::jsonb AS data FROM documents WHERE tenant_id = :t AND collection = 'skills'"
            ),
            {"t": tenant},
        )
        return {row.doc_id: row.data for row in rows}


async def test_status_less_skills_stay_installed_for_tenants_that_never_opted_in(_ensure_schema) -> None:
    migration = _load_migration_070()
    suffix = uuid.uuid4().hex[:8]
    legacy, opted_in = f"t-070-legacy-{suffix}", f"t-070-optin-{suffix}"

    async with get_session() as session:
        await session.execute(
            text("INSERT INTO organization_settings (org_id, settings) VALUES (:org, CAST(:s AS jsonb))"),
            {"org": opted_in, "s": json.dumps({"skills_factory": {"enabled": True}})},
        )
        for tenant in (legacy, opted_in):
            for doc_id, data in (
                ("no-status", {"name": "a", "content": "# a", "description": "d"}),
                ("later-rejected", {"name": "r", "content": "# r", "description": "d"}),
                ("staged", {"name": "b", "content": "# b", "description": "d", "status": "staged"}),
                ("active", {"name": "c", "content": "# c", "description": "d", "status": "active"}),
            ):
                await session.execute(
                    text(
                        "INSERT INTO documents (tenant_id, collection, doc_id, data) "
                        "VALUES (:t, 'skills', :d, CAST(:data AS jsonb))"
                    ),
                    {"t": tenant, "d": doc_id, "data": json.dumps(data)},
                )

    try:
        async with get_session() as session:
            await _run(session, migration.UPGRADE_STATEMENTS)

        legacy_rows = await _statuses(legacy)
        assert legacy_rows["no-status"]["status"] == "active"
        assert legacy_rows["staged"]["status"] == "staged"
        assert legacy_rows["active"]["status"] == "active"
        # The opted-in tenant's catalog was already active-only: unchanged.
        optin_rows = await _statuses(opted_in)
        assert "status" not in optin_rows["no-status"]

        # Running it again changes nothing.
        async with get_session() as session:
            await _run(session, migration.UPGRADE_STATEMENTS)
        assert await _statuses(legacy) == legacy_rows

        # An admin rejects one of the backfilled skills in the inbox.
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE documents SET data = ((data::jsonb) || jsonb_build_object('status', 'rejected'))::json "
                    "WHERE tenant_id = :t AND collection = 'skills' AND doc_id = 'later-rejected'"
                ),
                {"t": legacy},
            )

        async with get_session() as session:
            await _run(session, migration.DOWNGRADE_STATEMENTS)
        restored = await _statuses(legacy)
        assert "status" not in restored["no-status"]
        assert "_migrated_by" not in restored["no-status"]
        assert restored["active"]["status"] == "active"
        # The admin's decision survives: the row does not turn status-less.
        assert restored["later-rejected"]["status"] == "rejected"
        assert "_migrated_by" not in restored["later-rejected"]
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM documents WHERE tenant_id IN (:a, :b)"), {"a": legacy, "b": opted_in}
            )
            await session.execute(
                text("DELETE FROM organization_settings WHERE org_id = :o"), {"o": opted_in}
            )
