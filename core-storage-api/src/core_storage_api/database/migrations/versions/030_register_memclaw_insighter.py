"""Register the ``memclaw-insighter`` service agent for existing tenants.

The automated nightly insights run (core-api
``lifecycle_audit._CoreApiLifecycleAdapter.insights``) now attributes its
writes to a dedicated ``memclaw-insighter`` identity and self-registers that
agent the first time it runs for a tenant. This migration backfills the same
registration for tenants that ALREADY hold ``memclaw-insighter`` memories (e.g.
those renamed from the historical ``mcp-agent`` fallback), so the agent surfaces
in Prism and the per-agent report immediately — without waiting for the next
nightly run.

Data-driven and idempotent: one ``service`` agent per distinct tenant that has
an insighter memory, ``ON CONFLICT (tenant_id, agent_id) DO NOTHING``. trust
level 3 mirrors ``core_api.agent_ids.INSIGHTER_TRUST_LEVEL`` (hardcoded here —
migrations are frozen snapshots and must not import app code).

Revision ID: 030
Revises: 029
Create Date: 2026-07-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO agents (tenant_id, agent_id, fleet_id, display_name, belonging_type, trust_level)
        SELECT DISTINCT m.tenant_id, 'memclaw-insighter', NULL, 'MemClaw Insighter', 'service', 3
        FROM memories m
        WHERE m.agent_id = 'memclaw-insighter'
        ON CONFLICT (tenant_id, agent_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # Reverts the feature: removes every insighter registration, including any
    # the running job self-registered after this migration.
    op.execute("DELETE FROM agents WHERE agent_id = 'memclaw-insighter'")
