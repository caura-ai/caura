"""``organization_settings_audit.actor_kind`` and ``actor_credential_id``: who made a settings change.

``changed_by`` names a person and is NULL for a change made with an API key,
which names none, so a settings change made with a tenant key was recorded with
no actor at all. These two columns name the credential: its kind as the audit
trail labels it, and its id when core-api knows it (the gateway's credential id
behind the gateway secret, or ``config:<setting>`` for a key core-api matched
itself).

Separate columns rather than an id folded into ``changed_by``: that column
would then mix people and credentials, and a reader could no longer tell which
it holds without parsing it.

Nullable with no default: adding them rewrites nothing, and every existing row
reads NULL, which is what those rows recorded.

Revision ID: 069
Revises: 068
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "069"
down_revision: str | None = "068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organization_settings_audit", sa.Column("actor_kind", sa.Text(), nullable=True))
    op.add_column("organization_settings_audit", sa.Column("actor_credential_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("organization_settings_audit", "actor_credential_id")
    op.drop_column("organization_settings_audit", "actor_kind")
