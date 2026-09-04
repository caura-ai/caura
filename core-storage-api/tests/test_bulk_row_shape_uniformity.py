"""A bulk batch's rows must agree on which columns they set.

``memory_add_all`` builds ONE multi-values INSERT for the whole batch, and
SQLAlchemy derives a single column list for it. Rows that disagree therefore do
not produce "a row with a missing value" — they produce a statement that either
fails to compile or quietly writes a different set of columns than the caller
asked for, decided by which row happens to be first.

That is not theoretical. ``_filter_memory_fields`` stamps
``embedded_content_hash`` only on rows carrying a vector, and a deferred
deployment embeds only ``write_mode="strong"`` items — so a batch mixing strong
and ordinary items diverges on exactly that column. In production this compiled
0.6s of nothing 680 times an hour for 29 hours, surfacing three services away as
a gateway 504 that named neither the batch nor the column.

These tests compile the real statement rather than asserting on the mapper's
output, because compilation is where the defect lives: a dict-level assertion
would keep passing if SQLAlchemy changed how it treats an absent key.

No database — ``.compile()`` is pure.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert

from common.models.memory import Memory
from core_storage_api.services import postgres_service
from core_storage_api.services.postgres_service import (
    BulkRowShapeError,
    BulkValidationError,
    PostgresService,
)

# No ``integration`` marker deliberately: this suite reserves it for tests that
# need a running PostgreSQL, and nothing here opens a connection.


def _item(idx: int, *, embedded: bool) -> dict:
    """One item shaped exactly as core-api's bulk path builds it.

    The key set is fixed and identical for every item — that is what core-api
    does (``memory_service`` builds ``mem_data`` from a dict literal), and it
    matters: it means any divergence downstream was introduced by the storage
    mapper, not inherited from the request.

    ``embedded`` is the only difference, and it is the real one: under a
    deferred deployment only ``write_mode="strong"`` items come back from the
    embed call with a vector, and the rest arrive with ``embedding=None``.
    """
    return {
        "tenant_id": "t-1",
        "fleet_id": None,
        "agent_id": "a-1",
        "content": f"memory {idx}",
        "content_hash": f"hash-{idx}",
        "client_request_id": f"attempt-1:{idx}",
        # A real vector for the strong item, None for the deferred one.
        "embedding": [0.1, 0.2, 0.3] if embedded else None,
    }


def _compile(rows: list[dict]) -> str:
    """Compile the statement ``memory_add_all`` builds, as it builds it."""
    stmt = pg_insert(Memory).values(rows)
    return str(stmt.compile(dialect=postgresql.dialect()))


def _mapped(*items: dict) -> list[dict]:
    return [PostgresService._filter_memory_fields(d) for d in items]


def test_mixed_batch_compiles_when_the_stamped_row_is_first():
    """The production failure: row 0 stamped, a later row not.

    Without the fix this raises ``CompileError: INSERT value for column
    memories.embedded_content_hash is explicitly rendered as a boundparameter
    in the VALUES clause`` — the exact error the storage writer logged
    throughout the incident, and the whole batch is lost.
    """
    rows = _mapped(_item(0, embedded=True), _item(1, embedded=False))

    sql = _compile(rows)

    assert "embedded_content_hash" in sql


def test_mixed_batch_keeps_the_column_when_the_stamped_row_is_second():
    """The silent twin: row 0 unstamped, a later row stamped.

    This ordering never raised. It compiled cleanly, emitted no warning, and
    dropped ``embedded_content_hash`` from the INSERT altogether — writing NULL
    provenance for a row that HAD a vector and a known hash.

    Asserting the column is present is the point. A test that only checked
    "does not raise" passes on the broken code, which is precisely how this
    half went unnoticed while the other half paged.
    """
    rows = _mapped(_item(0, embedded=False), _item(1, embedded=True))

    sql = _compile(rows)

    assert "embedded_content_hash" in sql, (
        "the column was dropped from the INSERT: the stamped row's provenance "
        "would persist as NULL, which reads downstream as 'written before "
        "migration 037' — a bucket nothing re-embeds"
    )


def test_the_stamped_row_still_carries_its_hash():
    """Uniformity must not be bought by dropping the stamp itself.

    A fix that simply stopped stamping would also make every row agree, and
    would also make both tests above pass, while silently disabling the
    provenance tracking the column exists for.
    """
    stamped, deferred = _mapped(_item(0, embedded=True), _item(1, embedded=False))

    assert stamped["embedded_content_hash"] == "hash-0"
    # Present as an explicit NULL, not absent: identical on the wire for a
    # nullable, default-less column, and it is what makes the batch uniform.
    assert deferred["embedded_content_hash"] is None


def test_a_homogeneous_batch_is_unaffected():
    """Control. Both rows embedded — the case that always worked."""
    rows = _mapped(_item(0, embedded=True), _item(1, embedded=True))

    sql = _compile(rows)

    assert "embedded_content_hash" in sql
    assert [r["embedded_content_hash"] for r in rows] == ["hash-0", "hash-1"]


async def test_items_that_disagree_are_the_callers_to_fix():
    """Divergent ITEMS are a 422, not the permanent 500.

    Reachable through the real mapper, with no patching: the route hands
    ``request.json()`` straight to ``memory_add_all``, and
    ``_filter_memory_fields`` passes each item's own subset of valid columns
    through. Two items differing on ``title`` therefore produce two row shapes
    — so this guard is load-bearing, not defence-in-depth.

    It must NOT be ``BulkRowShapeError``: that one tells the caller a retry can
    never succeed, and this batch succeeds the moment the caller sends the same
    fields for every item. Getting that backwards is the same class of mistake
    as the incident itself — advice that does not match the failure.
    """
    svc = PostgresService()
    items = [
        {"tenant_id": "t-1", "client_request_id": "a:0", "content": "one", "title": "has one"},
        {"tenant_id": "t-1", "client_request_id": "a:1", "content": "two"},
    ]

    with pytest.raises(BulkValidationError) as exc:
        await svc.memory_add_all(items)

    # Names the offending field, and ONLY it. The production path's whole
    # failure was that nothing anywhere said which column diverged, so a
    # message listing every field in the row is barely better than silence.
    message = str(exc.value)
    assert "title" in message
    assert "content" not in message, f"shared fields must not be reported as divergent: {message}"


async def test_items_differing_only_on_a_dropped_key_are_not_rejected(monkeypatch):
    """An unrecognised key is not a shape difference.

    ``_filter_memory_fields`` drops anything outside ``_MEMORY_VALID_FIELDS``,
    so two items differing only on such a key map to identical rows and write
    correctly. Rejecting them would be a 422 for a batch that was always fine —
    which is why the item check intersects with the valid-field set instead of
    comparing raw keys.
    """

    class _ReachedTheSession(Exception):
        pass

    def _sentinel():
        raise _ReachedTheSession

    monkeypatch.setattr(postgres_service, "get_session", _sentinel)
    svc = PostgresService()
    items = [
        {"tenant_id": "t-1", "client_request_id": "a:0", "content": "one", "not_a_column": 1},
        {"tenant_id": "t-1", "client_request_id": "a:1", "content": "two"},
    ]

    with pytest.raises(_ReachedTheSession):
        await svc.memory_add_all(items)


async def test_a_mapper_divergence_is_the_permanent_one(monkeypatch):
    """A divergence the ITEMS do not explain is the server's fault.

    The mapper is stubbed to invent a key for one row while the items stay
    uniform — which is exactly what the pre-fix ``_filter_memory_fields`` did
    for ``embedded_content_hash``. That has to answer permanently rather than
    422: nothing the caller sent explains it, so re-sending cannot help.

    Reaching ``BulkRowShapeError`` also proves the check runs before
    ``get_session()`` — there is no database in this test — and the divergent
    column travels in ``fields`` as data, not as prose.
    """

    def _invents_a_key(d: dict) -> dict:
        out = dict(d)
        if d["client_request_id"].endswith(":0"):
            out["embedded_content_hash"] = "h"
        return out

    monkeypatch.setattr(PostgresService, "_filter_memory_fields", staticmethod(_invents_a_key))
    svc = PostgresService()
    items = [
        {"tenant_id": "t-1", "client_request_id": "a:0", "content": "one"},
        {"tenant_id": "t-1", "client_request_id": "a:1", "content": "two"},
    ]

    with pytest.raises(BulkRowShapeError) as exc:
        await svc.memory_add_all(items)

    assert exc.value.fields == {"columns": ["embedded_content_hash"]}


async def test_neither_guard_fires_on_a_uniform_batch(monkeypatch):
    """Control for both guards: identical field sets must reach the session.

    ``get_session`` is replaced with a sentinel-raising stub, so "the guards let
    this through" is a positive assertion rather than the absence of one.
    Asserting on whatever database error an unconfigured environment happens to
    raise would pass for the wrong reason — including if a guard fired first.
    """

    class _ReachedTheSession(Exception):
        pass

    def _sentinel():
        raise _ReachedTheSession

    monkeypatch.setattr(postgres_service, "get_session", _sentinel)
    svc = PostgresService()
    items = [
        {"tenant_id": "t-1", "client_request_id": "a:0", "content": "one"},
        {"tenant_id": "t-1", "client_request_id": "a:1", "content": "two"},
    ]

    with pytest.raises(_ReachedTheSession):
        await svc.memory_add_all(items)
