"""Tests for CAURA-591 Part B role flag (writer / reader / hybrid).

Scope: the config flag + migration skip + PATCH/PUT/DELETE block on reader.
POST routes stay mounted — several are reads (scored-search, find-successors,
entity-overlap-candidates, similar-candidates, semantic-duplicate) and must
still work on the reader.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

# Apps are expensive to build (router imports, SQLAlchemy metadata) so cache
# one per role across the module. Tests only send HTTP requests — they never
# mutate app state — so sharing is safe.
_APP_CACHE: dict[str, object] = {}


def _build_app(role: str):
    from core_storage_api.app import create_app
    from core_storage_api.config import settings

    with patch.object(settings, "core_storage_role", role):
        return create_app()


@pytest.fixture
async def client_for_role():
    """Yield an AsyncClient bound to a core-storage-api app with the
    requested role. ``raise_app_exceptions=False`` so tests observe
    handler-level 500s as status codes instead of raised exceptions —
    relevant for the POST-read routes where an empty body crashes the
    handler but we only care whether the middleware let the request
    through."""
    clients: list[AsyncClient] = []

    async def _make(role: str) -> AsyncClient:
        app = _APP_CACHE.setdefault(role, _build_app(role))
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        client = AsyncClient(transport=transport, base_url="http://test")
        clients.append(client)
        return client

    yield _make
    for c in clients:
        await c.aclose()


async def test_hybrid_default_is_current_behavior() -> None:
    """Hybrid = original single-service shape. Safe default for OSS +
    any deploy that hasn't opted into the split."""
    from core_storage_api.config import settings

    assert settings.core_storage_role == "hybrid"


async def test_reader_blocks_patch(client_for_role) -> None:
    c = await client_for_role("reader")
    r = await c.patch(f"/api/v1/storage/memories/{uuid4()}", json={"title": "x"})
    assert r.status_code == 405
    assert "reader role" in r.json()["detail"]


async def test_reader_blocks_delete(client_for_role) -> None:
    c = await client_for_role("reader")
    r = await c.delete(f"/api/v1/storage/memories/{uuid4()}")
    assert r.status_code == 405


async def test_reader_allows_get(client_for_role) -> None:
    """GET /memories/{id} must reach the handler on reader — the handler
    then returns 404 (memory doesn't exist), not 405."""
    c = await client_for_role("reader")
    r = await c.get(f"/api/v1/storage/memories/{uuid4()}")
    assert r.status_code == 404


async def test_reader_allows_post_searches(client_for_role) -> None:
    """POST /scored-search is a read disguised as POST (body is large)
    and must stay reachable on reader."""
    c = await client_for_role("reader")
    r = await c.post("/api/v1/storage/memories/scored-search", json={})
    assert r.status_code != 405


async def test_reader_allows_post_semantic_duplicate(client_for_role) -> None:
    c = await client_for_role("reader")
    r = await c.post("/api/v1/storage/memories/semantic-duplicate", json={})
    assert r.status_code != 405


async def test_writer_allows_all_methods(client_for_role) -> None:
    c = await client_for_role("writer")
    r = await c.patch(f"/api/v1/storage/memories/{uuid4()}", json={})
    assert r.status_code != 405


async def test_hybrid_allows_all_methods(client_for_role) -> None:
    c = await client_for_role("hybrid")
    r = await c.patch(f"/api/v1/storage/memories/{uuid4()}", json={})
    assert r.status_code != 405


async def test_init_database_is_noop_for_reader() -> None:
    """Reader-role startup must skip Alembic entirely — the writer owns
    schema and the read-replica pool rejects DDL. Fail loudly if the
    reader branch ever falls through to the DB path."""
    from core_storage_api.config import settings
    from core_storage_api.database import init as db_init

    def _fail():
        raise RuntimeError("reader must not touch the DB during init")

    with (
        patch.object(settings, "core_storage_role", "reader"),
        patch.object(db_init, "get_engine", _fail),
    ):
        await db_init.init_database()  # must not raise — early return skips get_engine


async def test_init_database_runs_for_writer() -> None:
    """Writer-role startup must reach get_engine() (the Alembic gateway)."""
    from core_storage_api.config import settings
    from core_storage_api.database import init as db_init

    def _fake_get_engine():
        raise RuntimeError(
            "stop before alembic — we only need to prove get_engine was reached"
        )

    with (
        patch.object(settings, "core_storage_role", "writer"),
        patch.object(db_init, "get_engine", _fake_get_engine),
        pytest.raises(RuntimeError, match="stop before alembic"),
    ):
        await db_init.init_database()
