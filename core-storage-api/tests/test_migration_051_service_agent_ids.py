"""Migration 051 moves both legacy service-agent identities together."""

from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD_INSIGHTER_ID = "memclaw-insighter"  # legacy-name-floor: migration input fixture
_OLD_DOC_INDEXER_ID = "memclaw-doc-indexer"  # legacy-name-floor: migration input fixture
_NEW_INSIGHTER_ID = "caura-insighter"
_NEW_DOC_INDEXER_ID = "caura-doc-indexer"


def _load_migration_051():
    path = (
        pathlib.Path("core-storage-api/src/core_storage_api/database/migrations/versions")
        / "051_retire_legacy_service_agent_ids.py"
    )
    spec = importlib.util.spec_from_file_location("migration_051", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(session, statements: tuple[str, ...]) -> None:
    for statement in statements:
        await session.execute(text(statement))


async def test_upgrade_and_downgrade_merge_both_identities(_ensure_schema) -> None:
    migration = _load_migration_051()
    suffix = uuid.uuid4().hex[:8]
    tenant = f"t-051-{suffix}"
    fleet = f"f-051-{suffix}"
    old_hash = f"old-hash-051-{suffix}"
    new_hash = f"new-hash-051-{suffix}"

    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO agents
                    (tenant_id, agent_id, fleet_id, display_name, install_id,
                     owner_install_uuid, trust_level, search_profile,
                     belonging_type, owner_ref, created_at, updated_at)
                VALUES
                    (:tenant, :old_insighter, :fleet, 'Caura Insighter',
                     'legacy-install', '00000000-0000-0000-0000-000000000051',
                     3, '{"source": "legacy"}'::json, 'service', NULL,
                     '2026-01-01T00:00:00Z', '2026-03-01T00:00:00Z'),
                    (:tenant, :new_insighter, NULL, 'Caura Insighter',
                     NULL, NULL, 1, NULL, 'personal', 'canonical-owner',
                     '2026-02-01T00:00:00Z', NULL),
                    (:tenant, :old_doc_indexer, :fleet,
                     'Caura Doc Indexer', NULL, NULL, 3, NULL, 'service', NULL,
                     '2026-01-01T00:00:00Z', NULL)
                """
            ),
            {
                "tenant": tenant,
                "fleet": fleet,
                "old_insighter": _OLD_INSIGHTER_ID,
                "new_insighter": _NEW_INSIGHTER_ID,
                "old_doc_indexer": _OLD_DOC_INDEXER_ID,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO memories
                    (tenant_id, fleet_id, agent_id, memory_type, content,
                     content_hash, created_at, status)
                VALUES
                    (:tenant, :fleet, :old_insighter, 'fact',
                     'legacy insight', :old_hash, '2026-01-01T00:00:00Z', 'active'),
                    (:tenant, :fleet, :new_insighter, 'fact',
                     'canonical insight', :new_hash, '2026-02-01T00:00:00Z', 'active'),
                    (:tenant, :fleet, :old_doc_indexer, 'fact',
                     'indexed document', :doc_hash, '2026-01-01T00:00:00Z', 'active')
                """
            ),
            {
                "tenant": tenant,
                "fleet": fleet,
                "old_hash": old_hash,
                "new_hash": new_hash,
                "doc_hash": f"doc-{suffix}",
                "old_insighter": _OLD_INSIGHTER_ID,
                "new_insighter": _NEW_INSIGHTER_ID,
                "old_doc_indexer": _OLD_DOC_INDEXER_ID,
            },
        )

    try:
        async with get_session() as session:
            await _run(session, migration.UPGRADE_STATEMENTS)

        async with get_session() as session:
            agents = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT agent_id, fleet_id, display_name, install_id,
                               owner_install_uuid, trust_level,
                               search_profile::jsonb AS search_profile,
                               belonging_type, owner_ref, created_at, updated_at
                        FROM agents
                        WHERE tenant_id = :tenant
                        ORDER BY agent_id
                        """
                        ),
                        {"tenant": tenant},
                    )
                )
                .mappings()
                .all()
            )
            memories = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT agent_id, content, deleted_at, status
                        FROM memories
                        WHERE tenant_id = :tenant
                        ORDER BY content
                        """
                        ),
                        {"tenant": tenant},
                    )
                )
                .mappings()
                .all()
            )

        assert [row["agent_id"] for row in agents] == [
            _NEW_DOC_INDEXER_ID,
            _NEW_INSIGHTER_ID,
        ]
        insighter = next(row for row in agents if row["agent_id"] == _NEW_INSIGHTER_ID)
        assert insighter["fleet_id"] == fleet
        assert insighter["display_name"] == "Caura Insighter"
        assert insighter["install_id"] == "legacy-install"
        assert insighter["owner_install_uuid"] == "00000000-0000-0000-0000-000000000051"
        assert insighter["trust_level"] == 1
        assert insighter["search_profile"] == {"source": "legacy"}
        assert insighter["belonging_type"] == "personal"
        assert insighter["owner_ref"] == "canonical-owner"
        assert insighter["created_at"].isoformat().startswith("2026-01-01")
        assert insighter["updated_at"].isoformat().startswith("2026-03-01")

        assert {row["agent_id"] for row in memories} == {
            _NEW_INSIGHTER_ID,
            _NEW_DOC_INDEXER_ID,
        }
        by_content = {row["content"]: row for row in memories}
        assert all(row["deleted_at"] is None for row in by_content.values())
        assert all(row["status"] == "active" for row in by_content.values())

        async with get_session() as session:
            await _run(session, migration.DOWNGRADE_STATEMENTS)

        async with get_session() as session:
            agent_ids = (
                (
                    await session.execute(
                        text("SELECT agent_id FROM agents WHERE tenant_id = :tenant ORDER BY agent_id"),
                        {"tenant": tenant},
                    )
                )
                .scalars()
                .all()
            )
            memory_ids = (
                (
                    await session.execute(
                        text("SELECT DISTINCT agent_id FROM memories WHERE tenant_id = :tenant"),
                        {"tenant": tenant},
                    )
                )
                .scalars()
                .all()
            )

        assert agent_ids == [_OLD_DOC_INDEXER_ID, _OLD_INSIGHTER_ID]
        assert set(memory_ids) == {_OLD_DOC_INDEXER_ID, _OLD_INSIGHTER_ID}
    finally:
        async with get_session() as session:
            await session.execute(text("DELETE FROM memories WHERE tenant_id = :tenant"), {"tenant": tenant})
            await session.execute(text("DELETE FROM agents WHERE tenant_id = :tenant"), {"tenant": tenant})


