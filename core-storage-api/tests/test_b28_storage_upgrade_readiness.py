"""Storage answers for the database it has (audit batch B28).

Each test names the finding it pins:

- L-37: a body that is not valid UTF-8 fails before JSON parsing, with
  ``UnicodeDecodeError``, which is not a ``JSONDecodeError``. The app-wide 422
  handler caught only the latter, so every router parsing its body inline
  answered 500, which core-api turns into a retryable 503. Two routers caught
  it locally.
- L-139: a reader given ``READ_DATABASE_URL`` sent only its ``get_read_session``
  call sites there. The rest, about two thirds, opened the writer engine, which
  still connected to ``DATABASE_URL``: the primary, on a reader set up from the
  README. Shutdown disposed only the writer engine, and on a reader nothing.
- L-145: ``preflight_012`` promised exit 0 for a database already past 012, and
  read the head, but decided on the row count alone. Every re-embedded database
  past 012 was reported destructive (exit 1), with the opt-in command printed.

L-97 is pinned beside the JSON-type check it completes, in
``test_models_match_the_migrated_schema.py``.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from core_storage_api.config import settings
from core_storage_api.database import init as db_init

pytestmark = pytest.mark.asyncio

_PRIMARY = "postgresql+asyncpg://caura:caura@primary.invalid:5432/caura"
_REPLICA = "postgresql+asyncpg://caura:caura@replica.invalid:5432/caura"


# --- L-37: a body that is not UTF-8 is the caller's error ---------------------------------------------


@pytest.fixture
async def lenient_client():
    """The app with its errors as responses, so an unhandled one reads as a 500."""
    from core_storage_api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"X-Storage-Secret": settings.core_storage_shared_secret.get_secret_value()},
    ) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/storage/agents",
        "/api/v1/storage/agents/create-only",
        # These two caught it locally; the app-wide handler now answers for them.
        "/api/v1/storage/tenant-suppression",
        "/api/v1/storage/preview/tenant-counts",
    ],
)
async def test_l37_a_body_that_is_not_utf8_is_a_422(lenient_client, path) -> None:
    resp = await lenient_client.post(
        path, content=b'{"tenant_id": "\xff"}', headers={"content-type": "application/json"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": "request body must be valid JSON"}


async def test_l37_a_decode_failure_past_the_body_stays_a_500(lenient_client, monkeypatch) -> None:
    """Only the body's decode is the caller's error. One raised while serving a
    valid request is the server's, and answering it 422 would tell core-api not
    to retry and keep it out of the 500s an alert watches."""
    from core_storage_api.services.postgres_service import PostgresService

    async def undecodable(self, tenant_id: str) -> dict[str, int]:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(PostgresService, "count_tenant_data", undecodable)

    resp = await lenient_client.post("/api/v1/storage/preview/tenant-counts", json={"tenant_id": "b28"})

    assert resp.status_code == 500, resp.text


# --- L-139: a reader's sessions all reach its read database -------------------------------------------


@pytest.fixture
def fresh_engines(monkeypatch):
    """Build engines from the settings each test patches, and put back the suite's."""
    import core_storage_api.services.postgres_service as pg_svc

    for module, name in (
        (db_init, "_engine"),
        (db_init, "_read_engine"),
        (pg_svc, "_session_factory"),
        (pg_svc, "_read_session_factory"),
    ):
        monkeypatch.setattr(module, name, None)
    monkeypatch.setattr(settings, "database_url", SecretStr(_PRIMARY))
    monkeypatch.setattr(settings, "read_database_url", SecretStr(_REPLICA))
    return pg_svc


async def test_l139_a_reader_given_a_read_database_opens_every_session_there(
    monkeypatch, fresh_engines
) -> None:
    monkeypatch.setattr(settings, "core_storage_role", "reader")

    writer_session = fresh_engines._get_session_factory()()
    read_session = fresh_engines._get_read_session_factory()()
    try:
        assert writer_session.bind.url.host == "replica.invalid"
        # One pool on a reader, not two against the same database.
        assert writer_session.bind is read_session.bind
    finally:
        await writer_session.close()
        await read_session.close()


@pytest.mark.parametrize("role", ["writer", "hybrid"])
async def test_l139_a_writer_still_writes_to_the_primary(monkeypatch, fresh_engines, role) -> None:
    """The control: only the reader follows ``read_database_url`` everywhere."""
    monkeypatch.setattr(settings, "core_storage_role", role)

    assert db_init.get_engine().url.host == "primary.invalid"
    assert db_init.get_read_engine().url.host == "replica.invalid"


@pytest.mark.parametrize("role", ["reader", "writer", "hybrid"])
async def test_l139_shutdown_disposes_every_engine_it_built(monkeypatch, role) -> None:
    from core_storage_api import app as app_module

    writer, reader = Mock(dispose=AsyncMock()), Mock(dispose=AsyncMock())
    monkeypatch.setattr(db_init, "_engine", writer)
    monkeypatch.setattr(db_init, "_read_engine", reader)
    monkeypatch.setattr(settings, "core_storage_role", role)
    monkeypatch.setattr(app_module, "init_database", AsyncMock())
    monkeypatch.setattr(app_module, "report_schema_drift", AsyncMock())

    async with app_module.lifespan(FastAPI()):
        pass

    writer.dispose.assert_awaited_once()
    reader.dispose.assert_awaited_once()


# --- L-145: a database past 012 is not reported destructive -------------------------------------------


class _Rows:
    def __init__(self, value: object) -> None:
        self._value = value

    def first(self) -> tuple[object]:
        return (self._value,)

    def scalar_one(self) -> object:
        return self._value


class _Database:
    """Stands in for ``_gather``'s engine: the head is ``head`` and every count is 5."""

    def __init__(self, head: str) -> None:
        self.head = head
        self.statements: list[str] = []

    @contextlib.asynccontextmanager
    async def connect(self):
        yield self

    async def execute(self, statement) -> _Rows:
        sql = str(statement)
        self.statements.append(sql)
        return _Rows(self.head if "alembic_version" in sql else 5)

    async def dispose(self) -> None:
        pass


async def _preflight(monkeypatch, head: str, *flags: str) -> tuple[int, _Database]:
    from core_storage_api.scripts import preflight_012

    database = _Database(head)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", lambda *a, **k: database)
    code = await preflight_012._amain(["--dsn", "postgresql+asyncpg://x/y", *flags])
    return code, database


async def test_l145_a_database_past_012_is_safe_and_says_so(monkeypatch, capsys) -> None:
    code, database = await _preflight(monkeypatch, "065")

    out = capsys.readouterr().out
    assert code == 0, out
    assert "already applied" in out
    assert "CAURA_RUN_DESTRUCTIVE_MIGRATIONS" not in out
    # Decided from the head alone: no count over a production-sized table.
    assert not [sql for sql in database.statements if "COUNT" in sql]


async def test_l145_the_json_report_says_012_is_applied(monkeypatch, capsys) -> None:
    code, _ = await _preflight(monkeypatch, "012", "--json")

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == 0
    assert payload["migration_applied"] is True
    assert payload["safety_gate_would_trip"] is False


async def test_l145_a_database_before_012_with_vectors_is_still_destructive(monkeypatch, capsys) -> None:
    """The control: the verdict the preflight exists for is unchanged."""
    code, _ = await _preflight(monkeypatch, "011")

    assert code == 1
    assert "CAURA_RUN_DESTRUCTIVE_MIGRATIONS=true" in capsys.readouterr().out
