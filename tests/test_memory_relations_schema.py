"""The ``memory_relations`` guarantees, exercised against a real database.

Every assertion here corresponds to a design decision stated in
``common/models/memory_relation.py``. They are DB-level guarantees rather than service
conventions precisely so a direct insert cannot bypass them, and a comment claiming a
constraint exists is worth nothing until something has watched the constraint refuse a
write.

Covered:
  * ``supersedes`` cannot be stored here (it lives on ``memories.supersedes_id``);
  * a memory cannot link to itself;
  * the same (tenant, from, type, to) cannot be stored twice — what makes a repeated
    ``memory_link`` idempotent rather than duplicating the edge;
  * the reversed pair of a SYMMETRIC type IS accepted by the constraint, which is why
    the service must check it — the schema alone does not make symmetry idempotent;
  * hard-deleting an endpoint cascades the link away;
  * SOFT-deleting an endpoint leaves the link in place, since soft delete is reversible.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from common.models.memory_relation import (
    ALL_RELATION_TYPES,
    DIRECTED_RELATION_TYPES,
    STORED_RELATION_TYPES,
    SUPERSEDES_RELATION_TYPE,
    SYMMETRIC_RELATION_TYPES,
)

pytestmark = pytest.mark.integration


async def _memory(db, tenant_id: str, content: str) -> uuid.UUID:
    mid = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO memories (id, tenant_id, agent_id, memory_type, content) "
            "VALUES (:id, :t, 'rel-agent', 'fact', :c)"
        ),
        {"id": mid, "t": tenant_id, "c": content},
    )
    return mid


async def _link(db, tenant_id: str, a: uuid.UUID, rel: str, b: uuid.UUID) -> None:
    await db.execute(
        text(
            "INSERT INTO memory_relations (tenant_id, from_memory_id, relation_type, to_memory_id) "
            "VALUES (:t, :a, :r, :b)"
        ),
        {"t": tenant_id, "a": a, "r": rel, "b": b},
    )


async def _count(db, a: uuid.UUID) -> int:
    return (
        await db.execute(
            text(
                "SELECT count(*) FROM memory_relations "
                "WHERE from_memory_id = :a OR to_memory_id = :a"
            ),
            {"a": a},
        )
    ).scalar_one()


def test_the_relation_vocabulary_matches_the_contract() -> None:
    """No DB. ``mcp-tools.md`` §8 names six types; five are stored, one is routed."""
    assert ALL_RELATION_TYPES == {
        "supersedes",
        "elaborates",
        "contradicts",
        "depends_on",
        "alternative_to",
        "related_to",
    }
    assert SUPERSEDES_RELATION_TYPE not in STORED_RELATION_TYPES, (
        "supersedes must NOT be in the stored set — it reuses memories.supersedes_id, "
        "and the CHECK constraint refuses it in this table"
    )
    assert SYMMETRIC_RELATION_TYPES.isdisjoint(DIRECTED_RELATION_TYPES)
    assert STORED_RELATION_TYPES == SYMMETRIC_RELATION_TYPES | DIRECTED_RELATION_TYPES


async def test_supersedes_cannot_be_stored_in_this_table(db, tenant_id) -> None:
    """The routing decision is structural, not a convention the service may forget."""
    a = await _memory(db, tenant_id, "older claim")
    b = await _memory(db, tenant_id, "newer claim")
    with pytest.raises(
        IntegrityError, match="ck_memory_relations_supersedes_not_stored"
    ):
        await _link(db, tenant_id, b, SUPERSEDES_RELATION_TYPE, a)


async def test_a_memory_cannot_link_to_itself(db, tenant_id) -> None:
    a = await _memory(db, tenant_id, "sole memory")
    with pytest.raises(IntegrityError, match="ck_memory_relations_no_self_link"):
        await _link(db, tenant_id, a, "related_to", a)


async def test_the_same_link_cannot_be_stored_twice(db, tenant_id) -> None:
    """What makes a repeated ``memory_link`` call idempotent at the storage layer."""
    a = await _memory(db, tenant_id, "detail source")
    b = await _memory(db, tenant_id, "detail target")
    await _link(db, tenant_id, a, "elaborates", b)
    with pytest.raises(IntegrityError, match="uq_memory_relations_natural_key"):
        await _link(db, tenant_id, a, "elaborates", b)


async def test_the_reversed_pair_of_a_symmetric_type_is_NOT_blocked_by_the_schema(
    db, tenant_id
) -> None:
    """The schema does not make symmetry idempotent — the service must.

    ``(A, contradicts, B)`` and ``(B, contradicts, A)`` are the same claim, but they are
    different rows under the natural key, so the constraint accepts both. This test
    exists to pin that gap: it is the reason the write path checks the reversed pair for
    a symmetric type, and if someone ever makes the schema enforce it, this test should
    fail and be replaced rather than deleted.
    """
    a = await _memory(db, tenant_id, "claim one")
    b = await _memory(db, tenant_id, "claim two")
    await _link(db, tenant_id, a, "contradicts", b)
    await _link(db, tenant_id, b, "contradicts", a)  # accepted, and semantically a dup
    assert await _count(db, a) == 2


async def test_hard_deleting_an_endpoint_cascades_the_link_away(db, tenant_id) -> None:
    a = await _memory(db, tenant_id, "depends on target")
    b = await _memory(db, tenant_id, "the dependency")
    await _link(db, tenant_id, a, "depends_on", b)
    assert await _count(db, a) == 1
    await db.execute(text("DELETE FROM memories WHERE id = :b"), {"b": b})
    assert await _count(db, a) == 0, (
        "ON DELETE CASCADE is what makes the purge sweep the only cleanup this table "
        "needs; without it a purged memory leaves dangling edges"
    )


async def test_soft_deleting_an_endpoint_LEAVES_the_link(db, tenant_id) -> None:
    """Soft delete is reversible, so it must not destroy edges.

    The link stops being *returned* because the read path joins ``memories`` and filters
    ``deleted_at IS NULL`` — but the row survives, so undeleting the memory restores its
    graph rather than silently losing it.
    """
    a = await _memory(db, tenant_id, "alternative one")
    b = await _memory(db, tenant_id, "alternative two")
    await _link(db, tenant_id, a, "alternative_to", b)
    await db.execute(
        text("UPDATE memories SET deleted_at = now() WHERE id = :b"), {"b": b}
    )
    assert await _count(db, a) == 1, (
        "a soft delete must leave the edge intact; cascading here would make undelete lossy"
    )
