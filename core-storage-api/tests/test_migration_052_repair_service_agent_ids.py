"""Migration 052 repairs service-agent rows written during the 051 deploy."""

from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OLD_INSIGHTER_ID = "memclaw-insighter"  # legacy-name-floor: migration input fixture
_OLD_DOC_INDEXER_ID = "memclaw-doc-indexer"  # legacy-name-floor: migration input fixture
_NEW_INSIGHTER_ID = "caura-insighter"
_NEW_DOC_INDEXER_ID = "caura-doc-indexer"


def _load_migration_052():
    path = (
        pathlib.Path("core-storage-api/src/core_storage_api/database/migrations/versions")
        / "052_repair_legacy_service_agent_ids.py"
    )
    spec = importlib.util.spec_from_file_location("migration_052", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(session, statements: tuple[str, ...]) -> None:
    for statement in statements:
        await session.execute(text(statement))


async def test_upgrade_repairs_post_051_stragglers_idempotently(_ensure_schema) -> None:
    migration = _load_migration_052()
    suffix = uuid.uuid4().hex[:8]
    tenant = f"t-052-{suffix}"
    fleet = f"f-052-{suffix}"
    run_ids = {f"run_{i}": uuid.uuid4() for i in range(1, 7)}

    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO agents
                    (tenant_id, agent_id, fleet_id, display_name, trust_level,
                     belonging_type, created_at, updated_at)
                VALUES
                    (:tenant, :new_doc, :fleet, 'Caura Doc Indexer', 3,
                     'service', '2026-08-25T11:29:52Z', '2026-08-25T11:29:52Z'),
                    (:tenant, :old_doc, :fleet, 'Caura Doc Indexer', 3,
                     'service', '2026-09-22T13:11:16Z', '2026-09-22T13:11:16Z'),
                    (:tenant, :old_insighter, :fleet, 'Caura Insighter', 3,
                     'service', '2026-09-22T13:11:16Z', '2026-09-22T13:11:16Z')
                """
            ),
            {
                "tenant": tenant,
                "fleet": fleet,
                "new_doc": _NEW_DOC_INDEXER_ID,
                "old_doc": _OLD_DOC_INDEXER_ID,
                "old_insighter": _OLD_INSIGHTER_ID,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO memories
                    (tenant_id, fleet_id, agent_id, memory_type, content,
                     content_hash, created_at, status)
                VALUES
                    (:tenant, :fleet, :new_doc, 'fact', 'canonical document',
                     :canonical_hash, '2026-08-25T11:30:00Z', 'active'),
                    (:tenant, :fleet, :old_doc, 'fact', 'straggler 1',
                     :straggler_hash_1, '2026-09-22T13:11:17Z', 'active'),
                    (:tenant, :fleet, :old_doc, 'fact', 'straggler 2',
                     :straggler_hash_2, '2026-09-22T13:11:19Z', 'active'),
                    (:tenant, :fleet, :old_doc, 'fact', 'straggler 3',
                     :straggler_hash_3, '2026-09-22T13:11:21Z', 'active'),
                    (:tenant, :fleet, :old_doc, 'fact', 'straggler 4',
                     :straggler_hash_4, '2026-09-22T13:11:23Z', 'active'),
                    (:tenant, :fleet, :old_doc, 'fact', 'straggler 5',
                     :straggler_hash_5, '2026-09-22T13:11:26Z', 'active'),
                    (:tenant, :fleet, :old_insighter, 'insight', 'straggler insight',
                     :insighter_hash, '2026-09-22T13:11:26Z', 'active')
                """
            ),
            {
                "tenant": tenant,
                "fleet": fleet,
                "new_doc": _NEW_DOC_INDEXER_ID,
                "old_doc": _OLD_DOC_INDEXER_ID,
                "old_insighter": _OLD_INSIGHTER_ID,
                "canonical_hash": f"canonical-052-{suffix}",
                "straggler_hash_1": f"straggler-1-052-{suffix}",
                "straggler_hash_2": f"straggler-2-052-{suffix}",
                "straggler_hash_3": f"straggler-3-052-{suffix}",
                "straggler_hash_4": f"straggler-4-052-{suffix}",
                "straggler_hash_5": f"straggler-5-052-{suffix}",
                "insighter_hash": f"insighter-052-{suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO agent_activity_digests
                    (run_id, tenant_id, fleet_id, agent_id, period,
                     window_start, window_end, narrative, status, generated_at)
                VALUES
                    (:run_1, :tenant, :fleet, :old_doc, 'day',
                     '2026-09-20T00:00:00Z', '2026-09-21T00:00:00Z',
                     'old-only fleet digest', 'ok', '2026-09-21T01:00:00Z'),
                    (:run_2, :tenant, :fleet, :new_doc, 'day',
                     '2026-09-21T00:00:00Z', '2026-09-22T00:00:00Z',
                     'canonical fleet digest', 'ok', '2026-09-22T01:02:00Z'),
                    (:run_3, :tenant, :fleet, :old_doc, 'day',
                     '2026-09-21T00:00:00Z', '2026-09-22T00:00:00Z',
                     'colliding legacy fleet digest', 'ok', '2026-09-22T01:01:00Z'),
                    (:run_3, :tenant, :fleet, 'digest-peer', 'day',
                     '2026-09-21T00:00:00Z', '2026-09-22T00:00:00Z',
                     'newer-run peer digest', 'ok', '2026-09-22T01:03:00Z'),
                    (:run_4, :tenant, NULL, :old_insighter, 'week',
                     '2026-09-08T00:00:00Z', '2026-09-15T00:00:00Z',
                     'old-only no-fleet digest', 'ok', '2026-09-15T01:00:00Z'),
                    (:run_5, :tenant, NULL, :new_insighter, 'week',
                     '2026-09-15T00:00:00Z', '2026-09-22T00:00:00Z',
                     'canonical no-fleet digest', 'ok', '2026-09-22T01:00:00Z'),
                    (:run_6, :tenant, NULL, :old_insighter, 'week',
                     '2026-09-15T00:00:00Z', '2026-09-22T00:00:00Z',
                     'colliding legacy no-fleet digest', 'ok', '2026-09-22T01:00:00Z')
                """
            ),
            {
                "tenant": tenant,
                "fleet": fleet,
                "new_doc": _NEW_DOC_INDEXER_ID,
                "old_doc": _OLD_DOC_INDEXER_ID,
                "new_insighter": _NEW_INSIGHTER_ID,
                "old_insighter": _OLD_INSIGHTER_ID,
                **run_ids,
            },
        )

    try:
        for _ in range(2):
            async with get_session() as session:
                await _run(session, migration.UPGRADE_STATEMENTS)

        async with get_session() as session:
            agent_rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT agent_id, count(*) AS rows, min(created_at) AS created_at
                            FROM agents
                            WHERE tenant_id = :tenant
                            GROUP BY agent_id
                            ORDER BY agent_id
                            """
                        ),
                        {"tenant": tenant},
                    )
                )
                .mappings()
                .all()
            )
            memory_counts = dict(
                (
                    await session.execute(
                        text(
                            """
                            SELECT agent_id, count(*)
                            FROM memories
                            WHERE tenant_id = :tenant
                            GROUP BY agent_id
                            """
                        ),
                        {"tenant": tenant},
                    )
                ).all()
            )
            digest_rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT agent_id, fleet_id, narrative
                            FROM agent_activity_digests
                            WHERE tenant_id = :tenant
                              AND agent_id IN (:new_doc, :new_insighter,
                                               :old_doc, :old_insighter)
                            """
                        ),
                        {
                            "tenant": tenant,
                            "new_doc": _NEW_DOC_INDEXER_ID,
                            "new_insighter": _NEW_INSIGHTER_ID,
                            "old_doc": _OLD_DOC_INDEXER_ID,
                            "old_insighter": _OLD_INSIGHTER_ID,
                        },
                    )
                )
                .mappings()
                .all()
            )

        assert [row["agent_id"] for row in agent_rows] == [
            _NEW_DOC_INDEXER_ID,
            _NEW_INSIGHTER_ID,
        ]
        assert all(row["rows"] == 1 for row in agent_rows)
        doc_agent = next(row for row in agent_rows if row["agent_id"] == _NEW_DOC_INDEXER_ID)
        assert doc_agent["created_at"].isoformat().startswith("2026-08-25")
        assert memory_counts == {
            _NEW_DOC_INDEXER_ID: 6,
            _NEW_INSIGHTER_ID: 1,
        }
        assert {(row["agent_id"], row["fleet_id"], row["narrative"]) for row in digest_rows} == {
            (_NEW_DOC_INDEXER_ID, fleet, "old-only fleet digest"),
            (_NEW_DOC_INDEXER_ID, fleet, "colliding legacy fleet digest"),
            (_NEW_INSIGHTER_ID, None, "old-only no-fleet digest"),
            (_NEW_INSIGHTER_ID, None, "canonical no-fleet digest"),
        }

        latest_day = await PostgresService().agent_activity_digest_get_latest(tenant, "day")
        assert {(row.agent_id, row.run_id, row.narrative) for row in latest_day} == {
            (_NEW_DOC_INDEXER_ID, run_ids["run_3"], "colliding legacy fleet digest"),
            ("digest-peer", run_ids["run_3"], "newer-run peer digest"),
        }

        migration.downgrade()

        async with get_session() as session:
            agent_ids_after_downgrade = set(
                (
                    await session.execute(
                        text("SELECT agent_id FROM agents WHERE tenant_id = :tenant"),
                        {"tenant": tenant},
                    )
                )
                .scalars()
                .all()
            )
            memory_ids_after_downgrade = set(
                (
                    await session.execute(
                        text("SELECT DISTINCT agent_id FROM memories WHERE tenant_id = :tenant"),
                        {"tenant": tenant},
                    )
                )
                .scalars()
                .all()
            )
            digest_ids_after_downgrade = set(
                (
                    await session.execute(
                        text(
                            "SELECT DISTINCT agent_id FROM agent_activity_digests WHERE tenant_id = :tenant"
                        ),
                        {"tenant": tenant},
                    )
                )
                .scalars()
                .all()
            )

        assert agent_ids_after_downgrade == {
            _NEW_DOC_INDEXER_ID,
            _NEW_INSIGHTER_ID,
        }
        assert memory_ids_after_downgrade == {
            _NEW_DOC_INDEXER_ID,
            _NEW_INSIGHTER_ID,
        }
        assert digest_ids_after_downgrade == {
            _NEW_DOC_INDEXER_ID,
            _NEW_INSIGHTER_ID,
            "digest-peer",
        }
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM agent_activity_digests WHERE tenant_id = :tenant"),
                {"tenant": tenant},
            )
            await session.execute(
                text("DELETE FROM memories WHERE tenant_id = :tenant"),
                {"tenant": tenant},
            )
            await session.execute(
                text("DELETE FROM agents WHERE tenant_id = :tenant"),
                {"tenant": tenant},
            )
