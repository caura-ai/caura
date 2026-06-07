"""Backfill ``source`` and ``status`` on existing skills docs (Skill Factory SF-001).

The Skill Factory canonical schema (plan §3) extends the ``skills``
collection with two lifecycle fields every doc is expected to carry:

  - ``source`` — the producer that minted the doc. Canonical values:
                 ``forge`` (Forge resident), ``agent`` (synchronous
                 agent write via ``memclaw_doc``), ``manual`` (a human
                 authored it), or ``imported`` (bulk import / external
                 producer).
  - ``status`` — lifecycle state. Pre-existing docs are already in
                 production use, so they land in ``active`` directly.

The eToro production survey + the 2026-06-06 dry-run revealed three
real-world pre-migration shapes the backfill has to handle:

  Shape A — no ``source``, has ``version`` (handcrafted skills)
            1,403 → 1 row at eToro: ``etoro-brand-guidelines-v2`` +
            the two OSS guides.
  Shape B — no ``source``, no ``version`` (bulk-imported pointer-only)
            ~1,405 rows at eToro: the catalog imports.
  Shape C — has ``source`` already, but the value is a legacy non-
            canonical string (e.g. ``cursor-marketplace``,
            ``claw-fleet-skills/cursor-marketplace``) and / or
            ``status`` is missing.
            73 rows at eToro: the dry-run found these orphans, which
            Branches 1+2 alone skip entirely.

This migration backfills in FOUR branches. Each branch is
independently idempotent — guard predicates ensure re-running over
already-migrated data is a no-op.

  1. **Manual heuristic** — docs with no ``source`` AND a ``version``
     field get ``source='manual'`` + ``status='active'``.
  2. **Imported default** — docs with no ``source`` get
     ``source='imported'`` + ``status='active'``.
  3. **Status backfill** — docs that already had a ``source`` value
     (Shape C) but lack ``status`` get ``status='active'`` ONLY. The
     legacy source value is left intact at this step; Branch 4
     normalizes it next.
  4. **Legacy source normalization** — docs whose ``source`` is NOT
     in the canonical set get rewritten to ``source='imported'``
     with the original value preserved as ``legacy_source``. The
     Skill Factory's SF-002 validator (``skill_lifecycle.ALLOWED_SOURCES``)
     enforces the canonical set on every NEW write; this branch
     brings existing data into the same vocabulary without data
     loss.

The branch order matters only for readability — every branch is
guarded so it does NOT depend on prior branches having run. Re-running
the entire migration is a strict no-op once the data is consistent.

The downgrade is safe to run against any deployment where this
migration was applied: Branches 1+2 stamp ``_migrated_by="022"`` into
every row they touch, and the downgrade ONLY strips ``source`` /
``status`` / ``_migrated_by`` from rows carrying that sentinel. Rows
with a pre-existing ``source='manual'`` or ``source='imported'`` that
we did NOT write (e.g. data manually set by an operator, OR rows
backfilled by a later Skill Factory migration) are left intact.

Branch 4's downgrade restores the original (legacy) ``source`` from
``legacy_source`` regardless of the sentinel — the legacy_source key
itself is the discriminator for "this row was normalized by Branch
4". Branch 3 is intentionally NOT reversed: those rows had a
``source`` value before the migration ran and the post-migration
``status='active'`` reflects production reality. Without a per-row
sentinel for Branch 3 we can't distinguish "status added by Branch
3" from "status set by a later write".

Operator caveat: if Forge has already produced ``source='forge'``
candidates against a downgraded baseline, do NOT run the downgrade —
those candidates are unaffected by the strip filter (sentinel-keyed),
but downstream consumers (plugin reconciliation, lifecycle, harness
install) will see a mix of post-migration and pre-migration shapes.
Take a backup first.

Revision ID: 022
Revises: 021
Create Date: 2026-05-10
Updated: 2026-06-06 — added Branches 3+4 after eToro dry-run found
73 orphaned rows; downgrade extended for ``legacy_source`` restore.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Canonical source enum — kept in lockstep with
# ``core_api.services.skill_lifecycle.ALLOWED_SOURCES``. A divergence
# between the two would let Branch 4 normalize a value the validator
# still considers acceptable (or vice-versa). The string literals
# below are inlined into the SQL for Postgres' ``= ANY`` operator.
_CANONICAL_SOURCES_SQL = "'forge', 'agent', 'manual', 'imported'"


# Why the ``::jsonb`` / ``::json`` round-trip on every UPDATE:
#
# Older eToro deployments declared ``documents.data`` as plain ``json``
# (we surfaced this during the migration 022 dry-run on 2026-06-06).
# The OSS schema in ``common/models/document.py`` uses ``jsonb``, but
# the migration must run cleanly on BOTH. Postgres' ``||`` /
# ``jsonb_build_object`` / ``?`` / ``- 'key'`` operators only exist on
# ``jsonb``. The pattern is:
#
#     SET data = ((data::jsonb) || jsonb_build_object(...))::json
#
# On a ``jsonb`` column the casts are a no-op (the planner removes
# them); on a ``json`` column they translate to/from the correct
# representation at row-update time. ``json_build_object`` exists too
# but lacks the merge operator, so we keep using ``jsonb_build_object``
# and round-trip via the casts.
#
# Predicates (``data ->> ...``, ``data ? ...``) also need the cast on
# the ``json`` side — they're jsonb-only operators. We cast the column
# expression in each WHERE clause for safety.


def upgrade() -> None:
    # Branch 1: manual-shaped docs (no source yet, carry ``version``)
    # → source='manual', status='active'. Stamps ``_migrated_by='022'``
    # so the downgrade can distinguish OUR writes from operator-set
    # ``source='manual'`` values on pre-existing rows.
    op.execute(
        """
        UPDATE documents
        SET data = ((data::jsonb) || jsonb_build_object(
                       'source',       'manual',
                       'status',       'active',
                       '_migrated_by', '022'
                   ))::json
        WHERE collection = 'skills'
          AND ((data::jsonb) ->> 'source') IS NULL
          AND (data::jsonb) ? 'version'
        """
    )
    # Branch 2: everything else with no source → source='imported',
    # status='active'. Same sentinel as Branch 1 for downgrade safety.
    op.execute(
        """
        UPDATE documents
        SET data = ((data::jsonb) || jsonb_build_object(
                       'source',       'imported',
                       'status',       'active',
                       '_migrated_by', '022'
                   ))::json
        WHERE collection = 'skills'
          AND ((data::jsonb) ->> 'source') IS NULL
        """
    )
    # Branch 3: status backfill on docs that already had a source value
    # but lack status. Plugs the 73-row gap surfaced by the eToro
    # dry-run. Idempotent via ``status IS NULL`` guard.
    op.execute(
        """
        UPDATE documents
        SET data = ((data::jsonb) || jsonb_build_object('status', 'active'))::json
        WHERE collection = 'skills'
          AND ((data::jsonb) ->> 'source') IS NOT NULL
          AND ((data::jsonb) ->> 'status') IS NULL
        """
    )
    # Branch 4: normalize legacy non-canonical source values. Stash the
    # original under ``legacy_source`` so audit + future migrations can
    # see where the doc came from. Idempotent via the canonical-set
    # NOT IN guard.
    op.execute(
        f"""
        UPDATE documents
        SET data = ((data::jsonb) || jsonb_build_object(
                       'source',        'imported',
                       'legacy_source', (data::jsonb) ->> 'source'
                   ))::json
        WHERE collection = 'skills'
          AND ((data::jsonb) ->> 'source') IS NOT NULL
          AND ((data::jsonb) ->> 'source') NOT IN ({_CANONICAL_SOURCES_SQL})
        """
    )


def downgrade() -> None:
    # Best-effort cleanup. Operators should not run this against a
    # deployment where Forge has produced candidates — see module
    # docstring.

    # Step 1: restore Branch-4 normalizations. For any row that
    # carries ``legacy_source``, put the original ``source`` back and
    # drop ``legacy_source``.
    op.execute(
        """
        UPDATE documents
        SET data = (((data::jsonb) - 'legacy_source')
                || jsonb_build_object('source', (data::jsonb) ->> 'legacy_source'))::json
        WHERE collection = 'skills'
          AND ((data::jsonb) ? 'legacy_source')
        """
    )
    # Step 2: reverse Branches 1+2 — but ONLY for rows that carry the
    # ``_migrated_by='022'`` sentinel this migration stamped. This is
    # the key safety improvement vs. the prior version, which
    # silently stripped ``source`` from any row whose source happened
    # to be ``'manual'`` or ``'imported'`` — including
    # operator-curated values we never wrote. The sentinel-keyed
    # filter limits the strip to OUR rows only. We also drop the
    # sentinel itself so a re-upgrade after a downgrade lands cleanly.
    op.execute(
        """
        UPDATE documents
        SET data = ((data::jsonb) - 'source' - 'status' - '_migrated_by')::json
        WHERE collection = 'skills'
          AND ((data::jsonb) ->> '_migrated_by') = '022'
        """
    )
    # NOTE: Branch 3's ``status`` backfill on legacy-source rows is
    # NOT reversed. Those rows had a ``source`` value before the
    # migration ran and the post-migration ``status='active'`` reflects
    # reality — they're in production use. Without a per-row sentinel
    # we can't distinguish Branch-3 writes from any subsequent
    # ``status`` writes; leaving them alone is the safe call.
