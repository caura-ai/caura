"""Allow ``status='invalidated'`` on procedures (fix ck_procedures_status).

The procedure-level lifecycle marker set by the invalidate path
(``PATCH /procedures/{id}`` with ``{"status": "invalidated"}`` →
``procedure_set_status``; the ranker skips it via
``Procedure.status != 'invalidated'``) was never added to the CHECK
constraint minted in migration 028, which only permitted
``active/candidate/staged/quarantined/stale/deprecated``. Invalidating a
procedure therefore violated ``ck_procedures_status`` and 500'd in
production. This recreates the constraint with ``invalidated`` included.

Reversible: downgrade restores the original allowed set. Downgrade will
fail if any row already carries ``status='invalidated'`` (correct — the
constraint can't be re-narrowed while a violating row exists).

Revision ID: 030
Revises: 029
Create Date: 2026-06-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "status IN ('active', 'candidate', 'staged', 'quarantined', 'stale', 'deprecated')"
_NEW = "status IN ('active', 'candidate', 'staged', 'quarantined', 'stale', 'deprecated', 'invalidated')"


def upgrade() -> None:
    op.drop_constraint("ck_procedures_status", "procedures", type_="check")
    op.create_check_constraint("ck_procedures_status", "procedures", _NEW)


def downgrade() -> None:
    op.drop_constraint("ck_procedures_status", "procedures", type_="check")
    op.create_check_constraint("ck_procedures_status", "procedures", _OLD)
