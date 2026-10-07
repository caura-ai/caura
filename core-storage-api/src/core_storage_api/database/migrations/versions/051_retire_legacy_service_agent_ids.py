"""Move the two legacy service-agent identities to their Caura IDs.

Step 1 deployed dual-read/new-write behavior before this migration. This step
moves attribution and registration together so historical memories remain
visible to the agents that wrote them.

Some tenant may already have both spellings after Step 1. For agents, the new
row is the merge target and its populated fields win; nullable gaps are filled
from the legacy row before that row is removed. For memories, canonicalizing
the ID could collapse two otherwise distinct live-content keys. The migration
checks that precondition and aborts before changing anything rather than
silently deleting either memory.

The transaction takes a SHARE ROW EXCLUSIVE lock on both tables. That pauses
writes globally, but reads continue and the measured move is only 2,970 memory
rows plus four agent rows in production. The lock is required because row locks
cannot prevent a new canonical insert between the collision check and rename;
an advisory lock would not help unless every application writer also took it.
Lock acquisition is capped at three seconds and each subsequent statement at
30, so an unexpected blocker fails the deploy cleanly instead of stalling.

Revision ID: 051
Revises: 050
Create Date: 2026-09-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "051"
down_revision: str | None = "050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
_STATEMENT_TIMEOUT = "SET LOCAL statement_timeout = '30s'"
_LOCK_TABLES = "LOCK TABLE agents, memories IN SHARE ROW EXCLUSIVE MODE"

_OLD_INSIGHTER_ID = "memclaw-insighter"  # legacy-name-floor: frozen pre-051 row identity
_OLD_DOC_INDEXER_ID = "memclaw-doc-indexer"  # legacy-name-floor: frozen pre-051 row identity
_NEW_INSIGHTER_ID = "caura-insighter"
_NEW_DOC_INDEXER_ID = "caura-doc-indexer"

_UPGRADE_IDENTITY_MAP = f"""
    VALUES
        ('{_OLD_INSIGHTER_ID}', '{_NEW_INSIGHTER_ID}'),
        ('{_OLD_DOC_INDEXER_ID}', '{_NEW_DOC_INDEXER_ID}')
"""
_DOWNGRADE_IDENTITY_MAP = f"""
    VALUES
        ('{_NEW_INSIGHTER_ID}', '{_OLD_INSIGHTER_ID}'),
        ('{_NEW_DOC_INDEXER_ID}', '{_OLD_DOC_INDEXER_ID}')
"""
_TARGET_AGENT_IDS = f"""
    '{_OLD_INSIGHTER_ID}',
    '{_NEW_INSIGHTER_ID}',
    '{_OLD_DOC_INDEXER_ID}',
    '{_NEW_DOC_INDEXER_ID}'
"""

_UPGRADE_MEMORY_COLLISION_GUARD = f"""
    DO $migration$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM memories
            WHERE deleted_at IS NULL
              AND content_hash IS NOT NULL
              AND agent_id IN (
                  {_TARGET_AGENT_IDS}
              )
            GROUP BY tenant_id,
                     COALESCE(fleet_id, ''),
                     CASE agent_id
                         WHEN '{_OLD_INSIGHTER_ID}'
                         THEN '{_NEW_INSIGHTER_ID}'
                         WHEN '{_OLD_DOC_INDEXER_ID}'
                         THEN '{_NEW_DOC_INDEXER_ID}'
                         ELSE agent_id
                     END,
                     content_hash
            HAVING count(*) > 1
        ) THEN
            RAISE EXCEPTION
                'migration 051 would create duplicate live memory keys';
        END IF;
    END
    $migration$
"""

_UPGRADE_MERGE_AGENTS = f"""
    WITH identity_map(old_id, new_id) AS (
        {_UPGRADE_IDENTITY_MAP}
    )
    UPDATE agents AS target
    SET fleet_id = COALESCE(target.fleet_id, source.fleet_id),
        display_name = COALESCE(target.display_name, source.display_name),
        install_id = COALESCE(target.install_id, source.install_id),
        owner_install_uuid = COALESCE(
            target.owner_install_uuid,
            source.owner_install_uuid
        ),
        search_profile = COALESCE(target.search_profile, source.search_profile),
        owner_ref = COALESCE(target.owner_ref, source.owner_ref),
        created_at = COALESCE(
            LEAST(target.created_at, source.created_at),
            target.created_at,
            source.created_at
        ),
        updated_at = COALESCE(
            GREATEST(target.updated_at, source.updated_at),
            target.updated_at,
            source.updated_at
        )
    FROM agents AS source, identity_map
    WHERE source.agent_id = identity_map.old_id
      AND target.tenant_id = source.tenant_id
      AND target.agent_id = identity_map.new_id
"""

_UPGRADE_RENAME_MEMORIES = f"""
    WITH identity_map(old_id, new_id) AS (
        {_UPGRADE_IDENTITY_MAP}
    )
    UPDATE memories AS memory
    SET agent_id = identity_map.new_id
    FROM identity_map
    WHERE memory.agent_id = identity_map.old_id
