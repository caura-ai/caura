"""The by-id detail read binds tenant, deletion and hold in its own statement.

``GET /memories/{id}`` reaches storage's ``memory_get_detail``. It selected the
row by id alone and compared ``tenant_id``, ``deleted_at`` and the held status
in Python afterwards: correct, but another tenant's row crossed the database
boundary first, and the protection was a comparison a later edit could drop
with no test failing. ``memory_get_by_id_for_tenant`` was moved off that shape
for the same reason. These read the statement itself, so dropping a predicate
fails here whatever the Python around it does.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from core_storage_api.services import postgres_service as ps

pytestmark = pytest.mark.unit


def _record_statements(monkeypatch) -> list:
    """Serve the detail read from a session that records what it executes."""
    statements: list = []

    @contextlib.asynccontextmanager
    async def _session():
        session = AsyncMock()

        async def _execute(stmt, *a, **k):
            statements.append(stmt)
            result = MagicMock()
            result.one_or_none.return_value = None
            return result

        session.execute.side_effect = _execute
        yield session

    monkeypatch.setattr(ps, "get_session", _session)
    monkeypatch.setattr(ps, "get_read_session", _session)
    return statements


async def _where(monkeypatch, *, include_held: bool) -> str:
    """The WHERE clause of the statement that fetches the memory row."""
    statements = _record_statements(monkeypatch)

    found = await ps.PostgresService().memory_get_detail(
        uuid4(), "tenant-a", include_held=include_held
    )

    assert found is None
    assert statements, "the detail read issued no statement"
    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    return sql.split("WHERE", 1)[1]


async def test_the_detail_statement_binds_the_tenant_and_excludes_deleted_rows(
    monkeypatch,
):
    where = await _where(monkeypatch, include_held=False)

    assert "memories.tenant_id = 'tenant-a'" in where
    assert "memories.deleted_at IS NULL" in where


async def test_the_detail_statement_excludes_held_rows_unless_asked(monkeypatch):
    held = await _where(monkeypatch, include_held=False)
    with_held = await _where(monkeypatch, include_held=True)

    assert "memories.status != 'quarantined'" in held
    assert "memories.status" not in with_held
    assert "memories.tenant_id = 'tenant-a'" in with_held
