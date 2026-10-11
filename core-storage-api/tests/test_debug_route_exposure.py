"""The pg_locks debug route is opt-in and lists only storage's own sessions (L-75).

``GET /_debug/pg_locks`` returned client sessions across the whole instance, each
with its database, role, application name and latest statement, to any holder
of the storage shared secret, on every deployment. The Cloud Run IAM gate its
docstring named exists only in SaaS. It now answers only when
``CORE_STORAGE_DEBUG_ENDPOINTS`` is on, and lists only sessions of storage's own
database role, so another service's sessions on the same database (the
platform-storage-api, an operator's psql) stay out of it.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from core_storage_api.config import settings
from core_storage_api.routers.debug import pg_locks_snapshot
from core_storage_api.services.postgres_service import get_session
from tests.test_integration import PREFIX


async def test_the_debug_route_is_off_by_default(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/_debug/pg_locks")
    assert resp.status_code == 404


async def test_the_snapshot_lists_only_storages_own_sessions() -> None:
    """Another role's session, idle in a transaction, waits on ``ClientRead``,
    so the unfiltered snapshot listed it; storage's own snapshot must not."""
    role = f"l75_other_{uuid.uuid4().hex[:8]}"
    async with get_session() as s:
        await s.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'l75-probe'"))
    url = make_url(settings.database_url.get_secret_value()).set(username=role, password="l75-probe")
    other = create_async_engine(url)
    try:
        async with other.connect() as conn:
            await conn.execute(text("SELECT 1"))
            async with get_session() as s:
                body = await pg_locks_snapshot(session=s)
        assert role not in {row["usename"] for row in body["rows"]}
    finally:
        await other.dispose()
        async with get_session() as s:
            await s.execute(text(f"DROP ROLE {role}"))
