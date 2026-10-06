"""Mask provider keys and other secrets in historical settings audit rows (M-99).

``organization_settings_audit.diff`` maps each changed settings path to
``[old, new]``. Since ``diff_settings`` learned to mask secret paths, every new
row records ``****`` for them. Rows written before hold the values that were
submitted, tenant provider API keys in plaintext among them, and the table is
never pruned. This rewrites those rows with the same rule: at an ``api_keys.*``
path, or one whose last segment ends in ``_token``, ``_secret``, ``_api_key`` or
``_password`` (case-insensitive), every value but ``null`` and ``""`` becomes
``****``. A key need not be a string: ``api_keys`` takes any value under it, so
one can sit in a list or an object. ``null`` and ``""`` stay, as the live mask
leaves them, so a row still says that a key was set or cleared, by whom and when.

A secret is never nested inside the value at a non-secret path such as
``api_keys`` itself, because every writer has recorded flat leaf paths.
``diff_settings`` has recursed into nested dicts since the first public release,
and settings validation (``_check_keys``) has always rejected a non-object at
``api_keys``. The only other writer, the heartbeat's ``__deployment__`` row,
stores ``deployment_token`` as a flat string.

The rule is copied rather than imported from ``common.organization_settings_merge``
so this revision keeps doing what it was reviewed to do if that helper changes;
``tests/test_settings_audit_secret_redaction.py`` holds the two to the same
output today. Idempotent: a masked value is left as it is. Irreversible: the
values are gone, which is the point, so ``downgrade`` only removes the marker.

The marker is a comment on the table. A data-only revision leaves no schema
object behind, and ``init_database``'s head fingerprint needs one to tell a
database this revision ran on from one that stopped at 055.

Revision ID: 056
Revises: 055
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "056"
down_revision: str | None = "055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REDACT_SQL = """
WITH redacted AS (
    SELECT a.id,
           jsonb_object_agg(
               d.key,
               CASE
                   WHEN NOT (
                       left(d.key, 9) = 'api_keys.'
                       OR lower(d.key) ~ '(_token|_secret|_api_key|_password)$'
                   ) THEN d.value
                   WHEN jsonb_typeof(d.value) = 'array' THEN COALESCE(
                       (
                           SELECT jsonb_agg(
                               CASE
                                   WHEN e.value IN ('null'::jsonb, '""'::jsonb) THEN e.value
                                   ELSE '"****"'::jsonb
                               END
                               ORDER BY e.ordinality
                           )
                           FROM jsonb_array_elements(d.value) WITH ORDINALITY AS e(value, ordinality)
                       ),
                       '[]'::jsonb
                   )
                   WHEN d.value IN ('null'::jsonb, '""'::jsonb) THEN d.value
                   ELSE '"****"'::jsonb
               END
           ) AS diff
    FROM organization_settings_audit AS a
    CROSS JOIN LATERAL jsonb_each(a.diff) AS d(key, value)
    WHERE jsonb_typeof(a.diff) = 'object'
    GROUP BY a.id
)
UPDATE organization_settings_audit AS a
SET diff = redacted.diff
FROM redacted
WHERE a.id = redacted.id AND a.diff IS DISTINCT FROM redacted.diff
"""


AUDIT_TABLE_COMMENT = "Settings change history. Values at secret paths are masked (migration 056, M-99)."


def upgrade() -> None:
    op.execute(REDACT_SQL)
    op.create_table_comment("organization_settings_audit", AUDIT_TABLE_COMMENT)


def downgrade() -> None:
    """The masked values cannot be recovered; only the marker comment goes."""
    op.drop_table_comment("organization_settings_audit")
