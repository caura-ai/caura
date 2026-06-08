"""Tests for CAURA-591 Part A — reader/writer engine split."""

from unittest.mock import patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_engines():
    """Force each test to build fresh engines from the patched settings
    — the module-level singletons would otherwise leak across tests."""
    from core_storage_api.database import init as db_init

    db_init._engine = None
    db_init._read_engine = None
    yield
    db_init._engine = None
    db_init._read_engine = None


async def test_read_engine_is_writer_when_read_url_empty():
    from core_storage_api.config import settings
    from core_storage_api.database import init as db_init

    with patch.object(settings, "read_database_url", ""):
        writer = db_init.get_engine()
        reader = db_init.get_read_engine()
    assert writer is reader


async def test_read_engine_is_distinct_when_read_url_set():
    from core_storage_api.config import settings
    from core_storage_api.database import init as db_init

    with patch.object(
        settings,
        "read_database_url",
        "postgresql+asyncpg://reader:changeme@replica:5432/memclaw",
    ):
        writer = db_init.get_engine()
        reader = db_init.get_read_engine()
    assert writer is not reader
    assert str(writer.url) != str(reader.url)


async def test_read_methods_use_read_session_factory(sc, tenant_id):
    """Read methods (memory_get_by_id, memory_count_active, etc.) must
    consult the reader factory; spy both and assert the split."""
    import core_storage_api.services.postgres_service as pg_svc

    writer_calls: list[str] = []
    reader_calls: list[str] = []

    real_writer = pg_svc._session_factory
    real_reader = pg_svc._read_session_factory

    class _SpyFactory:
        def __init__(self, label: str, delegate):
            self.label = label
            self.delegate = delegate

        def __call__(self):
            if self.label == "writer":
                writer_calls.append("w")
            else:
                reader_calls.append("r")
            return self.delegate()

    pg_svc._session_factory = _SpyFactory("writer", real_writer)  # type: ignore[assignment]
    pg_svc._read_session_factory = _SpyFactory("reader", real_reader)  # type: ignore[assignment]
    try:
        svc = pg_svc.PostgresService()
        await svc.memory_get_by_id(uuid4())
        await svc.memory_count_active(tenant_id=tenant_id)
    finally:
        pg_svc._session_factory = real_writer
        pg_svc._read_session_factory = real_reader

    assert len(reader_calls) > 0
    assert writer_calls == []
