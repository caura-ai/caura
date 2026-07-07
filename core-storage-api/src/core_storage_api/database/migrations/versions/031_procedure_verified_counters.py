"""Add verified outcome counters to ``procedure_stats`` (Loop Engineering LE-01).

The Loop Engineering working note's central failure mode is the *Nodding
Loop* (§VI.A): an agent that grades its own homework. Today
``memclaw_procedure_record`` moves ``success_count`` / ``failure_count``
purely from the agent's self-reported ``outcome_type`` — and the
``validation_passed`` field the tool already accepts is silently dropped.

This migration adds a parallel counter pair that only moves when an
outcome was *independently verified* (``validation_passed=True`` — set by
the harness's separate evaluator agent, not the generator). The existing
counters are unchanged; these are additive telemetry so a caller can tell
a VERIFIED-reliable procedure from a merely SELF-REPORTED one
(``verified_reliability`` in the record response is computed from these).

Purely additive + reversible: two integer columns, default 0. No backfill
(pre-existing rows correctly start with zero verified outcomes).

Revision ID: 031
Revises: 030
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "procedure_stats",
        sa.Column(
            "verified_success_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "procedure_stats",
        sa.Column(
            "verified_failure_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("procedure_stats", "verified_failure_count")
    op.drop_column("procedure_stats", "verified_success_count")
