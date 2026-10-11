"""``uq_relations_natural_key`` has to be on the model, not only in migration 001.

``relation_add`` upserts with ``ON CONFLICT ON CONSTRAINT
uq_relations_natural_key``, which names the constraint. Postgres resolves that
name in ``pg_constraint``, so the statement is only plannable where the
constraint actually exists. Migration 001 creates it; the ``Relation`` model did
not declare it — and ``Base.metadata.create_all`` (what ``tests/conftest.py``
builds this suite's schema with) only ever creates MISSING TABLES, so every
schema built that way had a ``relations`` table with no such constraint and
every ``relation_add`` against it failed outright::

    asyncpg.exceptions.UndefinedObjectError: constraint
    "uq_relations_natural_key" for table "relations" does not exist

Which is why the upsert had no coverage under ``tests/`` at all: it could only
be exercised in ``core-storage-api/tests/``, against the Alembic-owned schema.
``tests/test_storage_bulk_endpoints.py`` says so in as many words, and tests
only the refusal path for that reason.

``Entity`` already carried its equivalent declaration for the same class of
reason — see the comment on ``uq_entities_tenant_type_name_fleet``. Relations
were missed.

Two tests, because they fail for different reasons and neither subsumes the
other:

* the declaration test is static and pins the model against the migration's own
  source, so the two cannot drift apart in either direction; and
* the behavioural test is the coverage the gap was costing — the ON CONFLICT
  path, running under ``tests/`` for the first time.
"""

from __future__ import annotations

import ast
import pathlib
from uuid import uuid4

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy import text as sa_text

from common.models import Relation
from core_storage_api.services.postgres_service import get_session

_REPO = pathlib.Path(__file__).resolve().parents[1]
_MIGRATION_001 = (
    _REPO
    / "core-storage-api/src/core_storage_api/database/migrations/versions/001_initial_schema.py"
)

_CONSTRAINT = "uq_relations_natural_key"


# ---------------------------------------------------------------------------
# The declaration, pinned against the migration that owns it
# ---------------------------------------------------------------------------


def _unique_constraints_in_migration() -> dict[str, tuple[str, list[str]]]:
    """``{name: (table, [columns])}`` for every ``op.create_unique_constraint``."""
    found: dict[str, tuple[str, list[str]]] = {}
    for node in ast.walk(ast.parse(_MIGRATION_001.read_text())):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_unique_constraint"
            and len(node.args) >= 3
        ):
            continue
        name, table, cols = node.args[0], node.args[1], node.args[2]
        if not (
            isinstance(name, ast.Constant)
            and isinstance(table, ast.Constant)
            and isinstance(cols, ast.List)
        ):
            continue
        found[name.value] = (
            table.value,
            [c.value for c in cols.elts if isinstance(c, ast.Constant)],
        )
    return found