"""

_UPGRADE_DELETE_AGENT_COLLISIONS = f"""
    WITH identity_map(old_id, new_id) AS (
        {_UPGRADE_IDENTITY_MAP}
    )
    DELETE FROM agents AS source
    USING agents AS target, identity_map
    WHERE source.agent_id = identity_map.old_id
      AND target.tenant_id = source.tenant_id
      AND target.agent_id = identity_map.new_id
"""

_UPGRADE_RENAME_AGENTS = f"""
    WITH identity_map(old_id, new_id) AS (
        {_UPGRADE_IDENTITY_MAP}
    )
    UPDATE agents AS agent
    SET agent_id = identity_map.new_id
    FROM identity_map
    WHERE agent.agent_id = identity_map.old_id
"""

UPGRADE_STATEMENTS = (
    _LOCK_TIMEOUT,
    _STATEMENT_TIMEOUT,
    _LOCK_TABLES,
    _UPGRADE_MEMORY_COLLISION_GUARD,
    _UPGRADE_MERGE_AGENTS,
    _UPGRADE_RENAME_MEMORIES,
    _UPGRADE_DELETE_AGENT_COLLISIONS,
    _UPGRADE_RENAME_AGENTS,
)

_DOWNGRADE_MEMORY_COLLISION_GUARD = f"""
    DO $migration$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM memories
            WHERE deleted_at IS NULL
              AND content_hash IS NOT NULL
              AND agent_id IN (
                  {_TARGET_AGENT_IDS}
              )
            GROUP BY tenant_id,
                     COALESCE(fleet_id, ''),
                     CASE agent_id
                         WHEN '{_NEW_INSIGHTER_ID}'
                         THEN '{_OLD_INSIGHTER_ID}'
                         WHEN '{_NEW_DOC_INDEXER_ID}'
                         THEN '{_OLD_DOC_INDEXER_ID}'
                         ELSE agent_id
                     END,
                     content_hash
            HAVING count(*) > 1
        ) THEN
            RAISE EXCEPTION
                'migration 051 downgrade would create duplicate live memory keys';
        END IF;
    END
    $migration$
"""

_DOWNGRADE_MERGE_AGENTS = f"""
    WITH identity_map(new_id, old_id) AS (
        {_DOWNGRADE_IDENTITY_MAP}
    )
    UPDATE agents AS target
    SET fleet_id = COALESCE(target.fleet_id, source.fleet_id),
        display_name = COALESCE(target.display_name, source.display_name),
        install_id = COALESCE(target.install_id, source.install_id),
        owner_install_uuid = COALESCE(
            target.owner_install_uuid,
            source.owner_install_uuid
        ),
        search_profile = COALESCE(target.search_profile, source.search_profile),
        owner_ref = COALESCE(target.owner_ref, source.owner_ref),
        created_at = COALESCE(
            LEAST(target.created_at, source.created_at),
            target.created_at,
            source.created_at
        ),
        updated_at = COALESCE(
            GREATEST(target.updated_at, source.updated_at),
            target.updated_at,
            source.updated_at
        )
    FROM agents AS source, identity_map
    WHERE source.agent_id = identity_map.new_id
      AND target.tenant_id = source.tenant_id
      AND target.agent_id = identity_map.old_id
"""

_DOWNGRADE_RENAME_MEMORIES = f"""
    WITH identity_map(new_id, old_id) AS (
        {_DOWNGRADE_IDENTITY_MAP}
    )
    UPDATE memories AS memory
    SET agent_id = identity_map.old_id
    FROM identity_map
    WHERE memory.agent_id = identity_map.new_id
"""

_DOWNGRADE_DELETE_AGENT_COLLISIONS = f"""
    WITH identity_map(new_id, old_id) AS (
        {_DOWNGRADE_IDENTITY_MAP}
    )
    DELETE FROM agents AS source
    USING agents AS target, identity_map
    WHERE source.agent_id = identity_map.new_id
      AND target.tenant_id = source.tenant_id
      AND target.agent_id = identity_map.old_id
"""

_DOWNGRADE_RENAME_AGENTS = f"""
    WITH identity_map(new_id, old_id) AS (
        {_DOWNGRADE_IDENTITY_MAP}
    )
    UPDATE agents AS agent
    SET agent_id = identity_map.old_id
    FROM identity_map
    WHERE agent.agent_id = identity_map.new_id
"""

DOWNGRADE_STATEMENTS = (
    _LOCK_TIMEOUT,
    _STATEMENT_TIMEOUT,
    _LOCK_TABLES,
    _DOWNGRADE_MEMORY_COLLISION_GUARD,
    _DOWNGRADE_MERGE_AGENTS,
    _DOWNGRADE_RENAME_MEMORIES,
    _DOWNGRADE_DELETE_AGENT_COLLISIONS,
    _DOWNGRADE_RENAME_AGENTS,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
