"""The routes whose callers read no vector send none (audit 2026-10-01, B33: L-187, L-188, L-189).

``orm_to_dict`` with ``MEMORY_FIELDS`` put each row's 1024-float ``embedding`` and its ``search_vector``
on the wire, ~20 KB of JSON a row that storage serialised and core-api parsed for nothing. ``POST
/memories`` echoed the vector core-api had just sent (L-187). Search's by-id load and successor lookup
(L-188), the three contradiction-candidate routes and bulk-get (L-189) sent theirs to callers that read
none. Each now serialises ``MEMORY_LIST_FIELDS``. Bulk-get adds the embedding when asked, for the bulk
re-embed, which keeps a vector that landed first.

No database: each service method is stubbed and the request goes through the real route, as in
``test_scored_search_row_serialization.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from common.constants import VECTOR_DIM
from core_storage_api.app import create_app
from core_storage_api.config import settings
from core_storage_api.routers import memories as memories_router
from core_storage_api.schemas import MEMORY_FIELDS, MEMORY_LIST_FIELDS

pytestmark = pytest.mark.unit

_PREFIX = "/api/v1/storage"
_TENANT = "t-reads-send-no-vectors"
_ID = uuid.uuid4()
_EMBEDDING = [0.1] * VECTOR_DIM
_VECTORS = {"embedding", "search_vector"}


def _row() -> SimpleNamespace:
    """A row with both large columns loaded, as the full select loaded them."""
    row = SimpleNamespace(**dict.fromkeys(MEMORY_FIELDS))
    row.id = _ID
    row.tenant_id = _TENANT
    row.content = "a row with both large columns loaded"
    row.embedding = _EMBEDDING
    row.search_vector = "'column':6 'larg':5 'load':7 'row':2"
    return row


def _other() -> str:
    return str(uuid.uuid4())


# route: (service method, what it answers, HTTP method, path, request)
_ROUTES: dict[str, tuple[str, Any, str, str, dict]] = {
    "L-187 create": ("memory_add", _row(), "POST", "/memories", {"json": {"tenant_id": _TENANT}}),
    "L-188 load-by-ids": (
        "memory_load_by_ids",
        [_row()],
        "POST",
        "/memories/load-by-ids",
        {"json": {"tenant_id": _TENANT, "memory_ids": [str(_ID)]}},
    ),
    "L-188 find-successors": (
        "memory_find_successors",
        [(_row(), uuid.uuid4())],
        "POST",
        "/memories/find-successors",
        {"json": {"tenant_id": _TENANT, "supersedes_ids": [_other()]}},
    ),
    "L-189 similar-candidates": (
        "memory_find_similar_candidates",
        [_row()],
        "POST",
        "/memories/similar-candidates",
        {"json": {"tenant_id": _TENANT, "embedding": _EMBEDDING, "memory_id": _other()}},
    ),
    "L-189 entity-overlap-candidates": (
        "memory_find_entity_overlap_candidates",
        [_row()],
        "POST",
        "/memories/entity-overlap-candidates",
        {"json": {"tenant_id": _TENANT, "memory_id": _other()}},
    ),
    "L-189 rdf-conflicts": (
        "memory_find_rdf_conflicts",
        [_row()],
        "GET",
        "/memories/rdf-conflicts",
        {"params": {"tenant_id": _TENANT, "subject_entity_id": _other(), "predicate": "status"}},
    ),
    "L-189 bulk-get": (
        "memory_get_memories_by_ids",
        {_ID: _row()},
        "POST",
        "/memories/bulk-get",
        {"json": {"tenant_id": _TENANT, "ids": [str(_ID)]}},
    ),
}


async def _rows(
    monkeypatch: pytest.MonkeyPatch, route: str, *, calls: list[dict] | None = None, **request: Any
) -> list[dict]:
    """Call ``route`` with its service method answering a full row; the rows it sends back."""
    method, answer, verb, path, default_request = _ROUTES[route]

    async def _stub(*_args: Any, **kwargs: Any) -> Any:
        if calls is not None:
            calls.append(kwargs)
        return answer

    monkeypatch.setattr(memories_router._svc, method, _stub)
    headers = {"X-Storage-Secret": settings.core_storage_shared_secret.get_secret_value()}
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        resp = await client.request(verb, f"{_PREFIX}{path}", headers=headers, **(request or default_request))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body if isinstance(body, list) else [body]


@pytest.mark.parametrize("route", list(_ROUTES))
async def test_the_route_sends_no_vectors(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    (row,) = await _rows(monkeypatch, route)

    assert not _VECTORS & set(row), f"{route} sent {sorted(_VECTORS & set(row))}"
    assert set(MEMORY_LIST_FIELDS) <= set(row), f"{route} lost {sorted(set(MEMORY_LIST_FIELDS) - set(row))}"


async def test_bulk_get_sends_the_embedding_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """For the bulk re-embed, and the embedding only: no caller reads the tsvector."""
    calls: list[dict] = []

    (row,) = await _rows(
        monkeypatch,
        "L-189 bulk-get",
        calls=calls,
        json={"tenant_id": _TENANT, "ids": [str(_ID)], "with_embedding": True},
    )

    assert calls == [{"tenant_id": _TENANT, "with_embedding": True}]
    assert row["embedding"] == _EMBEDDING
    assert "search_vector" not in row
