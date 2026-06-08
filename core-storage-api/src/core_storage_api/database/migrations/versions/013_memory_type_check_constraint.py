"""Add CHECK constraint on ``memories.memory_type`` to enforce the
vocabulary at the database layer.

Why this exists. ``memory_type`` is constrained at the application
layer in two places: the Pydantic regex ``MEMORY_TYPES_PATTERN`` on
the API, and the LLM enrichment prompt's vocabulary list. Both are
bypassable — raw SQL backfills, ad-hoc bulk imports, future services
written against the storage tier directly, all of them can write any
string. This migration makes the DB the final guardrail so an invalid
``memory_type`` becomes an INSERT-time error instead of a row that
silently survives until somebody queries it.

This is a SNAPSHOT. Migrations are immutable historical artefacts —
do NOT import ``MEMORY_TYPES`` from Python constants here, even
though the values agree today. When a new type lands in
``common/enrichment/constants.py`` the contract is to also write a
new migration that drops this constraint and recreates it with the
expanded list. The frozen literal below is what tells future readers
"these were the 14 types accepted as of revision 013".

Lock posture. ``ADD CONSTRAINT ... NOT VALID`` takes a brief ACCESS
EXCLUSIVE lock and is near-instant; new writes are checked from then
on. ``VALIDATE CONSTRAINT`` then scans existing rows under a SHARE
UPDATE EXCLUSIVE lock — reads and normal writes are not blocked.
If a pre-existing row violates the constraint, VALIDATE raises and
the migration aborts; that is the desired loud failure (the operator
must reconcile the bad row before re-running). On a clean DB both
steps complete in a few ms.

Revision ID: 013
Revises: 012
Create Date: 2026-05-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Frozen snapshot of valid ``memory_type`` values as of this revision.
# See the module docstring for why this is a literal and not an import.
_VALID_MEMORY_TYPES_013: tuple[str, ...] = (
    "fact",
    "episode",
    "decision",
    "preference",
    "task",
    "semantic",
    "intention",
    "plan",
    "commitment",
    "action",
    "outcome",
    "cancellation",
    "rule",
    "insight",
)

_CONSTRAINT_NAME = "memories_memory_type_check"


def upgrade() -> None:
    values_sql = ", ".join(f"'{t}'" for t in _VALID_MEMORY_TYPES_013)
    op.execute(
        f"ALTER TABLE memories "
        f"ADD CONSTRAINT {_CONSTRAINT_NAME} "
        f"CHECK (memory_type IN ({values_sql})) NOT VALID"
    )
    op.execute(f"ALTER TABLE memories VALIDATE CONSTRAINT {_CONSTRAINT_NAME}")


def downgrade() -> None:
    op.execute(f"ALTER TABLE memories DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}")