def test_the_model_declares_the_constraint_the_migration_creates() -> None:
    """The fix. Without the declaration ``create_all`` builds ``relations``
    without the constraint, and every ``ON CONFLICT ON CONSTRAINT`` naming it
    is unplannable on that schema."""
    in_migration = _unique_constraints_in_migration()

    # Vacuity: an AST walk that finds nothing would make every assertion below
    # pass by default. The constraint has to be IN migration 001 for this test
    # to be about anything.
    assert _CONSTRAINT in in_migration, (
        f"{_CONSTRAINT} is no longer created by migration 001 — this test's "
        "premise is gone; re-check what owns the constraint before deleting it"
    )
    table, columns = in_migration[_CONSTRAINT]
    assert table == "relations"

    declared = {
        c.name: c
        for c in Relation.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert _CONSTRAINT in declared, (
        f"Relation does not declare {_CONSTRAINT}; a create_all-built schema "
        "will have no such constraint and relation_add's upsert cannot plan"
    )
    assert [c.name for c in declared[_CONSTRAINT].columns] == columns


def test_the_constraint_is_a_constraint_and_not_a_unique_index() -> None:
    """``ON CONFLICT ON CONSTRAINT`` reads ``pg_constraint``. A bare unique
    index — which is what ``Entity`` uses, because its key is over expressions
    ``UniqueConstraint`` cannot express — is not in that catalogue and would
    leave the upsert just as unplannable while looking like a fix."""
    assert not any(ix.name == _CONSTRAINT for ix in Relation.__table__.indexes), (
        f"{_CONSTRAINT} must be a UniqueConstraint, not an Index(unique=True)"
    )
    assert any(
        isinstance(c, UniqueConstraint) and c.name == _CONSTRAINT
        for c in Relation.__table__.constraints
    )


def test_the_key_stays_tenant_wide() -> None:
    """``fleet_id`` is deliberately out of the key, and three behaviours rest on
    it: a fleet-scoped ``infer_relations`` run can reinforce an edge a full run
    created, ``relation_add``'s fleet_id is first-writer-wins, and its re-fetch
    does not filter on fleet_id (it would raise ``NoResultFound``). Adding the
    column here would be a schema change that breaks all three silently."""
    constraint = next(
        c
        for c in Relation.__table__.constraints
        if isinstance(c, UniqueConstraint) and c.name == _CONSTRAINT
    )
    assert "fleet_id" not in {c.name for c in constraint.columns}


# ---------------------------------------------------------------------------
# The coverage the gap was costing: relation_add's ON CONFLICT path
# ---------------------------------------------------------------------------


def _t() -> str:
    return f"test-tenant-l03-relnk-{uuid4().hex[:8]}"


async def _entity(sc, tenant_id: str, name: str) -> str:
    row = await sc.create_entity(
        {
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": f"{name}-{uuid4().hex[:8]}",
        }
    )
    return row["id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_relation_add_upserts_on_the_natural_key(sc) -> None:
    """The behaviour the constraint carries, exercised under ``tests/`` for the
    first time: a second write of the same natural key updates the row it
    already has rather than inserting a second one or raising.

    Pre-fix this did not fail an assertion — it could not run. The statement
    named a constraint the create_all-built schema has never had.
    """
    tid = _t()
    a = await _entity(sc, tid, "l03-from")
    b = await _entity(sc, tid, "l03-to")

    first = await sc.create_relation(
        {
            "tenant_id": tid,
            "fleet_id": "fleet-first",
            "from_entity_id": a,
            "relation_type": "works_with",
            "to_entity_id": b,
            "weight": 0.25,
        }
    )
    second = await sc.create_relation(
        {
            "tenant_id": tid,
            "fleet_id": "fleet-second",
            "from_entity_id": a,
            "relation_type": "works_with",
            "to_entity_id": b,
            "weight": 0.75,
        }
    )

    # Same row, not a second one: the id is what callers hold on to.
    assert second["id"] == first["id"]
    assert second["weight"] == 0.75
    # First-writer-wins on fleet_id — it is not in the key and the SET clause
    # does not touch it.
    assert second["fleet_id"] == "fleet-first"

    async with get_session() as session:
        count = (
            await session.execute(
                sa_text(
                    "SELECT count(*) FROM relations WHERE tenant_id = :t "
                    "AND relation_type = 'works_with'"
                ),
                {"t": tid},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_different_relation_type_is_a_different_edge(sc) -> None:
    """The other half of the key's shape — without it, "upsert" would be
    indistinguishable from "one edge per entity pair"."""
    tid = _t()
    a = await _entity(sc, tid, "l03-multi-from")
    b = await _entity(sc, tid, "l03-multi-to")

    knows = await sc.create_relation(
        {
            "tenant_id": tid,
            "fleet_id": None,
            "from_entity_id": a,
            "relation_type": "knows",
            "to_entity_id": b,
            "weight": 0.5,
        }
    )
    reports_to = await sc.create_relation(
        {
            "tenant_id": tid,
            "fleet_id": None,
            "from_entity_id": a,
            "relation_type": "reports_to",
            "to_entity_id": b,
            "weight": 0.5,
        }
    )

    assert knows["id"] != reports_to["id"]

    async with get_session() as session:
        count = (
            await session.execute(
                sa_text("SELECT count(*) FROM relations WHERE tenant_id = :t"),
                {"t": tid},
            )
        ).scalar_one()
    assert count == 2
