"""Keep installed skills installed now that nodes install ``active`` skills only.

``POST /skills/installable`` serves ``status='active'`` skills to every tenant.
It used to serve every skills row to a tenant that had not turned the Skill
Factory on (``skills_factory.enabled``, off by default), and such a tenant's
writes skipped the lifecycle validator, so rows written there after migration
022 can carry no ``status``. Nodes have those on disk; without this backfill
the next heartbeat would remove them.

So: a status-less skill becomes ``active``, in tenants that never turned the
Skill Factory on, stamped ``_migrated_by='070'``. An opted-in tenant's catalog
was already active-only, so a status-less row there was never installed and is
left as it is. A row that carries a status keeps it: one an agent wrote as
``staged`` was installed under the old rule and now waits for approval in the
inbox like any other draft.

The downgrade removes ``status`` and the stamp from the rows this one stamped
that are still ``active``. A row whose status changed since (an admin rejected
or quarantined it in the inbox) keeps that status and loses only the stamp, so
a downgrade never turns a reviewed skill status-less again.

Revision ID: 070
Revises: 069
Create Date: 2026-10-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "070"
down_revision: str | None = "069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ``data`` is ``jsonb`` in the OSS schema and plain ``json`` on some older
# deployments; the casts are the round-trip migration 022 explains.
UPGRADE_STATEMENTS: tuple[str, ...] = (
    """
    UPDATE documents
    SET data = ((data::jsonb) || jsonb_build_object('status', 'active', '_migrated_by', '070'))::json
    WHERE collection = 'skills'
      AND ((data::jsonb) ->> 'status') IS NULL
      AND tenant_id NOT IN (
          SELECT org_id FROM organization_settings
          WHERE (settings -> 'skills_factory' ->> 'enabled') = 'true'
      )
    """,
)

DOWNGRADE_STATEMENTS: tuple[str, ...] = (
    """
    UPDATE documents
    SET data = (((data::jsonb) - 'status') - '_migrated_by')::json
    WHERE collection = 'skills'
      AND ((data::jsonb) ->> '_migrated_by') = '070'
      AND ((data::jsonb) ->> 'status') = 'active'
    """,
    """
    UPDATE documents
    SET data = ((data::jsonb) - '_migrated_by')::json
    WHERE collection = 'skills'
      AND ((data::jsonb) ->> '_migrated_by') = '070'
    """,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
