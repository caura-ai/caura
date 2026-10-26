"""Storage validates work bounds independently of its callers."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from core_storage_api.routers import (
    audit,
    documents,
    entities,
    fleet,
    insights,
    memories,
    reports,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class ReachedStorage(Exception):
    pass


CASES = [
    (
        documents,
        "GET",
        "/documents/notes",
        "document_list_by_collection",
        {},
        "limit",
        5000,
    ),
    (
        documents,
        "GET",
        "/documents/notes",
        "document_list_by_collection",
        {},
        "offset",
        10000,
    ),
    (
        documents,
        "POST",
        "/documents/list",
        "document_list_by_collection",
        {"collection": "notes"},
        "limit",
        5000,
    ),
    (
        documents,
        "POST",
        "/documents/list",
        "document_list_by_collection",
        {"collection": "notes"},
        "offset",
        10000,
    ),
    (
        documents,
        "POST",
        "/documents/query",
        "document_query",
        {"collection": "notes"},
        "limit",
        5000,
    ),
    (
        documents,
        "POST",
        "/documents/query",
        "document_query",
        {"collection": "notes"},
        "offset",
        10000,
    ),
    (
        documents,
        "POST",
        "/documents/search",
        "document_search",
        {"query_embedding": []},
        "top_k",
        5000,
    ),
    (audit, "GET", "/audit-logs", "audit_list_by_tenant", {}, "limit", 5000),
    (audit, "GET", "/audit-logs", "audit_list_by_tenant", {}, "offset", 10000),
    (fleet, "GET", "/fleet/commands", "fleet_list_commands", {}, "limit", 10000),
    (entities, "GET", "/entities", "entity_list", {}, "limit", 5000),
    (entities, "GET", "/entities", "entity_list", {}, "offset", 10000),
    (
        entities,
        "POST",
        "/entities/embedding-similarity",
        "entity_find_by_embedding_similarity",
        {"entity_type": "person", "name_embedding": []},
        "limit",
        5000,
    ),
    (
        entities,
        "POST",
        "/entities/expand-graph",
        "entity_expand_graph",
        {"seed_entity_ids": []},
        "max_hops",
        3,
    ),
    (reports, "GET", "/reports", "report_list_by_tenant", {}, "limit", 5000),
    (reports, "GET", "/reports", "report_list_by_tenant", {}, "offset", 10000),
    (
        insights,
        "POST",
        "/insights/contradictions",
        "insights_query_contradictions",
        {"agent_id": "a", "scope": "agent"},
        "max_memories",
        5000,
    ),
    (
        insights,
        "POST",
        "/insights/discover-sample",
        "insights_discover_sample",
        {"agent_id": "a", "scope": "agent"},
        "sample_size",
        5000,
    ),
    (
        memories,
        "POST",
        "/memories/near-duplicates",
        "memory_find_near_duplicate_pairs",
        {},
        "neighbor_limit",
        100,
    ),
    (
        memories,
        "POST",
        "/memories/near-duplicates",
        "memory_find_near_duplicate_pairs",
        {},
        "batch_size",
        5000,
    ),
    (memories, "POST", "/memories/admin-list", "memory_admin_list", {}, "limit", 5000),
    (
        memories,
        "POST",
        "/memories/admin-list",
        "memory_admin_list",
        {},
        "offset",
        10000,
    ),
]


@pytest.mark.parametrize("module,method,path,service,body,field,cap", CASES)
async def test_storage_work_limit_boundary(
    monkeypatch, module, method, path, service, body, field, cap
):
    downstream = AsyncMock(side_effect=ReachedStorage)
    monkeypatch.setattr(module._svc, service, downstream)
    app = FastAPI()
    app.include_router(module.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:

        async def call(value):
            payload = {"tenant_id": "t", **body, field: value}
            return await c.request(
                method,
                path,
                **({"params": payload} if method == "GET" else {"json": payload}),
            )

        with pytest.raises(ReachedStorage):
            await call(cap)
        for value in (cap + 1, -1):
            downstream.reset_mock()
            response = await call(value)
            assert response.status_code == 422, response.text
            downstream.assert_not_awaited()


@pytest.mark.parametrize(
    "module,path,service,body,field",
    [
        (
            documents,
            "/documents",
            "document_upsert",
            {"collection": "notes", "doc_id": "one"},
            "data",
        ),
        (
            documents,
            "/documents/upsert-xmax",
            "document_upsert_returning_xmax",
            {"collection": "notes", "doc_id": "one"},
            "data",
        ),
        (
            entities,
            "/entities",
            "entity_add",
            {"entity_type": "person", "canonical_name": "Name"},
            "attributes",
        ),
        (memories, "/memories", "memory_add", {"content": "Text"}, "metadata_"),
    ],
)
async def test_storage_json_boundaries(monkeypatch, module, path, service, body, field):
    downstream = AsyncMock(side_effect=ReachedStorage)
    monkeypatch.setattr(module._svc, service, downstream)
    app = FastAPI()
    app.include_router(module.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        with pytest.raises(ReachedStorage):
            await c.post(
                path, json={"tenant_id": "t", **body, field: {"x": "x" * (65536 - 8)}}
            )
        downstream.reset_mock()
        response = await c.post(
            path, json={"tenant_id": "t", **body, field: {"x": "x" * (65536 - 7)}}
        )
        assert response.status_code == 422
        downstream.assert_not_awaited()


async def test_field_signature_statement_timeout_is_retryable(monkeypatch, caplog):
    from sqlalchemy.exc import DBAPIError

    class QueryCanceled(Exception):
        sqlstate = "57014"

    monkeypatch.setattr(
        memories._svc,
        "memory_field_signature",
        AsyncMock(side_effect=DBAPIError("signature", {}, QueryCanceled(), False)),
    )
    app = FastAPI()
    app.include_router(memories.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post("/memories/field-signature", json={"tenant_id": "t"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert any(
        r.levelname == "WARNING" and "signature" in r.message.lower()
        for r in caplog.records
    )


async def test_trusted_dedup_sweep_scans_more_than_ten_thousand_rows(monkeypatch):
    from types import SimpleNamespace
    from uuid import UUID

    from core_api.services import crystallizer_service

    offsets = []

    async def page(**body):
        offset = body["offset"]
        offsets.append(offset)
        return [
            (UUID(int=i + 1), None, None)
            for i in range(offset, min(offset + body["batch_size"], 10501))
        ]

    monkeypatch.setattr(memories._svc, "memory_find_near_duplicate_pairs", page)
    app = FastAPI()
    app.include_router(memories.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:

        async def check(body):
            response = await c.post("/memories/near-duplicates", json=body)
            response.raise_for_status()
            return response.json()

        mark = AsyncMock()
        monkeypatch.setattr(
            crystallizer_service,
            "get_storage_client",
            lambda: SimpleNamespace(
                check_near_duplicates=check, mark_dedup_checked=mark
            ),
        )
        await crystallizer_service._check_near_duplicates("t", None, threshold=0.95)
        assert max(offsets) > 10000
        assert len(mark.await_args.args[0]) == 10501
        response = await c.post(
            "/memories/near-duplicates", json={"tenant_id": "t", "offset": -1}
        )
        assert response.status_code == 422
