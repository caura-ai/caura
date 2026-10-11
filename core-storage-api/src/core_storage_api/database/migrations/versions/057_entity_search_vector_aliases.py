"""Index entity aliases in ``entities.search_vector`` (L-129).

Revision ID: 057
Revises: 056
Create Date: 2026-10-04

First-seen-wins keeps an entity's first canonical name, and ``entity_service``
justifies dropping a later surface form with "they remain searchable /
discoverable" via ``attributes._aliases``. The FTS half never delivered that:
migration 001's trigger builds the vector from ``canonical_name`` alone and fires
only on ``UPDATE OF canonical_name``, so ``entity_fts_search`` (the entity boost
and the entity-lookup path) could not match a merged surface form.

The vector is now the canonical name and every alias, tokenised together at one
weight, the shape 034 gave memory titles. ``entity_fts_search`` only tests
``@@``, so a name repeated among the aliases changes no result.

``_aliases`` is read defensively. ``attributes`` is ``json`` (not ``jsonb``) and
nothing types its keys, so a value that is not an array contributes nothing,
rather than making ``json_array_elements_text`` raise and fail the write.

The trigger now also fires on ``UPDATE OF attributes``: aliases arrive by update
(an upsert merging into an existing row), not only at insert.

BACKFILL IS OUT OF BAND, for the reason 034 records: a backfill inside the
migration blocked the staging deploy on 2026-08-08. Rows written before this keep
their name-only vector, and so still match their names, until
``python -m core_storage_api.scripts.backfill_057_entity_search_vector`` rebuilds
them, or until their name or attributes are next written.

The function's comment is the head fingerprint ``init.py`` probes: this
migration creates no table or index of its own.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "057"
down_revision: str | None = "056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION_COMMENT = "Entity FTS vector: canonical name and _aliases (migration 057, L-129)."


def _vector(prefix: str = "") -> str:
    """The canonical name and every alias, one weight throughout.

    One definition for the trigger (``NEW.``) and the backfill script (``e.``,
    pinned equal by its test). ``coalesce`` on both parts: ``NULL || text`` is
    NULL and would empty the whole vector, and ``string_agg`` over no aliases is
    NULL.
    """
    aliases = f"{prefix}attributes->'_aliases'"
    return (
        f"to_tsvector('english', coalesce({prefix}canonical_name, '') || ' ' || coalesce(("
        f"SELECT string_agg(alias, ' ') FROM json_array_elements_text("
        f"CASE WHEN json_typeof({aliases}) = 'array' THEN {aliases} ELSE CAST('[]' AS json) END"
        f") AS a(alias)), ''))"
    )


def _name_only(prefix: str = "") -> str:
    """001's expression, for the downgrade."""
    return f"to_tsvector('english', coalesce({prefix}canonical_name, ''))"


def _set_trigger(vector: str, update_of: str) -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION entities_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector := {vector};
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    # Recreated rather than altered, as in 034: the fire condition is part of the
    # CREATE, and ``CREATE OR REPLACE TRIGGER`` needs PG 14+ (this schema's floor
    # is 13).
    op.execute("DROP TRIGGER IF EXISTS entities_search_vector_trigger ON entities")
    op.execute(f"""
        CREATE TRIGGER entities_search_vector_trigger
        BEFORE INSERT OR UPDATE OF {update_of} ON entities
        FOR EACH ROW EXECUTE FUNCTION entities_search_vector_update();
    """)


def upgrade() -> None:
    _set_trigger(_vector("NEW."), "canonical_name, attributes")
    op.execute(f"COMMENT ON FUNCTION entities_search_vector_update() IS '{FUNCTION_COMMENT}'")


def downgrade() -> None:
    _set_trigger(_name_only("NEW."), "canonical_name")
    op.execute("COMMENT ON FUNCTION entities_search_vector_update() IS NULL")
