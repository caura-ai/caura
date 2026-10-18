"""One entity per ``(tenant, type, lower(name), fleet)``, where the index is missing.

O24. Migration 001 creates ``uq_entities_tenant_type_name_fleet``, but
production's ``entities`` table predates the chain and was stamped onto it, so
it never had the index (read 2026-10-18: its only unique index is the primary
key). Three things follow there, and on any database stamped the same way:

* ``entity_add``'s ``INSERT ... ON CONFLICT`` names the index's key, so
  Postgres cannot infer an arbiter and refuses every call. ``upsert_entity``
  creates through it, so ``POST /entities`` fails for a new name, and the write
  pipeline's ``CreatePendingSubject`` catches the failure and stores the memory
  without its subject, predicate and object.
* The bulk upsert's ``IntegrityError`` race recovery never fires, so two
  writers create one name twice. Production held 11796 such groups, 23881
  entities (12085 surplus, at most 8 in a group), carrying 41026 links, 22626
  relation ends and 4026 memory subjects.
* ``entity_find_exact`` ends in ``scalar_one_or_none()``, so an exact lookup of
  a duplicated name raises ``MultipleResultsFound``, and the bulk upsert calls
  it for every create: a memory naming one fails entity extraction.

MERGE FIRST, in the normal transaction, then build, as 040 does. Migrations run
at startup and staging redeploys on every push to main, so a build that met a
duplicate would fail the deploy. The merge is the nightly merge's
(``_entity_merge_dupe_into_canonical``), set-based:

* The oldest member of a group survives, ``(created_at, id)``: rows from before
  migration 060 share ``created_at``, so the id decides often.
* Links move to the survivor. Where the memory already links it, or links two
  members, one link stays: the survivor's, else the lowest member id's, as the
  nightly merge keeps the first one it moves. It keeps that link's role, and is
  the caller's if any of them was, so a caller's tag still outlives an edit
  (migration 048).
* Relations move at both ends. One that would join a member to its own group
  goes. Where two land on one natural key, the relation that already had it
  stays (else the lowest id) with the higher weight and every asserting memory.
* Memory subjects move to the survivor.
* The survivor's attributes keep its own values, then each duplicate's other
  keys in id order, and ``_aliases`` holds every member's names and aliases,
  sorted, as the nightly merge leaves them.
* The duplicates are deleted.

Links and relations are updated in place rather than deleted and rewritten, so a
column a stamped table carries beyond the model's survives. Every member of a
group is locked ``FOR UPDATE`` before anything moves, so a concurrent write that
references one waits out the merge (seconds, after the scan) instead of racing
it; one that referenced a duplicate then fails its foreign key rather than
vanishing with it.

Where the index already stands valid, as on every database the chain built,
there is nothing to merge and nothing to build, so the migration returns before
reading ``entities``. An invalid one, left by an interrupted build, goes before
the merge: a unique index that is ready but invalid still checks every new row
version, so updating a survivor beside its duplicates would fail. A writer can
still create a duplicate between the merge's commit and the build, which then
fails; the next deploy drops the invalid index, merges again and rebuilds.

Downgrade does nothing. On a chain-built database the index is 001's, and the
merged rows cannot be told apart again.

Revision ID: 068
Revises: 067
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "068"
down_revision: str | None = "067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_entities_tenant_type_name_fleet"

# The key, in one place: the merge's groups and the index's columns must not
# drift apart. It is 001's, and the one ``entity_add``'s ``ON CONFLICT`` names.
_KEY = "tenant_id, entity_type, lower(canonical_name), COALESCE(fleet_id, '')"

# Public, as the merge below is, so the tests run the exact statement.
VALID_INDEX_SQL = """
    SELECT 1 FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = :name
      AND n.nspname = current_schema()
      AND i.indisvalid