async def test_upgrade_rejects_memory_collisions_without_mutating_rows(_ensure_schema) -> None:
    migration = _load_migration_051()
    suffix = uuid.uuid4().hex[:8]
    tenant = f"t-051-collision-{suffix}"
    fleet = f"f-051-collision-{suffix}"
    content_hash = f"hash-051-collision-{suffix}"

    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO memories
                    (tenant_id, fleet_id, agent_id, memory_type, content,
                     content_hash, created_at, status)
                VALUES
                    (:tenant, :fleet, :old_id, 'fact', 'legacy copy',
                     :content_hash, '2026-01-01T00:00:00Z', 'active'),
                    (:tenant, :fleet, :new_id, 'fact', 'canonical copy',
                     :content_hash, '2026-02-01T00:00:00Z', 'active')
                """
            ),
            {
                "tenant": tenant,
                "fleet": fleet,
                "old_id": _OLD_INSIGHTER_ID,
                "new_id": _NEW_INSIGHTER_ID,
                "content_hash": content_hash,
            },
        )

    try:
        with pytest.raises(DBAPIError, match="would create duplicate live memory keys"):
            async with get_session() as session:
                await _run(session, migration.UPGRADE_STATEMENTS)

        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT agent_id, deleted_at, status
                            FROM memories
                            WHERE tenant_id = :tenant
                            ORDER BY agent_id
                            """
                        ),
                        {"tenant": tenant},
                    )
                )
                .mappings()
                .all()
            )

        assert [row["agent_id"] for row in rows] == [_NEW_INSIGHTER_ID, _OLD_INSIGHTER_ID]
        assert all(row["deleted_at"] is None for row in rows)
        assert all(row["status"] == "active" for row in rows)
    finally:
        async with get_session() as session:
            await session.execute(text("DELETE FROM memories WHERE tenant_id = :tenant"), {"tenant": tenant})
