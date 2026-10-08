"""``background_task_log.handled_at``: when a row was marked ``rerun`` or ``skipped``.

The entity extraction re-run sweep caps how often one memory is re-run
(``task_list_open_failures``'s ``max_reruns_per_memory``). It counted the
memory's rows marked ``rerun``, but one re-run marks every open row the memory
has (a degraded run and a cancelled one, say), so a single re-run could use up
the whole cap. ``task_mark_handled`` now sets this column to ``now()``, which is
the same for every row one call moves, so the cap counts distinct values: one
per re-run, however many rows it marked. It also tells an operator when a row
was handled; ``completed_at`` is set when the row is written and cannot.

Nullable with no default: adding it rewrites nothing, and every existing row
reads NULL. Only ``task_mark_handled`` sets it, and no row was ever marked
before this column existed.

Revision ID: 064
Revises: 063
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "064"
down_revision: str | None = "063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("background_task_log", sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("background_task_log", "handled_at")
