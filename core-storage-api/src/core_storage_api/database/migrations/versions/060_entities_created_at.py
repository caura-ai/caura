"""``entities.created_at``: when the entity was first seen.

H-05. The nightly duplicate merge (``entity_resolve_duplicates``) keeps one
canonical entity per cluster of near-identical names. The write path keeps the
first name it saw and adds later spellings as aliases, but the nightly pass kept
the longest, so a later 'Acme Corporation' displaced the 'Acme' that memories
were linked under. A qualified or identifier-bearing name still wins first;
between compatible unqualified names the oldest now wins, which needs to know
when each entity was created. ``id`` cannot say: it is ``gen_random_uuid()``.

NOT NULL with ``DEFAULT now()``. ``now()`` is stable, not volatile, so
PostgreSQL 11+ evaluates the default once and keeps it in the catalog: the table
is not rewritten, and every existing row reads the migration's time. Between
such rows the merge falls back to the longer name, the old rule, until they are
dated. Per the owner decision of 2026-10-06 they are dated from their earliest
linked memory by a separate backfill script, not here: that is a pass over
``memory_entity_links`` and ``memories``, too large to run under the boot-time
migration lock.

Revision ID: 060
Revises: 059
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "060"
down_revision: str | None = "059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_column("entities", "created_at")