"""

# ``entities.attributes`` read defensively: SQL NULL, JSON null and any non-object
# count as no attributes, as ``_merge_entity_attributes`` reads them. The cast
# serves a ``json`` column (001's) and a ``jsonb`` one alike.
_ATTRIBUTES = (
    "CASE WHEN jsonb_typeof(e.attributes::jsonb) = 'object' THEN e.attributes::jsonb ELSE '{}'::jsonb END"
)

# In order, in one transaction: the temporary tables drop at its commit.
MERGE_SQL: tuple[str, ...] = (
    # Each duplicate and the member it merges into.
    """
    CREATE TEMPORARY TABLE entity_merge_map (
        dupe_id uuid PRIMARY KEY,
        survivor_id uuid NOT NULL
    ) ON COMMIT DROP
    """,
    f"""
    INSERT INTO entity_merge_map (dupe_id, survivor_id)
    SELECT id, survivor_id
    FROM (
        SELECT id,
               first_value(id) OVER (PARTITION BY {_KEY} ORDER BY created_at, id) AS survivor_id
        FROM entities
    ) ranked
    WHERE id <> survivor_id
    """,
    "ANALYZE entity_merge_map",
    """
    SELECT e.id FROM entities e
    WHERE e.id IN (SELECT dupe_id FROM entity_merge_map UNION SELECT survivor_id FROM entity_merge_map)
    ORDER BY e.id
    FOR UPDATE
    """,
    # Links: which one stays where a memory would link the survivor twice.
    """
    CREATE TEMPORARY TABLE entity_merge_links ON COMMIT DROP AS
    SELECT l.memory_id,
           l.entity_id,
           m.survivor_id,
           row_number() OVER (PARTITION BY l.memory_id, m.survivor_id ORDER BY l.entity_id) AS rn,
           bool_or(l.source = 'caller') OVER (PARTITION BY l.memory_id, m.survivor_id) AS any_caller,
           EXISTS (
               SELECT 1 FROM memory_entity_links s
               WHERE s.memory_id = l.memory_id AND s.entity_id = m.survivor_id
           ) AS survivor_linked
    FROM memory_entity_links l
    JOIN entity_merge_map m ON m.dupe_id = l.entity_id
    """,
    """
    UPDATE memory_entity_links k
    SET source = 'caller'
    FROM entity_merge_links x
    WHERE x.rn = 1
      AND x.any_caller
      AND k.memory_id = x.memory_id
      AND k.entity_id = CASE WHEN x.survivor_linked THEN x.survivor_id ELSE x.entity_id END
      AND k.source <> 'caller'
    """,
    """
    DELETE FROM memory_entity_links l
    USING entity_merge_links x
    WHERE l.memory_id = x.memory_id
      AND l.entity_id = x.entity_id
      AND (x.survivor_linked OR x.rn > 1)
    """,
    """
    UPDATE memory_entity_links l
    SET entity_id = x.survivor_id
    FROM entity_merge_links x
    WHERE l.memory_id = x.memory_id
      AND l.entity_id = x.entity_id
      AND x.rn = 1
      AND NOT x.survivor_linked
    """,
    # Relations: every one that moves, with its key after the move, and every
    # relation that already holds one of those keys.
    """
    CREATE TEMPORARY TABLE entity_merge_relations (
        id uuid PRIMARY KEY,
        tenant_id text NOT NULL,
        relation_type text NOT NULL,
        new_from uuid NOT NULL,
        new_to uuid NOT NULL,
        moved boolean NOT NULL
    ) ON COMMIT DROP
    """,
    """
    INSERT INTO entity_merge_relations
    SELECT r.id,
           r.tenant_id,
           r.relation_type,
           COALESCE(mf.survivor_id, r.from_entity_id),
           COALESCE(mt.survivor_id, r.to_entity_id),
           true
    FROM relations r
    LEFT JOIN entity_merge_map mf ON mf.dupe_id = r.from_entity_id
    LEFT JOIN entity_merge_map mt ON mt.dupe_id = r.to_entity_id
    WHERE r.id IN (
        SELECT r2.id FROM relations r2 JOIN entity_merge_map m ON m.dupe_id = r2.from_entity_id
        UNION
        SELECT r2.id FROM relations r2 JOIN entity_merge_map m ON m.dupe_id = r2.to_entity_id
    )
    """,
    """
    INSERT INTO entity_merge_relations
    SELECT r.id, r.tenant_id, r.relation_type, r.from_entity_id, r.to_entity_id, false
    FROM (SELECT DISTINCT tenant_id, relation_type, new_from, new_to FROM entity_merge_relations) k
    JOIN relations r
      ON r.tenant_id = k.tenant_id
     AND r.from_entity_id = k.new_from
     AND r.relation_type = k.relation_type
     AND r.to_entity_id = k.new_to
    ON CONFLICT (id) DO NOTHING
    """,
    "ANALYZE entity_merge_relations",
    # A relation between two members of one group would join the survivor to itself.
    """
    DELETE FROM relations r
    USING entity_merge_relations x
    WHERE r.id = x.id AND x.moved AND x.new_from = x.new_to
    """,
    "DELETE FROM entity_merge_relations WHERE moved AND new_from = new_to",
    """
    CREATE TEMPORARY TABLE entity_merge_relation_keep ON COMMIT DROP AS
    SELECT id,
           first_value(id) OVER (
               PARTITION BY tenant_id, new_from, relation_type, new_to ORDER BY moved, id
           ) AS keeper_id
    FROM entity_merge_relations
    """,
    """
    UPDATE relations k
    SET weight = g.weight
    FROM (
        SELECT x.keeper_id, max(r.weight) AS weight
        FROM entity_merge_relation_keep x
        JOIN relations r ON r.id = x.id
        GROUP BY x.keeper_id
        HAVING count(*) > 1
    ) g
    WHERE k.id = g.keeper_id
      AND g.weight IS NOT NULL
      AND (k.weight IS NULL OR k.weight < g.weight)
    """,
    """
    INSERT INTO relation_evidence (relation_id, memory_id)
    SELECT x.keeper_id, ev.memory_id
    FROM entity_merge_relation_keep x
    JOIN relation_evidence ev ON ev.relation_id = x.id
    WHERE x.id <> x.keeper_id
    ON CONFLICT DO NOTHING
    """,
    """
    DELETE FROM relations r
    USING entity_merge_relation_keep x
    WHERE r.id = x.id AND x.id <> x.keeper_id
    """,
    """
    UPDATE relations r
    SET from_entity_id = x.new_from, to_entity_id = x.new_to
    FROM entity_merge_relations x
    JOIN entity_merge_relation_keep k ON k.id = x.id
    WHERE r.id = x.id AND x.moved AND k.id = k.keeper_id
    """,
    """
    UPDATE memories mem
    SET subject_entity_id = m.survivor_id
    FROM entity_merge_map m
    WHERE mem.subject_entity_id = m.dupe_id
    """,
    # Attributes: the survivor's keys, then each duplicate's others in id order;
    # every member's names and aliases. Members rank survivor first, then by id.
    f"""
    WITH members AS (
        SELECT survivor_id, survivor_id AS member_id, 0 AS rank
        FROM (SELECT DISTINCT survivor_id FROM entity_merge_map) s
        UNION ALL
        SELECT survivor_id, dupe_id, 1 FROM entity_merge_map
    ),
    member_attributes AS (
        SELECT mb.survivor_id, mb.member_id, mb.rank, e.canonical_name,
               {_ATTRIBUTES} AS attributes
        FROM members mb
        JOIN entities e ON e.id = mb.member_id
    ),
    merged_keys AS (
        SELECT survivor_id, jsonb_object_agg(key, value) AS attributes
        FROM (
            SELECT DISTINCT ON (a.survivor_id, kv.key) a.survivor_id, kv.key, kv.value
            FROM member_attributes a
            CROSS JOIN LATERAL jsonb_each(a.attributes) kv
            WHERE kv.key <> '_aliases'
            ORDER BY a.survivor_id, kv.key, a.rank, a.member_id
        ) firsts
        GROUP BY survivor_id
    ),
    merged_aliases AS (
        SELECT survivor_id, jsonb_agg(alias ORDER BY alias COLLATE "C") AS aliases
        FROM (
            SELECT DISTINCT a.survivor_id, n.alias
            FROM member_attributes a
            CROSS JOIN LATERAL (
                SELECT a.canonical_name AS alias
                UNION
                SELECT elem #>> '{{}}'
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(a.attributes -> '_aliases') = 'array'
                         THEN a.attributes -> '_aliases' ELSE '[]'::jsonb END
                ) elem
                WHERE jsonb_typeof(elem) = 'string'
            ) n
        ) names
        GROUP BY survivor_id
    )
    UPDATE entities e
    SET attributes = COALESCE(k.attributes, '{{}}'::jsonb) || jsonb_build_object('_aliases', a.aliases)
    FROM merged_aliases a
    LEFT JOIN merged_keys k ON k.survivor_id = a.survivor_id
    WHERE e.id = a.survivor_id
    """,
    """
    DELETE FROM entities e
    USING entity_merge_map m
    WHERE e.id = m.dupe_id
    """,
)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text(VALID_INDEX_SQL), {"name": _INDEX_NAME}).fetchone():
        return
    with op.get_context().autocommit_block():
        drop_invalid_indexes(_INDEX_NAME)
    for statement in MERGE_SQL:
        op.execute(statement)

    # Commits the merge before the build: CONCURRENTLY cannot run in a
    # transaction, and the build must see the merged table.
    with op.get_context().autocommit_block():
        # The ``CREATE ... INDEX ... ON <table>`` prefix stays on one line with
        # the name spelled out, for ``test_no_plain_create_index_on_large_tables``.
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_entities_tenant_type_name_fleet ON entities"
            f" ({_KEY})"
        )


def downgrade() -> None:
    # On a chain-built database the index is 001's; dropping it here would leave
    # that database short of 067's schema. The merge cannot be undone.
    pass
