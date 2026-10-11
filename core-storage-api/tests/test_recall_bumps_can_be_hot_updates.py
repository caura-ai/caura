"""M-111 on the migrated schema: a recall bump writes no indexed column.

Every agent-identified search bumps ``recall_count`` and ``last_recalled_at`` on
the rows it returns (``memory_increment_recall``). PostgreSQL makes an UPDATE a
heap-only tuple (HOT) update, which adds no index entries, only when no column
it writes is indexed. ``ix_memories_recall_count`` (migration 001) keyed one of
the two, so each bumped row got a new entry in every index on ``memories``
instead, the HNSW index included, where an insert runs a neighbour search for
the unchanged vector. An index on either column, a ``(tenant_id,
recall_count)`` one included, would bring that back.
"""

from __future__ import annotations

from sqlalchemy import text

from core_storage_api.database.init import get_engine

# The columns ``PostgresService.memory_increment_recall`` writes.
_BUMPED = ("recall_count", "last_recalled_at")

# ``pg_get_indexdef`` spells out an index's key columns, expressions and partial
# predicate, and a column in any of them blocks a HOT update.
_INDEXES_ON_MEMORIES = text(
    "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) "
    "FROM pg_index WHERE indrelid = 'memories'::regclass"
)


async def test_no_index_on_memories_covers_a_column_a_recall_bump_writes(_ensure_schema):
    async with get_engine().connect() as conn:
        indexes = (await conn.execute(_INDEXES_ON_MEMORIES)).all()
    assert indexes, "found no indexes on memories: the query is broken"
    covering = sorted(name for name, definition in indexes if any(col in definition for col in _BUMPED))
    assert covering == [], f"every recall bump is a non-HOT update while these exist: {covering}"
