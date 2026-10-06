"""Repair service-agent rows written during the 051 deployment window.

Migration 051 and the dual-read/new-write code reached production in one
deploy. Storage completed first, so the still-running pre-cutover core API
re-created legacy doc-indexer rows before its replacement became ready.

The 051 upgrade is deliberately idempotent: it merges an old/new agent-row
collision, moves memories, removes the old row, and then renames any remaining
old-only agent row. Reusing those frozen statements keeps this repair identical
to the reviewed migration. This migration also repairs cached activity digests,
which 051 did not cover. On an old/new natural-key collision, the payload from
the newer run remains visible under the canonical identity; the canonical row
wins a run-freshness tie.

Revision ID: 052
Revises: 051
Create Date: 2026-09-22
"""

from collections.abc import Sequence
from importlib import import_module

from alembic import op

revision: str = "052"
down_revision: str | None = "051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_migration_051 = import_module(
    "core_storage_api.database.migrations.versions.051_retire_legacy_service_agent_ids"
)

_LOCK_DIGESTS = "LOCK TABLE agent_activity_digests IN SHARE ROW EXCLUSIVE MODE"

_MERGE_DIGEST_COLLISIONS = f"""
    WITH identity_map(old_id, new_id) AS (
        {_migration_051._UPGRADE_IDENTITY_MAP}
    ),
    run_freshness AS (
        SELECT tenant_id, period, run_id, max(generated_at) AS generated_at
        FROM agent_activity_digests
        GROUP BY tenant_id, period, run_id
    )
    UPDATE agent_activity_digests AS target
    SET run_id = source.run_id,
        window_end = source.window_end,
        narrative = source.narrative,
        sections = source.sections,
        subagents = source.subagents,
        source_count = source.source_count,
        recall_count = source.recall_count,
        model = source.model,
        status = source.status,
        error_detail = source.error_detail,
        generated_at = source.generated_at
    FROM agent_activity_digests AS source,
         identity_map,
         run_freshness AS source_run,
         run_freshness AS target_run
    WHERE source.agent_id = identity_map.old_id
      AND target.agent_id = identity_map.new_id
      AND target.tenant_id = source.tenant_id
      AND target.fleet_id IS NOT DISTINCT FROM source.fleet_id
      AND target.period = source.period
      AND target.window_start = source.window_start
      AND source_run.tenant_id = source.tenant_id
      AND source_run.period = source.period
      AND source_run.run_id = source.run_id
      AND target_run.tenant_id = target.tenant_id
      AND target_run.period = target.period
      AND target_run.run_id = target.run_id
      AND source_run.generated_at > target_run.generated_at
"""

_DELETE_DIGEST_COLLISIONS = f"""
    WITH identity_map(old_id, new_id) AS (
        {_migration_051._UPGRADE_IDENTITY_MAP}
    )
    DELETE FROM agent_activity_digests AS source
    USING agent_activity_digests AS target, identity_map
    WHERE source.agent_id = identity_map.old_id
      AND target.agent_id = identity_map.new_id
      AND target.tenant_id = source.tenant_id
      AND target.fleet_id IS NOT DISTINCT FROM source.fleet_id
      AND target.period = source.period
      AND target.window_start = source.window_start
"""

_RENAME_DIGESTS = f"""
    WITH identity_map(old_id, new_id) AS (
        {_migration_051._UPGRADE_IDENTITY_MAP}
    )
    UPDATE agent_activity_digests AS digest
    SET agent_id = identity_map.new_id
    FROM identity_map
    WHERE digest.agent_id = identity_map.old_id
"""

UPGRADE_STATEMENTS: tuple[str, ...] = (
    *_migration_051.UPGRADE_STATEMENTS,
    _LOCK_DIGESTS,
    _MERGE_DIGEST_COLLISIONS,
    _DELETE_DIGEST_COLLISIONS,
    _RENAME_DIGESTS,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Preserve 051's canonical-ID invariant.

    The repair does not record which canonical rows predated 052, so reversing
    only its stragglers is impossible. Replaying 051's downgrade here would
    incorrectly rename every canonical row while Alembic still reports 051.
    """
