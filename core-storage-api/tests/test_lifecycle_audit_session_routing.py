"""Writer-session regression coverage for exact lifecycle audit polling."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

import core_storage_api.services.postgres_service as postgres_service

pytestmark = pytest.mark.asyncio


async def test_exact_audit_read_uses_writer_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Result:
        @staticmethod
        def scalar_one_or_none() -> None:
            return None

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

    @asynccontextmanager
    async def _writer_session():
        calls.append("writer")
        yield _Session()

    @asynccontextmanager
    async def _reader_session():
        calls.append("reader")
        yield _Session()

    monkeypatch.setattr(postgres_service, "get_session", _writer_session)
    monkeypatch.setattr(postgres_service, "get_read_session", _reader_session)

    assert (
        await postgres_service.PostgresService().lifecycle_audit_get(
            41,
            org_id="canary",
        )
        is None
    )
    assert calls == ["writer"]
