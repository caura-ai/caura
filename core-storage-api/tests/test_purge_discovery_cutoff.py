from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

from core_storage_api.services import postgres_service
from core_storage_api.services.postgres_service import PostgresService


def test_purge_discovery_uses_minimum_retention_cutoff(monkeypatch):
    captured = []

    class Session:
        async def execute(self, statement):
            captured.append(statement)
            return SimpleNamespace(all=lambda: [])

    @asynccontextmanager
    async def open_session():
        yield Session()

    monkeypatch.setattr(postgres_service, "get_read_session", open_session)

    import asyncio

    asyncio.run(PostgresService().tenants_list_purgeable())

    cutoff_days = next(
        value.days
        for value in captured[0].compile().params.values()
        if hasattr(value, "days")
    )
    assert cutoff_days == 1, f"expected 1-day discovery cutoff, got {cutoff_days}"
